# -*- coding: utf-8 -*-
"""新精選歷史回查：按方法條件重評已完場賽事，並以落注盤重結算。
預設處理 2026 年；--full 處理全庫，--dry 只計數不寫入。
舊記錄保留原 added_at；盤口、方向或賽果有變時重結算，不把舊列當已完成而略過。
歷史回查不套用只供即時出場用的近期勝率閘。
"""
import json
import os
import sqlite3
import sys
import time

try:
    from . import fw8x
    from . import newjx_engine as X
except ImportError:
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
    bounds = ''
    args = []
    if year is not None:
        bounds = 'AND m.kickoff >= ? AND m.kickoff < ? '
        args = ['%d-01-01' % year, '%d-01-01' % (year + 1)]
    rows = conn.execute(
        'SELECT m.id, c.req_name, m.home_score, m.away_score '
        'FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'WHERE m.home_score IS NOT NULL ' + bounds +
        'AND c.req_name IN (%s)' % ','.join('?' * len(leagues)),
        args + leagues).fetchall()
    total = len(rows)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    tag = '全庫回查' if year is None else '%d回查' % year
    stats = {'scope': tag, 'scanned': 0, 'no_odds': 0, 'eligible': 0,
             'added': 0, 'updated': 0, 'w': 0, 'l': 0, 'p': 0, 'exist': 0,
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
                res = _settle(rule['dir'], ah.get('handicap'),
                              ah.get('giver'), hs, aws)
                if res is not None:
                    stats['eligible'] += 1
                    stats[res.lower()] += 1
                    bd = stats['by_dir'][rule['dir']]
                    bd['n'] += 1
                    bd['w'] += (res == 'W')
                    conds_json = json.dumps(conds, ensure_ascii=False)
                    old = conn.execute(
                        'SELECT dir,conds,hc,giver,result FROM newjx_log '
                        'WHERE match_id=?', (mid,)).fetchone()
                    vals = (rule['dir'], conds_json,
                            ah.get('handicap'), ah.get('giver'), res)
                    if old is None:
                        stats['added'] += 1
                        if write:
                            conn.execute(
                                'INSERT INTO newjx_log VALUES(?,?,?,?,?,?,?,?,?)',
                                (mid, lg) + vals[:4] + (tag, res, now))
                    elif tuple(old) != vals:
                        stats['updated'] += 1
                        if write:
                            conn.execute(
                                'UPDATE newjx_log SET dir=?,conds=?,hc=?,giver=?,'
                                'result=?,settled_at=? WHERE match_id=?',
                                vals + (now, mid))
                    else:
                        stats['exist'] += 1
        todo_batch += 1
        if write and todo_batch >= BATCH:
            conn.commit()
            todo_batch = 0
        if (i + 1) % progress_every == 0:
            print('  %d/%d added=%d updated=%d (%.0fs)' % (
                i + 1, total, stats['added'], stats['updated'],
                time.time() - t0), flush=True)
    if write:
        conn.commit()
    stats['secs'] = round(time.time() - t0, 1)
    return stats


if __name__ == '__main__':
    dry = '--dry' in sys.argv
    conn = sqlite3.connect(DB_PATH, timeout=180)
    try:
        rep = run(conn, year=None if '--full' in sys.argv else YEAR,
                  write=not dry)
    finally:
        conn.close()
    print(('DRY-RUN ' if dry else '') + json.dumps(
        rep, ensure_ascii=False, indent=1))
