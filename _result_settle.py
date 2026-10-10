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
TABLES = ('feat_hourly', 'featured', 'v1_featured', 'v3_featured', 'fw_grid_featured')
def cols(t):
    try:
        return [r[1] for r in conn.execute('PRAGMA table_info(%s)' % t)]
    except Exception:
        return []
def settle_table(t):
    C = cols(t)
    if not C or 'settled' not in C or 'match_id' not in C:
        return None
    pend = conn.execute(
        'SELECT x.id, x.match_id FROM %s x JOIN matches m ON m.id=x.match_id '
        'WHERE x.settled=0 AND m.home_score IS NOT NULL' % t).fetchall()
    if t != 'feat_hourly':
        # 其他表結算語義各異（唔好用主客 W/L 亂寫）——淨回報數量，交返 app 循環
        return len(pend), 0
    n = 0
    for rid, mid in pend:
        r = conn.execute(
            "SELECT oc.handicap, oc.giver, m.home_score, m.away_score "
            "FROM matches m LEFT JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 WHERE m.id=?",
            (mid,)).fetchone()
        if not r or r[0] is None or r[2] is None:
            continue
        hc, gv, hs, aws = r
        if gv == 'home':
            d = hs + hc - aws
        elif gv == 'away':
            d = aws + hc - hs
        else:
            continue
        res = 'W' if d > 0 else ('L' if d < 0 else 'P')
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
rows = [] if not CRAWL_OK else conn.execute(
    "SELECT id, kickoff FROM matches WHERE home_score IS NULL "
    "AND kickoff <= datetime('now', '-120 minutes') "
    "AND kickoff >= datetime('now', '-14 days') "
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

print('done %.0fs' % time.time() and 'done')
