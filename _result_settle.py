# -*- coding: utf-8 -*-
"""賽果回填＋精選結算（2026-10-10 用戶規格：完場後 120/150 分鐘爬賽果＋更新預測結果）。
1) 開賽逾 120 分鐘仍冇比分嘅場次 → task_goals 爬賽果（150 分鐘嗰批自然包埋，隔 15 分鐘再跑兜埋）。
2) 所有精選紀錄表（feat_hourly / featured / v1_featured / v3_featured / fw_grid_featured
   入面有 settled+result 欄嘅）對已完場場次補結算。
用法: python _result_settle.py [crawl上限=100]
"""
import os
import sqlite3
import sys
import time

WS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(WS, 'engine'))
sys.path.insert(0, WS)
import json
try:
    import crawler
    from crawl_extra import task_goals, task_odds_page
    CRAWL_OK = True
except ImportError:
    CRAWL_OK = False   # 雲端冇爬蟲模組（datacenter IP 被封）——淨做結算

cfg = json.load(open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8'))
conn = sqlite3.connect(os.path.join(crawler.ROOT_DIR, cfg.get('db_path', 'football.db')),
                       timeout=180)
CAP = int(sys.argv[1]) if len(sys.argv) > 1 else 100

# 1) 精選紀錄表補結算（行先——純 SQL 任何環境都做到）
# 2026-10-22 用戶規格「完場後自動更新到所有工具結果」：舊版淨結算 feat_hourly，
# 其餘表「淨回報數量交返 app 循環」→ featured/v1_featured 滯留 381 場唔結算。
# 而家全部表喺度用同 app lazy-settle 完全一致嘅口徑主動結算（每表獨立
# commit，一表出錯唔拖累其他表）。
TABLES = ('feat_hourly', 'featured', 'featured_z', 'v1_featured',
          'v1_featured_z', 'v3_featured', 'fw_grid_featured', 'fw8s_log',
          'fw8x_log', 'w88_log', 'newjx_log', 'hb_pick_log')


def cols(t):
    try:
        return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % t)]
    except Exception:
        return []


def _pr(hc, gv, hs, aws):
    """上/下/走：A=上盤贏 B=下盤贏 P=走（同 app.pick_result 口徑）。"""
    if hc is None or hs is None or aws is None:
        return None
    if gv == 'home':
        mg = hs - aws - hc
    elif gv == 'away':
        mg = aws - hs - hc
    else:
        mg = hs - aws
    return 'A' if mg > 0 else ('B' if mg < 0 else 'P')


def _settle_dir_family(t):
    """featured 系五張平表＋fw8s_log：direction up/down（fw8s 只有 up）
    對 company12 尾盤計 W/L/P。"""
    C = cols(t)
    if not C or 'match_id' not in C or 'result' not in C:
        return None
    dircol = 'direction' if 'direction' in C else None
    if dircol is None:
        return None
    rows = conn.execute(
        'SELECT x.match_id, x.%s, o.handicap, o.giver, m.home_score, '
        'm.away_score FROM %s x JOIN matches m ON m.id=x.match_id '
        "LEFT JOIN odds_asian o ON o.match_id=x.match_id "
        "AND o.label='closing' AND o.company_id=12 "
        'WHERE x.result IS NULL AND m.home_score IS NOT NULL' % (dircol, t)
    ).fetchall()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    for mid, d, hc, gv, hs, aws in rows:
        if t == 'fw8s_log':
            d = 'up'
        if d not in ('up', 'down'):
            continue
        r = _pr(hc, gv, hs, aws)
        if r is None:
            continue
        res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
        conn.execute('UPDATE %s SET result=?, settled_at=? WHERE match_id=?'
                     % t, (res, now, mid))
        n += 1
    if n:
        conn.commit()
    return len(rows), n


def _settle_fw_grid():
    C = cols('fw_grid_featured')
    if not C:
        return None
    rows = conn.execute(
        'SELECT x.grid, x.match_id, x.direction, o.handicap, o.giver, '
        'm.home_score, m.away_score FROM fw_grid_featured x '
        'JOIN matches m ON m.id=x.match_id '
        "LEFT JOIN odds_asian o ON o.match_id=x.match_id "
        "AND o.label='closing' AND o.company_id=12 "
        'WHERE x.result IS NULL AND m.home_score IS NOT NULL').fetchall()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    for g, mid, d, hc, gv, hs, aws in rows:
        if d not in ('up', 'down'):
            continue
        r = _pr(hc, gv, hs, aws)
        if r is None:
            continue
        res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
        conn.execute('UPDATE fw_grid_featured SET result=?, settled_at=? '
                     'WHERE grid=? AND match_id=?', (res, now, g, mid))
        n += 1
    if n:
        conn.commit()
    return len(rows), n


