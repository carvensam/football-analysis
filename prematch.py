# -*- coding: utf-8 -*-
"""開賽前對賽數據（43 項）重建工具。

對每一場已完場賽事，計算該場開踢前一刻，主隊與客隊在所属排名圈內的：
  總排名 / 總入球 / 總失球 / 總積分、
  主場排名 / 主場入球 / 主場失球 / 主場戰績(贏/和/輸) / 主場積分、
  客場排名 / 客場入球 / 客場失球 / 客場戰績(贏/和/輸) / 客場積分
（各 18 項，主客合計 36 項），另加場均入球：
  主隊場均入球(總/主/客)、客隊場均入球(總/主/客)、兩隊場均入球相加，
合計 43 項，寫入 match_prestandings。

排名圈（universe）規則：
  - 聯賽：同一賽季全部場次
  - 杯賽：同一賽季且同一 round_label（分組賽 -> 同組；聯賽階段 -> 整個階段；淘汰賽 -> 同階段）
只計算開賽時間早於該場的完場賽事（嚴格早於，同時開賽不計）。
未出賽球隊（played=0）排名記 NULL，其餘記 0。
排名次序：積分(贏3/和1/輸0) -> 淨勝球（正數最大至負數最大）-> 入球多者，並列同名次（1224 式）。

用法：
  python prematch.py              # 重建全部賽季
  python prematch.py <season_id>  # 只重建指定賽季
"""
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCOPES = ('total', 'home', 'away')
SIDES = ('home', 'away')
FIELDS = ('rank', 'gf', 'ga', 'w', 'd', 'l', 'pts')
COLS = ['%s_%s_%s' % (side, scope, field)
        for side in SIDES for scope in SCOPES for field in FIELDS]
# 場均入球 7 欄（REAL，小數點後 2 位；未出賽記 NULL；任一方 NULL 則相加欄亦 NULL）
AVG_COLS = ['home_avg_gf_total', 'home_avg_gf_home', 'home_avg_gf_away',
            'away_avg_gf_total', 'away_avg_gf_home', 'away_avg_gf_away',
            'avg_total_goals']
COLS = COLS + AVG_COLS

