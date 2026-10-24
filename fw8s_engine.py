# -*- coding: utf-8 -*-
"""fw8s_engine.py — 精選8s：專搵上盤嘅分聯賽獨立公式引擎（2026-10-14）

來源：fw8s_mine*.py 三輪挖掘＋分段驗證（訓練<2024-07；驗證 2024-07 起三段全過 55%，
整體 ≥65%，走盤剔除）。每個聯賽/杯賽一條獨立公式，兩類：
  grid 型：GRID8 入面指定格全部投上（θ0.50、孖生池≥5）
  slice 型：家族＝同向（初盤→尾盤讓球方向不變）＋尾盤＝客讓0.75
注意：呢 6 條係「上盤訊號稀薄嘅例外」，必須紙上交易監察；
每月覆核，連續兩個月低於 58% 應停用（見 精選8s總報告.md）。
"""
import json
import sqlite3
import traceback
from datetime import timedelta

import fw_grid_engine
import screen_engine as se

# 分聯賽獨立公式（key＝competitions.name_tc；引擎會用 req_name→name_tc 映射對位）
# 2026-10-22 R5 debug：澳維超／歐國聯標記 dead——全庫回測「同向＆尾客讓0.75」
# 歷史觸發 0 次（死規則），fw8s_report 會跳過，頁面公式計數照實扣減。
FORMULAS = {
    '智利甲': {'type': 'grid', 'cells': ['1I', '1O', '3I', '7'],
              'desc': '格1I+1O+3I+7 全部投上'},
    '美冠聯': {'type': 'grid', 'cells': ['1I', '1O', '7', '21'],
              'desc': '格1I+1O+7+21 全部投上'},
    '歐霸盃': {'type': 'slice', 'fam': '同向', 'sc': -0.75,
              'desc': '同向＆尾盤客讓0.75'},
    '澳維超': {'type': 'slice', 'fam': '同向', 'sc': -0.75,
              'desc': '同向＆尾盤客讓0.75（2026-10-22 回測 n=0，停用）',
              'status': 'dead'},
    '瑞典盃': {'type': 'slice', 'fam': '同向', 'sc': -0.75,
              'desc': '同向＆尾盤客讓0.75'},
    '歐國聯': {'type': 'slice', 'fam': '同向', 'sc': -0.75,
              'desc': '同向＆尾盤客讓0.75（2026-10-22 回測 n=0，停用）',
              'status': 'dead'},
}


def _active_formulas():
    return {k: f for k, f in FORMULAS.items()
            if f.get('status') != 'dead'}


def _name_tc_map(conn):
    return dict(conn.execute('SELECT req_name, name_tc FROM competitions'))


def _signed(T):
    """目標線 → 帶符號讓球（主隊角度）"""
    if T is None or T.get('h') is None:
        return None
    return T['h'] * {'home': 1, 'away': -1, 'none': 0}.get(T.get('g') or 'none', 0)


def fw8s_report(conn, t, grid8_rep=None, ctx=None):
    """精選8s 單場檢查。回傳同 fw_grid_engine.grid_report 相容嘅精簡結構。
    grid8_rep（2026-10-22 加速重構）：grid 型公式要用嘅 12 格報告——統一掃描
    已經計過嘅話傳入嚟慳一次重複運算（grid_report 約 0.75 秒/場）。
    ctx（2026-10-25）：v3.build_context 共享——grid_report 每格組合自建一次
    context（約 0.8s/個），統一評估共用先至慳。（slice 型唔用 ctx。）"""
    lg_tc = _name_tc_map(conn).get(t.get('league'), t.get('league'))
    f = _active_formulas().get(lg_tc)
    if f is None:
        return {'pass': False, 'direction': None, 'fail': '此聯賽沒有精選8s公式',
                'formula': None, 'grid': '8s'}
    out = {'pass': False, 'direction': None, 'fail': None,
           'formula': lg_tc + '：' + f['desc'], 'grid': '8s', 'cells': []}
    if f['type'] == 'slice':
        Ti = se.tline(t.get('odds') or {}, 'initial')
        Tc = se.tline(t.get('odds') or {}, 'closing')
        s_i, s_c = _signed(Ti), _signed(Tc)
        if s_i is None or s_c is None:
            out['fail'] = '缺少初盤或尾盤'
            return out
        same_dir = (s_i != 0 and s_c != 0 and (s_i > 0) == (s_c > 0))
        if not same_dir:
            out['fail'] = '讓球方向有變（非同向）'
            return out
        if abs(s_c - f['sc']) > 1e-9:
            out['fail'] = '尾盤不是客讓0.75（現為 {}）'.format(
                se.fmt_line(Tc['h'], Tc['g']))
            return out
        out['pass'] = True
        out['direction'] = 'up'
        out['fail'] = None
        out['cells'] = [{'cell': 'FAM', 'name': '初盤→尾盤方向', 'dir': 'up',
                         'n': 0, 'up_r': None, 'down_r': None},
                        {'cell': 'SC', 'name': '尾盤盤口', 'dir': 'up',
                         'n': 0, 'up_r': None, 'down_r': None}]
        return out
    # grid 型：借 fw_grid_engine 計全部 12 格方向（統一掃描已計嘅重用）
    rep = grid8_rep if grid8_rep is not None \
        else fw_grid_engine.grid_report(conn, t, '8', ctx=ctx)
    cells = {c['cell']: c for c in rep.get('cells', [])}
    dirs_, missing, downs = [], [], []
    for cn in f['cells']:
        c = cells.get(cn) or {}
        d = c.get('dir')
        if d is None:
            missing.append(cn)
        elif d != 'up':
            downs.append(cn)
        dirs_.append(d)
    out['cells'] = rep.get('cells', [])
    if missing:
        out['fail'] = '格未有方向：' + '+'.join(missing)
    elif downs:
        out['fail'] = '格未投上：' + '+'.join(downs)
    else:
        out['pass'] = True
        out['direction'] = 'up'
        out['fail'] = None
    return out


