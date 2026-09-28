# -*- coding: utf-8 -*-
"""精選7／精選8／精選12 格引擎（2026-09-28 減格實驗定稿）。

12 格：1A 1G 1I 1O 3A 3G 3I 3O 7 19 21 25
  1X＝同初盤&水位＋同尾盤；3X＝同開賽前30分鐘&水位＋同尾盤
  A＝淨主淨客·全庫 G＝淨主淨客·同一聯賽 I＝互換·全庫 O＝互換·同一聯賽
  7＝同初盤純盤口；19＝上賽完全相同（同主客）；21＝主主場及客客場勝和負±7%
  25＝主主場及客客場入失球±3
方向判定同減格實驗：n≥5 且 上或下 >49.99%。
精選7＝7/19/21/25 有共識＋1I、3A、3I 全部同方向（驗證期下盤70.0%）。
精選8＝7/19/21/25 有共識＋1I、1O、3A、3I 全部同方向。
精選12＝全部 12 格有方向且一致。
合格後附 29/31/33/35 相同尾盤盤口嚴格版回查。
"""
import numpy as np

import v2_engine as v2
import v3_engine as v3

GRID12 = ['1A', '1G', '1I', '1O', '3A', '3G', '3I', '3O', '7', '19', '21', '25']
GRID8 = ['1I', '1O', '3A', '3I', '7', '19', '21', '25']
GRID7 = ['1I', '3A', '3I', '7', '19', '21', '25']
MIN_N = 5

CELL_NAMES = {
    '1A': '同初盤&水位＋同尾盤（淨主淨客·全庫）',
    '1G': '同初盤&水位＋同尾盤（淨主淨客·同一聯賽）',
    '1I': '同初盤&水位＋同尾盤（互換·全庫）',
    '1O': '同初盤&水位＋同尾盤（互換·同一聯賽）',
    '3A': '同30分鐘&水位＋同尾盤（淨主淨客·全庫）',
    '3G': '同30分鐘&水位＋同尾盤（淨主淨客·同一聯賽）',
    '3I': '同30分鐘&水位＋同尾盤（互換·全庫）',
    '3O': '同30分鐘&水位＋同尾盤（互換·同一聯賽）',
    '7': '同初盤純盤口',
    '19': '上賽完全相同（同主客）',
    '21': '主主場及客客場勝和負±7%',
    '25': '主主場及客客場入失球±3',
}


def _dir_of(oc):
    if not oc or oc['n'] < MIN_N or oc['up_r'] is None:
        return None
    if oc['up_r'] > 0.4999:
        return 'up'
    if oc['down_r'] > 0.4999:
        return 'down'
    return None


def _pack(df, m):
    oc = v2._oc_mask(df, m)
    d = _dir_of(oc)
    out = {'dir': d, 'n': oc['n'] if oc else 0}
    if oc:
        out.update({'up_r': oc['up_r'], 'down_r': oc['down_r'],
                    'push': oc['push']})
    return out


