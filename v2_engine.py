# -*- coding: utf-8 -*-
"""V2 45 項分析引擎（2026-09-26 用戶定稿編號 1–45）。

編號（報告 3.8 節，用戶已確認）：
  1–6   六個時點組（同[初盤/4h/30m/15m/10m/5m]及同尾盤，盤口100%一樣水位±0.03）
        → 16 格 A–P（8 個範圍 × 混合/同主隊）＋12 段水位表
  7–13  H2H 純盤口版七時點（上賽尾盤[互換對比] vs 今場各時點，三合一：同主客場/對調/綜合）
  14–20 H2H 水位版七時點（12 段水位表 × 三合一 × 全庫/聯賽類/同一聯賽＝9 格）
  21–24 勝負比例 ±7%（21 主場盤口版／22 主場水位版／23 總和盤口版／24 總和水位版）＋同今場尾盤
  25–26 入球 ±3 主客場（25 盤口版／26 水位版）＋同今場尾盤
  27–28 排名 ±2（27 主場排名vs客場排名／28 總排名）＋同今場尾盤 → 分佈＋水位
  29    上次比賽完全相同（主隊角色+讓/受讓+讓球數）→ 盤口分佈＋各盤口水位
  30    上次比賽樣本 → 分佈最多盤口 與今場尾盤淨差距（8 條規則）
  31    今賽比上賽 深/淺/不變（對上賽尾盤：互換對比版＋同主客原盤版；Check 下先用）
  32    排名差距淨值（主場−客場）±1 ＋同今場尾盤 → 分佈＋水位
  33    上次比賽樣本 → 上盤勝率最接近 50% 盤口 與今場尾盤淨差距（8 條規則；精選用）
  34–35 入球 ±3（34 主場 → 分佈＋水位／35 總和 → 分佈＋水位）＋同今場尾盤
  36–45 首 14 分鐘入球%（主入/客入/冇入）：36–41 六時點組×16格／42–43 排名差距×2／44–45 入球±3×2

同主客（V2 語義，報告 3.2）：時點組 16 格嘅「同主隊」＝樣本場次嘅主隊係今場主隊（跟隊）；
H2H 嘅「同主客場」＝對上一次對賽嘅主客方向同今場一致（pv_same）。
N≤5 照顯示實數（用戶 2026-09-26 指示）。
"""
import threading
import time

import numpy as np
import pandas as pd

import screen_engine as se

RECENT2Y = '2024-07-01'
ZONES12 = ['≤0.64', '0.65-0.69', '0.70-0.74', '0.75-0.79', '0.80-0.84', '0.85-0.89',
           '0.90-0.94', '0.95-0.99', '1.00-1.04', '1.05-1.09', '1.10-1.14', '≥1.15']

# 六時點組（項目 1–6／36–41）
TP6 = [('1', 'initial', 'i', '初盤'),
       ('2', 'pre_4h', 'f', '開賽前4小時'),
       ('3', 'pre_30m', 'm30', '開賽前30分鐘'),
       ('4', 'pre_15m', 'm15', '開賽前15分鐘'),
       ('5', 'pre_10m', 'm10', '開賽前10分鐘'),
       ('6', 'pre_5m', 'm5', '開賽前5分鐘')]
# H2H 七時點（項目 7–13 純盤口／14–20 水位版）；13/20＝尾盤時點
TP7 = TP6 + [('7', 'closing', 'c', '尾盤')]

SCOPES8 = ['全庫', '聯賽類', '杯賽類', '近兩年', '近兩年聯賽類', '近兩年杯賽類',
           '同一聯賽', '近兩年同一聯賽']
MIX2 = ['混合', '同主隊']
LETTERS = 'ABCDEFGHIJKLMNOP'      # A–H＝混合×8 範圍，I–P＝同主隊×8 範圍

EXTRA_LABELS = {'m30': 'pre_30m', 'm15': 'pre_15m', 'm10': 'pre_10m', 'm5': 'pre_5m'}

_v2_pool = {'df': None, 'ts': 0}
_v2_lock = threading.Lock()


def zone12_of(w):
    """上盤水位 → 12 段 index（0..11）；冇水位 → -1"""
    if w is None or (isinstance(w, float) and np.isnan(w)):
        return -1
    if w < 0.65:
        return 0
    if w >= 1.15:
        return 11
    return int((w - 0.65) * 20) + 1     # 0.65-0.69→1 … 1.10-1.14→10


def load_v2_pool(conn, max_age=1800):
    """V2 數據池＝V1 池 + 30m/15m/10m/5m 四個時點 + 近兩年 flag + 12 段水位"""
    now = time.time()
    if _v2_pool['df'] is not None and now - _v2_pool['ts'] < max_age:
        return _v2_pool['df']
    with _v2_lock:
        now = time.time()
        if _v2_pool['df'] is not None and now - _v2_pool['ts'] < max_age:
            return _v2_pool['df']
        df, _ = se.load_pool(conn, max_age=max_age)
        df = df.copy()          # 唔好污染 V1 嘅快取
        for pfx, label in EXTRA_LABELS.items():
            ex = pd.read_sql(
                'SELECT match_id, handicap, giver, home_odds, away_odds '
                'FROM odds_asian WHERE label=? AND company_id=12', conn,
                params=(label,))
            ex = ex.rename(columns={'match_id': 'id', 'handicap': f'{pfx}_h',
                                    'giver': f'{pfx}_g', 'home_odds': f'{pfx}_ho',
                                    'away_odds': f'{pfx}_ao'})
            df = df.merge(ex, on='id', how='left')
        df['recent'] = (df['kickoff'] >= RECENT2Y).to_numpy()
        # 12 段上盤水位
        w = df['up_water'].to_numpy(dtype=float)
        z12 = np.full(len(df), -1, dtype=np.int8)
        ok = ~np.isnan(w)
        z12[ok & (w < 0.65)] = 0
        z12[ok & (w >= 1.15)] = 11
        midband = ok & (w >= 0.65) & (w < 1.15)
        z12[midband] = ((w[midband] - 0.65) * 20).astype(np.int64) + 1
        df['up_zone12'] = z12
        _v2_pool['df'] = df
        _v2_pool['ts'] = time.time()
        print(f'[v2_pool] {len(df)} 場', flush=True)
        return df


