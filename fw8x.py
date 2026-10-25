# -*- coding: utf-8 -*-
"""8X 版面引擎（2026-10-18 用戶規格）：
- 載入 league_formula/*.json 優化後公式（每聯賽／杯賽獨立）
- 賽前：對未來賽事計算全部特徵（同 lf_mine_all 193 項同名），公式條件全中→記錄入 fw8x_log
- 賽後：有全場賽果＋尾盤→自動結算上/下盤勝負
- 循環：每 20 分鐘 scan+settle；另有 /api/8x/* 端點俾前端
"""
import json
import os
import sqlite3
import threading
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(BASE_DIR)
# 公式目錄：本機 = 工作區/league_formula；雲端（deploy_repo 根即 BASE_DIR）=
# 仓库内 league_formula。可用 FORMULA_DIR 環境變數覆蓋。
FORMULA_DIR = os.environ.get('FORMULA_DIR') or (
    os.path.join(BASE_DIR, 'league_formula')
    if os.path.isdir(os.path.join(BASE_DIR, 'league_formula'))
    else os.path.join(WS, 'league_formula'))
TP7 = ['initial', 'pre_4h', 'pre_30m', 'pre_15m', 'pre_10m', 'pre_5m',
       'closing']
TP_SHORT = ['initial', 'pre_4h', 'closing']

_formulas = None
_formulas_ts = 0


def load_formulas(force=False):
    """league → formula dict（只取有 preds 嘅優化／驗證公式）。"""
    global _formulas, _formulas_ts
    if not force and _formulas and time.time() - _formulas_ts < 300:
        return _formulas
    out = {}
    if os.path.isdir(FORMULA_DIR):
        for f in os.listdir(FORMULA_DIR):
            if not f.endswith('.json') or f.startswith('_'):
                continue
            try:
                d = json.load(open(os.path.join(FORMULA_DIR, f),
                                   encoding='utf-8'))
            except Exception:
                continue
            if d.get('status') in ('optimized', 'verified') and \
                    d.get('preds') and d.get('league'):
                out[d['league']] = d
    _formulas = out
    _formulas_ts = time.time()
    return out


# ---------- bucket 函數（同 tools/lf_mine_all.py 完全一致） ----------

def water_band(v):
    if v is None:
        return None
    v = float(v)
    if v < 0.60 or v > 1.40:
        return None
    return 'w%.2f' % (int(v / 0.05) * 0.05)


def line_bucket(v):
    if v is None:
        return None
    v = round(float(v) * 4) / 4.0
    if abs(v) > 3.5:
        return None
    return 'l%.2f' % v


def ou_line_bucket(v):
    if v is None:
        return None
    v = round(float(v) * 4) / 4.0
    if v < 1.0 or v > 5.0:
        return None
    return 'o%.2f' % v


def prob_band(v):
    if v is None or v <= 1.0:
        return None
    p = 1.0 / float(v)
    if p > 0.95:
        return None
    return 'p%.2f' % (int(p / 0.05) * 0.05)


def rank_band(v):
    if v is None:
        return None
    v = int(v)
    if v <= 2:
        return 'r1_2'
    if v <= 4:
        return 'r3_4'
    if v <= 8:
        return 'r5_8'
    if v <= 12:
        return 'r9_12'
    if v <= 16:
        return 'r13_16'
    if v <= 20:
        return 'r17_20'
    if v <= 24:
        return 'r21_24'
    return 'r25+'


def diff_band(v, step, lo, hi):
    if v is None:
        return None
    v = float(v)
    if v < lo or v > hi:
        return None
    return 'd%.2f' % (int(v / step) * step)


def pts_band(v):
    return diff_band(v, 3, -18, 18)


def rate_band(v):
    return diff_band(v, 0.1, 0.0, 1.0)


def signed_line(hc, gv):
    if hc is None:
        return None
    s = 1 if gv == 'home' else (-1 if gv == 'away' else 0)
    return hc * s


RANK_KEYS = {'ps_home_total_rank', 'ps_away_total_rank',
             'ps_home_home_rank', 'ps_away_away_rank',
             'ps_home_away_rank', 'ps_away_home_rank',
             'rankdiff_total', 'rankdiff_homeaway'}


