# -*- coding: utf-8 -*-
"""發掘分析（2026-10-24 用戶指令，純分析唔改嘢）：
『對賽主客場對換＋上賽盤口 vs 今賽盤口差額』嘅預測價值。
對每場（窗口內已完場＋有 company12 尾盤讓球）：
  1) 搵兩隊最近一次『主客對換』對賽（今場主隊 = 上場客隊＆ vice versa，
     有比分＋有尾盤讓球）——攞上賽盤口換算做『今主隊視角』讓球
  2) 今賽盤口同樣換算今主隊視角 → 盤差 delta = 今 − 上（正＝今主隊讓得深咗）
  3) 結算今場上下盤 → 統計每個 delta 檔嘅 上/下/走 率
用法: python mine_swap_delta.py [--from 2025-01-01] [--to 2026-10-23]
"""
import json
import os
import sqlite3
import sys
import time
from bisect import bisect_right

WS = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(WS)
DB = os.environ.get('DB_PATH') or os.path.join(BASE, 'football.db')

FRM = '2025-01-01'
TO = '2026-10-23 23:59:59'


def _signed(hc, giver, from_home=True):
    """讓球換算帶符號：正＝(from_home?主隊:客隊) 讓球。hc=None→None"""
    if hc is None:
        return None
    if giver == ('home' if from_home else 'away'):
        return hc
    if giver == ('away' if from_home else 'home'):
        return -hc
    return 0.0


def main():
    global FRM, TO
    out = os.path.join(WS, '_mine_swap_delta.json')
    for i, a in enumerate(sys.argv[1:]):
        if a == '--from':
            FRM = sys.argv[i + 2]
        if a == '--to':
            TO = sys.argv[i + 2]
        if a == '--out':
            out = sys.argv[i + 2]
    conn = sqlite3.connect('file:%s?mode=ro' % DB, uri=True)
    c = conn.cursor()
    t0 = time.time()
    # 全部場次（搵對換對賽用）
    allm = c.execute(
        'SELECT id, kickoff, home_id, away_id, home_score, away_score '
        'FROM matches WHERE home_score IS NOT NULL').fetchall()
    by_pair = {}
    for mid, ko, h, a, hs, aws in allm:
        by_pair.setdefault((h, a), []).append((ko, mid))
    for k in by_pair:
        by_pair[k].sort()
    # 尾盤讓球（company12）
    odds = {}
    for mid, hc, gv in c.execute(
            "SELECT match_id, handicap, giver FROM odds_asian "
            "WHERE label='closing' AND company_id=12 AND handicap IS NOT NULL"):
        odds[mid] = (hc, gv)
    # 窗口場次
    rows = c.execute(
        'SELECT id, kickoff, home_id, away_id, home_score, away_score '
        'FROM matches WHERE home_score IS NOT NULL AND kickoff BETWEEN ? AND ?',
        (FRM, TO)).fetchall()
    total = len(rows)
    print('[mine] 窗口 %s → %s：%d 場（字典就緒 %.0fs）'
          % (FRM, TO, total, time.time() - t0), flush=True)
    buckets = {}
    n_swap = n_all = 0
    down_w = up_w = 0
    done = 0
    for mid, ko, h, a, hs, aws in rows:
        done += 1
        if mid not in odds:
            continue
        n_all += 1
        hc, gv = odds[mid]
        # 最近一次主客對換對賽
        lst = by_pair.get((a, h))
        if not lst:
            continue
        i = bisect_right(lst, (ko, 1 << 62)) - 1
        if i < 0:
            continue
        pko, pmid = lst[i]
        if pmid not in odds:
            continue
        phc, pgv = odds[pmid]
        # 上賽『今主隊視角』（今主隊上場係客隊：佢讓=正）
        prev_team = _signed(phc, pgv, from_home=False)
        now_team = _signed(hc, gv, from_home=True)
        if prev_team is None or now_team is None:
            continue
        delta = round((now_team - prev_team) * 4) / 4  # 0.25 檔
        n_swap += 1
        # 結算（今主隊視角）：margin>0＝上盤(讓球方=主)贏
        if gv == 'home':
            mg = hs - aws - hc
        elif gv == 'away':
            mg = aws - hs - hc
        else:
            mg = hs - aws
        res = 'p' if mg == 0 else ('up' if mg > 0 else 'down')
        if res == 'up':
            up_w += 1
        elif res == 'down':
            down_w += 1
        b = buckets.setdefault(delta, {'n': 0, 'up': 0, 'down': 0, 'p': 0})
        b['n'] += 1
        b[res] += 1
        if done % 5000 == 0:
            print('[mine] %d/%d（有對換對賽 %d）' % (done, total, n_swap),
                  flush=True)
    print('[mine] 完成 %.0fs：窗口 %d 場，有盤 %d，搵到對換對賽 %d（%.0f%%）'
          % (time.time() - t0, total, n_all, n_swap,
             n_swap / max(1, n_all) * 100), flush=True)
    # 輸出
    out_rows = []
    for d in sorted(buckets):
        b = buckets[d]
        dec = b['up'] + b['down']
        out_rows.append({
            'delta': d, 'n': b['n'], 'up': b['up'], 'down': b['down'],
            'p': b['p'],
            'up_r': round(b['up'] / dec * 100, 1) if dec else None,
            'down_r': round(b['down'] / dec * 100, 1) if dec else None})
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'window': [FRM, TO], 'matches': total,
                   'with_odds': n_all, 'with_swap': n_swap,
                   'buckets': out_rows}, f, ensure_ascii=False)
    print('%-8s %-6s %-8s %-8s %-8s' % ('盤差', '場數', '上盤率', '下盤率', '走'))
    for r in out_rows:
        print('%-8s %-6d %-8s %-8s %-8d' % (
            '%+.2f' % r['delta'], r['n'],
            str(r['up_r']) + '%' if r['up_r'] is not None else '—',
            str(r['down_r']) + '%' if r['down_r'] is not None else '—', r['p']))


if __name__ == '__main__':
    main()
