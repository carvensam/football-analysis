# -*- coding: utf-8 -*-
"""requalify_after_backfill.py — 補數後全精選系統資格重算（2026-10-21）

順序執行：
  P1  featured 家族逐場重評：featured / featured_z / v1_featured / v1_featured_z /
      v3_featured（精選W、精選7、精選12 等）——合資格加入、唔合資格踢走（已完場都重評）
  P1b fw_grid_featured 格 7/8/12/8s：只重評受影響場次（補數觸及 + 原有入選）
  P2  新精選：清 '2026回查' 列 → newjx_backfill 全年重查
  P3  88W：清 '2026回查' 列 → w88_backfill 全年重查
  P4  精選8s 回測庫：fw8s_backtest.py --reset 全量重建（另一個 db，唔阻主庫）
  P5  8X 回測庫：tools/fw8x_backtest_2026.py 全量重建
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
STATE = os.path.join(BASE, 'logs', 'requalify_state.json')
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
            d = json.load(open(STATE, encoding='utf-8'))
        except Exception:
            pass
    d.update(kw)
    d['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    json.dump(d, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
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


# ---------------- P1 featured 家族逐場重評 ----------------
def phase_p1(limit=None):
    import screen_engine
    import v2_engine
    import v3_engine
    conn = sqlite3.connect(DB, timeout=180)
    conn.execute('PRAGMA busy_timeout=180000')
    v2_engine.load_v2_pool(conn)
    v3_engine.load_v3_pool(conn)
    # 重評範圍（準確性×可行性平衡，2026-10-21 實測全庫逐場要 25 小時）：
    #   (a) 五個 featured 表現有入選列嘅場次（要檢查踢走）
    #   (b) 補數新攞到 asian 嘅場次（crawl16_state asian='ok'，要檢查加入）
    #   --full 先會全庫 45,271 場逐場（人口池變動理論上影響全部，但 1.4%
    #   新增人口實務上極少拗方向；要最嚴謹就用 --full）
    if '--full' in sys.argv:
        sql = ("SELECT m.id FROM matches m "
               "JOIN odds_asian oc ON oc.match_id=m.id "
               "AND oc.label='closing' AND oc.company_id=12 "
               "AND oc.handicap IS NOT NULL "
               "WHERE m.home_score IS NOT NULL AND m.kickoff>='2024-01-01' "
               "ORDER BY m.kickoff")
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
            # featured / featured_z（同 scan job 同一口徑，但加踢走）
            row = conn.execute('SELECT result FROM featured WHERE match_id=?',
                               (mid,)).fetchone()
            if d:
                conn.execute(
                    'INSERT INTO featured(match_id,direction,letters,added_at) '
                    'VALUES(?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, letters=excluded.letters',
                    (mid, d, meta, now))
                acted = True
            elif row:
                conn.execute('DELETE FROM featured WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            rowz = conn.execute('SELECT result FROM featured_z WHERE match_id=?',
                                (mid,)).fetchone()
            if dz:
                conn.execute(
                    'INSERT INTO featured_z(match_id,direction,letters,added_at) '
                    'VALUES(?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, letters=excluded.letters',
                    (mid, dz, meta, now))
                acted = True
            elif rowz:
                conn.execute('DELETE FROM featured_z WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            # v1_featured / v1_featured_z
            row = conn.execute('SELECT result FROM v1_featured WHERE match_id=?',
                               (mid,)).fetchone()
            if d:
                conn.execute(
                    'INSERT INTO v1_featured(match_id,direction,added_at) '
                    'VALUES(?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction', (mid, d, now))
                acted = True
            elif row:
                conn.execute('DELETE FROM v1_featured WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            rowz = conn.execute('SELECT result FROM v1_featured_z WHERE match_id=?',
                                (mid,)).fetchone()
            if dz:
                conn.execute(
                    'INSERT INTO v1_featured_z(match_id,direction,added_at) '
                    'VALUES(?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction', (mid, dz, now))
                acted = True
            elif rowz:
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
                    'INSERT INTO v3_featured(match_id,direction,detail,added_at) '
                    'VALUES(?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, detail=excluded.detail',
                    (mid, fw['direction'], det, now))
                acted = True
            elif row:
                conn.execute('DELETE FROM v3_featured WHERE match_id=?', (mid,))
                st['out'] += 1
                acted = True
            if acted:
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
    # 受影響 = 補數觸及（crawl16 asian 成功）+ 原有入選格（+雲端：近期窗口）
    try:
        affected = set(r[0] for r in conn.execute(
            "SELECT match_id FROM crawl16_state WHERE asian='ok'"))
    except sqlite3.OperationalError:
        affected = set()
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
                    conn.execute(
                        'INSERT INTO fw_grid_featured(grid,match_id,direction,'
                        'detail,added_at) VALUES(?,?,?,?,?) '
                        'ON CONFLICT(grid,match_id) DO UPDATE SET '
                        'direction=excluded.direction, detail=excluded.detail',
                        (g, mid, rep['direction'], meta, now))
                    st['in'] += 1
                elif row:
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
    conn = sqlite3.connect(DB, timeout=180)
    n = conn.execute("DELETE FROM %s WHERE added_at='%s'" % (table, tag)).rowcount
    conn.commit()
    conn.close()
    print('[%s] 清走舊回查 %d 列' % (table, n), flush=True)
    r = subprocess.run([sys.executable, os.path.join(WS, script)],
                       cwd=WS, capture_output=True, text=True)
    out = (r.stdout or '')[-1500:]
    print(out, flush=True)
    if r.returncode != 0:
        # stderr 可以係 None（子進程異常死亡），舊版直接 [-1500:] 會
        # TypeError 冧成全個重算中途停低（2026-10-21 實跑中招）
        print((r.stderr or '')[-1500:], flush=True)
    state(**{script + '_rc': r.returncode})


def phase_p2():
    state(phase='P2')
    _backfill('newjx_backfill.py', 'newjx_log', '2026回查')


def phase_p3():
    state(phase='P3')
    _backfill('w88_backfill.py', 'w88_log', '2026回查')


# ---------------- P4/P5 回測庫重建 ----------------
def phase_p4():
    state(phase='P4', note='fw8s_bt 全量重建中（獨立庫，約1-3小時）')
    r = subprocess.run([sys.executable, os.path.join(WS, 'fw8s_backtest.py'),
                        '--reset'], cwd=WS, capture_output=True, text=True)
    print((r.stdout or '')[-800:], flush=True)
    state(p4_rc=r.returncode)
    if r.returncode != 0:
        print((r.stderr or '')[-1500:], flush=True)


def phase_p5():
    state(phase='P5', note='fw8x_bt2026 全量重建中')
    r = subprocess.run([sys.executable,
                        os.path.join(BASE, 'tools', 'fw8x_backtest_2026.py')],
                       cwd=os.path.join(BASE, 'tools'),
                       capture_output=True, text=True)
    print((r.stdout or '')[-800:], flush=True)
    state(p5_rc=r.returncode)
    if r.returncode != 0:
        print((r.stderr or '')[-1500:], flush=True)


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
    conn.close()
    # fw8s_bt / fw8x_bt2026 統計（獨立庫/主庫）
    bt_stat = {}
    try:
        b = sqlite3.connect(os.path.join(BASE, 'fw8s_bt.db'), timeout=60)
        n = b.execute('SELECT COUNT(*) FROM fw8s_bt').fetchone()[0]
        w = b.execute("SELECT COUNT(*) FROM fw8s_bt WHERE res='W'").fetchone()[0]
        l = b.execute("SELECT COUNT(*) FROM fw8s_bt WHERE res='L'").fetchone()[0]
        bt_stat['fw8s_bt'] = {'n': n, 'W': w, 'L': l}
        b.close()
    except Exception as e:
        bt_stat['fw8s_bt'] = str(e)
    try:
        n = conn.execute('SELECT COUNT(*) FROM fw8x_bt2026').fetchone()[0] if False else None
    except Exception:
        pass
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
    path = os.path.join(BASE, '重算對比報告_%s.md' % TODAY)
    open(path, 'w', encoding='utf-8').write('\n'.join(lines))
    state(phase='DONE', report=path)
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
    todo = [p for p in PHASES if only is None or p == only or (only == 'P1' and p == 'P1b' and False)]
    try:
        if only in (None, 'P1'):
            print('P1 featured 家族逐場重評…', flush=True)
            st = phase_p1(limit)
            print('P1 完成', st, flush=True)
        if only in (None, 'P1b'):
            print('P1b 格 7/8/12/8s 重評…', flush=True)
            st = phase_p1b(limit)
            print('P1b 完成', st, flush=True)
        if only in (None, 'P2'):
            phase_p2()
        if only in (None, 'P3'):
            phase_p3()
        if only in (None, 'P4'):
            if CLOUD:
                print('雲端模式：跳過 P4（fw8s_bt 全量重建要1-3小時，留俾本機）',
                      flush=True)
            else:
                phase_p4()
        if only in (None, 'P5'):
            if CLOUD:
                print('雲端模式：跳過 P5（8X 回測庫重建留俾本機）', flush=True)
            else:
                phase_p5()
        if only in (None, 'P6'):
            phase_p6(before)
    except Exception:
        traceback.print_exc()
        state(status='error', error=traceback.format_exc(limit=3))
        return
    if only is None:
        state(status='done', phase='DONE')


if __name__ == '__main__':
    main()
