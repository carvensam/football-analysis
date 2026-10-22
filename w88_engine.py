# -*- coding: utf-8 -*-
"""88W 引擎 v2（2026-10-19）：全市場逐聯賽方法庫。

規則來源：w88_rules.json（w88_mine.py v3 逐聯賽挖掘，72 聯賽 304 條方法，
訓練期 ≤2024 ＋ 驗證期 2025+ 兩邊都 ≥72.5%，各 n≥5）。
市場：ah_up/ah_down/draw/even/over/under/home_win/away_win。
賽前掃描入 w88_log（一場可命中多條方法），賽後按市場結算。
舊 k3_log（雙數和波）歷史記錄會一次性搬入 w88_log，之後唔再寫。
"""
import json
import os
import sqlite3
import threading
import time
import traceback

WS = os.path.dirname(os.path.abspath(__file__))          # screen_app/
BASE_DIR = os.path.dirname(WS)                             # 項目根（DB 所在）
RULES_PATH = os.path.join(WS, 'w88_rules.json')
DRAW_RULES_PATH = os.path.join(WS, 'k3_draw_rules.json')
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'football.db')

# 賠率標籤優先次序（攞最接近開賽嘅一筆）
_LABEL_PRI = ('closing', 'pre_5m', 'pre_15m', 'pre_30m', 'pre_4h', 'initial')

# 用到賽前排名／近績（prestandings）嘅條件
PS_CONDS = {'rank_h5', 'rank_a5', 'rank_close',
            'hh_wr_hi', 'aa_wr_lo', 'hh_wr_lo', 'aa_wr_hi',
            'avg_hi', 'avg_lo',
            'hh_gf_hi', 'aa_gf_hi', 'hh_ga_hi', 'aa_ga_hi'}

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


_draw_rules = None
_draw_rules_ts = 0


def load_draw_rules(force=False):
    """K3 雙數和波規則（獨立檔）：欄位正規化成同 w88_rules 一樣
    （all_draw→all_hit、train_draw→train_hit、test_draw→test_hit）。"""
    global _draw_rules, _draw_rules_ts
    if not force and _draw_rules and time.time() - _draw_rules_ts < 300:
        return _draw_rules
    try:
        with open(DRAW_RULES_PATH, encoding='utf-8') as f:
            raw = json.load(f)
        out = {}
        for lg, r in raw.items():
            rr = dict(r)
            rr['market'] = 'draw'
            rr['all_hit'] = r.get('all_draw')
            rr['train_hit'] = r.get('train_draw')
            rr['test_hit'] = r.get('test_draw')
            out[lg] = [rr]
        _draw_rules = out
    except Exception:
        _draw_rules = {}
    _draw_rules_ts = time.time()
    return _draw_rules


