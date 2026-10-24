# -*- coding: utf-8 -*-
"""三條修正後結算路徑嘅 SELECT 版 dry-run：抽 10 行已完場記錄，
print 舊結果（closing 口徑）vs 新結果（stored-first 口徑）對比。唔寫庫。"""
import os
import re
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get('DB_PATH') or os.path.join(BASE, 'football.db')
conn = sqlite3.connect(DB, timeout=60)


def pick_result(hc, gv, hs, aws):
    if hc is None or hs is None or aws is None:
        return None
    mg = hs - aws - hc if gv == 'home' else (
        aws - hs - hc if gv == 'away' else hs - aws)
    return 'A' if mg > 0 else ('B' if mg < 0 else 'P')


def dir_res(r, d):
    if r is None or d not in ('up', 'down'):
        return None
    return 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')


print('=== 1) fw8x.settle 口徑（stored line_h/giver，NULL 先 closing）===')
rows = conn.execute(
    'SELECT f.match_id, f.direction, f.result, f.line_h, f.giver, '
    'oc.handicap, oc.giver, m.home_score, m.away_score FROM fw8x_log f '
    'JOIN matches m ON m.id=f.match_id '
    'LEFT JOIN odds_asian oc ON oc.match_id=f.match_id '
    "AND oc.label='closing' AND oc.company_id=12 "
    'WHERE f.result IS NOT NULL AND m.home_score IS NOT NULL '
    'ORDER BY f.match_id DESC LIMIT 10').fetchall()
for mid, d, old, lh, lgv, chc, cgv, hs, aws in rows:
    hc, gv = (lh, lgv) if lh is not None else (chc, cgv)
    new = dir_res(pick_result(hc, gv, hs, aws), d)
    old_c = dir_res(pick_result(chc, cgv, hs, aws), d)
    print('  mid=%s dir=%s 比分=%s-%s | stored=%s/%s → new=%s | closing=%s/%s → old=%s %s'
          % (mid, d, hs, aws, lh, lgv, new, chc, cgv, old_c,
             '← 現庫存 ' + str(old)))

print()
print('=== 2) _fw8s_settle 口徑（解析 stored line 文字）===')
NAME = {'平手': 0.0, '平/半': 0.25, '半球': 0.5, '半/一': 0.75, '一球': 1.0,
        '一/球半': 1.25, '球半': 1.5, '球半/兩': 1.75, '兩球': 2.0,
        '兩/兩球半': 2.25, '兩球半': 2.5}


def parse(s):
    if not s:
        return None
    s = str(s).strip()
    if s == '平手':
        return (0.0, None)
    if s.startswith('主讓'):
        gv, name = 'home', s[2:]
    elif s.startswith('客讓'):
        gv, name = 'away', s[2:]
    else:
        gv, name = None, s
    h = NAME.get(name)
    if h is None:
        m = re.match(r'-?\d+(?:\.\d+)?', name)
        h = float(m.group(0)) if m else None
    return (h, gv) if h is not None else None


rows = conn.execute(
    'SELECT f.match_id, f.result, f.line, oc.handicap, oc.giver, '
    'm.home_score, m.away_score FROM fw8s_log f '
    'JOIN matches m ON m.id=f.match_id '
    'LEFT JOIN odds_asian oc ON oc.match_id=f.match_id '
    "AND oc.label='closing' AND oc.company_id=12 "
    'WHERE f.result IS NOT NULL AND m.home_score IS NOT NULL '
    'ORDER BY f.match_id DESC LIMIT 10').fetchall()
for mid, old, ltxt, chc, cgv, hs, aws in rows:
    pl = parse(ltxt)
    hc, gv = pl if pl is not None else (chc, cgv)
    r = pick_result(hc, gv, hs, aws)
    new = 'P' if r == 'P' else (('W' if r == 'A' else 'L') if r else None)
    r0 = pick_result(chc, cgv, hs, aws)
    old_c = 'P' if r0 == 'P' else (('W' if r0 == 'A' else 'L') if r0 else None)
    print('  mid=%s 比分=%s-%s | stored line=%r → %s/%s → new=%s | closing=%s/%s → old=%s %s'
          % (mid, hs, aws, ltxt, hc, gv, new, chc, cgv, old_c,
             '← 現庫存 ' + str(old)))

print()
print('=== 3) feat_hourly 段口徑（stored ah_handicap/ah_giver）===')
rows = conn.execute(
    'SELECT f.id, f.match_id, f.direction, f.result, f.ah_handicap, '
    'f.ah_giver, oc.handicap, oc.giver, m.home_score, m.away_score '
    'FROM feat_hourly f JOIN matches m ON m.id=f.match_id '
    'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
    "AND oc.label='closing' AND oc.company_id=12 "
    'WHERE f.result IS NOT NULL AND m.home_score IS NOT NULL '
    'ORDER BY f.id DESC LIMIT 10').fetchall()
for fid, mid, d, old, shc, sgv, chc, cgv, hs, aws in rows:
    hc, gv = (shc, sgv) if shc is not None else (chc, cgv)
    new = dir_res(pick_result(hc, gv, hs, aws), d)
    old_c = dir_res(pick_result(chc, cgv, hs, aws), d)
    print('  id=%s mid=%s dir=%s 比分=%s-%s | stored=%s/%s → new=%s | closing=%s/%s → old=%s %s'
          % (fid, mid, d, hs, aws, shc, sgv, new, chc, cgv, old_c,
             '← 現庫存 ' + str(old)))

print()
print('=== 2996179 直接重播 ===')
r = conn.execute(
    'SELECT line_h, giver, direction, result FROM fw8x_log '
    'WHERE match_id=2996179').fetchone()
hs, aws = conn.execute(
    'SELECT home_score, away_score FROM matches WHERE id=2996179').fetchone()
mg = hs - aws - r[0]
print('  line_h=%s giver=%s dir=%s 比分=%s-%s → mg=%+.2f → result=%s %s'
      % (r[0], r[1], r[2], hs, aws, mg, r[3],
         'OK' if r[3] == 'P' else '(重結算前仍係舊值，run 腳本後會變 P)'))
conn.close()
