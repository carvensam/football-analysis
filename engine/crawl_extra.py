# -*- coding: utf-8 -*-
"""擴充數據爬蟲：每場 16 項任務，支援暫停/繼續/停止與進度匯報。

每場任務（market）：
  ou            全場大小球七時點   vip changeDetail/overunder.aspx   易胜博(12)
  ah_half       半場亞洲盤七時點   vip changeDetail/HandicapHalf.aspx 易胜博(12)
  ou_half       半場大小球七時點   vip changeDetail/OverUnderHalf.aspx 易胜博(12)
  1x2           全場歐洲盤七時點   vip changeDetail/1x2.aspx         易胜博(12)
  1x2_half      半場歐洲盤七時點   vip changeDetail/1x2Half.aspx     Crown(3)
  corner        角球讓球七時點     vip changeDetail/corner.aspx      Crown(3)
  hkjc_asian    馬會亞洲盤         vip changeDetail/handicap.aspx    香港馬會(48)
  hkjc_ou       馬會全場大小球     vip changeDetail/overunder.aspx   香港馬會(48)
  hkjc_ah_half  馬會半場亞洲盤     vip changeDetail/HandicapHalf.aspx 香港馬會(48)
  hkjc_ou_half  馬會半場大小球     vip changeDetail/OverUnderHalf.aspx 香港馬會(48)
  hkjc_1x2      馬會全場歐洲盤     vip changeDetail/1x2.aspx         香港馬會(48)
  hkjc_1x2_half 馬會半場歐洲盤     vip changeDetail/1x2Half.aspx     香港馬會(48)
  1x2all        百家歐指+凱利+返還率+中立場  1x2d.titan007.com/{id}.js
  corners_stat  角球四項統計       bf.titan007.com/detail/{id}.htm

香港馬會：主盤 line_idx=0；同場多盤（選擇2/3/4）逐條以 &l=1/2/3… 探取直至無數據。
亞盤不是每場都有——有效來源證明無盤才標無數據；網絡失敗保留待重試。
半場賽果已存 matches half_home/half_away。

控制：control.json {cmd: run|pause|stop}（GUI 寫入，引擎每個請求前讀取）
狀態：status.json （引擎每 5 秒寫入，GUI 顯示；工作列標題同步）

用法：
  python crawl_extra.py            正常續爬（補齊所有未完成任務）
  python crawl_extra.py --limit 3  只爬 3 場（測試）
  python crawl_extra.py --reconcile-cloud  全庫對賬，重查 cloud_done 待辦
"""
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import threading
import time
import random
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from crawler import (Fetcher, SNAPSHOTS, db_connect, derive_snapshots,  # noqa: E402
                     line_to_float, log, parse_odds_html)
from engine.extra_schema import migrate  # noqa: E402

CONTROL = os.path.join(BASE_DIR, 'control.json')
STATUS = os.path.join(BASE_DIR, 'status.json')

COMPANY_12BET = 12   # 易胜博
COMPANY_CROWN = 3    # Crown（半場歐盤/角球盤提供者）
COMPANY_HKJC = 48    # 香港馬會（changeDetail 盤口頁專用 ID；聯賽頁下拉為 432）

HKJC_MARKETS = ('hkjc_asian', 'hkjc_ou', 'hkjc_ah_half', 'hkjc_ou_half',
                'hkjc_1x2', 'hkjc_1x2_half')

MARKET_NAMES = {
    'asian': '亞洲盤（缺口補取）',
    'ou': '全場大小球盤', 'ah_half': '半場亞洲盤', 'ou_half': '半場大小球盤',
    '1x2': '全場歐洲主客和盤', '1x2_half': '半場歐洲主客和盤',
    'corner': '角球讓球盤', '1x2all': '百家歐指及凱利指數',
    'corners_stat': '角球統計', 'goals': '入球時間（逐球）',
    'hkjc_asian': '馬會亞洲盤', 'hkjc_ou': '馬會全場大小球盤',
    'hkjc_ah_half': '馬會半場亞洲盤', 'hkjc_ou_half': '馬會半場大小球盤',
    'hkjc_1x2': '馬會全場歐洲主客和盤', 'hkjc_1x2_half': '馬會半場歐洲主客和盤',
}
# 用戶 2026-09-29 指定優先次序：1st 易胜博七時點 → 2nd 角球統計 →
# 3rd 百家歐指 → 4th 入球時間 → 5th 馬會六盤。
# 線道共用一條優先隊列：邊條線做完手頭自然幫其他線，直至全部完成。
from engine.extra_schema import PRIORITY_CASE_SQL as PRIORITY_SQL
from engine.extra_schema import PRIORITY_ORD_CASE as PRIORITY_ORD_SQL


MARKET_ORDER = ['asian', 'ou', 'ah_half', 'ou_half', '1x2', '1x2_half', 'corner',
                'hkjc_asian', 'hkjc_ou', 'hkjc_ah_half', 'hkjc_ou_half',
                'hkjc_1x2', 'hkjc_1x2_half',
                '1x2all', 'corners_stat', 'goals']

# 每個市場對應嘅來源主機：主機斷線（冷卻期）時成組暫停派任，
# 避免逐任務 10 秒逾時空轉，仲誤標「三次未取得＝無數據」
MARKET_HOST = {m: 'vip.titan007.com' for m in
               ('ou', 'ah_half', 'ou_half', '1x2', '1x2_half', 'corner',
                'asian') + HKJC_MARKETS}
MARKET_HOST.update({'1x2all': '1x2d.titan007.com',
                    'corners_stat': 'bf.titan007.com',
                    'goals': 'live.titan007.com'})

CELL_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.S | re.I)
ROW_RE = re.compile(r'<TR[^>]*>(.*?)</TR>', re.S | re.I)
TAG_RE = re.compile(r'<[^>]+>')

# 斷線等週期嘅主動試探 URL（輕量頁；對應 MARKET_HOST 嘅主機）
_HOST_PROBE_URLS = {
    'vip.titan007.com':
        'https://vip.titan007.com/changeDetail/handicap.aspx?id=1&companyid=12',
    '1x2d.titan007.com': 'https://1x2d.titan007.com/',
    'bf.titan007.com': 'https://bf.titan007.com/',
    'live.titan007.com': 'https://live.titan007.com/',
}


# ---------------------------------------------------------------- 通用解析
def parse_change_time(change_time, kickoff):
    """'MM-DD HH:MM' -> datetime（年份依開賽時間推算）"""
    m = re.match(r'(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', change_time)
    if not m:
        return None
    mo, d, h, mi = map(int, m.groups())
    year = kickoff.year
    try:
        cand = dt.datetime(year, mo, d, h, mi)
        if cand > kickoff:
            cand = dt.datetime(year - 1, mo, d, h, mi)
    except ValueError:
        # 2-29 閏日邊界：前一年無 2-29 就退一日（2026-10-25 crawl_mk 每開必崩嘅元兇）
        cand = dt.datetime(year - 1, mo, min(d, 28), h, mi)
    return cand


def rows_of(html):
    for rowm in ROW_RE.finditer(html):
        cells = [TAG_RE.sub('', c).strip() for c in CELL_RE.findall(rowm.group(1))]
        if cells:
            yield cells


def parse_ou_html(html, kickoff):
    """大小球頁（全場/半場通用）：時間/比分/大球/進球數/小球/變化時間/狀態"""
    timeline = []
    for cells in rows_of(html):
        if len(cells) < 6:
            continue
        _minute, _score, over_s, line_s, under_s, change_time = cells[:6]
        status = cells[6] if len(cells) > 6 else ''
        if '滚' in status:
            continue
        try:
            over, under = float(over_s), float(under_s)
        except ValueError:
            continue
        line = parse_line_number(line_s)
        if line is None:
            continue
        t = parse_change_time(change_time, kickoff)
        if t is None:
            continue
        timeline.append((t, line, over, under))
    timeline.sort(key=lambda x: x[0])
    return timeline


def parse_line_number(s):
    """'2.5' / '2.5/3' / '两球半' 之類 -> 數值（取平均）"""
    s = s.strip()
    if not s:
        return None
    if re.match(r'^\d+(\.\d+)?(/\d+(\.\d+)?)?$', s):
        vals = [float(x) for x in s.split('/')]
        return sum(vals) / len(vals)
    try:
        return float(line_to_float(s))
    except (TypeError, ValueError):
        return None


def parse_ah5_html(html, kickoff):
    """香港馬會亞洲盤 changeDetail 5 格格式：主隊/盘/客隊/變化時間/狀態
    （易胜博頁為 7 格含時間/比分欄，用 crawler.parse_odds_html；馬會頁無此兩欄）"""
    timeline = []
    for cells in rows_of(html):
        if len(cells) != 5:
            continue
        home_s, line, away_s, change_time, status = cells
        if '滚' in status:
            continue
        try:
            ho = float(home_s)
            ao = float(away_s)
        except ValueError:
            continue
        hv = line_to_float(line)
        if hv is None:
            continue
        t = parse_change_time(change_time, kickoff)
        if t is None:
            continue
        timeline.append((t, hv, ho, ao))
    timeline.sort(key=lambda x: x[0])
    return timeline


def parse_ou5_html(html, kickoff):
    """馬會大小球 5 格格式：大球/进球数/小球/變化時間/狀態"""
    timeline = []
    for cells in rows_of(html):
        if len(cells) != 5:
            continue
        over_s, line_s, under_s, change_time, status = cells
        if '滚' in status:
            continue
        try:
            over, under = float(over_s), float(under_s)
        except ValueError:
            continue
        line = parse_line_number(line_s)
        if line is None:
            continue
        t = parse_change_time(change_time, kickoff)
        if t is None:
            continue
        timeline.append((t, line, over, under))
    timeline.sort(key=lambda x: x[0])
    return timeline


def parse_1x25_html(html, kickoff):
    """馬會歐洲盤 5 格格式：主勝/和局/客勝/變化時間/狀態"""
    timeline = []
    for cells in rows_of(html):
        if len(cells) != 5:
            continue
        home_s, draw_s, away_s, change_time, status = cells
        if '滚' in status:
            continue
        try:
            h, d, a = float(home_s), float(draw_s), float(away_s)
        except ValueError:
            continue
        t = parse_change_time(change_time, kickoff)
        if t is None:
            continue
        timeline.append((t, h, d, a))
    timeline.sort(key=lambda x: x[0])
    return timeline


