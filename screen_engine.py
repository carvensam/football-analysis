# -*- coding: utf-8 -*-
"""
篩查引擎 —— 為一場目標賽事在歷史數據庫做 18 項篩查。
口徑：
- 目標賽事的參照盤 = 尾盤（closing；未賽場次即現時最新盤）
- 第 1-5 項：盤口（讓球數+讓球方）100% 相同，主、客水位各 ±0.03
- 第 6/7 項：勝/和/負比例每項相差 ≤7 個百分點
- 第 8/9 項：入球、失球、得失球差各 ±3
- 第 10-13 項：輸出盤口分佈 + 每個盤口下的上盤水位分佈 + 盤口結果（上/下/走）
- 第 12/13 項：排名各 ±2
- 第 14 項：用第 12 項樣本，找分佈最多盤口及上盤勝率最接近 50% 的盤口，
            計算與今場尾盤的淨差距（以今場角度表達：今場讓深咗／讓淺咗）
- 第 15 項：第 12 項篩選（排名各±2）＋尾盤盤口同今場 100% 相同，
            按上盤水位分區列出 上盤率／下盤率／走盤率（結果只計 贏1／走盤／輸1）
- 第 16 項：今次主隊對上一次比賽（主/客角色＋讓/受讓＋讓球數 完全相同）嘅歷史場次
            → 今次主場歷史盤口分佈 ＋ 各水位 上/下/走 率
- 第 17 項：用第 16 項樣本，找分佈最多盤口及上盤勝率最接近 50% 的盤口，
            計算與今場尾盤的淨差距
- 第 18 項：組合篩查（剔選第1-17項，AND 全部條件；第14、17項除外）
- 每項分「全庫」及「同聯賽」兩套結果
"""
import os
import sqlite3
import threading
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(PARENT_DIR, 'football.db')
import sys
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

ZONES = ['≤0.69', '0.70-0.74', '0.75-0.79', '0.80-0.84', '0.85-0.89',
         '0.90-0.94', '0.95-0.99', '1.00-1.04', '1.05-1.09', '≥1.10']


def zone_idx(water):
    if water is None or (isinstance(water, float) and np.isnan(water)):
        return None
    c = int(round(water * 100))
    if c < 70: return 0
    if c < 110: return (c - 70) // 5 + 1
    return 9


def _h_name(h):
    """深盤中文名（>2.5）：如 2.75→兩球半/三、3.0→三球、3.25→三/三球半"""
    q = int(round(h * 4))
    base, frac = q // 4, q % 4
    cn = '零一二三四五六七八九'
    if base >= len(cn):
        return f'{h:g}'
    b = '兩' if base == 2 else cn[base]   # 香港波盤習慣：2 用「兩」
    if frac == 0:
        return b + '球'
    if frac == 2:
        return '半球' if base == 0 else ('球半' if base == 1 else b + '球半')
    if frac == 1:
        return ('平/半' if base == 0 else f'{b}/{b}球半')
    return ('半/一' if base == 0 else
            (f'球半/{cn[base + 1]}' if base == 1 else f'{b}球半/{cn[base + 1]}'))


def fmt_line(h, giver):
    """0.5,'home' -> 主讓半球；0.25,'away' -> 客讓平/半；0 -> 平手"""
    names = {0: '平手', 0.25: '平/半', 0.5: '半球', 0.75: '半/一', 1.0: '一球',
             1.25: '一/球半', 1.5: '球半', 1.75: '球半/兩', 2.0: '兩球',
             2.25: '兩/兩球半', 2.5: '兩球半'}
    if h == 0 or giver is None:
        return '平手'
    name = names.get(round(h, 2)) or _h_name(h)
    return ('主讓' if giver == 'home' else '客讓') + name


POOL_SQL = """
SELECT m.id, c.req_name AS league, c.category AS cat, m.home_id, m.away_id, m.kickoff,
       m.home_score AS hs, m.away_score AS aws,
       oi.handicap AS i_h, oi.giver AS i_g, oi.home_odds AS i_ho, oi.away_odds AS i_ao,
       o4.handicap AS f_h, o4.giver AS f_g, o4.home_odds AS f_ho, o4.away_odds AS f_ao,
       oc.handicap AS c_h, oc.giver AS c_g, oc.home_odds AS c_ho, oc.away_odds AS c_ao,
       p.home_home_w, p.home_home_d, p.home_home_l,
       p.home_total_w, p.home_total_d, p.home_total_l,
       p.home_home_gf, p.home_home_ga, p.home_total_gf, p.home_total_ga,
       p.home_home_rank, p.home_total_rank,
       p.away_away_w, p.away_away_d, p.away_away_l,
       p.away_total_w, p.away_total_d, p.away_total_l,
       p.away_away_gf, p.away_away_ga, p.away_total_gf, p.away_total_ga,
       p.away_away_rank, p.away_total_rank
FROM matches m
JOIN seasons s ON s.id = m.season_id
JOIN competitions c ON c.titan_id = s.titan_id
JOIN odds_asian oi ON oi.match_id = m.id AND oi.label = 'initial'  AND oi.company_id = 12
JOIN odds_asian o4 ON o4.match_id = m.id AND o4.label = 'pre_4h'   AND o4.company_id = 12
JOIN odds_asian oc ON oc.match_id = m.id AND oc.label = 'closing'  AND oc.company_id = 12
JOIN match_prestandings p ON p.match_id = m.id
WHERE m.home_score IS NOT NULL AND oc.handicap IS NOT NULL
"""

_pool_cache = {'df': None, 'prev_h2h': None, 'pair_latest': None, 'ts': 0}
_pool_lock = threading.Lock()


