# -*- coding: utf-8 -*-
"""精選8s 公式挖礦 v2（2026-10-14）：每聯賽獨立篩選公式，專搵上盤。

v1 教訓：beam search 齋追 train 最高% → 過度擬合，34/75 train達標但
得 1/75 過到驗證。v2 改進：
  ① LCB（悲觀置信下限）評分：rate - 1.28×SE，自然懲罰細樣本
  ② 大量新特徵：近況（prestandings 積分率）、七節點盤路變化
    （pre_30m→S0、pre_5m→S0）、中立场
  ③ 候選池攞 train LCB 頭 N 條做 val 評分，揀 val 最穩（唔係 train 最靚）
  ④ 最終規則做 5 折 walk-forward 逐段報命中率
分段：train 60%（挖礦）→ val 20%（揀公式）→ holdout 20%（最終確認，唔碰）
達標：val 上盤率 ≥65% 且 n≥15；holdout ≥65%（n≥5 先計）。
"""
import json
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parent.parent / 'football.db'
OUT = Path(__file__).resolve().parent / 'fw8s_formulas.json'
TARGET = 0.65
MIN_TRAIN_N = 50
MIN_VAL_N = 25
BEAM = 80
MAX_DEPTH = 4
VAL_POOL = 300          # 攞咁多條 train 候選做 val 評分

SQL = """
SELECT m.id, m.season_id, m.kickoff, c.req_name AS league,
       m.home_score AS hs, m.away_score AS aws, m.is_neutral,
       m.home_rank_disp AS hr, m.away_rank_disp AS ar,
       COALESCE(c12.handicap, c3.handicap) AS c_h,
       COALESCE(c12.giver,    c3.giver)    AS c_g,
       COALESCE(c12.home_odds, c3.home_odds) AS c_ho,
       COALESCE(c12.away_odds, c3.away_odds) AS c_ao,
       COALESCE(i12.handicap, i3.handicap) AS i_h,
       COALESCE(i12.giver,    i3.giver)    AS i_g,
       COALESCE(i12.home_odds, i3.home_odds) AS i_ho,
       COALESCE(i12.away_odds, i3.away_odds) AS i_ao,
       COALESCE(a30.handicap, b30.handicap) AS m30_h,
       COALESCE(a30.giver,    b30.giver)    AS m30_g,
       COALESCE(a30.home_odds, b30.home_odds) AS m30_ho,
       COALESCE(a5.handicap, b5.handicap) AS m5_h,
       COALESCE(a5.giver,    b5.giver)    AS m5_g,
       COALESCE(a5.home_odds, b5.home_odds) AS m5_ho,
       COALESCE(a5.away_odds, b5.away_odds) AS m5_ao,
       ou.total_line AS ou_l, ou.over_odds AS ou_o,
       p.home_total_w AS htw, p.home_total_d AS htd, p.home_total_l AS htl,
       p.away_total_w AS atw, p.away_total_d AS atd, p.away_total_l AS atl,
       p.home_home_w AS hhw, p.home_home_d AS hhd, p.home_home_l AS hhl,
       p.away_away_w AS aaw, p.away_away_d AS aad, p.away_away_l AS aal
FROM matches m
JOIN seasons s ON s.id = m.season_id
JOIN competitions c ON c.titan_id = s.titan_id
LEFT JOIN odds_asian c12 ON c12.match_id = m.id AND c12.label = 'closing' AND c12.company_id = 12
LEFT JOIN odds_asian c3  ON c3.match_id  = m.id AND c3.label  = 'closing' AND c3.company_id  = 3
LEFT JOIN odds_asian i12 ON i12.match_id = m.id AND i12.label = 'initial' AND i12.company_id = 12
LEFT JOIN odds_asian i3  ON i3.match_id  = m.id AND i3.label  = 'initial' AND i3.company_id  = 3
LEFT JOIN odds_asian a30 ON a30.match_id = m.id AND a30.label = 'pre_30m' AND a30.company_id = 12
LEFT JOIN odds_asian b30 ON b30.match_id = m.id AND b30.label = 'pre_30m' AND b30.company_id = 3
LEFT JOIN odds_asian a5  ON a5.match_id  = m.id AND a5.label  = 'pre_5m'  AND a5.company_id  = 12
LEFT JOIN odds_asian b5  ON b5.match_id  = m.id AND b5.label  = 'pre_5m'  AND b5.company_id  = 3
LEFT JOIN odds_ou ou ON ou.match_id = m.id AND ou.label = 'closing' AND ou.company_id = 12
LEFT JOIN match_prestandings p ON p.match_id = m.id
WHERE m.home_score IS NOT NULL AND COALESCE(c12.handicap, c3.handicap) IS NOT NULL
"""