UPSERT = ('INSERT INTO match_prestandings(match_id,' + ','.join(COLS) + ') '
          'VALUES(?' + ',?' * len(COLS) + ') '
          'ON CONFLICT(match_id) DO UPDATE SET ' +
          ','.join('%s=excluded.%s' % (c, c) for c in COLS))

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS match_prestandings (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id),
    home_total_rank INTEGER, home_total_gf INTEGER, home_total_ga INTEGER,
    home_total_w INTEGER, home_total_d INTEGER, home_total_l INTEGER, home_total_pts INTEGER,
    home_home_rank  INTEGER, home_home_gf  INTEGER, home_home_ga  INTEGER,
    home_home_w  INTEGER, home_home_d  INTEGER, home_home_l  INTEGER, home_home_pts INTEGER,
    home_away_rank  INTEGER, home_away_gf  INTEGER, home_away_ga  INTEGER,
    home_away_w  INTEGER, home_away_d  INTEGER, home_away_l  INTEGER, home_away_pts INTEGER,
    away_total_rank INTEGER, away_total_gf INTEGER, away_total_ga INTEGER,
    away_total_w INTEGER, away_total_d INTEGER, away_total_l INTEGER, away_total_pts INTEGER,
    away_home_rank  INTEGER, away_home_gf  INTEGER, away_home_ga  INTEGER,
    away_home_w  INTEGER, away_home_d  INTEGER, away_home_l  INTEGER, away_home_pts INTEGER,
    away_away_rank  INTEGER, away_away_gf  INTEGER, away_away_ga  INTEGER,
    away_away_w  INTEGER, away_away_d  INTEGER, away_away_l  INTEGER, away_away_pts INTEGER,
    home_avg_gf_total REAL, home_avg_gf_home REAL, home_avg_gf_away REAL,
    away_avg_gf_total REAL, away_avg_gf_home REAL, away_avg_gf_away REAL,
    avg_total_goals REAL
);
"""


def _new_stats():
    return {sc: {'p': 0, 'w': 0, 'd': 0, 'l': 0, 'gf': 0, 'ga': 0}
            for sc in SCOPES}


def _apply(stats, team, scope, gf, ga):
    st = stats.setdefault(team, _new_stats())
    sc = st[scope]
    sc['p'] += 1
    sc['gf'] += gf
    sc['ga'] += ga
    if gf > ga:
        sc['w'] += 1
    elif gf == ga:
        sc['d'] += 1
    else:
        sc['l'] += 1


def _snapshot(stats):
    """由目前累計計算每隊各 scope 的 (rank, gf, ga, w, d, l, pts)。
    排名次序：積分(3/1/0) -> 淨勝球 -> 入球，並列同名次（1224 式）。"""
    result = {}
    for sc in SCOPES:
        rows = []
        for team, st in stats.items():
            s = st[sc]
            if s['p'] > 0:
                pts = 3 * s['w'] + s['d']
                gd = s['gf'] - s['ga']
                rows.append((team, pts, gd, s['gf'],
                             s['gf'], s['ga'], s['w'], s['d'], s['l'], pts))
        rows.sort(key=lambda r: (-r[1], -r[2], -r[3], r[0]))
        prev_key = None
        prev_rank = 0
        for i, r in enumerate(rows):
            key = r[1:4]                       # (pts, gd, gf)
            rank = i + 1 if key != prev_key else prev_rank
            prev_key, prev_rank = key, rank
            # (rank, gf, ga, w, d, l, pts)
            result.setdefault(r[0], {})[sc] = (rank, r[4], r[5], r[6], r[7], r[8], r[9])
    return result


def _avg_gf(stats, team, scope):
    """該隊該 scope 的場均入球（gf/場數，2 位小數）；未出賽記 None。"""
    s = stats.get(team, {}).get(scope)
    if not s or s['p'] == 0:
        return None
    return round(s['gf'] / s['p'], 2)


def build_season(conn, season_id, category):
    matches = conn.execute(
        "SELECT id, kickoff, home_id, away_id, home_score, away_score, "
        "round_label FROM matches WHERE season_id=? AND home_score IS NOT NULL "
        "ORDER BY kickoff, id", (season_id,)).fetchall()

    if category == '杯賽':
        groups = {}
        for m in matches:
            groups.setdefault(m[6] or '', []).append(m)
    else:
        groups = {'': matches}

    n = 0
    for gms in groups.values():
        # 排名圈內出現過的所有球隊（含未出賽）
        stats = {}
        for m in gms:
            stats.setdefault(m[2], _new_stats())
            stats.setdefault(m[3], _new_stats())
        for mid, _ko, hid, aid, hs, as_, _rl in gms:
            snap = _snapshot(stats)
            vals = []
            for team in (hid, aid):
                for sc in SCOPES:
                    info = snap.get(team, {}).get(sc)
                    vals += list(info) if info else [None, 0, 0, 0, 0, 0, 0]
            h_avg = [_avg_gf(stats, hid, sc) for sc in SCOPES]
            a_avg = [_avg_gf(stats, aid, sc) for sc in SCOPES]
            combined = (round(h_avg[0] + a_avg[0], 2)
                        if h_avg[0] is not None and a_avg[0] is not None else None)
            vals += h_avg + a_avg + [combined]
            conn.execute(UPSERT, [mid] + vals)
            _apply(stats, hid, 'total', hs, as_)
            _apply(stats, hid, 'home', hs, as_)
            _apply(stats, aid, 'total', as_, hs)
            _apply(stats, aid, 'away', as_, hs)
            n += 1
    return n


def build_all(conn, only_season_id=None):
    conn.execute('DROP TABLE IF EXISTS match_prestandings')
    conn.executescript(CREATE_TABLE)
    seasons = conn.execute(
        'SELECT s.id, c.category FROM seasons s '
        'JOIN competitions c ON c.titan_id=s.titan_id').fetchall()
    total = 0
    for season_id, category in seasons:
        if only_season_id and season_id != only_season_id:
            continue
        total += build_season(conn, season_id, category)
    conn.commit()
    return total


if __name__ == '__main__':
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'football.db'))
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print('重建場數:', build_all(conn, sid))
