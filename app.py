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

import screen_engine

_fetch_lock = threading.Lock()
_last_fetch = {}          # match_id -> ts
_local = threading.local()
SERVER_VERSION = '2.2'
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
        "oc.handicap, oc.giver, oc.home_odds, oc.away_odds "
        "FROM matches m JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN teams ht ON ht.titan_id=m.home_id "
        "JOIN teams at ON at.titan_id=m.away_id "
        "LEFT JOIN odds_asian oc ON oc.match_id=m.id AND oc.company_id=12 "
        "AND oc.label='closing' "
        "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','localtime') "
        + cap + "ORDER BY m.kickoff", args).fetchall()
    out = []
    for mid, lg, ko, h, a, od, has, hc, gv, ho, ao in rows:
        line = None
        if hc is not None:
            line = {'line': screen_engine.fmt_line(hc, gv), 'ho': ho, 'ao': ao}
        out.append({'id': mid, 'league': lg, 'kickoff': ko, 'home': h, 'away': a,
                    'has_odds': bool(has), 'line': line,
                    'fetched_ago': int(time.time() - _last_fetch[mid]) if mid in _last_fetch else None})
    conn.close()
    return out


def do_fetch(mid, force=False):
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


# ============ 一鍵更新賽事（賽果＋新場次＋最近三日盤口） ============
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
    try:
        st = crawler.recent_update(
            conn, cfg, days=3,
            progress=lambda m: _update_state.update(phase=m))
        _update_state['last_result'] = st
        _update_state['error'] = None
        print(f"[update] 完成：{st}", flush=True)
    except Exception:
        import traceback
        traceback.print_exc()
        _update_state['error'] = traceback.format_exc(limit=3)
    finally:
        conn.close()
        _update_state['running'] = False
        _update_state['phase'] = ''
        _update_state['last_done'] = time.time()


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


def do_screen(mid, sel=None):
    conn = db()
    t = screen_engine.get_target(conn, mid)
    if not t:
        conn.close()
        return {'error': '找不到賽事'}
    res = screen_engine.screen(conn, t, sel=sel)
    conn.close()
    return res


# ============ 我的選擇（上/下盤 記錄 + 勝出率統計） ============

