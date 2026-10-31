# -*- coding: utf-8 -*-
"""requalify_after_backfill.py — 補數後全精選系統資格重算（2026-10-21）

順序執行：
  P1  featured 家族逐場重評：featured / featured_z / v1_featured / v1_featured_z /
      v3_featured（精選W、精選7、精選12 等）——合資格加入，未結算唔合資格先移走
  P1b fw_grid_featured 格 7/8/12/8s：預設重評受影響場次；--full 全部完場
  P2  新精選：保留原有記錄，newjx_backfill 回查並重結算受變動記錄
  P3  88W：保留原有記錄，w88_backfill 回查並重結算受變動記錄
  已結算記錄即使重評唔合格都保留作歷史，唔再刪除。
  P4  精選8s 回測庫：fw8s_backtest.py --reset 全量重建（另一個 db，唔阻主庫）
  P5  8X 回測庫：預設 2026 專用表；--full 另寫全歷史表，兩者唔混算
  P6  對比報告：各表重算前後計數＋命中率變化 → 重算對比報告_<日期>.md

進度寫 logs/requalify_state.json 俾 app 掣輪詢。
用法：
  python requalify_after_backfill.py            # 全部
  python requalify_after_backfill.py --phase P1 # 單一階段
  python requalify_after_backfill.py --limit 200 # P1 限場數（試速）
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback

WS = os.path.dirname(os.path.abspath(__file__))          # screen_app/
BASE = os.path.dirname(WS)                                # 工作區根
DB = os.environ.get('DB_PATH') or os.path.join(BASE, 'football.db')
STATE = os.environ.get('REQUALIFY_STATE') or os.path.join(
    BASE, 'logs', 'requalify_state.json')
REPORT_DIR = os.environ.get('REQUALIFY_REPORT_DIR') or BASE
TODAY = time.strftime('%Y%m%d')

sys.path.insert(0, WS)

PHASES = ['P1', 'P1b', 'P2', 'P3', 'P4', 'P5', 'P6']

# 雲端模式（2026-10-22）：雲端冇 crawl16/crawl_mk 補數記錄，P1/P1b 範圍加埋
# 「近14日已賽＋未來8日未賽（有 closing 亞盤）」窗口補位；P4/P5 回測庫重建
# 太重（本機都要1-3小時），雲端 0.5CPU 行唔郁，跳過留俾本機。
CLOUD = '--cloud' in sys.argv
RECENT_WINDOW_SQL = (
    "SELECT m.id FROM matches m "
    "WHERE m.kickoff >= date('now','-14 day') "
    "AND m.kickoff <= date('now','+8 day') "
    "AND EXISTS(SELECT 1 FROM odds_asian oc WHERE oc.match_id=m.id "
    "AND oc.label='closing' AND oc.company_id=12 "
    "AND oc.handicap IS NOT NULL)")


def state(**kw):
    d = {}
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding='utf-8') as fp:
                d = json.load(fp)
        except Exception:
            pass
    d.update(kw)
    d['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(d, fp, ensure_ascii=False)
    os.replace(tmp, STATE)
    return d


def counts(conn):
    out = {}
    for t in ('featured', 'featured_z', 'v1_featured', 'v1_featured_z',
              'v3_featured', 'fw_grid_featured', 'newjx_log', 'w88_log'):
        try:
            out[t] = conn.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
        except Exception:
            out[t] = -1
    for t, wcol in (('newjx_log', "added_at='2026回查'"),
                    ('w88_log', "added_at='2026回查'")):
        try:
            out[t + '_bf'] = conn.execute(
                'SELECT COUNT(*) FROM %s WHERE %s' % (t, wcol)).fetchone()[0]
        except Exception:
            out[t + '_bf'] = -1
    return out


def before_stats():
    conn = sqlite3.connect(DB, timeout=60)
    c = counts(conn)
    # 各 log 命中率（已結算口徑）
    hit = {}
    for t, dcol in (('v3_featured', 'direction'), ('v1_featured', 'direction'),
                    ('v1_featured_z', 'direction'),
                    ('featured', 'direction'), ('featured_z', 'direction')):
        try:
            w = conn.execute("SELECT COUNT(*) FROM %s WHERE result='W'" % t).fetchone()[0]
            l = conn.execute("SELECT COUNT(*) FROM %s WHERE result='L'" % t).fetchone()[0]
            p = conn.execute("SELECT COUNT(*) FROM %s WHERE result='P'" % t).fetchone()[0]
            hit[t] = {'W': w, 'L': l, 'P': p}
        except Exception:
            pass
    for t in ('newjx_log', 'w88_log'):
        try:
            w = conn.execute("SELECT COUNT(*) FROM %s WHERE result='W'" % t).fetchone()[0]
            l = conn.execute("SELECT COUNT(*) FROM %s WHERE result='L'" % t).fetchone()[0]
            hit[t] = {'W': w, 'L': l}
        except Exception:
            pass
    conn.close()
    return {'counts': c, 'hit': hit}


# ---------------- 落注時盤 helpers（同 app.py 同款；唔跨檔 import app.py）----------------
_PICK_LINE_COLS_TBLS = ('featured', 'featured_z', 'v1_featured',
                        'v1_featured_z', 'v3_featured', 'fw_grid_featured')


def _migrate_pick_line_cols(conn):
    """六張精選表補落注時盤口欄 hc/giver（idempotent）。舊行留 NULL，
    結算遇 NULL 先對 closing——legacy 行口徑唔變。"""
    for t in _PICK_LINE_COLS_TBLS:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % t)}
        if 'hc' not in cols:
            conn.execute('ALTER TABLE %s ADD COLUMN hc REAL' % t)
        if 'giver' not in cols:
            conn.execute('ALTER TABLE %s ADD COLUMN giver TEXT' % t)


def _pick_line(conn, mid):
    """該場 company12 落注時讓球盤：優先 closing，冇就用 change_time 最新。"""
    r = conn.execute(
        "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
        "AND company_id=12 AND handicap IS NOT NULL "
        "ORDER BY (label='closing') DESC, change_time DESC LIMIT 1",
        (mid,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _tgt_pick_line(conn, t, mid):
    """INSERT 位優先用 target 入面嘅 closing 盤；冇先查庫。"""
    oc = ((t or {}).get('odds') or {}).get('closing') or {}
    if oc.get('h') is not None:
        return oc.get('h'), oc.get('g')
    return _pick_line(conn, mid)


def _resettle_pick(conn, table, mid, grid=None):
    """重評後按新方向及落注盤重結算，保留該歷史記錄。"""
    try:
        from .fw8x import pick_result
    except ImportError:
        from fw8x import pick_result
    key = 'grid=? AND match_id=?' if grid is not None else 'match_id=?'
    args = (grid, mid) if grid is not None else (mid,)
    row = conn.execute(
        'SELECT t.direction,t.hc,t.giver,m.home_score,m.away_score,t.result '
        'FROM %s t JOIN matches m ON m.id=t.match_id WHERE %s' % (table, key),
        args).fetchone()
    if not row:
        return False
    direction, hc, giver, hs, aws, old = row
    result = pick_result(hc, giver, hs, aws)
    if result is None:
        return False
    result = ('P' if result == 'P' else
              ('W' if (result == 'A') == (direction == 'up') else 'L'))
    if direction not in ('up', 'down') or result == old:
        return False
    conn.execute('UPDATE %s SET result=?,settled_at=? WHERE %s' % (table, key),
                 (result, time.strftime('%Y-%m-%d %H:%M:%S')) + args)
    return True


# ---------------- P1 featured 家族逐場重評 ----------------
def phase_p1(limit=None):
    import screen_engine
    import v2_engine
    import v3_engine
    conn = sqlite3.connect(DB, timeout=180)
    conn.execute('PRAGMA busy_timeout=180000')
    _migrate_pick_line_cols(conn)
    v2_engine.load_v2_pool(conn)
    v3_engine.load_v3_pool(conn)
    # 日常預設只重評現有入選及補數場次；--full 重評全部有 closing 亞盤嘅完場記錄。
    if '--full' in sys.argv:
        sql = ("SELECT m.id FROM matches m "
               "JOIN odds_asian oc ON oc.match_id=m.id "
               "AND oc.label='closing' AND oc.company_id=12 "
               "AND oc.handicap IS NOT NULL "
               "WHERE m.home_score IS NOT NULL ORDER BY m.kickoff")
        rows = [r[0] for r in conn.execute(sql).fetchall()]
    else:
        ids = set()
        for tbl in ('featured', 'featured_z', 'v1_featured', 'v1_featured_z',
                    'v3_featured'):
            ids.update(r[0] for r in conn.execute(
                'SELECT match_id FROM %s' % tbl))
        try:
            ids.update(r[0] for r in conn.execute(
                "SELECT match_id FROM crawl16_state WHERE asian='ok'"))
        except sqlite3.OperationalError:
            pass
        if CLOUD:
            ids.update(r[0] for r in conn.execute(RECENT_WINDOW_SQL))
        rows = sorted(ids)
    if limit:
        rows = rows[:limit]
    total = len(rows)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    st = {'scanned': 0, 'in': 0, 'out': 0, 'skip': 0, 'err': 0}
    t0 = time.time()
    state(phase='P1', p1_total=total, **st)
    for i, mid in enumerate(rows):
        try:
            t = screen_engine.get_target(conn, mid)
            if not t:
                st['skip'] += 1
                continue
            acted = False
            fl = v2_engine.featured_letters(conn, t) or {}
            d = fl.get('direction')
            passed = fl.get('passed_letters') or []
            zpass = [e for e in (fl.get('letters') or [])
                     if e.get('mix') == '同主隊' and e['letter'] in passed]
            dz = zpass[0]['dir'] if zpass else None
            meta = json.dumps({'letters': passed, 'states': fl.get('states')},
                              ensure_ascii=False)
            phc, pgv = _tgt_pick_line(conn, t, mid)
            # featured / featured_z（同 scan job 同一口徑，但加踢走）
            row = conn.execute('SELECT result FROM featured WHERE match_id=?',
                               (mid,)).fetchone()
            if d:
                conn.execute(
                    'INSERT INTO featured(match_id,direction,letters,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?,?) ON CONFLICT(match_id) '
                    'DO UPDATE SET direction=excluded.direction, '
                    'letters=excluded.letters, hc=excluded.hc, '
                    'giver=excluded.giver',
                    (mid, d, meta, now, phc, pgv))
                acted = True
            elif row and row[0] is None:
                conn.execute('DELETE FROM featured WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            rowz = conn.execute('SELECT result FROM featured_z WHERE match_id=?',
                                (mid,)).fetchone()
            if dz:
                conn.execute(
                    'INSERT INTO featured_z(match_id,direction,letters,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?,?) ON CONFLICT(match_id) '
                    'DO UPDATE SET direction=excluded.direction, '
                    'letters=excluded.letters, hc=excluded.hc, '
                    'giver=excluded.giver',
                    (mid, dz, meta, now, phc, pgv))
                acted = True
            elif rowz and rowz[0] is None:
                conn.execute('DELETE FROM featured_z WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            # v1_featured / v1_featured_z
            row = conn.execute('SELECT result FROM v1_featured WHERE match_id=?',
                               (mid,)).fetchone()
            if d:
                conn.execute(
                    'INSERT INTO v1_featured(match_id,direction,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?) ON CONFLICT(match_id) '
                    'DO UPDATE SET direction=excluded.direction, '
                    'hc=excluded.hc, giver=excluded.giver',
                    (mid, d, now, phc, pgv))
                acted = True
            elif row and row[0] is None:
                conn.execute('DELETE FROM v1_featured WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            rowz = conn.execute('SELECT result FROM v1_featured_z WHERE match_id=?',
                                (mid,)).fetchone()
            if dz:
                conn.execute(
                    'INSERT INTO v1_featured_z(match_id,direction,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?) ON CONFLICT(match_id) '
                    'DO UPDATE SET direction=excluded.direction, '
                    'hc=excluded.hc, giver=excluded.giver',
                    (mid, dz, now, phc, pgv))
                acted = True
            elif rowz and rowz[0] is None:
                conn.execute('DELETE FROM v1_featured_z WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            # 精選W（V3）
            fw = v3_engine.featured_w(conn, t) or {}
            row = conn.execute('SELECT result FROM v3_featured WHERE match_id=?',
                               (mid,)).fetchone()
            if fw.get('pass'):
                det = json.dumps(fw, ensure_ascii=False)[:500]
                conn.execute(
                    'INSERT INTO v3_featured(match_id,direction,detail,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?,?) ON CONFLICT(match_id) '
                    'DO UPDATE SET direction=excluded.direction, '
                    'detail=excluded.detail, hc=excluded.hc, '
                    'giver=excluded.giver',
                    (mid, fw['direction'], det, now, phc, pgv))
                acted = True
            elif row and row[0] is None:
                conn.execute('DELETE FROM v3_featured WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            if acted:
                for table in ('featured', 'featured_z', 'v1_featured',
                              'v1_featured_z', 'v3_featured'):
                    _resettle_pick(conn, table, mid)
                st['in'] += 1
        except Exception:
            st['err'] += 1
            traceback.print_exc(limit=2)
        st['scanned'] = i + 1
        if (i + 1) % 200 == 0:
            conn.commit()
            el = time.time() - t0
            state(phase='P1', p1_total=total,
                  eta_min=round((el / (i + 1) * (total - i - 1)) / 60, 1), **st)
    conn.commit()
    conn.close()
    state(phase='P1', p1_total=total, **st)
    return st


# ---------------- P1b 格 7/8/12/8s 受影響場次重評 ----------------
def phase_p1b(limit=None):
    import screen_engine
    import fw8s_engine
    import fw_grid_engine
    conn = sqlite3.connect(DB, timeout=180)
    conn.execute('PRAGMA busy_timeout=180000')
    _migrate_pick_line_cols(conn)
    if '--full' in sys.argv:
        affected = [r[0] for r in conn.execute(
            'SELECT m.id FROM matches m JOIN odds_asian oc ON oc.match_id=m.id '
            "AND oc.label='closing' AND oc.company_id=12 "
            'AND oc.handicap IS NOT NULL WHERE m.home_score IS NOT NULL '
            'ORDER BY m.kickoff')]
    else:
        affected = set()
        try:
            affected.update(r[0] for r in conn.execute(
                "SELECT match_id FROM crawl16_state WHERE asian='ok'"))
        except sqlite3.OperationalError:
            pass
        for g in ('7', '8', '12', '8s'):
            affected.update(r[0] for r in conn.execute(
                'SELECT match_id FROM fw_grid_featured WHERE grid=?', (g,)))
        if CLOUD:
            affected.update(r[0] for r in conn.execute(RECENT_WINDOW_SQL))
        affected = sorted(affected)
    if limit:
        affected = affected[:limit]
    total = len(affected)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    st = {'scanned': 0, 'in': 0, 'out': 0, 'err': 0}
    t0 = time.time()
    state(phase='P1b', p1b_total=total, **st)
    for i, mid in enumerate(affected):
        try:
            t = screen_engine.get_target(conn, mid)
            if not t:
                continue
            for g in ('7', '8', '12', '8s'):
                rep = (fw8s_engine.fw8s_report(conn, t) if g == '8s'
                       else fw_grid_engine.grid_report(conn, t, g))
                row = conn.execute(
                    'SELECT result FROM fw_grid_featured WHERE grid=? AND match_id=?',
                    (g, mid)).fetchone()
                if rep.get('pass'):
                    meta = json.dumps(rep, ensure_ascii=False)[:500]
                    phc, pgv = _tgt_pick_line(conn, t, mid)
                    conn.execute(
                        'INSERT INTO fw_grid_featured(grid,match_id,direction,'
                        'detail,added_at,hc,giver) VALUES(?,?,?,?,?,?) '
                        'ON CONFLICT(grid,match_id) DO UPDATE SET '
                        'direction=excluded.direction, detail=excluded.detail, '
                        'hc=excluded.hc, giver=excluded.giver',
                        (g, mid, rep['direction'], meta, now, phc, pgv))
                    _resettle_pick(conn, 'fw_grid_featured', mid, g)
                    st['in'] += 1
                elif row and row[0] is None:
                    conn.execute(
                        'DELETE FROM fw_grid_featured WHERE grid=? AND match_id=?',
                        (g, mid))
                    st['out'] += 1
        except Exception:
            st['err'] += 1
            traceback.print_exc(limit=2)
        st['scanned'] = i + 1
        if (i + 1) % 100 == 0:
            conn.commit()
            state(phase='P1b', p1b_total=total, **st)
    conn.commit()
    conn.close()
    state(phase='P1b', p1b_total=total, **st)
    return st


# ---------------- P2/P3 新精選 / 88W 全年重查 ----------------
def _backfill(script, table, tag):
    cmd = [sys.executable, os.path.join(WS, script)]
    if '--full' in sys.argv:
        cmd.append('--full')
    r = subprocess.run(cmd, cwd=WS, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    out = (r.stdout or '')[-1500:]
    print(out, flush=True)
    if r.returncode != 0:
        print((r.stderr or '')[-1500:], flush=True)
    state(**{script + '_rc': r.returncode})
    return r.returncode


def phase_p2():
    state(phase='P2')
    return _backfill('newjx_backfill.py', 'newjx_log', '2026回查')


def phase_p3():
    state(phase='P3')
    return _backfill('w88_backfill.py', 'w88_log', '2026回查')


# ---------------- P4/P5 回測庫重建 ----------------
def phase_p4():
    state(phase='P4', note='fw8s_bt 全量重建中（獨立庫，約1-3小時）')
    r = subprocess.run([sys.executable, os.path.join(WS, 'fw8s_backtest.py'),
                        '--reset'], cwd=WS, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    print((r.stdout or '')[-800:], flush=True)
    state(p4_rc=r.returncode)
    if r.returncode != 0:
        print((r.stderr or '')[-1500:], flush=True)
    return r.returncode


def phase_p5():
    full = '--full' in sys.argv
    state(phase='P5', note='fw8x 全歷史重建中' if full else 'fw8x_bt2026 重建中')
    cmd = [sys.executable, os.path.join(BASE, 'tools', 'fw8x_backtest_2026.py')]
    if full:
        cmd.append('--full')
    r = subprocess.run(cmd, cwd=os.path.join(BASE, 'tools'),
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    print((r.stdout or '')[-800:], flush=True)
    state(p5_rc=r.returncode)
    if r.returncode != 0:
        print((r.stderr or '')[-1500:], flush=True)
    return r.returncode


# ---------------- P6 對比報告 ----------------
def phase_p6(before):
    state(phase='P6')
    conn = sqlite3.connect(DB, timeout=60)
    after = {'counts': counts(conn), 'hit': {}}
    for t in ('v3_featured', 'v1_featured', 'v1_featured_z', 'featured',
              'featured_z', 'newjx_log', 'w88_log'):
        try:
            w = conn.execute("SELECT COUNT(*) FROM %s WHERE result='W'" % t).fetchone()[0]
            l = conn.execute("SELECT COUNT(*) FROM %s WHERE result='L'" % t).fetchone()[0]
            p = conn.execute("SELECT COUNT(*) FROM %s WHERE result='P'" % t).fetchone()[0]
            after['hit'][t] = {'W': w, 'L': l, 'P': p}
        except Exception:
            pass
    table = 'fw8x_bt_all' if '--full' in sys.argv else 'fw8x_bt2026'
    try:
        bt_stat = {'fw8x': dict(zip(('n', 'W', 'L', 'P'), conn.execute(
            'SELECT COUNT(*),SUM(result="W"),SUM(result="L"),SUM(result="P") '
            'FROM %s' % table).fetchone()))}
    finally:
        conn.close()
    bt_path = os.environ.get('FW8S_BT_DB_PATH') or os.path.join(BASE, 'fw8s_bt.db')
    try:
        b = sqlite3.connect(bt_path, timeout=60)
        row = b.execute(
            'SELECT COUNT(*),SUM(direction IS NOT NULL),'
            'SUM(direction IS NOT NULL AND res<>"P" AND '
            '((res="A" AND direction="up") OR (res="B" AND direction="down"))),'
            'SUM(direction IS NOT NULL AND res="P") FROM fw8s_bt').fetchone()
        n, selected, w, p = row
        bt_stat['fw8s_bt'] = {'n': n, 'selected': selected or 0,
                              'W': w or 0, 'L': (selected or 0) - (w or 0) - (p or 0),
                              'P': p or 0}
        b.close()
    except Exception as e:
        if not CLOUD:
            raise
        bt_stat['fw8s_bt'] = str(e)
    lines = ['# 重算對比報告（%s）' % time.strftime('%Y-%m-%d %H:%M'), '',
             '## 各表入選場數（前 → 後）', '',
             '| 表 | 重算前 | 重算後 |', '|---|---|---|']
    for t, v in before['counts'].items():
        lines.append('| %s | %s | %s |' % (t, v, after['counts'].get(t, '?')))
    lines += ['', '## 已結算命中率（W/L/P）', '', '| 表 | 重算前 | 重算後 |',
              '|---|---|---|']
    for t in before['hit']:
        lines.append('| %s | %s | %s |'
                     % (t, before['hit'][t], after['hit'].get(t, '?')))
    lines += ['', '## 回測庫（重建後）', '', '```json',
              json.dumps(bt_stat, ensure_ascii=False), '```']
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, '重算對比報告_%s.md' % TODAY)
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(lines))
    state(phase='P6', report=path)
    print('報告：' + path, flush=True)


def main():
    os.makedirs(os.path.join(BASE, 'logs'), exist_ok=True)
    only = None
    limit = None
    if '--phase' in sys.argv:
        only = sys.argv[sys.argv.index('--phase') + 1]
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    before = before_stats()
    state(status='running', phase='START', error=None,
          mode=('cloud' if CLOUD else 'local'),
          before=json.dumps(before, ensure_ascii=False))
    failures = []
    partial = bool(CLOUD and only in (None, 'P4', 'P5'))
    try:
        if only in (None, 'P1'):
            print('P1 featured 家族逐場重評…', flush=True)
            st = phase_p1(limit)
            print('P1 完成', st, flush=True)
            if st.get('err'):
                failures.append({'phase': 'P1', 'errors': st['err']})
        if only in (None, 'P1b'):
            print('P1b 格 7/8/12/8s 重評…', flush=True)
            st = phase_p1b(limit)
            print('P1b 完成', st, flush=True)
            if st.get('err'):
                failures.append({'phase': 'P1b', 'errors': st['err']})
        if only in (None, 'P2'):
            rc = phase_p2()
            if rc:
                failures.append({'phase': 'P2', 'rc': rc})
        if only in (None, 'P3'):
            rc = phase_p3()
            if rc:
                failures.append({'phase': 'P3', 'rc': rc})
        if only in (None, 'P4'):
            if CLOUD:
                print('雲端模式：跳過 P4（fw8s_bt 全量重建要1-3小時，留俾本機）',
                      flush=True)
            else:
                rc = phase_p4()
                if rc:
                    failures.append({'phase': 'P4', 'rc': rc})
        if only in (None, 'P5'):
            if CLOUD:
                print('雲端模式：跳過 P5（8X 回測庫重建留俾本機）', flush=True)
            else:
                rc = phase_p5()
                if rc:
                    failures.append({'phase': 'P5', 'rc': rc})
        if only in (None, 'P6'):
            phase_p6(before)
    except Exception:
        traceback.print_exc()
        state(status='error', error=traceback.format_exc(limit=3))
        raise SystemExit(1)
    if failures:
        state(status='error', error='有階段未成功', failures=failures)
        raise SystemExit(1)
    state(status='partial' if partial else 'done', phase='DONE',
          skipped=(['P4', 'P5'] if partial else []))


if __name__ == '__main__':
    main()
