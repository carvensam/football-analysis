# -*- coding: utf-8 -*-
"""88W 歷史回查：按方法條件重評已完場賽事，包含方向守衛及對立裁決。
預設處理 2026 年；--full 處理全庫，--dry 只計數不寫入。
舊記錄保留原 added_at；盤口或賽果有變時重結算，不把舊列當已完成而略過。
"""
import json
import os
import sqlite3
import sys
import time

try:
    from . import fw8x
    from . import w88_engine as W
except ImportError:
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
    stats = {'scope': tag, 'scanned': 0, 'no_odds': 0, 'eligible_matches': 0,
             'added': 0, 'updated': 0, 'w': 0, 'l': 0, 'p': 0,
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
            tagged = []
            for rule in rules.get(lg, []):
                conds = rule['conds'] or []
                if set(conds) & {'hc_deep', 'hc025', 'hc05', 'hc075'}:
                    if rule['market'] == 'home_win' and 'gv_away' not in conds \
                            and not of.get('gv_home'):
                        continue
                    if rule['market'] == 'away_win' and 'gv_home' not in conds \
                            and not of.get('gv_away'):
                        continue
                if not all(of.get(c) for c in conds if c not in W.PS_CONDS):
                    continue
                if any(c in W.PS_CONDS for c in conds):
                    if psf is None:
                        info = fw8x.features_for(conn, mid, fill_live=True)
                        psf = W._ps_feats(info['feat']) if info else {}
                    if not all(psf.get(c) for c in conds if c in W.PS_CONDS):
                        continue
                conds_json = json.dumps(conds, ensure_ascii=False)
                key = (mid, rule['market'], conds_json)
                vals = (mid, rule['market'], conds_json, lg, rule.get('label'),
                        ou['total_line'] if ou else None,
                        ah.get('handicap'), ah.get('giver'), tag)
                tagged.append((key, vals, rule))
            hits = W._arbitrate(tagged)
            if hits:
                stats['eligible_matches'] += 1
            for key, vals in hits:
                market, conds_json = key[1], key[2]
                res = W._settle_one(market, hs, aws, vals[6], vals[7], vals[5])
                if res is None:
                    continue
                stats[res.lower()] += 1
                bm = stats['by_market'].setdefault(market, {'n': 0, 'w': 0})
                bm['n'] += 1
                bm['w'] += (res == 'W')
                old = conn.execute(
                    'SELECT ou_line,hc,giver,result FROM w88_log '
                    'WHERE match_id=? AND market=? AND conds=?',
                    (mid, market, conds_json)).fetchone()
                changed = (vals[5], vals[6], vals[7], res)
                if old is None:
                    stats['added'] += 1
                    if write:
                        conn.execute(
                            'INSERT INTO w88_log '
                            '(match_id,market,conds,league,label,ou_line,hc,giver,'
                            'added_at,result,settled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            vals + (res, now))
                elif tuple(old) != changed:
                    stats['updated'] += 1
                    if write:
                        conn.execute(
                            'UPDATE w88_log SET ou_line=?,hc=?,giver=?,result=?,'
                            'settled_at=? WHERE match_id=? AND market=? AND conds=?',
                            changed + (now, mid, market, conds_json))
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
