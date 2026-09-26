# -*- coding: utf-8 -*-
"""篩查 APP 本機伺服器（標準函式庫 + 工作區 crawler / screen_engine）
端點：
  GET  /                      主頁（賽事清單 + 篩查結果）
  GET  /api/upcoming?hours=48 未來賽事清單
  POST /api/fetch  {id}       抓取該場最新賠率（titan007 易胜博，限流）
  GET  /api/screen?id=        執行 14 項篩查
"""
import json
import os
import socket
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.environ.get('DB_PATH') or os.path.join(PARENT_DIR, 'football.db')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
import sys
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import numpy as np
import screen_engine
from concurrent.futures import ThreadPoolExecutor

# 精選／我的選擇等列表逐場運算（screen＋check）係 CPU 密集——
# 單線程每場 5–8 秒，幾十場就超時（用戶投訴「精選無反應」嘅根因）。
# 用執行緒池並行起唄（sqlite 每連線獨立、load_pool 有鎖，並行安全）。
_BUILD_POOL = ThreadPoolExecutor(max_workers=min(6, os.cpu_count() or 4),
                                 thread_name_prefix='build')

_fetch_lock = threading.Lock()
_last_fetch = {}          # match_id -> ts
_local = threading.local()
SERVER_VERSION = '4.1.0'
_started = time.time()
_pool_ready = {'done': False, 'err': None}


def get_crawler():
    """每個執行緒建立自己的 Fetcher（sqlite 連線不可跨執行緒）"""
    if not hasattr(_local, 'crawler'):
        import crawler
        # config.json 跟 crawler.py 同一目錄（本機=工作區根；雲端=/app）
        with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        # APP 即場抓取：禁用爬蟲的長時間休息（永不觸發 700 次/15分鐘 限流），
        # 保留每次請求間隔以免被封
        cfg['max_requests_before_rest'] = 999999999
        cfg['rest_minutes'] = 0
        cfg['blocked_retry_times'] = 1
        conn = sqlite3.connect(DB_PATH)
        _local.fetcher = crawler.Fetcher(conn, cfg)
        _local.crawler = crawler
    return _local.crawler, _local.fetcher


def db():
    return sqlite3.connect(DB_PATH)


def upcoming(hours=48):
    conn = db()
    # hours=0 → 不設時限上限，列出全部即將開賽賽事
    cap = "AND m.kickoff <= datetime('now','localtime', ?) " if hours else ""
    args = (f'+{hours} hours',) if hours else ()
    rows = conn.execute(
        "SELECT m.id, c.req_name, m.kickoff, ht.name_tc, at.name_tc, m.odds_done, "
        "EXISTS(SELECT 1 FROM odds_asian oa WHERE oa.match_id=m.id AND oa.company_id=12), "
        "oc.handicap, oc.giver, oc.home_odds, oc.away_odds, "
        "COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank) "
        "FROM matches m JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN teams ht ON ht.titan_id=m.home_id "
        "JOIN teams at ON at.titan_id=m.away_id "
        "LEFT JOIN odds_asian oc ON oc.match_id=m.id AND oc.company_id=12 "
        "AND oc.label='closing' "
        "LEFT JOIN match_prestandings ps ON ps.match_id=m.id "
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','localtime') "
        + cap + "ORDER BY m.kickoff", args).fetchall()
    out = []
    for mid, lg, ko, h, a, od, has, hc, gv, ho, ao, hr_, ar_ in rows:
        line = None
        if hc is not None:
            line = {'line': screen_engine.fmt_line(hc, gv), 'ho': ho, 'ao': ao}
        out.append({'id': mid, 'league': lg, 'kickoff': ko, 'home': h, 'away': a,
                    'has_odds': bool(has), 'line': line,
                    'rank_home': hr_, 'rank_away': ar_,
                    'fetched_ago': int(time.time() - _last_fetch[mid]) if mid in _last_fetch else None})
    conn.close()
    return out


def played():
    """最近 120 小時內已開賽嘅場次（上限 1200 場，最新排先），主頁「已開賽」區用"""
    conn = db()
    rows = conn.execute(
        "SELECT m.id, c.req_name, m.kickoff, ht.name_tc, at.name_tc, "
        "m.home_score, m.away_score, "
        "oc.handicap, oc.giver, oc.home_odds, oc.away_odds "
        "FROM matches m JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN teams ht ON ht.titan_id=m.home_id "
        "JOIN teams at ON at.titan_id=m.away_id "
        "LEFT JOIN odds_asian oc ON oc.match_id=m.id AND oc.company_id=12 "
        "AND oc.label='closing' "
        "WHERE m.kickoff < datetime('now','localtime') "
        "AND m.kickoff >= datetime('now','localtime','-120 hours') "
        "ORDER BY m.kickoff DESC LIMIT 1200", ()).fetchall()
    out = []
    for mid, lg, ko, h, a, hs, aws, hc, gv, ho, ao in rows:
        line = None
        if hc is not None:
            line = {'line': screen_engine.fmt_line(hc, gv), 'ho': ho, 'ao': ao}
        out.append({'id': mid, 'league': lg, 'kickoff': ko, 'home': h, 'away': a,
                    'score': None if hs is None else f'{hs}-{aws}', 'line': line})
    conn.close()
    return out


def do_fetch(mid, force=False):
    """即場抓取單場賽事嘅賠率（盤口＋水位）"""
    with _fetch_lock:
        if not force and time.time() - _last_fetch.get(mid, 0) < 120:
            return {'ok': True, 'cached': True}
        crawler, fetcher = get_crawler()
        conn = db()
        row = conn.execute('SELECT kickoff FROM matches WHERE id=?', (mid,)).fetchone()
        if not row:
            conn.close()
            return {'ok': False, 'error': '找不到賽事'}
        ok = crawler.crawl_odds_for_match(conn, fetcher, mid, row[0], 12)
        conn.close()
        _last_fetch[mid] = time.time()
        return {'ok': bool(ok)}


# ============ 一鍵更新賽事（賽果＋新場次＋已存在場次嘅盤口及賠率全部刷新） ============
# 手動掣 / 每次開 APP 自動觸發（30 分鐘內只會自動跑一次）
_update_state = {'running': False, 'phase': '', 'last_done': 0.0,
                 'last_result': None, 'error': None}
UPDATE_THROTTLE_SEC = 1800


def _update_worker():
    import crawler
    with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['max_requests_before_rest'] = 999999999
    cfg['rest_minutes'] = 0
    conn = sqlite3.connect(DB_PATH, timeout=180)
    ok = False
    try:
        # ① 賽果＋新場次＋最近三日補盤口（各聯賽現行賽季檔）
        st = crawler.recent_update(
            conn, cfg, days=3,
            progress=lambda m: _update_state.update(phase=m))
        _update_state['last_result'] = st
        # ② 已存在場次嘅盤口＋賠率全部強制刷新（未開賽且已有盤口嘅，最近開賽排先）
        todo = conn.execute(
            'SELECT m.id, m.kickoff FROM matches m '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff >= datetime('now','localtime') "
            'AND EXISTS(SELECT 1 FROM odds_asian o '
            'WHERE o.match_id=m.id AND o.company_id=12) '
            'ORDER BY m.kickoff').fetchall()
        fetcher = crawler.Fetcher(conn, cfg)
        n_ok = n_fail = 0
        for i, (mid, ko) in enumerate(todo):
            _update_state['phase'] = (
                f'刷新已存在場次嘅盤口及賠率 {i + 1}/{len(todo)}')
            try:
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    n_ok += 1
                else:
                    n_fail += 1
            except Exception:
                n_fail += 1
        st['odds_refresh'] = f'{n_ok} 場成功 / {n_fail} 場失敗'
        # ②b 未開賽但完全冇盤口嘅場次（未來72小時內）補爬——
        # 舊快照/新插入場次可能從未抓過賠率，冇呢步佢哋永遠冇盤口顯示
        todo2 = conn.execute(
            'SELECT m.id, m.kickoff FROM matches m '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff >= datetime('now','localtime') "
            "AND m.kickoff <= datetime('now','localtime','+72 hours') "
            'AND NOT EXISTS(SELECT 1 FROM odds_asian o '
            'WHERE o.match_id=m.id AND o.company_id=12) '
            'ORDER BY m.kickoff').fetchall()
        n_fill_ok = n_fill_fail = 0
        for i, (mid, ko) in enumerate(todo2):
            _update_state['phase'] = (
                f'補爬缺少盤口嘅場次 {i + 1}/{len(todo2)}')
            try:
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    n_fill_ok += 1
                else:
                    n_fill_fail += 1
            except Exception:
                n_fill_fail += 1
        st['odds_fill'] = f'{n_fill_ok} 場成功 / {n_fill_fail} 場失敗'
        _update_state['error'] = None
        print(f"[update] 完成：{st}", flush=True)
        ok = True
    except Exception:
        import traceback
        traceback.print_exc()
        _update_state['error'] = traceback.format_exc(limit=3)
    finally:
        conn.close()
        _update_state['running'] = False
        _update_state['phase'] = ''
        _update_state['last_done'] = time.time()
    if ok:
        _recompute_featured_after_update()


def do_update(auto=False):
    if _update_state['running']:
        return {'ok': False, 'running': True, 'error': '更新進行中'}
    if (auto and _update_state['last_done']
            and time.time() - _update_state['last_done'] < UPDATE_THROTTLE_SEC):
        return {'ok': True, 'skipped': True}
    _update_state.update(running=True, phase='準備中…', error=None,
                         last_result=None)
    threading.Thread(target=_update_worker, daemon=True).start()
    return {'ok': True, 'started': True}


def update_status():
    d = dict(_update_state)
    d['ok'] = True
    if d['last_done']:
        d['last_done_ago'] = int(time.time() - d['last_done'])
    return d


# ============ V2 快速窗口更新：只更新未來 24小時／30分鐘／10分鐘 場次嘅盤口賠率 ============
# 賽事清單直接由 matches 表按開賽時間攞（每日中午 12 時自動更新時已入庫未來 48 小時場次），
# 唔使重新掃瞄邊啲場，直接逐場抓最新盤口。
_win_state = {'running': False, 'window': '', 'phase': '', 'done': 0, 'total': 0,
              'ok': 0, 'fail': 0, 'last': None, 'error': None}
WIN_SQL = {
    '24h': ("AND m.kickoff <= datetime('now','localtime','+24 hours')", '未來24小時'),
    '30m': ("AND m.kickoff <= datetime('now','localtime','+30 minutes')", '未來30分鐘'),
    '10m': ("AND m.kickoff <= datetime('now','localtime','+10 minutes')", '未來10分鐘'),
}


