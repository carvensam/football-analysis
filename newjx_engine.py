# -*- coding: utf-8 -*-
"""新精選引擎（2026-10-19）：16 欄位全組合挖掘嘅逐聯賽上下盤最高命中方法。

規則來源：jx_rules.json（jx_mine.py：65,535 組合暴力枚舉，訓練 ≤2024 ＋
驗證 2025+ 兩期 ≥72.5% 兼 n≥5，55/75 聯賽達標，每聯賽一條最高命中方法）。
賽前掃描入 newjx_log，賽後按方向結算（上盤：主讓 diff-hc>0／客讓 aws-hs-hc>0）。
"""
import json
import os
import sqlite3
import threading
import time
import traceback

WS = os.path.dirname(os.path.abspath(__file__))          # screen_app/
BASE_DIR = os.path.dirname(WS)                           # 項目根（DB 所在）
RULES_PATH = os.path.join(WS, 'jx_rules.json')
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'football.db')

_LABEL_PRI = ('closing', 'pre_5m', 'pre_15m', 'pre_30m', 'pre_4h', 'initial')

# 用到賽前排名／近績嘅條件
PS_CONDS = {'rank_h', 'rank_a', 'hh_wr_hi', 'aa_wr_hi', 'avg_hi'}

_rules = None
_rules_ts = 0


def load_rules(force=False):
    global _rules, _rules_ts
    if not force and _rules and time.time() - _rules_ts < 300:
        return _rules
    try:
        with open(RULES_PATH, encoding='utf-8') as f:
            _rules = json.load(f)
    except Exception:
        _rules = {}
    _rules_ts = time.time()
    return _rules