# ---------- 未來場次即場特徵（mirror engine/prematch.py 同 engine/derive.py 口徑） ----------
# match_prestandings / match_derived 只係賽後先有人寫入；未來場次要即場計，
# 否則 48/74 條用到排名／近況嘅公式賽前永遠觸發唔到。

import sys
for _p in (os.path.join(WS, 'engine'), os.path.join(BASE_DIR, 'engine')):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import prematch as _pm
    from derive import handicap_outcome as _hw_outcome
except Exception:
    _pm = None
    _hw_outcome = None


def _needs_live(f):
    """公式有冇用到 ps_/md_/rankdiff/pts_diff 呢啲賽前要先計嘅特徵。"""
    for p in f['preds']:
        k = str(p).rsplit('~', 1)[0].rsplit(':', 1)[0].rsplit('=', 1)[0]
        if k.startswith(('ps_', 'md_', 'rankdiff')) or k == 'pts_diff_l6':
            return True
    return False


def _fill_live_ps(conn, mid, feat):
    """按 prematch.py 同口径即場計 43 項賽前排名（DB 已有就跳過）。"""
    if _pm is None or feat.get('ps_home_total_rank') is not None:
        return
    row = conn.execute(
        'SELECT season_id, kickoff, home_id, away_id FROM matches WHERE id=?',
        (mid,)).fetchone()
    if not row:
        return
    sid, ko, hid, aid = row
    gms = conn.execute(
        'SELECT home_id, away_id, home_score, away_score FROM matches '
        'WHERE season_id=? AND home_score IS NOT NULL AND kickoff < ? '
        'ORDER BY kickoff, id', (sid, ko)).fetchall()
    stats = {}
    for h2, a2, _s1, _s2 in gms:
        stats.setdefault(h2, _pm._new_stats())
        stats.setdefault(a2, _pm._new_stats())
    for h2, a2, hs2, as2 in gms:
        _pm._apply(stats, h2, 'total', hs2, as2)
        _pm._apply(stats, h2, 'home', hs2, as2)
        _pm._apply(stats, a2, 'total', as2, hs2)
        _pm._apply(stats, a2, 'away', as2, hs2)
    snap = _pm._snapshot(stats)
    vals = {}
    for team, side in ((hid, 'home'), (aid, 'away')):
        for sc in _pm.SCOPES:
            info = snap.get(team, {}).get(sc)
            fields = list(info) if info else [None, 0, 0, 0, 0, 0, 0]
            for fld, v in zip(_pm.FIELDS, fields):
                vals['ps_%s_%s_%s' % (side, sc, fld)] = v
            vals['ps_%s_avg_gf_%s' % (side, sc)] = _pm._avg_gf(stats, team, sc)
    h = vals.get('ps_home_avg_gf_total')
    a = vals.get('ps_away_avg_gf_total')
    vals['ps_avg_total_goals'] = \
        round(h + a, 2) if h is not None and a is not None else None
    feat.update(vals)


