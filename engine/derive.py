# -*- coding: utf-8 -*-
"""離線衍生數據：近六場同賽事戰績積分 + 近20場贏盤率（尾盤結算）。

寫入 match_derived（每場已完場比賽一列）：
  home_last6_w/d/l/pts、away_last6_w/d/l/pts
    該場開賽前，主/客隊在同一 titan007 賽事（跨季）最近 6 場的 勝/和/負/積分（3/1/0）。
  home_hw_winrate_home / home_hw_winrate_all
    主隊近 20 場「只計主場」/「主客全部」賽事的贏盤率（以 odds_asian 尾盤結算，走盤剔除）。
  away_hw_winrate_away / away_hw_winrate_all
    客隊近 20 場「只計客場」/「主客全部」賽事的贏盤率。

贏盤判定（以該隊視角）：讓球方 淨勝-讓球數；受讓方 淨勝+讓球數；>0 贏、<0 輸、=0 走盤。
無尾盤的場次不計入分母；分母為 0 記 NULL。

用法：
  python derive.py            全庫重建
  python derive.py <match_id> 只重建指定場次所在賽事（測試）
"""
import os
import sqlite3
import sys
from collections import defaultdict, deque

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WINDOW_FORM = 6      # 近六場
WINDOW_HW = 20       # 近20場

UPSERT = (
    'INSERT INTO match_derived(match_id,'
    'home_last6_w,home_last6_d,home_last6_l,home_last6_pts,'
    'away_last6_w,away_last6_d,away_last6_l,away_last6_pts,'
    'home_hw_winrate_home,home_hw_winrate_all,'
    'away_hw_winrate_away,away_hw_winrate_all) '
    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
    'ON CONFLICT(match_id) DO UPDATE SET '
    'home_last6_w=excluded.home_last6_w,home_last6_d=excluded.home_last6_d,'
    'home_last6_l=excluded.home_last6_l,home_last6_pts=excluded.home_last6_pts,'
    'away_last6_w=excluded.away_last6_w,away_last6_d=excluded.away_last6_d,'
    'away_last6_l=excluded.away_last6_l,away_last6_pts=excluded.away_last6_pts,'
    'home_hw_winrate_home=excluded.home_hw_winrate_home,'
    'home_hw_winrate_all=excluded.home_hw_winrate_all,'
    'away_hw_winrate_away=excluded.away_hw_winrate_away,'
    'away_hw_winrate_all=excluded.away_hw_winrate_all')


def handicap_outcome(team_is_home, team_score, opp_score, giver, handicap):
    """回傳 'win'/'lose'/'push'/None（無盤）"""
    if handicap is None:
        return None
    margin = team_score - opp_score
    gives = (giver == 'home' and team_is_home) or \
            (giver == 'away' and not team_is_home)
    adj = margin - handicap if gives else margin + handicap
    if adj > 0:
        return 'win'
    if adj < 0:
        return 'lose'
    return 'push'


def rate(win, lose):
    denom = win + lose
    if denom == 0:
        return None
    return round(win / denom, 4)


def build(conn, only_titan=None):
    """逐 titan007 賽事單次掃描（按開賽時間順序），維護每隊滑動視窗。"""
    sql = (
        'SELECT c.titan_id, m.id, m.kickoff, m.home_id, m.away_id, '
        'm.home_score, m.away_score, o.handicap, o.giver '
        'FROM matches m '
        'JOIN seasons s ON s.id = m.season_id '
        'JOIN competitions c ON c.titan_id = s.titan_id '
        'LEFT JOIN odds_asian o ON o.match_id = m.id AND o.label = "closing" '
        'AND o.company_id = 12 '
        'WHERE m.home_score IS NOT NULL')
    args = ()
    if only_titan is not None:
        sql += ' AND c.titan_id = ?'
        args = (only_titan,)
    sql += ' ORDER BY c.titan_id, m.kickoff, m.id'

    form = defaultdict(lambda: deque(maxlen=WINDOW_FORM))   # team -> ['W'/'D'/'L',...]
    # team -> deque(maxlen=WINDOW_HW) of (venue_filter_outcomes)
    hw = defaultdict(lambda: deque(maxlen=WINDOW_HW))       # (is_home, outcome)

    n = 0
    cur_titan = None
    for titan, mid, _ko, hid, aid, hs, as_, hp, giver in conn.execute(sql, args):
        if cur_titan is not None and titan != cur_titan:
            form.clear()
            hw.clear()
        cur_titan = titan

        # ---- 近六場積分（賽前）
        def form_stats(team):
            w = form[team].count('W')
            d = form[team].count('D')
            l = form[team].count('L')
            if not form[team]:
                return None, None, None, None
            return w, d, l, 3 * w + d

        hw_l, hw_d, hw_lo, hw_pts = form_stats(hid)
        aw_l, aw_d, aw_lo, aw_pts = form_stats(aid)

        # ---- 近20場贏盤率（賽前）
        def hw_rates(team, venue):
            """venue: 'home' 只計主場 / 'all' 全部。回傳 (win,lose) 或 (0,0)"""
            w = l = 0
            for is_home, out in hw[team]:
                if venue == 'home' and not is_home:
                    continue
                if venue == 'away' and is_home:
                    continue
                if out == 'win':
                    w += 1
                elif out == 'lose':
                    l += 1
            return w, l

        hwh_w, hwh_l = hw_rates(hid, 'home')
        hwa_w, hwa_l = hw_rates(hid, 'all')
        awa_w, awa_l = hw_rates(aid, 'away')
        awl_w, awl_l = hw_rates(aid, 'all')

        conn.execute(UPSERT, (
            mid, hw_l, hw_d, hw_lo, hw_pts, aw_l, aw_d, aw_lo, aw_pts,
            rate(hwh_w, hwh_l), rate(hwa_w, hwa_l),
            rate(awa_w, awa_l), rate(awl_w, awl_l)))
        n += 1

        # ---- 賽後更新滑動視窗
        form[hid].append('W' if hs > as_ else ('D' if hs == as_ else 'L'))
        form[aid].append('W' if as_ > hs else ('D' if hs == as_ else 'L'))
        out_h = handicap_outcome(True, hs, as_, giver, hp)
        out_a = handicap_outcome(False, as_, hs, giver, hp)
        hw[hid].append((True, out_h))
        hw[aid].append((False, out_a))

    conn.commit()
    return n


if __name__ == '__main__':
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'football.db'))
    sys.path.insert(0, BASE_DIR)
    import engine.extra_schema as es
    es.migrate(conn)
    only = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if only:
        only = conn.execute(
            'SELECT c.titan_id FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id WHERE m.id=?',
            (only,)).fetchone()[0]
    else:
        conn.execute('DROP TABLE IF EXISTS match_derived')
        es.migrate(conn)
    print('重建場數:', build(conn, only))
