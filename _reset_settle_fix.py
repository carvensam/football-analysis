# -*- coding: utf-8 -*-
"""_reset_settle_fix.py — 一次性歷史重結算（2026-10-25 落注時盤口徑修正）

背景：6 個結算路徑舊時用 odds_asian closing 盤結算，令走盤場計錯
（實證 matches.id=2996179：落注時主讓一球 line_h=1.0，3:2 淨勝 1 球
應係 P；closing 變 1.25 舊邏輯計成 W）。

呢個腳本用修正後嘅口徑（同修正後 app.py / fw8x.py 語義一致）重結算
所有已 settled 行（result 已有值都重計）：

  fw8x_log      用 stored line_h/giver；line_h NULL（legacy）先對 closing
  fw8s_log      用解析 stored line（fmt_line 中文盤文字）；解析唔到先 closing
  feat_hourly   用 stored ah_handicap/ah_giver（嗰個鐘頭影低嘅盤）；
                NULL 先對 closing
  newjx_log     用 stored hc/giver（重計係保險，預期零改動）
  w88_log       用 stored hc/giver/ou_line 行 w88 語義（重計係保險）
  hb_pick_log   用 stored line 對大小（重計係保險）
  featured / featured_z / v1_featured / v1_featured_z / v3_featured /
  fw_grid_featured：hc IS NULL 嘅 legacy 行一律 skip（唔准靠估）；
                只重計有 stored hc 嘅行（將來新 INSERT 嘅行）

每表一個短事務（WAL mode 下同其他進程並發，唔會鎖晒成個庫）。
用法：python3 _reset_settle_fix.py          # 實際重結算
      python3 _reset_settle_fix.py --dry    # 只打印對比，唔寫庫
"""
import os
import re
import sqlite3
import sys
import time
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get('DB_PATH') or os.path.join(BASE, 'football.db')
DRY = '--dry' in sys.argv


def pick_result(hc, gv, hs, aws):
    """同 app.pick_result / fw8x.pick_result：A=上盤贏 B=下盤贏 P=走。"""
    if hc is None or hs is None or aws is None:
        return None
    if gv == 'home':
        mg = hs - aws - hc
    elif gv == 'away':
        mg = aws - hs - hc
    else:                       # 平手盤
        mg = hs - aws
    return 'A' if mg > 0 else ('B' if mg < 0 else 'P')


def dir_result(r, d):
    """方向結算：direction up=揀上盤。P→P；A==up→W 否則 L。"""
    if r is None or d not in ('up', 'down'):
        return None
    return 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')


def w88_settle_one(market, hs, aws, hc, giver, ou_line):
    """照抄 w88_engine._settle_one（純函數，唔 import 引擎避免副作用）。"""
    tot = hs + aws
    if market in ('ah_up', 'ah_down'):
        if hc is None:
            return None
        if giver == 'home':
            mg = hs - aws - hc
        elif giver == 'away':
            mg = aws - hs - hc
        else:
            mg = hs - aws
        if abs(mg) < 1e-9:
            return 'P'
        win = mg > 0
        if market == 'ah_down':
            win = not win
        return 'W' if win else 'L'
    if market == 'draw':
        return 'W' if hs == aws else 'L'
    if market == 'even':
        return 'W' if tot % 2 == 0 else 'L'
    if market in ('over', 'under'):
        if ou_line is None:
            return None
        if abs(tot - ou_line) < 1e-9:
            return 'P'
        win = tot > ou_line
        if market == 'under':
            win = not win
        return 'W' if win else 'L'
    if market == 'home_win':
        return 'W' if hs > aws else 'L'
    if market == 'away_win':
        return 'W' if hs < aws else 'L'
    return None


_LINE_TXT_TO_H = {}


def parse_line_txt(s):
    """fw8s_log.line（fmt_line 中文格式）→ (handicap, giver)；失敗 None。"""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    if not _LINE_TXT_TO_H:
        names = {'平手': 0.0, '平/半': 0.25, '半球': 0.5, '半/一': 0.75,
                 '一球': 1.0, '一/球半': 1.25, '球半': 1.5, '球半/兩': 1.75,
                 '兩球': 2.0, '兩/兩球半': 2.25, '兩球半': 2.5, '兩球半/三': 2.75,
                 '三球': 3.0, '三/三球半': 3.25, '三球半': 3.5, '三球半/四': 3.75,
                 '四球': 4.0}
        _LINE_TXT_TO_H.update(names)
    if s == '平手':
        return (0.0, None)
    if s.startswith('主讓'):
        gv, name = 'home', s[2:]
    elif s.startswith('客讓'):
        gv, name = 'away', s[2:]
    else:
        gv, name = None, s
    h = _LINE_TXT_TO_H.get(name)
    if h is None:
        m = re.match(r'-?\d+(?:\.\d+)?', name)
        if not m:
            return None
        h = float(m.group(0))
    return (h, gv)


