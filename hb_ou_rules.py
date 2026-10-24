# -*- coding: utf-8 -*-
"""好波大細規則引擎（2026-10-22 用戶規格重建）：
逐聯賽獨立篩選——用歷史庫所有相關欄位（大小線各數值／初終盤變動／
臨場變動／大小球水位／聯賽場均／主客隊場均總入球／主客防守／讓球
方向／排名差）挖掘組合條件；訓練期 ≤2024 ＋ 驗證期 2025+ 兩邊
命中率都 ≥82.5% 先收錄（n：訓練≥10 驗證≥5）。唔場場預測——規則
命中先出聲，觀望係常態。

本模組係『單一真相來源』：hb_mine.py（礦工）同 haobao_engine.predict
（運行時）都用呢度嘅特徵構建同規則評估，邏輯上不可能漂移。
"""
import json
import os
import time

WS = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(WS, 'haobao_ou_rules.json')

THRESH = 0.825          # 用戶規格：兩期都 ≥82.5%
TRAIN_MAX = '2024-12-31'
MIN_TRAIN_N = 10
MIN_TEST_N = 5

_rules = None
_rules_ts = 0


def load_rules(force=False):
    global _rules, _rules_ts
    if not force and _rules and time.time() - _rules_ts < 300:
        return _rules
    try:
        with open(RULES_PATH, encoding='utf-8') as f:
            d = json.load(f)
        # 2026-10-22 修正：檔案結構係 {'meta':…, 'leagues': {lg: [rules]}}——
        # 本函數回傳 leagues 字典（eval_rules 直接用 .get(league)）
        _rules = d.get('leagues') if isinstance(d, dict) else {}
        if not isinstance(_rules, dict):
            _rules = {}
    except Exception:
        _rules = {}
    _rules_ts = time.time()
    return _rules


def load_meta():
    try:
        with open(RULES_PATH, encoding='utf-8') as f:
            return json.load(f).get('meta') or {}
    except Exception:
        return {}


# ---------- 特徵構建（礦工／運行時共用） ----------
def feats(line=None, init_line=None, over=None, under=None, x8dir=None, jxdir=None,
          init_over=None, pre4h_line=None,
          lg_avg_total=None,
          h_avg_total=None, a_avg_total=None,
          h_ga=None, a_ga=None,
          sline=None, rank_h=None, rank_a=None):
    """由原始欄位值構建原子條件集合（set of 'key=value'）。
    line/init_line/pre4h_line＝大小線；over/under＝尾盤大小水；
    init_over＝初盤大球水；sline＝帶符號讓球（主隊角度）；
    rank_h/rank_a＝總排名（細數字＝名次高）。任何欄位缺 → 對應條件不出現。"""
    F = set()
    if line is not None:
        F.add('line=%.2f' % (round(line * 2) / 2))
        if init_line is not None:
            mv = round(line * 2) / 2 - round(init_line * 2) / 2
            if mv <= -0.5:
                F.add('move=dn1')
            elif mv == -0.25:
                F.add('move=dn05')
            elif mv == 0:
                F.add('move=flat')
            elif mv == 0.25:
                F.add('move=up05')
            elif mv >= 0.5:
                F.add('move=up1')
        if pre4h_line is not None:
            lm = round(pre4h_line * 2) / 2 - round(line * 2) / 2
            if lm >= 0.25:
                F.add('late=dn')
            elif lm <= -0.25:
                F.add('late=up')
    if over is not None:
        if over < 0.85:
            F.add('cow=lo')
        elif over <= 0.98:
            F.add('cow=mid')
        else:
            F.add('cow=hi')
    if under is not None:
        if under < 0.85:
            F.add('cuw=lo')
        elif under <= 0.98:
            F.add('cuw=mid')
        else:
            F.add('cuw=hi')
    if init_over is not None:
        if init_over < 0.85:
            F.add('ioc=lo')
        elif init_over <= 0.98:
            F.add('ioc=mid')
        else:
            F.add('ioc=hi')
    if lg_avg_total is not None:
        if lg_avg_total < 2.6:
            F.add('lg=lo')
        elif lg_avg_total >= 2.9:
            F.add('lg=hi')
    if h_avg_total is not None:
        if h_avg_total >= 3.0:
            F.add('havg=hi')
        elif h_avg_total <= 2.3:
            F.add('havg=lo')
    if a_avg_total is not None:
        if a_avg_total >= 3.0:
            F.add('aavg=hi')
        elif a_avg_total <= 2.3:
            F.add('aavg=lo')
    if h_ga is not None and h_ga <= 1.0:
        F.add('hga=lo')
    if a_ga is not None and a_ga <= 1.0:
        F.add('aga=lo')
    if sline is not None:
        if sline >= 1.0:
            F.add('sline=hd')
        elif sline <= -1.0:
            F.add('sline=ad')
        elif abs(sline) < 0.25:
            F.add('sline=eq')
    # 2026-10-24 用戶驗證：8X／新精選 方向作為原子條件（有預測先計）
    if x8dir in ('up', 'down'):
        F.add('x8=' + x8dir)
    if jxdir in ('up', 'down'):
        F.add('jx=' + jxdir)
    if rank_h is not None and rank_a is not None:
        if rank_h + 5 <= rank_a:
            F.add('rankd=h5')
        elif rank_a + 5 <= rank_h:
            F.add('rankd=a5')
    return F


def best_pick(hits):
    """2026-10-22 優化版（用戶：淨係高命中率高機會）：
    出「精選」要過雙重門檻——
    ①單條獨立：min(訓,驗) ≥ 88%；或
    ②共識：≥2 條同向規則同時命中（每條本身已 ≥82.5% 收錄閘）。
    淨係 82.5-88% 嘅單條命中 → 出 (side, rule, False)＝『參考訊號』
    （lean 顯示、唔出精選、唔記錄 hb_pick_log）。
    回傳 (side, best_rule, is_pick)；冇命中 → None。"""
    if not hits:
        return None
    def key(r):
        return (min(r.get('train_hit') or 0, r.get('test_hit') or 0),
                r.get('all_n') or 0)
    by_side = {}
    for r in hits:
        by_side.setdefault(r['side'], []).append(r)
    best_side, best_rules = max(by_side.items(),
                                key=lambda kv: max(key(r) for r in kv[1]))
    top = max(best_rules, key=key)
    top_min = min(top.get('train_hit') or 0, top.get('test_hit') or 0)
    if top_min >= 0.88:
        return best_side, top, True
    if len(best_rules) >= 2:
        # 共識：最強嗰條做代表（顯示用），但標明共識場數
        top = dict(top)
        top['label'] = '%s（%d 條規則共識）' % (top.get('label') or '',
                                               len(best_rules))
        return best_side, top, True
    return best_side, top, False


def live_gate_paused(rule_label):
    """2026-10-22：live 健康度門——規則實戰 n≥6 且命中率 <70% 自動停用
    （hb_rule_gate.json，由 settle 後聚合更新）。停用規則唔出預測——
    保證『高命中率』唔會隨時間腐化。回傳 True＝已停用。"""
    try:
        with open(os.path.join(WS, 'hb_rule_gate.json'), encoding='utf-8') as f:
            return rule_label in (json.load(f).get('paused') or [])
    except Exception:
        return False


def eval_rules(league, F):
    """評一場：回傳命中規則列表 [{side, conds, all_hit, all_n, label}]。
    冇規則庫／冇命中 → []。live 停用嘅規則自動過濾。"""
    out = []
    for r in load_rules().get(league) or []:
        conds = set(r['conds'])
        if conds and conds <= F and not live_gate_paused(r.get('label')):
            out.append(r)
    return out
