# -*- coding: utf-8 -*-
"""fw8s_backtest.py — 全庫「精選8」8 格共識方向回測（向量版，口徑同 fw_grid_engine.grid_report）

對每一場有賽果嘅歷史賽事，計 8 格（1I/1O/3A/3I/7/19/21/25）各自方向，
全部一致 → 該場 direction='up'/'down'；否則唔入表。
結果寫入 fw8s_bt(match_id, league, direction, res)，可中斷續跑。

口徑要點（同 grid_report 完全一致）：
- 1X/3X 格＝tp1(初盤)/tp3(開賽前30分鐘) base/base_sw & 全庫/同聯賽；
  base＝盤口100%＋主客水位各±0.03（該時點&尾盤都要中）；
  base_sw＝對調盤100%一樣＋目標返還率 93.9%±0.04（人口 ret_ok 已含）
- 7＝初盤帶符號盤口相同（純盤口，冇水位）
- 19＝上場比賽尾盤盤口（角色＋讓球方＋讓球數）相同
- 21＝主主場/客客場 勝和負比例各±0.07（兩隊 p>0）
- 25＝主主場/客客場 入球/失球/差 各±3（兩隊 p>0）
- 每格 up_r/down_r 分母剔除走盤，n 含走盤；n<5 或 eff=0 → 無方向；
  up_r>0.4999→up，down_r>0.4999→down
- 共識：必填格 7/19/21/25 全部有方向且一致，其餘格一致 → 方向
- 人口包含目標場自己；未完場場次 res 計入 'A'（同引擎 _res_code 一致）

用法： python fw8s_backtest.py           # 全量（可中斷續跑）
        python fw8s_backtest.py --limit N # 只跑前 N 場（測試）
"""
import os
import sqlite3
import sys
import time

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import screen_engine as se
import fw_grid_engine as fg

MIN_N = fg.MIN_N            # 5
RET_LO, RET_HI = 0.939 - 0.04, 0.939 + 0.04
GMAP = {'home': 1, 'away': -1, 'none': 0}
GRID8 = fg.GRID8            # ['1I','1O','3A','3I','7','19','21','25']
MAND = ['7', '19', '21', '25']
NAN_KEY = -999999           # NaN 盤口嘅整數鍵（索引時已過濾，只作佔位）

NEED = [
    'id', 'league', 'res',
    'i_h', 'i_g', 'i_ho', 'i_ao',
    'm30_h', 'm30_g', 'm30_ho', 'm30_ao',
    'c_h', 'c_g', 'c_ho', 'c_ao',
    'i_sw_h', 'i_sw_g', 'm30_sw_h', 'm30_sw_g', 'c_sw_h', 'c_sw_g',
    'ret_ok',
    'hp_h', 'hp_g', 'hp_role',
    'home_home_p', 'away_away_p',
    'home_home_wp', 'home_home_dp', 'home_home_lp',
    'away_away_wp', 'away_away_dp', 'away_away_lp',
    'home_home_gf', 'home_home_ga', 'home_home_gd',
    'away_away_gf', 'away_away_ga', 'away_away_gd',
]


def qk(x):
    """盤口×4 整數鍵（NaN 安全）"""
    x = np.asarray(x, dtype=float)
    return np.where(np.isnan(x), NAN_KEY,
                    np.round(np.where(np.isnan(x), 0.0, x) * 4).astype(np.int64))


def signed_arr(h, g_obj):
    """h(float 陣列) × gmap(g 陣列)；NaN h → NaN"""
    h = np.asarray(h, dtype=float)
    g = np.array([GMAP.get(x, 0) for x in g_obj], dtype=float)
    out = h * g
    out[np.isnan(h)] = np.nan
    return out


def _target_ret_ok(ho, ao):
    if np.isnan(ho) or np.isnan(ao):
        return False
    r = 1.0 / (1.0 / (1.0 + ho) + 1.0 / (1.0 + ao))
    return RET_LO <= r <= RET_HI


def _group(key_cols, valid):
    """key_cols: 整數陣列 list（全行長度）；valid: bool 陣列。→ dict tuple→int64 行號"""
    out = {}
    idx = np.nonzero(valid)[0]
    if idx.size == 0:
        return out
    cols = [c[idx] for c in key_cols]
    for j in range(idx.size):
        out.setdefault(tuple(c[j] for c in cols), []).append(idx[j])
    return {k_: np.asarray(v, dtype=np.int64) for k_, v in out.items()}