def parse_live_goals(html):
    """live.titan007.com 事件時間軸 -> 入球列表 [(minute, period, side, type, player)]
    回傳 None 代表頁面無事件數據（無 time_line 項目）。
    新版頁面用 <li class='lists'>；2021 或之前嘅舊版頁面用「详细事件」表（5 欄 tr），
    minute 原文保存：45+N=上半場補時、90+N=下半場補時（唔加總）。"""
    items = re.findall(r"<li class='lists'>(.*?)</li>", html, re.S)
    if not items:
        old = _parse_live_goals_old(html)
        return old
    raw = []
    for it in items:
        m = re.search(r"<span>(\d+(?:\+\d+)?)'\s*</span>", it)
        if not m:
            continue
        minute = m.group(1)
        pre, post = it[:m.start()], it[m.end():]
        for side, seg in (('home', pre), ('away', post)):
            gt = re.search(r"bf_img2/\d+\.png' title='(入球|乌龙|点球)'", seg)
            if 'goalicon' not in seg and not gt:
                continue
            gtype = gt.group(1) if gt else '入球'
            pm = re.search(r">\s*([^<>]+?)\s*<i class=\"goalicon\">", seg)
            player = pm.group(1).strip() if pm else ''
            base_s, _, plus_s = minute.partition('+')
            base = int(base_s)
            period = 1 if base <= 45 else (2 if base <= 90 else 3)
            sort_key = base + (int(plus_s) / 100.0 if plus_s else 0.0)
            raw.append((sort_key, minute, period, side, gtype, player))
            break
    raw.sort(key=lambda x: x[0])
    return [(minute, period, side, gtype, player)
            for _k, minute, period, side, gtype, player in raw]


GOAL_TITLE_RE = re.compile(r"bf_img/\d+\.png' title='(入球|乌龙|点球)'")