def load_pool(conn, max_age=1800):
    """載入歷史數據池（約 7 萬場），結果記憶 30 分鐘；多執行緒同時呼叫只會建立一次"""
    import time
    now = time.time()
    if _pool_cache['df'] is not None and now - _pool_cache['ts'] < max_age:
        return _pool_cache['df'], _pool_cache['prev_h2h']
    with _pool_lock:
        now = time.time()
        if _pool_cache['df'] is not None and now - _pool_cache['ts'] < max_age:
            return _pool_cache['df'], _pool_cache['prev_h2h']
        t0 = time.time()
        df = pd.read_sql(POOL_SQL, conn)
        # 上/下/走 結果（讓球盤：上=讓球方贏；平手盤：上=主勝）
        # 主讓：hs-aws-h>0 上盤贏；客讓：aws-hs-h>0 上盤贏；平手：hs-aws>0 主勝
        # （2026-09-19 修正：客讓分支原本正負號顛倒；平手唔可以跌入客讓分支）
        giver_home = df['c_g'] == 'home'
        giver_away = df['c_g'] == 'away'
        margin = np.where(
            giver_home, df['hs'] - df['aws'] - df['c_h'],
            np.where(giver_away, df['aws'] - df['hs'] - df['c_h'],
                     df['hs'] - df['aws']))
        df['res'] = np.where(margin > 0, 'A', np.where(margin < 0, 'B', 'P'))  # A=上盤/主勝 B=下盤/客勝 P=走
        # 上盤水位（讓球方水位；平手取低水方）
        df['up_water'] = np.where(df['c_g'] == 'home', df['c_ho'],
                                  np.where(df['c_g'] == 'away', df['c_ao'],
                                           df[['c_ho', 'c_ao']].min(axis=1)))
        df['up_zone'] = df['up_water'].map(zone_idx)
        # 勝率/和率/負率
        for pre in ('home_home', 'home_total', 'away_away', 'away_total'):
            played = df[f'{pre}_w'] + df[f'{pre}_d'] + df[f'{pre}_l']
            df[f'{pre}_p'] = played
            df[f'{pre}_wp'] = np.where(played > 0, df[f'{pre}_w'] / played, np.nan)
            df[f'{pre}_dp'] = np.where(played > 0, df[f'{pre}_d'] / played, np.nan)
            df[f'{pre}_lp'] = np.where(played > 0, df[f'{pre}_l'] / played, np.nan)
            df[f'{pre}_gd'] = df[f'{pre}_gf'] - df[f'{pre}_ga']
        # 每場的「對上一次對賽」尾盤（同兩隊，不限主客方向）——向量化：pair 內按時間 shift(1)
        allm = pd.read_sql(
            "SELECT m.id, m.home_id, m.away_id, m.kickoff, oc.handicap, oc.giver, "
            "oc.home_odds, oc.away_odds FROM matches m "
            "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' AND oc.company_id=12 "
            "WHERE m.home_score IS NOT NULL AND oc.handicap IS NOT NULL", conn)
        prev = {}
        pair_latest = {}
        if len(allm):
            a = np.minimum(allm['home_id'].to_numpy(), allm['away_id'].to_numpy())
            b = np.maximum(allm['home_id'].to_numpy(), allm['away_id'].to_numpy())
            allm = allm.assign(_pa=a, _pb=b).sort_values(['kickoff', 'id'], kind='mergesort')
            grp = allm.groupby(['_pa', '_pb'], sort=False)
            allm['_pv_h'] = grp['handicap'].shift(1)
            allm['_pv_g'] = grp['giver'].shift(1)
            allm['_pv_ho'] = grp['home_odds'].shift(1)
            allm['_pv_ao'] = grp['away_odds'].shift(1)
            has = allm['_pv_h'].notna()
            if has.any():
                sub = allm.loc[has]
                prev = dict(zip(sub['id'].astype(int).tolist(),
                                zip(sub['_pv_h'], sub['_pv_g'], sub['_pv_ho'], sub['_pv_ao'])))
                mprev = allm.loc[has, ['id', '_pv_h', '_pv_g', '_pv_ho', '_pv_ao']]
                df = df.merge(mprev, on='id', how='left')
                df = df.rename(columns={'_pv_h': 'pv_h', '_pv_g': 'pv_g',
                                        '_pv_ho': 'pv_ho', '_pv_ao': 'pv_ao'})
            # pair → 最新一場對賽 (kickoff, id, h, g, ho, ao)，供目標賽事查「對上一次對賽尾盤」
            tail = grp.tail(1)
            pair_latest = {(int(pa), int(pb)): (ko, int(i), h, g, ho, ao)
                           for pa, pb, ko, i, h, g, ho, ao in zip(
                               tail['_pa'].tolist(), tail['_pb'].tolist(),
                               tail['kickoff'].tolist(), tail['id'].tolist(),
                               tail['handicap'].tolist(), tail['giver'].tolist(),
                               tail['home_odds'].tolist(), tail['away_odds'].tolist())}
        if 'pv_h' not in df.columns:
            for c in ('pv_h', 'pv_ho', 'pv_ao'):
                df[c] = np.nan
            df['pv_g'] = None
        # 每場「主隊」對上一次比賽（任何對手）嘅尾盤 —— 第16/17項用
        line_of = {}
        if len(allm):
            line_of = {int(r.id): (r.handicap, r.giver)
                       for r in allm.itertuples(index=False)}
        allf = pd.read_sql(
            "SELECT id, home_id, away_id FROM matches "
            "WHERE home_score IS NOT NULL ORDER BY kickoff, id", conn)
        n = len(allf)
        hp_h = [np.nan] * n
        hp_g = [None] * n
        hp_role = [None] * n
        last = {}          # team_id -> (match_id, match_home_id)
        f_ids = allf['id'].tolist()
        f_hs = allf['home_id'].tolist()
        f_as = allf['away_id'].tolist()
        for i in range(n):
            hid = f_hs[i]
            p = last.get(hid)
            if p is not None and p[0] in line_of:
                h, g = line_of[p[0]]
                hp_h[i] = h
                hp_g[i] = g
                hp_role[i] = 'home' if p[1] == hid else 'away'
            last[hid] = (f_ids[i], hid)
            last[f_as[i]] = (f_ids[i], hid)
        allf['hp_h'] = hp_h
        allf['hp_g'] = hp_g
        allf['hp_role'] = hp_role
        df = df.merge(allf[['id', 'hp_h', 'hp_g', 'hp_role']], on='id', how='left')
        _pool_cache['df'] = df
        _pool_cache['prev_h2h'] = prev
        _pool_cache['pair_latest'] = pair_latest
        _pool_cache['ts'] = time.time()
        print(f'[load_pool] {len(df)} 場，耗時 {time.time()-t0:.1f}s')
        return df, prev