def _dir(n, a, b, p):
    """同 fw_grid_engine._dir_of：n 含走盤；分母剔走"""
    if n < MIN_N:
        return None
    eff = n - p
    if eff <= 0:
        return None
    if a / eff > 0.4999:
        return 'up'
    if b / eff > 0.4999:
        return 'down'
    return None


def main():
    limit = None
    reset = '--reset' in sys.argv
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    import v3_engine
    conn = sqlite3.connect(se.DB_PATH, timeout=30)
    # 結果寫獨立庫：football.db 俾爬蟲長寫鎖住都唔會影響回測落庫
    bt_path = os.path.join(os.path.dirname(se.DB_PATH), 'fw8s_bt.db')
    bt = sqlite3.connect(bt_path, timeout=30)
    if reset:
        for a in range(30):
            try:
                bt.execute('DELETE FROM fw8s_bt')
                bt.commit()
                print('[bt] 已清空舊結果', flush=True)
                break
            except sqlite3.OperationalError:
                print('[bt] 清空被鎖，等 10s（%d/30）' % a, flush=True)
                time.sleep(10)
    t0 = time.time()
    df = v3_engine.load_v3_pool(conn)
    print('[bt] pool %d 場，載入 %.0fs' % (len(df), time.time() - t0), flush=True)
    miss = [c for c in NEED if c not in df.columns]
    if miss:
        print('[bt] 缺欄位:', miss)
        print('[bt] 可用欄位:', sorted(df.columns.tolist()))
        sys.exit(1)

    N = len(df)
    f = lambda c: df[c].to_numpy(dtype=float)
    ids = df['id'].to_numpy()
    lg = df['league'].astype('category').cat.codes.to_numpy()
    res_s = df['res'].to_numpy()
    ri = np.zeros(N, dtype=np.int8)
    ri[res_s == 'B'] = 1
    ri[res_s == 'P'] = 2
    has_res = (res_s == 'A') | (res_s == 'B') | (res_s == 'P')

    i_h, i_ho, i_ao = f('i_h'), f('i_ho'), f('i_ao')
    m30_h, m30_ho, m30_ao = f('m30_h'), f('m30_ho'), f('m30_ao')
    c_h, c_ho, c_ao = f('c_h'), f('c_ho'), f('c_ao')
    i_gc = df['i_g'].fillna('none').map(GMAP).to_numpy()
    m30_gc = df['m30_g'].fillna('none').map(GMAP).to_numpy()
    c_gc = df['c_g'].fillna('none').map(GMAP).to_numpy()

    s_i = signed_arr(i_h, df['i_g'].fillna('none'))
    s_m30 = signed_arr(m30_h, df['m30_g'].fillna('none'))
    s_c = signed_arr(c_h, df['c_g'].fillna('none'))
    s_i_sw = signed_arr(f('i_sw_h'), df['i_sw_g'].fillna('none'))
    s_m30_sw = signed_arr(f('m30_sw_h'), df['m30_sw_g'].fillna('none'))
    s_c_sw = signed_arr(f('c_sw_h'), df['c_sw_g'].fillna('none'))
    ret_ok = df['ret_ok'].to_numpy(dtype=bool)

    hp_h = f('hp_h')
    hp_g = df['hp_g'].fillna('none').map(GMAP).to_numpy()
    hp_role = df['hp_role'].map({'home': 1, 'away': 0}).to_numpy()
    hp_role_i = np.where(np.isnan(hp_role), NAN_KEY,
                         np.where(np.isnan(hp_role), 0, hp_role).astype(np.int64))

    hh_p, aa_p = f('home_home_p'), f('away_away_p')
    v_form = (hh_p > 0) & (aa_p > 0)
    i_form = np.nonzero(v_form)[0]
    FH = {c: f(c)[i_form] for c in (
        'home_home_wp', 'home_home_dp', 'home_home_lp',
        'away_away_wp', 'away_away_dp', 'away_away_lp',
        'home_home_gf', 'home_home_ga', 'home_home_gd',
        'away_away_gf', 'away_away_ga', 'away_away_gd')}
    T_HW, T_HD, T_HL = f('home_home_wp'), f('home_home_dp'), f('home_home_lp')
    T_AW, T_AD, T_AL = f('away_away_wp'), f('away_away_dp'), f('away_away_lp')
    T_HF, T_HA, T_HG = f('home_home_gf'), f('home_home_ga'), f('home_home_gd')
    T_AF, T_AA, T_AG = f('away_away_gf'), f('away_away_ga'), f('away_away_gd')

    # ---- 預建索引 ----
    t1 = time.time()
    qi_h, qc_h, qm30_h = qk(i_h), qk(c_h), qk(m30_h)
    g1 = _group([qi_h, i_gc.astype(np.int64), qc_h, c_gc.astype(np.int64)],
                ~(np.isnan(i_h) | np.isnan(c_h)))
    g3 = _group([qm30_h, m30_gc.astype(np.int64), qc_h, c_gc.astype(np.int64)],
                ~(np.isnan(m30_h) | np.isnan(c_h)))
    g7 = _group([qk(s_i)], ~np.isnan(s_i))
    g1s = _group([qk(s_i_sw), qk(s_c_sw)],
                 ~(np.isnan(s_i_sw) | np.isnan(s_c_sw)))
    g3s = _group([qk(s_m30_sw), qk(s_c_sw)],
                 ~(np.isnan(s_m30_sw) | np.isnan(s_c_sw)))
    g19 = _group([hp_role_i, hp_g.astype(np.int64), qk(hp_h)],
                 ~(np.isnan(hp_h) | np.isnan(hp_role)))
    print('[bt] 索引 %.0fs（g1=%d g3=%d g7=%d g1s=%d g3s=%d g19=%d）' % (
        time.time() - t1, len(g1), len(g3), len(g7), len(g1s), len(g3s),
        len(g19)), flush=True)

    # match_id → req_name（一次過）
    id2lg = dict(conn.execute(
        'SELECT m.id, c.req_name FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id'))

    bt.execute('''CREATE TABLE IF NOT EXISTS fw8s_bt(
        match_id INTEGER PRIMARY KEY, league TEXT, direction TEXT, res TEXT)''')
    bt.execute('CREATE INDEX IF NOT EXISTS idx_fw8s_bt_lg '
               'ON fw8s_bt(league, direction)')
    bt.commit()
    existing = set(r[0] for r in bt.execute('SELECT match_id FROM fw8s_bt'))
    print('[bt] 已有 %d 場，待評估 %d 場' % (
        len(existing), int(has_res.sum()) - len(existing)), flush=True)

    def pack(idx, t_lg):
        if idx is None:
            return None
        if t_lg is not None:
            idx = idx[lg[idx] == t_lg]
        n = int(idx.size)
        if n == 0:
            return None
        return _dir(n, int(np.count_nonzero(ri[idx] == 0)),
                    int(np.count_nonzero(ri[idx] == 1)),
                    int(np.count_nonzero(ri[idx] == 2)))

    qs_i, qs_m30, qs_c = qk(s_i), qk(s_m30), qk(s_c)
    qhp_h = qk(hp_h)

    def _flush():
        if not batch:
            return
        for attempt in range(4):
            try:
                bt.executemany(
                    'INSERT OR IGNORE INTO fw8s_bt(match_id, league, direction, res) '
                    'VALUES(?,?,?,?)', batch)
                bt.commit()
                batch.clear()
                return
            except sqlite3.OperationalError:
                print('[bt] 寫入被鎖，等 8s（%d/4）' % attempt, flush=True)
                time.sleep(8)
        print('[bt] 警告：本批 %d 場寫唔入，跳過（下輪會補）' % len(batch), flush=True)
        batch.clear()

    batch = []
    done = kept = 0
    t2 = time.time()
    order = np.nonzero(has_res)[0]
    if limit:
        order = order[:limit]
    for k in order:
        mid = int(ids[k])
        if mid in existing:
            continue
        try:
            t_lg = lg[k]
            # ---- tp1 base / base_sw ----
            idx1 = idx1s = None
            if not (np.isnan(i_h[k]) or np.isnan(c_h[k])):
                cand = g1.get((qi_h[k], i_gc[k], qc_h[k], c_gc[k]))
                if cand is not None:
                    w = (np.abs(i_ho[cand] - i_ho[k]) <= 0.03) & \
                        (np.abs(i_ao[cand] - i_ao[k]) <= 0.03) & \
                        (np.abs(c_ho[cand] - c_ho[k]) <= 0.03) & \
                        (np.abs(c_ao[cand] - c_ao[k]) <= 0.03)
                    idx1 = cand[w]
                # swap：人口「對調盤」== 目標「純盤口」（_signed_T 係 plain，
                # 唔係 population 嘅 sw——呢度係 bug 修正關鍵）
                if _target_ret_ok(i_ho[k], i_ao[k]) and \
                        _target_ret_ok(c_ho[k], c_ao[k]):
                    cand = g1s.get((qs_i[k], qs_c[k]))
                    if cand is not None:
                        idx1s = cand[ret_ok[cand]]
            # ---- tp3 base / base_sw ----
            idx3 = idx3s = None
            if not (np.isnan(m30_h[k]) or np.isnan(c_h[k])):
                cand = g3.get((qm30_h[k], m30_gc[k], qc_h[k], c_gc[k]))
                if cand is not None:
                    w = (np.abs(m30_ho[cand] - m30_ho[k]) <= 0.03) & \
                        (np.abs(m30_ao[cand] - m30_ao[k]) <= 0.03) & \
                        (np.abs(c_ho[cand] - c_ho[k]) <= 0.03) & \
                        (np.abs(c_ao[cand] - c_ao[k]) <= 0.03)
                    idx3 = cand[w]
                if _target_ret_ok(m30_ho[k], m30_ao[k]) and \
                        _target_ret_ok(c_ho[k], c_ao[k]):
                    cand = g3s.get((qs_m30[k], qs_c[k]))
                    if cand is not None:
                        idx3s = cand[ret_ok[cand]]
            # ---- 7 / 19 ----
            idx7 = None if np.isnan(s_i[k]) else g7.get((qs_i[k],))
            idx19 = None
            if not (np.isnan(hp_h[k]) or np.isnan(hp_role[k])):
                idx19 = g19.get((hp_role_i[k], hp_g[k], qhp_h[k]))
            # ---- 21 / 25 ----
            idx21 = idx25 = None
            if hh_p[k] > 0 and aa_p[k] > 0:
                m = (np.abs(FH['home_home_wp'] - T_HW[k]) <= 0.07) & \
                    (np.abs(FH['home_home_dp'] - T_HD[k]) <= 0.07) & \
                    (np.abs(FH['home_home_lp'] - T_HL[k]) <= 0.07) & \
                    (np.abs(FH['away_away_wp'] - T_AW[k]) <= 0.07) & \
                    (np.abs(FH['away_away_dp'] - T_AD[k]) <= 0.07) & \
                    (np.abs(FH['away_away_lp'] - T_AL[k]) <= 0.07)
                idx21 = i_form[m]
                m = (np.abs(FH['home_home_gf'] - T_HF[k]) <= 3) & \
                    (np.abs(FH['home_home_ga'] - T_HA[k]) <= 3) & \
                    (np.abs(FH['home_home_gd'] - T_HG[k]) <= 3) & \
                    (np.abs(FH['away_away_gf'] - T_AF[k]) <= 3) & \
                    (np.abs(FH['away_away_ga'] - T_AA[k]) <= 3) & \
                    (np.abs(FH['away_away_gd'] - T_AG[k]) <= 3)
                idx25 = i_form[m]
            # ---- 8 格方向 + 共識 ----
            dirs = {'1I': pack(idx1s, None), '1O': pack(idx1s, t_lg),
                    '3A': pack(idx3, None), '3G': pack(idx3, t_lg),
                    '3I': pack(idx3s, None), '3O': pack(idx3s, t_lg),
                    '7': pack(idx7, None), '19': pack(idx19, None),
                    '21': pack(idx21, None), '25': pack(idx25, None)}
            direction = None
            mand = [dirs[c] for c in MAND]
            if not any(d is None for d in mand) and len(set(mand)) == 1:
                want = [dirs[c] for c in GRID8]
                if not any(d is None for d in want) and len(set(want)) == 1:
                    direction = want[0]
            # 全部評估過嘅場都落庫（direction 可以係 NULL）——否則冇共識嘅場
            # 每輪都要重新評估，永遠行唔到尾（2026-10-18 教訓）
            batch.append((mid, id2lg.get(mid, ''), direction,
                          str(res_s[k])))
            if direction:
                kept += 1
        except Exception:
            pass
        done += 1
        if done % 1500 == 0:
            _flush()
        if done % 5000 == 0:
            el = time.time() - t2
            print('[bt] %d/%d，%.0f 場/秒，kept=%d' % (
                done, len(order), done / max(el, 0.1), kept), flush=True)
    _flush()
    print('[bt] 完成：評估 %d 場，有共識 %d 場，回測耗時 %.0fs' % (
        done, kept, time.time() - t2), flush=True)
    print('[bt] 總計 %.0fs' % (time.time() - t0), flush=True)
    bt.close()


if __name__ == '__main__':
    main()
