# -*- coding: utf-8 -*-
"""主客對調公式 v5（公式規格_主客對調.txt，2026-09-26）

原理：將「主場優勢」由 A 隊搬去 B 隊——盤口跟住移 2 倍主場優勢，水位跟住新盤口重計。

  第 1 步｜定 H：聯賽賽事用該聯賽 H_L；杯賽用兩隊所屬聯賽 H_L 平均；中立場 H=0
  第 2 步｜對調盤口：h_新 = 2×H − h_舊（h 以「主隊角度」計：正=主讓，負=主受讓，0=平手）
           中立場：h_新 = −h_舊（純鏡像）
  第 3 步｜水位（v4，對調後重新計）：w_主 = 1/(O×p) − 1；w_客 = 1/(O×(1−p)) − 1
           p = 新盤口下主隊贏盤概率，由該聯賽淨勝球分佈 F_L 推得
  第 4 步｜節點處理：初盤／各即時節點／尾盤各自做第 2、3 步

盤口量化：h_新 量化到最接近嘅 0.25 格（資料庫盤口全部係 0.25 嘅倍數），
p 用量化後嘅盤口計。

O（overround）注意：規格文件寫「易胜博預設 O = 1.081（兩邊水位和 1.85：2 ÷ 1.85 = 1.081）」——
呢個推導本身內部矛盾：2÷1.85 確係 1.081，但將 O=1.081 代入第 3 步公式，p=0.5 時
兩邊水位得 0.85/0.85（和 1.70），唔係文件講嘅 1.85。要令 p=0.5 出到 0.925/0.925
（和 1.85，即文件嘅校驗錨），O 必須係 2÷1.925 = 1.0390。故此處 O 用 1.0390
（易胜博），或按實測兩邊水位計（馬會式）。呢個偏差已同用戶標明。
"""
import os
import sqlite3

# ---- 第四節：H_L 變數數值表（football.db 全庫計，2026-09-26 快照）----
# key = competitions.name_tc
H_TABLE = {
    '英超': 0.267, '英冠': 0.290, '英甲': 0.268, '英乙': 0.230,
    '意甲': 0.178, '西甲': 0.361, '西乙': 0.348, '德甲': 0.372, '德乙': 0.281,
    '法甲': 0.261, '法乙': 0.234, '葡超': 0.269, '蘇超': 0.363,
    '荷甲': 0.331, '荷乙': 0.331, '比甲': 0.302, '瑞典超': 0.295, '芬超': 0.183,
    '挪超': 0.421, '丹麥超': 0.169, '奧甲': 0.222, '瑞士超': 0.332, '俄超': 0.333,
    '阿甲': 0.297, '烏拉甲': 0.164, '巴西甲': 0.400, '巴西乙': 0.407, '巴聖錦標': 0.357,
    '智利甲': 0.311, '墨西聯': 0.276, '墨西甲': 0.549, '美職業': 0.442, '美冠聯': 0.342,
    '日職聯': 0.214, '日職乙': 0.124, '韓K聯': 0.136, '韓K2聯': 0.097, '澳超': 0.267,
    '歐國聯': 0.331, '歐青U21外': 0.304,
    '澳南超': 0.191, '澳塔超': 0.098, '澳威超': 0.179, '澳布超': 0.152, '澳昆超': 0.076,
    '澳維超': 0.180, '澳西超': 0.103, '澳首超': 0.253,
}

# 易胜博（公司12）水位和 1.85 → 賠率 1.925/邊 → O = 2/1.925（見模組 docstring 偏差說明）
O_EASYBETS = 2.0 / 1.925   # = 1.0390

_h_memo = {}


def league_H(conn, comp_titan_id, name_tc=None):
    """聯賽主場優勢 H_L：先查規格表（name_tc），冇就全庫計
    AVG(主隊得分−客隊得分)（剔中立場）。結果記憶化。"""
    key = (comp_titan_id, name_tc)
    if key in _h_memo:
        return _h_memo[key]
    h = H_TABLE.get(name_tc) if name_tc else None
    if h is None:
        row = conn.execute(
            'SELECT AVG(m.home_score - m.away_score) FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'WHERE s.titan_id=? AND m.home_score IS NOT NULL '
            'AND COALESCE(m.is_neutral,0)=0', (comp_titan_id,)).fetchone()
        h = float(row[0]) if row and row[0] is not None else 0.30
    _h_memo[key] = h
    return h


def cup_H(conn, home_league, away_league):
    """杯賽：兩隊各自聯賽 H_L 嘅平均（原始 H 被種子編排扭曲，唔直接用）。"""
    return (league_H(conn, *home_league) + league_H(conn, *away_league)) / 2.0


def quantize(h, grid=0.25):
    """量化到最接近嘅 0.25 格（半格盤用）。0.344→0.25，0.284→0.25。"""
    return round(h / grid) * grid


def _cover1(m, h):
    """單邊（半格或整格）盤嘅投注回報比例：1=全贏 0.5=走盤(半注回) 0=全輸。
    m=主隊淨勝球（整數），h=主隊讓球數（0.5 倍數或整數）。"""
    d = m - h
    if d > 0:
        return 1.0
    if d < 0:
        return 0.0
    return 0.5     # 讓整數球剛好贏相同球數 → 走盤


def cover_prob(margins, h):
    """p = 新盤口 h 下主隊贏盤概率。margins = 淨勝球分佈 [(margin, count), ...]。
    0.25/0.75 盤拆兩半注各計。"""
    h = round(h * 4) / 4
    total = sum(c for _, c in margins)
    if total == 0:
        return 0.5
    quarter = round((h % 0.5) * 4) == 1    # .25 / .75
    hs = [h - 0.25, h + 0.25] if quarter else [h]

    def avg_p(line):
        win = sum(c for m, c in margins if _cover1(m, line) == 1.0)
        push = sum(c for m, c in margins if _cover1(m, line) == 0.5)
        return (win + 0.5 * push) / total
    return sum(avg_p(x) for x in hs) / len(hs)