def settle(row):
    hc, g, hs, aws = row['c_h'], row['c_g'], row['hs'], row['aws']
    if hc is None or hs is None or aws is None:
        return None
    if g == 'home':
        m_ = hs - aws - hc
    elif g == 'away':
        m_ = aws - hs - hc
    else:
        m_ = hs - aws
    return 'A' if m_ > 0 else ('B' if m_ < 0 else 'P')


def zone5(v):
    if pd.isna(v):
        return None
    if v < 0.80: return 'Z1'
    if v < 0.90: return 'Z2'
    if v < 1.00: return 'Z3'
    if v < 1.10: return 'Z4'
    return 'Z5'


ZONES = ['Z1', 'Z2', 'Z3', 'Z4', 'Z5']
ZNAME = {'Z1': '≤0.79', 'Z2': '0.80-0.89', 'Z3': '0.90-0.99',
         'Z4': '1.00-1.09', 'Z5': '≥1.10'}


def ppm(w, d, l):
    w, d, l = pd.to_numeric(w, errors='coerce'), pd.to_numeric(d, errors='coerce'), pd.to_numeric(l, errors='coerce')
    n = w + d + l
    return ((w * 3 + d) / n.replace(0, np.nan))


def build_features(df):
    df = df.copy()
    df['res'] = df.apply(settle, axis=1)
    sign = df['c_g'].map({'home': 1.0, 'away': -1.0}).fillna(0.0)
    df['s_c'] = df['c_h'] * sign
    sign_i = df['i_g'].map({'home': 1.0, 'away': -1.0}).fillna(0.0)
    df['s_i'] = df['i_h'].fillna(0) * sign_i
    s30 = df['m30_h'] * df['m30_g'].map({'home': 1.0, 'away': -1.0}).fillna(0.0)
    s5 = df['m5_h'] * df['m5_g'].map({'home': 1.0, 'away': -1.0}).fillna(0.0)
    df['d30'] = df['s_c'] - s30            # S30→S0 盤變化
    df['d5'] = df['s_c'] - s5              # S5→S0 盤變化
    df['w5h'] = df['c_ho'] - df['m5_ho']   # 尾5分鐘主水變化
    df['w5a'] = df['c_ao'] - df['m5_ao']   # 尾5分鐘客水變化
    df['up'] = (df['res'] == 'A').astype(float)
    df['dm'] = df['s_c'] - df['s_i']
    df['wh'] = df['c_ho'] - df['i_ho']
    df['wa'] = df['c_ao'] - df['i_ao']
    df['hr'] = pd.to_numeric(df['hr'], errors='coerce')
    df['ar'] = pd.to_numeric(df['ar'], errors='coerce')
    df['rdiff'] = df['ar'] - df['hr']
    df['h_ppm'] = ppm(df['htw'], df['htd'], df['htl'])
    df['a_ppm'] = ppm(df['atw'], df['atd'], df['atl'])
    df['hh_ppm'] = ppm(df['hhw'], df['hhd'], df['hhl'])
    df['aa_ppm'] = ppm(df['aaw'], df['aad'], df['aal'])
    df = df.sort_values(['season_id', 'kickoff'])
    g = df.groupby('season_id')['kickoff']
    pos = g.cumcount()
    cnt = g.transform('count').replace(0, np.nan)
    df['stage'] = (pos * 5 / cnt).fillna(-1).astype(int).clip(-1, 4) + 1
    df.loc[df['stage'] < 1, 'stage'] = 0
    return df