def invalidate_pool():
    _v2_pool['ts'] = 0
    _v2_pool['df'] = None


# ---------- 基本工具（numpy） ----------

def _res_code(res):
    ri = np.zeros(len(res), dtype=np.int8)
    ri[res == 'B'] = 1
    ri[res == 'P'] = 2
    return ri


def _oc(ri):
    n = len(ri)
    if n == 0:
        return None
    a = int(np.count_nonzero(ri == 0))
    b = int(np.count_nonzero(ri == 1))
    p = int(np.count_nonzero(ri == 2))
    eff = n - p
    return {'n': n, 'up': a, 'down': b, 'push': p,
            'up_r': a / eff if eff else None, 'down_r': b / eff if eff else None,
            'push_r': p / n}


def _oc_mask(df, m):
    if m is None:
        return None
    return _oc(_res_code(df['res'].to_numpy()[np.nonzero(m)[0]]))


def _n_mask(m):
    return 0 if m is None else int(np.count_nonzero(m))


def _signed_vec(h, g):
    """h/g 陣列 → 帶符號讓球（主讓+, 客讓-, 平手/冇資料 0/NaN）"""
    h = np.asarray(h, dtype=float)
    out = np.where(np.isnan(h), np.nan, np.where(h == 0, 0.0, h))
    g = np.asarray(g, dtype=object)
    away = np.zeros(len(h), dtype=bool)
    away[g == 'away'] = True
    out = np.where(away & ~np.isnan(out), -out, out)
    return out


def _signed_line(h, g):
    if h is None or (isinstance(h, float) and np.isnan(h)) or h == 0 or not g:
        return 0.0
    return h if g == 'home' else -h


def state_of(ref_signed, cur_signed):
    """深/淺/不變（讓球方角度：今場比參照盤讓得多＝深）"""
    d = cur_signed - ref_signed
    if abs(d) < 1e-9:
        return 'same'
    return 'deep' if d > 0 else 'shallow'


STATE_TXT = {'deep': '深', 'shallow': '淺', 'same': '不變'}


def _line_only_eq(df, pfx, T):
    """純盤口相同（讓球數+讓球方），唔計水位"""
    if T is None or T.get('h') is None:
        return None
    return (df[f'{pfx}_h'] == T['h']) & \
           (df[f'{pfx}_g'].fillna('none') == (T.get('g') or 'none'))


def _line_water_eq(df, pfx, T, tol=0.03):
    """盤口 100% 相同＋主客水位各 ±tol"""
    m = _line_only_eq(df, pfx, T)
    if m is None:
        return None
    return m & ((df[f'{pfx}_ho'] - T['ho']).abs() <= tol) & \
               ((df[f'{pfx}_ao'] - T['ao']).abs() <= tol)


def _up_water_of(T):
    """目標線嘅上盤水位（讓球方水位；平手取低水方）——同 V1 語義"""
    if T is None:
        return None
    g = T.get('g')
    if g == 'home':
        return T.get('ho')
    if g == 'away':
        return T.get('ao')
    ho, ao = T.get('ho'), T.get('ao')
    if ho is not None and ao is not None:
        return min(ho, ao)
    return None


def _scope_masks(df, lg):
    cat = df['cat'].to_numpy()
    rec = df['recent'].to_numpy()
    is_lg = cat == '聯賽'
    is_cup = cat == '杯賽'
    lg_m = (df['league'] == lg) if lg else np.zeros(len(df), dtype=bool)
    return {
        '全庫': np.ones(len(df), dtype=bool),
        '聯賽類': is_lg,
        '杯賽類': is_cup,
        '近兩年': rec,
        '近兩年聯賽類': rec & is_lg,
        '近兩年杯賽類': rec & is_cup,
        '同一聯賽': lg_m,
        '近兩年同一聯賽': rec & lg_m,
    }


def _cells16(df, base, t):
    """16 格 A–P：8 範圍 × [混合, 同主隊]；base=None → 全部唔適用"""
    scopes = _scope_masks(df, t.get('league'))
    home_id = t.get('home_id')
    same_team = (df['home_id'] == home_id) if home_id else np.zeros(len(df), dtype=bool)
    res_np = df['res'].to_numpy()
    cells = []
    for mix in MIX2:
        for sc in SCOPES8:
            letter = LETTERS[len(cells)]
            if base is None:
                cells.append({'letter': letter, 'scope': sc, 'mix': mix,
                              'error': '不適用'})
                continue
            m = base & scopes[sc]
            if mix == '同主隊':
                m &= same_team
            oc = _oc(_res_code(res_np[np.nonzero(m)[0]]))
            c = {'letter': letter, 'scope': sc, 'mix': mix}
            c.update(oc or {'n': 0, 'up': 0, 'down': 0, 'push': 0,
                            'up_r': None, 'down_r': None, 'push_r': None})
            cells.append(c)
    return cells


def _water12(df, m):
    """12 段水位表：每段 場數＋上/下/走"""
    if m is None:
        return {'n': 0, 'zones': []}
    idx = np.nonzero(m)[0]
    if len(idx) == 0:
        return {'n': 0, 'zones': []}
    z = df['up_zone12'].to_numpy()[idx]
    ri = _res_code(df['res'].to_numpy()[idx])
    ok = z >= 0
    zc = np.bincount(ri[ok] + 3 * z[ok], minlength=36).reshape(12, 3) \
        if ok.any() else np.zeros((12, 3), dtype=np.int64)
    zones = []
    for zi in range(12):
        a, b, p = (int(x) for x in zc[zi])
        if a + b + p == 0:
            zones.append({'zone': ZONES12[zi], 'n': 0, 'up': 0, 'down': 0,
                          'push': 0, 'up_r': None, 'down_r': None, 'push_r': None})
            continue
        n = a + b + p
        eff = n - p
        zones.append({'zone': ZONES12[zi], 'n': n, 'up': a, 'down': b, 'push': p,
                      'up_r': a / eff if eff else None,
                      'down_r': b / eff if eff else None,
                      'push_r': p / n})
    return {'n': int(len(idx)), 'zones': zones}