def grid_report(conn, t, cells=None):
    """計算指定格組合嘅方向報告。cells=None→12格；'7'→精選7格。"""
    want = {'7': GRID7, '8': GRID8}.get(cells, GRID12)
    ctx = v3.build_context(conn, t)
    df = ctx['df']
    if ctx['T_close'] is None:
        return {'error': '今場缺少尾盤數據'}
    scopes = v2._scope_masks(df, t.get('league'))
    sc_all, sc_lg = scopes['全庫'], scopes['同一聯賽']

    masks = {}
    for grp, key in (('1', '1'), ('3', '3')):
        _T, _pfx, _nm, base, base_sw = ctx['tp'][key]
        masks[grp + 'A'] = base & sc_all if base is not None else None
        masks[grp + 'G'] = base & sc_lg if base is not None else None
        masks[grp + 'I'] = base_sw & sc_all if base_sw is not None else None
        masks[grp + 'O'] = base_sw & sc_lg if base_sw is not None else None
    T1 = ctx['tp']['1'][0]
    s1 = v3._signed_T(T1) if T1 else None
    masks['7'] = v3._eq_signed_line(df, 'i', s1) if s1 is not None else None
    masks['19'] = ctx['m16']
    masks['21'] = ctx['cond']['f6']
    masks['25'] = ctx['cond']['g8']

    info = {}
    for cn in GRID12:
        if cn in masks and masks[cn] is not None:
            info[cn] = _pack(df, masks[cn])
        else:
            info[cn] = {'dir': None, 'n': 0}

    dirs = [info[cn]['dir'] for cn in want]
    mand = ['7', '19', '21', '25']
    mand_dirs = [info[cn]['dir'] for cn in mand]
    pass_ = None
    if any(d is None for d in mand_dirs):
        pass_ = False
        fail = '必填格 7/19/21/25 有格未有方向'
    elif len(set(mand_dirs)) != 1:
        pass_ = False
        fail = '必填格方向唔一致'
    elif any(info[cn]['dir'] is None for cn in want):
        pass_ = False
        fail = '有格未有方向'
    elif len(set(dirs)) != 1:
        pass_ = False
        fail = '格方向唔一致'
    else:
        pass_ = True
        fail = None
    direction = dirs[0] if pass_ else None

    # 29/31/33/35 相同尾盤盤口嚴格版回查（合格與否都計，Check 頁用）
    same = ctx['same_close']
    strict = {}
    if same is not None:
        andm = None
        for cn in want:
            if masks.get(cn) is None:
                andm = None
                break
            andm = masks[cn] if andm is None else andm & masks[cn]
        for rn, mm in (('29', ctx['m16']), ('31', ctx['m31']),
                       ('33', ctx['m33']), ('35', ctx['cond']['g8'])):
            if andm is None or mm is None:
                strict[rn] = None
                continue
            oc = v2._oc_mask(df, andm & mm & same)
            strict[rn] = oc

    # 相同情況回查（Check 頁用）：格組合 AND 樣本嘅四個口徑，
    # 每口徑 綜合上/下/走 ＋ 12 段水位區域。
    # 1X/3X 格有互換版（base_sw），7/19/21/25 冇（兩版相同）。
    # 缺數據嘅格（mask None）唔計入，但會列出用咗邊啲格（cb_cells）。
    lb = None
    tp1, tp3 = ctx['tp']['1'], ctx['tp']['3']

    def _cell_ms(cn):
        if cn[0] in ('1', '3') and cn[1:] in ('A', 'G', 'I', 'O'):
            _tp = tp1 if cn[0] == '1' else tp3
            return _tp[3], _tp[4]
        m = masks.get(cn)
        return m, m

    _mp = _ms = None
    _used = []
    for cn in want:
        p, s = _cell_ms(cn)
        if p is None or s is None:
            continue
        _used.append(cn)
        _mp = p if _mp is None else _mp & p
        _ms = s if _ms is None else _ms & s
    if _mp is not None and len(_used) >= 2:
        def _lb_pack2(mm):
            return {'oc': v2._oc_mask(df, mm), 'water': v2._water12(df, mm)}
        lb = {'all': _lb_pack2(_ms & sc_all), 'pure': _lb_pack2(_mp & sc_all),
              'lg_all': _lb_pack2(_ms & sc_lg),
              'lg_pure': _lb_pack2(_mp & sc_lg),
              'cells_used': _used}

    # 30 項語義淺深（上賽完全相同樣本：最多盤口／最接近50%盤 vs 今場尾盤）
    gap = v3.item_gap_v3(ctx, '30', ctx['m16']) if ctx['m16'] is not None \
        else None
    # 情境歸類（Check 一下用）：格方向 × 現時盤vs最多盤口 × 現時盤vs最接近50%盤口
    scenario = None
    if gap is not None and gap.get('mode') and gap.get('d50') and \
            ctx['T_close'] is not None:
        T_close = ctx['T_close']
        cur_s = T_close['h'] * {'home': 1, 'away': -1, 'none': 0}.get(
            T_close.get('g') or 'none', 0)

        def _s(pk):
            if not pk:
                return None
            return pk['h_g'] * {'home': 1, 'away': -1, 'none': 0}.get(
                pk.get('g_g') or 'none', 0)

        def _st(ref_s):
            if ref_s is None:
                return None
            dd = cur_s - ref_s
            if abs(dd) < 1e-9:
                return 'same'
            return 'deep' if dd > 0 else 'shallow'

        scenario = {'dir': direction,
                    'mode_gap': _st(_s(gap.get('mode'))),
                    'd50_gap': _st(_s(gap.get('d50')))}
        if scenario['dir'] and scenario['mode_gap'] and scenario['d50_gap']:
            scenario['key'] = '{}|{}|{}'.format(
                scenario['dir'], scenario['mode_gap'], scenario['d50_gap'])
        else:
            scenario['key'] = None

    cells_out = [{'cell': cn, 'name': CELL_NAMES[cn], **info[cn]}
                 for cn in GRID12]
    grid_no = cells if cells in ('7', '8') else '12'
    return {'pass': pass_, 'direction': direction, 'fail': fail,
            'cells': cells_out, 'strict': strict, 'lb': lb, 'gap30': gap,
            'scenario': scenario,
            'grid': grid_no, 'grid_cells': want}
