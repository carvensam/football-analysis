# -*- coding: utf-8 -*-
"""新精選 2026 全年回查（2026-10-20）：將 2026 年所有「合資格」已完場賽事
（聯賽有方法＋條件全中，口徑同 newjx_engine.scan 完全一致）補入 newjx_log，
並即時結算——新精選頁面會顯示成個 2026 實戰成績。

用法（本機，對住 master DB）：
    python newjx_backfill.py            # 實際寫入
    python newjx_backfill.py --dry      # 淺計數唔寫入

冧把：INSERT OR IGNORE（PK match_id），重複跑安全；
added_at 標 '2026回查' 方便同實戰預測分開。
"""
import json
import os
import sqlite3
import sys
import time

import fw8x
import newjx_engine as X

WS = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(WS)
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'football.db')
YEAR = 2026
BATCH = 100


def _settle(dir_, hc, giver, hs, aws):
    """同 newjx_engine.settle 一致：up＝上盤贏 W；down＝下盤贏 W；走盤 P。"""
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
    win = mg > 0 if dir_ == 'up' else mg < 0
    return 'W' if win else 'L'


def run(conn, year=YEAR, write=True, progress_every=500):
    X.migrate(conn)
    rules = X.load_rules(force=True)
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
    stats = {'scanned': 0, 'no_odds': 0, 'eligible': 0,
             'added': 0, 'w': 0, 'l': 0, 'p': 0, 'exist': 0,
             'by_dir': {'up': {'n': 0, 'w': 0}, 'down': {'n': 0, 'w': 0}}}
    todo_batch = 0
    for i, (mid, lg, hs, aws) in enumerate(rows):
        stats['scanned'] += 1
        rule = rules.get(lg)
        got = X._odds_feats(conn, mid) if rule else None
        if not got:
            stats['no_odds'] += 1
        else:
            of, ah = got
            conds = rule['conds'] or []
            ok = all(of.get(c) for c in conds if c not in X.PS_CONDS)
            if ok and any(c in X.PS_CONDS for c in conds):
                info = fw8x.features_for(conn, mid, fill_live=True)
                psf = X._ps_feats(info['feat']) if info else {}
                ok = all(psf.get(c) for c in conds if c in X.PS_CONDS)
            if ok:
                stats['eligible'] += 1
                res = _settle(rule['dir'], ah.get('handicap'),
                              ah.get('giver'), hs, aws)
                bd = stats['by_dir'][rule['dir']]
                bd['n'] += 1
                if res == 'W':
                    bd['w'] += 1
                if write and res is not None:
                    cur = conn.execute(
                        'INSERT OR IGNORE INTO newjx_log VALUES(?,?,?,?,?,?,?,?,?)',
                        (mid, lg, rule['dir'],
                         json.dumps(conds, ensure_ascii=False),
                         ah.get('handicap'), ah.get('giver'),
                         '%d回查' % year, res, now))
                    if cur.rowcount:
                        stats['added'] += 1
                        stats[{'W': 'w', 'L': 'l', 'P': 'p'}[res]] += 1
                    else:
                        stats['exist'] += 1
                elif res is not None:
                    stats['added'] += 1
                    stats[{'W': 'w', 'L': 'l', 'P': 'p'}[res]] += 1
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