def migrate_pick_line_cols(conn):
    """六張精選表補 hc/giver（idempotent）——同 app.py 同款。
    舊行留 NULL：重結算 skip 佢哋（唔准靠估）。
    寫鎖要同 server／爬蟲搶：失敗會重試；最終失敗回傳 False，
    呼叫者跳過六張精選表（legacy 行全部 hc NULL，本來就冇行會改）。"""
    for attempt in range(30):
        try:
            for t in ('featured', 'featured_z', 'v1_featured', 'v1_featured_z',
                      'v3_featured', 'fw_grid_featured'):
                cols = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % t)}
                if 'hc' not in cols:
                    conn.execute('ALTER TABLE %s ADD COLUMN hc REAL' % t)
                if 'giver' not in cols:
                    conn.execute('ALTER TABLE %s ADD COLUMN giver TEXT' % t)
            conn.commit()
            return True
        except sqlite3.OperationalError:
            conn.rollback()
            time.sleep(20)
    return False


def snap(conn, tbl):
    """BEFORE result 分佈打印。"""
    rows = conn.execute('SELECT result, COUNT(*) FROM %s '
                        'GROUP BY result ORDER BY result' % tbl).fetchall()
    print('  BEFORE %s: %s' % (tbl, dict(rows)))
    return dict(rows)


def finish(conn, tbl, checked, todo, transitions):
    """執行 UPDATE（每表一個短事務）＋AFTER 打印。todo=[(key..., old, new)]"""
    n_upd = 0
    if todo and not DRY:
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            for item in todo:
                new = item[-1]
                key = item[:-2]
                if tbl == 'feat_hourly':
                    conn.execute('UPDATE feat_hourly SET result=? WHERE id=?',
                                 (new, key[0]))
                elif tbl == 'fw_grid_featured':
                    conn.execute('UPDATE fw_grid_featured SET result=? '
                                 'WHERE grid=? AND match_id=?', (new, key[0], key[1]))
                elif tbl == 'w88_log':
                    conn.execute('UPDATE w88_log SET result=?, settled_at=? '
                                 'WHERE match_id=? AND market=?',
                                 (new, now, key[0], key[1]))
                else:
                    conn.execute('UPDATE %s SET result=? WHERE match_id=?' % tbl,
                                 (new, key[0]))
                n_upd += 1
                if n_upd % 5000 == 0:
                    conn.commit()   # 分段放鎖：每 5000 行一個短事務
            conn.commit()
        except sqlite3.OperationalError:
            # 爬蟲長寫交易搶鎖：等 60 秒重来一次；再失敗就 rollback 留待下輪
            conn.rollback()
            time.sleep(60)
            try:
                for item in todo:
                    new = item[-1]
                    key = item[:-2]
                    if tbl == 'feat_hourly':
                        conn.execute('UPDATE feat_hourly SET result=? WHERE id=?',
                                     (new, key[0]))
                    elif tbl == 'fw_grid_featured':
                        conn.execute(
                            'UPDATE fw_grid_featured SET result=? '
                            'WHERE grid=? AND match_id=?', (new, key[0], key[1]))
                    elif tbl == 'w88_log':
                        conn.execute(
                            'UPDATE w88_log SET result=?, settled_at=? '
                            'WHERE match_id=? AND market=?',
                            (new, now, key[0], key[1]))
                    else:
                        conn.execute(
                            'UPDATE %s SET result=? WHERE match_id=?' % tbl,
                            (new, key[0]))
                    n_upd += 1
                    if n_upd % 5000 == 0:
                        conn.commit()
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()
                print('  !! %s 寫入搶唔到鎖，今輪跳过（rerun 可補）' % tbl)
    elif todo and DRY:
        n_upd = len(todo)   # dry-run：報告「將會改」嘅行數
    print('  AFTER  %s: 重計 %d 行，改 %d 行；轉移 %s' %
          (tbl, checked, n_upd,
           dict(transitions) if transitions else '{}'))
    rows = conn.execute('SELECT result, COUNT(*) FROM %s '
                        'GROUP BY result ORDER BY result' % tbl).fetchall()
    print('  AFTER  %s 分佈: %s' % (tbl, dict(rows)))


