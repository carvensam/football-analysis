# -*- coding: utf-8 -*-
"""V1/V2 「I–P 優化」歷史回測（2026-10-24 用戶批准）：
同場比較舊制（方向=A→P 第一個合格字頭）vs 新制（同主隊 I→P 優先）。
對窗口內全部已完場+有 company12 尾盤讓球嘅場次，用 v2_engine.featured_letters
評一次（letters/pass/mix 齊），兩種揀法各自結算對比：
  old  ｜new（整體）｜new 且 I–P 主導｜new 且 A–H 主導
用法: python v2_backtest.py [--from 2026-09-01] [--to 2026-10-24 02:30]
      [--out v2_bt.json] [--limit N]
"""
import json
import os
import sqlite3
import sys
import time

WS = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(WS)
sys.path.insert(0, WS)
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'football.db')

import screen_engine
import v2_engine
import app


def settle(direction, hc, giver, hs, aws):
    if direction is None or hc is None:
        return None
    r = app.pick_result(hc, giver, hs, aws)
    if r is None:
        return None
    return 'P' if r == 'P' else ('W' if (r == 'A') == (direction == 'up')
                                 else 'L')


def main():
    frm = '2026-09-01'
    to = '2026-10-24 02:30'
    out = os.path.join(WS, 'v2_bt.json')
    limit = 0
    for i, a in enumerate(sys.argv[1:]):
        if a == '--from':
            frm = sys.argv[i + 2]
        if a == '--to':
            to = sys.argv[i + 2]
        if a == '--out':
            out = sys.argv[i + 2]
        if a == '--limit':
            limit = int(sys.argv[i + 2])
    conn = sqlite3.connect(DB_PATH, timeout=180)
    rows = conn.execute(
        'SELECT m.id, m.home_score, m.away_score FROM matches m '
        "JOIN odds_asian o ON o.match_id=m.id AND o.label='closing' "
        'AND o.company_id=12 AND o.handicap IS NOT NULL '
        'WHERE m.home_score IS NOT NULL AND m.kickoff BETWEEN ? AND ? '
        'ORDER BY m.kickoff', (frm, to)).fetchall()
    if limit:
        rows = rows[:limit]
    total = len(rows)
    print('[v2-bt] 窗口 %s → %s：%d 場' % (frm, to, total), flush=True)
    screen_engine.load_pool(conn)
    v2_engine.load_v2_pool(conn)
    # 2026-10-24 用戶 D 方案：對賽主客對換＋盤差（升盤≥+0.25 / 降盤≤-0.25）
    from bisect import bisect_right
    def _sgn(hc, gv, fh=True):
        if hc is None:
            return None
        if gv == ('home' if fh else 'away'):
            return hc
        if gv == ('away' if fh else 'home'):
            return -hc
        return 0.0
    _allm = conn.execute(
        'SELECT id, kickoff, home_id, away_id FROM matches '
        'WHERE home_score IS NOT NULL').fetchall()
    _pair = {}
    for _mid, _ko, _h, _a in _allm:
        _pair.setdefault((_h, _a), []).append((_ko, _mid))
    for _k in _pair:
        _pair[_k].sort()
    _odds = {}
    for _mid, _hc, _gv in conn.execute(
            "SELECT match_id, handicap, giver FROM odds_asian "
            "WHERE label='closing' AND company_id=12 AND handicap IS NOT NULL"):
        _odds[_mid] = (_hc, _gv)
    _ko_of = {}
    for _mid, _ko, _h, _a in _allm:
        _ko_of[_mid] = (_ko, _h, _a)

    def _delta(mid):
        if mid not in _odds or mid not in _ko_of:
            return None
        ko, h, a = _ko_of[mid]
        hc, gv = _odds[mid]
        lst = _pair.get((a, h))
        if not lst:
            return None
        i = bisect_right(lst, (ko, 1 << 62)) - 1
        if i < 0:
            return None
        _, pmid = lst[i]
        if pmid not in _odds:
            return None
        phc, pgv = _odds[pmid]
        d = _sgn(hc, gv, True)
        p = _sgn(phc, pgv, False)
        if d is None or p is None:
            return None
        return d - p
    agg = {k: {'n': 0, 'w': 0, 'l': 0, 'p': 0}
           for k in ('old', 'new', 'new_ip', 'new_ah',
                     'new_rise', 'new_fall', 'old_rise', 'old_fall',
                     'new_ip_rise', 'new_ip_fall')}
    t0 = time.time()
    done = 0
    for mid, hs, aws in rows:
        try:
            t = screen_engine.get_target(conn, mid)
            if not t:
                continue
            fl = v2_engine.featured_letters(conn, t)
            if not fl:
                continue
            passed = [e for e in fl['letters'] if e.get('pass')]
            if not passed:
                continue
            oc = conn.execute(
                "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
                "AND label='closing' AND company_id=12", (mid,)).fetchone()
            hc, gv = oc if oc else (None, None)
            old_dir = passed[0]['dir']
            ip = [e for e in passed if e.get('mix') == '同主隊']
            drv = ip[0] if ip else passed[0]
            new_dir = drv['dir']
            buckets = [('old', old_dir)]
            buckets.append(('new', new_dir))
            buckets.append(('new_ip' if ip else 'new_ah', new_dir))
            _d = _delta(mid)
            if _d is not None:
                _dg = 'rise' if _d >= 0.25 else ('fall' if _d <= -0.25
                                                 else None)
                if _dg:
                    buckets.append(('new_' + _dg, new_dir))
                    buckets.append(('old_' + _dg, old_dir))
                    if ip:
                        buckets.append(('new_ip_' + _dg, new_dir))
            for k, d in buckets:
                r = settle(d, hc, gv, hs, aws)
                if r is None:
                    continue
                agg[k]['n'] += 1
                agg[k][r.lower()] += 1
        except Exception:
            pass
        done += 1
        if done % 400 == 0:
            el = time.time() - t0
            print('[v2-bt] %d/%d（%.0f%%，%.0f 場/分鐘）' % (
                done, total, done / total * 100, done / (el / 60)),
                flush=True)
            _dump(out, agg, done, total, frm, to)
    _dump(out, agg, done, total, frm, to, final=True)


def _dump(path, agg, done, total, frm, to, final=False):
    def fin(a):
        d = a['w'] + a['l']
        return round(a['w'] / d * 100, 1) if d else None
    out = {'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
           'window': [frm, to], 'matches': {'done': done, 'total': total},
           'final': final,
           'note': 'old=A→P 第一個合格字頭；new=同主隊(I–P)優先；'
                   'new_ip/new_ah=新制下邊類字頭主導',
           'results': {k: dict(v, hit=fin(v)) for k, v in agg.items()}}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)


if __name__ == '__main__':
    main()