def migrate(conn):
    # 讀檢查先行：表已存在就唔使攞寫鎖（爬蟲長寫入時頁面載入唔會被阻塞）
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('w88_log', 'k3_log')")}
    if 'w88_log' in have:
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS w88_log(
        match_id INTEGER, market TEXT, conds TEXT, league TEXT, label TEXT,
        ou_line REAL, hc REAL, giver TEXT,
        added_at TEXT, result TEXT, settled_at TEXT,
        PRIMARY KEY(match_id, market, conds))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS k3_log(
        match_id INTEGER PRIMARY KEY, league TEXT, conds TEXT,
        ou_line REAL, hc REAL, giver TEXT,
        added_at TEXT, result TEXT, even INTEGER, settled_at TEXT)''')
    # 舊 k3_log（雙數和波）歷史一次性搬入 w88_log
    conn.execute(
        "INSERT OR IGNORE INTO w88_log "
        "(match_id, market, conds, league, label, ou_line, hc, giver, "
        " added_at, result, settled_at) "
        "SELECT match_id, 'draw', conds, league, 'K3開發雙數和波', "
        "ou_line, hc, giver, added_at, result, settled_at FROM k3_log")
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


def _initial(conn, mid, table, cols):
    r = conn.execute(
        "SELECT %s FROM %s WHERE match_id=? AND company_id=12 "
        "AND label='initial'" % (', '.join(cols), table), (mid,)).fetchone()
    return r


def _signed(hc, gv):
    if hc is None:
        return None
    return 0.0 if gv is None else (hc if gv == 'home' else -hc)


def _odds_feats(conn, mid):
    """盤口類條件（對齊 w88_mine.py 口徑）：
    hc0/hc025/hc05/hc075/hc_deep、gv_home/gv_away、
    ou_le20/ou_le25/ou_ge30、ou_drop/ou_rise、uw_lo/uw_hi、ah_deep/ah_shallow。
    回傳 (feats, ah, ou) 或 None（冇讓球盤）。"""
    ah = _latest(conn, mid, 'odds_asian')
    if not ah or ah.get('handicap') is None:
        return None
    ou = _latest(conn, mid, 'odds_ou')
    ou_i = _initial(conn, mid, 'odds_ou', ['total_line'])
    ah_i = _initial(conn, mid, 'odds_asian', ['handicap', 'giver'])
    hc, gv = ah['handicap'], ah.get('giver')
    ho, ao = ah.get('home_odds'), ah.get('away_odds')
    ol = ou['total_line'] if ou else None
    il = ou_i[0] if ou_i else None
    f = {
        'hc0': gv is None,
        'hc025': gv is not None and hc <= 0.25,
        'hc05': gv is not None and 0.25 < hc <= 0.5,
        'hc075': gv is not None and hc == 0.75,
        'hc_deep': gv is not None and hc >= 1.5,
        'gv_home': gv == 'home',
        'gv_away': gv == 'away',
    }
    if ol is not None:
        f['ou_le20'] = ol <= 2.0
        f['ou_le25'] = ol <= 2.5
        f['ou_ge30'] = ol >= 3.0
    if ol is not None and il is not None:
        f['ou_drop'] = ol < il
        f['ou_rise'] = ol > il
    if gv and ho is not None and ao is not None:
        uw = ho if gv == 'home' else ao
        f['uw_lo'] = uw < 0.725
        f['uw_hi'] = uw >= 0.90
    sc = _signed(hc, gv)
    si = _signed(ah_i[0], ah_i[1]) if ah_i else None
    if sc is not None and si is not None:
        f['ah_deep'] = sc > si
        f['ah_shallow'] = sc < si
    return f, ah, ou


def _ps_feats(feat):
    """由 fw8x.features_for 嘅 feat 計排名／近績類條件（對齊挖掘口徑）。"""
    f = {}
    hr = feat.get('ps_home_total_rank')
    ar = feat.get('ps_away_total_rank')
    if hr is not None and ar is not None:
        f['rank_h5'] = hr + 5 <= ar
        f['rank_a5'] = ar + 5 <= hr
        f['rank_close'] = abs(hr - ar) <= 3
    hw = feat.get('ps_home_home_w') or 0
    hd = feat.get('ps_home_home_d') or 0
    hl = feat.get('ps_home_home_l') or 0
    aw = feat.get('ps_away_away_w') or 0
    ad = feat.get('ps_away_away_d') or 0
    al = feat.get('ps_away_away_l') or 0
    hp = hw + hd + hl
    ap = aw + ad + al
    if hp:
        wr = hw / hp
        f['hh_wr_hi'] = wr >= 0.60
        f['hh_wr_lo'] = wr <= 0.30
    if ap:
        wr = aw / ap
        f['aa_wr_lo'] = wr <= 0.30
        f['aa_wr_hi'] = wr >= 0.55
    avg = feat.get('ps_avg_total_goals')
    if avg is not None:
        f['avg_hi'] = avg >= 3.0
        f['avg_lo'] = avg <= 2.3
    hgf = feat.get('ps_home_avg_gf_home')
    agf = feat.get('ps_away_avg_gf_away')
    if hgf is not None:
        f['hh_gf_hi'] = hgf >= 1.8
    if agf is not None:
        f['aa_gf_hi'] = agf >= 1.8
    hga = feat.get('ps_home_home_ga')
    aga = feat.get('ps_away_away_ga')
    if hga is not None and hp:
        f['hh_ga_hi'] = hga / hp >= 1.5
    if aga is not None and ap:
        f['aa_ga_hi'] = aga / ap >= 1.5
    return f


def eval_match(conn, mid, lg=None):
    """單場評估（2026-10-22 資格重檢 sweep 用）：條件全中嘅方法 hit 列表
    [(market, conds_key, insert_vals), ...]；冇中／冇方法庫 → 空 list。
    同 scan 共用同一套邏輯，唔會漂移。
    2026-10-22 K3 方向 bug 修正：1X2 市場（home_win/away_win）用咗
    hc_deep/hc025/hc05/hc075 幅度條件但冇寫明 gv_home/gv_away——
    舊制會喺『客讓深』場照 fire『K3主勝』（實證：美冠聯客讓深 4 場主勝
    0%，10-22 運動Jax 0-2 中招）。而家：home_win 規則含幅度條件
    但冇 gv_away → 隱式要求 gv_home；away_win 反之。挖掘期訓練命中率
    （主讓深 82.5%）證明原意係主隊方向。"""
    if lg is None:
        row = conn.execute(
            'SELECT c.req_name FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'WHERE m.id=?', (mid,)).fetchone()
        lg = row[0] if row else None
    rules = load_rules()
    if not lg or lg not in rules:
        return []
    got = _odds_feats(conn, mid)
    if not got:
        return []
    of, ah, ou = got
    psf = None  # 惰性：有方法用到 ps 條件先計一次
    hits = []
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    import fw8x
    for rule in rules.get(lg, []):
        conds = rule['conds']
        key = (mid, rule['market'], json.dumps(conds, ensure_ascii=False))
        # 2026-10-22 方向守衛（見 docstring）：1X2 市場嘅幅度條件
        # 必須指向預測嗰一方
        if set(conds) & {'hc_deep', 'hc025', 'hc05', 'hc075'}:
            if rule['market'] == 'home_win' and 'gv_away' not in conds \
                    and not of.get('gv_home'):
                continue
            if rule['market'] == 'away_win' and 'gv_home' not in conds \
                    and not of.get('gv_away'):
                continue
        # 盤口條件先過（平）；全中而又有 ps 條件先計 ps 特徵（貴）
        if not all(of.get(c) for c in conds if c not in PS_CONDS):
            continue
        if any(c in PS_CONDS for c in conds):
            if psf is None:
                info = fw8x.features_for(conn, mid, fill_live=True)
                psf = _ps_feats(info['feat']) if info else {}
            if not all(psf.get(c) for c in conds if c in PS_CONDS):
                continue
        hits.append((key, (mid, rule['market'], key[2], lg,
                           rule.get('label'),
                           ou['total_line'] if ou else None,
                           ah.get('handicap'), ah.get('giver'), now)))
    return hits


def scan(conn, hours=168, grace_min=10):
    """賽前掃描：未來 hours 小時、聯賽有方法庫、條件全中 → 記入 w88_log。
    兩段式：先讀取＋計算（唔攞寫鎖），最後先一次過短事務寫入——
    爬蟲長寫入期間唔會長時間攞住寫鎖，鎖等待集中喺最後一刻。"""
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
    hits = []
    for mid, lg in rows:
        try:
            hits.extend(eval_match(conn, mid, lg))
        except Exception:
            traceback.print_exc()
    if not hits:
        return 0
    added = 0
    for key, vals in hits:
        cur = conn.execute(
            'INSERT OR IGNORE INTO w88_log '
            '(match_id, market, conds, league, label, ou_line, hc, '
            ' giver, added_at) VALUES(?,?,?,?,?,?,?,?,?)', vals)
        added += cur.rowcount
    conn.commit()
    return added


def _settle_one(market, hs, aws, hc, giver, ou_line):
    """按市場結算：'W'／'L'／'P'（走盤）；None＝資料唔夠暫唔結。"""
    tot = hs + aws
    if market in ('ah_up', 'ah_down'):
        if hc is None:
            return None
        if giver == 'home':
            mg = hs - aws - hc
        elif giver == 'away':
            mg = aws - hs - hc
        else:
            mg = hs - aws
        if abs(mg) < 1e-9:
            return 'P'
        win = mg > 0
        if market == 'ah_down':
            win = not win
        return 'W' if win else 'L'
    if market == 'draw':
        return 'W' if hs == aws else 'L'
    if market == 'even':
        return 'W' if tot % 2 == 0 else 'L'
    if market in ('over', 'under'):
        if ou_line is None:
            return None
        if abs(tot - ou_line) < 1e-9:
            return 'P'
        win = tot > ou_line
        if market == 'under':
            win = not win
        return 'W' if win else 'L'
    if market == 'home_win':
        return 'W' if hs > aws else 'L'
    if market == 'away_win':
        return 'W' if hs < aws else 'L'
    return None


def settle(conn):
    """賽後結算 w88_log（兩段式：讀＋計算先行，短事務寫入）。"""
    migrate(conn)
    rows = conn.execute(
        'SELECT k.match_id, k.market, k.hc, k.giver, k.ou_line, '
        'm.home_score, m.away_score FROM w88_log k '
        'JOIN matches m ON m.id=k.match_id '
        'WHERE k.result IS NULL AND m.home_score IS NOT NULL').fetchall()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    todo = []
    backfill = []
    for mid, market, hc, giver, ou_line, hs, aws in rows:
        if market in ('over', 'under') and ou_line is None:
            # 2026-10-22 修正：揀嗰刻未爬到大小線而滯留嘅列——而家補查
            # closing 大小線（12 易勝博優先），搵到就回填兼結算
            r = conn.execute(
                "SELECT total_line FROM odds_ou WHERE match_id=? "
                "AND label='closing' ORDER BY CASE company_id "
                'WHEN 12 THEN 0 ELSE 1 END LIMIT 1', (mid,)).fetchone()
            if r and r[0] is not None:
                ou_line = r[0]
                backfill.append((ou_line, mid, market))
        res = _settle_one(market, hs, aws, hc, giver, ou_line)
        if res is not None:
            todo.append((res, now, mid, market))
    for ou_line, mid, market in backfill:
        conn.execute('UPDATE w88_log SET ou_line=? '
                     'WHERE match_id=? AND market=?', (ou_line, mid, market))
    for res, ts, mid, market in todo:
        conn.execute('UPDATE w88_log SET result=?, settled_at=? '
                     'WHERE match_id=? AND market=?', (res, ts, mid, market))
    if todo:
        conn.commit()
    return len(todo)


def _stats(conn):
    st = conn.execute(
        "SELECT COUNT(*), SUM(result='W'), SUM(result='L'), "
        "SUM(result='P'), SUM(result IS NULL) FROM w88_log").fetchone()
    by_mkt = [dict(zip(('market', 'label', 'n', 'w', 'l', 'p'), r)) for r in
              conn.execute(
                  "SELECT market, label, COUNT(*), SUM(result='W'), "
                  "SUM(result='L'), SUM(result='P') FROM w88_log "
                  'GROUP BY market ORDER BY 3 DESC')]
    by_lg = [dict(zip(('league', 'n', 'w', 'l', 'p'), r)) for r in
             conn.execute(
                 "SELECT league, COUNT(*), SUM(result='W'), SUM(result='L'),"
                 " SUM(result='P') FROM w88_log GROUP BY league "
                 'ORDER BY 2 DESC')]
    return {'total': st[0] or 0, 'w': st[1] or 0, 'l': st[2] or 0,
            'push': st[3] or 0, 'pending': st[4] or 0,
            'by_market': by_mkt, 'by_league': by_lg}


_BT_CACHE = {'ts': 0.0, 'data': None}


def load_backtest():
    """全庫回測結果（w88_bt.json，由 w88_backtest.py 生成；2026-10-22
    用戶指示：K3 方向 bug 修正後回跑全部歷史，八個市場命中率）。
    60 秒快取；冇檔案／未跑過 → None（頁面照舊，唔會炸）。"""
    if _BT_CACHE['data'] is not None and time.time() - _BT_CACHE['ts'] < 60:
        return _BT_CACHE['data']
    try:
        with open(os.path.join(WS, 'w88_bt.json'), encoding='utf-8') as f:
            d = json.load(f)
        _BT_CACHE['data'] = d
        _BT_CACHE['ts'] = time.time()
        return d
    except Exception:
        return None


def full(conn, limit=300):
    """88W 頁面數據：全市場方法庫＋最近預測＋統計。"""
    migrate(conn)
    rows = conn.execute(
        'SELECT k.match_id, k.league, k.market, k.label, k.conds, '
        'k.ou_line, k.hc, k.giver, k.added_at, k.result, '
        'm.kickoff, m.home_score, m.away_score, h.name_tc, a.name_tc '
        'FROM w88_log k '
        'JOIN matches m ON m.id=k.match_id '
        'JOIN teams h ON h.titan_id=m.home_id '
        'JOIN teams a ON a.titan_id=m.away_id '
        'ORDER BY (k.result IS NULL) DESC, m.kickoff DESC LIMIT ?',
        (limit,)).fetchall()
    preds = [{'match_id': r[0], 'league': r[1], 'market': r[2],
              'label': r[3],
              'conds': json.loads(r[4]) if r[4] else [],
              'ou_line': r[5], 'hc': r[6], 'giver': r[7],
              'added_at': r[8], 'result': r[9], 'kickoff': r[10],
              'score': ('%s-%s' % (r[11], r[12]))
              if r[11] is not None else None,
              'home': r[13], 'away': r[14]} for r in rows]
    # 每條預測對返方法庫嘅歷史命中率（2026-10-20 用戶要求逐場顯示）：
    # 優先（聯賽＋市場＋條件）精確對，對唔到就用（聯賽＋label）
    _rlk = {}
    for _src in (load_rules(), load_draw_rules()):
        for _lg, _rl in _src.items():
            for _r in _rl:
                _rlk[(_lg, _r['market'], tuple(_r.get('conds') or []))] = _r
                if _r.get('label'):
                    _rlk[(_lg, 'lbl', _r['label'])] = _r
    for p in preds:
        _r = _rlk.get((p['league'], p['market'], tuple(p['conds']))) \
            or _rlk.get((p['league'], 'lbl', p['label']))
        if _r:
            p['hist_hit'] = _r.get('all_hit')
            p['hist_n'] = _r.get('all_n')
    # 方法庫：w88_rules 全部方法；fw8x 讓球公式做補充
    try:
        import fw8x
        formulas = fw8x.load_formulas()
    except Exception:
        formulas = {}
    library = {}
    for _src in (load_rules(), load_draw_rules()):
        for lg, rl in _src.items():
            slot = library.setdefault(lg, {'league': lg, 'methods': []})
            for r in rl:
                slot['methods'].append({
                    'market': r['market'], 'label': r.get('label'),
                    'conds': r['conds'],
                    'train_n': r['train_n'], 'train_hit': r['train_hit'],
                    'test_n': r['test_n'], 'test_hit': r['test_hit'],
                    'all_n': r['all_n'], 'all_hit': r['all_hit']})
    for lg, f in formulas.items():
        slot = library.setdefault(lg, {'league': lg, 'methods': []})
        slot['formula'] = {
            'dir': f.get('dir'), 'hit': f.get('hit'), 'n': f.get('n'),
            'preds': f.get('preds'),
            'label': 'K3上盤' if f.get('dir') == 'up' else '下盤公式'}
    return {'stats': _stats(conn), 'rows': preds,
            'library': sorted(library.values(), key=lambda x: x['league']),
            'bt': load_backtest()}   # 2026-10-22 全庫回測（八市場命中率）


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
                print('[88w] scan +%d settle %d' % (a, s), flush=True)
        except Exception:
            traceback.print_exc()
        _stop.wait(interval)


def start_loop():
    threading.Thread(target=loop, daemon=True).start()