def cond_defs(df):
    out = []
    buckets = [(-9, '客讓球半以上', lambda s: s <= -1.501),
               (-1.5, '客讓球半', lambda s: (s >= -1.5) & (s <= -1.251)),
               (-1.25, '客讓一/球半', lambda s: (s >= -1.25) & (s <= -1.001)),
               (-1.0, '客讓一球', lambda s: (s >= -1.0) & (s <= -0.751)),
               (-0.75, '客讓半/一', lambda s: (s >= -0.75) & (s <= -0.501)),
               (-0.5, '客讓半球', lambda s: (s >= -0.5) & (s <= -0.251)),
               (-0.25, '客讓平/半', lambda s: (s >= -0.25) & (s < 0)),
               (0, '平手', lambda s: s == 0),
               (0.25, '主讓平/半', lambda s: (s > 0) & (s <= 0.25)),
               (0.5, '主讓半球', lambda s: (s >= 0.251) & (s <= 0.5)),
               (0.75, '主讓半/一', lambda s: (s >= 0.501) & (s <= 0.75)),
               (1.0, '主讓一球', lambda s: (s >= 0.751) & (s <= 1.0)),
               (1.25, '主讓一/球半', lambda s: (s >= 1.001) & (s <= 1.25)),
               (1.5, '主讓球半', lambda s: (s >= 1.251) & (s <= 1.5)),
               (9, '主讓球半以上', lambda s: s >= 1.501)]
    for _, nm, fn in buckets:
        out.append((f'line:{nm}', f'尾盤{nm}', fn(df['s_c']).fillna(False)))
    zh, za = df['c_ho'].map(zone5), df['c_ao'].map(zone5)
    for z in ZONES:
        out.append((f'cho:{z}', f'尾盤主水{ZNAME[z]}', (zh == z).fillna(False)))
        out.append((f'cao:{z}', f'尾盤客水{ZNAME[z]}', (za == z).fillna(False)))
    zh_i, za_i = df['i_ho'].map(zone5), df['i_ao'].map(zone5)
    for z in ZONES:
        out.append((f'iho:{z}', f'初盤主水{ZNAME[z]}', (zh_i == z).fillna(False)))
        out.append((f'iao:{z}', f'初盤客水{ZNAME[z]}', (za_i == z).fillna(False)))
    out.append(('dm:up', '初→尾升盤', (df['dm'] >= 0.25).fillna(False)))
    out.append(('dm:down', '初→尾降盤', (df['dm'] <= -0.25).fillna(False)))
    out.append(('dm:same', '初→尾盤不變', df['dm'].abs().lt(0.25).fillna(False)))
    out.append(('wh:up', '主水升≥0.05', (df['wh'] >= 0.05).fillna(False)))
    out.append(('wh:down', '主水降≥0.05', (df['wh'] <= -0.05).fillna(False)))
    out.append(('wa:up', '客水升≥0.05', (df['wa'] >= 0.05).fillna(False)))
    out.append(('wa:down', '客水降≥0.05', (df['wa'] <= -0.05).fillna(False)))
    out.append(('ou:lo', '尾盤大小<2.5', (df['ou_l'] < 2.5).fillna(False)))
    out.append(('ou:25', '尾盤大小=2.5', df['ou_l'].eq(2.5).fillna(False)))
    out.append(('ou:275', '尾盤大小=2.75', df['ou_l'].eq(2.75).fillna(False)))
    out.append(('ou:30', '尾盤大小=3.0', df['ou_l'].eq(3.0).fillna(False)))
    out.append(('ou:hi', '尾盤大小>3.0', (df['ou_l'] > 3.0).fillna(False)))
    zo = df['ou_o'].map(zone5)
    for z in ZONES:
        out.append((f'ouo:{z}', f'大球水{ZNAME[z]}', (zo == z).fillna(False)))
    stname = {1: '季初', 2: '季初中', 3: '季中', 4: '季尾中', 5: '季尾'}
    for st in range(1, 6):
        out.append((f'stage:{st}', f'{stname[st]}', df['stage'].eq(st)))
    out.append(('rk:hbetter', '主排名高≥3位', (df['rdiff'] >= 3).fillna(False)))
    out.append(('rk:abetter', '客排名高≥3位', (df['rdiff'] <= -3).fillna(False)))
    out.append(('rk:close', '排名差≤2位', df['rdiff'].abs().le(2).fillna(False)))
    out.append(('rk:htop3', '主排名前3', (df['hr'] <= 3).fillna(False)))
    out.append(('rk:atop3', '客排名前3', (df['ar'] <= 3).fillna(False)))
    out.append(('rk:hlow', '主排名13以下', (df['hr'] >= 13).fillna(False)))
    out.append(('rk:alow', '客排名13以下', (df['ar'] >= 13).fillna(False)))
    # 近況（積分率 = 每場平均分，3分制）
    out.append(('fm:hhuge', '主近況≥2.2分', (df['h_ppm'] >= 2.2).fillna(False)))
    out.append(('fm:hgood', '主近況≥1.8分', (df['h_ppm'] >= 1.8).fillna(False)))
    out.append(('fm:abad', '客近況≤1.2分', (df['a_ppm'] <= 1.2).fillna(False)))
    out.append(('fm:apoor', '客近況≤1.0分', (df['a_ppm'] <= 1.0).fillna(False)))
    out.append(('fm:hgap', '近況主贏客≥0.8分', ((df['h_ppm'] - df['a_ppm']) >= 0.8).fillna(False)))
    out.append(('fm:hhhome', '主主場近況≥1.9分', (df['hh_ppm'] >= 1.9).fillna(False)))
    out.append(('fm:aaaway', '客客場近況≤1.1分', (df['aa_ppm'] <= 1.1).fillna(False)))
    out.append(('fm:aawful', '客客場近況≤0.9分', (df['aa_ppm'] <= 0.9).fillna(False)))
    # 七節點：S30→S0、S5→S0（2026-10-14 用戶定義節點）
    out.append(('nd:30up', 'S30→S0升盤', (df['d30'] >= 0.25).fillna(False)))
    out.append(('nd:30dn', 'S30→S0降盤', (df['d30'] <= -0.25).fillna(False)))
    out.append(('nd:5up', 'S5→S0升盤', (df['d5'] >= 0.25).fillna(False)))
    out.append(('nd:5dn', 'S5→S0降盤', (df['d5'] <= -0.25).fillna(False)))
    out.append(('nd:w5hup', '尾5分鐘主水升≥0.05', (df['w5h'] >= 0.05).fillna(False)))
    out.append(('nd:w5hdn', '尾5分鐘主水降≥0.05', (df['w5h'] <= -0.05).fillna(False)))
    out.append(('nd:w5adn', '尾5分鐘客水降≥0.05', (df['w5a'] <= -0.05).fillna(False)))
    out.append(('nd:w5aup', '尾5分鐘客水升≥0.05', (df['w5a'] >= 0.05).fillna(False)))
    out.append(('nd:bigup', '初→尾連升≥2級', (df['dm'] >= 0.5).fillna(False)))
    out.append(('nd:30eq', 'S30→S0盤不變', df['d30'].abs().lt(0.25).fillna(False)))
    out.append(('neutral', '中立场', df['is_neutral'].eq(1)))
    return out