def fw8s_meta(rep):
    """detail JSON 瘦身版（同 _fw_grid_meta 用法）。"""
    slim = [{'cell': c['cell'], 'name': c['name'], 'dir': c.get('dir'),
             'n': c.get('n'), 'up_r': c.get('up_r'), 'down_r': c.get('down_r')}
            for c in rep.get('cells', [])]
    return json.dumps({'cells': slim, 'fail': rep.get('fail'),
                       'formula': rep.get('formula'), 'grid': '8s'},
                      ensure_ascii=False)


# ---------- /api/fw8s 合約：load_rules ＋ scan_upcoming（fw8s.js 用） ----------

def load_rules(conn=None):
    """每聯賽公式表（key＝req_name）：公式聯賽→direct；其餘有 check_rows
    上盤紀錄嘅聯賽→watch。供 /api/fw8s/rules 標模式、/api/fw8s/full 計聯賽數。
    2026-10-22：澳維超／歐國聯公式全庫 n=0 觸發已標 dead——active 得 4 條。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(se.DB_PATH)
    try:
        ntc = _name_tc_map(conn)
        rows = conn.execute(
            "SELECT DISTINCT c.req_name FROM check_rows cr "
            "JOIN matches m ON m.id=cr.match_id "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "WHERE cr.direction='up'").fetchall()
        rules = {}
        # 6 條公式聯賽必定 direct（即使 check_rows 未有佢哋嘅紀錄）
        tc2req = {v: k for k, v in ntc.items()}
        for tc in _active_formulas():
            rn = tc2req.get(tc)
            if rn:
                rules[rn] = {'mode': 'direct', 'conds': [FORMULAS[tc]['desc']],
                             'refine': None}
        for (rn,) in rows:
            if rn not in rules:
                rules[rn] = {'mode': 'watch', 'conds': [], 'refine': None}
        return rules
    finally:
        if own:
            conn.close()


def _hist_up_map(conn):
    """每聯賽（req_name）格共識方向上盤嘅歷史命中率（check_rows 口徑）。"""
    hist = {}
    for rn, res, n in conn.execute(
            "SELECT c.req_name, cr.res, COUNT(*) FROM check_rows cr "
            "JOIN matches m ON m.id=cr.match_id "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "WHERE cr.direction='up' GROUP BY 1, 2"):
        e = hist.setdefault(rn, {'A': 0, 'B': 0})
        e[res] = e.get(res, 0) + n
    out = {}
    for rn, e in hist.items():
        eff = e.get('A', 0) + e.get('B', 0)
        out[rn] = {'up_r': round(e.get('A', 0) / eff, 4) if eff else None,
                   'n': eff}
    return out


def scan_upcoming(conn, hours=72):
    """掃描未來 hours 小時未開賽場次。
    入選＝6 個公式聯賽內 fw8s_report 通過（寫入 fw8s_log，INSERT OR IGNORE）；
    觀望＝格8共識方向上盤但公式未過（refine_miss）或非公式聯賽（watch）。
    回傳 (picks, watched, stats)。"""
    import time as _time
    import v3_engine
    now = v3_engine.hk_now_str()
    till = (v3_engine.hk_now() + timedelta(hours=hours)
            ).strftime('%Y-%m-%d %H:%M:%S')
    ntc = _name_tc_map(conn)
    hist = _hist_up_map(conn)
    rows = conn.execute(
        "SELECT DISTINCT m.id, c.req_name FROM matches m "
        "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
        "AND oc.company_id=12 AND oc.handicap IS NOT NULL "
        "JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "WHERE m.home_score IS NULL AND m.kickoff >= ? AND m.kickoff <= ? "
        "ORDER BY m.kickoff", (now, till)).fetchall()
    # 格8 已掃出方向上盤嘅場（觀望候選；唔使逐場行 grid_report）
    grid8_up = {r[0] for r in conn.execute(
        "SELECT match_id FROM fw_grid_featured "
        "WHERE grid='8' AND direction='up' AND result IS NULL")}
    f_rows = [(mid, rn) for mid, rn in rows
              if ntc.get(rn, rn) in _active_formulas()]
    # 預熱 V3 池（grid 型公式要用；有快取就即刻返）
    if any(FORMULAS[ntc.get(rn, rn)]['type'] == 'grid' for _, rn in f_rows):
        v3_engine.load_v3_pool(conn)
    picks, watched = [], []
    for mid, rn in f_rows:
        try:
            t = se.get_target(conn, mid)
            if not t:
                continue
            rep = fw8s_report(conn, t)
            oc = (t.get('odds') or {}).get('closing') or {}
            line = se.fmt_line(oc.get('h'), oc.get('g')) \
                if oc.get('h') is not None else None
            odds = ('主{}/客{}'.format(oc.get('ho'), oc.get('ao'))
                    if oc.get('ho') is not None else None)
            hu = hist.get(rn, {})
            base = {'match_id': mid, 'kickoff': t['kickoff'], 'league': rn,
                    'home': t['home'], 'away': t['away'],
                    'line': line, 'odds': odds,
                    'hist_up_r': hu.get('up_r'), 'hist_n': hu.get('n')}
            f = FORMULAS[ntc.get(rn, rn)]
            if rep.get('pass'):
                fcells = f.get('cells')
                if fcells:
                    cmap = {c['cell']: c for c in (rep.get('cells') or [])}
                    base['cells'] = [{'cell': cn, 'dir': (cmap.get(cn) or {}).get('dir'),
                                      'up_r': (cmap.get(cn) or {}).get('up_r'),
                                      'down_r': (cmap.get(cn) or {}).get('down_r'),
                                      'n': (cmap.get(cn) or {}).get('n') or 0}
                                     for cn in fcells]
                else:
                    base['cells'] = [{'cell': c.get('cell'), 'dir': c.get('dir'),
                                      'up_r': None, 'down_r': None,
                                      'n': 0} for c in (rep.get('cells') or [])]
                picks.append(dict(base, mode='direct', conds=[f['desc']],
                                  miss_conds=None))
            elif mid in grid8_up:
                watched.append(dict(
                    base, mode='refine_miss', conds=[f['desc']],
                    miss_conds=[rep.get('fail') or '公式未通過']))
        except Exception:
            traceback.print_exc()
    # 非公式聯賽：格8共識上＋歷史未過 65% → watch
    for mid, rn in rows:
        if ntc.get(rn, rn) in _active_formulas() or mid not in grid8_up:
            continue
        try:
            t = se.get_target(conn, mid)
            if not t:
                continue
            oc = (t.get('odds') or {}).get('closing') or {}
            hu = hist.get(rn, {})
            watched.append({
                'match_id': mid, 'kickoff': t['kickoff'], 'league': rn,
                'home': t['home'], 'away': t['away'], 'mode': 'watch',
                'conds': [], 'miss_conds': None,
                'line': se.fmt_line(oc.get('h'), oc.get('g'))
                if oc.get('h') is not None else None,
                'odds': ('主{}/客{}'.format(oc.get('ho'), oc.get('ao'))
                         if oc.get('ho') is not None else None),
                'hist_up_r': hu.get('up_r'), 'hist_n': hu.get('n')})
        except Exception:
            traceback.print_exc()
    picks.sort(key=lambda r: r['kickoff'])
    watched.sort(key=lambda r: r['kickoff'])
    st = {'n': 0, 'w': 0, 'l': 0, 'p': 0}
    try:
        for res, in conn.execute(
                'SELECT result FROM fw8s_log WHERE result IS NOT NULL'):
            st['n'] += 1
            if res in ('W', 'L', 'P'):
                st[res.lower()] += 1
    except sqlite3.OperationalError:
        pass  # 爬蟲鎖庫：統計今次略過
    eff = st['w'] + st['l']
    st['hit_r'] = round(st['w'] / eff * 100, 1) if eff else None
    return picks, watched, st


def log_picks(conn, recs):
    """記低入選場次（app 層背景執行＋重試；爬蟲長寫交易鎖庫，同步寫會 500）。"""
    import time as _time
    conn.execute('''CREATE TABLE IF NOT EXISTS fw8s_log(
        match_id INTEGER PRIMARY KEY, league TEXT, direction TEXT,
        mode TEXT, conds TEXT, line TEXT, odds TEXT,
        added_at TEXT, result TEXT, settled_at TEXT)''')
    now = _time.strftime('%Y-%m-%d %H:%M:%S')
    for rec in recs:
        conn.execute(
            'INSERT INTO fw8s_log(match_id, league, direction, mode, conds, '
            'line, odds, added_at) VALUES(?,?,?,?,?,?,?,?) '
            'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
            'mode=excluded.mode, conds=excluded.conds, line=excluded.line, '
            'odds=excluded.odds',
            (rec['match_id'], rec['league'], 'up', rec['mode'],
             json.dumps(rec.get('conds') or [], ensure_ascii=False),
             rec.get('line'), rec.get('odds'), now))
    conn.commit()