def _settle_fw8x():
    C = cols('fw8x_log')
    if not C or 'settled' not in C:
        return None
    rows = conn.execute(
        'SELECT f.match_id, f.direction, o.handicap, o.giver, m.home_score, '
        'm.away_score FROM fw8x_log f JOIN matches m ON m.id=f.match_id '
        "LEFT JOIN odds_asian o ON o.match_id=f.match_id "
        "AND o.label='closing' AND o.company_id=12 "
        'WHERE f.settled=0 AND m.home_score IS NOT NULL').fetchall()
    n = 0
    for mid, d, hc, gv, hs, aws in rows:
        r = _pr(hc, gv, hs, aws)
        if r is None:
            continue
        res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
        conn.execute('UPDATE fw8x_log SET result=?, settled=1 WHERE match_id=?',
                     (res, mid))
        n += 1
    if n:
        conn.commit()
    return len(rows), n


def _settle_w88():
    C = cols('w88_log')
    if not C:
        return None
    rows = conn.execute(
        'SELECT k.match_id, k.market, k.hc, k.giver, k.ou_line, '
        'm.home_score, m.away_score FROM w88_log k '
        'JOIN matches m ON m.id=k.match_id '
        'WHERE k.result IS NULL AND m.home_score IS NOT NULL').fetchall()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    for mid, market, hc, giver, ou_line, hs, aws in rows:
        tot = hs + aws
        res = None
        if market in ('ah_up', 'ah_down'):
            if hc is not None:
                mg = (hs - aws - hc if giver == 'home'
                      else (aws - hs - hc if giver == 'away' else hs - aws))
                if abs(mg) < 1e-9:
                    res = 'P'
                else:
                    win = mg > 0
                    if market == 'ah_down':
                        win = not win
                    res = 'W' if win else 'L'
        elif market == 'draw':
            res = 'W' if hs == aws else 'L'
        elif market == 'even':
            res = 'W' if tot % 2 == 0 else 'L'
        elif market in ('over', 'under'):
            if ou_line is not None:
                if abs(tot - ou_line) < 1e-9:
                    res = 'P'
                else:
                    win = tot > ou_line
                    if market == 'under':
                        win = not win
                    res = 'W' if win else 'L'
        elif market == 'home_win':
            res = 'W' if hs > aws else 'L'
        elif market == 'away_win':
            res = 'W' if hs < aws else 'L'
        if res is None:
            continue
        conn.execute('UPDATE w88_log SET result=?, settled_at=? '
                     'WHERE match_id=? AND market=?', (res, now, mid, market))
        n += 1
    if n:
        conn.commit()
    return len(rows), n


def _settle_hb():
    C = cols('hb_pick_log')
    if not C:
        return None
    conn.execute('''CREATE TABLE IF NOT EXISTS hb_pick_log(
        match_id INTEGER PRIMARY KEY, league TEXT, side TEXT, rate REAL,
        line REAL, added_at TEXT, result TEXT, settled_at TEXT)''')
    cur = conn.execute(
        "UPDATE hb_pick_log AS h SET result = CASE "
        "WHEN m.home_score + m.away_score = h.line THEN 'P' "
        "WHEN (m.home_score + m.away_score > h.line) = (h.side = '大') "
        "THEN 'W' ELSE 'L' END, "
        "settled_at = datetime('now','localtime') "
        'FROM matches m '
        'WHERE h.match_id = m.id AND m.home_score IS NOT NULL '
        'AND h.result IS NULL')
    if cur.rowcount:
        conn.commit()
    pend = conn.execute(
        'SELECT COUNT(*) FROM hb_pick_log h JOIN matches m ON m.id=h.match_id '
        'WHERE h.result IS NULL AND m.home_score IS NOT NULL').fetchone()[0]
    return pend, cur.rowcount