def get_target(conn, match_id):
    """目標賽事：基本資料 + 三個時點盤 + 開賽前 43 項 + 對上一次對賽尾盤"""
    row = conn.execute(
        'SELECT m.id, m.kickoff, m.home_id, m.away_id, m.season_id, m.round_label, '
        'ht.name_tc, at.name_tc, c.req_name, c.category '
        'FROM matches m JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id WHERE m.id=?',
        (match_id,)).fetchone()
    if not row:
        return None
    t = {'id': row[0], 'kickoff': row[1], 'home_id': row[2], 'away_id': row[3],
         'season_id': row[4], 'round_label': row[5], 'home': row[6], 'away': row[7],
         'league': row[8], 'category': row[9]}
    odds = {}
    for label, h, g, ho, ao in conn.execute(
            'SELECT label, handicap, giver, home_odds, away_odds FROM odds_asian '
            'WHERE match_id=? AND company_id=12', (match_id,)):
        odds[label] = {'h': h, 'g': g, 'ho': ho, 'ao': ao}
    t['odds'] = odds
    t['pre'] = calc_target_pre(conn, t)
    # 對上一次對賽尾盤（在已完賽的對賽記錄中找雙方最近一場）
    load_pool(conn)
    pair = tuple(sorted((t['home_id'], t['away_id'])))
    cand = _pool_cache['pair_latest'].get(pair)
    if cand is None:
        t['prev_h2h'] = None
    elif cand[1] == t['id']:
        # 目標賽事自己已是雙方最新一場 → 取佢之前嗰場
        t['prev_h2h'] = _pool_cache['prev_h2h'].get(t['id'])
    elif cand[0] < t['kickoff']:
        t['prev_h2h'] = (cand[2], cand[3], cand[4], cand[5])
    else:
        t['prev_h2h'] = None
    # 今次主隊對上一次比賽（任何對手）尾盤 —— 第16/17項用
    prow = conn.execute(
        'SELECT m.id, m.home_id, oc.handicap, oc.giver, oc.home_odds, oc.away_odds '
        'FROM matches m JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 AND oc.handicap IS NOT NULL "
        'WHERE (m.home_id=? OR m.away_id=?) AND m.home_score IS NOT NULL '
        'AND m.kickoff < ? ORDER BY m.kickoff DESC, m.id DESC LIMIT 1',
        (t['home_id'], t['home_id'], t['kickoff'])).fetchone()
    if prow:
        t['home_prev'] = {'h': prow[2], 'g': prow[3],
                          'role': 'home' if prow[1] == t['home_id'] else 'away',
                          'ho': prow[4], 'ao': prow[5]}
    else:
        t['home_prev'] = None
    return t


def calc_target_pre(conn, t):
    """與 prematch.py 同口徑計算目標賽事開賽前 43 項（含勝和負）"""
    import prematch
    if t['category'] == '杯賽':
        rows = conn.execute(
            'SELECT id, kickoff, home_id, away_id, home_score, away_score FROM matches '
            'WHERE season_id=? AND home_score IS NOT NULL AND round_label IS ? '
            'ORDER BY kickoff, id', (t['season_id'], t['round_label'])).fetchall()
    else:
        rows = conn.execute(
            'SELECT id, kickoff, home_id, away_id, home_score, away_score FROM matches '
            'WHERE season_id=? AND home_score IS NOT NULL ORDER BY kickoff, id',
            (t['season_id'],)).fetchall()
    stats = {}
    for _id, _ko, hid, aid, _hs, _aws in rows:
        stats.setdefault(hid, prematch._new_stats())
        stats.setdefault(aid, prematch._new_stats())
    for _mid, ko, hid, aid, hs, aws in rows:
        if ko >= t['kickoff']:
            break
        prematch._apply(stats, hid, 'total', hs, aws)
        prematch._apply(stats, hid, 'home', hs, aws)
        prematch._apply(stats, aid, 'total', aws, hs)
        prematch._apply(stats, aid, 'away', aws, hs)
    snap = prematch._snapshot(stats)
    out = {}
    for side, tid in (('home', t['home_id']), ('away', t['away_id'])):
        for scope in ('total', 'home', 'away'):
            info = snap.get(tid, {}).get(scope)
            if info:
                rank, gf, ga, w, d, l, pts = info
            else:
                rank, gf, ga, w, d, l, pts = None, 0, 0, 0, 0, 0, 0
            out[f'{side}_{scope}_rank'] = rank
            out[f'{side}_{scope}_gf'] = gf
            out[f'{side}_{scope}_ga'] = ga
            out[f'{side}_{scope}_w'] = w
            out[f'{side}_{scope}_d'] = d
            out[f'{side}_{scope}_l'] = l
            p = w + d + l
            out[f'{side}_{scope}_p'] = p
            out[f'{side}_{scope}_wp'] = w / p if p else None
            out[f'{side}_{scope}_dp'] = d / p if p else None
            out[f'{side}_{scope}_lp'] = l / p if p else None
            out[f'{side}_{scope}_gd'] = gf - ga
    return out