def migrate(conn):
    # 讀檢查先行：表已存在就唔使攞寫鎖（爬蟲長寫入時頁面載入唔會被阻塞）
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='newjx_log'").fetchone()
    if r:
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS newjx_log(
        match_id INTEGER PRIMARY KEY, league TEXT, dir TEXT, conds TEXT,
        hc REAL, giver TEXT,
        added_at TEXT, result TEXT, settled_at TEXT)''')
    conn.commit()


def _latest(conn, mid, table):
    """攞該場最接近開賽嘅一筆盤口。"""
    rows = conn.execute(
        'SELECT label, * FROM %s WHERE match_id=? AND company_id=12' % table,
        (mid,)).fetchall()
    if not rows:
        return None
    cols = [c[1] for c in conn.execute('PRAGMA table_info(%s)' % table)]
    out = [dict(zip(['label'] + cols, r)) for r in rows]
    out.sort(key=lambda d: _LABEL_PRI.index(d['label'])
             if d['label'] in _LABEL_PRI else 99)
    return out[0]


def _signed(hc, gv):
    if hc is None:
        return None
    return 0.0 if gv is None else (hc if gv == 'home' else -hc)


def _odds_feats(conn, mid):
    """盤口類條件（對齊 jx_mine.py 口徑）。回傳 (feats, ah) 或 None。"""
    ah = _latest(conn, mid, 'odds_asian')
    if not ah or ah.get('handicap') is None:
        return None
    ou = _latest(conn, mid, 'odds_ou')
    ou_i = conn.execute(
        "SELECT total_line FROM odds_ou WHERE match_id=? AND company_id=12 "
        "AND label='initial'", (mid,)).fetchone()
    ah_i = conn.execute(
        "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
        "AND company_id=12 AND label='initial'", (mid,)).fetchone()
    hc, gv = ah['handicap'], ah.get('giver')
    ho, ao = ah.get('home_odds'), ah.get('away_odds')
    sc = _signed(hc, gv)
    si = _signed(ah_i[0], ah_i[1]) if ah_i else None
    f = {}
    if sc is not None:
        f['sline_hi'] = sc >= 0.75
        f['sline_lo'] = sc <= -0.75
    if ho is not None:
        f['ho_lo'] = ho <= 0.80
    if ao is not None:
        f['ao_lo'] = ao <= 0.80
    if ou:
        ol = ou.get('total_line')
        if ol is not None:
            f['ou_hi'] = ol >= 2.75
            f['ou_lo'] = ol <= 2.25
            if ou_i and ou_i[0] is not None:
                f['ou_rise'] = ol > ou_i[0]
        oo, uo = ou.get('over_odds'), ou.get('under_odds')
        if oo is not None:
            f['ov_lo'] = oo <= 0.85
        if uo is not None:
            f['un_lo'] = uo <= 0.85
    if sc is not None and si is not None:
        f['ah_upmove'] = sc - si >= 0.25
        f['ah_dnmove'] = sc - si <= -0.25
    return f, ah


def _ps_feats(feat):
    """由 fw8x.features_for 嘅 feat 計賽前條件（對齊挖掘口徑）。"""
    f = {}
    hr = feat.get('ps_home_total_rank')
    ar = feat.get('ps_away_total_rank')
    if hr is not None and ar is not None:
        f['rank_h'] = hr + 5 <= ar
        f['rank_a'] = ar + 5 <= hr
    hw = feat.get('ps_home_home_w') or 0
    hd = feat.get('ps_home_home_d') or 0
    hl = feat.get('ps_home_home_l') or 0
    aw = feat.get('ps_away_away_w') or 0
    ad = feat.get('ps_away_away_d') or 0
    al = feat.get('ps_away_away_l') or 0
    hp, ap = hw + hd + hl, aw + ad + al
    if hp:
        f['hh_wr_hi'] = hw / hp >= 0.60
    if ap:
        f['aa_wr_hi'] = aw / ap >= 0.55
    avg = feat.get('ps_avg_total_goals')
    if avg is not None:
        f['avg_hi'] = avg >= 2.8
    return f


def eval_match(conn, mid, lg=None):
    """單場評估（2026-10-22 資格重檢 sweep 用）：該聯賽方法條件全中
    → INSERT 用 vals 元組；否則 None。同 scan 共用同一套邏輯，唔會漂移。"""
    if lg is None:
        row = conn.execute(
            'SELECT c.req_name FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'WHERE m.id=?', (mid,)).fetchone()
        lg = row[0] if row else None
    rule = load_rules().get(lg) if lg else None
    if not rule:
        return None
    # 2026-10-25 live 腐化門禁（同 fw8x/hb 一脈）：方法 live n≥6 且 <60%
    # 自動停產——礦工公式嘅長生保養，防止失效公式繼續出錯預測。
    try:
        _lv = conn.execute(
            "SELECT COUNT(*), SUM(result='W'), SUM(result='L'), SUM(result='P') "
            "FROM newjx_log WHERE result IS NOT NULL").fetchone()
        if _lv[0] >= 6 and \
                ((_lv[1] or 0) + 0.5 * (_lv[3] or 0)) / _lv[0] < 0.60:
            return None
    except Exception:
        pass
    got = _odds_feats(conn, mid)
    if not got:
        return None
    of, ah = got
    conds = rule['conds']
    if not all(of.get(c) for c in conds if c not in PS_CONDS):
        return None
    if any(c in PS_CONDS for c in conds):
        import fw8x
        info = fw8x.features_for(conn, mid, fill_live=True)
        psf = _ps_feats(info['feat']) if info else {}
        if not all(psf.get(c) for c in conds if c in PS_CONDS):
            return None
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    return (mid, lg, rule['dir'],
            json.dumps(conds, ensure_ascii=False),
            ah.get('handicap'), ah.get('giver'), now)


def scan(conn, hours=168, grace_min=10, progress=None):
    """賽前掃描：未來 hours 小時、聯賽有新精選方法、條件全中 → 記入 newjx_log。"""
    migrate(conn)
    rules = load_rules()
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

    def report(index, mid, league, status):
        if not progress:
            return
        try:
            row = conn.execute(
                'SELECT m.kickoff, ht.name_tc, at.name_tc FROM matches m '
                'JOIN teams ht ON ht.titan_id=m.home_id '
                'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
                (mid,)).fetchone()
            progress(index, len(rows), (mid, row[0], league, row[1], row[2]), status)
        except Exception:
            pass

    added = 0
    hits = []
    for index, (mid, lg) in enumerate(rows, 1):
        try:
            vals = eval_match(conn, mid, lg)
            if vals:
                hits.append(vals)
        except Exception:
            traceback.print_exc()
            report(index, mid, lg, '失敗')
            continue
        report(index, mid, lg, '完成')
    for vals in hits:
        cur = conn.execute(
            'INSERT OR IGNORE INTO newjx_log VALUES(?,?,?,?,?,?,?,?,?)',
            vals + (None, None))
        added += cur.rowcount
    if hits:
        conn.commit()
    return added


def settle(conn):
    """賽後結算：dir=up 上盤贏＝W；dir=down 下盤贏＝W；走盤＝P。
    兩段式：讀＋計算先行，短事務寫入。"""
    migrate(conn)
    rows = conn.execute(
        'SELECT k.match_id, k.dir, k.hc, k.giver, m.home_score, m.away_score '
        'FROM newjx_log k JOIN matches m ON m.id=k.match_id '
        'WHERE k.result IS NULL AND m.home_score IS NOT NULL').fetchall()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    todo = []
    for mid, d, hc, giver, hs, aws in rows:
        if hc is None:
            continue
        if giver == 'home':
            mg = hs - aws - hc
        elif giver == 'away':
            mg = aws - hs - hc
        else:
            mg = hs - aws
        if abs(mg) < 1e-9:
            res = 'P'
        else:
            win = mg > 0 if d == 'up' else mg < 0
            res = 'W' if win else 'L'
        todo.append((res, now, mid))
    for res, ts, mid in todo:
        conn.execute('UPDATE newjx_log SET result=?, settled_at=? '
                     'WHERE match_id=?', (res, ts, mid))
    if todo:
        conn.commit()
    return len(todo)


def _stats(conn):
    st = conn.execute(
        "SELECT COUNT(*), SUM(result='W'), SUM(result='L'), "
        "SUM(result='P'), SUM(result IS NULL) FROM newjx_log").fetchone()
    by_lg = [dict(zip(('league', 'dir', 'n', 'w', 'l', 'p'), r)) for r in
             conn.execute(
                 "SELECT league, dir, COUNT(*), SUM(result='W'), "
                 "SUM(result='L'), SUM(result='P') FROM newjx_log "
                 'GROUP BY league ORDER BY 3 DESC')]
    by_dir = [dict(zip(('dir', 'n', 'w', 'l', 'p'), r)) for r in
              conn.execute(
                  "SELECT dir, COUNT(*), SUM(result='W'), SUM(result='L'), "
                  "SUM(result='P') FROM newjx_log GROUP BY dir")]
    return {'total': st[0] or 0, 'w': st[1] or 0, 'l': st[2] or 0,
            'push': st[3] or 0, 'pending': st[4] or 0,
            'by_dir': by_dir, 'by_league': by_lg}


def full(conn, limit=300):
    """新精選頁面數據：規則庫＋最近預測＋統計。"""
    migrate(conn)
    rows = conn.execute(
        'SELECT k.match_id, k.league, k.dir, k.conds, k.hc, k.giver, '
        'k.added_at, k.result, m.kickoff, m.home_score, m.away_score, '
        'h.name_tc, a.name_tc FROM newjx_log k '
        'JOIN matches m ON m.id=k.match_id '
        'JOIN teams h ON h.titan_id=m.home_id '
        'JOIN teams a ON a.titan_id=m.away_id '
        'ORDER BY (k.result IS NULL) DESC, m.kickoff DESC LIMIT ?',
        (limit,)).fetchall()
    preds = [{'match_id': r[0], 'league': r[1], 'dir': r[2],
              'conds': json.loads(r[3]) if r[3] else [],
              'hc': r[4], 'giver': r[5], 'added_at': r[6], 'result': r[7],
              'kickoff': r[8],
              'score': ('%s-%s' % (r[9], r[10]))
              if r[9] is not None else None,
              'home': r[11], 'away': r[12],
              'label': '新精選上盤' if r[2] == 'up' else '新精選下盤'}
             for r in rows]
    library = [{'league': lg, 'dir': r['dir'], 'conds': r['conds'],
                'label': r.get('label'),
                'train_n': r['train_n'], 'train_hit': r['train_hit'],
                'test_n': r['test_n'], 'test_hit': r['test_hit'],
                'all_n': r['all_n'], 'all_hit': r['all_hit']}
               for lg, r in sorted(load_rules().items())]
    return {'stats': _stats(conn), 'rows': preds, 'library': library}


_stop = threading.Event()


def loop(interval=1200):
    """每 20 分鐘 scan+settle（開機等 5 分鐘，唔好同 fw8x/w88 一齊搶鎖）。"""
    time.sleep(300)
    while not _stop.is_set():
        try:
            conn = sqlite3.connect(DB_PATH, timeout=120)
            a = scan(conn)
            s = settle(conn)
            conn.close()
            if a or s:
                print('[newjx] scan +%d settle %d' % (a, s), flush=True)
        except Exception:
            traceback.print_exc()
        _stop.wait(interval)


def start_loop():
    threading.Thread(target=loop, daemon=True).start()