def _fill_live_md(conn, mid, feat):
    """按 derive.py 同口径即場計近六場＋近20場贏盤率（DB 已有就跳過）。"""
    if _hw_outcome is None or feat.get('md_home_last6_w') is not None:
        return
    row = conn.execute(
        'SELECT m.kickoff, m.home_id, m.away_id, c.titan_id FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id WHERE m.id=?',
        (mid,)).fetchone()
    if not row:
        return
    ko, hid, aid, titan = row
    for team, side in ((hid, 'home'), (aid, 'away')):
        gms = conn.execute(
            'SELECT m.home_id, m.home_score, m.away_score, o.handicap, '
            'o.giver FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'LEFT JOIN odds_asian o ON o.match_id=m.id '
            "AND o.label='closing' AND o.company_id=12 "
            'WHERE c.titan_id=? AND m.home_score IS NOT NULL '
            'AND m.kickoff < ? AND (m.home_id=? OR m.away_id=?) '
            'ORDER BY m.kickoff DESC, m.id DESC', (titan, ko, team, team)
        ).fetchall()
        # 近六場 勝/和/負/積分
        w = d = l = 0
        for mh, hs, as_, _hp, _gv in gms[:6]:
            gf, ga = (hs, as_) if mh == team else (as_, hs)
            if gf > ga:
                w += 1
            elif gf == ga:
                d += 1
            else:
                l += 1
        if gms:
            feat['md_%s_last6_w' % side] = w
            feat['md_%s_last6_d' % side] = d
            feat['md_%s_last6_l' % side] = l
            feat['md_%s_last6_pts' % side] = 3 * w + d
        else:
            feat['md_%s_last6_w' % side] = None
            feat['md_%s_last6_d' % side] = None
            feat['md_%s_last6_l' % side] = None
            feat['md_%s_last6_pts' % side] = None
        # 近20場贏盤率（venue: home 只計主場 / away 只計客場 / all 全部）
        for venue in (('home', 'all') if side == 'home' else ('away', 'all')):
            win = lose = 0
            for mh, hs, as_, hp, gv in gms[:20]:
                is_home = (mh == team)
                if venue == 'home' and not is_home:
                    continue
                if venue == 'away' and is_home:
                    continue
                out = _hw_outcome(is_home,
                                  hs if is_home else as_,
                                  as_ if is_home else hs, gv, hp)
                if out == 'win':
                    win += 1
                elif out == 'lose':
                    lose += 1
            denom = win + lose
            feat['md_%s_hw_winrate_%s' % (side, venue)] = \
                round(win / denom, 4) if denom else None


def pred_match(feat, p):
    for sep in ('~', ':', '='):
        if sep in p:
            k, b = p.rsplit(sep, 1)
            break
    else:
        return False
    v = feat.get(k)
    if sep == '~':
        if k.startswith('x2_'):
            return prob_band(v) == b
        return water_band(v) == b
    if sep == ':':
        if k in RANK_KEYS:
            return rank_band(v) == b
        if 'winrate' in k:
            return rate_band(v) == b
        if 'pts' in k or k.startswith('md_home_last6_w') or \
                k.startswith('md_away_last6_w'):
            return pts_band(v) == b
        if k.startswith('ps_'):
            return diff_band(v, 0.25, 0, 6) == b
        if 'line' in k and k.startswith('move_'):
            return line_bucket(v) == b
        if k.startswith('move_'):
            return diff_band(v, 0.05, -0.5, 0.5) == b
        return False
    if sep == '=':
        if k == 'neut':
            return v == 1
        if 'handicap' in k or k.startswith('ah_sline') or k == 'line_c':
            return line_bucket(v) == b
        if k.startswith('ou_') and 'total_line' in k:
            return ou_line_bucket(v) == b
        return water_band(v) == b
    return False


# ---------- 單場特徵（同 lf_mine_all 特徵名對齊） ----------