def _parse_live_goals_old(html):
    """舊版「详细事件」表：每列 5 格——主隊球員/主隊圖標/時間/客隊圖標/客隊球員，
    入球圖標喺主隊第2格或客隊第4格。無此表回傳 None；有表但無入球回傳 []。"""
    mtab = re.search(r'<th\b[^>]*>\s*详细事件\s*</th>(.*?)</table>', html, re.S)
    if not mtab:
        return None
    raw = []
    for rowm in re.finditer(r'<tr[^>]*>(.*?)</tr>', mtab.group(1), re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', rowm.group(1), re.S)
        if len(cells) != 5:
            continue
        mm = re.fullmatch(r"(\d+(?:\+\d+)?)'?", TAG_RE.sub('', cells[2]).strip())
        if not mm:
            continue
        minute = mm.group(1)
        for side, player_cell, icon_cell in (('home', cells[0], cells[1]),
                                             ('away', cells[4], cells[3])):
            gt = GOAL_TITLE_RE.search(icon_cell)
            if not gt:
                continue
            pm = re.search(r"title='([^']+)'>", player_cell)
            player = pm.group(1).strip() if pm else ''
            base_s, _, plus_s = minute.partition('+')
            base = int(base_s)
            period = 1 if base <= 45 else (2 if base <= 90 else 3)
            sort_key = base + (int(plus_s) / 100.0 if plus_s else 0.0)
            raw.append((sort_key, minute, period, side, gt.group(1), player))
    raw.sort(key=lambda x: x[0])
    return [(minute, period, side, gtype, player)
            for _k, minute, period, side, gtype, player in raw]


def task_goals(conn, fetcher, match_id, kickoff):
    """每場入球時間（第一球、第二球…）：live.titan007.com/detail/{id}cn.htm"""
    url = 'https://live.titan007.com/detail/%dcn.htm' % match_id
    html = fetcher.get(url, referer='https://live.titan007.com/')
    if html is None or not html.strip():
        return False
    if html == 'NO_DATA':
        return 'NO_DATA'
    goals = parse_live_goals(html)
    if goals is None:
        return 'NO_DATA'
    conn.execute('''CREATE TABLE IF NOT EXISTS match_goals_empty(
        match_id INTEGER PRIMARY KEY, source TEXT, checked_at TEXT)''')
    if not goals:
        score = conn.execute(
            'SELECT home_score,away_score FROM matches WHERE id=?',
            (match_id,)).fetchone()
        if not score or score[0] is None or score[1] is None or sum(score) > 0:
            if score and score[0] is not None and score[1] is not None and sum(score) > 0:
                conn.execute('DELETE FROM match_goals_empty WHERE match_id=?',
                             (match_id,))
                conn.commit()
            return False
    conn.execute('DELETE FROM match_goals WHERE match_id=?', (match_id,))
    if not goals:
        conn.execute(
            'INSERT OR REPLACE INTO match_goals_empty(match_id,source,checked_at) '
            'VALUES(?,?,?)',
            (match_id, 'live.titan007.com',
             dt.datetime.now().isoformat(timespec='seconds')))
    else:
        conn.execute('DELETE FROM match_goals_empty WHERE match_id=?', (match_id,))
    for seq, (minute, period, side, gtype, player) in enumerate(goals, 1):
        conn.execute(
            'INSERT INTO match_goals(match_id,seq,minute,period,side,goal_type,'
            'player) VALUES(?,?,?,?,?,?,?)',
            (match_id, seq, minute, period, side, gtype, player or None))
    conn.commit()
    return True


def parse_1x2_html(html, kickoff):
    """歐洲盤頁（全場/半場通用）：時間/比分/主勝/和局/客勝/變化時間/狀態"""
    timeline = []
    for cells in rows_of(html):
        if len(cells) < 6:
            continue
        _minute, _score, home_s, draw_s, away_s, change_time = cells[:6]
        status = cells[6] if len(cells) > 6 else ''
        if '滚' in status:
            continue
        try:
            h, d, a = float(home_s), float(draw_s), float(away_s)
        except ValueError:
            continue
        t = parse_change_time(change_time, kickoff)
        if t is None:
            continue
        timeline.append((t, h, d, a))
    timeline.sort(key=lambda x: x[0])
    return timeline


def parse_1x2d_js(text):
    """解析 1x2d.titan007.com/{id}.js。
    回傳 (neutrality_or_None, [公司列...] 或 None)；公司列 = 27 欄位 list。"""
    m = re.search(r'neutrality="(\d)"', text)
    neutrality = int(m.group(1)) if m else None
    rows = []
    mg = re.search(r'game=Array\((.*?)\);', text, re.S)
    if not mg:
        return neutrality, None
    for item in re.findall(r'"((?:[^"\\]|\\.)*)"', mg.group(1)):
        f = item.split('|')
        rows.append(f)
    return neutrality, rows


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def save_1x2all(conn, match_id, rows):
    """公司列欄位（27）：
    0 companyId | 1 oddId | 2 name | 3-5 初賠 | 6-8 初概率 | 9 初返還 |
    10-12 即賠 | 13-15 即概率 | 16 即返還 | 17-19 即凱利 | 20 變化時間 |
    21 簡稱 | 22-23 旗標 | 24-26 初凱利"""
    now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    for f in rows:
        if len(f) < 21:
            continue
        try:
            cid = int(f[0])
        except ValueError:
            continue
        init_kelly = [fnum(x) for x in f[24:27]] if len(f) >= 27 else [None] * 3
        change_time = f[20].replace(',', '-').replace('-1,', '-01-').strip()
        # 時間格式 '2026,09-1,17,13,54,00' -> '2026-09-17 13:54'
        mt = re.match(r'(\d{4}),(\d{2})-(\d{1,2}),(\d{1,2}),(\d{1,2}),(\d{1,2})',
                      f[20])
        if mt:
            change_time = '%04d-%02d-%02d %02d:%02d' % (
                int(mt.group(1)), int(mt.group(2)), int(mt.group(3)),
                int(mt.group(4)), int(mt.group(5)))
        else:
            change_time = f[20]
        conn.execute(
            'INSERT INTO odds_1x2_all(match_id,company_id,company_name,'
            'init_home,init_draw,init_away,init_prob_home,init_prob_draw,'
            'init_prob_away,init_payout,init_kelly_home,init_kelly_draw,'
            'init_kelly_away,cur_home,cur_draw,cur_away,cur_prob_home,'
            'cur_prob_draw,cur_prob_away,cur_payout,cur_kelly_home,'
            'cur_kelly_draw,cur_kelly_away,change_time) VALUES(?,?,?,?,?,?,?,'
            '?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(match_id,company_id) DO UPDATE SET '
            'company_name=excluded.company_name,'
            'init_home=excluded.init_home,init_draw=excluded.init_draw,'
            'init_away=excluded.init_away,'
            'init_prob_home=excluded.init_prob_home,'
            'init_prob_draw=excluded.init_prob_draw,'
            'init_prob_away=excluded.init_prob_away,'
            'init_payout=excluded.init_payout,'
            'init_kelly_home=excluded.init_kelly_home,'
            'init_kelly_draw=excluded.init_kelly_draw,'
            'init_kelly_away=excluded.init_kelly_away,'
            'cur_home=excluded.cur_home,cur_draw=excluded.cur_draw,'
            'cur_away=excluded.cur_away,cur_prob_home=excluded.cur_prob_home,'
            'cur_prob_draw=excluded.cur_prob_draw,'
            'cur_prob_away=excluded.cur_prob_away,'
            'cur_payout=excluded.cur_payout,'
            'cur_kelly_home=excluded.cur_kelly_home,'
            'cur_kelly_draw=excluded.cur_kelly_draw,'
            'cur_kelly_away=excluded.cur_kelly_away,'
            'change_time=excluded.change_time',
            (match_id, cid, f[2],
             fnum(f[3]), fnum(f[4]), fnum(f[5]),
             fnum(f[6]), fnum(f[7]), fnum(f[8]), fnum(f[9]),
             init_kelly[0], init_kelly[1], init_kelly[2],
             fnum(f[10]), fnum(f[11]), fnum(f[12]),
             fnum(f[13]), fnum(f[14]), fnum(f[15]), fnum(f[16]),
             fnum(f[17]), fnum(f[18]), fnum(f[19]), change_time))
        n += 1
    return n


def parse_bf_corners(html):
    """bf detail 技術統計列：全場角球(主/客)、半場角球(主/客)"""
    out = {}
    for m in re.finditer(
            r"<div class='data'><span[^>]*>\s*(-?\d+)\s*</span><span>\s*"
            r"(角球|半場角球|半场角球)\s*</span><span[^>]*>\s*(-?\d+)\s*</span>",
            html):
        v1, name, v2 = int(m.group(1)), m.group(2), int(m.group(3))
        if '半' in name:
            out['ht_home'], out['ht_away'] = v1, v2
        else:
            out['ft_home'], out['ft_away'] = v1, v2
    labels = {'角球', '角球次数', '角球次數', '半場角球', '半场角球',
              '半場角球次数', '半场角球次数', '半場角球次數', '半场角球次數'}
    for cells in rows_of(html):
        for i, name in enumerate(cells):
            if name not in labels or i == 0 or i + 1 >= len(cells):
                continue
            try:
                v1, v2 = int(cells[i - 1]), int(cells[i + 1])
            except ValueError:
                continue
            if '半' in name:
                out['ht_home'], out['ht_away'] = v1, v2
            else:
                out['ft_home'], out['ft_away'] = v1, v2
            break
    return out


# ---------------------------------------------------------------- 存儲
UPSERTS = {
    'ou': ('odds_ou', 'total_line,over_odds,under_odds',
           '(?,?,?,?,?,?,?)',
           'total_line=excluded.total_line,over_odds=excluded.over_odds,'
           'under_odds=excluded.under_odds,change_time=excluded.change_time'),
    'ah_half': ('odds_ah_half', 'handicap,giver,home_odds,away_odds',
                '(?,?,?,?,?,?,?,?)',
                'handicap=excluded.handicap,giver=excluded.giver,'
                'home_odds=excluded.home_odds,away_odds=excluded.away_odds,'
                'change_time=excluded.change_time'),
    'ou_half': ('odds_ou_half', 'total_line,over_odds,under_odds',
                '(?,?,?,?,?,?,?)',
                'total_line=excluded.total_line,over_odds=excluded.over_odds,'
                'under_odds=excluded.under_odds,'
                'change_time=excluded.change_time'),
    '1x2': ('odds_1x2', 'home_odds,draw_odds,away_odds',
            '(?,?,?,?,?,?,?)',
            'home_odds=excluded.home_odds,draw_odds=excluded.draw_odds,'
            'away_odds=excluded.away_odds,change_time=excluded.change_time'),
    '1x2_half': ('odds_1x2_half', 'home_odds,draw_odds,away_odds',
                 '(?,?,?,?,?,?,?)',
                 'home_odds=excluded.home_odds,draw_odds=excluded.draw_odds,'
                 'away_odds=excluded.away_odds,'
                 'change_time=excluded.change_time'),
    'corner': ('odds_corner', 'handicap,giver,home_odds,away_odds',
               '(?,?,?,?,?,?,?,?)',
               'handicap=excluded.handicap,giver=excluded.giver,'
               'home_odds=excluded.home_odds,away_odds=excluded.away_odds,'
               'change_time=excluded.change_time'),
}


def save_snapshots(conn, market, match_id, company_id, snaps):
    table, cols, ph, upd = UPSERTS[market]
    for label, _mins in SNAPSHOTS:
        if label not in snaps:
            continue
        vals = snaps[label]
        t = vals[0].strftime('%Y-%m-%d %H:%M')
        rest = list(vals[1:])
        if market in ('ou', 'ou_half'):
            line, o1, o2 = rest
            params = (match_id, label, company_id, line, o1, o2, t)
        elif market in ('ah_half', 'corner'):
            hv, ho, ao = rest
            abs_v = abs(hv)
            giver = None if abs_v == 0 else ('away' if hv > 0 else 'home')
            params = (match_id, label, company_id, abs_v, giver, ho, ao, t)
        else:  # 1x2 / 1x2_half
            h, d, a = rest
            params = (match_id, label, company_id, h, d, a, t)
        conn.execute(
            'INSERT INTO %s(match_id,label,company_id,%s,change_time) VALUES%s '
            'ON CONFLICT(match_id,label,company_id) DO UPDATE SET %s'
            % (table, cols, ph, upd), params)


# ---------------------------------------------------------------- 香港馬會（company_id=48）
HKJC_PAGES = {'hkjc_asian': 'handicap.aspx', 'hkjc_ou': 'overunder.aspx',
              'hkjc_ah_half': 'HandicapHalf.aspx',
              'hkjc_ou_half': 'OverUnderHalf.aspx',
              'hkjc_1x2': '1x2.aspx', 'hkjc_1x2_half': '1x2Half.aspx'}
# 每種盤最多探幾條盤線（l=0 主盤；l=1/2/3…=選擇2/3/4；探到無數據即止）
HKJC_MAX_LINES = {'hkjc_asian': 4, 'hkjc_ou': 5, 'hkjc_ah_half': 2,
                  'hkjc_ou_half': 3, 'hkjc_1x2': 1, 'hkjc_1x2_half': 1}

HKJC_UPSERTS = {
    'hkjc_asian': ('hkjc_asian', 'handicap,giver,home_odds,away_odds',
                   '(?,?,?,?,?,?,?,?)',
                   'handicap=excluded.handicap,giver=excluded.giver,'
                   'home_odds=excluded.home_odds,away_odds=excluded.away_odds,'
                   'change_time=excluded.change_time'),
    'hkjc_ah_half': ('hkjc_ah_half', 'handicap,giver,home_odds,away_odds',
                     '(?,?,?,?,?,?,?,?)',
                     'handicap=excluded.handicap,giver=excluded.giver,'
                     'home_odds=excluded.home_odds,away_odds=excluded.away_odds,'
                     'change_time=excluded.change_time'),
    'hkjc_ou': ('hkjc_ou', 'total_line,over_odds,under_odds',
                '(?,?,?,?,?,?,?)',
                'total_line=excluded.total_line,over_odds=excluded.over_odds,'
                'under_odds=excluded.under_odds,change_time=excluded.change_time'),
    'hkjc_ou_half': ('hkjc_ou_half', 'total_line,over_odds,under_odds',
                     '(?,?,?,?,?,?,?)',
                     'total_line=excluded.total_line,over_odds=excluded.over_odds,'
                     'under_odds=excluded.under_odds,'
                     'change_time=excluded.change_time'),
    'hkjc_1x2': ('hkjc_1x2', 'home_odds,draw_odds,away_odds',
                 '(?,?,?,?,?,?,?)',
                 'home_odds=excluded.home_odds,draw_odds=excluded.draw_odds,'
                 'away_odds=excluded.away_odds,change_time=excluded.change_time'),
    'hkjc_1x2_half': ('hkjc_1x2_half', 'home_odds,draw_odds,away_odds',
                      '(?,?,?,?,?,?,?)',
                      'home_odds=excluded.home_odds,draw_odds=excluded.draw_odds,'
                      'away_odds=excluded.away_odds,'
                      'change_time=excluded.change_time'),
}


def save_hkjc_snapshots(conn, market, match_id, line_idx, snaps):
    table, cols, ph, upd = HKJC_UPSERTS[market]
    for label, _mins in SNAPSHOTS:
        if label not in snaps:
            continue
        vals = snaps[label]
        t = vals[0].strftime('%Y-%m-%d %H:%M')
        rest = list(vals[1:])
        if market in ('hkjc_ou', 'hkjc_ou_half'):
            line, o1, o2 = rest
            params = (match_id, label, line_idx, line, o1, o2, t)
        elif market in ('hkjc_asian', 'hkjc_ah_half'):
            hv, ho, ao = rest
            abs_v = abs(hv)
            giver = None if abs_v == 0 else ('away' if hv > 0 else 'home')
            params = (match_id, label, line_idx, abs_v, giver, ho, ao, t)
        else:  # hkjc_1x2 / hkjc_1x2_half
            h, d, a = rest
            params = (match_id, label, line_idx, h, d, a, t)
        conn.execute(
            'INSERT INTO %s(match_id,label,line_idx,%s,change_time) VALUES%s '
            'ON CONFLICT(match_id,label,line_idx) DO UPDATE SET %s'
            % (table, cols, ph, upd), params)


def task_hkjc(conn, fetcher, match_id, kickoff, market):
    """香港馬會盤口：逐條盤線探取（l=0 主盤，l=1…=選擇2/3/4）直至無數據。
    全部盤線都無數據 -> 'NO_DATA'（亞盤並非場場有，屬正常）。"""
    page = HKJC_PAGES[market]
    any_data = False
    first_timeline = None
    for line_idx in range(HKJC_MAX_LINES[market]):
        if line_idx == 0:
            url = ('https://vip.titan007.com/changeDetail/%s?id=%d&companyid=%d'
                   % (page, match_id, COMPANY_HKJC))
        else:
            url = ('https://vip.titan007.com/changeDetail/%s?id=%d&companyid=%d&l=%d'
                   % (page, match_id, COMPANY_HKJC, line_idx))
        html = fetcher.get(url, encoding='gbk', referer='https://zq.titan007.com/',
                           is_odds_page=True)
        if html is None:
            return False
        if html == 'NO_DATA':
            break
        if market in ('hkjc_ou', 'hkjc_ou_half'):
            timeline = parse_ou5_html(html, kickoff)
        elif market in ('hkjc_1x2', 'hkjc_1x2_half'):
            timeline = parse_1x25_html(html, kickoff)
        else:
            timeline = parse_ah5_html(html, kickoff)
        if not timeline:
            break
        if line_idx > 0 and timeline == first_timeline:
            # 服務器對超出實際盤線數的 &l=N 會重複回傳主盤 -> 無更多盤線
            break
        if line_idx == 0:
            first_timeline = timeline
        snaps = derive_snapshots(timeline, kickoff)
        if not snaps:
            break
        if line_idx == 0:
            # 重爬前先清舊列，保證多盤線數量變少時不留殘留
            conn.execute('DELETE FROM %s WHERE match_id=?' %
                         HKJC_UPSERTS[market][0], (match_id,))
        save_hkjc_snapshots(conn, market, match_id, line_idx, snaps)
        any_data = True
    if any_data:
        conn.commit()
        import crawler as _cr
        _cr.notify_line_move(match_id)
        return True
    return 'NO_DATA'


# ---------------------------------------------------------------- 各任務
def task_odds_page(conn, fetcher, match_id, kickoff, market):
    """五種 changeDetail 盤口頁通用任務"""
    pages = {'ou': 'overunder.aspx', 'ah_half': 'HandicapHalf.aspx',
             'ou_half': 'OverUnderHalf.aspx', '1x2': '1x2.aspx',
             '1x2_half': '1x2Half.aspx', 'corner': 'corner.aspx'}
    # 半場盤（亞讓/大小/歐）同角球盤用 Crown(3)：易胜博(12) 唔開半場盤，
    # 用 12 會成頁空殼誤標無數據（2026-10-07 用戶指正後實測驗證）
    company = COMPANY_CROWN if market in ('1x2_half', 'corner', 'ah_half',
                                          'ou_half') else COMPANY_12BET
    # 好波大小球後備供應商（2026-10-12 用戶指示）：易胜博(12) 無盤 → 用
    # 皇冠(3) 頂替。company_id 欄位就係供應商註明（odds_ou 每行有
    # company_id，12=易胜博 3=皇冠），唔使另開欄。
    companies = [company]
    if market in ('ou', '1x2') and company == COMPANY_12BET:
        # 歐盤同大小一樣：易勝博無盤 → 皇冠後備（2026-10-14 用戶指示，company_id 欄註明）
        companies = [COMPANY_12BET, COMPANY_CROWN]
    for comp in companies:
        url = ('https://vip.titan007.com/changeDetail/%s?id=%d&companyid=%d'
               % (pages[market], match_id, comp))
        html = fetcher.get(url, encoding='gbk', referer='https://zq.titan007.com/',
                           is_odds_page=True)
        if html is None:
            return False        # 網絡層失敗：打返 0 重試，唔轉供應商
        if html == 'NO_DATA':
            continue            # 呢間無盤：試後備供應商
        if market == 'corner':
            # 角球頁有兩個表：表0=角球讓球、表1=角球大小。只取表0（讓球）
            mtab = re.search(r'<table[^>]*>.*?</table>', html, re.S | re.I)
            if not mtab:
                continue
            html = mtab.group(0)

        if market in ('ou', 'ou_half'):
            timeline = parse_ou_html(html, kickoff)
        elif market in ('1x2', '1x2_half'):
            timeline = parse_1x2_html(html, kickoff)
        else:
            timeline = parse_odds_html(html, kickoff)
        if not timeline:
            continue            # 有頁面但冇賠率列：試後備供應商
        snaps = derive_snapshots(timeline, kickoff)
        if not snaps:
            continue
        save_snapshots(conn, market, match_id, comp, snaps)
        conn.commit()
        import crawler as _cr
        _cr.notify_line_move(match_id)
        return True
    return 'NO_DATA'


# ---------------------------------------------------------------- 未開賽場次 OU 預抓（好波 APP）
# titan007 大小球盤喺開賽前數小時至數日先開盤；舊隊列（enqueue_all）只為完場場次補抓，
# 搞到未開賽場次永遠冇 OU 線（好波 APP 嘅莊家尾盤線來源斷咗）。
# 解法：主線道 worker 0 喺冇任務可派時，直接為未來 36 小時內開賽嘅場次
# 抓 overunder.aspx（行 task_odds_page 同一條寫庫路徑，initial→closing 七時點），
# 每場每 20 分鐘最多刷新一次，直至開賽。開賽後舊隊列照舊補全歷史時點。
_UPCOMING_OU_LAST = {}        # match_id -> epoch，今次行程內嘅節流表
UPCOMING_OU_INTERVAL = 480    # 8 分鐘（2026-10-12 好波 OU 提速：睇門狗並行 4 線+
                              # 單一選舉後每輪實際容量大 4 倍，節流收緊——舊制
                              # 15 分鐘係順序行嘅樽頸妥協，而家唔使）
UPCOMING_OU_WINDOW_H = 60     # 60 小時：48h 顯示窗口外圍再加緩衝——舊版 36h，
                              # 第 37-48 小時嘅場次（如今晚嘅 3024480）冇人預抓，
                              # OU 線要等到開賽前一日先出現（2026-10-08）


OU_WATCHDOG_LOCK = os.path.join(BASE_DIR, 'engine', 'ou_watchdog.lock')


def _ou_watchdog_elect():
    """睇門狗單一選舉（2026-10-12 好波 OU 提速 4 倍措施）：舊制每個 worker
    進程各跑一條睇門狗線程——5 條一齊刷同一批臨場場次，代理配額燒 5 重複。
    而家每輪選舉一次：鎖冇人持有／持有人死咗 → 自己搶；否則今輪罷工。
    持有人中途冧 → 下一輪自動由其他線道接手。"""
    import ctypes
    try:
        if os.path.exists(OU_WATCHDOG_LOCK):
            pid = int(open(OU_WATCHDOG_LOCK).read().strip())
            if pid > 0:
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if h:
                    ctypes.windll.kernel32.CloseHandle(h)
                    return False        # 有人做緊
    except (ValueError, OSError):
        pass
    try:
        tmp = OU_WATCHDOG_LOCK + '.tmp'
        with open(tmp, 'w') as fp:
            fp.write(str(os.getpid()))
        os.replace(tmp, OU_WATCHDOG_LOCK)
        return True
    except OSError:
        return False


def _ou_watchdog(cfg, state):
    """好波 APP OU 預抓看門狗（獨立線程）：每 60 秒一循環，為未來 36 小時內
    開賽嘅未開賽場次預抓/刷新大小球盤。2026-10-12 提速：單一選舉（見
    _ou_watchdog_elect）+ 4 線並行取盤 + 節流收緊到 8 分鐘。"""
    conn = db_connect(cfg)
    fetcher = ExtraFetcher(conn, cfg, state_prefix='extra:ouwatch:')
    while not state['stop']:
        cmd = read_control()
        if cmd == 'stop':
            break
        try:
            if _ou_watchdog_elect() and upcoming_ou_pending(conn):
                # 10-16：好波全家四盤一齊預抓（全場大小/半場大小/馬會大小/馬會半場）
                n = refresh_upcoming_ou(
                    conn, fetcher, cfg=cfg, threads=6,
                    markets=('ou', 'ou_half', 'hkjc_ou', 'hkjc_ou_half'))
                if n:
                    log(conn, 'INFO', '預抓 OU（看門狗）：%d 項有更新' % n)
        except Exception:
            log(conn, 'WARN', 'OU 看門狗異常\n%s' % traceback.format_exc())
        time.sleep(40)
    try:
        conn.close()
    except Exception:
        pass


def upcoming_ou_pending(conn):
    """未來 36 小時內開賽、未完場嘅場次數（用嚟決定線道要唔要保持運作）"""
    now = dt.datetime.now()
    hi = (now + dt.timedelta(hours=UPCOMING_OU_WINDOW_H)).strftime('%Y-%m-%d %H:%M')
    lo = (now - dt.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M')
    try:
        return conn.execute(
            'SELECT COUNT(*) FROM matches WHERE home_score IS NULL '
            'AND kickoff >= ? AND kickoff <= ?', (lo, hi)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _ou_refresh_one(cfg, mid, kickoff, market='ou'):
    """並行取盤 worker：獨立 DB 連線 + Fetcher（同 shenbo 並行取盤同款）。
    task_odds_page 回傳 True=有更新 / 'NO_DATA' / False；hkjc_* 行
    task_hkjc（逐條盤線探取）。10-16：market 參數化——好波全家（全/半場
    大小、馬會大小）都經呢度預抓。"""
    c2 = db_connect(cfg)
    f2 = ExtraFetcher(c2, cfg, state_prefix='extra:ouwatch:t%d:' % mid)
    try:
        ko = dt.datetime.strptime(kickoff, '%Y-%m-%d %H:%M')
        if market.startswith('hkjc_'):
            ok = task_hkjc(c2, f2, mid, ko, market) is True
        else:
            ok = task_odds_page(c2, f2, mid, ko, market) is True
        if ok:
            # 2026-10-24：大小線更新都通知 APP（好波規則用 OU 線）
            import crawler as _cr
            _cr.notify_line_move(mid)
        return mid, market, ok
    except Exception:
        log(c2, 'WARN', '預抓 %s 失敗 %d\n%s'
            % (market, mid, traceback.format_exc()))
        return mid, market, False
    finally:
        c2.close()


def refresh_upcoming_ou(conn, fetcher, cfg=None, threads=1, markets=None):
    """為即將開賽場次預抓/刷新大小球盤（逐場逐市場節流）。回傳今輪成功幾多項。
    2026-10-12：threads>1 時並行取盤（每線獨立連線），好波 OU 更新提速 4 倍。
    2026-10-16：markets 參數化（預設 ('ou',) 兼容舊呼叫）——好波全家四盤
    （全場大小/半場大小/馬會大小/馬會半場）一齊預抓；節流表 key 變 (mid, market)。"""
    if markets is None:
        markets = ('ou',)
    now = dt.datetime.now()
    hi = (now + dt.timedelta(hours=UPCOMING_OU_WINDOW_H)).strftime('%Y-%m-%d %H:%M')
    lo = (now - dt.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M')
    try:
        rows = conn.execute(
            'SELECT id, kickoff FROM matches WHERE home_score IS NULL '
            'AND kickoff >= ? AND kickoff <= ? ORDER BY kickoff', (lo, hi)
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    due = []
    t = time.time()
    for mid, kickoff in rows:
        for market in markets:
            if t - _UPCOMING_OU_LAST.get((mid, market), 0) < UPCOMING_OU_INTERVAL:
                continue
            _UPCOMING_OU_LAST[(mid, market)] = t
            due.append((mid, kickoff, market))
    if len(_UPCOMING_OU_LAST) > 6000:      # 節流表防無限膨脹（key 變 tuple 後放大一倍上限）
        _UPCOMING_OU_LAST.clear()
    if not due:
        return 0
    if threads <= 1 or cfg is None:
        did = 0
        for mid, kickoff, market in due:
            try:
                ko = dt.datetime.strptime(kickoff, '%Y-%m-%d %H:%M')
                if market.startswith('hkjc_'):
                    ok = task_hkjc(conn, fetcher, mid, ko, market) is True
                else:
                    ok = task_odds_page(conn, fetcher, mid, ko, market) is True
                if ok:
                    did += 1
            except Exception:
                log(conn, 'WARN', '預抓 %s 失敗 %d\n%s'
                    % (market, mid, traceback.format_exc()))
        return did
    import concurrent.futures as cf
    did = 0
    with cf.ThreadPoolExecutor(max_workers=threads) as pool:
        futs = [pool.submit(_ou_refresh_one, cfg, mid, ko_, market)
                for mid, ko_, market in due]
        for fut in cf.as_completed(futs):
            try:
                _, _, ok = fut.result()
            except Exception:                     # noqa: BLE001
                ok = False
            if ok:
                did += 1
    return did


def task_1x2all(conn, fetcher, match_id, kickoff):
    url = 'https://1x2d.titan007.com/%d.js' % match_id
    text = fetcher.get(url, encoding='utf-8-sig',
                       referer='https://1x2.titan007.com/')
    if text is None or not text.strip():
        return False
    if text == 'NO_DATA' or len(text) < 200:
        return 'NO_DATA'
    neutrality, rows = parse_1x2d_js(text)
    if rows is None:
        return False
    if neutrality is not None:
        conn.execute('UPDATE matches SET is_neutral=? WHERE id=?',
                     (neutrality, match_id))
    if not rows:
        return 'NO_DATA'
    if save_1x2all(conn, match_id, rows) == 0:
        return 'NO_DATA'
    conn.commit()
    return True


def task_corners_stat(conn, fetcher, match_id, kickoff):
    url = 'https://bf.titan007.com/detail/%d.htm' % match_id
    html = fetcher.get(url, encoding='utf-8',
                       referer='https://bf.titan007.com/')
    if html is None:
        return False
    if html == 'NO_DATA' or '角球' not in html:
        return 'NO_DATA'
    out = parse_bf_corners(html)
    if 'ft_home' not in out and 'ht_home' not in out:
        return 'NO_DATA'
    conn.execute(
        'INSERT INTO match_corners(match_id,corner_ft_home,corner_ft_away,'
        'corner_ht_home,corner_ht_away) VALUES(?,?,?,?,?) '
        'ON CONFLICT(match_id) DO UPDATE SET '
        'corner_ft_home=excluded.corner_ft_home,'
        'corner_ft_away=excluded.corner_ft_away,'
        'corner_ht_home=excluded.corner_ht_home,'
        'corner_ht_away=excluded.corner_ht_away',
        (match_id, out.get('ft_home'), out.get('ft_away'),
         out.get('ht_home'), out.get('ht_away')))
    conn.commit()
    return True


def task_asian(conn, fetcher, match_id, kickoff):
    """亞洲盤缺口補取：已有盤口（含曾確認無數據）的場次直接跳過，不發請求"""
    row = conn.execute('SELECT odds_done FROM matches WHERE id=?',
                       (match_id,)).fetchone()
    if row and row[0] == 1:
        return True
    if row and row[0] == 2:
        return 'NO_DATA'
    from crawler import crawl_odds_for_match
    result = crawl_odds_for_match(conn, fetcher, match_id,
                                  kickoff.strftime('%Y-%m-%d %H:%M'),
                                  COMPANY_12BET,
                                  fallback_company=COMPANY_CROWN)
    if result is True:
        row = conn.execute('SELECT odds_done FROM matches WHERE id=?',
                           (match_id,)).fetchone()
        if row and row[0] == 2:
            return 'NO_DATA'
    return result


TASK_FUNCS = {m: task_odds_page for m in
              ('ou', 'ah_half', 'ou_half', '1x2', '1x2_half', 'corner')}
TASK_FUNCS['1x2all'] = task_1x2all
TASK_FUNCS['corners_stat'] = task_corners_stat
TASK_FUNCS['asian'] = task_asian
TASK_FUNCS['goals'] = task_goals
for _m in HKJC_MARKETS:
    TASK_FUNCS[_m] = task_hkjc


# ---------------------------------------------------------------- 控制與狀態
def read_control():
    try:
        with open(CONTROL, encoding='utf-8') as fp:
            return json.load(fp).get('cmd', 'run')
    except (OSError, json.JSONDecodeError):
        return 'run'


def write_control(cmd):
    with open(CONTROL, 'w', encoding='utf-8') as fp:
        json.dump({'cmd': cmd, 'ts': time.time()}, fp)


def write_status(**kw):
    st = {}
    if os.path.exists(STATUS):
        try:
            with open(STATUS, encoding='utf-8') as fp:
                st = json.load(fp)
        except (OSError, ValueError):   # ValueError 包 UnicodeDecodeError——
            pass                        # 多進程爭寫會撕爛 status.json（2026-10-08
                                        # w1 開機即死元兇：except 漏咗佢）
    st.update(kw)
    st['updated_at'] = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    tmp = STATUS + '.tmp'
    payload = json.dumps(st, ensure_ascii=False)
    # Windows 下 status.json 可能被 GUI／防毒短暫霸佔：重試，最後直接寫本檔兜底
    for attempt in range(5):
        try:
            with open(tmp, 'w', encoding='utf-8') as fp:
                fp.write(payload)
            os.replace(tmp, STATUS)
            return
        except OSError:
            time.sleep(0.4 * (attempt + 1))
    try:
        with open(STATUS, 'w', encoding='utf-8') as fp:
            fp.write(payload)
    except OSError:
        pass


def fmt_duration(sec):
    if sec is None or sec < 0:
        return '計算中…'
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return '%d天%02d小時%02d分' % (d, h, m)
    if h:
        return '%d小時%02d分' % (h, m)
    return '%d分鐘' % m


class ExtraFetcher(Fetcher):
    """休息前把狀態寫進 status.json，讓 GUI 顯示「休息中」而不是看似卡住"""

    def _rest(self, minutes, reason):
        write_status(state='resting',
                     task_text='%s，休息 %d 分鐘後繼續…' % (reason, minutes))
        super()._rest(minutes, reason)
        write_status(state='running')


# ---------------------------------------------------------------- 單例鎖
LOCK = os.path.join(BASE_DIR, 'engine', 'crawl_extra.lock')


def _pid_alive(pid):
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def acquire_lock(lock_key=''):
    # 每條線道（市場集）各自一把鎖——多線道並行全速爬（用戶 2026-09-29 再加速指示）
    path = LOCK + ('.' + lock_key if lock_key else '')
    if os.path.exists(path):
        try:
            pid = int(open(path).read().strip())
            if pid > 0 and _pid_alive(pid):
                return False
        except (ValueError, OSError):
            pass
    with open(path, 'w') as fp:
        fp.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return True


# ---------------------------------------------------------------- 主流程
def enqueue_all(conn):
    """為所有已完場比賽補上任務（已存在則跳過）；asian 用於補回基本盤口缺口。
    2026-10-12：一併寫入正規化排序欄位（ord/season_label/market_ord），
    令領任務 ORDER BY 完全行 idx_tasks_claim4，唔再 join 三張表。"""
    mkts = ' UNION ALL '.join('SELECT "%s" AS market' % m for m in MARKET_ORDER)
    conn.execute(
        'INSERT OR IGNORE INTO extra_tasks(match_id,market,ord,season_label,'
        'market_ord) '
        'SELECT m.id, t.market, COALESCE(lo.ord, 999999), s.season_label, '
        'CASE t.market %s END FROM matches m '
        'CROSS JOIN (%s) t '
        'JOIN seasons s ON s.id=m.season_id '
        'LEFT JOIN _league_order lo ON lo.titan_id=s.titan_id '
        'WHERE m.home_score IS NOT NULL'
        % (' '.join('WHEN "%s" THEN %d' % (m, i)
                    for i, m in enumerate(MARKET_ORDER)), mkts))
    conn.commit()


# 子聯賽結構聯賽（賽季數據分散在 s<id>_<subId>.js，過往只入庫單一階段）：
# 178 巴西聖保羅州錦標賽、13 芬蘭超級聯賽、436/616/774/1029/481/1063/1068/1450 澳州各州聯賽
# 520 海灣盃（海灣球會錦標賽）、161 海灣運動會——過往完全未收錄，一併補全
# 1864 歐國聯、93 非洲盃、1964 中北美國家聯賽、114 歐青U21外圍賽——國家隊賽事首次收錄
# 358 巴西乙、284 日乙(J2)、35 英格蘭乙組、240 烏拉圭甲（春/秋雙階段）——補回缺失季度
BASE_LEAGUES = [178, 13, 436, 616, 774, 1029, 481, 1063, 1068, 1450, 520, 161,
                1864, 93, 1964, 114, 358, 284, 35, 240, 72, 144,
                83, 6, 153, 108, 224, 95, 192, 239]
BASE_SEASONS_N = 7        # 每個聯賽補最近 7 個球季（之前6年+今年；杯賽季名如 2024-2025）
# 指定聯賽額外補嘅舊季（list[:N] 唔夠位覆蓋嘅）：72 日聯杯要 2019 起；144 英錦標要 2019-2020 起
BASE_EXTRA_SEASONS = {72: ['2019'], 144: ['2019-2020'],
                      162: ['2019-2020', '2020-2021']}

# 插隊優先聯賽：分三層——日聯杯(72)+英錦標(144)最頂(-3)；國家隊賽事(-2)；其餘優先聯賽(-1)
PRIORITY_TIER_TOP = [72, 144, 162]                # 日聯杯（2019起）、英錦標（2019-2020起）、日皇杯（2019-2020起）
PRIORITY_TIER_NATIONAL = [1864, 93, 1964, 114]             # 歐國聯、非國盃、中北美聯、歐青U21外
PRIORITY_LEAGUES = [714, 520, 161,          # 智利盃、海灣盃、海灣運動會
                    358, 284, 35, 240] + PRIORITY_TIER_NATIONAL

# 新增聯賽的顯示名（寫入 competitions；已存在則不覆蓋）
NEW_LEAGUES = {520: ('海灣盃', '海灣錦標', '杯賽'),
               161: ('海灣運動會', '海灣運', '杯賽'),
               1864: ('歐洲國家聯賽', '歐國聯', '聯賽'),
               93: ('非洲國家盃', '非國盃', '杯賽'),
               1964: ('中北美國家聯賽', '中北美聯', '聯賽'),
               114: ('歐青U21外圍賽', '歐青U21外', '聯賽'),
               144: ('英格蘭聯賽錦標賽', '英錦標', '杯賽')}


def backfill_base(conn, fetcher):
    """補全子聯賽缺口：逐季下載主檔+全部子聯賽檔入庫（冇就做，做咗就跳過）。
    回傳 False 代表收到停止指令。"""
    conn.execute(
        'CREATE TABLE IF NOT EXISTS extra_backfill('
        'league_id INTEGER NOT NULL, season_label TEXT NOT NULL, done INTEGER,'
        'PRIMARY KEY(league_id, season_label))')
    conn.commit()
    from crawler import (get_season_list, resolve_prefix,
                         save_with_sub_leagues, parse_season_js)
    for lid in BASE_LEAGUES:
        if lid in NEW_LEAGUES:
            req, tc, cat = NEW_LEAGUES[lid]
            conn.execute(
                'INSERT INTO competitions(titan_id,req_name,name_tc,category,'
                'sub_of,enabled) VALUES(?,?,?,?,NULL,1) ON CONFLICT(titan_id) '
                'DO NOTHING', (lid, req, tc, cat))
            conn.commit()
        lg = conn.execute('SELECT req_name FROM competitions WHERE titan_id=?',
                          (lid,)).fetchone()
        lg_name = lg[0] if lg else str(lid)
        seasons = get_season_list(fetcher, lid)[:BASE_SEASONS_N]
        seasons = seasons + [s for s in BASE_EXTRA_SEASONS.get(lid, [])
                             if s not in seasons]
        if not seasons:
            log(conn, 'WARN', '補全：無法取得賽季列表，跳過 %s' % lid)
            continue
        for sl in seasons:
            if conn.execute(
                    'SELECT done FROM extra_backfill WHERE league_id=? '
                    'AND season_label=? AND done=1', (lid, sl)).fetchone():
                continue
            while read_control() == 'pause':
                write_status(state='paused',
                             task_text='已暫停（按「繼續」恢復爬取）')
                time.sleep(2)
            if read_control() == 'stop':
                return False
            write_status(state='running',
                         task_text='補全賽程缺口｜%s｜%s' % (lg_name, sl))
            prefix = resolve_prefix(fetcher, lid)
            if not prefix:
                log(conn, 'WARN', '補全：無法判定前綴，跳過 %s %s' % (lid, sl))
                continue
            url = ('https://zq.titan007.com/jsData/matchResult/'
                   '%s/%s.js?version=1' % (sl, prefix))
            text = fetcher.get(url)
            if not text or text.lstrip().startswith('<') or 'jh[' not in text:
                log(conn, 'WARN', '補全：主檔不存在 %s %s' % (lid, sl))
                continue
            # 144 英錦標 / 162 日皇杯 ID 校驗：檔案必須係對應賽事，唔係就係 ID 錯，唔入庫
            if lid == 144 and '锦标' not in text:
                log(conn, 'ERROR',
                    '補全：144 檔案名稱校驗失敗（唔係英錦標），跳過 %s' % sl)
                continue
            if lid == 162 and '天皇' not in text:
                log(conn, 'ERROR',
                    '補全：162 檔案名稱校驗失敗（唔係日皇杯），跳過 %s' % sl)
                continue
            try:
                is_current = 1 if sl == seasons[0] else 0
                season_id, n = save_with_sub_leagues(
                    conn, fetcher, lid, sl, is_current, text,
                    parse_season_js(text), expand=True)
                fin = conn.execute(
                    'SELECT COUNT(*) FROM matches WHERE season_id=? AND '
                    'home_score IS NOT NULL', (season_id,)).fetchone()[0]
                conn.execute(
                    'UPDATE seasons SET match_count_expected=?, '
                    'match_count_done=? WHERE id=?', (n, fin, season_id))
                conn.execute(
                    'INSERT OR REPLACE INTO extra_backfill VALUES(?,?,1)',
                    (lid, sl))
                conn.commit()      # 季檔寫入先落盤，build_all 長工唔帶住寫鎖
                import prematch
                np = prematch.build_all(conn, only_season_id=season_id)
                conn.commit()
                log(conn, 'INFO',
                    '補全：%s %s 檔案 %d 場 / 已完場 %d 場 / 開賽前數據 %d 場'
                    % (lg_name, sl, n, fin, np))
            except Exception:
                log(conn, 'ERROR', '補全失敗 %s %s\n%s'
                    % (lid, sl, traceback.format_exc()))
    return True


# 額外插隊區間（回測/補數據用）：設定後 boost_priority 每次開機都會將呢段
# 期間嘅完場賽事標 -1——手動 UPDATE 會俾 w1 開機 reset  wipes，所以要寫入
# boost_priority 先活得過重啟（2026-10-09 教訓）。格式 (開始, 結束) 或 None。
BOOST_EXTRA_RANGE = ('2026-09-01', '2026-10-01')


def boost_priority(conn):
    """插隊分三層：attempts=-3 日聯杯最頂；-2 國家隊賽事；-1 其餘優先聯賽。
    2026-10-08 修訂：插隊只俾【近 14 日】嘅場次——舊版成個盃賽歷史（19.4 萬項）
    全部插隊，fleet 幾日都做唔完普通場次，明日賽事反被餓住。每次先清零舊標記
    （rolling 窗口，唔會有漏網）。BOOST_EXTRA_RANGE 期間內嘅場次一律 -1。"""
    conn.execute('UPDATE extra_tasks SET attempts=0 WHERE attempts<0')
    # 2026-10-10 用戶指示「先清大細→半場大細→全部」：_phase.json 指定嘅市場長駐
    # 最頂（-6）。看門狗喺 phase 完成後改寫 _phase.json 推進（完成后推半場）。
    _phase = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', '_phase.json')
    try:
        with open(_phase, encoding='utf-8') as _fp:
            _pm = json.load(_fp).get('markets') or []
        if _pm:
            conn.execute(
                'UPDATE extra_tasks SET attempts=-6 WHERE status=0 AND '
                'market IN (%s)' % ','.join('"%s"' % m for m in _pm))
    except (OSError, ValueError):
        pass
    for tier, lids in ((-3, PRIORITY_TIER_TOP), (-2, PRIORITY_TIER_NATIONAL),
                       (-1, PRIORITY_LEAGUES)):
        conn.execute(
            "UPDATE extra_tasks SET attempts=%d WHERE status=0 AND match_id IN ("
            'SELECT m.id FROM matches m JOIN seasons s ON m.season_id=s.id '
            "WHERE s.titan_id IN (%s) AND m.kickoff >= datetime('now','localtime','-14 days'))"
            % (tier, ','.join(str(x) for x in lids)))
    if BOOST_EXTRA_RANGE:
        conn.execute(
            "UPDATE extra_tasks SET attempts=-1 WHERE status=0 AND match_id IN ("
            "SELECT id FROM matches WHERE kickoff >= ? AND kickoff < ? "
            'AND home_score IS NOT NULL)', BOOST_EXTRA_RANGE)
    # 2026-10-10 用戶指示（好波/footballanalysis 盤口保鮮）：未來 48 小時未開賽場次
    # 長駐插隊最前——舊版一次性標記會被本函數清掉，改為每次 boost 自動補上
    conn.execute(
        "UPDATE extra_tasks SET attempts=-5 WHERE status=0 AND match_id IN ("
        "SELECT id FROM matches WHERE home_score IS NULL "
        "AND kickoff >= datetime('now','localtime') "
        "AND kickoff < datetime('now','localtime','+48 hours'))")
    # footballanalysis 亞盤保鮮（2026-10-10 晚）：未來48h asian 長駐 -7——
    # 行喺大細 phase（-6）前面，確保 desktop app 亞盤持續新鮮；task_asian 嘅
    # odds_done 跳過邏輯由看門狗重置配套處理
    conn.execute(
        "UPDATE extra_tasks SET attempts=-7 WHERE status=0 AND market='asian' AND match_id IN ("
        "SELECT id FROM matches WHERE home_score IS NULL "
        "AND kickoff >= datetime('now','localtime','-30 minutes') "
        "AND kickoff < datetime('now','localtime','+48 hours'))")
    conn.commit()


def retry_backfill_72(conn, fetcher):
    """主循環內定期重試日聯杯(72)同英錦標(144)未補嘅球季；網絡復原後自動補，
    全部補完後即刻變 no-op（每次只係幾條 SQL + 少量請求）。"""
    if conn.execute(
            "SELECT COUNT(*) FROM extra_backfill WHERE league_id=72 AND done=1"
    ).fetchone()[0] >= 8 and conn.execute(
            "SELECT COUNT(*) FROM extra_backfill WHERE league_id=144 AND done=1"
    ).fetchone()[0] >= 7 and conn.execute(
            "SELECT COUNT(*) FROM extra_backfill WHERE league_id=162 AND done=1"
    ).fetchone()[0] >= 2:   # 72: 2019-2026 共 8 季；144: 2019-2020 起共 7 季；162: 2019-2020、2020-2021 共 2 季
        return
    from crawler import (get_season_list, resolve_prefix,
                         save_with_sub_leagues, parse_season_js)
    for lid, extra, want in ((72, ['2019'], 8), (144, ['2019-2020'], 7),
                             (162, ['2019-2020', '2020-2021'], 2)):
        if conn.execute(
                'SELECT COUNT(*) FROM extra_backfill WHERE league_id=? '
                'AND done=1', (lid,)).fetchone()[0] >= want:
            continue
        seasons = get_season_list(fetcher, lid)[:BASE_SEASONS_N] + extra
        seen = set()
        seasons = [s for s in seasons if not (s in seen or seen.add(s))]
        prefix = resolve_prefix(fetcher, lid)
        if not prefix:
            continue
        for sl in seasons:
            if conn.execute(
                    'SELECT 1 FROM extra_backfill WHERE league_id=? AND '
                    'season_label=? AND done=1', (lid, sl)).fetchone():
                continue
            text = fetcher.get(
                'https://zq.titan007.com/jsData/matchResult/%s/%s.js?version=1'
                % (sl, prefix))
            if not text or text.lstrip().startswith('<') or 'jh[' not in text:
                continue
            if lid == 144 and '锦标' not in text:
                log(conn, 'ERROR',
                    '補季重試：144 檔案名稱校驗失敗（唔係英錦標），跳過 %s' % sl)
                continue
            if lid == 162 and '天皇' not in text:
                log(conn, 'ERROR',
                    '補季重試：162 檔案名稱校驗失敗（唔係日皇杯），跳過 %s' % sl)
                continue
            is_current = 1 if seasons and sl == seasons[0] else 0
            season_id, n = save_with_sub_leagues(
                conn, fetcher, lid, sl, is_current, text, parse_season_js(text),
                expand=True)
            fin = conn.execute(
                'SELECT COUNT(*) FROM matches WHERE season_id=? AND '
                'home_score IS NOT NULL', (season_id,)).fetchone()[0]
            conn.execute(
                'UPDATE seasons SET match_count_expected=?, match_count_done=? '
                'WHERE id=?', (n, fin, season_id))
            conn.execute('INSERT OR REPLACE INTO extra_backfill VALUES(?,?,1)',
                         (lid, sl))
            enqueue_all(conn)     # 新入庫場次即刻有任務
            boost_priority(conn)  # 插隊優先保持最前
            conn.commit()
            log(conn, 'INFO', '補季完成：%s %s（%d 場）' % (lid, sl, n))


def match_context(conn, match_id):
    r = conn.execute(
        'SELECT m.kickoff, c.req_name, s.season_label FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'WHERE m.id=?', (match_id,)).fetchone()
    if not r:
        return None, '', ''
    return (dt.datetime.strptime(r[0], '%Y-%m-%d %H:%M'), r[1], r[2])


# 擴充爬蟲專用節奏（只影響本爬蟲；主爬蟲用 config.json 原值，互不干擾）
# 2026-09-29 用戶指明：全速最快速不停頓，實測 titan007 極限——
# 唔設任何休息（tiered 同 legacy 都唔要）；
# 極限數據（邊個 req_count 觸發封鎖、封鎖幾耐）記入 crawl_state 供之後調速
# 2026-10-08 恢復全速（用戶「加速」指示）：網絡恢復後改返 0.15——
# 慢速 2.0s 係 09-30 遠端控制遷就，而家 titan007 配額/冷卻機制自律，
# 24 線程全速會自動喺代理配額（650/6min）度調節
# 2026-10-10 熱點直連時段：直通道（1x2d/bf/live）全速冇代理限制，但要用
# APP 日常驗證過嘅節奏（0.2s+分層休息）防止一手 IP 俾 titan007 封——
# 0.15s 全無休會即封（800 req/s），封咗就 0 速度。proxy_direct=true 時
# 用 config.json 原值（家用 WiFi 時設 false，行代理池全速檔）。
if json.load(open(os.path.join(BASE_DIR, 'config.json'), encoding='utf-8')).get('proxy_direct'):
    EXTRA_TUNE = {}
else:
    EXTRA_TUNE = {'request_delay_sec': 0.15,    # 請求間隔（秒）
                  'rest_tiers': []}             # 全速：分層休息全關


def run(limit=None, only=None, threads_override=None, worker=1):
    # worker：並行 worker 編號（1..N），各自一個鎖位；共用一條優先隊列，
    # 邊個行得快邊個幫手，直至全部完成（用戶 2026-09-29 指示）。
    # 只有 worker 1 做 backfill/enqueue（避免 N 個一齊做）；
    # 其餘 worker 純做任務——自然達成「完成嘅線去分擔未完成的線」。
    only_key = 'main.w%d' % worker
    if not acquire_lock(only_key):
        print('worker %d 鎖位已被佔，本次啟動退出' % worker)
        return
    # 一開機即確認 run：手動重啟代表用戶想行，唔俾上一次遺留嘅 stop 指令
    # 喺 backfill_base 入面靜默扼殺今次啟動
    write_control('run')
    cfg = json.load(open(os.path.join(BASE_DIR, 'config.json'), encoding='utf-8'))
    cfg.update(EXTRA_TUNE)
    cfg['stop_on_rate_limit'] = True
    if threads_override:
        cfg['crawl_threads'] = threads_override
    main_lane = (worker == 1)
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    conn = db_connect(cfg)
    migrate(conn)
    fetcher_tmp = ExtraFetcher(conn, cfg, state_prefix='extra:%s:' % only_key)
    if main_lane:
        # 開機試水：賽程補全全部行 zq 主機，一開始就斷線冇理由逐聯賽試足一輪
        probe = fetcher_tmp.get('https://zq.titan007.com/jsData/LeagueSeason/sea178.js')
        if probe:
            backfill_ok = backfill_base(conn, fetcher_tmp)
        else:
            log(conn, 'WARN', 'zq.titan007.com 暫時連不上，今輪跳過賽程補全（主循環定時自動試水）')
            backfill_ok = True
        if not backfill_ok:
            write_status(state='stopped', task_text='已停止')
            conn.close()
            return
        enqueue_all(conn)
        boost_priority(conn)
    else:
        # 支線道：淨係做市場任務，唔搶 backfill/enqueue（主線道做）
        backfill_ok = True

    only_sql = ''
    only_args = ()
    if only:
        only_sql = ' AND market IN (%s) ' % ','.join('?' * len(only))
        only_args = tuple(only)
        log(conn, 'INFO', '線道 %s 只爬：%s' % (only_key, ','.join(only)))

    total_tasks = conn.execute('SELECT COUNT(*) FROM extra_tasks').fetchone()[0]
    done_tasks = conn.execute(
        'SELECT COUNT(*) FROM extra_tasks WHERE status>0').fetchone()[0]
    total_matches = conn.execute(
        'SELECT COUNT(DISTINCT match_id) FROM extra_tasks').fetchone()[0]
    done_matches = conn.execute(
        'SELECT COUNT(DISTINCT match_id) FROM extra_tasks WHERE status>0'
    ).fetchone()[0]
    log(conn, 'INFO', '擴充爬取開始：共 %d 場 / %d 項任務，已完成 %d 項'
        % (total_matches, total_tasks, done_tasks))

    t_start = time.time()
    # 線程池全速並行（用戶 2026-09-29：加速至 10 小時內完成）：
    # 每個 worker 獨立 Fetcher＋DB 連線；任務用 status 0→9 原子領取，唔會重複做；
    # 開機先清返上次意外死咗留下嘅 in-flight（status=9）
    _exec_retry(conn, 'UPDATE extra_tasks SET status=0 WHERE status=9')
    n_start = done_tasks
    n_workers = int(cfg.get('crawl_threads', 1))
    state = {'stop': False, 'matches_done': 0}
    threads = [threading.Thread(target=_worker, daemon=True, args=(
        cfg, only, only_key, i, main_lane, t_start, n_start, total_tasks,
        done_matches, total_matches, limit, state))
        for i in range(n_workers)]
    # 好波 APP OU 看門狗：獨立線程，為即將開賽場次持續預抓大小球盤
    if only is None or 'ou' in only:
        threads.append(threading.Thread(target=_ou_watchdog, daemon=True,
                                        args=(cfg, state)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn.close()
    if state['stop']:
        _status(state='stopped', task_text='已停止')
        return
    write_status(state='done', task_text='全部完成！',
                 done_matches=total_matches, total_matches=total_matches,
                 done_tasks=total_tasks, total_tasks=total_tasks,
                 eta_sec=0, eta_text='已完成')
    conn2 = db_connect(cfg)
    log(conn2, 'INFO', '擴充爬取全部完成')
    conn2.close()


_TASK_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()


def _status(**kw):
    with _STATUS_LOCK:
        write_status(**kw)


def _exec_retry(conn, sql, args=()):
    """locked 就瞓 1 秒重試，最多 5 次（screen_app 更新會搶鎖，
    以前一撞 locked 就殺死成條線道）"""
    for i in range(5):
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur
        except sqlite3.OperationalError:
            time.sleep(1)
    return None


def _cloud_exclusion():
    if '--reconcile-cloud' in sys.argv or os.environ.get('RECONCILE_CLOUD') == '1':
        return ''
    return ('AND NOT EXISTS(SELECT 1 FROM cloud_done cd WHERE '
            'cd.match_id=t.match_id AND cd.market=t.market) ')


def _record_failed_attempt(conn, fetcher, match_id, market, now):
    if fetcher.last_outage:
        _exec_retry(conn,
                    'UPDATE extra_tasks SET status=0,updated_at=? '
                    'WHERE match_id=? AND market=?', (now, match_id, market))
        return
    _exec_retry(conn,
                'UPDATE extra_tasks SET status=0,attempts=attempts+1,'
                'updated_at=? WHERE match_id=? AND market=?',
                (now, match_id, market))
    attrow = None
    for _i in range(3):
        try:
            attrow = conn.execute(
                'SELECT attempts FROM extra_tasks WHERE match_id=? '
                'AND market=?', (match_id, market)).fetchone()
            break
        except sqlite3.OperationalError:
            time.sleep(1)
    att = attrow[0] if attrow else 0
    if att >= 3:
        log(conn, 'WARN', '場次%d %s 三次未取得，保留待重試而不標無數據'
            % (match_id, market))


def _claim_task(conn, only_sql, only_args, excl_sql, excl_args):
    """原子領任務：status 0→9（in-flight）。俾人搶咗就回 None 再搵下個。
    派序（2026-10-10 用戶定調）：attempts 插隊層 → 聯賽次序（_league_order，
    日本J1→…→墨西甲→其他，NULL 行尾）→ 球季新→舊 → 場次新→舊 → 市場層。
    2026-10-12 提速：排序欄位已正規化落 extra_tasks（ord/season_label/
    market_ord，enqueue 時寫定），查詢零 join 零 expression，ORDER BY 完全
    行 idx_tasks_claim4（實測 0.5s → 0.1ms）。"""
    with _TASK_LOCK:
        try:
            row = conn.execute(
                'SELECT match_id, market FROM extra_tasks t '
                'WHERE status=0 ' + only_sql + excl_sql + _cloud_exclusion() +
                'ORDER BY attempts, ord, season_label DESC, match_id DESC, '
                'market_ord LIMIT 1',
                only_args + excl_args).fetchone()
        except sqlite3.OperationalError:
            return 'LOCKED'      # 撞鎖：上層瞓吓重試，唔當冇任務（k2/k3 早退元兇）
        if not row:
            return None
        cur = _exec_retry(conn,
                          'UPDATE extra_tasks SET status=9, updated_at=? '
                          'WHERE match_id=? AND market=? AND status=0',
                          (dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           row[0], row[1]))
        if cur is None:
            return 'LOCKED'      # DB 連續 locked：上層瞓吓重試，唔當冇任務
        return row if cur.rowcount == 1 else None


def _worker(cfg, only, only_key, wid, main_lane, t_start, n_start,
            total_tasks, done_matches, total_matches, limit, state):
    conn = db_connect(cfg)
    fetcher = ExtraFetcher(conn, cfg,
                           state_prefix='extra:%s:w%d:' % (only_key, wid))
    only_args = tuple(only) if only else ()
    only_sql = (' AND market IN (%s) ' % ','.join('?' * len(only))) if only else ''
    matches_done_this_run = 0
    bf72_counter = 0
    last_status = 0.0
    zero_hits = 0            # 連續「確認冇可派任務」次數（要 3 連中先准收工）
    last_reap = time.time()  # 滯留 status=9（死線道留下）清理節拍
    while True:
        if state['stop']:
            break
        # 主機斷線（電路斷路冷卻期）：對應市場成組暫停派任
        down_hosts = {h for h, until in fetcher.host_down_until.items()
                      if time.time() < until}
        down_markets = [m for m, h in MARKET_HOST.items() if h in down_hosts]
        excl_sql, excl_args = '', ()
        if down_markets:
            excl_sql = ' AND market NOT IN (%s) ' % ','.join('?' * len(down_markets))
            excl_args = tuple(down_markets)
        row = _claim_task(conn, only_sql, only_args, excl_sql, excl_args)
        if row == 'LOCKED':
            # 05:20 鎖風暴修正：2s 熱重試 × 144 線程＝每秒幾十個寫鎖爭奪，
            # WAL 單寫者模式直接餓死其他寫手（claim 131s 超時實證）。
            # 退避加長兼加抖動，等風暴過去。
            time.sleep(8 + random.uniform(0, 4))
            continue
        if not row:
            # 淨計「可派」任務（排除斷線主機市場）：斷線主機嘅 pending
            # 唔阻住 alive 主機繼續做（舊版計全部 pending，誤入 waiting）
            try:
                claimable = conn.execute(
                    'SELECT COUNT(*) FROM extra_tasks t WHERE status=0 ' +
                    only_sql + excl_sql + _cloud_exclusion(),
                    only_args + excl_args).fetchone()[0]
            except sqlite3.OperationalError:
                time.sleep(2)
                continue
            if claimable:
                continue                # 有任務領唔到（撞鎖/俾人搶）→ 即刻重領
            if down_markets:
                try:
                    allpend = conn.execute(
                        'SELECT COUNT(*) FROM extra_tasks WHERE status=0'
                        ).fetchone()[0]
                except sqlite3.OperationalError:
                    allpend = -1     # I/O 抖動：照等，下個循環再計
                msg = '等待 %s 恢復連線（可派任務已派完，%d 項待命）' % (
                    '、'.join(sorted(down_hosts)), allpend)
                if wid == 0:
                    _status(state='waiting_net', task_text=msg)
                    log(conn, 'INFO', msg)
                # 主動試探（2026-10-08）：舊版淨係等冷卻到期，幾何退避最壞
                # 30 分鐘先再探——代理池其實早就有活代理，空等令尾盤/預抓斷糧。
                # 每個等週期攞輕量 URL 試，一通即解鎖本線道全部冷卻即刻復工
                for h in sorted(down_hosts):
                    u = _HOST_PROBE_URLS.get(h)
                    if not u:
                        continue
                    try:
                        if fetcher.get(u):
                            fetcher.host_down_until.clear()
                            log(conn, 'INFO', '%s 試探成功，本線道即刻復工' % h)
                            break
                    except Exception:
                        pass
                wait_until = time.time() + 60
                last_beat = 0.0
                while time.time() < wait_until:
                    cmd = read_control()
                    if cmd == 'stop':
                        state['stop'] = True
                        break
                    if wid == 0 and time.time() - last_beat > 15:
                        _status(state='waiting_net', task_text=msg)
                        last_beat = time.time()
                    time.sleep(5)
                continue
            # 真係冇晒可派任務：連續 3 次、每次隔 5 秒確認先收工
            # （舊版一撞 locked 就誤判「全部完成」扼殺成條線道）
            zero_hits += 1
            if zero_hits < 3:
                time.sleep(5)
                continue
            break
        zero_hits = 0
        match_id, market = row

        # 控制檢查
        while read_control() == 'pause':
            if wid == 0:
                _status(state='paused',
                        task_text='已暫停（按「繼續」恢復爬取）')
            time.sleep(2)
        if read_control() == 'stop':
            log(conn, 'INFO', '收到停止指令')
            state['stop'] = True
            break

        try:
            kickoff, comp, season = match_context(conn, match_id)
        except sqlite3.OperationalError:
            # 磁碟 I/O 抖動（WAL 大＋多進程）：任務打返做 status=0，
            # 瞓 5 秒再嚟——呢啲瞬間錯誤舊版會直接殺死成條線道
            _exec_retry(conn,
                        'UPDATE extra_tasks SET status=0,updated_at=? '
                        'WHERE match_id=? AND market=? AND status=9',
                        (dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                         match_id, market))
            time.sleep(5)
            continue
        if kickoff is None:
            conn.execute(
                'UPDATE extra_tasks SET status=0,attempts=attempts+1,updated_at=? '
                'WHERE match_id=? AND market=?',
                (dt.datetime.now().isoformat(), match_id, market))
            conn.commit()
            log(conn, 'WARN', '場次%d %s 缺少開賽或賽事資料，保留待處理'
                % (match_id, market))
            continue

        # 狀態匯報（worker 0 每 5 秒一次；I/O 抖動就跳過今次，唔死）
        if wid == 0 and time.time() - last_status > 5:
            try:
                n_done = conn.execute(
                    'SELECT COUNT(*) FROM extra_tasks WHERE status>0'
                ).fetchone()[0]
            except sqlite3.OperationalError:
                n_done = None
            if n_done is not None:
                elapsed = time.time() - t_start
                rate = ((n_done - n_start) / elapsed
                        if elapsed > 30 and n_done > n_start else None)
                remain = total_tasks - n_done
                eta = (remain / rate) if rate and rate > 0 else None
                _status(
                    state='running',
                    task_text='%s %s｜%s｜場次 %d｜%d線程' % (
                        comp, season, MARKET_NAMES[market], match_id,
                        int(cfg.get('crawl_threads', 1))),
                    done_matches=done_matches + state['matches_done'],
                    total_matches=total_matches,
                    done_tasks=n_done, total_tasks=total_tasks,
                    eta_sec=eta, eta_text=fmt_duration(eta))
            last_status = time.time()

        func = TASK_FUNCS[market]
        try:
            if market in ('ou', 'ah_half', 'ou_half', '1x2', '1x2_half',
                          'corner') + HKJC_MARKETS:
                result = func(conn, fetcher, match_id, kickoff, market)
            else:
                result = func(conn, fetcher, match_id, kickoff)
        except Exception as e:                      # noqa: BLE001
            log(conn, 'WARN', '場次%d %s 解析異常：%s' % (match_id, market, e))
            result = False

        now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 所有任務狀態寫入行 _exec_retry：screen_app 搶鎖時唔會再殺死線道
        if result is True:
            _exec_retry(conn,
                        'UPDATE extra_tasks SET status=1,attempts=attempts+1,'
                        'updated_at=? WHERE match_id=? AND market=?',
                        (now, match_id, market))
        elif result == 'NO_DATA':
            _exec_retry(conn,
                        'UPDATE extra_tasks SET status=2,attempts=attempts+1,'
                        'updated_at=? WHERE match_id=? AND market=?',
                        (now, match_id, market))
        else:
            _record_failed_attempt(conn, fetcher, match_id, market, now)

        try:
            if only:
                left = conn.execute(
                    'SELECT COUNT(*) FROM extra_tasks WHERE match_id=? AND status=0 '
                    'AND market IN (%s)' % ','.join('?' * len(only)),
                    (match_id,) + tuple(only)).fetchone()[0]
            else:
                left = conn.execute(
                    'SELECT COUNT(*) FROM extra_tasks WHERE match_id=? AND status=0',
                    (match_id,)).fetchone()[0]
        except sqlite3.OperationalError:
            left = -1            # I/O 抖動：當仲有，唔影響主流程
        if left == 0:
            matches_done_this_run += 1
            state['matches_done'] += 1

        # 每 300 個任務順手重試日聯杯未補季（主線道 worker 0）
        bf72_counter += 1
        if main_lane and wid == 0 and bf72_counter >= 300:
            bf72_counter = 0
            try:
                retry_backfill_72(conn, fetcher)
            except Exception as e:                  # noqa: BLE001
                log(conn, 'WARN', '日聯杯補季重試異常：%s' % e)

        # 每 10 分鐘清滯留 in-flight：死線道/被 taskkill 留下嘅 status=9。
        # updated_at 舊過 45 分鐘先 reset（自己線道 in-flight 一定新鮮，
        # 單任務冇可能做超過 45 分鐘）
        if main_lane and wid == 0 and time.time() - last_reap > 600:
            last_reap = time.time()
            _exec_retry(conn,
                        "UPDATE extra_tasks SET status=0 WHERE status=9 "
                        "AND updated_at < datetime('now', '+8 hours', '-45 minutes')")
            # WAL 只增唔截（多進程長開 reader 擋 auto-checkpoint）會脹到
            # GB 級引發 disk I/O error——每 10 分鐘順手 PASSIVE checkpoint
            try:
                conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
                conn.commit()
            except sqlite3.OperationalError:
                pass

        if limit is not None and matches_done_this_run >= limit:
            break
    conn.close()


if __name__ == '__main__':
    limit = None
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    only = None
    only = None
    if '--market' in sys.argv:
        # 支援逗號分隔多市場（一線道一組市場，多線道並行）
        only = sys.argv[sys.argv.index('--market') + 1].split(',')
    tovr = None
    if '--threads' in sys.argv:
        tovr = int(sys.argv[sys.argv.index('--threads') + 1])
    wkr = 1
    if '--worker' in sys.argv:
        wkr = int(sys.argv[sys.argv.index('--worker') + 1])
    run(limit, only, tovr, wkr)
