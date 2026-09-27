# -*- coding: utf-8 -*-
"""V3 分析引擎（2026-09-28 V3 規格定稿）。

編號（用戶 2026-09-28 確認）：
  1–6   六時點組（同[初盤/4h/30m/15m/10m/5m]及同尾盤，盤口100%一樣＋水位±0.03）
        → A–H＝淨主淨客、I–P＝淨主淨客＋主客互換，各×8範圍＝16格；每格12段水位
  7–13  上賽尾盤 vs 今場各時點 ±3%：同主客／對調／綜合 三結果
  14–20 同上但12段水位版：3類×(同聯賽/聯賽類/全庫)＝9格
  21–22 戰績主主場/客客場±7%：盤口分佈／水位版
  23–24 戰績總和±7%：盤口分佈／水位版
  25–26 入失球主主場/客客場±3：盤口分佈／水位版
  27–28 排名主主場&客客場／總排名（各±2）：分佈＋水位
  29    上賽完全相同（主客一樣＋讓/受讓＋讓球數）→ 今場主場盤口分佈＋水位
  30    29樣本：分佈最多盤口＋最接近50%盤口 vs 今場尾盤淨差距（全部水位區）
  31    排名差淨值（主主場−客客場）±1 → 分佈＋水位
  32    31樣本淺深淨差距
  33–34 排名差淨值（總和）±1 → 分佈＋水位／淺深
  35–36 入失球主主場/客客場±3 → 分佈＋水位／淺深
  37–38 入失球總和±3 → 分佈＋水位／淺深
  39–44 首14分鐘入球%：六時點組×16格（A–P）
  45    首14分鐘：排名差主／排名差總／入失球主／入失球總 四小項
  組合篩查：第1–38項多選（AND）
  精選W：1A、1I、19同主客、19+互換、31、35 全部同方向≥50%

時間：全引擎用香港時間（UTC+8 硬編碼），唔靠系統時區。
互換：公式規格_主客對調.txt v5（h新=2H−h舊，水位鏡像±0.03，返還率93.9%±0.04）。
N≤5 照顯示實數。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import screen_engine as se
import swap_v5
import v2_engine as v2

HK_TZ = timezone(timedelta(hours=8))
TP6 = v2.TP6
TP7 = v2.TP7
SCOPES8 = v2.SCOPES8
LETTERS = v2.LETTERS
ZONES12 = v2.ZONES12
RECENT2Y = v2.RECENT2Y
RETURN_LO, RETURN_HI = 0.939 - 0.04, 0.939 + 0.04   # 雙向總返還率容差


def hk_now():
    """香港時間 now（naive，同 kickoff 字串比較用）"""
    return datetime.now(HK_TZ).replace(tzinfo=None)


def hk_now_str():
    return hk_now().strftime('%Y-%m-%d %H:%M:%S')


def match_state(kickoff, home_score):
    """scheduled / live / finished（開賽後冇比分＝進行中）"""
    if home_score is not None:
        return 'finished'
    if kickoff and kickoff <= hk_now_str():
        return 'live'
    return 'scheduled'


# ---------- V3 數據池（V2 池 + 對調盤/對調水位欄） ----------

_v3_pool = {'df': None, 'ts': 0}
_v3_lock = threading.Lock()


def _gmap_arr(s):
    return s.fillna('none').map({'home': 1, 'away': -1, 'none': 0})


def _p_table(conn):
    """每聯賽每個 0.25 盤口嘅主隊贏盤概率 P(margin>h)（由該聯賽全部賽果計）。
    key = (聯賽名, 帶符號盤口)。"""
    if _p_table.cache is not None:
        return _p_table.cache
    rows = conn.execute(
        'SELECT c.req_name, m.home_score - m.away_score, COUNT(*) '
        'FROM matches m JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'WHERE m.home_score IS NOT NULL GROUP BY 1, 2').fetchall()
    by_lg = {}
    for lg, margin, cnt in rows:
        by_lg.setdefault(lg, []).append((int(margin), int(cnt)))
    grid = [x * 0.25 for x in range(-12, 13)]
    tab = {}
    for lg, margins in by_lg.items():
        for h in grid:
            tab[(lg, h)] = swap_v5.cover_prob(margins, h)
    _p_table.cache = tab
    return tab


_p_table.cache = None


def load_v3_pool(conn, max_age=1800):
    """V3 池＝V2 池＋每時點『對調後』(h,g,ho,ao)。
    對調：s新=2H−s舊（中立場 −s舊），水位鏡像；H 由聯賽名 H_TABLE／全庫平均。
    測試模式：環境變數 V3_TEST_YEAR=2026 → 只保留該年 1 月 1 日起開賽嘅場次。"""
    import os
    now = time.time()
    if _v3_pool['df'] is not None and now - _v3_pool['ts'] < max_age:
        return _v3_pool['df']
    with _v3_lock:
        now = time.time()
        if _v3_pool['df'] is not None and now - _v3_pool['ts'] < max_age:
            return _v3_pool['df']
        df = v2.load_v2_pool(conn, max_age=max_age).copy()
        test_year = os.environ.get('V3_TEST_YEAR')
        if test_year:
            before = len(df)
            df = df[df['kickoff'] >= f'{test_year}-01-01'].copy()
            print(f'[v3_pool] 測試模式：只計 {test_year} 年起開賽（{before}→{len(df)} 場）', flush=True)
        n = len(df)
        # is_neutral 由 matches 補入
        neu_df = pd.read_sql('SELECT id, is_neutral FROM matches', conn)
        df = df.merge(neu_df, on='id', how='left')
        # 每場所屬聯賽 H：pool 用 req_name（全名），H_TABLE 用 name_tc（簡稱），要做映射
        lg_names = df['league'].fillna('').to_numpy()
        req2tc = dict(conn.execute('SELECT req_name, name_tc FROM competitions'))
        h_vec = np.array([swap_v5.H_TABLE.get(req2tc.get(x, x)) for x in lg_names],
                         dtype=object)
        missing = np.array([x is None for x in h_vec])
        h_vals = np.where(missing, 0.30, np.array(
            [x if x is not None else 0.30 for x in h_vec], dtype=float))
        # 中立場 H=0
        neu = df['is_neutral'].fillna(0).to_numpy()
        h_vals = np.where(neu.astype(bool), 0.0, h_vals)
        ptab = _p_table(conn)
        lg_arr = df['league'].fillna('').to_numpy()
        for _no, _label, pfx, _nm in TP7:
            h = df[f'{pfx}_h'].to_numpy(dtype=float)
            g = _gmap_arr(df[f'{pfx}_g']).to_numpy()
            s = np.where(np.isnan(h), np.nan, h * g)      # 主隊角度帶符號
            with np.errstate(invalid='ignore'):
                s_sw = np.where(np.isnan(s), np.nan,
                                np.where(neu.astype(bool), -s, 2.0 * h_vals - s))
            # 量化到 0.25 格（DB 盤口全部係 0.25 倍數）
            s_sw_q = np.where(np.isnan(s_sw), np.nan,
                              np.round(s_sw / 0.25) * 0.25)
            df[f'{pfx}_sw_h'] = np.abs(s_sw_q)
            df[f'{pfx}_sw_g'] = np.where(s_sw_q > 0, 'home',
                                         np.where(s_sw_q < 0, 'away', 'none'))
            # 對調水位（v4 公式）：O=該場實測；p=該聯賽淨勝球分佈推得
            ho = df[f'{pfx}_ho'].to_numpy(dtype=float)
            ao = df[f'{pfx}_ao'].to_numpy(dtype=float)
            sw_ho = np.full(n, np.nan)
            sw_ao = np.full(n, np.nan)
            okm = (~np.isnan(ho)) & (~np.isnan(ao)) & (~np.isnan(s_sw_q))
            if okm.any():
                O_row = 1.0 / (1.0 + ho[okm]) + 1.0 / (1.0 + ao[okm])
                p_vec = np.array([ptab.get((lg_arr[i], s_sw_q[i]), 0.5)
                                  for i in np.nonzero(okm)[0]])
                is_neu = neu[okm].astype(bool)
                p_vec = np.where(is_neu, 0.5, p_vec)
                O_row = np.where(is_neu, swap_v5.O_EASYBETS, O_row)
                p_vec = np.clip(p_vec, 0.05, 0.95)
                sw_ho[okm] = 1.0 / (O_row * p_vec) - 1.0
                sw_ao[okm] = 1.0 / (O_row * (1.0 - p_vec)) - 1.0
            df[f'{pfx}_sw_ho'] = sw_ho
            df[f'{pfx}_sw_ao'] = sw_ao
        # 雙向總返還率：O = 1/(1+ho)+1/(1+ao)；返還率 = 1/O
        ho = df['c_ho'].to_numpy(dtype=float)
        ao = df['c_ao'].to_numpy(dtype=float)
        with np.errstate(invalid='ignore', divide='ignore'):
            O = 1.0 / (1.0 + ho) + 1.0 / (1.0 + ao)
            ret = 1.0 / O
        df['ret_ok'] = (~np.isnan(ret)) & (ret >= RETURN_LO) & (ret <= RETURN_HI)
        _v3_pool['df'] = df
        _v3_pool['ts'] = time.time()
        print(f'[v3_pool] {n} 場', flush=True)
        return df


def invalidate_pool():
    _v3_pool['ts'] = 0
    _v3_pool['df'] = None
    v2.invalidate_pool()


# ---------- 基本工具 ----------

_oc = v2._oc
_res_code = v2._res_code
_oc_mask = v2._oc_mask
_scope_masks = v2._scope_masks
_water12 = v2._water12
_dist_lines = v2._dist_lines
_form_mask = v2._form_mask
_goal_mask = v2._goal_mask
_rank_mask = v2._rank_mask
_up_water_of = v2._up_water_of
zone12_of = v2.zone12_of
fmt_line = se.fmt_line


def _signed_T(T):
    """目標線 → 帶符號讓球（主隊角度）"""
    if T is None or T.get('h') is None:
        return None
    h, g = T['h'], T.get('g') or 'none'
    return h * {'home': 1, 'away': -1, 'none': 0}[g]


def _eq_signed_line(df, pfx, s_t, swap=False):
    """純盤口（帶符號）相同；swap=True 用對調後欄"""
    if s_t is None:
        return None
    if swap:
        h = df[f'{pfx}_sw_h'].to_numpy(dtype=float)
        g = df[f'{pfx}_sw_g'].fillna('none').to_numpy()
    else:
        h = df[f'{pfx}_h'].to_numpy(dtype=float)
        g = df[f'{pfx}_g'].fillna('none').to_numpy()
    s = h * np.array([{'home': 1, 'away': -1, 'none': 0}[x] for x in g])
    return (s == s_t) & ~np.isnan(h)


def _target_ret_ok(T):
    """目標線兩邊水位嘅雙向總返還率係咪喺 93.9%±0.04（互換公式適用前提）"""
    if T is None:
        return False
    ho, ao = T.get('ho'), T.get('ao')
    if ho is None or ao is None:
        return False
    try:
        O = 1.0 / (1.0 + ho) + 1.0 / (1.0 + ao)
        r = 1.0 / O
    except ZeroDivisionError:
        return False
    return RETURN_LO <= r <= RETURN_HI


def _eq_line_water(df, pfx, T, tol=0.03, swap=False):
    """盤口100%一樣＋主客水位各±tol；swap=True＝對調匹配：
    對調盤100%一樣＋該場返還率喺93.9%±0.04（公式適用前提），
    水位唔再逐點比（對調水位由公式計出，用戶2026-09-26確認容差＝返還率）"""
    s_t = _signed_T(T)
    m = _eq_signed_line(df, pfx, s_t, swap=swap)
    if m is None:
        return None
    if swap:
        # 尾盤對調用 c_* 欄，ret_ok 建於尾盤；其他時點近似沿用（全行守尾盤返還率）
        return m & df['ret_ok'].to_numpy()
    ho_t, ao_t = T.get('ho'), T.get('ao')
    if ho_t is None or ao_t is None:
        return None
    ho = df[f'{pfx}_ho'].to_numpy(dtype=float)
    ao = df[f'{pfx}_ao'].to_numpy(dtype=float)
    return m & (np.abs(ho - ho_t) <= tol) & (np.abs(ao - ao_t) <= tol)


def _and(a, b):
    if a is None or b is None:
        return None
    return a & b


def _n(m):
    return 0 if m is None else int(np.count_nonzero(m))


# ---------- build_context ----------

def build_context(conn, t):
    """V3 全部項目共用樣本 mask"""
    df = load_v3_pool(conn)
    odds = t.get('odds') or {}
    T_close = se.tline(odds, 'closing')
    ctx = {'df': df, 't': t, 'T_close': T_close,
           'cur_zone12': zone12_of(_up_water_of(T_close)),
           'cur_line_key': (T_close['h'], T_close.get('g') or 'none')
                           if T_close and T_close.get('h') is not None else None}
    ctx['same_close'] = v2._line_only_eq(df, 'c', T_close) if T_close else None
    sc = _signed_T(T_close)
    ctx['sc_close'] = sc

    # 時點組 1–6：淨主淨客 base / 互換 base_sw
    tp = {}
    for no, label, pfx, name in TP6:
        T = se.tline(odds, label)
        Tc = T_close
        if T is None or Tc is None or T.get('h') is None or Tc.get('h') is None:
            tp[no] = (T, pfx, name, None, None)
        else:
            base = _and(_eq_line_water(df, pfx, T), _eq_line_water(df, 'c', Tc))
            if _target_ret_ok(T) and _target_ret_ok(Tc):
                base_sw = _and(_eq_line_water(df, pfx, T, swap=True),
                               _eq_line_water(df, 'c', Tc, swap=True))
            else:
                base_sw = None   # 目標返還率超出93.9%±0.04，互換公式唔適用
            tp[no] = (T, pfx, name, base, base_sw)
    ctx['tp'] = tp

    # H2H 七時點 7–20
    h2h = {}
    for i, (no, label, pfx, name) in enumerate(TP7):
        vno, wno = str(7 + i), str(14 + i)
        T = se.tline(odds, label)
        ml = v2._line_only_eq(df, 'pvN', T) if T else None
        mw = v2._line_water_eq(df, 'pvN', T) if T else None
        h2h[vno] = (T, name, ml)
        h2h[wno] = (T, name, mw)
    ctx['h2h'] = h2h

    f6 = _form_mask(df, t, 'home', 'away')
    f7 = _form_mask(df, t, 'total', 'total')
    g8 = _goal_mask(df, t, 'home', 'away')
    g9 = _goal_mask(df, t, 'total', 'total')
    m12 = _rank_mask(df, t, 'home_home_rank', 'away_away_rank')
    m13 = _rank_mask(df, t, 'home_total_rank', 'away_total_rank')
    ctx['cond'] = {'f6': f6, 'f7': f7, 'g8': g8, 'g9': g9,
                   'm12': m12, 'm13': m13}
    # 31／33：排名差距淨值 ±1
    ctx['d31'] = ctx['d33'] = None
    ctx['m31'] = ctx['m33'] = None
    if v2.pre_get(t, 'home_home_rank') and v2.pre_get(t, 'away_away_rank'):
        d = t['pre']['home_home_rank'] - t['pre']['away_away_rank']
        ctx['m31'] = ((df['home_home_rank'] - df['away_away_rank']) - d).abs() <= 1
        ctx['d31'] = d
    if v2.pre_get(t, 'home_total_rank') and v2.pre_get(t, 'away_total_rank'):
        d = t['pre']['home_total_rank'] - t['pre']['away_total_rank']
        ctx['m33'] = ((df['home_total_rank'] - df['away_total_rank']) - d).abs() <= 1
        ctx['d33'] = d
    # 29：上賽完全相同
    hp = t.get('home_prev')
    m16 = None
    if hp is not None and hp.get('h') is not None:
        m16 = (df['hp_role'] == hp['role']) & \
              (df['hp_g'].fillna('none') == (hp.get('g') or 'none')) & \
              (df['hp_h'] == hp['h'])
    ctx['m16'] = m16
    return ctx


def pre_get(t, k):
    return (t.get('pre') or {}).get(k)


# ---------- 16 格（V3：A–H 淨主淨客／I–P 互換） ----------

def _cells16_v3(df, base, base_sw, t):
    scopes = _scope_masks(df, t.get('league'))
    res_np = df['res'].to_numpy()
    cells = []
    for swap in (False, True):
        for sc in SCOPES8:
            letter = LETTERS[len(cells)]
            b = base_sw if swap else base
            if b is None:
                cells.append({'letter': letter, 'scope': sc,
                              'swap': swap, 'error': '不適用'})
                continue
            m = b & scopes[sc]
            oc = _oc(_res_code(res_np[np.nonzero(m)[0]]))
            c = {'letter': letter, 'scope': sc, 'swap': swap}
            c.update(oc or {'n': 0, 'up': 0, 'down': 0, 'push': 0,
                            'up_r': None, 'down_r': None, 'push_r': None})
            c['zones'] = _water12(df, m)['zones']
            cells.append(c)
    return cells


def item_timegroup_v3(ctx, no):
    """項目 1–6：16格（淨主淨客／互換）＋每格12段水位（同水位區黃底 highlight）"""
    df, t = ctx['df'], ctx['t']
    T, pfx, name, base, base_sw = ctx['tp'][no]
    if T is None or T.get('h') is None:
        return {'no': no, 'title': f'同{name}及尾盤的盤口&水位',
                'ref': f'今場缺少{name}數據', 'error': '不適用'}
    if ctx['T_close'] is None:
        return {'no': no, 'title': f'同{name}及尾盤的盤口&水位',
                'ref': '今場缺少尾盤數據', 'error': '不適用'}
    Tc = ctx['T_close']
    ref = (f"今場{name}：{fmt_line(T['h'], T['g'])} 主{T['ho']}/客{T['ao']}　"
           f"今場尾盤：{fmt_line(Tc['h'], Tc['g'])} 主{Tc['ho']}/客{Tc['ao']}　"
           '歷史場次兩個時點都同今場 100% 相同（水位±0.03）；'
           'A–H＝淨主淨客（讓球方一致），I–P＝計埋主客互換（公式對調盤＋水位鏡像）')
    return {'no': no,
            'title': f'同{name}及尾盤的盤口&水位（A–H 淨主淨客／I–P 連互換 ×8範圍）',
            'ref': ref, 'cells': _cells16_v3(df, base, base_sw, t),
            'cur_zone12': ctx['cur_zone12'], 'zones12': ZONES12,
            'mix_note': '淨主淨客＝讓球方一致（主讓平半≠客讓平半）；'
                        '互換＝按公式規格_主客對調 v5 對調後一致（返還率93.9%±0.04）'}


# ---------- H2H 7–20 ----------

def item_h2h_line_v3(ctx, no):
    """項目 7–13：上賽尾盤 vs 今場時點 ±3%：同主客／對調／綜合"""
    df = ctx['df']
    T, name, m = ctx['h2h'][no]
    if m is None:
        return {'no': no, 'title': f'上賽尾盤 對 今場{name}（水位±3%）',
                'ref': f'今場缺少{name}數據', 'error': '不適用'}
    pv_same = df['pv_same'].to_numpy()
    has_pv = df['pv_h'].notna().to_numpy()
    res_np = df['res'].to_numpy()

    def oc(mask):
        return _oc(_res_code(res_np[np.nonzero(mask)[0]]))
    return {'no': no, 'title': f'上賽尾盤 對 今場{name}（盤口100%一樣＋水位±3%）',
            'ref': (f'今場{name}：{fmt_line(T["h"], T["g"])} 主{T["ho"]}/客{T["ao"]}　'
                    '上賽尾盤已做主客互換對比'),
            'tri': {'same': oc(m & pv_same),
                    'swap': oc(m & has_pv & ~pv_same),
                    'all': oc(m)}}


def item_h2h_water_v3(ctx, no):
    """項目 14–20：水位段版：3類×(同聯賽/聯賽類/全庫)＝9格，每格12段水位"""
    df, t = ctx['df'], ctx['t']
    T, name, m = ctx['h2h'][no]
    if m is None:
        return {'no': no, 'title': f'上賽尾盤 對 今場{name}（水位段版）',
                'ref': f'今場缺少{name}數據', 'error': '不適用'}
    pv_same = df['pv_same'].to_numpy()
    has_pv = df['pv_h'].notna().to_numpy()
    scopes = _scope_masks(df, t.get('league'))
    cells = []
    for tri, tm in (('同主客場', m & pv_same),
                    ('對調', m & has_pv & ~pv_same),
                    ('綜合', m)):
        for sc in ('同一聯賽', '聯賽類', '全庫'):
            w = _water12(df, tm & scopes[sc])
            cells.append({'tri': tri, 'scope': sc, 'n': w['n'],
                          'zones': w['zones']})
    return {'no': no,
            'title': f'上賽尾盤 對 今場{name}（12段水位 × 同主客/對調/綜合 × 3範圍）',
            'ref': (f'今場{name}：{fmt_line(T["h"], T["g"])} 主{T["ho"]}/客{T["ao"]}　'
                    '盤口100%一樣＋水位±0.03（上賽尾盤互換對比）；'
                    '黃底＝今場上盤水位所處段位'),
            'cells': cells, 'cur_zone12': ctx['cur_zone12'], 'zones12': ZONES12}


# ---------- 條件項 21–28／31／33／35／37（分佈＋水位，3範圍） ----------

COND_SPEC = {
    '21': ('主隊主場勝和負比例 及 客隊客場（每項±7%）', 'f6'),
    '22': ('主隊主場勝和負比例 及 客隊客場（每項±7%）【水位版】', 'f6'),
    '23': ('主隊主客場總和勝和負比例 及 客隊總和（每項±7%）', 'f7'),
    '24': ('主隊主客場總和勝和負比例 及 客隊總和（每項±7%）【水位版】', 'f7'),
    '25': ('主隊主場入球/失球/得失球差 及 客隊客場（各±3球）', 'g8'),
    '26': ('主隊主場入球/失球/得失球差 及 客隊客場（各±3球）【水位版】', 'g8'),
    '27': ('主隊主場排名 及 客隊客場排名（各±2）', 'm12'),
    '28': ('主隊主客場總排名 及 客隊總排名（各±2）', 'm13'),
    '31': ('主隊主場排名 − 客隊客場排名 差距淨值（±1）', 'm31'),
    '33': ('主隊總排名 − 客隊總排名 差距淨值（±1）', 'm33'),
    '35': ('主隊主場入球/失球/得失球差 及 客隊客場（各±3球）', 'g8'),
    '37': ('主隊主客場總和入球/失球/得失球差 及 客隊總和（各±3球）', 'g9'),
}
COND3 = ('同一聯賽', '聯賽類', '全庫')


def item_cond_v3(ctx, no):
    """分佈項（21,23,25,27,29,31,33,35,37）＋水位版（22,24,26）"""
    df, t = ctx['df'], ctx['t']
    title, key = COND_SPEC[no]
    if key == 'm31':
        base, extra = ctx['m31'], (f'差距 {ctx["d31"]:+d}（±1）'
                                   if ctx['d31'] is not None else '')
    elif key == 'm33':
        base, extra = ctx['m33'], (f'差距 {ctx["d33"]:+d}（±1）'
                                   if ctx['d33'] is not None else '')
    else:
        base, extra = ctx['cond'].get(key), ''
    water_only = no in ('22', '24', '26')
    if base is None:
        return {'no': no, 'title': title,
                'ref': '目標賽事數據不足（未有開賽前狀態）', 'error': '不適用'}
    scopes = _scope_masks(df, t.get('league'))
    Tc = ctx['T_close']
    cur = (Tc['h'], Tc.get('g') or 'none') if Tc else None
    out = {'no': no, 'title': title,
           'ref': (f'{title}；{extra} ' if extra else f'{title}；') +
                  '黃底＝今場尾盤盤口／上盤水位段位',
           'oc': _oc_mask(df, base), 'cur_zone12': ctx['cur_zone12'],
           'zones12': ZONES12}
    for sc in COND3:
        m = base & scopes[sc]
        oc = _oc_mask(df, m)
        out.setdefault('scopes', []).append(
            {'scope': sc, 'n': oc['n'] if oc else 0,
             'up_r': oc['up_r'] if oc else None,
             'down_r': oc['down_r'] if oc else None,
             'push_r': oc['push_r'] if oc else None})
    if not water_only:
        out['dist'] = _dist_lines(df, base, cur_zone12=cur)
        out['water'] = {'all': _water12(df, base)}
    else:
        out['water'] = {'all': _water12(df, base)}
    return out


def _mode_d50(df, m):
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
    return mode, (d50[1] if d50 else None)


def _gap_pack(df, r, T_close, conn=None):
    """淺深 pack：盤＋勝率＋全部水位區勝率場次"""
    if r is None:
        return None
    gap = se.gap_text(r['h'], None if r['g'] == 'none' else r['g'], T_close)
    if isinstance(gap, dict):
        gap = gap.get('text')
    return {'line': r['line'], 'n': r['n'],
            'up_r': r['up_r'], 'down_r': r['down_r'],
            'push': r['push'], 'push_r': r['push_r'], 'gap': gap,
            'zones': r['zones'], 'h_g': r['h'], 'g_g': r['g']}


def item_gap_v3(ctx, no, base):
    """淺深項（30／32／34／36／38）：分佈最多盤口＋最接近50%盤口 vs 今場尾盤"""
    df, Tc = ctx['df'], ctx['T_close']
    if base is None:
        return {'no': no, 'title': '淺深淨差距', 'ref': '樣本不足',
                'error': '不適用'}
    mode, d50 = _mode_d50(df, base)
    return {'no': no,
            'title': '分佈最多盤口 及 上盤勝率最接近50%盤口 與今場尾盤淨差距',
            'ref': '參照盤＝上項樣本嘅分佈最多／最接近50%盤口（至少5場）；'
                   '深淺以讓球方角度計（8條規則）',
            'cur_line': fmt_line(Tc['h'], Tc['g']) if Tc else None,
            'mode': _gap_pack(df, mode, Tc),
            'd50': _gap_pack(df, d50, Tc)}


def item19_v3(ctx):
    """項目 29：上賽完全相同 → 今場主場盤口分佈＋水位分佈（3範圍）"""
    df, t = ctx['df'], ctx['t']
    if ctx['m16'] is None:
        return {'no': '29',
                'title': '上賽完全相同（主客一樣＋讓/受讓＋讓球數）→ 今場主場盤口分佈',
                'ref': '今次主隊未有對上一次比賽尾盤數據', 'error': '不適用'}
    hp = t.get('home_prev') or {}
    base = ctx['m16']
    scopes = _scope_masks(df, t.get('league'))
    Tc = ctx['T_close']
    cur = (Tc['h'], Tc.get('g') or 'none') if Tc else None
    res = {'no': '29',
           'title': '上賽完全相同（主客一樣＋讓/受讓＋讓球數）→ 今場主場盤口分佈＋水位',
           'ref': f'{t.get("home", "")} 上次：{hp.get("role", "")} '
                  f'{fmt_line(hp.get("h"), hp.get("g"))}；黃底＝今場尾盤盤口',
           'oc': _oc_mask(df, base), 'cur_zone12': ctx['cur_zone12'],
           'zones12': ZONES12}
    for sc in COND3:
        oc = _oc_mask(df, base & scopes[sc])
        res.setdefault('scopes', []).append(
            {'scope': sc, 'n': oc['n'] if oc else 0,
             'up_r': oc['up_r'] if oc else None,
             'down_r': oc['down_r'] if oc else None,
             'push_r': oc['push_r'] if oc else None})
    res['dist'] = _dist_lines(df, base, cur_zone12=cur)
    res['water'] = {'all': _water12(df, base)}
    return res


# ---------- 首 14 分鐘（39–45） ----------

def item_first14_v3(conn, ctx, no):
    df, t = ctx['df'], ctx['t']
    f14 = v2._first14_map(conn)
    total = len(f14)
    if no in tuple(x[0] for x in TP6):
        _, pfx, name, base, _sw = ctx['tp'][no]
        if base is None:
            return {'no': no,
                    'title': f'首14分鐘入球%（同{name}及尾盤時點組）',
                    'error': '不適用'}
        cells = []
        scopes = _scope_masks(df, t.get('league'))
        home_id = t.get('home_id')
        same_team = (df['home_id'] == home_id) if home_id \
            else np.zeros(len(df), dtype=bool)
        for mix in v2.MIX2:
            for sc in SCOPES8:
                letter = LETTERS[len(cells)]
                m = base & scopes[sc]
                if mix == '同主隊':
                    m &= same_team
                st = v2._first14_stats(df, m, f14)
                st.update({'letter': letter, 'scope': sc, 'mix': mix})
                cells.append(st)
        return {'no': no,
                'title': f'首14分鐘入球%：主入/客入/冇入（同{name}及尾盤，16格）',
                'cells': cells, 'data_n': total,
                'data_note': None if total >= 1000 else
                f'分鐘級入球數據爬取中（暫得 {total} 場有首14分鐘記錄）'}
    single = {
        '31': ('排名差距淨值（主場−客場）±1', ctx.get('m31')),
        '33': ('主隊總排名−客隊總排名 差距淨值（±1）', ctx.get('m33')),
        '35': ('主隊主場入球/失球/差 及 客隊客場（各±3）', ctx['cond'].get('g8')),
        '37': ('主隊總和入球/失球/差 及 客隊總和（各±3）', ctx['cond'].get('g9')),
    }
    parts = []
    for k, (nm, m) in single.items():
        parts.append({'src': k, 'name': nm,
                      'stats': v2._first14_stats(df, m, f14)})
    return {'no': no, 'title': '首14分鐘入球%（排名差距／入失球 四小項）',
            'parts': parts, 'data_n': total,
            'data_note': None if total >= 1000 else
            f'分鐘級入球數據爬取中（暫得 {total} 場有首14分鐘記錄）'}


# ---------- 統一入口 ----------

GAP_ITEMS = {'30': 'm16', '32': 'm31', '34': 'm33', '36': 'g8', '38': 'g9'}


def compute_item_v3(conn, t, no):
    n = int(no)
    ctx = build_context(conn, t)
    if no in tuple(x[0] for x in TP6):
        return item_timegroup_v3(ctx, no)
    if 7 <= n <= 13:
        return item_h2h_line_v3(ctx, no)
    if 14 <= n <= 20:
        return item_h2h_water_v3(ctx, no)
    if no in COND_SPEC:
        return item_cond_v3(ctx, no)
    if no == '29':
        return item19_v3(ctx)
    if no in GAP_ITEMS:
        key = GAP_ITEMS[no]
        base = ctx['m16'] if key == 'm16' else \
            (ctx['m31'] if key == 'm31' else
             (ctx['m33'] if key == 'm33' else ctx['cond'].get(key)))
        return item_gap_v3(ctx, no, base)
    if no in ('39', '40', '41', '42', '43', '44', '45'):
        sub = str(n - 38) if n <= 44 else '45'
        out = item_first14_v3(conn, ctx, sub)
        out['no'] = no   # 對外一定要係 V3 項目號（39–45），唔係內部細項號
        return out
    return {'error': f'未知項目 {no}'}


def item_applicability_v3(conn, t):
    ctx = build_context(conn, t)
    ok = {}
    for no, label, pfx, name in TP6:
        T = se.tline((t.get('odds') or {}), label)
        ok[no] = bool(T is not None and T.get('h') is not None
                      and ctx['T_close'] is not None)
        ok[str(int(no) + 38)] = ok[no]
    ok['45'] = bool(ctx.get('m31') is not None or ctx.get('m33') is not None
                    or ctx['cond'].get('g8') is not None
                    or ctx['cond'].get('g9') is not None)
    for i in range(7):
        vno, wno = str(7 + i), str(14 + i)
        T = ctx['h2h'][vno][0]
        ok[vno] = bool(T is not None and ctx['df']['pvN_h'].notna().any())
        ok[wno] = ok[vno]
    for no, (title, key) in COND_SPEC.items():
        if key == 'm31':
            ok[no] = ctx['m31'] is not None
        elif key == 'm33':
            ok[no] = ctx['m33'] is not None
        else:
            ok[no] = ctx['cond'].get(key) is not None
    ok['29'] = ctx['m16'] is not None
    for no, key in GAP_ITEMS.items():
        ok[no] = ok.get('29' if key == 'm16' else
                        ('31' if key == 'm31' else
                         ('33' if key == 'm33' else
                          ('35' if key == 'g8' else '37'))), False)
    return ok


# ---------- 組合篩查（第 1–38 項多選，AND） ----------

def _combo_mask(ctx, no):
    """每個項目嘅篩選 mask（None＝不適用）。淺深項用『尾盤==分佈最多盤口』做條件。"""
    df = ctx['df']
    n = int(no)
    if no in tuple(x[0] for x in TP6):
        return ctx['tp'][no][3]
    if 7 <= n <= 13:
        return ctx['h2h'][no][2]
    if 14 <= n <= 20:
        return ctx['h2h'][no][2]
    if no in COND_SPEC:
        title, key = COND_SPEC[no]
        if key == 'm31':
            return ctx['m31']
        if key == 'm33':
            return ctx['m33']
        return ctx['cond'].get(key)
    if no == '29':
        return ctx['m16']
    if no in GAP_ITEMS:
        key = GAP_ITEMS[no]
        base = ctx['m16'] if key == 'm16' else \
            (ctx['m31'] if key == 'm31' else
             (ctx['m33'] if key == 'm33' else ctx['cond'].get(key)))
        if base is None:
            return None
        mode, _d = _mode_d50(df, base)
        if mode is None:
            return None
        return v2._line_only_eq(df, 'c', {'h': mode['h_g'],
                                          'g': None if mode['g_g'] == 'none'
                                          else mode['g_g']})
    return None


def combo_v3(conn, t, sel):
    sel = [str(s).strip() for s in sel if str(s).strip()]
    bad = [s for s in sel if not s.isdigit() or not (1 <= int(s) <= 38)]
    if bad:
        return {'error': f'組合篩查只接受第 1–38 項：{"、".join(bad)}'}
    if len(sel) > 12:
        return {'error': '最多同時剔選 12 項（計算量控制）'}
    ctx = build_context(conn, t)
    df, lg = ctx['df'], t.get('league')
    m = None
    for no in sel:
        cm = _combo_mask(ctx, no)
        if cm is None:
            return {'error': f'第 {no} 項而家不適用（目標賽事數據不足）'}
        m = cm if m is None else (m & cm)
    if m is None:
        return {'error': '請至少剔選一項'}
    scopes = _scope_masks(df, lg)
    out = {'n': _n(m), 'sel': sel}
    for sc in COND3:
        oc = _oc_mask(df, m & scopes[sc]) or {
            'n': 0, 'up': 0, 'down': 0, 'push': 0,
            'up_r': None, 'down_r': None, 'push_r': None}
        out.setdefault('scopes', []).append(
            {'scope': sc, **oc})
    out['water'] = {'all': _water12(df, m)}
    out['cur_zone12'] = ctx['cur_zone12']
    out['zones12'] = ZONES12
    return out


# ---------- 精選 W ----------

def _cell_dir(c):
    ur, dr = c.get('up_r'), c.get('down_r')
    if ur is not None and ur > 0.4999:
        return 'up', ur
    if dr is not None and dr > 0.4999:
        return 'down', dr
    return None, None


def featured_w(conn, t):
    """精選W：1A、1I、19同主客、19+互換、31、35 全部同方向≥50%。
    每項展示 盤口%＋同水位區%。合格後展示方向＋淺深分析（30項：最多/最平均盤）。
    回傳 {'pass', 'direction', 'conds', 'gap30'}；不適用／唔合格照樣回傳 details。"""
    ctx = build_context(conn, t)
    df = ctx['df']
    T1, _p1, _n1, base1, base1_sw = ctx['tp']['1']
    if base1 is None or base1_sw is None or ctx['T_close'] is None:
        return {'pass': False, 'error': '今場缺少初盤或尾盤數據'}
    scopes = _scope_masks(df, t.get('league'))

    def cond_pack(mask, note):
        oc = _oc_mask(df, mask)
        d = None
        if oc and oc['up_r'] is not None:
            if oc['up_r'] > 0.4999:
                d = 'up'
            elif oc['down_r'] > 0.4999:
                d = 'down'
        z = ctx['cur_zone12']
        zr = None
        if mask is not None and z >= 0:
            zm = mask & (df['up_zone12'].to_numpy() == z)
            zoc = _oc_mask(df, zm)
            if zoc and zoc['n'] > 0:
                zr = {'n': zoc['n'], 'up_r': zoc['up_r'],
                      'down_r': zoc['down_r'], 'zone': ZONES12[z]}
        return {'dir': d, 'oc': oc, 'zone_r': zr, 'note': note}

    conds = []
    conds.append(cond_pack(base1 & scopes['全庫'], '1A：同初盤&水位＋同尾盤（淨主淨客·全庫）'))
    conds.append(cond_pack(base1_sw & scopes['全庫'],
                           '1I：同上但計埋主客互換（全庫）'))
    # 19：上賽完全相同（同主客／＋互換）
    m16 = ctx['m16']
    if m16 is not None:
        pv_same = df['pv_same'].to_numpy()
        has_pv = df['pv_h'].notna().to_numpy()
        conds.append(cond_pack(m16 & pv_same,
                               '19：上賽完全相同（只計同主、同客）'))
        conds.append(cond_pack(m16 & has_pv,
                               '19＋互換：上賽完全相同（同主客＋對調）'))
    else:
        conds.append({'dir': None, 'oc': None, 'note': '19：無上次比賽數據'})
        conds.append({'dir': None, 'oc': None, 'note': '19＋互換：無上次比賽數據'})
    conds.append(cond_pack(ctx['m31'],
                           '31：主主場排名−客客場排名差距淨值（±1）'))
    conds.append(cond_pack(ctx['cond'].get('g8'),
                           '35：主主場入失球 及 客客場（各±3）'))
    dirs = [c['dir'] for c in conds]
    if any(d is None for d in dirs):
        return {'pass': False, 'conds': conds,
                'fail_note': '有條件未有方向（數據不足或未過50%）'}
    if len(set(dirs)) != 1:
        return {'pass': False, 'conds': conds,
                'fail_note': f'方向唔一致：{dirs}'}
    direction = dirs[0]
    # 淺深分析（30項語義：上賽完全相同樣本）
    gap = item_gap_v3(ctx, '30', ctx['m16']) if ctx['m16'] is not None else None
    return {'pass': True, 'direction': direction, 'conds': conds, 'gap30': gap}
