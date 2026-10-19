# -*- coding: utf-8 -*-
"""88W 2026 全年回查（2026-10-20）：將 2026 年所有「合資格」已完場賽事
（聯賽有方法庫＋條件全中，口徑同 w88_engine.scan 完全一致）補入 w88_log，
並即時結算——精選8s 頁面「過往紀錄」會顯示成個 2026 實戰成績。

用法（本機，對住 master DB）：
    python w88_backfill.py            # 實際寫入
    python w88_backfill.py --dry      # 淺計數唔寫入

冧把：INSERT OR IGNORE（PK match_id+market+conds），重複跑安全；
added_at 標 '2026回查' 方便同實戰預測分開。
"""
import json
import os
import sqlite3
import sys
import time

import fw8x
import w88_engine as W

WS = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(WS)
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'football.db')
YEAR = 2026
BATCH = 100           # 每幾場 commit 一次
PS_CHUNK = 50         # 每幾場釋放/重攞連接，避免長讀快照阻 checkpoint


def collect_rules():
    rules = {}
    for src in (W.load_rules(force=True), W.load_draw_rules(force=True)):
        for lg, rl in src.items():
            rules.setdefault(lg, []).extend(rl)
    return rules


def run(conn, year=YEAR, write=True, progress_every=200):
    W.migrate(conn)
    rules = collect_rules()
    if not rules:
        return {'error': 'rules empty'}
    leagues = sorted(rules.keys())
    t0 = time.time()
    rows = conn.execute(
        'SELECT m.id, c.req_name, m.home_score, m.away_score '
        'FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'WHERE m.kickoff >= ? AND m.kickoff < ? '
        'AND m.home_score IS NOT NULL '
        'AND c.req_name IN (%s)' % ','.join('?' * len(leagues)),
        ['%d-01-01' % year, '%d-01-01' % (year + 1)] + leagues).fetchall()
    total = len(rows)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    stats = {'scanned': 0, 'no_odds': 0, 'eligible_matches': 0,
             'added': 0, 'w': 0, 'l': 0, 'p': 0,
             'by_market': {}, 'exist': 0}
    todo_batch = 0
    for i, (mid, lg, hs, aws) in enumerate(rows):
        stats['scanned'] += 1
        got = W._odds_feats(conn, mid)
        if not got:
            stats['no_odds'] += 1
        else:
            of, ah, ou = got
            psf = None
            hit_any = False
            for rule in rules.get(lg, []):
                conds = rule['conds'] or []
                if not all(of.get(c) for c in conds
                           if c not in W.PS_CONDS):
                    continue
                if any(c in W.PS_CONDS for c in conds):
                    if psf is None:
                        info = fw8x.features_for(conn, mid, fill_live=True)
                        psf = W._ps_feats(info['feat']) if info else {}
                    if not all(psf.get(c) for c in conds
                               if c in W.PS_CONDS):
                        continue
                res = W._settle_one(rule['market'], hs, aws,
                                    ah.get('handicap'), ah.get('giver'),
                                    ou['total_line'] if ou else None)
                if res is None:
                    continue
                hit_any = True
                if not write:
                    stats['added'] += 1
                    stats[res.lower() if res != 'P' else 'p'] += 1
                    continue
                cur = conn.execute(
                    'INSERT OR IGNORE INTO w88_log '
                    '(match_id, market, conds, league, label, ou_line, '
                    ' hc, giver, added_at, result, settled_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (mid, rule['market'],
                     json.dumps(conds, ensure_ascii=False), lg,
                     rule.get('label'),
                     ou['total_line'] if ou else None,
                     ah.get('handicap'), ah.get('giver'),
                     '%d回查' % year, res, now))
                if cur.rowcount:
                    stats['added'] += 1
                    stats[{'W': 'w', 'L': 'l', 'P': 'p'}[res]] += 1
                    bm = stats['by_market'].setdefault(
                        rule['market'], {'n': 0, 'w': 0})
                    bm['n'] += 1
                    bm['w'] += (res == 'W')
                else:
                    stats['exist'] += 1
            if hit_any:
                stats['eligible_matches'] += 1
        todo_batch += 1
        if write and todo_batch >= BATCH:
            conn.commit()
            todo_batch = 0
        if (i + 1) % progress_every == 0:
            print('  %d/%d added=%d (%.0fs)' % (i + 1, total,
                  stats['added'], time.time() - t0), flush=True)
    if write:
        conn.commit()
    stats['secs'] = round(time.time() - t0, 1)
    return stats


if __name__ == '__main__':
    dry = '--dry' in sys.argv
    conn = sqlite3.connect(DB_PATH, timeout=180)
    try:
        rep = run(conn, write=not dry)
    finally:
        conn.close()
    print(('DRY-RUN ' if dry else '') + json.dumps(
        rep, ensure_ascii=False, indent=1))