def _dist_lines(df, m, cur_zone12=None):
    """盤口分佈：每個(讓球,讓球方) 場數＋上/下/走＋12 段水位；列＝盤口（同今場尾盤嗰列黃底）"""
    if m is None:
        return []
    idx = np.nonzero(m)[0]
    if len(idx) == 0:
        return []
    hr = df['c_h'].to_numpy(dtype=float)[idx]
    ok = ~np.isnan(hr)
    idx = idx[ok]
    if len(idx) == 0:
        return []
    hr2 = np.round(hr[ok], 2)
    g = df['c_g'].fillna('none').to_numpy()[idx]
    gu, ginv = np.unique(g, return_inverse=True)
    glut = np.array([{'away': 0, 'home': 1, 'none': 2}.get(x, 3)
                     for x in gu], dtype=np.int64)
    key = (hr2 * 100).astype(np.int64) * 10 + glut[ginv]
    ri = _res_code(df['res'].to_numpy()[idx])
    z = df['up_zone12'].to_numpy()[idx]
    srt = np.argsort(key, kind='stable')
    ks = key[srt]
    bounds = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1], True])
    cur_line = None
    rows = []
    for b0, b1 in zip(bounds[:-1], bounds[1:]):
        gi = srt[b0:b1]
        n = len(gi)
        gidx = gi[z[gi] >= 0]
        zc = np.bincount(ri[gidx] + 3 * z[gidx], minlength=36).reshape(12, 3) \
            if len(gidx) else np.zeros((12, 3), dtype=np.int64)
        a, b_, p = (int(x) for x in zc.sum(axis=0))
        eff = n - p
        zones = []
        for zi in range(12):
            za, zb, zp = (int(x) for x in zc[zi])
            if za + zb + zp == 0:
                zones.append(None)
                continue
            zn = za + zb + zp
            zeff = zn - zp
            zones.append({'n': zn, 'up': za, 'down': zb, 'push': zp,
                          'up_r': za / zeff if zeff else None,
                          'down_r': zb / zeff if zeff else None,
                          'push_r': zp / zn})
        k = ks[b0]
        h = (k // 10) / 100.0
        gg = ('away', 'home', 'none')[k % 10]
        line_txt = se.fmt_line(h, None if gg == 'none' else gg)
        is_cur = cur_zone12 is not None and abs(h - (cur_zone12[0] or 0)) < 1e-9 \
            and gg == (cur_zone12[1] or 'none')
        rows.append({'line': line_txt, 'h': h, 'g': gg, 'n': n,
                     'up': a, 'down': b_, 'push': p,
                     'up_r': a / eff if eff else None,
                     'down_r': b_ / eff if eff else None,
                     'push_r': p / n,
                     'zones': zones, 'is_cur': bool(is_cur)})
    rows.sort(key=lambda r: -r['n'])
    return rows


# ---------- 各項目輸出 ----------

def _form_mask(df, t, home_sc, away_sc, tol=0.07):
    """勝和負比例類似（每項±tol）"""
    pre = t['pre']
    m = (df[f'home_{home_sc}_p'] > 0) & (df[f'away_{away_sc}_p'] > 0)
    for side, sc in (('home', home_sc), ('away', away_sc)):
        base = f'{side}_{sc}'
        if not pre.get(f'{base}_p'):
            return None
        for f_ in ('wp', 'dp', 'lp'):
            m = m & ((df[f'{base}_{f_}'] - pre[f'{base}_{f_}']).abs() <= tol)
    return m


def _goal_mask(df, t, home_sc, away_sc, tol=3):
    """入球/失球/得失球差 類似（各±tol）"""
    pre = t['pre']
    m = (df[f'home_{home_sc}_p'] > 0) & (df[f'away_{away_sc}_p'] > 0)
    for side, sc in (('home', home_sc), ('away', away_sc)):
        base = f'{side}_{sc}'
        if not pre.get(f'{base}_p'):
            return None
        m = m & ((df[f'{base}_gf'] - pre[f'{base}_gf']).abs() <= tol)
        m = m & ((df[f'{base}_ga'] - pre[f'{base}_ga']).abs() <= tol)
        m = m & ((df[f'{base}_gd'] - pre[f'{base}_gd']).abs() <= tol)
    return m


def _rank_mask(df, t, home_rank, away_rank, tol=2):
    """排名類似（各±tol）"""
    pre = t['pre']
    if not pre.get(home_rank) or not pre.get(away_rank):
        return None
    return ((df[home_rank] - pre[home_rank]).abs() <= tol) & \
           ((df[away_rank] - pre[away_rank]).abs() <= tol)


def build_context(conn, t):
    """預備目標賽事嘅全部樣本 mask（45 項共用）"""
    df = load_v2_pool(conn)
    odds = t.get('odds') or {}
    T_close = se.tline(odds, 'closing')
    ctx = {'df': df, 't': t, 'T_close': T_close,
           'cur_zone12': zone12_of(_up_water_of(T_close)),
           'cur_line_key': (T_close['h'], T_close.get('g') or 'none')
                           if T_close and T_close.get('h') is not None else None}
    same_close = _line_only_eq(df, 'c', T_close) if T_close else None
    ctx['same_close'] = same_close

    # 時點組 1–6
    tp = {}
    for no, label, pfx, name in TP6:
        T = se.tline(odds, label)
        if T is None or T_close is None:
            tp[no] = (T, pfx, name, None)
        else:
            tp[no] = (T, pfx, name,
                      _line_water_eq(df, pfx, T) & _line_water_eq(df, 'c', T_close))
    ctx['tp'] = tp
    # H2H 七時點 7–13（純盤口）／14–20（水位版）
    h2h = {}
    for i, (no, label, pfx, name) in enumerate(TP7):
        vno = str(7 + i)        # 7..13
        wno = str(14 + i)       # 14..20
        T = se.tline(odds, label)
        ml = _line_only_eq(df, 'pvN', T) if T else None
        mw = _line_water_eq(df, 'pvN', T) if T else None
        h2h[vno] = (T, name, ml)
        h2h[wno] = (T, name, mw)
    ctx['h2h'] = h2h

    f6 = _form_mask(df, t, 'home', 'away')
    f7 = _form_mask(df, t, 'total', 'total')
    g8 = _goal_mask(df, t, 'home', 'away')
    g9 = _goal_mask(df, t, 'total', 'total')
    m12 = _rank_mask(df, t, 'home_home_rank', 'away_away_rank')
    m13 = _rank_mask(df, t, 'home_total_rank', 'away_total_rank')
    def _and(a, b):
        if a is None or b is None:
            return None
        return a & b
    ctx['form'] = {
        '21': _and(f6, same_close), '22': _and(f6, same_close),
        '23': _and(f7, same_close), '24': _and(f7, same_close),
        '25': _and(g8, same_close), '26': _and(g8, same_close),
        '27': _and(m12, same_close), '28': _and(m13, same_close),
        '34': _and(g8, same_close), '35': _and(g9, same_close),
        'f6': f6, 'f7': f7, 'g8': g8, 'g9': g9, 'm12': m12, 'm13': m13,
    }
    # 29–33：上次比賽完全相同
    hp = t.get('home_prev')
    m16 = None
    if hp is not None and hp.get('h') is not None:
        m16 = (df['hp_role'] == hp['role']) & \
              (df['hp_g'].fillna('none') == (hp.get('g') or 'none')) & \
              (df['hp_h'] == hp['h'])
    ctx['m16'] = m16
    # 32：排名差距淨值 ±1（主場−客場）＋同尾盤
    if pre_get(t, 'home_home_rank') and pre_get(t, 'away_away_rank'):
        d = t['pre']['home_home_rank'] - t['pre']['away_away_rank']
        m32 = ((df['home_home_rank'] - df['away_away_rank']) - d).abs() <= 1
        ctx['m32'] = _and(m32, same_close)
        ctx['d32'] = d
    else:
        ctx['m32'], ctx['d32'] = None, None
    return ctx


def pre_get(t, k):
    return (t.get('pre') or {}).get(k)


# ---------- 各項目輸出 ----------

def item_timegroup(ctx, no):
    """項目 1–6：時點組 16 格＋12 段水位表"""
    df, t = ctx['df'], ctx['t']
    T, pfx, name, base = ctx['tp'][no]
    Tc = ctx['T_close']
    if T is None:
        return {'no': no, 'title': f'同{name}及尾盤的盤口&水位',
                'ref': f'今場缺少{name}數據', 'error': '不適用'}
    if Tc is None:
        return {'no': no, 'title': f'同{name}及尾盤的盤口&水位',
                'ref': '今場缺少尾盤數據', 'error': '不適用'}
    ref = (f"今場{name}：{se.fmt_line(T['h'], T['g'])} 主{T['ho']}/客{T['ao']}　"
           f"今場尾盤：{se.fmt_line(Tc['h'], Tc['g'])} 主{Tc['ho']}/客{Tc['ao']}　"
           '歷史場次兩個時點都同今場 100% 相同（水位±0.03）')
    scopes = _scope_masks(df, t.get('league'))
    cells = _cells16(df, base, t)
    m_lg = base & scopes['同一聯賽'] if base is not None else None
    return {'no': no, 'title': f'同{name}及尾盤的盤口&水位（16格＋12段水位表）',
            'ref': ref, 'cells': cells,
            'water': {'all': _water12(df, base), 'league': _water12(df, m_lg)},
            'cur_zone12': ctx['cur_zone12'], 'zones12': ZONES12,
            'mix_note': '混合＝全部樣本；同主隊＝樣本場次嘅主隊同今場主隊係同一隊'}


def item_h2h_line(ctx, no):
    """項目 7–13：H2H 純盤口（三合一）"""
    df = ctx['df']
    T, name, m = ctx['h2h'][no]
    if m is None:
        return {'no': no, 'title': f'對上一次對賽尾盤 對 今場{name}（純盤口）',
                'ref': f'今場缺少{name}數據', 'error': '不適用'}
    pv_same = df['pv_same'].to_numpy()
    has_pv = df['pv_h'].notna().to_numpy()
    res_np = df['res'].to_numpy()
    def oc(mask):
        return _oc(_res_code(res_np[np.nonzero(mask)[0]]))
    m_same = m & pv_same
    m_swap = m & has_pv & ~pv_same
    ref = (f'今場{name}：{se.fmt_line(T["h"], T["g"])}　'
           '上賽尾盤已做主客互換對比；純盤口＝只比較讓球數+讓球方')
    return {'no': no, 'title': f'對上一次對賽尾盤 對 今場{name}（純盤口，三合一）',
            'ref': ref,
            'tri': {'same': oc(m_same), 'swap': oc(m_swap), 'all': oc(m)}}


def item_h2h_water(ctx, no):
    """項目 14–20：H2H 水位版（三合一 × 全庫/聯賽類/同一聯賽＝9 格，每格 12 段水位表）"""
    df, t = ctx['df'], ctx['t']
    T, name, m = ctx['h2h'][no]
    if m is None:
        return {'no': no, 'title': f'對上一次對賽尾盤 對 今場{name}（水位版）',
                'ref': f'今場缺少{name}數據', 'error': '不適用'}
    pv_same = df['pv_same'].to_numpy()
    has_pv = df['pv_h'].notna().to_numpy()
    scopes = _scope_masks(df, t.get('league'))
    cells = []
    for tri, tm in (('同主客場', m & pv_same), ('對調', m & has_pv & ~pv_same),
                    ('綜合', m)):
        for sc in ('全庫', '聯賽類', '同一聯賽'):
            mm = tm & scopes[sc]
            w = _water12(df, mm)
            cells.append({'tri': tri, 'scope': sc, 'n': w['n'], 'zones': w['zones']})
    return {'no': no, 'title': f'對上一次對賽尾盤 對 今場{name}（水位版，9格）',
            'ref': (f'今場{name}：{se.fmt_line(T["h"], T["g"])} 主{T["ho"]}/客{T["ao"]}　'
                    '盤口100%一樣＋水位±0.03（上賽尾盤互換對比）'),
            'cells': cells, 'cur_zone12': ctx['cur_zone12'], 'zones12': ZONES12}


def item_form_goal_rank(ctx, no):
    """項目 21–28／34–35：條件項（盤口版=分佈／水位版=12段水位）"""
    df, t = ctx['df'], ctx['t']
    fm = ctx['form']
    Tc = ctx['T_close']
    line_txt = se.fmt_line(Tc['h'], Tc['g']) if Tc else '—'
    spec = {
        '21': ('主隊主場勝和負比例 及 客隊客場（每項±7%）→ 盤口分佈', 'dist'),
        '22': ('主隊主場勝和負比例 及 客隊客場（每項±7%）→ 12段水位', 'water'),
        '23': ('主隊總和勝和負比例 及 客隊總和（每項±7%）→ 盤口分佈', 'dist'),
        '24': ('主隊總和勝和負比例 及 客隊總和（每項±7%）→ 12段水位', 'water'),
        '25': ('主隊主場入球/失球/得失球差 及 客隊客場（各±3）→ 盤口分佈', 'dist'),
        '26': ('主隊主場入球/失球/得失球差 及 客隊客場（各±3）→ 12段水位', 'water'),
        '27': ('主隊主場排名 及 客隊客場排名（各±2）→ 分佈＋水位', 'both'),
        '28': ('主隊總排名 及 客隊總排名（各±2）→ 分佈＋水位', 'both'),
        '34': ('主隊主場入球/失球/得失球差 及 客隊客場（各±3）→ 分佈＋水位', 'both'),
        '35': ('主隊總和入球/失球/得失球差 及 客隊總和（各±3）→ 分佈＋水位', 'both'),
    }
    title, kind = spec[no]
    base = fm.get(no)
    if base is None:
        return {'no': no, 'title': title,
                'ref': '目標賽事數據不足（未有開賽前狀態或尾盤）', 'error': '不適用'}
    ref = (title.split('→')[0] + f'→ 再加條件：尾盤同今場 100% 相同（{line_txt}）')
    out = {'no': no, 'title': title, 'ref': ref,
           'oc': _oc_mask(df, base), 'cur_zone12': ctx['cur_zone12'],
           'zones12': ZONES12}
    if kind in ('dist', 'both'):
        out['dist'] = _dist_lines(
            df, base,
            cur_zone12=(Tc['h'], Tc.get('g') or 'none') if Tc else None)
    if kind in ('water', 'both'):
        out['water'] = {'all': _water12(df, base)}
    return out


def item29(ctx):
    df = ctx['df']
    if ctx['m16'] is None:
        return {'no': '29', 'title': '上次比賽完全相同 → 盤口分佈＋各盤口水位',
                'ref': '今次主隊未有對上一次比賽尾盤數據', 'error': '不適用'}
    return {'no': '29',
            'title': '上次比賽完全相同（角色＋讓/受讓＋讓球數）→ 盤口分佈＋各盤口水位',
            'ref': ctx['t'].get('home', '') + ' 對上一次比賽同樣角色、同樣讓/受讓、同樣讓球數',
            'oc': _oc_mask(df, ctx['m16']),
            'dist': _dist_lines(df, ctx['m16'],
                                cur_zone12=ctx['cur_line_key']),
            'cur_zone12': ctx['cur_zone12'], 'zones12': ZONES12}


def item30_33(ctx, no):
    df, Tc = ctx['df'], ctx['T_close']
    if ctx['m16'] is None:
        return {'no': no, 'title': '上次比賽樣本分析', 'ref': '今次主隊未有上次比賽數據',
                'error': '不適用'}
    mode, d50 = _mode_d50_lines(df, ctx['m16'], Tc)
    if no == '30':
        return {'no': '30', 'title': '上次比賽樣本：分佈最多盤口 與今場尾盤淨差距',
                'ref': '參照盤＝上次比賽完全相同嘅歷史場次嘅分佈最多盤口', 'mode': mode}
    return {'no': '33', 'title': '上次比賽樣本：上盤勝率最接近50%盤口 與今場尾盤淨差距',
            'ref': '參照盤＝上次比賽完全相同嘅歷史場次嘅最接近50%盤口（至少5場）',
            'd50': d50}


def item31(ctx):
    """今賽比上賽 深/淺/不變（互換對比版＋同主客原盤版）"""
    t, Tc = ctx['t'], ctx['T_close']
    pv = t.get('prev_h2h')
    if not pv or pv[0] is None or Tc is None:
        return {'no': '31', 'title': '今賽比上賽 深/淺/不變',
                'ref': '今場缺少尾盤或雙方無對賽記錄', 'error': '不適用'}
    pvc = t.get('prev_conv')
    cur_s = _signed_line(Tc['h'], Tc.get('g'))
    raw = (pv[0], pv[1])
    same_role = pvc.get('same') if pvc else None
    out = {'no': '31', 'title': '今賽比上賽 深/淺/不變（Check 下先參照）',
           'ref': (f'今場尾盤：{se.fmt_line(Tc["h"], Tc["g"])}　'
                   f'上賽尾盤：{se.fmt_line(raw[0], raw[1])}')}
    if pvc and pvc.get('ok'):
        conv_s = _signed_line(pvc['h'], pvc.get('g'))
        out['conv'] = {'state': state_of(conv_s, cur_s),
                       'state_txt': STATE_TXT[state_of(conv_s, cur_s)],
                       'ref_line': se.fmt_line(pvc['h'], pvc.get('g')),
                       'note': '互換對比盤（上賽作客已轉換主隊角度）'}
    else:
        out['conv'] = None
    if same_role:
        raw_s = _signed_line(raw[0], raw[1])
        st = state_of(raw_s, cur_s)
        out['raw'] = {'state': st, 'state_txt': STATE_TXT[st],
                      'ref_line': se.fmt_line(raw[0], raw[1]),
                      'note': '同主客原盤（唔轉換）'}
    else:
        out['raw'] = None
    return out


def item32(ctx):
    df = ctx['df']
    if ctx['m32'] is None:
        return {'no': '32', 'title': '排名差距淨值（主場−客場）±1 → 分佈＋水位',
                'ref': '目標賽事未有排名數據或尾盤', 'error': '不適用'}
    Tc = ctx['T_close']
    return {'no': '32',
            'title': '主隊主場排名 − 客隊客場排名 差距淨值（±1）＋同今場尾盤 → 分佈＋水位',
            'ref': f'差距 {ctx["d32"]:+d}（±1）',
            'oc': _oc_mask(df, ctx['m32']),
            'dist': _dist_lines(df, ctx['m32'],
                                cur_zone12=(Tc['h'], Tc.get('g') or 'none')
                                if Tc else None),
            'water': {'all': _water12(df, ctx['m32'])},
            'cur_zone12': ctx['cur_zone12'], 'zones12': ZONES12}


# ---------- 首 14 分鐘（36–45） ----------

_first14_cache = {'ts': 0, 'map': {}}


def _first14_map(conn, max_age=1800):
    now = time.time()
    if _first14_cache['map'] and now - _first14_cache['ts'] < max_age:
        return _first14_cache['map']
    m = {}
    try:
        for mid, side in conn.execute(
                "SELECT match_id, side FROM match_goals "
                "WHERE minute <= 14 AND side IN ('home','away')"):
            e = m.setdefault(int(mid), [False, False])
            if side == 'home':
                e[0] = True
            else:
                e[1] = True
    except Exception:
        pass
    _first14_cache['map'] = m
    _first14_cache['ts'] = now
    return m


def _first14_stats(df, m, f14):
    """樣本嘅首14分鐘：主入/客入/冇入 場數＋%"""
    if m is None:
        return None
    ids = df['id'].to_numpy()
    idx = np.nonzero(m)[0]
    n = nh = na = 0
    for i in idx:
        e = f14.get(int(ids[i]))
        if e is None:
            continue
        n += 1
        if e[0]:
            nh += 1
        if e[1]:
            na += 1
    if n == 0:
        return {'n': 0, 'home': 0, 'away': 0, 'none': 0,
                'home_r': None, 'away_r': None, 'none_r': None}
    nn = n - nh - na
    return {'n': n, 'home': nh, 'away': na, 'none': nn,
            'home_r': nh / n, 'away_r': na / n, 'none_r': nn / n}


def item_first14(conn, ctx, no):
    df, t = ctx['df'], ctx['t']
    f14 = _first14_map(conn)
    total = len(f14)
    if no in tuple(x[0] for x in TP6):
        _, _, name, base = ctx['tp'][no]
        if base is None:
            return {'no': no, 'title': f'首14分鐘入球%（同{name}及尾盤時點組）',
                    'error': '不適用'}
        cells = []
        scopes = _scope_masks(df, t.get('league'))
        home_id = t.get('home_id')
        same_team = (df['home_id'] == home_id) if home_id \
            else np.zeros(len(df), dtype=bool)
        for mix in MIX2:
            for sc in SCOPES8:
                letter = LETTERS[len(cells)]
                m = base & scopes[sc]
                if mix == '同主隊':
                    m &= same_team
                st = _first14_stats(df, m, f14)
                st.update({'letter': letter, 'scope': sc, 'mix': mix})
                cells.append(st)
        return {'no': no, 'title': f'首14分鐘入球%：主入/客入/冇入（同{name}及尾盤，16格）',
                'cells': cells, 'data_n': total,
                'data_note': None if total >= 1000 else
                f'分鐘級入球數據爬取中（暫得 {total} 場有首14分鐘記錄）'}
    single = {
        '42': ('排名差距淨值（主場−客場）±1', ctx.get('m32')),
        '43': ('主隊總排名−客隊總排名 差距淨值（±1）＋同尾盤',
               _and_rank_gap_total(ctx)),
        '44': ('主隊主場入球/失球/差 及 客隊客場（各±3）＋同尾盤',
               ctx['form'].get('34')),
        '45': ('主隊總和入球/失球/差 及 客隊總和（各±3）＋同尾盤',
               ctx['form'].get('35')),
    }
    title, m = single[no]
    return {'no': no, 'title': f'首14分鐘入球%（{title}）',
            'stats': _first14_stats(df, m, f14), 'data_n': total,
            'data_note': None if total >= 1000 else
            f'分鐘級入球數據爬取中（暫得 {total} 場有首14分鐘記錄）'}


def _and_rank_gap_total(ctx):
    df, t = ctx['df'], ctx['t']
    if not pre_get(t, 'home_total_rank') or not pre_get(t, 'away_total_rank'):
        return None
    d = t['pre']['home_total_rank'] - t['pre']['away_total_rank']
    m = ((df['home_total_rank'] - df['away_total_rank']) - d).abs() <= 1
    if ctx['same_close'] is None:
        return None
    return m & ctx['same_close']


# ---------- 統一入口 ----------

def compute_item(conn, t, no):
    """no：'1'..'35'、'36'..'45'（36–41 對應六時點組編號 1–6 嘅首14分鐘版）"""
    n = int(no)
    ctx = build_context(conn, t)
    if no in tuple(x[0] for x in TP6):
        return item_timegroup(ctx, no)
    if 7 <= n <= 13:
        return item_h2h_line(ctx, no)
    if 14 <= n <= 20:
        return item_h2h_water(ctx, no)
    if no in ('21', '22', '23', '24', '25', '26', '27', '28', '34', '35'):
        return item_form_goal_rank(ctx, no)
    if no == '29':
        return item29(ctx)
    if no == '30':
        return item30_33(ctx, '30')
    if no == '31':
        return item31(ctx)
    if no == '32':
        return item32(ctx)
    if no == '33':
        return item30_33(ctx, '33')
    if no in ('36', '37', '38', '39', '40', '41'):
        return item_first14(conn, ctx, str(int(no) - 35))
    if no in ('42', '43', '44', '45'):
        return item_first14(conn, ctx, no)
    return {'error': f'未知項目 {no}'}


def item_applicability(conn, t):
    """邊項適用（前端未展開前顯示用；平，唔計重嘢）"""
    ctx = build_context(conn, t)
    ok = {}
    for no, label, pfx, name in TP6:
        T = se.tline((t.get('odds') or {}), label)
        ok[no] = bool(T is not None and ctx['T_close'] is not None)
    for i in range(7):
        vno, wno = str(7 + i), str(14 + i)
        T = ctx['h2h'][vno][0]
        ok[vno] = bool(T is not None and ctx['df']['pvN_h'].notna().any())
        ok[wno] = ok[vno]
    for no in ('21', '22', '23', '24', '25', '26', '27', '28', '34', '35'):
        ok[no] = bool(ctx['form'].get(no) is not None)
    ok['29'] = ok['30'] = ok['33'] = bool(ctx['m16'] is not None)
    ok['31'] = bool(t.get('prev_h2h') is not None and ctx['T_close'] is not None)
    ok['32'] = bool(ctx['m32'] is not None)
    for no in ('36', '37', '38', '39', '40', '41'):
        ok[no] = ok[str(int(no) - 35)]
    ok['42'] = bool(ctx.get('m32') is not None)
    ok['43'] = bool(_and_rank_gap_total(ctx) is not None)
    ok['44'] = bool(ctx['form'].get('34') is not None)
    ok['45'] = bool(ctx['form'].get('35') is not None)
    return ok


# ---------- 組合篩查（第 1–35 項多選） ----------

def combo(conn, t, sel):
    sel = [str(s).strip() for s in sel if str(s).strip()]
    bad = [s for s in sel if s in ('30', '31', '33')]
    if bad:
        return {'error': '第30、31、33項係分析項（淨差距／深淺），無篩選條件，不能剔選'}
    if len(sel) > 12:
        return {'error': '最多同時剔選 12 項（計算量控制）'}
    ctx = build_context(conn, t)
    df, lg, cat = ctx['df'], t.get('league'), t.get('category')
    masks = {}
    for no, label, pfx, name in TP6:
        masks[no] = ctx['tp'][no][3]
    for i in range(7):
        masks[str(7 + i)] = ctx['h2h'][str(7 + i)][2]
        masks[str(14 + i)] = ctx['h2h'][str(14 + i)][2]
    for no in ('21', '22', '23', '24', '25', '26', '27', '28', '34', '35'):
        masks[no] = ctx['form'].get(no)
    masks['29'] = ctx['m16']
    masks['32'] = ctx['m32']
    none_items = [s for s in sel if s not in masks or masks.get(s) is None]
    if none_items:
        return {'error': f'剔選咗而家不適用嘅項目：第{"、".join(none_items)}項（目標賽事數據不足）'}
    m = masks[sel[0]].copy()
    for s in sel[1:]:
        m &= masks[s]
    scopes = _scope_masks(df, lg)
    return {'sel': sel,
            'all': _oc_mask(df, m),
            'league': _oc_mask(df, m & scopes['同一聯賽']) if lg else None,
            'cat': _oc_mask(df, m & (df['cat'] == cat)) if cat else None}


# ---------- 精選 16 字頭（1X & 13X 同向 + 30 + 33） ----------

def _cell_dir(c):
    """格嘅方向：上盤率>49.99%→'up'；下盤率>49.99%→'down'；否則 None"""
    ur, dr = c.get('up_r'), c.get('down_r')
    if ur is not None and ur > 0.4999:
        return 'up'
    if dr is not None and dr > 0.4999:
        return 'down'
    return None


def featured_letters(conn, t):
    """16 字頭 A–P。每字頭 X：
    ① 2X（時點組@開賽前4小時 格X）有方向；② 13X（H2H純盤口@尾盤 格X）同方向；
    ③ 上次比賽樣本中【同30最多盤口】嘅場次，同方向率>49.99%；
    ④ 同上【同33最接近50%盤】>49.99%。
    ①②兩項各自獨立展示贏盤率，唔夾埋。四項全過＝字頭合格；任一字頭合格＝入選。
    回傳 {'direction', 'letters', 'states'}"""
    ctx = build_context(conn, t)
    df = ctx['df']
    if ctx['tp']['2'][3] is None:
        return None
    cells2 = _cells16(df, ctx['tp']['2'][3], t)
    base13 = ctx['h2h']['13'][2]      # 項目 13＝H2H 純盤口@尾盤
    cells13 = _cells16(df, base13, t) if base13 is not None else None
    mode, d50 = _mode_d50_lines(df, ctx['m16'], ctx['T_close']) \
        if ctx['m16'] is not None else (None, None)

    def line_rate(line_pack, d):
        """歷史場次尾盤==line_pack 嘅盤 → d 方向率"""
        if line_pack is None:
            return None
        m = _line_only_eq(df, 'c', {'h': line_pack['h_g'], 'g': line_pack['g_g']})
        # _line_only_eq needs h/g keys
        oc = _oc_mask(df, m)
        if not oc or oc['n'] == 0:
            return None
        r = (oc['up_r'] if d == 'up' else oc['down_r'])
        return {'n': oc['n'], 'r': r}

    # line_pack 帶 h/g 原始值（pack 時存入）
    letters = []
    for i, c2 in enumerate(cells2):
        letter = LETTERS[i]
        d = _cell_dir(c2)
        entry = {'letter': letter, 'scope': c2['scope'], 'mix': c2['mix'],
                 'dir': d, 'pass': False}
        if d is None:
            letters.append(entry)
            continue
        # ①項目2 自己嘅方向率（獨立展示，唔同②夾埋）
        entry['r2'] = c2.get('up_r') if d == 'up' else c2.get('down_r')
        entry['n2'] = c2.get('n')
        if cells13 is None:
            letters.append(entry)
            continue
        c13 = cells13[i]
        d13 = _cell_dir(c13)
        if d13 != d:
            letters.append(entry)
            continue
        # ②項目13 同方向率（獨立展示）
        entry['d13_r'] = c13.get('up_r') if d == 'up' else c13.get('down_r')
        entry['n13'] = c13.get('n')
        r30 = line_rate(mode, d) if mode else None
        r33 = line_rate(d50, d) if d50 else None
        entry['r30'] = r30
        entry['r33'] = r33
        ok30 = r30 is not None and r30['r'] is not None and r30['r'] > 0.4999
        ok33 = r33 is not None and r33['r'] is not None and r33['r'] > 0.4999
        entry['pass'] = bool(ok30 and ok33)
        letters.append(entry)
    passed = [e for e in letters if e['pass']]
    states = None
    if ctx['m16'] is not None or t.get('prev_h2h'):
        i31 = item31(ctx)
        states = {'conv': (i31.get('conv') or {}).get('state'),
                  'raw': (i31.get('raw') or {}).get('state')}
    if not passed:
        return {'direction': None, 'letters': letters, 'states': states}
    # 方向＝第一個合格字頭嘅方向（A→P 順序）
    return {'direction': passed[0]['dir'], 'letters': letters, 'states': states,
            'passed_letters': [e['letter'] for e in passed]}


def _mode_d50_lines(df, m, T_close):
    """分佈最多盤口 及 上盤勝率最接近 50% 盤口（≥5 場）＋同今場尾盤淨差距（8 條規則）。
    pack 帶埋原始 h/g（h_g/g_g）方便後續按線再篩選"""
    rows = _dist_lines(df, m)
    if not rows:
        return None, None
    mode = max(rows, key=lambda r: r['n'])
    d50 = None
    for r in rows:
        if r['n'] >= 5 and r['up_r'] is not None:
            d = abs(r['up_r'] - 0.5)
            if d50 is None or d < d50[0]:
                d50 = (d, r)

    def pack(r):
        if r is None:
            return None
        gap = se.gap_text(r['h'], None if r['g'] == 'none' else r['g'], T_close)
        if isinstance(gap, dict):
            gap = gap.get('text')          # 前端直接顯示字串，唔好傳物件
        return {'line': r['line'], 'n': r['n'],
                'up_r': r['up_r'], 'down_r': r['down_r'],
                'push': r['push'], 'push_r': r['push_r'],
                'gap': gap, 'h_g': r['h'], 'g_g': r['g']}
    return pack(mode), (pack(d50[1]) if d50 else None)


# ---------- Check 下先（2 行 × 3×3） ----------

def _state_grid(df, pool_mask, state_a, state_b, res_np):
    """3×3 格：state_a/state_b ∈ 'deep'/'shallow'/'same'"""
    grid = {}
    names = ['deep', 'shallow', 'same']
    idx = np.nonzero(pool_mask)[0]
    ri = _res_code(res_np[idx])
    sa = state_a[idx]
    sb = state_b[idx]
    for a in names:
        for b in names:
            mm = (sa == a) & (sb == b)
            grid[f'{a}|{b}'] = _oc(ri[mm]) or {'n': 0, 'up': 0, 'down': 0,
                                               'push': 0, 'up_r': None,
                                               'down_r': None, 'push_r': None}
    # 軸上的 1D 分佈（對角以外都有用）
    marg = {}
    for a in names:
        marg[a] = _oc(ri[sa == a]) or {'n': 0}
    for b in names:
        marg[f'b_{b}'] = _oc(ri[sb == b]) or {'n': 0}
    return {'grid': grid, 'marginal': marg, 'n': int(len(idx))}


def check_grid(conn, t=None):
    """Check 下先：行1＝只計上賽同主客方向；行2＝計埋互換。
    軸A＝今賽尾盤對上賽尾盤（互換對比盤）；軸B＝今賽尾盤對上賽尾盤（同主客原盤）"""
    df, _ = se.load_pool(conn)
    res_np = df['res'].to_numpy()
    has_pv = df['pv_h'].notna().to_numpy() & df['pvN_h'].notna().to_numpy()
    c_s = _signed_vec(df['c_h'], df['c_g'].fillna('none'))
    pvN_s = _signed_vec(df['pvN_h'], df['pvN_g'].fillna('none'))
    pv_s = _signed_vec(df['pv_h'], df['pv_g'].fillna('none'))
    valid = has_pv & ~np.isnan(pvN_s) & ~np.isnan(c_s)
    sa = np.full(len(df), '', dtype=object)
    sb = np.full(len(df), '', dtype=object)
    sa[valid] = [state_of(pvN_s[i], c_s[i]) for i in np.nonzero(valid)[0]]
    same_role = valid & df['pv_same'].to_numpy() & ~np.isnan(pv_s)
    sb[same_role] = [state_of(pv_s[i], c_s[i]) for i in np.nonzero(same_role)[0]]
    sb_raw = np.full(len(df), '', dtype=object)
    raw_ok = valid & ~np.isnan(pv_s)
    sb_raw[raw_ok] = [state_of(pv_s[i], c_s[i]) for i in np.nonzero(raw_ok)[0]]

    row1 = _state_grid(df, same_role, sa, sb, res_np)
    row2 = _state_grid(df, valid, sa, sb_raw, res_np)

    out = {'row1': row1, 'row2': row2,
           'axes': ['deep', 'shallow', 'same'],
           'axis_txt': {'deep': '深', 'shallow': '淺', 'same': '不變'},
           'row_note': {
               'row1': '行1（同主客）：只計「對上一次對賽都係同主客方向」嘅歷史場次，原盤直比',
               'row2': '行2（計埋互換）：全部有對賽記錄嘅場次，互換對比盤直比'}}
    if t is not None:
        cur = item31(build_context(conn, t))
        out['target31'] = cur
    return out