def init_picks():
    conn = db()
    conn.execute('''CREATE TABLE IF NOT EXISTS user_picks(
        match_id INTEGER PRIMARY KEY, choice TEXT NOT NULL, created REAL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS featured(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, result TEXT, settled_at TEXT)''')
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
    conn.execute('INSERT INTO user_picks(match_id, choice, created) VALUES(?,?,?) '
                 'ON CONFLICT(match_id) DO UPDATE SET choice=excluded.choice, '
                 'created=excluded.created', (mid, choice, time.time()))
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
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds '
        'FROM user_picks p '
        'JOIN matches m ON m.id=p.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'ORDER BY m.kickoff DESC').fetchall()
    out = []
    wins = losses = pushes = pending = 0
    for (mid, choice, ko, hs, aws, h, a, lg, hc, gv, ho, ao) in rows:
        rec = {'id': mid, 'choice': choice, 'kickoff': ko, 'home': h, 'away': a,
               'league': lg,
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


def get_picks_full():
    conn = db()
    rows = conn.execute(
        'SELECT p.match_id, p.choice, m.kickoff, m.home_score, m.away_score, '
        'ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds '
        'FROM user_picks p '
        'JOIN matches m ON m.id=p.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        # 未開賽全部保留；已開賽只保留 24 小時
        "WHERE m.home_score IS NULL "
        "OR m.kickoff >= datetime('now','localtime','-24 hours')").fetchall()
    conn.close()
    out = []
    for (mid, choice, ko, hs, aws, h, a, lg, hc, gv, ho, ao) in rows:
        rec = {'id': mid, 'choice': choice, 'kickoff': ko, 'home': h, 'away': a,
               'league': lg,
               'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
               'odds': f'主{ho}/客{ao}' if ho is not None else None,
               'played': hs is not None}
        if hs is not None:
            r = pick_result(hc, gv, hs, aws)
            rec['score'] = f'{hs}-{aws}'
            rec['result'] = 'X' if r is None else (
                'P' if r == 'P' else ('W' if (r == 'A') == (choice == 'up') else 'L'))
        rec['brief'] = _screen_brief(mid)
        out.append(rec)
    # 未開賽順時間排先；已開賽（24h內）跟後，最新嘅排最前
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

_feat_scan = {'running': False, 'done': 0, 'total': 0, 'added': 0,
              'last': None, 'error': None}


def _featured_direction(b):
    """七重準則：①⑤⑧ 全庫同方向≥50%；⑫⑮⑱ 全庫+同聯賽同方向≥50%。
    方向由第①項（全庫）揀：上盤率≥50%→'up'，否則下盤率≥50%→'down'，否則唔入選。
    通過全部返回 'up'/'down'，任何一項不達標返回 None。"""
    if not b:
        return None

    def rate(oc, d):
        if not oc:
            return None
        return oc.get('up_r') if d == 'up' else oc.get('down_r')

    a1 = (b.get('i1') or {}).get('all')
    if not a1:
        return None
    if (a1.get('up_r') or 0) >= 0.5:
        d = 'up'
    elif (a1.get('down_r') or 0) >= 0.5:
        d = 'down'
    else:
        return None
    # ①⑤⑧：全庫同方向 ≥50%
    for k in ('i1', 'i5', 'i8'):
        r = rate((b.get(k) or {}).get('all'), d)
        if r is None or r < 0.5:
            return None
    # ⑫：同尾盤盤口（全庫 + 同聯賽）
    i12 = b.get('i12') or {}
    for row in (i12.get('cur'), (i12.get('lg') or {}).get('cur')):
        r = rate(row, d)
        if r is None or r < 0.5:
            return None
    # ⑮：同尾盤＋今場水位區（全庫 + 同聯賽）
    i15 = b.get('i15') or {}
    for row in (i15.get('zone'), i15.get('lg_zone')):
        r = rate(row, d)
        if r is None or r < 0.5:
            return None
    # ⑱：排名差距淨值±1＋同尾盤（全庫 + 同聯賽）
    i18 = b.get('i18') or {}
    for row in (i18.get('all'), i18.get('lg')):
        r = rate(row, d)
        if r is None or r < 0.5:
            return None
    return d


def _featured_scan_job():
    global _feat_scan
    if _feat_scan['running']:
        return
    _feat_scan.update(running=True, done=0, added=0, error=None)
    conn = db()
    try:
        # 所有未開賽且有尾盤嘅場次
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM matches m "
            "JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 AND oc.handicap IS NOT NULL "
            "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','localtime') "
            "ORDER BY m.kickoff").fetchall()
        _feat_scan['total'] = len(rows)
        screen_engine.load_pool(conn)   # 預熱數據池
        added = 0
        for (mid,) in rows:
            try:
                d = _featured_direction(_screen_brief(mid))
                if d and not conn.execute(
                        'SELECT 1 FROM featured WHERE match_id=?', (mid,)).fetchone():
                    conn.execute(
                        'INSERT INTO featured(match_id, direction, added_at) '
                        'VALUES(?,?,?)',
                        (mid, d, time.strftime('%Y-%m-%d %H:%M:%S')))
                    conn.commit()
                    added += 1
            except Exception:
                pass
            _feat_scan['done'] += 1
        _feat_scan['added'] = added
        _feat_scan['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        _feat_scan['error'] = str(e)
    finally:
        conn.close()
        _feat_scan['running'] = False


def get_featured_full():
    """精選全量：未開賽順時間排先；已完場最新排先（永不刪除）。
    順便結算新完場場次（以入選方向計 贏/輸/走）。"""
    conn = db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    pending_rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.added_at, m.kickoff, '
        'm.home_score, m.away_score, ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds '
        'FROM featured f JOIN matches m ON m.id=f.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'WHERE m.home_score IS NULL ORDER BY m.kickoff').fetchall()
    played_rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.added_at, m.kickoff, '
        'm.home_score, m.away_score, ht.name_tc, at.name_tc, c.req_name, '
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds '
        'FROM featured f JOIN matches m ON m.id=f.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'WHERE m.home_score IS NOT NULL ORDER BY m.kickoff DESC').fetchall()
    conn.close()

    def build(row):
        (mid, d, res, added, ko, hs, aws, h, a, lg, hc, gv, ho, ao) = row
        rec = {'id': mid, 'direction': d, 'added_at': added, 'kickoff': ko,
               'home': h, 'away': a, 'league': lg,
               'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
               'odds': f'主{ho}/客{ao}' if ho is not None else None,
               'played': hs is not None}
        if hs is not None:
            r = pick_result(hc, gv, hs, aws)
            if res is None and r is not None:
                # 自動結算（以入選方向計）
                res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
                c2 = db()
                c2.execute('UPDATE featured SET result=?, settled_at=? WHERE match_id=?',
                           (res, now, mid))
                c2.commit()
                c2.close()
            rec['score'] = f'{hs}-{aws}'
            rec['result'] = res
        rec['brief'] = _screen_brief(mid)
        return rec

    pending = [build(r) for r in pending_rows]
    played = [build(r) for r in played_rows]
    wins = sum(1 for r in played if r.get('result') == 'W')
    losses = sum(1 for r in played if r.get('result') == 'L')
    pushes = sum(1 for r in played if r.get('result') == 'P')
    stats = {'total': len(pending) + len(played), 'pending': len(pending),
             'played': len(played), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': pending, 'played': played,
            'scan': {k: _feat_scan[k] for k in ('running', 'done', 'total', 'added', 'last', 'error')}}


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
            self._send(200, json.dumps(upcoming(hours), ensure_ascii=False))
            return
        if u.path == '/api/screen':
            q = parse_qs(u.query)
            mid = int(q.get('id', ['0'])[0])
            sel = None
            if q.get('sel'):
                try:
                    sel = [int(x) for x in q['sel'][0].split(',') if x.strip()]
                except ValueError:
                    sel = None
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
                self._send(200, json.dumps(get_featured_full(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/featured/scan-status':
            self._send(200, json.dumps(_feat_scan, ensure_ascii=False))
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
        if u.path == '/api/featured/scan':
            try:
                if not _feat_scan['running']:
                    threading.Thread(target=_featured_scan_job, daemon=True).start()
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


class Server(ThreadingHTTPServer):
    """獨佔 7100 埠——如果已有舊伺服器在跑，即刻報錯退出，唔會兩個搶一個埠"""
    allow_reuse_address = False

    def server_bind(self):
        # SO_EXCLUSIVEADDRUSE 只係 Windows 先有
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == '__main__':
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
    threading.Thread(target=_warmup, daemon=True).start()
    httpd.serve_forever()