def line_eq(df, pfx, T, tol=0.03):
    """盤口 100% 相同（讓球數+讓球方），主客水位各 ±tol。T={'h','g','ho','ao'}"""
    if T is None or T.get('h') is None:
        return pd.Series(False, index=df.index)
    same_h = (df[f'{pfx}_h'] == T['h'])
    same_g = (df[f'{pfx}_g'].fillna('none') == (T.get('g') or 'none'))
    w_ho = (df[f'{pfx}_ho'] - T['ho']).abs() <= tol
    w_ao = (df[f'{pfx}_ao'] - T['ao']).abs() <= tol
    return same_h & same_g & w_ho & w_ao


def tline(odds, label):
    o = odds.get(label)
    if not o or o['h'] is None:
        return None
    return {'h': o['h'], 'g': o['g'], 'ho': o['ho'], 'ao': o['ao']}


def outcome_counts(sub):
    """上/下/走 場次與比率（讓球盤：A=上盤；平手：A=主勝）"""
    n = len(sub)
    if n == 0:
        return None
    a = int((sub['res'] == 'A').sum())
    b = int((sub['res'] == 'B').sum())
    p = int((sub['res'] == 'P').sum())
    eff = n - p
    return {'n': n, 'up': a, 'down': b, 'push': p,
            'up_r': a / eff if eff else None, 'down_r': b / eff if eff else None,
            'push_r': p / n}


def dist_table(sub, min_n=1):
    """盤口分佈：每個(讓球,讓球方) 場次、上/下/走、上盤水位分佈"""
    rows = []
    if len(sub) == 0:
        return rows
    grp = sub.groupby([sub['c_h'].round(2), sub['c_g'].fillna('none')], sort=True)
    for (h, g), s in grp:
        if len(s) < min_n:
            continue
        oc = outcome_counts(s)
        # 每個水位區：場次 + 上/下/走 率
        zones = [None] * 10
        for zi in range(10):
            zs = s[s['up_zone'] == zi]
            if len(zs):
                zones[zi] = outcome_counts(zs)
        rows.append({'line': fmt_line(h, None if g == 'none' else g), 'h': h, 'g': g,
                     'n': oc['n'], 'up': oc['up'], 'down': oc['down'], 'push': oc['push'],
                     'up_r': oc['up_r'], 'down_r': oc['down_r'], 'push_r': oc['push_r'],
                     'zones': zones,
                     'max_zone_n': max((z['n'] for z in zones if z), default=0)})
    rows.sort(key=lambda r: -r['n'])
    return rows


def gap_text(ref_h, ref_g, T_close):
    """參照盤 vs 今場尾盤 的淨差距，以【今場】角度表達（今場讓深咗／讓淺咗）"""
    if T_close is None:
        return None
    cur_h, cur_g = T_close['h'], T_close.get('g')
    # 帶符號：主讓=+h，客讓=-h
    def signed(h, g):
        if h == 0 or g is None:
            return 0.0
        return h if g == 'home' else -h
    rs, cs = signed(ref_h, ref_g), signed(cur_h, cur_g)
    gap = rs - cs
    ref_line = fmt_line(ref_h, ref_g)
    cur_line = fmt_line(cur_h, cur_g)
    if abs(gap) < 1e-9:
        desc = f'與今場相同（{ref_line}）'
    elif gap > 0:
        desc = f'今場讓淺咗{gap:g}（參照：{ref_line}；今場：{cur_line}）'
    else:
        desc = f'今場讓深咗{-gap:g}（參照：{ref_line}；今場：{cur_line}）'
    return {'gap': gap, 'text': desc, 'ref_line': ref_line, 'note': ''}


