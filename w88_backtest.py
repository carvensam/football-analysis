# -*- coding: utf-8 -*-
"""88W 全市場全庫回測（2026-10-22 用戶指示：Debug K3 方向 bug 後，
用新篩選回跑歷史資料庫所有資料，更新 88W 八個標準命中率）。

範圍：規則聯賽（w88_rules ∪ k3_draw_rules）全部已完場＋有 company12
closing 讓球盤嘅場次。逐場行同正式掃描完全一致嘅評估（w88_engine 內部
函數，包括 2026-10-22 嘅 1X2 方向守衛），按市場結算，聚合：
  markets: 8 個市場各自 n/W/L/P/命中率
  rules:   每條方法（聯賽＋市場＋條件）嘅全庫實證命中率
用法: python w88_backtest.py [--out w88_bt.json] [--limit N]
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

import w88_engine
from w88_engine import (_odds_feats, _ps_feats, _settle_one, PS_CONDS,
                        load_rules, load_draw_rules)


def merged_rules():
    """w88_rules + k3_draw_rules 合一（同 88W 頁面顯示口徑一致）。"""
    out = {}
    for src in (load_rules(), load_draw_rules()):
        for lg, rs in src.items():
            out.setdefault(lg, []).extend(rs)
    return out


def eval_merged(conn, mid, lg, rules_by_lg):
    """同 w88_engine.eval_match 同一套邏輯（連方向守衛），但用合一規則庫。
    回傳 hits: [(market, conds_key, hc, giver, ou_line), ...]"""
    got = _odds_feats(conn, mid)
    if not got:
        return []
    of, ah, ou = got
    psf = None
    hits = []
    for rule in rules_by_lg.get(lg, []):
        conds = rule['conds']
        if set(conds) & {'hc_deep', 'hc025', 'hc05', 'hc075'}:
            if rule['market'] == 'home_win' and 'gv_away' not in conds \
                    and not of.get('gv_home'):
                continue
            if rule['market'] == 'away_win' and 'gv_home' not in conds \
                    and not of.get('gv_away'):
                continue
        if not all(of.get(c) for c in conds if c not in PS_CONDS):
            continue
        if any(c in PS_CONDS for c in conds):
            if psf is None:
                import fw8x
                # fill_live=True：同正式掃描完全同一口徑——舊場次缺
                # match_prestandings 就即場由排名/近績計（慢啲但忠實）
                info = fw8x.features_for(conn, mid, fill_live=True)
                psf = _ps_feats(info['feat']) if info else {}
            if not all(psf.get(c) for c in conds if c in PS_CONDS):
                continue
        hits.append((rule['market'],
                     json.dumps(conds, ensure_ascii=False),
                     ah.get('handicap'), ah.get('giver'),
                     ou['total_line'] if ou else None,
                     rule.get('label')))
    return hits


def main():
    out_path = os.path.join(WS, 'w88_bt.json')
    limit = 0
    for i, a in enumerate(sys.argv[1:]):
        if a == '--out' and i + 2 <= len(sys.argv):
            out_path = sys.argv[i + 2]
        if a == '--limit' and i + 2 <= len(sys.argv):
            limit = int(sys.argv[i + 2])
    rules_by_lg = merged_rules()
    leagues = tuple(rules_by_lg.keys())
    ph = ','.join('?' * len(leagues))
    conn = sqlite3.connect(DB_PATH, timeout=180)
    rows = conn.execute(
        'SELECT m.id, c.req_name, m.home_score, m.away_score, m.kickoff '
        'FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
        'AND oc.company_id=12 AND oc.handicap IS NOT NULL '
        'WHERE m.home_score IS NOT NULL AND c.req_name IN (%s) '
        'ORDER BY m.kickoff' % ph, leagues).fetchall()
    if limit:
        rows = rows[:limit]
    total = len(rows)
    print('[w88-bt] 回測場次：%d（%d 個規則聯賽）' % (total, len(leagues)),
          flush=True)
    mkt_stats = {}
    mkt_recent = {}
    rule_stats = {}
    t0 = time.time()
    done = 0
    for mid, lg, hs, aws, ko in rows:
        recent = ko >= '2025-01-01'
        try:
            hits = eval_merged(conn, mid, lg, rules_by_lg)
        except Exception:
            continue
        for market, ck, hc, giver, ou_line, label in hits:
            res = _settle_one(market, hs, aws, hc, giver, ou_line)
            if res is None:
                continue
            ms = mkt_stats.setdefault(market, {'n': 0, 'W': 0, 'L': 0,
                                               'P': 0})
            ms['n'] += 1
            ms[res] += 1
            if recent:
                mr = mkt_recent.setdefault(
                    market, {'n': 0, 'W': 0, 'L': 0, 'P': 0})
                mr['n'] += 1
                mr[res] += 1
            rk = (lg, market, ck)
            rs = rule_stats.setdefault(rk, {'n': 0, 'W': 0, 'L': 0, 'P': 0,
                                            'label': label})
            rs['n'] += 1
            rs[res] += 1
        done += 1
        if done % 3000 == 0:
            el = time.time() - t0
            print('[w88-bt] %d/%d（%.0f%%，%.0f 場/分鐘）' % (
                done, total, done / total * 100, done / (el / 60)),
                flush=True)
            # 中途存檔（中斷都有 partial 結果）
            _dump(out_path, mkt_stats, mkt_recent, rule_stats, done, total,
                  partial=True)
    _dump(out_path, mkt_stats, mkt_recent, rule_stats, done, total,
          partial=False)
    print('[w88-bt] 完成 %.0fs → %s' % (time.time() - t0, out_path),
          flush=True)


def _dump(path, mkt_stats, mkt_recent, rule_stats, done, total, partial):
    def fin(s):
        d = s['W'] + s['L']
        return round(s['W'] / d * 100, 1) if d else None
    out = {
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'partial': partial,
        'matches': {'done': done, 'total': total},
        'note': ('2026-10-22 全庫回測（含 1X2 方向守衛修正）。'
                 '命中率＝W÷(W+L)，走盤 P 唔計入。'
                 'markets_recent＝2025 起（近似樣本外）。'),
        'markets': {m: dict(v, hit=fin(v)) for m, v in
                    sorted(mkt_stats.items(),
                           key=lambda kv: -kv[1]['n'])},
        'markets_recent': {m: dict(v, hit=fin(v)) for m, v in
                           sorted(mkt_recent.items(),
                                  key=lambda kv: -kv[1]['n'])},
        'rules': [
            dict(league=lg, market=mkt, conds=json.loads(ck), label=v['label'],
                 n=v['n'], W=v['W'], L=v['L'], P=v['P'], hit=fin(v))
            for (lg, mkt, ck), v in
            sorted(rule_stats.items(), key=lambda kv: -kv[1]['n'])],
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)


if __name__ == '__main__':
    main()
