# -*- coding: utf-8 -*-
"""88W 引擎（2026-10-19）：三市場逐聯賽規則庫。
上盤／下盤＝league_formula（fw8x 公式，方向睇 dir）；
雙數和波＝k3_draw_rules.json（k3_mine.py 逐聯賽挖掘，時段分割驗證過）。
賽前掃描入 k3_log，賽後結算（和=W、唔和=L；順便記雙數中唔中）。
"""
import json
import os
import sqlite3
import threading
import time
import traceback

WS = os.path.dirname(os.path.abspath(__file__))
K3_RULES_PATH = os.path.join(WS, 'k3_draw_rules.json')
DB_PATH = os.environ.get('DB_PATH') or os.path.join(WS, 'football.db')

# 賠率標籤優先次序（攞最接近開賽嘅一筆）
_LABEL_PRI = ('closing', 'pre_5m', 'pre_15m', 'pre_30m', 'pre_4h', 'initial')

_k3_rules = None
_k3_ts = 0


def load_k3_rules(force=False):
    global _k3_rules, _k3_ts
    if not force and _k3_rules and time.time() - _k3_ts < 300:
        return _k3_rules
    try:
        with open(K3_RULES_PATH, encoding='utf-8') as f:
            _k3_rules = json.load(f)
    except Exception:
        _k3_rules = {}
    _k3_ts = time.time()
    return _k3_rules


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS k3_log(
        match_id INTEGER PRIMARY KEY, league TEXT, conds TEXT,
        ou_line REAL, hc REAL, giver TEXT,
        added_at TEXT, result TEXT, even INTEGER, settled_at TEXT)''')
    conn.commit()


def _latest(conn, mid, table):
    """攞該場最接近開賽嘅一筆盤口（odds_asian／odds_ou 通用）。"""
    rows = conn.execute(
        'SELECT label, * FROM %s WHERE match_id=? AND company_id=12' % table,
        (mid,)).fetchall()
    if not rows:
        return None
    cols = [c[1] for c in conn.execute('PRAGMA table_info(%s)' % table)]
    out = []
    for r in rows:
        d = dict(zip(['label'] + cols, r))
        out.append(d)
    out.sort(key=lambda d: _LABEL_PRI.index(d['label'])
             if d['label'] in _LABEL_PRI else 99)
    return out[0]


def _feats(conn, mid):
    """計 K3 條件用嘅特徵：尾盤（或最新）讓球＋大小線＋初盤大小線。"""
    ah = _latest(conn, mid, 'odds_asian')
    if not ah or ah.get('handicap') is None:
        return None
    ou = _latest(conn, mid, 'odds_ou')
    ou_i = None
    if ou:
        r = conn.execute(
            "SELECT total_line FROM odds_ou WHERE match_id=? AND company_id=12 "
            "AND label='initial'", (mid,)).fetchone()
        ou_i = r[0] if r else None
    hc, gv = ah['handicap'], ah.get('giver')
    ho, ao = ah.get('home_odds'), ah.get('away_odds')
    ol = ou['total_line'] if ou else None
    f = {
        'hc0': gv is None,
        'hc025': gv is not None and hc <= 0.25,
        'hc05': gv is not None and hc <= 0.5,
        'hc_shallow': gv is None or hc <= 0.5,
        'gv_home': gv == 'home',
        'gv_away': gv == 'away',
    }
    if ol is not None:
        f['ou_le20'] = ol <= 2.0
        f['ou_le25'] = ol <= 2.5
        f['ou_gt30'] = ol > 3.0
    if ol is not None and ou_i is not None:
        f['ou_drop'] = ol < ou_i
        f['ou_rise'] = ol > ou_i
    if gv and ho is not None and ao is not None:
        uw = ho if gv == 'home' else ao
        f['uw_lo'] = uw < 0.725
        f['uw_hi'] = uw >= 0.90
    return f, ah, ou


def scan(conn, hours=168, grace_min=10):
    """賽前掃描：未來 hours 小時、聯賽有 K3 規則、條件全中 → 記入 k3_log。"""
    migrate(conn)
    rules = load_k3_rules()
    if not rules:
        return 0
    rows = conn.execute(
        'SELECT m.id, c.req_name FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        "WHERE m.kickoff > datetime('now','localtime','-%d minutes') "
        "AND m.kickoff < datetime('now','localtime','+%d hours') "
        'AND c.req_name IN (%s) AND m.home_score IS NULL'
        % (grace_min, hours, ','.join('?' * len(rules))),
        tuple(rules.keys())).fetchall()
    added = 0
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for mid, lg in rows:
        if conn.execute('SELECT 1 FROM k3_log WHERE match_id=?',
                        (mid,)).fetchone():
            continue
        rule = rules.get(lg)
        if not rule:
            continue
        try:
            got = _feats(conn, mid)
            if not got:
                continue
            f, ah, ou = got
            if all(f.get(c) for c in rule['conds']):
                conn.execute(
                    'INSERT OR IGNORE INTO k3_log VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (mid, lg, json.dumps(rule['conds'], ensure_ascii=False),
                     ou['total_line'] if ou else None,
                     ah.get('handicap'), ah.get('giver'),
                     now, None, None, None))
                added += 1
        except Exception:
            traceback.print_exc()
    conn.commit()
    return added


def settle(conn):
    """賽後結算：和=W、唔和=L；even=總入球雙數。"""
    migrate(conn)
    rows = conn.execute(
        'SELECT k.match_id, m.home_score, m.away_score FROM k3_log k '
        'JOIN matches m ON m.id=k.match_id '
        'WHERE k.result IS NULL AND m.home_score IS NOT NULL').fetchall()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for mid, hs, aws in rows:
        res = 'W' if hs == aws else 'L'
        even = 1 if (hs + aws) % 2 == 0 else 0
        conn.execute('UPDATE k3_log SET result=?, even=?, settled_at=? '
                     'WHERE match_id=?', (res, even, now, mid))
    if rows:
        conn.commit()
    return len(rows)


def _k3_stats(conn):
    st = conn.execute(
        "SELECT COUNT(*), SUM(result='W'), SUM(result='L'), "
        "SUM(result IS NULL), SUM(even) FROM k3_log").fetchone()
    by_lg = [dict(zip(('league', 'n', 'w', 'l', 'even'), r)) for r in
             conn.execute(
                 "SELECT league, COUNT(*), SUM(result='W'), SUM(result='L'),"
                 ' SUM(even) FROM k3_log GROUP BY league ORDER BY 2 DESC')]
    return {'total': st[0] or 0, 'w': st[1] or 0, 'l': st[2] or 0,
            'pending': st[3] or 0, 'even': st[4] or 0, 'by_league': by_lg}


def full(conn, limit=300):
    """88W 頁面數據：三市場規則庫＋最近預測＋統計。"""
    migrate(conn)
    # K3 雙數和波預測列表
    k3_rows = conn.execute(
        'SELECT k.match_id, k.league, k.conds, k.ou_line, k.hc, k.giver, '
        'k.added_at, k.result, k.even, m.kickoff, m.home_score, m.away_score, '
        'h.name_tc, a.name_tc FROM k3_log k '
        'JOIN matches m ON m.id=k.match_id '
        'JOIN teams h ON h.titan_id=m.home_id '
        'JOIN teams a ON a.titan_id=m.away_id '
        'ORDER BY (k.result IS NULL) DESC, m.kickoff DESC LIMIT ?',
        (limit,)).fetchall()
    k3_list = [{'match_id': r[0], 'league': r[1],
                'conds': json.loads(r[2]) if r[2] else [],
                'ou_line': r[3], 'hc': r[4], 'giver': r[5],
                'added_at': r[6], 'result': r[7], 'even': r[8],
                'kickoff': r[9],
                'score': ('%s-%s' % (r[10], r[11]))
                if r[10] is not None else None,
                'home': r[12], 'away': r[13],
                'label': 'K3開發雙數和波'} for r in k3_rows]
    # 三市場規則庫
    try:
        import fw8x
        formulas = fw8x.load_formulas()
    except Exception:
        formulas = {}
    library = {}
    for lg, f in formulas.items():
        slot = library.setdefault(lg, {'league': lg})
        slot['up' if f.get('dir') == 'up' else 'down'] = {
            'hit': f.get('hit'), 'n': f.get('n'), 'preds': f.get('preds'),
            'label': 'K3上盤' if f.get('dir') == 'up' else '下盤公式'}
    for lg, r in load_k3_rules().items():
        slot = library.setdefault(lg, {'league': lg})
        slot['draw'] = {'conds': r['conds'], 'train_n': r['train_n'],
                        'train_draw': r['train_draw'],
                        'test_n': r['test_n'], 'test_draw': r['test_draw'],
                        'even_rate': r.get('even_rate'),
                        'draw_in_even': r.get('draw_in_even'),
                        'label': 'K3開發雙數和波'}
    return {'k3': {'stats': _k3_stats(conn), 'rows': k3_list},
            'library': sorted(library.values(), key=lambda x: x['league'])}


_stop = threading.Event()


def loop(interval=1200):
    """每 20 分鐘 scan+settle（開機等 4 分鐘，唔好同 fw8x 一齊搶鎖）。"""
    time.sleep(240)
    while not _stop.is_set():
        try:
            conn = sqlite3.connect(DB_PATH, timeout=120)
            a = scan(conn)
            s = settle(conn)
            conn.close()
            if a or s:
                print('[88w] k3 scan +%d settle %d' % (a, s), flush=True)
        except Exception:
            traceback.print_exc()
        _stop.wait(interval)


def start_loop():
    threading.Thread(target=loop, daemon=True).start()