def screen(conn, t, sel=None):
    """對目標賽事 t 執行 14 項篩查 + 第15項組合篩查（sel=剔選項目列表），回傳結果 dict"""
    df, _prev = load_pool(conn)
    lg = t['league']
    cat = t.get('category', '')
    odds = t['odds']
    T_close = tline(odds, 'closing')
    T_init = tline(odds, 'initial')
    T_4h = tline(odds, 'pre_4h')
    pre = t['pre']

    pool_all = int(len(df))
    pool_lg = int((df['league'] == lg).sum()) if lg else 0
    pool_cat = int((df['cat'] == cat).sum()) if cat else 0
    pools = {'all': pool_all, 'league': pool_lg, 'cat': pool_cat}

    def pair(sub):
        return {'all': outcome_counts(sub),
                'league': outcome_counts(sub[sub['league'] == lg]) if lg else None,
                'pool': dict(pools)}

    masks = {}   # 第15項組合用：每項的篩選 mask（None=不適用）
    items = {}

    # 1 / 2：某時點與尾盤雙重相同
    for no, pfx, Tref, tname in ((1, 'i', T_close, '初盤'), (2, 'f', T_close, '開賽前4小時')):
        if T_close is None or Tref is None:
            items[str(no)] = {'error': '目標賽事缺少尾盤（或對應時點）盤口，請先獲取賠率', 'pool': dict(pools)}
            continue
        m = line_eq(df, pfx, Tref) & line_eq(df, 'c', T_close)
        masks[no] = m
        items[str(no)] = {
            'title': f'同{tname}及尾盤的盤口&水位（盤口100%一樣，水位±0.03）',
            'ref': (f"基準＝今場尾盤：{fmt_line(T_close['h'], T_close['g'])} 主{T_close['ho']}/客{T_close['ao']}　"
                    f"篩選：歷史場次嘅【{tname}】同【尾盤】都同基準完全相同（水位±0.03）"),
            **pair(df[m])}

    # 3 / 4 / 5：對上一次對賽尾盤 vs 今次第X時點
    for no, Tref, tname in ((3, T_init, '初盤'), (4, T_4h, '開賽前4小時'), (5, T_close, '尾盤')):
        if Tref is None:
            items[str(no)] = {'error': f'目標賽事缺少{tname}，請先獲取賠率', 'pool': dict(pools)}
            continue
        m = line_eq(df, 'pv', Tref)
        masks[no] = m
        pv = t.get('prev_h2h')
        items[str(no)] = {
            'title': f'對上一次對賽尾盤 對 今次{tname}（盤口100%一樣，水位±0.03）',
            'ref': (f"今次{tname}：{fmt_line(Tref['h'], Tref['g'])} 主{Tref['ho']}/客{Tref['ao']}　"
                    + (f"雙方對上一次對賽尾盤：{fmt_line(pv[0], pv[1])} 主{pv[2]}/客{pv[3]}"
                       if pv else '雙方過往無對賽記錄')),
            **pair(df[m])}

    # 6 / 7：勝和負比例 ±7%
    def form_filter(hsc, asc):
        hp = df[f'home_{hsc}_p'] > 0
        ap = df[f'away_{asc}_p'] > 0
        m = hp & ap
        for side, sc in (('home', hsc), ('away', asc)):
            Tbase = f'{side}_{sc}'
            if not pre.get(f'{Tbase}_p'):
                return None
            for f_ in ('wp', 'dp', 'lp'):
                m &= (df[f'{Tbase}_{f_}'] - pre[f'{Tbase}_{f_}']).abs() <= 0.07
        return m
    f6 = form_filter('home', 'away')
    f7 = form_filter('total', 'total')
    masks[6], masks[10] = f6, f6
    masks[7], masks[11] = f7, f7
    t6 = (f"主隊主場 勝{pre['home_home_wp']*100:.0f}% 和{pre['home_home_dp']*100:.0f}% 負{pre['home_home_lp']*100:.0f}%　"
          f"客隊客場 勝{pre['away_away_wp']*100:.0f}% 和{pre['away_away_dp']*100:.0f}% 負{pre['away_away_lp']*100:.0f}%（每項±7%）"
          ) if f6 is not None else '目標賽事主隊主場或客隊客場未出賽，不適用'
    t7 = (f"主隊總計 勝{pre['home_total_wp']*100:.0f}% 和{pre['home_total_dp']*100:.0f}% 負{pre['home_total_lp']*100:.0f}%　"
          f"客隊總計 勝{pre['away_total_wp']*100:.0f}% 和{pre['away_total_dp']*100:.0f}% 負{pre['away_total_lp']*100:.0f}%（每項±7%）"
          ) if f7 is not None else '目標賽事其中一方未出賽，不適用'
    items['6'] = {'title': '主隊主場勝和負比例 及 客隊客場勝和負比例 類似（每項±7%）',
                  'ref': t6, 'pool': dict(pools),
                  **(pair(df[f6]) if f6 is not None else {'error': '不適用'})}
    items['7'] = {'title': '主隊主客場總和勝和負比例 及 客隊總和類似（每項±7%）',
                  'ref': t7, 'pool': dict(pools),
                  **(pair(df[f7]) if f7 is not None else {'error': '不適用'})}

    # 8 / 9：入球/失球/得失球差 ±1
    def goal_filter(hsc, asc):
        hp = df[f'home_{hsc}_p'] > 0
        ap = df[f'away_{asc}_p'] > 0
        m = hp & ap
        for side, sc in (('home', hsc), ('away', asc)):
            Tbase = f'{side}_{sc}'
            if not pre.get(f'{Tbase}_p'):
                return None
            m &= (df[f'{Tbase}_gf'] - pre[f'{Tbase}_gf']).abs() <= 3
            m &= (df[f'{Tbase}_ga'] - pre[f'{Tbase}_ga']).abs() <= 3
            m &= (df[f'{Tbase}_gd'] - pre[f'{Tbase}_gd']).abs() <= 3
        return m
    g8 = goal_filter('home', 'away')
    g9 = goal_filter('total', 'total')
    masks[8], masks[9] = g8, g9
    t8 = (f"主隊主場 入{pre['home_home_gf']} 失{pre['home_home_ga']} 差{pre['home_home_gd']}　"
          f"客隊客場 入{pre['away_away_gf']} 失{pre['away_away_ga']} 差{pre['away_away_gd']}（各±3）"
          ) if g8 is not None else '目標賽事主隊主場或客隊客場未出賽，不適用'
    t9 = (f"主隊總計 入{pre['home_total_gf']} 失{pre['home_total_ga']} 差{pre['home_total_gd']}　"
          f"客隊總計 入{pre['away_total_gf']} 失{pre['away_total_ga']} 差{pre['away_total_gd']}（各±3）"
          ) if g9 is not None else '目標賽事其中一方未出賽，不適用'
    items['8'] = {'title': '主隊主場入球/失球/得失球差 及 客隊客場（各±3球）',
                  'ref': t8, 'pool': dict(pools),
                  **(pair(df[g8]) if g8 is not None else {'error': '不適用'})}
    items['9'] = {'title': '主隊主客場總和入球/失球/得失球差 及 客隊總和（各±3球）',
                  'ref': t9, 'pool': dict(pools),
                  **(pair(df[g9]) if g9 is not None else {'error': '不適用'})}

    # 10 / 11 / 12 / 13：盤口分佈 + 水位分佈
    def dist_item(no, mask, title, ref):
        if mask is None:
            return {'title': title, 'ref': ref, 'error': '不適用', 'pool': dict(pools)}
        sub = df[mask]
        return {'title': title, 'ref': ref, 'pool': dict(pools),
                'all': {'n': len(sub), 'pool': pool_all, 'dist': dist_table(sub)},
                'league': {'n': len(sub[sub['league'] == lg]), 'pool': pool_lg,
                           'dist': dist_table(sub[sub['league'] == lg])}}
    items['10'] = dist_item(10, f6,
        '同第6項篩選（主客場勝和負比例類似）→ 歷史盤口分佈 + 各盤口水位分佈', t6)
    items['11'] = dist_item(11, f7,
        '同第7項篩選（總和勝和負比例類似）→ 歷史盤口分佈 + 各盤口水位分佈', t7)
    if pre.get('home_home_rank') and pre.get('away_away_rank'):
        m12 = (df['home_home_rank'] - pre['home_home_rank']).abs() <= 2
        m12 &= (df['away_away_rank'] - pre['away_away_rank']).abs() <= 2
        r12 = (f"主隊主場排名 第{pre['home_home_rank']}　"
               f"客隊客場排名 第{pre['away_away_rank']}（各±2）")
    else:
        m12, r12 = None, '目標賽事主隊主場或客隊客場未有排名，不適用'
    masks[12] = m12
    items['12'] = dist_item(12, m12,
        '主隊主場排名 及 客隊客場排名（各±2）→ 歷史盤口分佈 + 各盤口水位分佈', r12)
    if pre.get('home_total_rank') and pre.get('away_total_rank'):
        m13 = (df['home_total_rank'] - pre['home_total_rank']).abs() <= 2
        m13 &= (df['away_total_rank'] - pre['away_total_rank']).abs() <= 2
        r13 = (f"主隊總排名 第{pre['home_total_rank']}　"
               f"客隊總排名 第{pre['away_total_rank']}（各±2）")
    else:
        m13, r13 = None, '目標賽事其中一方未有總排名，不適用'
    masks[13] = m13
    items['13'] = dist_item(13, m13,
        '主隊主客場總排名 及 客隊總排名（各±2）→ 歷史盤口分佈 + 各盤口水位分佈', r13)

    # 14/17 共用：搵 分佈最多盤口 及 上盤勝率最接近50%盤口（≥5場），計與今場尾盤淨差距
    def mode_d50(sub_all, sub_lg):
        def calc(sub):
            if len(sub) == 0:
                return None
            grp = sub.groupby([sub['c_h'].round(2), sub['c_g'].fillna('none')])
            best_n, best_line = -1, None
            best_oc = None
            best_d50, d50_line = 1e9, None
            for (h, g), s in grp:
                n = len(s)
                oc = outcome_counts(s)
                if n > best_n:
                    best_n, best_line = n, (h, None if g == 'none' else g)
                    best_oc = oc
                if n >= 5:
                    d = abs((oc['up_r'] or 0) - 0.5)
                    if d < best_d50:
                        best_d50, d50_line = d, (h, None if g == 'none' else g, oc, n)
            out = {}
            if best_line:
                out['mode'] = {'line': fmt_line(*best_line), 'n': best_n,
                               'up_r': best_oc['up_r'], 'down_r': best_oc['down_r'],
                               'push': best_oc['push'], 'push_r': best_oc['push_r'],
                               'gap': gap_text(best_line[0], best_line[1], T_close)}
            if d50_line:
                oc50 = d50_line[2]
                out['d50'] = {'line': fmt_line(d50_line[0], d50_line[1]),
                              'up_r': oc50['up_r'], 'down_r': oc50['down_r'],
                              'push': oc50['push'], 'push_r': oc50['push_r'],
                              'n': d50_line[3],
                              'gap': gap_text(d50_line[0], d50_line[1], T_close)}
            return out
        return {'all': calc(sub_all), 'league': calc(sub_lg)}

    if m12 is not None:
        sub12 = df[m12]
        items['14'] = {'title': '第12項樣本：分佈最多盤口 及 上盤勝率最接近50%盤口，與今場尾盤淨差距',
                       'ref': r12, 'pool': dict(pools),
                       **mode_d50(sub12, sub12[sub12['league'] == lg])}
    else:
        items['14'] = {'title': '第12項延伸', 'ref': r12, 'error': '不適用', 'pool': dict(pools)}

    # 15：排名±2 ＋ 同今場尾盤 100% 相同盤口 → 各水位 上/下/走 率（只計 贏1／走盤／輸1）
    def zone_rates(sub):
        rows = []
        for zi in range(10):
            s = sub[sub['up_zone'] == zi]
            oc = outcome_counts(s)
            if oc:
                rows.append({'zone': ZONES[zi], 'n': oc['n'], 'up': oc['up'],
                             'down': oc['down'], 'push': oc['push'],
                             'up_r': oc['up_r'], 'down_r': oc['down_r'],
                             'push_r': oc['push_r']})
        return rows
    if m12 is not None and T_close is not None:
        m15 = m12 & (df['c_h'] == T_close['h']) & \
              (df['c_g'].fillna('none') == (T_close.get('g') or 'none'))
        masks[15] = m15
        sub_all = df[m15]
        sub_lg = sub_all[sub_all['league'] == lg]
        items['15'] = {'title': '主隊主場排名(±2)及客隊客場排名(±2)＋同今場尾盤相同盤口 → 各水位 上盤率／下盤率／走盤率',
                       'ref': (f"{r12}　再加條件：尾盤盤口同今場 100% 相同"
                               f"（{fmt_line(T_close['h'], T_close['g'])} 主{T_close['ho']}/客{T_close['ao']}）"),
                       'pool': dict(pools),
                       'all': {'n': len(sub_all), 'pool': pool_all, 'zones': zone_rates(sub_all)},
                       'league': {'n': len(sub_lg), 'pool': pool_lg, 'zones': zone_rates(sub_lg)}}
    else:
        items['15'] = {'title': '主隊主場排名(±2)及客隊客場排名(±2)＋同今場尾盤相同盤口 → 各水位 上盤率／下盤率／走盤率',
                       'ref': r12 if m12 is not None else '目標賽事缺少尾盤或排名數據',
                       'error': '不適用', 'pool': dict(pools)}

    # 16：今次主隊對上一次比賽（角色＋讓/受讓＋讓球數 完全相同）→ 今次主場歷史盤口分佈＋各水位率
    hp = t.get('home_prev')
    m16 = None
    r16 = ''
    if hp is not None and hp['h'] is not None:
        m16 = (df['hp_role'] == hp['role']) & \
              (df['hp_g'].fillna('none') == (hp['g'] or 'none')) & \
              (df['hp_h'] == hp['h'])
        masks[16] = m16
        r16 = (f"今次主隊（{t['home']}）對上一次比賽："
               f"{'主場' if hp['role'] == 'home' else '作客'}"
               f"{fmt_line(hp['h'], hp['g'])} 主{hp['ho']}/客{hp['ao']}　"
               f"→ 歷史場次【主隊】對上一次比賽都係同樣角色、同樣讓/受讓、同樣讓球數")
    else:
        r16 = '今次主隊未有對上一次比賽尾盤數據，不適用'
    if m16 is not None:
        sub_all = df[m16]
        sub_lg = sub_all[sub_all['league'] == lg]
        items['16'] = {'title': '今次主隊對上一次比賽（角色＋讓/受讓＋讓球數 完全相同）→ 今次主場歷史盤口分佈 ＋ 各水位 上/下/走 率',
                       'ref': r16, 'pool': dict(pools),
                       'all': {'n': len(sub_all), 'pool': pool_all,
                               'dist': dist_table(sub_all), 'zones': zone_rates(sub_all)},
                       'league': {'n': len(sub_lg), 'pool': pool_lg,
                                  'dist': dist_table(sub_lg), 'zones': zone_rates(sub_lg)}}
    else:
        items['16'] = {'title': '今次主隊對上一次比賽（角色＋讓/受讓＋讓球數 完全相同）→ 今次主場歷史盤口分佈 ＋ 各水位 上/下/走 率',
                       'ref': r16, 'error': '不適用', 'pool': dict(pools)}

    # 17：第16項樣本 → 分佈最多盤口 及 最接近50%盤口，與今場尾盤淨差距
    if m16 is not None:
        sub16 = df[m16]
        items['17'] = {'title': '第16項樣本：分佈最多盤口 及 上盤勝率最接近50%盤口，與今場尾盤淨差距',
                       'ref': r16, 'pool': dict(pools),
                       **mode_d50(sub16, sub16[sub16['league'] == lg])}
    else:
        items['17'] = {'title': '第16項延伸', 'ref': r16, 'error': '不適用', 'pool': dict(pools)}

    # 18：主隊主場排名 − 客隊客場排名 差距淨值（±1）＋ 同今場尾盤 100% 相同盤口 → 結果＋各水位率
    if pre.get('home_home_rank') and pre.get('away_away_rank'):
        d18 = pre['home_home_rank'] - pre['away_away_rank']
        r18 = (f"主隊主場排名 第{pre['home_home_rank']} − 客隊客場排名 第{pre['away_away_rank']}"
               f"＝差距 {d18:+d}（±1）")
        m18 = ((df['home_home_rank'] - df['away_away_rank']) - d18).abs() <= 1
        if T_close is not None:
            m18 &= (df['c_h'] == T_close['h']) & \
                   (df['c_g'].fillna('none') == (T_close.get('g') or 'none'))
            r18 += f"　同尾盤：{fmt_line(T_close['h'], T_close['g'])} 主{T_close['ho']}/客{T_close['ao']}"
        else:
            m18 = None
            r18 += '　（目標賽事缺少尾盤數據）'
    else:
        m18, r18 = None, '目標賽事主隊主場或客隊客場未有排名，不適用'
    masks[18] = m18
    if m18 is not None:
        sub_all = df[m18]
        sub_lg = sub_all[sub_all['league'] == lg]
        items['18'] = {'title': '主隊主場排名 − 客隊客場排名 差距淨值（±1）＋同今場尾盤相同盤口 → 結果＋各水位 上/下/走 率',
                       'ref': r18, 'pool': dict(pools),
                       'all': {'n': len(sub_all), 'pool': pool_all,
                               'oc': outcome_counts(sub_all),
                               'zones': zone_rates(sub_all)},
                       'league': {'n': len(sub_lg), 'pool': pool_lg,
                                  'oc': outcome_counts(sub_lg),
                                  'zones': zone_rates(sub_lg)}}
    else:
        items['18'] = {'title': '主隊主場排名 − 客隊客場排名 差距淨值（±1）＋同今場尾盤相同盤口 → 結果＋各水位 上/下/走 率',
                       'ref': r18, 'error': '不適用', 'pool': dict(pools)}

    # 19：主隊總排名 − 客隊總排名 差距淨值（±1）＋ 同今場尾盤 100% 相同盤口 → 結果＋各水位率
    if pre.get('home_total_rank') and pre.get('away_total_rank'):
        d19 = pre['home_total_rank'] - pre['away_total_rank']
        r19 = (f"主隊總排名 第{pre['home_total_rank']} − 客隊總排名 第{pre['away_total_rank']}"
               f"＝差距 {d19:+d}（±1）")
        m19 = ((df['home_total_rank'] - df['away_total_rank']) - d19).abs() <= 1
        if T_close is not None:
            m19 &= (df['c_h'] == T_close['h']) & \
                   (df['c_g'].fillna('none') == (T_close.get('g') or 'none'))
            r19 += f"　同尾盤：{fmt_line(T_close['h'], T_close['g'])} 主{T_close['ho']}/客{T_close['ao']}"
        else:
            m19 = None
            r19 += '　（目標賽事缺少尾盤數據）'
    else:
        m19, r19 = None, '目標賽事其中一方未有總排名，不適用'
    masks[19] = m19
    if m19 is not None:
        sub_all = df[m19]
        sub_lg = sub_all[sub_all['league'] == lg]
        items['19'] = {'title': '主隊總排名 − 客隊總排名 差距淨值（±1）＋同今場尾盤相同盤口 → 結果＋各水位 上/下/走 率',
                       'ref': r19, 'pool': dict(pools),
                       'all': {'n': len(sub_all), 'pool': pool_all,
                               'oc': outcome_counts(sub_all),
                               'zones': zone_rates(sub_all)},
                       'league': {'n': len(sub_lg), 'pool': pool_lg,
                                  'oc': outcome_counts(sub_lg),
                                  'zones': zone_rates(sub_lg)}}
    else:
        items['19'] = {'title': '主隊總排名 − 客隊總排名 差距淨值（±1）＋同今場尾盤相同盤口 → 結果＋各水位 上/下/走 率',
                       'ref': r19, 'error': '不適用', 'pool': dict(pools)}

    # 20：組合篩查（剔選第1-19項，AND 全部條件）
    items['20'] = {'title': '組合篩查：剔選第1-19項，同時符合全部剔選條件',
                   'ref': '喺下面剔選條件再按「提交」（第14、17項係分析項、第20項係組合本身，都不能剔）；結果分 全資料庫／同聯賽／同賽事類別（聯賽/杯賽）',
                   'pool': dict(pools)}
    if sel:
        sel = [int(s) for s in sel if str(s).strip()]
        bad = [s for s in sel if s in (14, 17, 20)]
        none_items = [s for s in sel if s not in (14, 17, 20) and masks.get(s) is None]
        none_items += [s for s in sel if s not in masks and s not in (14, 17, 20)]
        if bad:
            items['20'] = {**items['20'],
                           'error': '第14、17項係分析項（淨差距）、第20項係組合篩查本身，無篩選條件，不能剔選'}
        elif none_items:
            items['20'] = {**items['20'],
                           'error': f'剔選咗而家不適用嘅項目：第{"、".join(map(str, sorted(set(none_items))))}項（目標賽事數據不足）'}
        else:
            m = masks[sel[0]].copy()
            for s in sel[1:]:
                m &= masks[s]
            sub = df[m]
            items['20'] = {'title': '組合篩查：剔選第1-19項，同時符合全部剔選條件',
                           'ref': '已剔選：' + '、'.join(f'第{s}項' for s in sel),
                           'sel': sel, 'pool': dict(pools),
                           'all': outcome_counts(sub),
                           'league': outcome_counts(sub[sub['league'] == lg]) if lg else None,
                           'cat': outcome_counts(sub[sub['cat'] == cat]) if cat else None}
    return {
        'target': {'id': t['id'], 'home': t['home'], 'away': t['away'],
                   'league': lg, 'category': cat, 'kickoff': t['kickoff'],
                   'close': ({'line': fmt_line(T_close['h'], T_close['g']),
                              'ho': T_close['ho'], 'ao': T_close['ao']} if T_close else None),
                   'init': ({'line': fmt_line(T_init['h'], T_init['g']),
                             'ho': T_init['ho'], 'ao': T_init['ao']} if T_init else None),
                   'h4': ({'line': fmt_line(T_4h['h'], T_4h['g']),
                           'ho': T_4h['ho'], 'ao': T_4h['ao']} if T_4h else None)},
        'zones': ZONES,
        'items': items,
    }


if __name__ == '__main__':
    import json, sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    conn = sqlite3.connect(DB_PATH)
    mid = int(sys.argv[1]) if len(sys.argv) > 1 else 3072972  # 曼城 vs 諾域治
    sel = [int(x) for x in sys.argv[2].split(',')] if len(sys.argv) > 2 else None
    t = get_target(conn, mid)
    res = screen(conn, t, sel=sel)
    slim = {'target': res['target'],
            'items': {k: {kk: vv for kk, vv in v.items() if kk != 'dist'}
                      for k, v in res['items'].items()}}
    print(json.dumps(slim, ensure_ascii=False, indent=1, default=str))