def fix_dir_table(conn, tbl, pkey='match_id'):
    """featured×5 + fw_grid_featured：只重計有 stored hc 嘅已結算行。"""
    snap(conn, tbl)
    if pkey == 'grid':
        rows = conn.execute(
            'SELECT x.grid, x.match_id, x.direction, x.result, x.hc, x.giver '
            'FROM %s x JOIN matches m ON m.id=x.match_id '
            "WHERE x.result IS NOT NULL AND x.hc IS NOT NULL "
            'AND m.home_score IS NOT NULL' % tbl).fetchall()
    else:
        rows = conn.execute(
            'SELECT x.match_id, x.direction, x.result, x.hc, x.giver '
            'FROM %s x JOIN matches m ON m.id=x.match_id '
            "WHERE x.result IS NOT NULL AND x.hc IS NOT NULL "
            'AND m.home_score IS NOT NULL' % tbl).fetchall()
    todo = []
    tr = Counter()
    for r in rows:
        if pkey == 'grid':
            g, mid, d, old, hc, gv = r
            key = (g, mid)
        else:
            mid, d, old, hc, gv = r
            key = (mid,)
        new = dir_result(pick_result(hc, gv, *scores(conn, mid)), d)
        if new is not None and new != old:
            todo.append(key + (old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, len(rows), todo, tr)


_SCORES_CACHE = {}


def scores(conn, mid):
    if mid not in _SCORES_CACHE:
        _SCORES_CACHE[mid] = conn.execute(
            'SELECT home_score, away_score FROM matches WHERE id=?',
            (mid,)).fetchone()
        if len(_SCORES_CACHE) > 200000:
            _SCORES_CACHE.clear()
    s = _SCORES_CACHE[mid]
    return (s[0], s[1]) if s else (None, None)


def closing_line(conn, mid):
    r = conn.execute(
        "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
        "AND label='closing' AND company_id=12", (mid,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def fix_fw8x(conn):
    tbl = 'fw8x_log'
    snap(conn, tbl)
    rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.line_h, f.giver '
        'FROM fw8x_log f JOIN matches m ON m.id=f.match_id '
        'WHERE f.result IS NOT NULL AND m.home_score IS NOT NULL').fetchall()
    todo = []
    tr = Counter()
    for mid, d, old, lh, lgv in rows:
        hs, aws = scores(conn, mid)
        hc, gv = (lh, lgv) if lh is not None else closing_line(conn, mid)
        new = dir_result(pick_result(hc, gv, hs, aws), d)
        if new is not None and new != old:
            todo.append((mid, old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, len(rows), todo, tr)


def fix_fw8s(conn):
    tbl = 'fw8s_log'
    snap(conn, tbl)
    rows = conn.execute(
        'SELECT f.match_id, f.result, f.line FROM fw8s_log f '
        'JOIN matches m ON m.id=f.match_id '
        'WHERE f.result IS NOT NULL AND m.home_score IS NOT NULL').fetchall()
    todo = []
    tr = Counter()
    for mid, old, ltxt in rows:
        hs, aws = scores(conn, mid)
        pl = parse_line_txt(ltxt)
        hc, gv = pl if pl is not None else closing_line(conn, mid)
        r = pick_result(hc, gv, hs, aws)
        new = 'P' if r == 'P' else (('W' if r == 'A' else 'L') if r else None)
        if new is not None and new != old:
            todo.append((mid, old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, len(rows), todo, tr)


def fix_feat_hourly(conn):
    tbl = 'feat_hourly'
    snap(conn, tbl)
    rows = conn.execute(
        'SELECT f.id, f.match_id, f.direction, f.result, f.ah_handicap, '
        'f.ah_giver FROM feat_hourly f JOIN matches m ON m.id=f.match_id '
        'WHERE f.result IS NOT NULL AND m.home_score IS NOT NULL').fetchall()
    todo = []
    tr = Counter()
    n = 0
    for fid, mid, d, old, shc, sgv in rows:
        hs, aws = scores(conn, mid)
        hc, gv = (shc, sgv) if shc is not None else closing_line(conn, mid)
        new = dir_result(pick_result(hc, gv, hs, aws), d)
        n += 1
        if new is not None and new != old:
            todo.append((fid, old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, n, todo, tr)


def fix_newjx(conn):
    tbl = 'newjx_log'
    snap(conn, tbl)
    rows = conn.execute(
        'SELECT k.match_id, k.dir, k.result, k.hc, k.giver '
        'FROM newjx_log k JOIN matches m ON m.id=k.match_id '
        'WHERE k.result IS NOT NULL AND m.home_score IS NOT NULL '
        'AND k.hc IS NOT NULL').fetchall()
    todo = []
    tr = Counter()
    for mid, d, old, hc, gv in rows:
        hs, aws = scores(conn, mid)
        new = dir_result(pick_result(hc, gv, hs, aws), d)
        if new is not None and new != old:
            todo.append((mid, old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, len(rows), todo, tr)


def fix_w88(conn):
    tbl = 'w88_log'
    snap(conn, tbl)
    rows = conn.execute(
        'SELECT k.match_id, k.market, k.result, k.hc, k.giver, k.ou_line, '
        'm.home_score, m.away_score FROM w88_log k '
        'JOIN matches m ON m.id=k.match_id '
        'WHERE k.result IS NOT NULL AND m.home_score IS NOT NULL').fetchall()
    todo = []
    tr = Counter()
    for mid, market, old, hc, gv, ou, hs, aws in rows:
        new = w88_settle_one(market, hs, aws, hc, gv, ou)
        if new is not None and new != old:
            todo.append((mid, market, old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, len(rows), todo, tr)


def fix_hb(conn):
    tbl = 'hb_pick_log'
    snap(conn, tbl)
    rows = conn.execute(
        'SELECT h.match_id, h.side, h.result, h.line, '
        'm.home_score, m.away_score FROM hb_pick_log h '
        'JOIN matches m ON m.id=h.match_id '
        'WHERE h.result IS NOT NULL AND m.home_score IS NOT NULL '
        'AND h.line IS NOT NULL').fetchall()
    todo = []
    tr = Counter()
    for mid, side, old, line, hs, aws in rows:
        tot = hs + aws
        if tot == line:
            new = 'P'
        else:
            new = 'W' if (tot > line) == (side == '大') else 'L'
        if new != old:
            todo.append((mid, old, new))
            tr['%s→%s' % (old, new)] += 1
    finish(conn, tbl, len(rows), todo, tr)


def verify_2996179(conn):
    """實證案例重播：fw8x_log 2996179 必須係 P。"""
    r = conn.execute(
        'SELECT result, line_h, giver, direction FROM fw8x_log '
        'WHERE match_id=2996179').fetchone()
    sc = conn.execute('SELECT home_score, away_score FROM matches '
                      'WHERE id=2996179').fetchone()
    cl = closing_line(conn, 2996179)
    print('\n=== 2996179 驗證 ===')
    print('  stored: %s；closing: %s；比分: %s' % (r, cl, sc))
    if r:
        hs, aws = sc
        mg_stored = (hs - aws - r[1]) if r[2] == 'home' else (aws - hs - r[1])
        mg_close = (hs - aws - cl[0]) if cl[1] == 'home' else (aws - hs - cl[0])
        print('  stored line %.2f(%s) → mg=%+.2f → %s' %
              (r[1], r[2], mg_stored,
               'P' if abs(mg_stored) < 1e-9 else (
                   'A' if mg_stored > 0 else 'B')))
        print('  closing line %.2f(%s) → mg=%+.2f → %s（舊錯誤口徑）' %
              (cl[0], cl[1], mg_close,
               'P' if abs(mg_close) < 1e-9 else (
                   'A' if mg_close > 0 else 'B')))
        print('  重結算後 result=%s（期望 P）→ %s' % (r[0], 'OK' if r[0] == 'P'
                                                    else 'FAIL'))


def main():
    conn = sqlite3.connect(DB, timeout=120)
    conn.execute('PRAGMA busy_timeout=120000')
    mig_ok = migrate_pick_line_cols(conn)
    print('DB: %s%s' % (DB, '（--dry 只讀）' if DRY else ''))
    print('====== BEFORE 快照 + 重結算 ======')
    fix_fw8x(conn)
    fix_fw8s(conn)
    fix_feat_hourly(conn)
    fix_newjx(conn)
    fix_w88(conn)
    fix_hb(conn)
    print('\n------ 六張精選表（hc IS NULL legacy 行 skip）------')
    if mig_ok:
        for t in ('featured', 'featured_z', 'v1_featured', 'v1_featured_z',
                  'v3_featured'):
            fix_dir_table(conn, t)
        fix_dir_table(conn, 'fw_grid_featured', pkey='grid')
    else:
        print('!! hc/giver 遷移搶唔到寫鎖（30×20 秒）——跳過六張精選表。'
              '全部 legacy 行 hc 本來就係 NULL，冇行會被改；'
              'server 重啟後 app.py 會自己補遷移，再 rerun 呢個腳本即可。')
    verify_2996179(conn)
    conn.close()


if __name__ == '__main__':
    main()