def _window_update_worker(win):
    import crawler
    with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['max_requests_before_rest'] = 999999999
    cfg['rest_minutes'] = 0
    cond, wname = WIN_SQL[win]
    conn = sqlite3.connect(DB_PATH, timeout=180)
    try:
        todo = conn.execute(
            'SELECT m.id, m.kickoff FROM matches m '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff >= datetime('now','localtime') " + cond + ' '
            'ORDER BY m.kickoff').fetchall()
        _win_state.update(total=len(todo), done=0, ok=0, fail=0)
        fetcher = crawler.Fetcher(conn, cfg)
        for i, (mid, ko) in enumerate(todo):
            _win_state['phase'] = f'更新{wname}盤口及賠率 {i + 1}/{len(todo)}'
            try:
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    _win_state['ok'] += 1
                else:
                    _win_state['fail'] += 1
            except Exception:
                _win_state['fail'] += 1
        _win_state['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
        _win_state['error'] = None
        print(f"[win-update] {wname} 完成：{_win_state['ok']} 成 / "
              f"{_win_state['fail']} 敗 / 共 {len(todo)} 場", flush=True)
    except Exception:
        traceback.print_exc()
        _win_state['error'] = traceback.format_exc(limit=3)
    finally:
        conn.close()
        _win_state['running'] = False
        _win_state['phase'] = ''
    # 注意：唔喺度重掃精選——快速掣只係更新指定窗口嘅盤口，掃精選係另一個掣嘅工作；
    # 否則撳「更新未來10分鐘」都會觸發全庫精選重掃（用戶投訴「都係全部掃」嘅根因）。


def do_update_window(win):
    """人手撳『更新未來24／30／10』：後台起線程逐場抓，立即回應 started"""
    if win not in WIN_SQL:
        return {'ok': False, 'error': '未知窗口'}
    if _win_state['running']:
        return {'ok': False, 'running': True, 'error': '窗口更新進行中'}
    _win_state.update(running=True, window=win, error=None,
                      phase='準備中…', done=0, total=0)
    threading.Thread(target=_window_update_worker, daemon=True,
                     args=(win,)).start()
    return {'ok': True, 'started': True}


def win_status():
    d = dict(_win_state)
    d['ok'] = True
    d['window_name'] = WIN_SQL.get(d['window'], ('', ''))[1]
    return d


# ============ V2 背景排程：每日 12 時起每 2 小時自動更新未來 24 小時盤口直至尾盤 ============
# （12/14/16/18/20/22/24 時；賽果補抓沿用 15 分鐘巡邏。手機 APP 閂咗都會行——
#   排程喺 Render 伺服器端，唔係手機端。）
_sched_state = {'last_mark': '', 'runs': 0, 'last_error': None}


def _v2_sched_job():
    import datetime as dt
    while True:
        time.sleep(300)   # 每 5 分鐘睇一次
        try:
            now = dt.datetime.now()
            if now.hour < 12:
                continue
            # 今日 12 時起每 2 小時一個刻度：12,14,16,18,20,22,(24=翌日0)
            base = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now < base:
                continue
            elapsed = (now - base).total_seconds()
            mark_n = int(elapsed // 7200)          # 過咗幾多個 2 小時刻度
            mark = (base + dt.timedelta(hours=2 * mark_n)).strftime('%Y-%m-%d %H:%M')
            if _sched_state['last_mark'] >= mark:
                continue
            if _win_state['running'] or _update_state['running']:
                continue   # 有更新緊就等下個刻度
            _sched_state['last_mark'] = mark
            _sched_state['runs'] += 1
            print(f'[sched] 到咗 {mark} 刻度，自動更新未來24小時盤口', flush=True)
            do_update_window('24h')
        except Exception:
            _sched_state['last_error'] = traceback.format_exc(limit=3)


def do_screen(mid, sel=None):
    conn = db()
    t = screen_engine.get_target(conn, mid)
    if not t:
        conn.close()
        return {'error': '找不到賽事'}
    res = screen_engine.screen(conn, t, sel=sel)
    conn.close()
    return res


def log_featured(conn, mid, direction):
    """過往紀錄（2026-09-20）：任何渠道入選精選 → 自動影低當刻尾盤快照。
    計一場：同一 match_id 保留首次 added_at，但尾盤快照同方向更新為最新。"""
    row = conn.execute(
        "SELECT handicap, giver, home_odds, away_odds FROM odds_asian "
        "WHERE match_id=? AND company_id=12 "
        "ORDER BY (label='closing') DESC, label DESC LIMIT 1", (mid,)).fetchone()
    hc, gv, ho, ao = row if row else (None, None, None, None)
    conn.execute(
        'INSERT INTO featured_log(match_id, direction, added_at, handicap, '
        'giver, home_odds, away_odds) VALUES(?,?,?,?,?,?,?) '
        'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
        'handicap=excluded.handicap, giver=excluded.giver, '
        'home_odds=excluded.home_odds, away_odds=excluded.away_odds',
        (mid, direction, time.strftime('%Y-%m-%d %H:%M:%S'), hc, gv, ho, ao))


def log_featured_z(conn, mid, direction):
    """精選Z 版嘅過往紀錄（同 log_featured，但寫入 featured_z_log，完全分開）。"""
    row = conn.execute(
        "SELECT handicap, giver, home_odds, away_odds FROM odds_asian "
        "WHERE match_id=? AND company_id=12 "
        "ORDER BY (label='closing') DESC, label DESC LIMIT 1", (mid,)).fetchone()
    hc, gv, ho, ao = row if row else (None, None, None, None)
    conn.execute(
        'INSERT INTO featured_z_log(match_id, direction, added_at, handicap, '
        'giver, home_odds, away_odds) VALUES(?,?,?,?,?,?,?) '
        'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
        'handicap=excluded.handicap, giver=excluded.giver, '
        'home_odds=excluded.home_odds, away_odds=excluded.away_odds',
        (mid, direction, time.strftime('%Y-%m-%d %H:%M:%S'), hc, gv, ho, ao))


def do_featured_refresh(mid):
    """精選單場重新整理：重抓該場最新盤口賠率，重算該場精選資格。
    仍合準則 → 保留／更新方向；唔再合 → 未結算移出；已完場歷史保留。
    （精選 同 精選Z 一併重算）"""
    st = do_fetch(mid, True)
    if not st.get('ok'):
        return {'ok': False, 'error': st.get('error', '抓取賠率失敗'), 'fetch': st}
    conn = db()
    try:
        screen_engine._pool_cache['ts'] = 0   # 用新盤口重新載入數據池
        d = dz = None
        try:
            b = _screen_brief(mid)
            d = _featured_direction(b)
            dz = _featured_direction(b, z=True)
        except Exception:
            pass
        row = conn.execute('SELECT result FROM featured WHERE match_id=?',
                           (mid,)).fetchone()
        rowz = conn.execute('SELECT result FROM featured_z WHERE match_id=?',
                            (mid,)).fetchone()
        removed = removed_z = False
        if d:
            conn.execute(
                'INSERT INTO featured(match_id, direction, added_at) VALUES(?,?,?) '
                'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction',
                (mid, d, time.strftime('%Y-%m-%d %H:%M:%S')))
            log_featured(conn, mid, d)
        elif row and row[0] is None:
            conn.execute('DELETE FROM featured WHERE match_id=?', (mid,))
            removed = True
        if dz:
            conn.execute(
                'INSERT INTO featured_z(match_id, direction, added_at) VALUES(?,?,?) '
                'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction',
                (mid, dz, time.strftime('%Y-%m-%d %H:%M:%S')))
            log_featured_z(conn, mid, dz)
        elif rowz and rowz[0] is None:
            conn.execute('DELETE FROM featured_z WHERE match_id=?', (mid,))
            removed_z = True
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'direction': d, 'direction_z': dz,
            'removed': removed, 'removed_z': removed_z}


# ============ 我的選擇（上/下盤 記錄 + 勝出率統計） ============

def init_picks():
    conn = db()
    conn.execute('''CREATE TABLE IF NOT EXISTS user_picks(
        match_id INTEGER PRIMARY KEY, choice TEXT NOT NULL, created REAL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS featured(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, result TEXT, settled_at TEXT)''')
    # V2（2026-09-26）：letters＝入選時嘅合格字頭＋31深淺狀態（JSON，事後回查用）
    for tbl in ('featured', 'featured_z'):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info({tbl})')]
        if 'letters' not in cols:
            conn.execute(f'ALTER TABLE {tbl} ADD COLUMN letters TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS check_rows(
        match_id INTEGER PRIMARY KEY, direction TEXT,
        g14 TEXT, g17 TEXT, res TEXT)''')
    # 過往紀錄（2026-09-20）：精選一出現即自動紀錄尾盤快照，永久保留，計一場
    conn.execute('''CREATE TABLE IF NOT EXISTS featured_log(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, handicap REAL, giver TEXT,
        home_odds REAL, away_odds REAL)''')
    # 精選Z（2026-09-21）：同主客版精選，紀錄同命中率完全分開
    conn.execute('''CREATE TABLE IF NOT EXISTS featured_z(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, result TEXT, settled_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS featured_z_log(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, handicap REAL, giver TEXT,
        home_odds REAL, away_odds REAL)''')
    conn.execute(
        "INSERT OR IGNORE INTO featured_log("
        "match_id, direction, added_at, handicap, giver, home_odds, away_odds) "
        "SELECT f.match_id, f.direction, f.added_at, oc.handicap, oc.giver, "
        "oc.home_odds, oc.away_odds FROM featured f "
        "LEFT JOIN odds_asian oc ON oc.match_id=f.match_id "
        "AND oc.label='closing' AND oc.company_id=12")
    conn.execute(
        "INSERT OR IGNORE INTO featured_z_log("
        "match_id, direction, added_at, handicap, giver, home_odds, away_odds) "
        "SELECT f.match_id, f.direction, f.added_at, oc.handicap, oc.giver, "
        "oc.home_odds, oc.away_odds FROM featured_z f "
        "LEFT JOIN odds_asian oc ON oc.match_id=f.match_id "
        "AND oc.label='closing' AND oc.company_id=12")
    conn.commit()
    # 選擇當刻嘅盤口/水位快照（2026-09-19：我的選擇要同時展示揀時＋尾盤數據）
    cols = [r[1] for r in conn.execute('PRAGMA table_info(user_picks)')]
    for c, t in (('pick_handicap', 'REAL'), ('pick_giver', 'TEXT'),
                 ('pick_home_odds', 'REAL'), ('pick_away_odds', 'REAL')):
        if c not in cols:
            conn.execute(f'ALTER TABLE user_picks ADD COLUMN {c} {t}')
    conn.commit()
    conn.close()


def pick_result(handicap, giver, hs, aws):
    """與 screen_engine 相同嘅 上/下/走 計法：A=上盤贏 B=下盤贏 P=走"""
    if handicap is None or hs is None or aws is None:
        return None
    if giver == 'home':
        margin = hs - aws - handicap
    elif giver == 'away':
        margin = aws - hs - handicap   # 2026-09-19 修正：客讓分支原本正負號顛倒
    else:  # 平手盤
        margin = hs - aws
    return 'A' if margin > 0 else ('B' if margin < 0 else 'P')


def do_pick(mid, choice):
    if choice not in ('up', 'down'):
        return {'ok': False, 'error': '選擇無效'}
    conn = db()
    # 揀嘅當刻影低當時嘅盤口/水位（冇 closing 就用最新一筆）
    row = conn.execute(
        'SELECT handicap, giver, home_odds, away_odds FROM odds_asian '
        'WHERE match_id=? AND company_id=12 '
        'ORDER BY (label=\'closing\') DESC, label DESC LIMIT 1',
        (mid,)).fetchone()
    conn.execute(
        'INSERT INTO user_picks(match_id, choice, created, pick_handicap, '
        'pick_giver, pick_home_odds, pick_away_odds) VALUES(?,?,?,?,?,?,?) '
        'ON CONFLICT(match_id) DO UPDATE SET choice=excluded.choice, '
        'created=excluded.created, pick_handicap=excluded.pick_handicap, '
        'pick_giver=excluded.pick_giver, '
        'pick_home_odds=excluded.pick_home_odds, '
        'pick_away_odds=excluded.pick_away_odds',
        (mid, choice, time.time()) + tuple(row) if row else
        (mid, choice, time.time(), None, None, None, None))
    conn.commit()
    conn.close()
    return {'ok': True}


def do_pick_delete(mid):
    conn = db()
    conn.execute('DELETE FROM user_picks WHERE match_id=?', (mid,))
    conn.commit()
    conn.close()
    return {'ok': True}


def get_picks():
    conn = db()
    rows = conn.execute(
        'SELECT p.match_id, p.choice, m.kickoff, m.home_score, m.away_score, '
        'ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank) '
        'FROM user_picks p '
        'JOIN matches m ON m.id=p.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'LEFT JOIN match_prestandings ps ON ps.match_id=m.id '
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        'ORDER BY m.kickoff DESC').fetchall()
    out = []
    wins = losses = pushes = pending = 0
    for (mid, choice, ko, hs, aws, h, a, lg, hc, gv, ho, ao, hr_, ar_) in rows:
        rec = {'id': mid, 'choice': choice, 'kickoff': ko, 'home': h, 'away': a,
               'league': lg, 'rank_home': hr_, 'rank_away': ar_,
               'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
               'odds': f'主{ho}/客{ao}' if ho is not None else None}
        if hs is None:
            rec['result'] = None          # 未開賽
            pending += 1
        else:
            r = pick_result(hc, gv, hs, aws)
            rec['score'] = f'{hs}-{aws}'
            if r is None:
                rec['result'] = 'X'       # 無尾盤數據，無法判定
                pending += 1
            else:
                won = (r == 'A' and choice == 'up') or (r == 'B' and choice == 'down')
                rec['result'] = 'W' if won else ('L' if r != 'P' else 'P')
                if r == 'P':
                    pushes += 1
                elif won:
                    wins += 1
                else:
                    losses += 1
        out.append(rec)
    stats = {'total': len(out), 'pending': pending, 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'win_rate': wins / (wins + losses) if (wins + losses) else None}
    conn.close()
    return {'stats': stats, 'picks': out}


# ============ 我的選擇 · 大版面（未開賽全部＋已開賽保留24小時） ============
_pk_full_cache = {}


def _oc_brief(oc):
    if not oc:
        return None
    return {'n': oc['n'], 'up_r': oc['up_r'], 'down_r': oc['down_r'],
            'push_r': oc['push_r']}


def _pair_brief(it):
    """第 1/5/8 項：全庫＋同聯賽 上/下/走 率"""
    if not it or it.get('error'):
        return None
    return {'all': _oc_brief(it.get('all')), 'lg': _oc_brief(it.get('league'))}


def _dist_brief(it, cur_line=None):
    """第 12 項：樣本數＋分佈最多盤口＋今場同盤口列（全庫及同聯賽）"""
    if not it or it.get('error') or not it.get('all'):
        return None

    def scope_brief(s):
        if not s:
            return None
        dist = s.get('dist') or []
        top, cur = None, None
        for r in dist:
            if top is None or r.get('n', 0) > top.get('n', 0):
                top = r
            if cur_line and r.get('line') == cur_line:
                cur = r
        return {'n': s.get('n'), 'top': top, 'cur': cur}

    return scope_brief(it.get('all')) | {'lg': scope_brief(it.get('league'))}


def _zone_brief(it, up_water):
    """第 15 項：樣本數＋今場上盤水位所屬水位區（全庫及同聯賽）"""
    if not it or it.get('error') or not it.get('all'):
        return None
    name = None
    if up_water is not None:
        zi = screen_engine.zone_idx(up_water)
        name = screen_engine.ZONES[zi]

    def find_zone(s):
        if not s or name is None:
            return None
        for r in (s.get('zones') or []):
            if r.get('zone') == name:
                return r
        return None

    return {'n': it['all'].get('n'), 'zone': find_zone(it['all']),
            'lg_zone': find_zone(it.get('league'))}


def _gap_scope(scope):
    if not scope:
        return None

    def cv(d):
        if not d:
            return None
        return {'line': d.get('line'), 'n': d.get('n'),
                'up_r': d.get('up_r'), 'down_r': d.get('down_r'),
                'push_r': d.get('push_r'), 'gap': d.get('gap')}

    return {'mode': cv(scope.get('mode')), 'd50': cv(scope.get('d50'))}


def _gap_brief(it):
    """第 14/17 項：分佈最多盤口 及 最接近50%盤口（全庫＋同聯賽）"""
    if not it or it.get('error'):
        return None
    return {'all': _gap_scope(it.get('all')), 'lg': _gap_scope(it.get('league'))}


def _i18_brief(it):
    """第 18 項：結果上/下/走 率（全庫＋同聯賽）；結構係 all.oc / league.oc"""
    if not it or it.get('error'):
        return None
    def oc(scope):
        return _oc_brief(scope.get('oc')) if scope else None
    return {'all': oc(it.get('all')), 'lg': oc(it.get('league'))}


def _screen_brief(mid):
    """對單場跑篩查，抽出 1/5/8/12/15 摘要 及 14/17 差距（5 分鐘快取）"""
    ts, data = _pk_full_cache.get(mid, (0, None))
    if data is not None and time.time() - ts < 300:
        return data
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return None
        res = screen_engine.screen(conn, t)
        items = res.get('items', {})
        close = screen_engine.tline(t.get('odds') or {}, 'closing') or {}
        g = close.get('g')
        if g == 'home':
            up_water = close.get('ho')
        elif g == 'away':
            up_water = close.get('ao')
        else:
            ho, ao = close.get('ho'), close.get('ao')
            up_water = min(ho, ao) if ho is not None and ao is not None else None
        cur_line = screen_engine.fmt_line(close.get('h'), g) \
            if close.get('h') is not None else None
        brief = {
            'cur_line': cur_line,
            'cur_water': up_water,
            'i1': _pair_brief(items.get('1')),
            'i5': _pair_brief(items.get('5')),
            'i5z': _pair_brief(items.get('5Z')),
            'i8': _pair_brief(items.get('8')),
            'i12': _dist_brief(items.get('12'), cur_line),
            'i15': _zone_brief(items.get('15'), up_water),
            'i18': _i18_brief(items.get('18')),
            'g14': _gap_brief(items.get('14')),
            'g17': _gap_brief(items.get('17')),
        }
        if len(_pk_full_cache) > 200:
            _pk_full_cache.clear()
        _pk_full_cache[mid] = (time.time(), brief)
        return brief
    except Exception:
        import traceback
        traceback.print_exc()
        return None
    finally:
        conn.close()


def _build_pick_rec(row, now_str):
    """我的選擇單場卡片（基本資料＋結果＋brief）"""
    (mid, choice, ko, hs, aws, h, a, lg, hc, gv, ho, ao,
     ph, pg, pho, pao, hr_, ar_) = row
    rec = {'id': mid, 'choice': choice, 'kickoff': ko, 'home': h, 'away': a,
           'league': lg, 'rank_home': hr_, 'rank_away': ar_,
           # 尾盤數據（賽後定案）
           'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
           'odds': f'主{ho}/客{ao}' if ho is not None else None,
           # 揀嘅當刻嘅數據（快照）
           'pick_line': screen_engine.fmt_line(ph, pg) if ph is not None else None,
           'pick_odds': f'主{pho}/客{pao}' if pho is not None else None,
           # played = 已開賽（kickoff 過咗）；有冇賽果係另一回事
           'played': ko < now_str}
    if hs is not None:
        r = pick_result(hc, gv, hs, aws)
        rec['score'] = f'{hs}-{aws}'
        rec['result'] = 'X' if r is None else (
            'P' if r == 'P' else ('W' if (r == 'A') == (choice == 'up') else 'L'))
    rec['brief'] = _screen_brief(mid)
    return rec


def get_picks_full():
    conn = db()
    rows = conn.execute(
        'SELECT p.match_id, p.choice, m.kickoff, m.home_score, m.away_score, '
        'ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, '
        'p.pick_handicap, p.pick_giver, p.pick_home_odds, p.pick_away_odds, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank) '
        'FROM user_picks p '
        'JOIN matches m ON m.id=p.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'LEFT JOIN match_prestandings ps ON ps.match_id=m.id '
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        # 全部選擇永久保留（唔設 24 小時限制）
    ).fetchall()
    conn.close()
    now_str = time.strftime('%Y-%m-%d %H:%M:%S')

    def build(row):
        try:
            return _build_pick_rec(row, now_str)
        except Exception:
            traceback.print_exc()
            (mid, choice, ko, hs, aws, h, a, lg, hc, gv, ho, ao,
             ph, pg, pho, pao, hr_, ar_) = row
            return {'id': mid, 'choice': choice, 'kickoff': ko, 'home': h,
                    'away': a, 'league': lg, 'rank_home': hr_, 'rank_away': ar_,
                    'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
                    'odds': f'主{ho}/客{ao}' if ho is not None else None,
                    'pick_line': screen_engine.fmt_line(ph, pg)
                    if ph is not None else None,
                    'pick_odds': f'主{pho}/客{pao}' if pho is not None else None,
                    'played': ko < now_str, 'brief': None}

    # 並行起唄——單線程每場 5–8 秒，幾十場必超時
    out = list(_BUILD_POOL.map(build, rows))
    # 未開賽順時間排先；已開賽永久保留跟後（按日子分類），最新嘅排最前
    pending = sorted([r for r in out if not r['played']],
                     key=lambda r: r['kickoff'])
    played = sorted([r for r in out if r['played']],
                    key=lambda r: r['kickoff'], reverse=True)
    wins = sum(1 for r in played if r.get('result') == 'W')
    losses = sum(1 for r in played if r.get('result') == 'L')
    pushes = sum(1 for r in played if r.get('result') == 'P')
    stats = {'total': len(out), 'pending': len(pending), 'wins': wins,
             'losses': losses, 'pushes': pushes,
             'win_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': pending, 'played': played}


# ============ 精選（七重準則全通過嘅場次，永久保留＋自動結算） ============

_feat_scan = {'running': False, 'done': 0, 'total': 0, 'added': 0, 'added_z': 0,
              'last': None, 'error': None}


def _featured_direction(b, gates=True, z=False):
    """七重準則（2026-09-19 最終版；2026-09-21 加 z=精選Z：第⑤項改用 5Z 同主客版）：
    方向揀選：第①⑤項一齊睇——「上」：①⑤ 全部已存在範圍（全庫及同聯賽）上盤率≥50%；
    否則「下」：①⑤ 全部已存在範圍下盤率≥50%；上→下順序，兩者都唔得→唔入選。
    gates=True 時跟住 ①⑤⑧⑫⑮⑱：每項【全庫或同聯賽其中一個】同方向 ≥50% 即合格。
    gates=False 只揀方向（Check 下先用）。
    全部通過返回 'up'/'down'，任何一項不達標返回 None。"""
    if not b:
        return None
    k5 = 'i5z' if z else 'i5'

    def rate(oc, d):
        if not oc:
            return None
        return oc.get('up_r') if d == 'up' else oc.get('down_r')

    def ok(oc_all, oc_lg, d):
        # 全庫或同聯賽其中一個同方向 ≥50%
        for oc in (oc_all, oc_lg):
            r = rate(oc, d)
            if r is not None and r >= 0.5:
                return True
        return False

    def dir_pick(d):
        # ①⑤：全部已存在嘅範圍都要同方向 ≥50%（範圍冇數據就略過）
        found = False
        for k in ('i1', k5):
            it = b.get(k) or {}
            for oc in (it.get('all'), it.get('lg')):
                r = rate(oc, d)
                if r is not None:
                    found = True
                    if r < 0.5:
                        return False
        return found

    if dir_pick('up'):
        d = 'up'
    elif dir_pick('down'):
        d = 'down'
    else:
        return None
    if not gates:
        return d
    # ①⑤⑧：全庫或同聯賽其中一個同方向 ≥50%
    for k in ('i1', k5, 'i8'):
        it = b.get(k) or {}
        if not ok(it.get('all'), it.get('lg'), d):
            return None
    # ⑫：同尾盤盤口（全庫或同聯賽）
    i12 = b.get('i12') or {}
    if not ok(i12.get('cur'), (i12.get('lg') or {}).get('cur'), d):
        return None
    # ⑮：同尾盤＋今場水位區（全庫或同聯賽）
    i15 = b.get('i15') or {}
    if not ok(i15.get('zone'), i15.get('lg_zone'), d):
        return None
    # ⑱：排名差距淨值±1＋同尾盤（全庫或同聯賽）
    i18 = b.get('i18') or {}
    if not ok(i18.get('all'), i18.get('lg'), d):
        return None
    return d


def _featured_scan_job():
    """V2 精選掃描（2026-09-26 十六字頭規則）：
    每場跑 v2_engine.featured_letters——任一字頭 A–P 合格（1X&13X 同方向>49.99%
    ＋30 同盤同方向>49.99%＋33 同盤同方向>49.99%）即入選；
    精選Z＝只有同主隊字頭（I–P）合格。入選時記低合格字頭＋31深淺狀態（事後回查）。"""
    global _feat_scan
    if _feat_scan['running']:
        return
    _feat_scan.update(running=True, done=0, added=0, error=None)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM matches m "
            "JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 AND oc.handicap IS NOT NULL "
            "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','localtime') "
            "ORDER BY m.kickoff").fetchall()
        _feat_scan['total'] = len(rows)
        import v2_engine
        v2_engine.load_v2_pool(conn)   # 預熱數據池
        added = added_z = 0
        for (mid,) in rows:
            try:
                t = screen_engine.get_target(conn, mid)
                if not t:
                    continue
                fl = v2_engine.featured_letters(conn, t) or {}
                d = fl.get('direction')
                passed = fl.get('passed_letters') or []
                meta = json.dumps({'letters': passed, 'states': fl.get('states')},
                                  ensure_ascii=False)
                if d and not conn.execute(
                        'SELECT 1 FROM featured WHERE match_id=?', (mid,)).fetchone():
                    conn.execute(
                        'INSERT INTO featured(match_id, direction, letters, added_at) '
                        'VALUES(?,?,?,?)',
                        (mid, d, meta, time.strftime('%Y-%m-%d %H:%M:%S')))
                    log_featured(conn, mid, d)
                    conn.commit()
                    added += 1
                dz = None
                if passed:
                    zpass = [e for e in (fl.get('letters') or [])
                             if e.get('mix') == '同主隊' and e['letter'] in passed]
                    if zpass:
                        dz = zpass[0]['dir']
                if dz and not conn.execute(
                        'SELECT 1 FROM featured_z WHERE match_id=?', (mid,)).fetchone():
                    conn.execute(
                        'INSERT INTO featured_z(match_id, direction, letters, added_at) '
                        'VALUES(?,?,?,?)',
                        (mid, dz, meta, time.strftime('%Y-%m-%d %H:%M:%S')))
                    log_featured_z(conn, mid, dz)
                    conn.commit()
                    added_z += 1
            except Exception:
                pass
            _feat_scan['done'] += 1
        _feat_scan['added'] = added
        _feat_scan['added_z'] = added_z
        _feat_scan['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        _feat_scan['error'] = str(e)
    finally:
        conn.close()
        _feat_scan['running'] = False


def _recompute_featured_after_update():
    """盤口／賠率更新後重算精選：清走未結算入選（歷史已結算保留做統計），
    清數據池快取，再全量重新篩選。"""
    try:
        conn = db()
        conn.execute('DELETE FROM featured WHERE match_id IN '
                     '(SELECT id FROM matches WHERE home_score IS NULL)')
        conn.execute('DELETE FROM featured_z WHERE match_id IN '
                     '(SELECT id FROM matches WHERE home_score IS NULL)')
        conn.commit()
        conn.close()
        screen_engine._pool_cache['ts'] = 0   # 令 load_pool 重新載入新盤口
        try:
            import v2_engine
            v2_engine.invalidate_pool()       # V2 池（30m/15m/10m/5m 時點）都要清
            _v2_item_cache.clear()              # V2 單項快取一併清
        except Exception:
            pass
        print('[update] 重算精選…', flush=True)
        if not _feat_scan['running']:
            threading.Thread(target=_featured_scan_job, daemon=True).start()
    except Exception:
        import traceback
        traceback.print_exc()


def _build_featured_rec(row, tbl, now):
    """精選單場卡片：基本資料＋自動結算（未結算嘅以入選方向計 贏/輸/走）＋brief＋check＋letters"""
    (mid, d, res, added, ko, hs, aws, h, a, lg, hc, gv, ho, ao, letters, hr_,
     ar_) = row
    rec = {'id': mid, 'direction': d, 'added_at': added, 'kickoff': ko,
           'home': h, 'away': a, 'league': lg, 'rank_home': hr_, 'rank_away': ar_,
           'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
           'odds': f'主{ho}/客{ao}' if ho is not None else None,
           # played = 已開賽（kickoff 過咗）；冇賽果嘅會顯示「待結算」
           'played': ko < now}
    if letters:
        try:
            rec['letters'] = json.loads(letters)
        except Exception:
            rec['letters'] = None
    if hs is not None:
        r = pick_result(hc, gv, hs, aws)
        if res is None and r is not None:
            # 自動結算（以入選方向計）
            res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
            c2 = db()
            c2.execute(f'UPDATE {tbl} SET result=?, settled_at=? WHERE match_id=?',
                       (res, now, mid))
            c2.commit()
            c2.close()
        rec['score'] = f'{hs}-{aws}'
        rec['result'] = res
    rec['brief'] = _screen_brief(mid)
    rec['check'] = _check_combo_for(mid, d)     # 中咗 Check 下先邊條組合
    return rec


def get_featured_full(z=False):
    """精選全量：未開賽順時間排先；已完場最新排先（永不刪除）。
    z=True 讀精選Z（featured_z）。順便結算新完場場次（以入選方向計 贏/輸/走）。"""
    tbl = 'featured_z' if z else 'featured'
    conn = db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    pending_rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.added_at, m.kickoff, '
        'm.home_score, m.away_score, ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, f.letters, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank) '
        f'FROM {tbl} f JOIN matches m ON m.id=f.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'LEFT JOIN match_prestandings ps ON ps.match_id=m.id '
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        'WHERE m.kickoff >= ? ORDER BY m.kickoff', (now,)).fetchall()
    played_rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.added_at, m.kickoff, '
        'm.home_score, m.away_score, ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, f.letters, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank) '
        f'FROM {tbl} f JOIN matches m ON m.id=f.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'LEFT JOIN match_prestandings ps ON ps.match_id=m.id '
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        'WHERE m.kickoff < ? ORDER BY m.kickoff DESC', (now,)).fetchall()
    conn.close()

    def build(row):
        try:
            return _build_featured_rec(row, tbl, now)
        except Exception:
            traceback.print_exc()
            (mid, d, res, added, ko, hs, aws, h, a, lg, hc, gv, ho, ao, letters,
             hr_, ar_) = row
            return {'id': mid, 'direction': d, 'added_at': added,
                    'kickoff': ko, 'home': h, 'away': a, 'league': lg,
                    'rank_home': hr_, 'rank_away': ar_,
                    'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
                    'odds': f'主{ho}/客{ao}' if ho is not None else None,
                    'played': ko < now, 'brief': None, 'check': None,
                    'letters': None}

    # 並行起唄——單線程逐場 5–8 秒，幾十場必超時
    pending = list(_BUILD_POOL.map(build, pending_rows))
    played = list(_BUILD_POOL.map(build, played_rows))
    wins = sum(1 for r in played if r.get('result') == 'W')
    losses = sum(1 for r in played if r.get('result') == 'L')
    pushes = sum(1 for r in played if r.get('result') == 'P')
    stats = {'total': len(pending) + len(played), 'pending': len(pending),
             'played': len(played), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': pending, 'played': played,
            'scan': {k: _feat_scan[k] for k in ('running', 'done', 'total', 'added', 'last', 'error')}}


# ============ 過往賽果（已完場＋尾盤 → 上/下/走盤＋五項命中率統計＋篩選＋走勢） ============

def get_results(limit=1000, league=None, hc=None, gv=None, scope='featured'):
    """過往賽果（2026-09-20 起 = 精選場次專頁）。
    scope='featured'：只計精選已開賽場次；scope='all'：全部已完場（舊版口徑）。
    五項命中率：上盤命中率（方向=上嘅精選命中）、下盤命中率（方向=下）、
    總命中率／精選命中率（全部精選已結算）、我的選擇命中率。"""
    conn = db()
    featured = (scope in ('featured', 'featuredz'))
    tbl = 'featured_z' if scope == 'featuredz' else 'featured'
    where = ("WHERE m.kickoff < datetime('now','localtime')" if featured
             else 'WHERE m.home_score IS NOT NULL')
    args = []
    if league:
        where += ' AND c.req_name = ?'
        args.append(league)
    line_where = ''
    line_args = []
    if hc is not None:
        line_where = ' AND oc.handicap = ? AND ' + (
            'oc.giver IS NULL' if gv in (None, 'none') else 'oc.giver = ?')
        line_args.append(hc)
        if gv not in (None, 'none'):
            line_args.append(gv)
    if featured:
        base_from = (
            f'FROM {tbl} f JOIN matches m ON m.id=f.match_id '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
            "AND oc.label='closing' AND oc.company_id=12 ")
        stat_select = ('SELECT f.direction, oc.handicap, oc.giver, '
                       'm.home_score, m.away_score ')
        stat_where = where + ' AND m.home_score IS NOT NULL'
    else:
        base_from = (
            'FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN odds_asian oc ON oc.match_id=m.id '
            "AND oc.label='closing' AND oc.company_id=12 ")
        stat_select = ('SELECT NULL, oc.handicap, oc.giver, '
                       'm.home_score, m.away_score ')
        stat_where = where
    # 統計：符合篩選嘅場次（featured=已結算精選；all=有尾盤完場）
    stat_rows = conn.execute(
        stat_select + base_from + stat_where + line_where,
        args + line_args).fetchall()
    up = down = push = 0          # 賽果開出角度
    uw = ul = dw = dl = 0         # 精選方向命中角度（featured 才有方向）
    for d, hc_, gv_, hs, aws in stat_rows:
        r = pick_result(hc_, gv_, hs, aws)
        if r == 'A':
            up += 1
        elif r == 'B':
            down += 1
        elif r == 'P':
            push += 1
        else:
            continue
        if d and r != 'P':
            if (r == 'A') == (d == 'up'):
                if d == 'up':
                    uw += 1
                else:
                    dw += 1
            else:
                if d == 'up':
                    ul += 1
                else:
                    dl += 1
    eff = up + down
    n_all = len(stat_rows)
    ueff = uw + ul
    deff = dw + dl
    teff = ueff + deff
    stats = {
        'total': n_all, 'up': up, 'down': down, 'push': push,
        # 上/下盤命中率（精選方向角度，走盤唔計分母）；all 範圍退回開出比例
        'up_r': (uw / ueff if ueff else None) if featured
                else (up / eff if eff else None),
        'down_r': (dw / deff if deff else None) if featured
                  else (down / eff if eff else None),
        'decisive_r': ((uw + dw) / teff if teff else None) if featured
                      else (eff / n_all if n_all else None),
        # 方向分項場數（featured 顯示用）
        'up_n': ueff, 'up_hit': uw,
        'down_n': deff, 'down_hit': dw,
    }
    # 精選命中率（全部精選已結算，走盤唔計分母）
    fr = conn.execute(
        'SELECT result, COUNT(*) FROM featured WHERE result IS NOT NULL '
        'GROUP BY result').fetchall()
    fw = fl = fp = 0
    for res, n in fr:
        if res == 'W':
            fw = n
        elif res == 'L':
            fl = n
        else:
            fp = n
    stats['feat_w'] = fw
    stats['feat_l'] = fl
    stats['feat_p'] = fp
    stats['feat_r'] = fw / (fw + fl) if (fw + fl) else None
    # 我的選擇命中率（已完場，走盤唔計入分母）
    pr = conn.execute(
        'SELECT p.choice, oc.handicap, oc.giver, m.home_score, m.away_score '
        'FROM user_picks p JOIN matches m ON m.id=p.match_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'WHERE m.home_score IS NOT NULL').fetchall()
    pw = pl = pp = 0
    for choice, hc_, gv_, hs, aws in pr:
        r = pick_result(hc_, gv_, hs, aws)
        if r is None:
            continue
        if r == 'P':
            pp += 1
        elif (r == 'A') == (choice == 'up'):
            pw += 1
        else:
            pl += 1
    stats['pk_w'] = pw
    stats['pk_l'] = pl
    stats['pk_p'] = pp
    stats['pk_r'] = pw / (pw + pl) if (pw + pl) else None
    # 走勢：按日計（featured=逐日命中；all=逐日開出上/下），最近 30 日，舊→新
    trend_select = ('SELECT substr(m.kickoff,1,10) d, f.direction, '
                    'oc.handicap, oc.giver, m.home_score, m.away_score '
                    if featured else
                    'SELECT substr(m.kickoff,1,10) d, NULL, '
                    'oc.handicap, oc.giver, m.home_score, m.away_score ')
    trend_rows = conn.execute(
        trend_select + base_from + stat_where + line_where + ' ORDER BY d',
        args + line_args).fetchall()
    by_day = {}
    for d, drc, hc_, gv_, hs, aws in trend_rows:
        r = pick_result(hc_, gv_, hs, aws)
        b = by_day.setdefault(d, {'up': 0, 'down': 0, 'push': 0,
                                  'w': 0, 'l': 0})
        if r == 'A':
            b['up'] += 1
        elif r == 'B':
            b['down'] += 1
        elif r == 'P':
            b['push'] += 1
        else:
            continue
        if drc and r != 'P':
            if (r == 'A') == (drc == 'up'):
                b['w'] += 1
            else:
                b['l'] += 1
    trend = [{'date': d, **by_day[d]} for d in sorted(by_day)][-30:]
    # 篩選選項：聯賽清單＋常見盤口清單（已開賽場次計）
    if featured:
        lg_from = ('FROM featured f JOIN matches m ON m.id=f.match_id '
                   'JOIN seasons s ON s.id=m.season_id '
                   'JOIN competitions c ON c.titan_id=s.titan_id ')
        lg_where = "WHERE m.kickoff < datetime('now','localtime')"
    else:
        lg_from = ('FROM matches m JOIN seasons s ON s.id=m.season_id '
                   'JOIN competitions c ON c.titan_id=s.titan_id ')
        lg_where = 'WHERE m.home_score IS NOT NULL'
    leagues = [r[0] for r in conn.execute(
        'SELECT DISTINCT c.req_name ' + lg_from + lg_where +
        ' ORDER BY c.req_name').fetchall()]
    line_rows = conn.execute(
        'SELECT oc.handicap, oc.giver, COUNT(*) n ' +
        base_from + where + ' AND oc.handicap IS NOT NULL' +
        ' GROUP BY oc.handicap, oc.giver ORDER BY n DESC LIMIT 80',
        args).fetchall()
    lines = [{'line': screen_engine.fmt_line(hc_, gv_), 'hc': hc_,
              'gv': gv_ or 'none', 'n': n}
             for hc_, gv_, n in line_rows]
    # 最近 N 場列表（最新排先，同一篩選）
    if featured:
        rows = conn.execute(
            'SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc, '
            'm.home_score, m.away_score, oc.handicap, oc.giver, '
            'oc.home_odds, oc.away_odds, f.direction, f.result, f.added_at '
            'FROM featured f JOIN matches m ON m.id=f.match_id '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
            "AND oc.label='closing' AND oc.company_id=12 " +
            where + line_where +
            ' ORDER BY m.kickoff DESC LIMIT ?',
            args + line_args + [limit]).fetchall()
    else:
        rows = conn.execute(
            'SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc, '
            'm.home_score, m.away_score, oc.handicap, oc.giver, '
            'oc.home_odds, oc.away_odds, NULL, NULL, NULL '
            'FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
            "AND oc.label='closing' AND oc.company_id=12 " +
            where + line_where +
            ' ORDER BY m.kickoff DESC LIMIT ?',
            args + line_args + [limit]).fetchall()
    items = []
    for (mid, ko, lg, h, a, hs, aws, hc_, gv_, ho, ao,
         drc, fres, added) in rows:
        r = pick_result(hc_, gv_, hs, aws) \
            if (hc_ is not None and hs is not None) else None
        items.append({
            'id': mid, 'kickoff': ko, 'league': lg, 'home': h, 'away': a,
            'score': f'{hs}-{aws}' if hs is not None else None,
            'line': screen_engine.fmt_line(hc_, gv_) if hc_ is not None else None,
            'odds': f'主{ho}/客{ao}' if ho is not None else None,
            'outcome': ('up' if r == 'A' else 'down' if r == 'B'
                        else 'push' if r == 'P' else None),
            # 精選專屬：入選方向＋結算結果
            'direction': drc, 'ft_result': fres, 'added_at': added,
        })
    conn.close()
    return {'stats': stats, 'items': items, 'limit': limit,
            'trend': trend, 'leagues': leagues, 'lines': lines,
            'filter': {'league': league, 'hc': hc, 'gv': gv},
            'scope': scope}


# ============ 過往紀錄（精選一出現即自動紀錄尾盤快照，永久保留，計一場） ============

def get_featlog(z=False):
    """過往紀錄：任何渠道入選精選都會自動影低當刻尾盤（featured_log / featured_z_log）。
    同一場多次紀錄 → 計一場（保留首次入選時間，尾盤快照用最新）。
    結算以 log 影低嘅尾盤快照計，唔係而家嘅 closing。z=True 讀精選Z。"""
    tbl = 'featured_z_log' if z else 'featured_log'
    conn = db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        'SELECT l.match_id, l.direction, l.added_at, l.handicap, l.giver, '
        'l.home_odds, l.away_odds, m.kickoff, m.home_score, m.away_score, '
        'ht.name_tc, at.name_tc, c.req_name '
        f'FROM {tbl} l JOIN matches m ON m.id=l.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'ORDER BY m.kickoff DESC').fetchall()
    conn.close()
    pending, played = [], []
    for (mid, d, added, hc, gv, ho, ao, ko, hs, aws, h, a, lg) in rows:
        rec = {'id': mid, 'direction': d, 'added_at': added, 'kickoff': ko,
               'home': h, 'away': a, 'league': lg,
               'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
               'odds': f'主{ho}/客{ao}' if ho is not None else None,
               'played': ko < now}
        if hs is not None:
            rec['score'] = f'{hs}-{aws}'
            r = pick_result(hc, gv, hs, aws)
            rec['result'] = ('P' if r == 'P' else
                             'W' if ((r == 'A') == (d == 'up')) else 'L') \
                if r is not None else None
        (pending if ko >= now else played).append(rec)
    pending.reverse()   # 未開賽順開賽時間排
    wins = sum(1 for r in played if r.get('result') == 'W')
    losses = sum(1 for r in played if r.get('result') == 'L')
    pushes = sum(1 for r in played if r.get('result') == 'P')
    stats = {'total': len(pending) + len(played), 'pending': len(pending),
             'played': len(played), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': pending, 'played': played}


# ============ Check 下先（8 組合回測：①⑤⑧⑫⑮⑱方向 × ⑭深淺 × ⑰深淺） ============

_check_scan = {'running': False, 'done': 0, 'total': 0, 'kept': 0,
               'last': None, 'error': None}

_GMAP = {'home': 1, 'away': -1, 'none': 0}


def _rates_of(m, res):
    """mask → {n, up_r, down_r}（分母剔除走盤），n=0 返回 None"""
    n = int(m.sum())
    if n == 0:
        return None
    r = res[m]
    a = int((r == 1).sum())
    p = int((r == 0).sum())
    eff = n - p
    return {'n': n,
            'up_r': a / eff if eff else None,
            'down_r': (eff - a) / eff if eff else None}


def _idx_rates(idx, res):
    """行號陣列版 _rates_of（加速回測用），idx=None 或空 → None"""
    if idx is None or idx.size == 0:
        return None
    n = int(idx.size)
    r = res[idx]
    a = int((r == 1).sum())
    p = int((r == 0).sum())
    eff = n - p
    return {'n': n,
            'up_r': a / eff if eff else None,
            'down_r': (eff - a) / eff if eff else None}


def _lg_rates(idx, res, lg, t_lg):
    """行號陣列嘅同聯賽版比例"""
    if idx is None or idx.size == 0:
        return None
    return _idx_rates(idx[lg[idx] == t_lg], res)


def _d50_dir_idx(idx, c_h, c_gc, res, t_h, t_gc):
    """樣本行號 → 最接近50%盤口（n>=5，冇就用分佈最多）對今場尾盤嘅方向：
    'deep'=上盤(讓球方)深咗，'shallow'=上盤淺咗，None=無樣本或同盤"""
    if idx is None or idx.size == 0:
        return None
    q = np.round(c_h[idx] * 4).astype(np.int32)      # 讓球數×4（整數）
    key = q * 4 + (c_gc[idx] + 1)                     # 複合鍵：讓球數+讓球方
    uk, inv = np.unique(key, return_inverse=True)
    cnt = np.bincount(inv)
    up = np.bincount(inv, weights=(res[idx] == 1))
    push = np.bincount(inv, weights=(res[idx] == 0))
    eff = cnt - push
    with np.errstate(invalid='ignore', divide='ignore'):
        up_r = np.where(eff > 0, up / np.maximum(eff, 1), np.nan)
    cand = np.where((cnt >= 5) & (eff > 0))[0]
    if cand.size:
        j = int(cand[np.nanargmin(np.abs(up_r[cand] - 0.5))])
    else:
        j = int(np.argmax(cnt))
    rq = (uk[j] // 4) / 4.0
    rg = uk[j] % 4 - 1
    rs_ = 0.0 if (rq == 0 or rg == 0) else (rq if rg == 1 else -rq)
    cs_ = 0.0 if (t_h == 0 or t_gc == 0) else (t_h if t_gc == 1 else -t_h)
    delta = cs_ - rs_
    if abs(delta) < 1e-9:
        return None
    gv = 1.0 if (t_gc == 1 or t_gc == 0) else -1.0   # 平手當主=上盤
    return 'deep' if delta * gv > 0 else 'shallow'


def _d50_dir(m, c_h, c_gc, res, t_h, t_gc):
    """mask 版（兼容舊呼叫）：轉行號後交畀 _d50_dir_idx"""
    return _d50_dir_idx(np.nonzero(m)[0], c_h, c_gc, res, t_h, t_gc)


_EMPTY_IDX = np.zeros(0, dtype=np.int64)

# 精選場次「Check 下先」組合快取（10 分鐘）
_check_combo_cache = {}


def _check_combo_for(mid, direction):
    """精選場次中咗 Check 下先邊條組合。
    目標場嘅尾盤/排名/上次比賽經 get_target 攞（未開賽場唔喺歷史池入面），
    樣本索引嚟自歷史池；g14/g17 分類同回測掃描完全一致（上盤角度 deep/shallow），
    再喺 check_rows 度攞該組合嘅歷史開出統計。
    回傳 {'g14','g17','n','up','down','push','up_r','down_r'}；唔中任何組合 → None"""
    key = (mid, direction)
    ent = _check_combo_cache.get(key)
    if ent and time.time() - ent[0] < 600:
        return ent[1]
    out = None
    try:
        conn = db()
        t = screen_engine.get_target(conn, mid)
        close = screen_engine.tline((t or {}).get('odds') or {}, 'closing')
        if t and close:
            df, _prev = screen_engine.load_pool(conn)
            c_h = df['c_h'].to_numpy()
            c_gc = df['c_g'].fillna('none').map(_GMAP).to_numpy()
            res = df['res'].map({'A': 1, 'B': -1, 'P': 0}).to_numpy()
            t_h = float(close['h'])
            t_g = _GMAP.get(close.get('g') or 'none', 0)
            # ⑫ 樣本：主場/客場排名 ±2（同回測掃描一致）
            hhr = t['pre']['home_home_rank']
            aar = t['pre']['away_away_rank']
            hhr_a = df['home_home_rank'].to_numpy()
            aar_a = df['away_away_rank'].to_numpy()
            idx12 = None
            if hhr is not None and aar is not None:
                r1, r2 = int(hhr), int(aar)
                cells = []
                for di in range(-2, 3):
                    for dj in range(-2, 3):
                        w = (hhr_a == r1 + di) & (aar_a == r2 + dj)
                        if w.any():
                            cells.append(np.nonzero(w)[0])
                idx12 = np.unique(np.concatenate(cells)) if cells else _EMPTY_IDX
            g14 = (_d50_dir_idx(idx12, c_h, c_gc, res, t_h, t_g)
                   if idx12 is not None else None)
            # ⑰ 樣本：今次主隊上次比賽（角色＋盤口，同回測掃描一致）
            g17d = None
            hp = t.get('home_prev')
            if hp and hp.get('h') is not None:
                hp_h = df['hp_h'].to_numpy()
                hp_gc = df['hp_g'].fillna('none').map(_GMAP).to_numpy()
                hp_rc = df['hp_role'].map({'home': 1, 'away': 0}).fillna(-1).to_numpy()
                t_rc = 1 if hp['role'] == 'home' else 0
                t_hp = float(hp['h'])
                t_hg = _GMAP.get(hp.get('g') or 'none', 0)
                w = (hp_rc == t_rc) & (hp_h == t_hp) & (hp_gc == t_hg)
                idx17 = np.nonzero(w)[0] if w.any() else _EMPTY_IDX
                g17d = _d50_dir_idx(idx17, c_h, c_gc, res, t_h, t_g)
            if g14 and g17d:
                rows = conn.execute(
                    'SELECT res, COUNT(*) FROM check_rows '
                    'WHERE direction=? AND g14=? AND g17=? GROUP BY res',
                    (direction, g14, g17d)).fetchall()
                up = down = push = 0
                for r_, n in rows:
                    if r_ == 'A':
                        up = n
                    elif r_ == 'B':
                        down = n
                    else:
                        push = n
                eff = up + down
                out = {'g14': g14, 'g17': g17d, 'n': up + down + push,
                       'up': up, 'down': down, 'push': push,
                       'up_r': up / eff if eff else None,
                       'down_r': down / eff if eff else None}
        conn.close()
    except Exception as e:
        traceback.print_exc()
        out = {'error': str(e)[:200]}     # 計算失敗同「唔中組合」分開顯示
    if len(_check_combo_cache) > 500:
        _check_combo_cache.clear()
    _check_combo_cache[key] = (time.time(), out)
    return out



def _check_scan_job(table='check_rows'):
    """全庫回測：每場計 ①⑤⑧⑫⑮⑱ 方向（同精選一致）＋⑭⑰ 最接近50%盤嘅上盤深淺。
    結果落 check_rows 表（可中斷續跑）。
    v2 索引版：盤口用字典分組、排名/入球用整數格索引，逐場成本由 O(N) 降到 O(組)，
    免費雲端機限速下都由 0.3場/秒 提升到可接受速度。"""
    global _check_scan
    if _check_scan['running']:
        return
    _check_scan.update(running=True, error=None)
    conn = db()
    try:
        df, _prev = screen_engine.load_pool(conn)
        n_total = len(df)
        _check_scan['total'] = n_total
        ids = df['id'].to_numpy()
        lg = df['league'].astype('category').cat.codes.to_numpy()
        res = df['res'].map({'A': 1, 'B': -1, 'P': 0}).to_numpy()
        i_h = df['i_h'].to_numpy(); i_ho = df['i_ho'].to_numpy(); i_ao = df['i_ao'].to_numpy()
        c_h = df['c_h'].to_numpy(); c_ho = df['c_ho'].to_numpy(); c_ao = df['c_ao'].to_numpy()
        i_gc = df['i_g'].fillna('none').map(_GMAP).to_numpy()
        c_gc = df['c_g'].fillna('none').map(_GMAP).to_numpy()
        pv_h = df['pv_h'].to_numpy(); pv_ho = df['pv_ho'].to_numpy(); pv_ao = df['pv_ao'].to_numpy()
        pv_gc = df['pv_g'].fillna('none').map(_GMAP).to_numpy()
        hhr = df['home_home_rank'].to_numpy(); aar = df['away_away_rank'].to_numpy()
        hh_p = df['home_home_p'].to_numpy(); aa_p = df['away_away_p'].to_numpy()
        hh_gf = df['home_home_gf'].to_numpy(); hh_ga = df['home_home_ga'].to_numpy()
        hh_gd = df['home_home_gd'].to_numpy()
        aa_gf = df['away_away_gf'].to_numpy(); aa_ga = df['away_away_ga'].to_numpy()
        aa_gd = df['away_away_gd'].to_numpy()
        hp_h = df['hp_h'].to_numpy()
        hp_gc = df['hp_g'].fillna('none').map(_GMAP).to_numpy()
        hp_rc = df['hp_role'].map({'home': 1, 'away': 0}).fillna(-1).to_numpy()
        zone = df['up_zone'].to_numpy()

        # ---- 預建索引（一次過，之後每場 O(組) 查詢）----
        ihl = i_h.tolist(); igl = i_gc.tolist()
        chl = c_h.tolist(); cgl = c_gc.tolist()
        pvl = pv_h.tolist(); pgl = pv_gc.tolist()
        hpl = hp_h.tolist(); hgl = hp_gc.tolist(); hrl = hp_rc.tolist()
        # ①：初盤＋尾盤盤口完全相同 → 候選組
        g1 = {}
        for i in range(n_total):
            g1.setdefault((ihl[i], igl[i], chl[i], cgl[i]), []).append(i)
        for k_ in g1:
            g1[k_] = np.asarray(g1[k_], dtype=np.int64)
        # ⑤：對上一次對賽尾盤盤口
        g5 = {}
        for i in range(n_total):
            h5 = pvl[i]
            if h5 == h5:    # 非 NaN
                g5.setdefault((h5, pgl[i]), []).append(i)
        for k_ in g5:
            g5[k_] = np.asarray(g5[k_], dtype=np.int64)
        # ⑧：主隊主場（hh_p>0）/ 客隊客場（aa_p>0）入失球統計（全部整數）
        v8 = (hh_p > 0) & (aa_p > 0)
        i8v = np.nonzero(v8)[0]
        HH_GF = hh_gf[i8v]; HH_GA = hh_ga[i8v]; HH_GD = hh_gd[i8v]
        AA_GF = aa_gf[i8v]; AA_GA = aa_ga[i8v]; AA_GD = aa_gd[i8v]
        # ⑫/⑱：排名整數格（兩隊排名齊先有效）
        v12 = ~(np.isnan(hhr) | np.isnan(aar))
        i12v = np.nonzero(v12)[0]
        g12 = {}
        for i in i12v.tolist():
            g12.setdefault((int(hhr[i]), int(aar[i])), []).append(i)
        for k_ in g12:
            g12[k_] = np.asarray(g12[k_], dtype=np.int64)
        gap = hhr - aar
        g18 = {}
        for i in i12v.tolist():
            g18.setdefault(int(gap[i]), []).append(i)
        for k_ in g18:
            g18[k_] = np.asarray(g18[k_], dtype=np.int64)
        # ⑰：上次對賽角色＋盤口
        g17 = {}
        for i in range(n_total):
            h7 = hpl[i]
            if h7 == h7 and hrl[i] >= 0:
                g17.setdefault((hrl[i], h7, hgl[i]), []).append(i)
        for k_ in g17:
            g17[k_] = np.asarray(g17[k_], dtype=np.int64)

        existing = set(r[0] for r in conn.execute(f'SELECT match_id FROM {table}'))
        _check_scan['done'] = len(existing)
        kept = int(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        _check_scan['kept'] = kept
        batch = []
        for k in range(n_total):
            mid = int(ids[k])
            if mid in existing:
                continue
            try:
                t_h = chl[k]; t_g = int(cgl[k])
                t_ho = float(c_ho[k]); t_ao = float(c_ao[k])
                t_lg = int(lg[k])
                idx12 = None
                # ① 同初盤及尾盤（候選初盤&尾盤都對照今場尾盤 t_h/t_g，
                #   同舊 mask (i_h==t_h)&(i_gc==t_g)&(c_h==t_h)&(c_gc==t_g) 一致）
                cand = g1.get((t_h, t_g, t_h, t_g))
                if cand is not None:
                    w = (np.abs(i_ho[cand] - t_ho) <= 0.03) & \
                        (np.abs(i_ao[cand] - t_ao) <= 0.03) & \
                        (np.abs(c_ho[cand] - t_ho) <= 0.03) & \
                        (np.abs(c_ao[cand] - t_ao) <= 0.03)
                    idx1 = cand[w]
                else:
                    idx1 = None
                # ⑤ 對上一次對賽尾盤 vs 今場尾盤（候選嘅 pv 盤口 = 今場尾盤 t_h/t_g；
                #   同舊 mask (pv_h==t_h)&(pv_gc==t_g) 一致，唔係對照今場嘅 pv）
                cand5 = g5.get((t_h, t_g))
                if cand5 is not None:
                    w5 = (np.abs(pv_ho[cand5] - t_ho) <= 0.03) & \
                         (np.abs(pv_ao[cand5] - t_ao) <= 0.03)
                    idx5 = cand5[w5]
                else:
                    idx5 = None
                brief = {'i1': {'all': _idx_rates(idx1, res),
                                'lg': _lg_rates(idx1, res, lg, t_lg)},
                         'i5': {'all': _idx_rates(idx5, res),
                                'lg': _lg_rates(idx5, res, lg, t_lg)}}
                d = _featured_direction(brief, gates=False)
                if d:
                    # ⑧ 主隊主場/客隊客場 入失球差 ±3
                    if hh_p[k] > 0 and aa_p[k] > 0:
                        m8 = (np.abs(HH_GF - hh_gf[k]) <= 3) & \
                             (np.abs(HH_GA - hh_ga[k]) <= 3) & \
                             (np.abs(HH_GD - hh_gd[k]) <= 3) & \
                             (np.abs(AA_GF - aa_gf[k]) <= 3) & \
                             (np.abs(AA_GA - aa_ga[k]) <= 3) & \
                             (np.abs(AA_GD - aa_gd[k]) <= 3)
                        idx8 = i8v[m8]
                        brief['i8'] = {'all': _idx_rates(idx8, res),
                                       'lg': _lg_rates(idx8, res, lg, t_lg)}
                    # 快速閘：i1/i5/i8 過唔到就唔使計 ⑫⑱（同 _featured_direction 首批閘一致）
                    def _ok1418(oc_all, oc_lg, dd):
                        for oc in (oc_all, oc_lg):
                            if oc:
                                r_ = oc.get('up_r') if dd == 'up' else oc.get('down_r')
                                if r_ is not None and r_ >= 0.5:
                                    return True
                        return False
                    i8b = brief.get('i8') or {}
                    if not (_ok1418(brief['i1']['all'], brief['i1']['lg'], d) and
                            _ok1418(brief['i5']['all'], brief['i5']['lg'], d) and
                            _ok1418(i8b.get('all'), i8b.get('lg'), d)):
                        d = None
                if d:
                    # ⑫ 主場/客場排名±2
                    if not (np.isnan(hhr[k]) or np.isnan(aar[k])):
                        r1, r2 = int(hhr[k]), int(aar[k])
                        cells = []
                        for di in range(-2, 3):
                            for dj in range(-2, 3):
                                cc = g12.get((r1 + di, r2 + dj))
                                if cc is not None:
                                    cells.append(cc)
                        idx12 = np.unique(np.concatenate(cells)) if cells else _EMPTY_IDX
                        m15 = idx12[(c_h[idx12] == t_h) & (c_gc[idx12] == t_g)] \
                            if idx12.size else _EMPTY_IDX
                        brief['i12'] = {'n': int(idx12.size),
                                        'cur': _idx_rates(m15, res),
                                        'lg': {'cur': _lg_rates(m15, res, lg, t_lg)}}
                        # ⑮ = ⑫＋同尾盤，再按今場上盤水位區
                        t_zone = int(zone[k])
                        m15z = m15[zone[m15] == t_zone] if m15.size else _EMPTY_IDX
                        brief['i15'] = {'zone': _idx_rates(m15z, res),
                                        'lg_zone': _lg_rates(m15z, res, lg, t_lg)}
                        # ⑱ 排名差距淨值±1＋同尾盤
                        g0 = int(gap[k])
                        c18 = []
                        for dg in range(-1, 2):
                            cc = g18.get(g0 + dg)
                            if cc is not None:
                                c18.append(cc)
                        idx18 = np.unique(np.concatenate(c18)) if c18 else _EMPTY_IDX
                        m18 = idx18[(c_h[idx18] == t_h) & (c_gc[idx18] == t_g)] \
                            if idx18.size else _EMPTY_IDX
                        brief['i18'] = {'all': _idx_rates(m18, res),
                                        'lg': _lg_rates(m18, res, lg, t_lg)}
                    d = _featured_direction(brief)
                if d:
                    g14 = (_d50_dir_idx(idx12, c_h, c_gc, res, t_h, t_g)
                           if idx12 is not None else None)
                    g17d = None
                    if hpl[k] == hpl[k] and hrl[k] >= 0:   # hp 有效
                        idx17 = g17.get((hrl[k], hpl[k], hgl[k]))
                        g17d = _d50_dir_idx(idx17, c_h, c_gc, res, t_h, t_g)
                    r = res[k]
                    batch.append((mid, d, g14, g17d,
                                  'A' if r == 1 else ('B' if r == -1 else 'P')))
                    if g14 and g17d:
                        kept += 1
            except Exception:
                pass
            _check_scan['done'] += 1
            if len(batch) >= 500:
                conn.executemany(
                    f'INSERT OR IGNORE INTO {table}(match_id, direction, g14, g17, res) '
                    'VALUES(?,?,?,?,?)', batch)
                conn.commit()
                batch.clear()
                _check_scan['kept'] = kept
        if batch:
            conn.executemany(
                f'INSERT OR IGNORE INTO {table}(match_id, direction, g14, g17, res) '
                'VALUES(?,?,?,?,?)', batch)
            conn.commit()
        _check_scan['kept'] = kept
        _check_scan['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        _check_scan['error'] = str(e)
    finally:
        conn.close()
        _check_scan['running'] = False


def get_check():
    """8 組合開出上/下盤比例（g14/g17 以『上盤』深淺編碼；顯示時下方向換算）"""
    conn = db()
    rows = conn.execute(
        "SELECT direction, g14, g17, res, COUNT(*) FROM check_rows "
        "WHERE direction IS NOT NULL AND g14 IS NOT NULL AND g17 IS NOT NULL "
        "GROUP BY direction, g14, g17, res").fetchall()
    n_rows = conn.execute('SELECT COUNT(*) FROM check_rows').fetchone()[0]
    conn.close()
    combos = {}
    for d, g14, g17, r, n in rows:
        c = combos.setdefault((d, g14, g17), {'n': 0, 'up': 0, 'down': 0, 'push': 0})
        c['n'] += n
        c['up' if r == 'A' else ('down' if r == 'B' else 'push')] += n
    out = []
    for (d, g14, g17), c in combos.items():
        eff = c['up'] + c['down']
        out.append({'direction': d, 'g14': g14, 'g17': g17, **c,
                    'up_r': c['up'] / eff if eff else None,
                    'down_r': c['down'] / eff if eff else None})
    return {'combos': out, 'rows': n_rows,
            'scan': {k: _check_scan[k] for k in
                     ('running', 'done', 'total', 'kept', 'last', 'error')}}


# ---------- V2 API（2026-09-26：45 項引擎／16 字頭精選／Check 2 行×9 組／組合篩查） ----------
_v2_item_cache = {}
_v2_item_cache_lock = threading.Lock()
V2_ITEM_CACHE_TTL = 600        # 10 分鐘快取（同一賽事同一項重複撳唔使重算）


def _jdefault(o):
    """numpy 型別 → Python 原生（json.dumps 保險）"""
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    raise TypeError(f'not serializable: {type(o)}')


def api_v2_summary(mid):
    """一頁過：邊 45 項適用＋精選 16 字頭結果＋今場 12 段水位位置"""
    import v2_engine
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}
        v2_engine.load_v2_pool(conn)
        app_ok = v2_engine.item_applicability(conn, t)
        letters = v2_engine.featured_letters(conn, t) or {}
        odds = t.get('odds') or {}
        T = screen_engine.tline(odds, 'closing') or screen_engine.tline(odds, 'initial')
        uw = v2_engine._up_water_of(T) if T else None
        return {'ok': True,
                'applicability': app_ok,
                'letters': letters,
                'cur_zone12': v2_engine.zone12_of(uw),
                'zones12': v2_engine.ZONES12,
                'cur_up_water': uw}
    finally:
        conn.close()


def api_v2_item(mid, no):
    """單項計算（10 分鐘快取）"""
    import v2_engine
    key = (mid, str(no))
    now = time.time()
    with _v2_item_cache_lock:
        hit = _v2_item_cache.get(key)
        if hit and now - hit[0] < V2_ITEM_CACHE_TTL:
            return hit[1]
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}
        v2_engine.load_v2_pool(conn)
        out = v2_engine.compute_item(conn, t, str(no))
        out.setdefault('ok', 'error' not in out)
        with _v2_item_cache_lock:
            _v2_item_cache[key] = (now, out)
        return out
    finally:
        conn.close()


def api_v2_check(mid):
    """Check 下先：2 行×3×3（行1＝同主客原盤；行2＝計埋互換）；mid=0 即全庫統計"""
    import v2_engine
    conn = db()
    try:
        t = None
        if mid:
            t = screen_engine.get_target(conn, mid)
            if not t:
                return {'error': '找不到賽事'}
        return v2_engine.check_grid(conn, t)
    finally:
        conn.close()


def api_v2_combo(mid, sel):
    """組合篩查：1–35 多選（30/31/33 唔准剔），分全庫／同一聯賽／同類別"""
    import v2_engine
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}
        v2_engine.load_v2_pool(conn)
        out = v2_engine.combo(conn, t, sel)
        out.setdefault('ok', 'error' not in out)
        return out
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/' or u.path == '/index.html':
            with open(os.path.join(STATIC_DIR, 'index.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
            return
        if u.path.startswith('/static/'):
            fp = os.path.join(STATIC_DIR, os.path.basename(u.path))
            if os.path.isfile(fp):
                ext = os.path.splitext(fp)[1]
                ct = {'.js': 'text/javascript', '.css': 'text/css',
                      '.html': 'text/html'}.get(ext, 'application/octet-stream')
                with open(fp, 'rb') as f:
                    self._send(200, f.read(), ct)
                return
            self._send(404, '{}')
            return
        if u.path == '/api/ready':
            self._send(200, json.dumps({
                'ready': _pool_ready['done'], 'version': SERVER_VERSION,
                'started': _started, 'error': _pool_ready['err']},
                ensure_ascii=False))
            return
        if u.path == '/api/upcoming':
            q = parse_qs(u.query)
            hours = int(q.get('hours', ['48'])[0])
            self._send(200, json.dumps(
                {'upcoming': upcoming(hours), 'played': played()},
                ensure_ascii=False))
            return
        if u.path == '/api/screen':
            q = parse_qs(u.query)
            mid = int(q.get('id', ['0'])[0])
            sel = None
            if q.get('sel'):
                sel = [x.strip() for x in q['sel'][0].split(',') if x.strip()] or None
            try:
                self._send(200, json.dumps(do_screen(mid, sel), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/picks':
            try:
                self._send(200, json.dumps(get_picks(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/update-status':
            try:
                self._send(200, json.dumps(update_status(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/update-window-status':
            self._send(200, json.dumps(win_status(), ensure_ascii=False))
            return
        if u.path == '/api/picks/full':
            try:
                self._send(200, json.dumps(get_picks_full(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/featured/full':
            try:
                qs = parse_qs(u.query)
                z = qs.get('z', [''])[0] in ('1', 'true')
                self._send(200, json.dumps(get_featured_full(z=z), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/featured/scan-status':
            self._send(200, json.dumps(_feat_scan, ensure_ascii=False))
            return
        if u.path == '/api/check/full':
            try:
                self._send(200, json.dumps(get_check(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/check/scan-status':
            self._send(200, json.dumps(_check_scan, ensure_ascii=False))
            return
        if u.path == '/api/results':
            try:
                qs = parse_qs(u.query)
                limit = int(qs.get('limit', ['1000'])[0])
                league = qs.get('league', [''])[0] or None
                hc = qs.get('hc', [''])[0]
                hc = float(hc) if hc not in ('', None) else None
                gv = qs.get('gv', [''])[0] or None
                scope = qs.get('scope', ['featured'])[0]
                self._send(200, json.dumps(
                    get_results(limit, league, hc, gv, scope), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/featlog':
            try:
                qs = parse_qs(u.query)
                z = qs.get('z', [''])[0] in ('1', 'true')
                self._send(200, json.dumps(get_featlog(z=z), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v2/summary':
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                self._send(200, json.dumps(api_v2_summary(mid), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v2/item':
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                no = qs.get('no', [''])[0]
                self._send(200, json.dumps(api_v2_item(mid, no), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v2/check':
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                self._send(200, json.dumps(api_v2_check(mid), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        self._send(404, '{}')

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == '/api/fetch':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_fetch(int(body.get('id')), bool(body.get('force'))),
                    ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/update':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(do_update(bool(body.get('auto'))),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/update-window':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_update_window(str(body.get('window', ''))), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/pick':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_pick(int(body.get('id')), str(body.get('choice', ''))),
                    ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/featured/refresh':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_featured_refresh(int(body.get('id'))), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/featured/scan':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                if body.get('reset'):
                    conn = db()
                    conn.execute('DELETE FROM featured')
                    conn.commit()
                    conn.close()
                if not _feat_scan['running']:
                    threading.Thread(target=_featured_scan_job, daemon=True).start()
                self._send(200, json.dumps({'started': True, 'running': True,
                                            'reset': bool(body.get('reset'))},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/check/scan':
            try:
                if not _check_scan['running']:
                    threading.Thread(target=_check_scan_job, daemon=True).start()
                self._send(200, json.dumps({'started': True, 'running': True},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/pick/delete':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(do_pick_delete(int(body.get('id'))),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v2/combo':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    api_v2_combo(int(body.get('id')), body.get('sel') or []),
                    ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        self._send(404, '{}')


def _warmup():
    """伺服器啟動時在背景預先載入歷史數據池，用戶第一擊唔使等"""
    try:
        conn = sqlite3.connect(DB_PATH)
        screen_engine.load_pool(conn)
        conn.close()
        _pool_ready['done'] = True
        print('[warmup] 歷史數據池已就緒')
    except Exception:
        import traceback
        traceback.print_exc()
        _pool_ready['err'] = traceback.format_exc(limit=3)


def _seed_check_rows():
    """開機種入隨映像焗入嘅 check_rows 回測結果（check_rows.csv，約 8-9 千行）。
    雲端免費機 CPU 限速，全庫回測要幾十分鐘；焗入 CSV 就即刻有完整 8 組合數據。
    之後仍可『開始統計』補上新賽事。種入失敗／冇檔案就交畀自動掃描。"""
    csv_path = os.path.join(BASE_DIR, 'check_rows.csv')
    if not os.path.exists(csv_path):
        return False
    try:
        conn = db()
        n = int(conn.execute('SELECT COUNT(*) FROM check_rows').fetchone()[0])
        if n >= 1000:
            conn.close()
            return True     # 已有數據（例如本機資料庫），唔使種
        import csv
        rows = []
        with open(csv_path, newline='', encoding='utf-8') as f:
            for r in csv.reader(f):
                if not r or r[0] == 'match_id':
                    continue
                rows.append((int(r[0]), r[1] or None, r[2] or None,
                             r[3] or None, r[4] or None))
        conn.executemany(
            'INSERT OR IGNORE INTO check_rows(match_id, direction, g14, g17, res) '
            'VALUES(?,?,?,?,?)', rows)
        conn.commit()
        conn.close()
        print(f'[check] 已種入 {len(rows)} 行回測結果', flush=True)
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        return False


# ============ 賽果補抓（開賽後 2.5 小時仍無賽果 → 自動重抓補上） ============
_result_catchup = {'running': False, 'last': None, 'fixed': 0, 'error': None}


def _result_catchup_once():
    """搵開賽超過 2.5 小時但仲未入賽果嘅場次，
    按所屬聯賽分組重抓現行賽季檔（save_season 會順便更新賽果）。
    每輪最多 20 個聯賽，其餘下輪再補，避免一次太重。"""
    conn = db()
    rows = conn.execute(
        'SELECT m.id, s.titan_id FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'WHERE m.home_score IS NULL '
        "AND m.kickoff <= datetime('now','localtime','-2 hours 30 minutes') "
        "AND m.kickoff >= datetime('now','localtime','-10 days') "
        'ORDER BY m.kickoff').fetchall()
    if not rows:
        conn.close()
        return 0
    by_lg = {}
    for mid, tid in rows:
        by_lg.setdefault(tid, []).append(mid)
    crawler, fetcher = get_crawler()
    conn = db()
    fixed = 0
    for i, (tid, mids) in enumerate(by_lg.items()):
        if i >= 20:
            break
        try:
            seasons = crawler.get_season_list(fetcher, tid)
            if not seasons:
                continue
            season_label = seasons[0]        # 只取現行賽季
            prefix = crawler.resolve_prefix(fetcher, tid)
            if not prefix:
                continue
            url = (f'https://zq.titan007.com/jsData/matchResult/'
                   f'{season_label}/{prefix}.js?version=1')
            text = fetcher.get(url)
            if not text or text.lstrip().startswith('<'):
                continue
            parsed = crawler.parse_season_js(text)
            crawler.save_season(conn, tid, season_label, 1, parsed)
            for mid in mids:
                if conn.execute('SELECT home_score FROM matches WHERE id=?',
                                (mid,)).fetchone()[0] is not None:
                    fixed += 1
        except Exception:
            crawler.log(conn, 'ERROR',
                        f'賽果補抓失敗 {tid}\n' + traceback.format_exc())
    conn.close()
    return fixed


def _result_catchup_job():
    """每 15 分鐘巡一次。賽果更新後，精選自動結算／我的選擇勝負讀取時自動跟上。"""
    while True:
        time.sleep(15 * 60)
        if _result_catchup['running']:
            continue
        _result_catchup['running'] = True
        try:
            _result_catchup['fixed'] = _result_catchup_once()
            _result_catchup['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
            _result_catchup['error'] = None
        except Exception:
            _result_catchup['error'] = traceback.format_exc()[-400:]
        finally:
            _result_catchup['running'] = False


def _auto_scans():
    """開機自動補數：featured 空咗（雲端重部署會清磁碟）就重掃精選；
    check_rows 太少（種入失敗）就自動開始全庫回測。唔阻塞服務。"""
    try:
        conn = db()
        n_feat = int(conn.execute('SELECT COUNT(*) FROM featured').fetchone()[0])
        n_chk = int(conn.execute('SELECT COUNT(*) FROM check_rows').fetchone()[0])
        conn.close()
        if n_feat == 0 and not _feat_scan['running']:
            print('[auto] featured 空白，開始重掃精選', flush=True)
            threading.Thread(target=_featured_scan_job, daemon=True).start()
        if n_chk < 1000 and not _check_scan['running']:
            print('[auto] check_rows 不足，開始全庫回測', flush=True)
            threading.Thread(target=_check_scan_job, daemon=True).start()
    except Exception:
        pass


class Server(ThreadingHTTPServer):
    """獨佔 7100 埠——如果已有舊伺服器在跑，即刻報錯退出，唔會兩個搶一個埠"""
    allow_reuse_address = False

    def server_bind(self):
        # SO_EXCLUSIVEADDRUSE 只係 Windows 先有
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == '__main__':
    # 雲端：數據庫放永久硬碟（/data）。首次啟動（硬碟全新）由映像焗入嘅種子複製一份
    if not os.path.exists(DB_PATH):
        seed = os.path.join(BASE_DIR, 'football.db')   # Dockerfile 焗入嘅快照
        if os.path.exists(seed) and os.path.abspath(seed) != os.path.abspath(DB_PATH):
            import shutil
            shutil.copy(seed, DB_PATH)
            print(f'[boot] 永久硬碟未見數據庫，已由映像種子複製到 {DB_PATH}', flush=True)
    port = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 7100))
    host = os.environ.get('HOST', '127.0.0.1')
    try:
        httpd = Server((host, port), Handler)
    except OSError:
        print(f'[錯誤] {port} 埠已被佔用——已有篩查APP伺服器在運行。')
        print('如要重開，請先用工作管理員結束舊嘅 python/app.py 進程。')
        sys.exit(1)
    print(f'篩查 APP v{SERVER_VERSION}：http://localhost:{port}')
    init_picks()
    _seed_check_rows()
    _auto_scans()
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=_result_catchup_job, daemon=True).start()
    # V2 排程：每日 12 時起每 2 小時自動更新未來 24 小時盤口直至尾盤（伺服器端，手機閂咗都行）
    threading.Thread(target=_v2_sched_job, daemon=True).start()
    # 開機自動更新：雲端每次重新部署會用返舊數據快照起機（數據過舊），
    # 偵測到數據 stale 就喺背景自動更新（賽果＋新場次＋補爬缺少嘅盤口），無需人手撳；
    # 本機數據新鮮就唔會白行一次
    def _boot_auto_update():
        try:
            import datetime as dt
            conn = db()
            # 用 crawler 每次寫入嘅 matches.updated_at 判斷快照新鮮度
            # （MAX(kickoff) 唔得——成季賽程一早入咗庫，舊快照都有「最近」嘅過去場次）
            row = conn.execute(
                'SELECT MAX(updated_at) FROM matches').fetchone()
            conn.close()
            latest = row[0] if row and row[0] else None
            stale = True
            if latest:
                try:
                    t = dt.datetime.strptime(latest, '%Y-%m-%d %H:%M:%S')
                    stale = (dt.datetime.now() - t).total_seconds() > 12 * 3600
                except Exception:
                    stale = True
            if stale:
                print('[boot] 數據快照過舊（%s），自動開始更新…' % latest, flush=True)
                do_update(auto=True)
            else:
                print('[boot] 數據新鮮（%s），跳過自動更新' % latest, flush=True)
        except Exception:
            import traceback
            traceback.print_exc()
    threading.Timer(5, _boot_auto_update).start()
    httpd.serve_forever()
