# -*- coding: utf-8 -*-
"""好波大細規則礦工（2026-10-22 用戶規格）：逐聯賽獨立挖掘 ≥82.5% 篩選。
用法: python hb_mine.py [--min-lg 150] [--out haobao_ou_rules.json]
規格：訓練期 ≤2024 ＋ 驗證期 2025+ 兩邊命中率 ≥82.5%（走盤唔計），
訓練 n≥10／驗證 n≥5。特徵構建同運行時完全共用 hb_ou_rules.feats。
"""
import itertools
import json
import os
import sqlite3
import sys
import time

import numpy as np

WS = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(WS)
sys.path.insert(0, WS)
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'football.db')

import haobao_engine as hb
import hb_ou_rules as R

TRAIN_MAX = R.TRAIN_MAX
THRESH = R.THRESH


def main():
    out_path = os.path.join(WS, 'haobao_ou_rules.json')
    min_lg = 150
    for i, a in enumerate(sys.argv[1:]):
        if a == '--out' and i + 2 <= len(sys.argv):
            out_path = sys.argv[i + 2]
        if a == '--min-lg' and i + 2 <= len(sys.argv):
            min_lg = int(sys.argv[i + 2])
    conn = sqlite3.connect(DB_PATH, timeout=180)
    t0 = time.time()
    df = hb.load_pool(conn)
    print('[hb-mine] 池 %d 場（%.0fs）' % (len(df), time.time() - t0),
          flush=True)
    # 排名（match_prestandings 有先有；冇就跳過 rankd 條件）
    ps = {}
    try:
        for mid, hr, ar in conn.execute(
                'SELECT match_id, home_total_rank, away_total_rank '
                'FROM match_prestandings'):
            ps[mid] = (hr, ar)
    except sqlite3.Error:
        pass
    # 球隊全季均值（歷史描述統計，同 w88 ps 特徵同一思路）
    home_g = df.dropna(subset=['home_score']).groupby('home_id')
    away_g = df.dropna(subset=['away_score']).groupby('away_id')
    h_avg = ((home_g['home_score'].mean() + home_g['away_score'].mean()))
    a_avg = ((away_g['away_score'].mean() + away_g['home_score'].mean()))
    h_ga = home_g['away_score'].mean()
    a_ga = away_g['home_score'].mean()
    lg_avg = df.dropna(subset=['home_score']).groupby('league').apply(
        lambda s: (s['home_score'] + s['away_score']).mean())
    lg_avg_d = lg_avg.to_dict()
    # 2026-10-24：8X／新精選 方向特徵（用戶驗證——礦工/運行時共用 eval_match，
    # 條件一致唔會漂移；惰性快取，淨係 8X/新精選 有公式嘅聯賽至會計）
    _x8_cache = {}
    _jx_cache = {}
    import fw8x as _fw8x
    import newjx_engine as _nje
    def _dirs(mid):
        x8 = _x8_cache.get(mid)
        if x8 is None:
            try:
                r = _fw8x.eval_match(conn, mid)
                x8 = r[1]['dir'] if r else 'none'
            except Exception:
                x8 = 'none'
            _x8_cache[mid] = x8
        jx = _jx_cache.get(mid)
        if jx is None:
            try:
                v = _nje.eval_match(conn, mid)
                jx = v[2] if v else 'none'
            except Exception:
                jx = 'none'
            _jx_cache[mid] = jx
        return x8, jx
    # 逐場特徵集合
    print('[hb-mine] 構建特徵…', flush=True)
    feat_sets = []
    mids = df['id'].tolist()
    kickoffs = df['kickoff'].tolist()
    leagues = df['league'].tolist()
    lines = df['ou_line'].tolist()
    overs = df['ou_over'].tolist()
    unders = df['ou_under'].tolist()
    ilines = df['oi_line'].tolist()
    iovers = df['oi_over'].tolist()
    f4s = df['o4_line'].tolist()
    hcs = df['ah_hc'].tolist()
    gvs = df['ah_gv'].tolist()
    hids = df['home_id'].tolist()
    aids = df['away_id'].tolist()
    tots = (df['home_score'] + df['away_score']).tolist()
    for i in range(len(df)):
        hc, gv = hcs[i], gvs[i]
        sline = None if hc is None or (hc is not None and np.isnan(hc)) else \
            (hc if gv == 'home' else (-hc if gv == 'away' else 0.0))
        def _f(x):
            return None if x is None or (isinstance(x, float) and np.isnan(x)) \
                else float(x)
        pr = ps.get(mids[i]) or (None, None)
        _x8d, _jxd = _dirs(mids[i])
        feat_sets.append(R.feats(
            line=_f(lines[i]), init_line=_f(ilines[i]),
            over=_f(overs[i]), under=_f(unders[i]),
            init_over=_f(iovers[i]), pre4h_line=_f(f4s[i]),
            lg_avg_total=float(lg_avg_d.get(leagues[i], np.nan)) or None,
            h_avg_total=float(h_avg.get(hids[i], np.nan)) or None,
            a_avg_total=float(a_avg.get(aids[i], np.nan)) or None,
            h_ga=float(h_ga.get(hids[i], np.nan)) or None,
            a_ga=float(a_ga.get(aids[i], np.nan)) or None,
            sline=sline, rank_h=pr[0], rank_a=pr[1],
            x8dir=None if _x8d == 'none' else _x8d,
            jxdir=None if _jxd == 'none' else _jxd))
    print('[hb-mine] 特徵完成 %.0fs' % (time.time() - t0), flush=True)
    # 逐聯賣挖掘
    out = {'meta': {
        'spec': 'train<=2024 & test 2025+ 兩期命中率>=%.1f%%，訓練n>=%d 驗證n>=%d；'
                '走盤唔計入' % (THRESH * 100, R.MIN_TRAIN_N, R.MIN_TEST_N),
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'matches': int(len(df)), 'prestandings_matches': len(ps)},
        'leagues': {}}
    by_lg = {}
    for i, lg in enumerate(leagues):
        by_lg.setdefault(lg, []).append(i)
    n_lg = 0
    for lg, idxs in sorted(by_lg.items()):
        if len(idxs) < min_lg:
            continue
        # 條件出現率（訓練期）
        train_mask = np.array([kickoffs[i] <= TRAIN_MAX for i in idxs])
        valid = np.array([tots[i] == tots[i] and lines[i] == lines[i]
                          and not (isinstance(lines[i], float)
                                   and np.isnan(lines[i])) for i in idxs])
        if train_mask.sum() < min_lg // 2:
            continue
        # 條件 bool 陣列
        cond_idx = {}
        for j, i in enumerate(idxs):
            for c in feat_sets[i]:
                cond_idx.setdefault(c, set()).add(j)
        singles = [c for c, s in cond_idx.items()
                   if len([j for j in s if train_mask[j]]) >= 15]
        C = {c: np.array([j in s for j in range(len(idxs))])
             for c, s in cond_idx.items()}
        over = np.array([tots[i] == tots[i] and tots[i] > lines[i]
                         if not (isinstance(lines[i], float) and
                                 (lines[i] is None or (isinstance(lines[i], float) and np.isnan(lines[i]))))
                         else False for i in idxs])
        push = np.array([isinstance(tots[i], float) and tots[i] == lines[i]
                         if not (lines[i] is None or (isinstance(lines[i], float) and np.isnan(lines[i])))
                         else False for i in idxs])
        dec = valid & ~push
        combos = [[c] for c in singles]
        singles_sorted = sorted(singles,
                                key=lambda c: -int((C[c] & train_mask).sum()))
        top = singles_sorted[:15]
        combos += [list(p) for p in itertools.combinations(top, 2)]
        combos += [list(p) for p in itertools.combinations(top[:10], 3)]
        rules = []
        seen = set()
        for combo in combos:
            m = C[combo[0]].copy()
            for c in combo[1:]:
                m &= C[c]
            m &= dec
            tr = m & train_mask
            te = m & ~train_mask
            tn = int(tr.sum())
            sen = int(te.sum())
            if tn < R.MIN_TRAIN_N or sen < R.MIN_TEST_N:
                continue
            tw = int((over & tr).sum())
            sw = int((over & te).sum())
            t_rate = tw / tn
            s_rate = sw / sen
            if t_rate >= THRESH and s_rate >= THRESH:
                side = '大'
                trate, srate = t_rate, s_rate
            elif 1 - t_rate >= THRESH and 1 - s_rate >= THRESH:
                side = '細'
                trate, srate = 1 - t_rate, 1 - s_rate
            else:
                continue
            key = (side, tuple(sorted(combo)))
            if key in seen:
                continue
            seen.add(key)
            an = tn + sen
            # all_hit 用該方向嘅總命中率（細＝1−over 率）
            aw_side = (tw + sw) if side == '大' else (an - (tw + sw))
            rules.append({
                'conds': sorted(combo), 'side': side,
                'train_hit': round(trate, 3), 'train_n': tn,
                'test_hit': round(srate, 3), 'test_n': sen,
                'all_hit': round(aw_side / an, 3) if an else None,
                'all_n': an,
                'label': '%s｜%.1f%%(訓%d)/%.1f%%(驗%d)' % (
                    '＋'.join(sorted(combo)), trate * 100, tn,
                    srate * 100, sen)})
        if rules:
            rules.sort(key=lambda r: (-min(r['train_hit'], r['test_hit']),
                                      -r['all_n']))
            out['leagues'][lg] = rules[:40]
            n_lg += 1
    conn.close()
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    n_rules = sum(len(v) for v in out['leagues'].values())
    print('[hb-mine] 完成 %.0fs：%d 個聯賽有規則，共 %d 條 → %s'
          % (time.time() - t0, n_lg, n_rules, out_path), flush=True)
    for lg, rs in list(out['leagues'].items())[:5]:
        print('  %s: %d 條（最強 %s %.1f%%）' % (
            lg, len(rs), rs[0]['side'], min(rs[0]['train_hit'],
                                            rs[0]['test_hit']) * 100))


if __name__ == '__main__':
    main()