def features_for(conn, mid, fill_live=True):
    feat = {}
    base = conn.execute(
        'SELECT m.kickoff, m.is_neutral, c.req_name, '
        'ht.name_tc, at.name_tc, oc.handicap, oc.giver, oc.home_odds, '
        'oc.away_odds '
        'FROM matches m JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'LEFT JOIN teams ht ON ht.titan_id=m.home_id '
        'LEFT JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'WHERE m.id=?', (mid,)).fetchone()
    if not base:
        return None
    (ko, neut, lg, home, away, hc, gv, cho, cao) = base
    feat['line_c'] = signed_line(hc, gv)
    feat['cho_c'] = cho
    feat['cao_c'] = cao
    feat['upw_c'] = cho if gv in ('home', None, 'none') else cao
    feat['dnw_c'] = cao if gv in ('home', None, 'none') else cho
    feat['neut'] = neut

    def grab(table, cols, tps, pfx):
        for tp in tps:
            row = conn.execute(
                'SELECT %s FROM %s WHERE match_id=? AND company_id=12 '
                "AND label='%s' ORDER BY change_time DESC LIMIT 1"
                % (', '.join(cols), table, tp), (mid,)).fetchone()
            for i, c in enumerate(cols):
                feat['%s%s_%s' % (pfx, c, tp)] = row[i] if row else None

    grab('odds_asian', ['handicap', 'giver', 'home_odds', 'away_odds'],
         TP7, 'ah_')
    grab('odds_ou', ['total_line', 'over_odds', 'under_odds'], TP7, 'ou_')
    grab('odds_1x2', ['home_odds', 'draw_odds', 'away_odds'], TP7, 'x2_')
    grab('odds_ah_half', ['handicap', 'giver', 'home_odds', 'away_odds'],
         TP_SHORT, 'ahh_')
    grab('odds_ou_half', ['total_line', 'over_odds', 'under_odds'],
         TP_SHORT, 'ouh_')
    grab('odds_corner', ['handicap', 'giver', 'home_odds', 'away_odds'],
         TP_SHORT, 'cn_')

    for tp in TP7:
        feat['ah_sline_%s' % tp] = signed_line(
            feat.get('ah_handicap_%s' % tp), feat.get('ah_giver_%s' % tp))
        ho = feat.get('ah_home_odds_%s' % tp)
        ao = feat.get('ah_away_odds_%s' % tp)
        gv2 = feat.get('ah_giver_%s' % tp)
        feat['ah_upw_%s' % tp] = ho if gv2 in ('home', None, 'none') else ao
    for a, b in (('initial', 'pre_4h'), ('pre_4h', 'closing'),
                 ('initial', 'closing')):
        la, lb = feat.get('ah_sline_%s' % a), feat.get('ah_sline_%s' % b)
        feat['move_line_%s_%s' % (a, b)] = \
            None if la is None or lb is None else lb - la
        wa, wb = feat.get('ah_upw_%s' % a), feat.get('ah_upw_%s' % b)
        feat['move_upw_%s_%s' % (a, b)] = \
            None if wa is None or wb is None else wb - wa

    ps = conn.execute('SELECT * FROM match_prestandings WHERE match_id=?',
                      (mid,)).fetchone()
    if ps:
        cols = [d[0] for d in conn.execute(
            'SELECT * FROM match_prestandings LIMIT 0').description]
        for c, v in zip(cols, ps):
            if c != 'match_id':
                feat['ps_%s' % c] = v
    md = conn.execute('SELECT * FROM match_derived WHERE match_id=?',
                      (mid,)).fetchone()
    if md:
        cols = [d[0] for d in conn.execute(
            'SELECT * FROM match_derived LIMIT 0').description]
        for c, v in zip(cols, md):
            if c != 'match_id':
                feat['md_%s' % c] = v

    if fill_live:
        # 未來場次：prestandings／derived 未有人寫入，按 prematch.py／derive.py
        # 同口径即場計（DB 已有值嘅話 fill 函數會自己跳過）
        _fill_live_ps(conn, mid, feat)
        _fill_live_md(conn, mid, feat)

    h, a = feat.get('ps_home_total_rank'), feat.get('ps_away_total_rank')
    feat['rankdiff_total'] = None if h is None or a is None else a - h
    h, a = feat.get('ps_home_home_rank'), feat.get('ps_away_away_rank')
    feat['rankdiff_homeaway'] = None if h is None or a is None else a - h
    h, a = feat.get('md_home_last6_pts'), feat.get('md_away_last6_pts')
    feat['pts_diff_l6'] = None if h is None or a is None else h - a
    return {'feat': feat, 'kickoff': ko, 'league': lg, 'home': home,
            'away': away, 'hc': hc, 'gv': gv, 'cho': cho, 'cao': cao}


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS fw8x_log(
        match_id INTEGER PRIMARY KEY, kickoff TEXT, league TEXT,
        home TEXT, away TEXT, direction TEXT, preds TEXT, hit_back REAL,
        n_back INTEGER, line_h REAL, giver TEXT, upw REAL, dnw REAL,
        created_ts REAL, result TEXT, settled INTEGER DEFAULT 0)''')
    conn.commit()


def pick_result(hc, gv, hs, aws):
    if hc is None or hs is None or aws is None:
        return None
    mg = hs - aws - hc if gv == 'home' else \
        (aws - hs - hc if gv == 'away' else hs - aws)
    return 'A' if mg > 0 else ('B' if mg < 0 else 'P')


def scan(conn, hours=168, grace_min=10):
    """賽前掃描：未來 hours 小時內（開賽前 grace 分鐘起）嘅場次，
    聯賽有公式且條件全中→記錄。回傳新增場數。"""
    migrate(conn)
    formulas = load_formulas()
    if not formulas:
        return 0
    now = time.time()
    rows = conn.execute(
        'SELECT m.id, c.req_name FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        "WHERE m.kickoff > datetime('now', 'localtime', '-%d minutes') "
        "AND m.kickoff < datetime('now', 'localtime', '+%d hours') "
        'AND c.req_name IN (%s) AND m.home_score IS NULL'
        % (grace_min, hours, ','.join('?' * len(formulas))),
        tuple(formulas.keys())).fetchall()
    added = 0
    for mid, lg in rows:
        if conn.execute('SELECT 1 FROM fw8x_log WHERE match_id=?',
                        (mid,)).fetchone():
            continue
        got = eval_match(conn, mid, lg)
        if not got:
            continue
        info, f = got
        conn.execute(
            'INSERT OR IGNORE INTO fw8x_log VALUES(?,?,?,?,?,?,?,?,?,'
            '?,?,?,?,?,?,?)',
            (mid, info['kickoff'], info['league'], info['home'],
             info['away'], f['dir'], json.dumps(f['preds'],
                                                ensure_ascii=False),
             f.get('hit'), f.get('n'), info['hc'], info['gv'],
             info['cho'], info['cao'], now, None, 0))
        added += 1
    conn.commit()
    return added


def settle(conn):
    """賽後結算：有賽果嘅記錄→result W/L/P。
    用落注時影低嘅 line_h/giver；legacy 行（line_h 為 NULL）先對 closing。"""
    migrate(conn)
    rows = conn.execute(
        'SELECT f.match_id, f.direction, f.line_h, f.giver, '
        'oc.handicap, oc.giver, m.home_score, m.away_score FROM fw8x_log f '
        'JOIN matches m ON m.id=f.match_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=f.match_id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'WHERE f.settled=0 AND m.home_score IS NOT NULL').fetchall()
    n = 0
    for mid, d, lh, lgv, chc, cgv, hs, aws in rows:
        hc, gv = (lh, lgv) if lh is not None else (chc, cgv)
        pr = pick_result(hc, gv, hs, aws)
        if pr is None:
            continue
        res = 'P' if pr == 'P' else ('W' if (pr == 'A') == (d == 'up')
                                     else 'L')
        conn.execute('UPDATE fw8x_log SET result=?, settled=1 WHERE '
                     'match_id=?', (res, mid))
        n += 1
    conn.commit()
    return n


def stats_and_list(conn, limit=300):
    migrate(conn)
    st = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN result='W' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN result='L' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN result='P' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN result IS NULL THEN 1 ELSE 0 END) "
        'FROM fw8x_log').fetchone()
    by_lg = conn.execute(
        "SELECT league, COUNT(*), "
        "SUM(CASE WHEN result='W' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN result='L' THEN 1 ELSE 0 END) "
        "FROM fw8x_log WHERE result IN ('W','L') GROUP BY league "
        'ORDER BY COUNT(*) DESC').fetchall()
    by_dir = [dict(zip(('dir', 'n', 'w', 'l', 'p'), r)) for r in conn.execute(
        "SELECT direction, COUNT(*), SUM(result='W'), SUM(result='L'), "
        "SUM(result='P') FROM fw8x_log GROUP BY direction")]
    rows = conn.execute(
        'SELECT match_id, kickoff, league, home, away, direction, hit_back,'
        ' n_back, line_h, giver, upw, dnw, created_ts, result, preds '
        'FROM fw8x_log ORDER BY (result IS NULL) DESC, kickoff DESC '
        'LIMIT ?', (limit,)).fetchall()
    _mkt = {lg: (f.get('market') or 'draw')
            for lg, f in load_formulas().items()}
    return {'total': st[0] or 0, 'w': st[1] or 0, 'l': st[2] or 0,
            'p': st[3] or 0, 'pending': st[4] or 0,
            'by_league': [{'league': r[0], 'n': r[1], 'w': r[2] or 0,
                           'l': r[3] or 0} for r in by_lg],
            'by_dir': by_dir,
            'rows': [{'match_id': r[0], 'kickoff': r[1], 'league': r[2],
                      'home': r[3], 'away': r[4], 'dir': r[5],
                      'hit_back': r[6], 'n_back': r[7], 'line_h': r[8],
                      'giver': r[9], 'upw': r[10], 'dnw': r[11],
                      'sline': signed_line(r[8], r[9]),
                      'ts': r[12], 'result': r[13],
                      'preds': json.loads(r[14]) if r[14] else [],
                      'market': _mkt.get(r[2], 'draw')} for r in rows]}


def newsince(conn, since):
    migrate(conn)
    # 2026-10-22 修正：conn 行工廠係預設元組，dict(元組) 會 TypeError
    # 「object is not iterable」——/api/8x/new 500 元兇。改用 zip 配欄位名。
    cur = conn.execute(
        'SELECT match_id, kickoff, league, home, away, direction, '
        'created_ts FROM fw8x_log WHERE created_ts > ? '
        'ORDER BY created_ts', (since,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def eval_match(conn, mid, lg=None):
    """單場評估（2026-10-22 資格重檢 sweep 用）：聯賽有公式且條件全中
    → (info, formula)；否則 None。同 scan 用緊同一套邏輯，唔會漂移。"""
    if lg is None:
        row = conn.execute(
            'SELECT c.req_name FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'WHERE m.id=?', (mid,)).fetchone()
        lg = row[0] if row else None
    formulas = load_formulas()
    if not lg or lg not in formulas:
        return None
    f = formulas[lg]
    info = features_for(conn, mid, fill_live=_needs_live(f))
    if not info:
        return None
    # 2026-10-25 深盤守衛（蘇超流浪 4-0 聖莊士東事件）：down 公式喺深盤
    # （|尾盤|≥1.75——三方案回測實證 1.75 最佳：基準 370 注 78.7%｜≥1.5→78.9%｜
    # ≥1.75→358 注 79.1%✓｜升盤收窄→78.6% 棄）——班霸對弱旅臨場升盤係市場
    # 確認實力差距（1.75→2.0），誘盤邏輯失效，照推下盤係送人。up 唔受影響。
    if f.get('dir') == 'down' and info.get('hc') is not None \
            and abs(info['hc']) >= 1.75:
        return None
    # 2026-10-25 live 腐化門禁（同好波門禁一脈）：公式掘礦期靚唔代表長生——
    # 該聯賽公式 live 結算 n≥8 且命中率 <60% 即自動停產（流浪事件嘅泛化：
    # 小樣本公式遇情境失效要有自動斷路器）。動態查 fw8x_log，零檔案自維護。
    try:
        _lv = conn.execute(
            "SELECT COUNT(*), SUM(result='W'), SUM(result='L'), SUM(result='P') "
            "FROM fw8x_log WHERE league=? AND result IS NOT NULL", (lg,)).fetchone()
        if _lv[0] >= 8 and \
                ((_lv[1] or 0) + 0.5 * (_lv[3] or 0)) / _lv[0] < 0.60:
            return None
    except Exception:
        pass
    if all(pred_match(info['feat'], p) for p in f['preds']):
        return info, f
    return None


_stop = threading.Event()


def loop(interval=1200):
    """每 20 分鐘 scan+settle（開機後等 3 分鐘熱身）。"""
    time.sleep(180)
    while not _stop.is_set():
        try:
            conn = sqlite3.connect(
                os.environ.get('DB_PATH') or
                os.path.join(WS, 'football.db'), timeout=120)
            a = scan(conn)
            s = settle(conn)
            conn.close()
            if a or s:
                print('[fw8x] scan +%d settle %d' % (a, s), flush=True)
        except Exception:
            traceback.print_exc()
        _stop.wait(interval)


def start_loop():
    threading.Thread(target=loop, daemon=True).start()