def settle_table(t):
    if t == 'fw_grid_featured':
        return _settle_fw_grid()
    if t == 'fw8x_log':
        return _settle_fw8x()
    if t == 'w88_log':
        return _settle_w88()
    if t == 'newjx_log':
        return _settle_dir_family(t)
    if t == 'fw8s_log':
        return _settle_dir_family(t)
    if t == 'hb_pick_log':
        return _settle_hb()
    C = cols(t)
    if not C or 'settled' not in C or 'match_id' not in C:
        return None
    if t != 'feat_hourly':
        # featured 系五張平表（feat_hourly 行舊路徑）
        return _settle_dir_family(t)
    pend = conn.execute(
        'SELECT x.id, x.match_id, x.direction FROM %s x JOIN matches m '
        'ON m.id=x.match_id WHERE x.settled=0 AND m.home_score IS NOT NULL'
        % t).fetchall()
    n = 0
    for rid, mid, direction in pend:
        r = conn.execute(
            "SELECT oc.handicap, oc.giver, m.home_score, m.away_score "
            "FROM matches m LEFT JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 WHERE m.id=?",
            (mid,)).fetchone()
        if not r or r[0] is None or r[2] is None:
            continue
        pr = _pr(r[0], r[1], r[2], r[3])
        if pr is None or direction not in ('up', 'down'):
            continue
        # 2026-10-19 修正：舊版 d=hs+hc-aws 符號倒轉兼冇睇入選方向；
        # 口徑同 app.py pick_result＋以入選方向計 W/L/P
        res = 'P' if pr == 'P' else ('W' if (pr == 'A')
                                     == (direction == 'up') else 'L')
        conn.execute('UPDATE feat_hourly SET settled=1, result=? WHERE id=?',
                     (res, rid))
        n += 1
    if n:
        conn.commit()
    return len(pend), n

print('[settle]', flush=True)
for t in TABLES:
    r = settle_table(t)
    if r:
        print('  %s: 可結算 %d，已結算 %d' % (t, r[0], r[1]), flush=True)

# 2) 賽果回填＋補大小線（本機限定；逾時唔阻塞結算）（逾 120 分鐘）
# 2026-10-22 時區修正：kickoff 存香港時間——舊版漏 '+8 hours'，變相淨係追
# 到開賽 10 個鐘前嘅場次；近 10 個鐘完場嘅場次永遠追唔到。
rows = [] if not CRAWL_OK else conn.execute(
    "SELECT id, kickoff FROM matches WHERE home_score IS NULL "
    "AND kickoff <= datetime('now', '+8 hours', '-120 minutes') "
    "AND kickoff >= datetime('now', '+8 hours', '-14 days') "
    'ORDER BY kickoff DESC LIMIT ?', (CAP,)).fetchall()
print('[crawl] 候補 %d 場（CRAWL_OK=%s）' % (len(rows), CRAWL_OK), flush=True)
fetcher = crawler.Fetcher(conn, cfg)
import datetime as dt
ok = nd = fail = 0
ou_hit = 0
for mid, ko in rows:
    try:
        # 順手補大小尾盤（完場逾120分鐘缺 odds_ou 嘅場次）——治近7日 30% 缺口
        has_ou = conn.execute(
            "SELECT 1 FROM odds_ou WHERE match_id=? AND company_id=12 "
            "AND label='closing'", (mid,)).fetchone()
        if not has_ou:
            if task_odds_page(conn, fetcher, mid,
                              dt.datetime.strptime(ko, '%Y-%m-%d %H:%M'), 'ou') is True:
                ou_hit += 1
        r = task_goals(conn, fetcher, mid, dt.datetime.strptime(ko, '%Y-%m-%d %H:%M'))
        if r is True: ok += 1
        elif r == 'NO_DATA': nd += 1
        else: fail += 1
    except Exception as e:
        fail += 1
        print('[crawl] 場%d EX: %s' % (mid, e), flush=True)
conn.commit()
print('[crawl] 成=%d 無數據=%d 敗=%d 補大小線=%d' % (ok, nd, fail, ou_hit), flush=True)

# 2b) 2026-10-22：聯賽賽季檔補「真賽果」——task_goals 淨補 match_goals/
# 角球，matches.home_score 要經 save_season 先寫到（同 app 賽果巡邏同通道）。
# 限 10 個聯賽、30 秒，best-effort。
if CRAWL_OK and rows:
    try:
        tids = [r[0] for r in conn.execute(
            'SELECT DISTINCT s.titan_id FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff <= datetime('now','+8 hours','-120 minutes') "
            "AND m.kickoff >= datetime('now','+8 hours','-14 days') "
            'LIMIT 10')]
        _t0 = time.time()
        _lg = 0
        for tid in tids:
            if time.time() - _t0 > 30:
                break
            try:
                seasons = crawler.get_season_list(fetcher, tid)
                if not seasons:
                    continue
                prefix = crawler.resolve_prefix(fetcher, tid)
                if not prefix:
                    continue
                url = ('https://zq.titan007.com/jsData/matchResult/'
                       '%s/%s.js?version=1' % (seasons[0], prefix))
                text = fetcher.get(url)
                if not text or text.lstrip().startswith('<'):
                    continue
                parsed = crawler.parse_season_js(text)
                crawler.save_season(conn, tid, seasons[0], 1, parsed)
                _lg += 1
            except Exception:
                pass
        if _lg:
            conn.commit()
        print('[crawl] 賽季檔補賽果：%d 個聯賽' % _lg, flush=True)
    except Exception:
        pass

print('done %.0fs' % time.time() and 'done')