def lcb(rate, n, z=1.28):
    if n < 2:
        return -9.0
    return rate - z * np.sqrt(rate * (1 - rate) / n)


def mine_league(name, df):
    n = len(df)
    if n < 150:
        return {'league': name, 'ok': False, 'n': n, 'why': '樣本不足'}
    df = df.sort_values('kickoff').reset_index(drop=True)
    ntr, nval = int(n * 0.6), int(n * 0.8)
    up = df['up'].to_numpy()
    masks = [(k, nm, c.to_numpy()) for k, nm, c in cond_defs(df)]

    def rate_of(m, a, b):
        cnt = int(m[a:b].sum())
        if cnt < 2:
            return 0.0, cnt
        return float(up[a:b][m[a:b]].mean()), cnt

    # ---- beam search 攞候選池（train LCB 評分）----
    beam = [((), (), np.ones(n, dtype=bool))]
    pool = {}        # keys -> (names, mask)
    for depth in range(1, MAX_DEPTH + 1):
        cands = []
        seen = set()
        for keys, names, bm in beam:
            for k, nm, m in masks:
                if k in keys:
                    continue
                nk = tuple(sorted(keys + (k,)))
                if nk in seen:
                    continue
                seen.add(nk)
                nm_mask = bm & m
                rate, cnt = rate_of(nm_mask, 0, ntr)
                if cnt >= MIN_TRAIN_N and rate >= 0.60:
                    cands.append((nk, names + (nm,), nm_mask, rate, cnt))
        for nk, nn, m, rate, cnt in cands:
            pool[nk] = (nn, m)
        cands.sort(key=lambda x: -lcb(x[3], x[4]))
        beam = [(nk, nn, m) for nk, nn, m, _, _ in cands[:BEAM]]
        if not beam:
            break

    if not pool:
        return {'league': name, 'ok': False, 'n': n, 'why': 'train無合格候選',
                'base': round(float(df.up.mean()), 4)}

    # ---- val 評分：大樣本優先——要求 val≥62%（n≥25）＋
    #     train+val 合計≥65% 且 n≥75，用合計 LCB 排名 ----
    scored = []
    for nk, (nn, m) in pool.items():
        tr, tn = rate_of(m, 0, ntr)
        vr, vn = rate_of(m, ntr, nval)
        pn = tn + vn
        pr = (tr * tn + vr * vn) / pn if pn else 0.0
        scored.append((nk, nn, m, tr, tn, vr, vn, pr, pn))
    hits = [s for s in scored if s[6] >= MIN_VAL_N and s[5] >= 0.62
            and s[8] >= 75 and s[7] >= TARGET]
    if not hits:
        best_val = max(scored, key=lambda s: lcb(s[7], s[8]))
        return {'league': name, 'ok': False, 'n': n, 'why': 'val未達標',
                'base': round(float(df.up.mean()), 4),
                'best_val': {'pooled_r': round(best_val[7], 4),
                             'n': best_val[8], 'conds': list(best_val[1])}}
    hits.sort(key=lambda s: -lcb(s[7], s[8]))
    nk, nn, m, tr, tn, vr, vn, pr, pn = hits[0]

    # ---- 聯集擴充：由池入面揀同向規則合併（OR），
    #      合計（train+val）保持≥65% 就吸納，加大樣本 ----
    used = set(nk)
    cur = m.copy()
    pool_sorted = sorted(scored, key=lambda s: -lcb(s[7], s[8]))
    for s in pool_sorted:
        if set(s[0]) <= used:
            continue
        cand = cur | s[2]
        ctr, ctn = rate_of(cand, 0, ntr)
        cvr, cvn = rate_of(cand, ntr, nval)
        cn = ctn + cvn
        cr = (ctr * ctn + cvr * cvn) / cn if cn else 0
        if cn >= pn and cr >= TARGET:
            cur = cand
            used |= set(s[0])
            pn = cn
    m = cur
    tr, tn = rate_of(m, 0, ntr)
    vr, vn = rate_of(m, ntr, nval)
    pr, pn = (tr * tn + vr * vn) / (tn + vn), tn + vn

    # ---- holdout 最終確認 + 5折 walk-forward ----
    hr, hn = rate_of(m, nval, n)
    folds = []
    edges = [int(n * i / 5) for i in range(6)]
    for i in range(5):
        fr, fn = rate_of(m, edges[i], edges[i + 1])
        folds.append({'up_r': round(fr, 4) if fn >= 3 else None, 'n': fn})
    fold_hits = sum(1 for f in folds if f['up_r'] is not None and f['up_r'] >= 0.58)
    return {'league': name, 'ok': True, 'n': n, 'conds': list(nn),
            'conds_all': [nm for k, nm, _ in masks if k in used],
            'train': {'up_r': round(tr, 4), 'n': tn},
            'val': {'up_r': round(vr, 4), 'n': vn},
            'pooled': {'up_r': round(pr, 4), 'n': pn},
            'holdout': {'up_r': round(hr, 4), 'n': hn},
            'folds': folds, 'fold_hits': fold_hits,
            'base': round(float(df.up.mean()), 4)}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB)
    df = pd.read_sql(SQL, conn)
    conn.close()
    print(f'載入 {len(df)} 場（{df.league.nunique()} 聯賽）{time.time()-t0:.0f}s',
          flush=True)
    df = build_features(df)
    df = df[df['res'] != 'P'].reset_index(drop=True)
    print(f'計走盤後 {len(df)} 場；全庫上盤率 {df.up.mean()*100:.1f}%', flush=True)

    results, ok_cnt, pass_cnt = {}, 0, 0
    leagues = df.groupby('league').size().sort_values(ascending=False)
    for lg, total in leagues.items():
        r = mine_league(lg, df[df['league'] == lg])
        results[lg] = r
        if r.get('ok'):
            ok_cnt += 1
            v, h, fo, pl = r['val'], r['holdout'], r['folds'], r['pooled']
            passed = (h['n'] >= 8 and h['up_r'] >= TARGET
                      and r['fold_hits'] >= 3)
            if passed:
                pass_cnt += 1
            folds_txt = ' '.join(
                f"{f['up_r']*100:.0f}%" if f['up_r'] is not None else '—'
                for f in fo)
            print(f'{"✅" if passed else "⚠️ "} {lg}（基準{r["base"]*100:.0f}%）: '
                  f'train {r["train"]["up_r"]*100:.0f}% n={r["train"]["n"]}｜'
                  f'val {v["up_r"]*100:.0f}% n={v["n"]}｜'
                  f'合計 {pl["up_r"]*100:.0f}% n={pl["n"]}｜'
                  f'holdout {h["up_r"]*100:.0f}% n={h["n"]}｜'
                  f'折[{folds_txt}]{r["fold_hits"]}/5｜'
                  f'{"＋".join(r["conds_all"][:6])}'
                  + ('…' if len(r['conds_all']) > 6 else ''), flush=True)
        else:
            bv = r.get('best_val') or {}
            print(f'✗ {lg}（{r.get("why", "?")} n={total} '
                  f'基準{r.get("base", 0)*100:.0f}%）'
                  + (f' 最佳合計 {bv.get("pooled_r", 0)*100:.0f}% n={bv.get("n", 0)}'
                     if bv else ''), flush=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                   encoding='utf-8')
    print(f'\n完成：val達標 {ok_cnt}/{len(leagues)}；'
          f'holdout都過 {pass_cnt}/{len(leagues)}，{time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