_margins_memo = {}


def league_margins(conn, comp_titan_id):
    """聯賽淨勝球分佈 F_L：{margin: 場數}（全部賽果；記憶化）。"""
    if comp_titan_id in _margins_memo:
        return _margins_memo[comp_titan_id]
    rows = conn.execute(
        'SELECT m.home_score - m.away_score, COUNT(*) FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'WHERE s.titan_id=? AND m.home_score IS NOT NULL '
        'GROUP BY 1', (comp_titan_id,)).fetchall()
    margins = [(int(m), int(c)) for m, c in rows]
    _margins_memo[comp_titan_id] = margins
    return margins


def swap_handicap(h_old, H, neutral=False):
    """第 2 步：對調盤口。h_old 以「上一次對賽主隊角度」計（正=主讓）。
    回傳 (h_new_raw, h_new_q)：前者係 2H−h 原始值，後者量化到 0.25。"""
    if neutral:
        h_new = -h_old
    else:
        h_new = 2.0 * H - h_old
    return h_new, quantize(h_new)


def swap_waters(p, O=None):
    """第 3 步：v4 水位公式。回傳 (w_主, w_客)。"""
    O = O or O_EASYBETS
    p = min(0.95, max(0.05, p))
    return 1.0 / (O * p) - 1.0, 1.0 / (O * (1.0 - p)) - 1.0


def convert(conn, comp_titan_id, name_tc, h_old_from_prev_home,
            prev_home_is_cur_home, neutral=False, O=None,
            margins=None, H=None):
    """一條龍：上一次對賽嘅盤（以當時主隊角度 h_old）→ 今場主隊角度嘅對比盤。

    h_old_from_prev_home：上一次對賽「當時主隊」角度讓球數
        （giver='home' → +handicap；'away' → −handicap；平手 → 0.0）
    prev_home_is_cur_home：上一次對賽嘅主隊係咪今場主隊（同主客就係 True，唔使對調）
    回傳 dict：
        same    同主客（唔使對調）
        h/g     量化後對比盤（今場主隊角度：正=今場主讓）同 giver（'home'/'away'/None）
        ho/ao   對應主/客水位（O 代入 v4 公式；中立場=鏡像水位）
        H       用咗嘅主場優勢
        p       新盤下今場主隊贏盤概率
    """
    if H is None:
        H = league_H(conn, comp_titan_id, name_tc)
    if prev_home_is_cur_home and not neutral:
        # 同主客：唔使對調，直用原始盤（水位照舊）
        h_q = quantize(h_old_from_prev_home)
        g = 'home' if h_q > 0 else ('away' if h_q < 0 else None)
        return {'same': True, 'h': h_q, 'g': g, 'ho': None, 'ao': None,
                'H': H, 'p': None, 'raw': h_old_from_prev_home}
    h_raw, h_q = swap_handicap(h_old_from_prev_home, H, neutral)
    g = 'home' if h_q > 0 else ('away' if h_q < 0 else None)
    if neutral:
        # 純鏡像：水位直接對調（用原始水位喺呼叫處理；此處只俾盤）
        return {'same': False, 'h': h_q, 'g': g, 'ho': None, 'ao': None,
                'H': 0.0, 'p': None, 'raw': h_raw}
    if margins is None:
        margins = league_margins(conn, comp_titan_id)
    p = cover_prob(margins, h_q)
    w_home, w_away = swap_waters(p, O)
    return {'same': False, 'h': h_q, 'g': g, 'ho': round(w_home, 4),
            'ao': round(w_away, 4), 'H': H, 'p': round(p, 4), 'raw': h_raw}


if __name__ == '__main__':
    """規格文件速查例自檢（唔使用數據庫，淨係驗公式）"""
    # 例 1｜英超 H=0.267，A 主場讓 0.25 → h_新 = 0.534−0.25 = 0.284 ≈ 讓平半
    raw, q = swap_handicap(0.25, 0.267)
    assert abs(raw - 0.284) < 1e-9 and q == 0.25, (raw, q)
    # 例 2｜阿甲 H=0.297 → h_新 = 0.594−0.25 = 0.344 → 量化 0.25（顯示註明 0.25-0.5 之間）
    raw, q = swap_handicap(0.25, 0.297)
    assert abs(raw - 0.344) < 1e-9 and q == 0.25, (raw, q)
    # 例 3｜中立場：A 讓 0.5 → 對調後 B 讓 0.5
    raw, q = swap_handicap(0.5, 0.0, neutral=True)
    assert raw == -0.5 and q == -0.5
    # 水位：p=0.5 → 0.925/0.925（和 1.85，符合文件校驗錨）
    wh, wa = swap_waters(0.5)
    assert abs(wh - 0.925) < 0.001 and abs(wa - 0.925) < 0.001, (wh, wa)
    # 水位隨 p 單調：p 升 → 主水跌客水升
    w1 = swap_waters(0.55)
    assert w1[0] < wh and w1[1] > wa
    # cover_prob：整數淨勝球分佈，讓 0.5 → P(m>=1)；讓 1 → P(m>=2)+0.5·P(m==1)
    mg = [(0, 30), (1, 30), (2, 40)]
    assert abs(cover_prob(mg, 0.5) - 0.7) < 1e-9
    assert abs(cover_prob(mg, 1.0) - (0.4 + 0.15)) < 1e-9
    assert abs(cover_prob(mg, 0.25) - (0.5 * 0.85 + 0.5 * 0.7)) < 1e-9
    print('✓ swap_v5 公式自檢全部通過')
