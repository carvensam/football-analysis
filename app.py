# -*- coding: utf-8 -*-
"""篩查 APP 本機伺服器（標準函式庫 + 工作區 crawler / screen_engine）
端點（V3 5.0.x；舊版 V1/V2 端點照舊保留喺「舊版本」分頁）：
  GET  /                          主頁（index.html）
  GET  /api/ready                 數據池就緒狀態＋版本號（頂部狀態列用）
  GET  /api/upcoming?hours=48     未來賽事清單＋已開賽（120h）
  POST /api/fetch  {id}           抓取該場最新賠率（titan007 易胜博，限流）
  GET  /api/screen?id=            舊版 V1 篩查（14 項＋第15項組合）
  POST /api/update                一鍵更新（賽果＋新場次＋盤口賠率全部刷新）
  POST /api/update-window         快掣：更新未來24／30／10 分鐘窗口
  GET  /api/v3/list|target|summary|item   V3 主頁／場次頭／45 項適用＋精選W／單項
  POST /api/v3/combo              V3 組合篩查（第 1–38 項多選 AND）
  GET  /api/v3/featured/full      精選W 清單（POST …/scan|refresh 掃描／單場重算）
  GET  /api/v3/featlog            精選W 過往紀錄（尾盤快照）
  GET  /api/fwgrid/full?g=7|8|12  精選7／8／12 清單（POST …/scan|refresh）
  GET  /api/fwcheck/full|target   Check 一下 7／8／12（8 情境回測表＋單場檢驗）
  GET  /api/ou_rules              大小規則（分聯賽 O/U 規則版，POST …/scan 掃描）
  GET  /api/htft_rules            半全場方法（分聯賽 HT/FT 方法版，POST …/scan 掃描）
  GET  /api/predstats?days=&lg=   預測結果統計（完場場次逐來源結算＋命中率）
  GET  /api/lgpred                聯賽預測標籤（>78% 規則）
  GET  /api/lgalerts              🔔 聯賽規則提示鐘
  GET  /api/hitrate?kind=&id=     命中率回查（fw7／fw8／fw12／fwx）
  GET  /api/8x/list|new           8X 版面（66 公式賽前記錄＋結算＋新預測輪詢）
  POST /api/8x/scan               8X 即時掃描＋結算
  仲有：/api/picks、/api/results、/api/featlog、/api/v2/*、/api/v1/*（V1/V2 舊頁）
"""
import json
import os
import re
import socket
import sqlite3
import threading
import time
import traceback
import datetime as dt
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
# engine/ 放尾（2026-10-24）：淨係令 crawl_extra 等 engine-only 模組喺
# APP 進程 import 得到；擺尾係刻意——crawler 等雙份模組繼續解析倉庫根
# 正本（ROOT crawler.py 係 crawl16/fleet 共用嘅正本，唔畀 engine 後備搶）。
_ENGINE_DIR = os.path.join(PARENT_DIR, 'engine')
if _ENGINE_DIR not in sys.path:
    sys.path.append(_ENGINE_DIR)

import numpy as np
import screen_engine
try:
    import fw8x
except ImportError:
    fw8x = None
try:
    import w88_engine
except ImportError:
    w88_engine = None
try:
    import newjx_engine
except ImportError:
    newjx_engine = None
from concurrent.futures import ThreadPoolExecutor

# 精選／我的選擇等列表逐場運算（screen＋check）係 CPU 密集——
# 單線程每場 5–8 秒，幾十場就超時（用戶投訴「精選無反應」嘅根因）。
# 用執行緒池並行起唄（sqlite 每連線獨立、load_pool 有鎖，並行安全）。
def _total_ram_gb():
    """實體記憶體總量（GB）——Render 512MB 實例同本機 16GB 要用唔同並行度"""
    try:
        if hasattr(os, 'sysconf'):
            return (os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
                    ) / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        import ctypes

        class _M(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

        m = _M()
        m.dwLength = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass
    return 8.0   # 探唔到就當中型機


_RAM_GB = _total_ram_gb()
# 低記憶體機（Render 512MB）：分析並行度壓到 1——6 個 worker 同時各自
# 配置中間陣列，峰值會同共享池疊加爆 RAM（2026-10-02 實測 OOM 元兇）；
# 本機 16GB 照舊最多 6 個
_BUILD_WORKERS = 1 if _RAM_GB < 2.5 else min(6, (os.cpu_count() or 4))
_BUILD_POOL = ThreadPoolExecutor(max_workers=_BUILD_WORKERS,
                                 thread_name_prefix='build')


_BUILD_GEN = {'n': 0}   # 2026-10-11 殭屍 build 剎車：每輪 map 遞增，過期任務即刻作廢


def _map_bounded(build, rows, budget_sec=20.0):
    """有界並行 build：保證整體喺 budget_sec 內返回。單個 worker 卡住（死循環、
    僵死 DB 連線）舊版 map 會無限等，連環請求全部阻塞——2026-10-08 V1/V2 舊頁
    「用唔到」元兇：一個卡死 build 霸住 build_lock，之後所有請求排隊到死。
    超時未完成嘅列回傳 None（調用方丟棄），下次重建（TTL 內快取照舊）再補。
    2026-10-11 補世代計：超時作廢嘅 future 舊版照舊霸住 BUILD_POOL 隊列燒 CPU
    （refresher 每 150 秒 4 impl × 2 段 × 20 秒预算，提交量遠超消化量，隊列
    無限堆積——95% CPU 被困 screen_engine，HTTP 請求餓死：feathourly 58s、
    htftboard 89s、v3/list 逾時 200s+）。而家每輪 map 遞增世代，worker 輪到
    先發現自己過期即回 None，隊列即刻排乾，CPU 還返畀 HTTP 線程。"""
    if not rows:
        return []
    import concurrent.futures as cf
    _BUILD_GEN['n'] += 1
    gen = _BUILD_GEN['n']

    def guarded(r):
        if gen != _BUILD_GEN['n']:
            return None
        return build(r)

    futs = {}
    for i, r in enumerate(rows):
        futs[_BUILD_POOL.submit(guarded, r)] = i
    done = {}
    try:
        for f in cf.as_completed(futs, timeout=budget_sec):
            done[futs[f]] = f.result()
    except cf.TimeoutError:
        pass
    return [done.get(i) for i in range(len(rows))]

# 全局掃描閘（2026-10-02 記憶體審計）：512MB/0.5C 實例同時間只行一個全庫掃描，
# 其餘自動跳過等下個觸發——精選/V3/Check/FWGrid 四個掃描以前可以疊住行，
# 並行嗰陣 CPU+RAM 雙峰值係 OOM 誘因之一
_scan_gate = {'busy': False}

# 重型 GET 並發閘（2026-10-06 記憶體審計實測：12 並發重型請求峰值 +233MB RSS，
# 512MB 雲端（baseline ~300MB）一撞就 OOM——Render 事件「Ran out of memory
# used over 512MB」一日四單。低記憶體機同時間最多 2 個重型請求，其餘排隊
# 最多 45 秒，排唔到回 429 叫前端重試；輕端點（health/靜態/status）唔過閘。
# 2026-10-15 第二輪 debug：512MB 實例並發閘 2→1（兩個重型請求同時 build 池
# 嘅瞬間峰值先係 OOM 元兇），再加 RSS 軟上限——過線即刻 429 卸載，唔等佢撞爆
# 512MB 俾 Render 成個容器殺咗（每次 OOM = 全體 502 十幾分鐘，好過逐個 429）。
_HEAVY_GET_SEM = threading.Semaphore(
    1 if _RAM_GB < 1.0 else (2 if _RAM_GB < 2.5 else 6))
_HEAVY_GET_TIMEOUT = 45

# 2026-10-26 重型 GET 回應快取（用戶「太慢」優化 #2）：30s TTL＋數據事件即清。
# 白名單制——淨限冇副作用嘅列表 API；settle/入選/剔走/重算完會
# _bump_data_epoch() 全部作廢，新鮮度同「完場即更新」規格冇衝突（事件驅動，
# 唔係純 TTL）。NOTE：呢度取代咗「結算即物化」建議——因為報表類計算本身
# 已經係快（13 工具全跑 ~3s），真正 heavy 係 v3/list 同 featured 頁嘅
# 逐場推論；事件驅動快取達到同一效果但冇多一張表要同步。
_GET_CACHE = {'epochs': {'tools': 0, 'report': 0}, 'data': {},
              'lock': threading.Lock()}
_GET_CACHE_TTL = 30.0
_GET_CACHE_MAX = 300
# 路由→標籤：'report'（league_report）只受結算影響——入選/剔走寫嘅係
# result NULL 嘅 pending 列，報表淨計已結算，唔使跟住 bust；其餘工具頁
# API 受入選/剔走/轉盤影響行 'tools'。
_GET_CACHE_ROUTES = {
    '/api/v3/list': 'tools', '/api/8x/list': 'tools',
    '/api/v3/featured/full': 'tools', '/api/v1/featured/full': 'tools',
    '/api/fwgrid/full': 'tools', '/api/league_report': 'report',
}


def _bump_data_epoch(tag=None):
    """數據有實質變動——作廢指定標籤（None＝兩個都）。settle 用 None，
    入選/剔走/轉盤用 'tools'（report 唔受影響，快取先有用——2026-10-26
    首版單一 epoch，繁忙期每幾秒 bust 一次，快取形同虛設實測捉到）。"""
    try:
        with _GET_CACHE['lock']:
            if tag is None:
                for k in _GET_CACHE['epochs']:
                    _GET_CACHE['epochs'][k] += 1
            else:
                _GET_CACHE['epochs'][tag] =                     _GET_CACHE['epochs'].get(tag, 0) + 1
            # 唔好 clear data——命中檢查用 epoch 比對自動作廢舊條目；
            # 舊制 bump 就清晒，繁忙期 tools bump 每幾秒一次，report
            # 快取連環被誤殺（2026-10-26 實測 3/3 miss 捉到）。條目
            # 靠 TTL 檢查＋MAX 上限自然淘汰。
    except Exception:
        pass


def _rss_mb():
    """目前進程 RSS（MB）；Linux 讀 /proc/self/status，其他平台返 0（閘唔生效）。"""
    try:
        with open('/proc/self/status', encoding='ascii') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


_MEM_GUARD = _RAM_GB < 2.5          # 低記憶體機（Render 512MB）先啟用
_MEM_SOFT_MB = 430.0                 # 超過：重型 GET 直接 429，畀在途請求留 80MB 緩衝
_HEAVY_GET_PREFIXES = (
    '/api/hb/predict', '/api/featured', '/api/v1/featured',
    '/api/fw8s',
    '/api/v2/', '/api/v3/', '/api/check', '/api/fwcheck', '/api/fwgrid',
    '/api/ou_rules',
    '/api/htft_rules',
    '/api/predstats',
    '/api/results', '/api/htft', '/api/picks', '/api/screen',
    '/api/upcoming', '/api/lgpred', '/api/oupred', '/api/twin',
)


def _scan_thread(fn, *args):
    def run():
        if _scan_gate['busy']:
            print(f'[scan] {fn.__name__}{args} 跳過：另一個掃描進行中', flush=True)
            return
        _scan_gate['busy'] = True
        try:
            fn(*args)
        finally:
            _scan_gate['busy'] = False
    threading.Thread(target=run, daemon=True).start()

_fetch_lock = threading.Lock()
_last_fetch = {}          # match_id -> ts
_local = threading.local()
SERVER_VERSION = '5.6.8'

# ---------------------------------------------------------------- 爬取控制
# 「停止／繼續爬」掣（2026-10-12 用戶指示：每個版面都要有位停止掣）：
# 寫入倉庫根 control.json，crawl_extra 每個任務前都會 read_control——
# cmd=stop 時工人完成手頭任務即退出（唔再派新嘢）；cmd=run 由本處重開工人。
CONTROL_PATH = os.path.join(PARENT_DIR, 'control.json')
_CRAWL_WORKERS = {   # 線道編號 → 啟動參數（同 auto_doctor 一致）
    'w1': '--worker 1',
    'w2': '--worker 2 --market ou',
    'w3': '--worker 3',
    'w7': '--worker 7',
    'w8': '--worker 8',
    'w10': '--worker 10 --market hkjc_ou',
}
_PYW = r'C:\Users\user\AppData\Local\Python\bin\pythonw.exe'


def _read_crawl_control():
    try:
        with open(CONTROL_PATH, encoding='utf-8') as fp:
            return json.load(fp).get('cmd', 'run')
    except (OSError, ValueError):
        return 'run'


def _write_crawl_control(cmd):
    tmp = CONTROL_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump({'cmd': cmd, 'ts': time.time()}, fp)
    os.replace(tmp, CONTROL_PATH)


def _crawl_workers_alive():
    """由鎖檔數 live 工人（同 crawl_extra.acquire_lock 機制一致）。"""
    import ctypes
    alive = []
    for wkey in _CRAWL_WORKERS:
        lock = os.path.join(PARENT_DIR, 'engine',
                            'crawl_extra.lock.main.' + wkey)
        try:
            pid = int(open(lock).read().strip())
        except (OSError, ValueError):
            pid = 0
        ok = False
        if pid:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                ok = True
        if ok:
            alive.append(wkey)
    return alive


def _spawn_crawl_worker(args):
    import subprocess
    exe = _PYW if os.path.exists(_PYW) else sys.executable
    try:
        subprocess.Popen('"%s" "%s" %s' % (exe, os.path.join(
            PARENT_DIR, 'engine', 'crawl_extra.py'), args),
            cwd=PARENT_DIR, shell=True,
            creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        pass   # 雲端（Render Linux）冇本機 crawl_extra——spawn 失敗都照寫 run


# ---------- 爬蟲控制中繼（2026-10-15）：APP 接雲端，但真正爬蟲喺本機 ----------
# 問題：Render 上 _spawn_crawl_worker 路徑錯（PARENT_DIR=/）＋ 0.5C 根本唔應該爬，
# 撳「▶ 繼續爬」launch 完即刻死，用戶以為掣壞。修正：雲端只做中繼——
# stop/run 寫入待取指令，本機 crawl_relay.py（計劃任務每分鐘）攞走並套用，
# 再將本機真實狀態推返上嚟；state 優先顯示本機嘅。
_CRAWL_RELAY = {'cmd': None, 'ts': 0.0}       # 雲端待本機攞嘅指令（一次性）
_PC_CRAWL_STATE = {'data': None, 'ts': 0.0}   # 本機推上嚟嘅真實狀態
_PC_STATE_MAX_AGE = 150.0                      # 狀態幾秒內算新鮮


def _cloud_relay():
    """雲端（DISABLE_UPDATE）行中繼邏輯；本機照舊直接控制。"""
    return DISABLE_UPDATE


def api_crawl_state():
    # 本機狀態新鮮 → 如實顯示本機爬蟲（APP 睇到嘅就係真正嗰部）
    pc = _PC_CRAWL_STATE
    if pc['data'] is not None and time.time() - pc['ts'] < _PC_STATE_MAX_AGE:
        d = dict(pc['data'])
        d['via'] = '本機'
        d['pc_age'] = int(time.time() - pc['ts'])
        return d
    alive = _crawl_workers_alive()
    d = {'cmd': _read_crawl_control(),
         'workers_alive': len(alive), 'workers_total': len(_CRAWL_WORKERS),
         'workers': alive}
    if _cloud_relay():
        d['via'] = '雲端（本機離線或未回報）'
    return d


def do_crawl_stop():
    _write_crawl_control('stop')
    # 2026-10-25 修正（用戶：「停止掣又係廢」）：舊制淨停爬蟲 fleet——掃描同
    # 「更新賠率」批量郁緊時撳掣完全冇反應。而家三線全停：掃描／批量會喺完
    # 成手頭嗰場後退出（旗標由各自循環每場檢查）。
    _unified_scan['stop'] = True
    _fbatch_state['stop'] = True
    _update_state['stop'] = True
    _win_state['stop'] = True
    if _cloud_relay():
        _CRAWL_RELAY['cmd'] = 'stop'
        _CRAWL_RELAY['ts'] = time.time()
        return {'ok': True, 'cmd': 'stop', 'relay': True,
                'note': '已通知本機停止；本機完成手頭任務後陸續退出（約 1 分鐘內生效）'}
    return {'ok': True, 'cmd': 'stop',
            'note': '已停止派新任務；手頭任務完成後工人會陸續退出'}


def do_crawl_run():
    _write_crawl_control('run')
    if _cloud_relay():
        _CRAWL_RELAY['cmd'] = 'run'
        _CRAWL_RELAY['ts'] = time.time()
        return {'ok': True, 'cmd': 'run', 'relay': True,
                'note': '已通知本機開爬；約 1 分鐘內本機工人會陸續復工'}
    alive = set(_crawl_workers_alive())
    launched = []
    for wkey, args in _CRAWL_WORKERS.items():
        if wkey in alive:
            continue
        _spawn_crawl_worker(args)
        launched.append(wkey)
    return {'ok': True, 'cmd': 'run', 'launched': launched,
            'already_running': sorted(alive)}


def api_crawl_relay_fetch():
    """本機輪詢攞指令（一次性：攞完即清，避免重複套用）。"""
    cmd = _CRAWL_RELAY['cmd']
    _CRAWL_RELAY['cmd'] = None
    return {'ok': True, 'cmd': cmd, 'ts': _CRAWL_RELAY['ts']}


def api_crawl_relay_state(body):
    """本機推送真實爬蟲狀態上雲端。"""
    try:
        _PC_CRAWL_STATE['data'] = {
            'cmd': str(body.get('cmd'))[:10],
            'workers_alive': int(body.get('workers_alive') or 0),
            'workers_total': int(body.get('workers_total') or 0),
            'workers': [str(w)[:10] for w in (body.get('workers') or [])][:10],
        }
        if body.get('err'):
            _PC_CRAWL_STATE['data']['err'] = str(body.get('err'))[:200]
        _PC_CRAWL_STATE['ts'] = time.time()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def api_probe_hosts():
    """探測 titan007 各數據主機喺本機網絡通唔通（2026-10-12 分工架構）：
    邊個節點（本機／football-analysis 雲端／balltrend-api）通邊啲主機，
    邊啲市場就派俾邊個爬。404 都算通（到達伺服器）。"""
    import time as _t
    from curl_cffi import requests as creq
    targets = {
        'zq': 'https://zq.titan007.com/jsData/LeagueSeason/sea178.js',
        'vip': ('https://vip.titan007.com/changeDetail/handicap.aspx'
                '?id=1&companyid=12'),
        'bf': 'https://bf.titan007.com/',
        'live': 'https://live.titan007.com/',
        '1x2d': 'https://1x2d.titan007.com/3077673.js',
    }
    out = {}
    for name, url in targets.items():
        t0 = _t.time()
        try:
            r = creq.get(url, timeout=8, impersonate='chrome')
            out[name] = {'ok': r.status_code in (200, 404),
                         'status': r.status_code, 'bytes': len(r.content),
                         'ms': int((_t.time() - t0) * 1000)}
        except Exception as e:                        # noqa: BLE001
            out[name] = {'ok': False, 'error': str(e)[:120],
                         'ms': int((_t.time() - t0) * 1000)}
    return {'ts': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'node': os.environ.get('RENDER_SERVICE_NAME', 'local'),
            'hosts': out}


_EXPORT_TABLES = ('odds_asian', 'odds_ou', 'odds_1x2_all', 'odds_corner',
                  'hkjc_asian', 'hkjc_ou', 'match_goals', 'match_corners')


def api_export_odds(after_rowid=0, limit=5000):
    """本節點爬到的賠率數據倒出——其他節點（本機電腦）pull 返去 merge，
    令手機 API 同本機 DB 兩邊都有最新數據。
    2026-10-12 修訂：增量改用 rowid（= 寫入順序＝爬取順序）。舊版用
    change_time 做游標係錯嘅——歷史場次今日先爬到，change_time 係幾年前，
    永遠過唔到 since 過濾，本機收唔到雲端新爬嘅舊賽事。"""
    import sqlite3 as _sq
    dbp = os.environ.get('DB_PATH') or os.path.join(PARENT_DIR, 'football.db')
    out = {'after_rowid': after_rowid, 'node': os.environ.get(
               'RENDER_SERVICE_NAME', 'local'), 'tables': {}}
    con = _sq.connect(dbp, timeout=15)
    try:
        for tbl in _EXPORT_TABLES:
            try:
                cols = [r[1] for r in con.execute(
                    'PRAGMA table_info(%s)' % tbl)]
            except _sq.Error:
                continue
            if not cols:
                continue
            rows = con.execute(
                'SELECT rowid, * FROM %s WHERE rowid > ? '
                'ORDER BY rowid LIMIT %d' % (tbl, limit),
                (after_rowid,)).fetchall()
            if rows:
                max_rid = max(r[0] for r in rows)
                out['tables'][tbl] = {
                    'cols': cols,
                    'rows': [r[1:] for r in rows],   # 剝走 rowid
                    'max_rowid': max_rid}
    finally:
        con.close()
    return out
_started = time.time()
_pool_ready = {'done': False, 'err': None}


def get_crawler():
    """每個執行緒建立自己的 Fetcher（sqlite 連線不可跨執行緒）"""
    if not hasattr(_local, 'crawler'):
        import crawler
        # config.json 跟 crawler.py 同一目錄（本機=工作區根；雲端=/app）
        with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        # APP 即場抓取：限流跟 config.json 分層休息方案（2026-09-29），
        # 只將被封重試次數調低，令失敗快閃
        cfg['blocked_retry_times'] = 1
        conn = sqlite3.connect(DB_PATH)
        _local.fetcher = crawler.Fetcher(conn, cfg)
        _local.crawler = crawler
    return _local.crawler, _local.fetcher


def db():
    # timeout=60：賽果補抓等長寫入交易會鎖庫分鐘級——舊預設 5 秒令並行請求
    # 即刻「database is locked」（2026-10-06 雲端 /api/fetch 實測）
    return sqlite3.connect(DB_PATH, timeout=60)


def _scan_with_retry(scan_fn, settle_fn, tries=5, wait=8):
    """同步版：鎖錯誤短等重試（內部用）。"""
    last = None
    for _ in range(tries):
        c = db()
        try:
            a = scan_fn(c)
            s = settle_fn(c)
            c.close()
            return a, s
        except sqlite3.OperationalError as e:
            last = e
            try:
                c.close()
            except Exception:
                pass
            if 'locked' not in str(e):
                raise
            time.sleep(wait)
    raise last


_bg_scan_state = {}
_bg_scan_info = {}   # 2026-10-22：最近一次背景掃描結果（/api/scan-status?kind= 用）


def _bg_scan_public(kind):
    """/api/scan-status 讀取：running＋最近一次 added/settled/錯誤/時間。
    2026-10-22：running 兼容統一掃描（newjx/w88 掣行統一 job，進度寫入
    _bg_scan_info，running 要跟 _unified_scan 走）。"""
    t = _bg_scan_state.get(kind)
    info = dict(_bg_scan_info.get(kind) or {})
    info['running'] = bool((t and t.is_alive())
                           or _unified_scan['running'])
    return info


def _start_bg_scan(kind):
    """一鍵掃描改背景作業（2026-10-19 用戶報告撳完無反應）：
    爬蟲間中長時間攞住寫鎖（實測 >160 秒連續鎖），同步等會令按鈕
    好似死咗。依家 API 即刻回，背景線程每 15 秒等空窗重試（最多 10 分鐘），
    寫入動作本身已改兩段式短事務，一有空窗即入。已在跑就唔重複開。
    2026-10-22：過程寫入 _bg_scan_info——前端可以 5 秒輪詢真實狀態，
    唔再係盲等 12 秒。"""
    t = _bg_scan_state.get(kind)
    if t and t.is_alive():
        return False

    def job():
        _bg_scan_info[kind] = {
            'started_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        for att in range(40):
            c = db()
            try:
                if kind == 'w88':
                    a = w88_engine.scan(c)
                    s = w88_engine.settle(c)
                    a8 = fw8x.scan(c) if fw8x else 0
                    s8 = fw8x.settle(c) if fw8x else 0
                    print('[bg-scan] 88W +%s settle %s｜8X +%s settle %s'
                          % (a, s, a8, s8), flush=True)
                    _bg_scan_info[kind].update(
                        {'added': a, 'settled': s, 'added_8x': a8,
                         'settled_8x': s8, 'error': None})
                else:
                    a = newjx_engine.scan(c)
                    s = newjx_engine.settle(c)
                    print('[bg-scan] 新精選 +%s settle %s' % (a, s), flush=True)
                    _bg_scan_info[kind].update(
                        {'added': a, 'settled': s, 'error': None})
                c.close()
                return
            except sqlite3.OperationalError as e:
                try:
                    c.close()
                except Exception:
                    pass
                if 'locked' not in str(e):
                    traceback.print_exc()
                    _bg_scan_info[kind]['error'] = str(e)[:400]
                    return
                _bg_scan_info[kind]['error'] = (
                    '資料庫忙緊，等緊寫入空窗（第 %d/40 次重試）' % (att + 1))
                time.sleep(15)
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
                traceback.print_exc()
                _bg_scan_info[kind]['error'] = traceback.format_exc(limit=2)[-400:]
                return
        _bg_scan_info[kind]['error'] = (
            '等咗 10 分鐘都入唔到寫鎖，今次放棄（20 分鐘背景循環會繼續補）')
        print('[bg-scan] %s 等咗 10 分鐘都入唔到寫鎖，今次放棄'
              '（20 分鐘背景循環會繼續補）' % kind, flush=True)

    t = threading.Thread(target=job, daemon=True)
    _bg_scan_state[kind] = t
    t.start()
    return True


def _migrate_db():
    """開機遷移：odds_asian 加 fetched_at（每場賠率/盤口「最後更新」顯示用）。
    同 crawler.connect() 內嘅遷移一致；喺度再做一次係保證 APP 查詢前欄位必存在
    （例如雲端 DB 係舊快照種子，未行過 crawler 遷移）。"""
    conn = sqlite3.connect(DB_PATH, timeout=120)
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(odds_asian)')]
        if 'fetched_at' not in cols:
            conn.execute("ALTER TABLE odds_asian ADD COLUMN fetched_at TEXT")
            conn.execute("UPDATE odds_asian SET fetched_at = (SELECT updated_at FROM "
                         "matches WHERE matches.id = odds_asian.match_id) "
                         "WHERE fetched_at IS NULL")
            conn.commit()
            print('[boot] odds_asian 已升級：新增 fetched_at（賠率最後更新時間）',
                  flush=True)
        # 2026-10-12：孖生查詢/盤口統計用——label+handicap 索引，冇佢就要成表掃描
        conn.execute('CREATE INDEX IF NOT EXISTS idx_oa_label_hc '
                     'ON odds_asian(label, company_id, handicap, giver)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ou_label_line '
                     'ON odds_ou(label, company_id, total_line)')
        # 2026-10-15：matches 冇 kickoff 索引——/api/upcoming、/api/htftboard
        # 呢啲時間窗查詢（ORDER BY kickoff LIMIT）每次都要全表掃 10 萬行；
        # 爬蟲重寫期間磁盤繁忙，主頁列表同半全場板就係咁樣等到 timeout。
        # 一勞永逸：kickoff 索引（一次性建立，秒級完成）。
        conn.execute('CREATE INDEX IF NOT EXISTS idx_matches_kickoff '
                     'ON matches(kickoff)')
        # 2026-10-20：odds_asian/odds_ou 上嘅變動記錄 trigger（跟種子庫嚟）會寫
        # 呢兩張 history 表——雲端舊庫冇呢兩張表，/api/admin/ingest 一 replace
        # odds_asian 就炸「no such table: main.odds_asian_history」成盤 rollback。
        # 開機補建（schema 同本機 master 一致）。
        conn.execute('''CREATE TABLE IF NOT EXISTS odds_asian_history (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER, label TEXT, company_id INTEGER,
            handicap REAL, home_odds REAL, away_odds REAL,
            change_time TEXT, giver TEXT, fetched_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS odds_ou_history (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER, label TEXT, company_id INTEGER,
            total_line REAL, over_odds REAL, under_odds REAL,
            change_time TEXT)''')
        # 2026-10-24：結算準則大修——featured×5＋fw_grid_featured 記落注盤
        # （hc REAL/giver TEXT）。雲端舊庫冇呢兩欄，本機 delta 同步 schema
        # 唔夾會 skip 晒精選表（手機冇新精選）。開機冇欄即補，補完同步恢復。
        for t in ('featured', 'featured_z', 'v1_featured', 'v1_featured_z',
                  'v3_featured', 'fw_grid_featured'):
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
            if not cols:
                continue  # 舊庫冇呢張表——_apply_delta 會照源 schema 即場起
            if 'hc' not in cols:
                conn.execute(f'ALTER TABLE {t} ADD COLUMN hc REAL')
            if 'giver' not in cols:
                conn.execute(f'ALTER TABLE {t} ADD COLUMN giver TEXT')
        conn.commit()
    finally:
        conn.close()


def upcoming(hours=48):
    conn = db()
    # hours=0 → 不設時限上限，列出全部即將開賽賽事
    cap = "AND m.kickoff <= datetime('now','+8 hours', ?) " if hours else ""
    args = (f'+{hours} hours',) if hours else ()
    rows = conn.execute(
        "SELECT m.id, c.req_name, m.kickoff, ht.name_tc, at.name_tc, m.odds_done, "
        "EXISTS(SELECT 1 FROM odds_asian oa WHERE oa.match_id=m.id AND oa.company_id=12), "
        "oc.handicap, oc.giver, oc.home_odds, oc.away_odds, "
        "oc3.handicap, oc3.giver, oc3.home_odds, oc3.away_odds, "
        "COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank), "
        "(SELECT MAX(oa2.fetched_at) FROM odds_asian oa2 WHERE oa2.match_id=m.id) "
        "FROM matches m JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN teams ht ON ht.titan_id=m.home_id "
        "JOIN teams at ON at.titan_id=m.away_id "
        "LEFT JOIN odds_asian oc ON oc.match_id=m.id AND oc.company_id=12 "
        "AND oc.label='closing' "
        # Crown(3) 後備：易胜博未開盤嘅場次照顯示盤口（標明來源）
        "LEFT JOIN odds_asian oc3 ON oc3.match_id=m.id AND oc3.company_id=3 "
        "AND oc3.label='closing' "
        "LEFT JOIN match_prestandings ps ON ps.match_id=m.id "
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','+8 hours') "
        + cap + "ORDER BY m.kickoff", args).fetchall()
    out = []
    for mid, lg, ko, h, a, od, has, hc, gv, ho, ao, c3h, c3g, c3ho, c3ao, hr_, ar_, odds_at in rows:
        line = None
        if hc is not None:
            line = {'line': screen_engine.fmt_line(hc, gv), 'ho': ho, 'ao': ao,
                    'giver': gv or 'none',
                    'v': (-hc if gv == 'home' else (0.0 if not gv else hc))}
        elif c3h is not None:
            line = {'line': screen_engine.fmt_line(c3h, c3g), 'ho': c3ho, 'ao': c3ao,
                    'giver': c3g or 'none', 'src': 'crown',
                    'v': (-c3h if c3g == 'home' else (0.0 if not c3g else c3h))}
        out.append({'id': mid, 'league': lg, 'kickoff': ko, 'home': h, 'away': a,
                    'has_odds': bool(has), 'line': line, 'odds_at': odds_at,
                    'rank_home': hr_, 'rank_away': ar_,
                    'fetched_ago': int(time.time() - _last_fetch[mid]) if mid in _last_fetch else None})
    # 同賽事盤口規則：每場附上/下盤%預測（2026-10-05 用戶指示）
    try:
        _attach_oupred(out)
    except Exception:
        pass
    # 10-16 用戶：主頁三欄——精選8／精選8s／好波大細
    try:
        _attach_preds3(out)
    except Exception:
        pass
    conn.close()
    return out


def played(fin_hours=120, fin_cat='all'):
    """已開賽場次（預設最近 120 小時），主頁「已開賽／已完賽」區用。
    2026-10-11 用戶規格：已完賽回查——Period（fin_hours，最遠 720 小時）
    同聯賽杯賽類（fin_cat=聯賽/杯賽）二選一或二選二；輸出連 cat 同
    大小球尾盤（之前已完賽部分得亞盤，資料唔齊全）。"""
    fin_hours = max(1, min(int(fin_hours or 120), 720))
    cat_sql, cat_arg = '', ()
    if fin_cat in ('聯賽', '杯賽'):
        cat_sql, cat_arg = ' AND c.category = ?', (fin_cat,)
    limit = 1200 if fin_hours <= 120 else 3000
    conn = db()
    rows = conn.execute(
        "SELECT m.id, c.req_name, c.category, m.kickoff, ht.name_tc, at.name_tc, "
        "m.home_score, m.away_score, m.half_home, m.half_away, "
        "(SELECT g.side FROM match_goals g WHERE g.match_id=m.id "
        "ORDER BY g.seq LIMIT 1) AS fg, "
        "oc.handicap, oc.giver, oc.home_odds, oc.away_odds, "
        "oc3.handicap, oc3.giver, oc3.home_odds, oc3.away_odds, "
        "ou.total_line, ou.over_odds, ou.under_odds, "
        "(SELECT MAX(oa2.fetched_at) FROM odds_asian oa2 WHERE oa2.match_id=m.id) "
        "FROM matches m JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN teams ht ON ht.titan_id=m.home_id "
        "JOIN teams at ON at.titan_id=m.away_id "
        "LEFT JOIN odds_asian oc ON oc.match_id=m.id AND oc.company_id=12 "
        "AND oc.label='closing' "
        # 易勝博無資料 → Crown 後備照出（同即將開賽區玩法；前端已有 Crown 標籤）
        "LEFT JOIN odds_asian oc3 ON oc3.match_id=m.id AND oc3.company_id=3 "
        "AND oc3.label='closing' "
        "LEFT JOIN odds_ou ou ON ou.match_id=m.id AND ou.company_id=12 "
        "AND ou.label='closing' "
        "WHERE m.kickoff < datetime('now','+8 hours') "
        "AND m.kickoff >= datetime('now','+8 hours','-%d hours') " % fin_hours
        + cat_sql +
        " ORDER BY m.kickoff DESC LIMIT %d" % limit, cat_arg).fetchall()
    out = []
    for (mid, lg, cat, ko, h, a, hs, aws, hh, ha, fg, hc, gv, ho, ao,
         c3h, c3g, c3ho, c3ao, oul, ouo, ouu, odds_at) in rows:
        line = None
        if hc is not None:
            line = {'line': screen_engine.fmt_line(hc, gv), 'ho': ho, 'ao': ao,
                    'v': (-hc if gv == 'home' else (0.0 if not gv else hc))}
        elif c3h is not None:
            line = {'line': screen_engine.fmt_line(c3h, c3g), 'ho': c3ho,
                    'ao': c3ao, 'src': 'crown',
                    'v': (-c3h if c3g == 'home' else (0.0 if not c3g else c3h))}
        rec = {'id': mid, 'league': lg, 'cat': cat, 'kickoff': ko,
               'home': h, 'away': a,
               'score': None if hs is None else f'{hs}-{aws}', 'line': line,
               'odds_at': odds_at}
        if oul is not None:
            rec['ou'] = {'line': oul, 'over': ouo, 'under': ouu}
        if hh is not None:
            rec['ht'] = f'{hh}-{ha}'
        if hs is not None and fg in ('home', 'away'):
            rec['first_goal'] = fg
        out.append(rec)
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
        before = _closing_now(conn, mid)
        ok = crawler.crawl_odds_for_match(conn, fetcher, mid, row[0], 12)
        conn.close()
        # 失敗唔緩存（2026-09-29）：冷卻期內被拒絕嘅請求唔記錄時間，
        # 下次撳掣即刻重試，唔會俾 120 秒快取擋住
        if ok:
            _last_fetch[mid] = time.time()
            # 2026-10-25：after_odds_update 已刪除——0.15 門檻後備路徑淨係會
            # skip；watcher（notify→20s）已無條件覆蓋每次成功爬取，冇存在價值。
            return {'ok': True}
        err = (getattr(fetcher, 'last_error', None)
               or 'titan007 無回應（數據伺服器可能暫時中斷，已排定自動重試）')
        return {'ok': False, 'error': err}


# ============ 2026-10-20 尾盤變動 → 該場精選資格自動重檢 ============
# 背景：用戶 10-20 投訴「尾盤無更新到，合唔合乎資格都有待商榷」——雲端（Render）
# 凌晨 00–12 點冇排程爬蟲，尾盤停舊；就算重抓咗新尾盤，精選資格都仲係用舊線
# 判定（do_fetch 從來唔觸發重算，只有 do_update 會）。呢組函數喺任何渠道寫入
# 新盤口之後，若尾盤有重大變動（讓球±0.25／讓球方轉向／水位±0.15），背景線程
# 用新線重算該場所有未結算精選資格：仍合 → 更新 detail＋快照；唔合 → 移出。
_LINE_MOVE_H = 0.25   # 讓球變動門檻（淺/深一檔）
_LINE_MOVE_W = 0.15   # 水位變動門檻（2026-10-25 起只用喺批量「變盤場次」
                      # 統計；重算本身已改為無條件）


# 2026-10-25 有限並發重檢池：line-move watcher 舊制每場開一條線程（一批
# 40 場＝40 條 full eval 同時搶 RAM/數據池，部機即刻窒息，掃描亦被拖慢）；
# 舊制 watcher/批量通道每場開一條線程。而家共用細池排隊：本機 2 線、雲端 1。
_REVAL_POOL = None


def _reval_pool():
    global _REVAL_POOL
    if _REVAL_POOL is None:
        import concurrent.futures as _cf
        _REVAL_POOL = _cf.ThreadPoolExecutor(
            max_workers=1 if DISABLE_UPDATE else 2,
            thread_name_prefix='reval')
    return _REVAL_POOL


def _reval_submit(mid, tag='line-move'):
    """排隊做單場資格重檢（冇任何節流/讓路——任何變動即時入隊）。
    2026-10-25 加埋好波：OU 線轉都屬「盤口轉/賠率轉」——12 工具重檢完順手
    重算好波（api_hb_predict 自帶賠率 signature 快取＋淨係規則命中先記低，
    唔會污染 hb_pick_log；兩邊都做（雲端 2GB 夠行））。"""
    def _job():
        # 2026-10-25 讓路邏輯全拆：舊制掃描行緊就排隊等（最多 30 分鐘）——
        # 水位變動喺掃描期間會被拖後，違反「任何變動即時重算」。2 線池本身
        # 就係並發調節器，同掃描短暫重疊係正確性應有嘅代價（舊制 53s/場係
        # 每場一 thread 時代嘅問題，同池化後已唔存在）。
        for _att in range(3):
            try:
                conn = db()
                try:
                    acts = _pick_revalidate(conn, mid)
                    conn.commit()
                finally:
                    conn.close()
                if acts:
                    print('[%s] 場次%s 轉盤→重算：%s'
                          % (tag, mid, ','.join(acts)), flush=True)
                # 2026-10-25：好波重算兩邊都做——HB 池僅 14MB（雲端 2GB
                # 預熱本來就建），signature 快取令冇變嘅場微秒過；唔再做
                # 本地限定（舊 512MB 思維）。
                try:
                    conn = db()
                    try:
                        has_ou = conn.execute(
                            "SELECT 1 FROM odds_ou WHERE match_id=? "
                            "AND label='closing' LIMIT 1", (mid,)).fetchone()
                    finally:
                        conn.close()
                    if has_ou:
                        api_hb_predict(mid)   # 快取命中＝微秒；miss 先計
                except Exception:
                    pass
                return
            except sqlite3.OperationalError:
                time.sleep(4)
            except Exception:
                traceback.print_exc(limit=2)
                return
    try:
        _reval_pool().submit(_job)
    except RuntimeError:
        pass


def _closing_now(conn, mid):
    return conn.execute(
        "SELECT handicap, giver, home_odds, away_odds FROM odds_asian "
        "WHERE match_id=? AND label='closing' AND company_id=12",
        (mid,)).fetchone()


def _line_changed(a, b):
    """尾盤有冇重大變動（讓球差≥0.25、讓球方轉向、任一侧水位差≥0.15）。"""
    if not a or a[0] is None:
        return bool(b and b[0] is not None)
    if not b or b[0] is None:
        return True
    if abs(abs(a[0]) - abs(b[0])) >= _LINE_MOVE_H - 1e-9:
        return True
    if (a[1] or None) != (b[1] or None):
        return True
    for i in (2, 3):
        if a[i] is not None and b[i] is not None \
                and abs(a[i] - b[i]) >= _LINE_MOVE_W:
            return True
    return False


class _EvalCtx:
    """單場統一評估上下文（2026-10-22 加速重構核心）：
    同一場嘅 target / featured_letters / featured_w / grid_report(7/8/12) /
    fw8s_report / features——全部系統共用。舊邏輯每個系統各自掃描：
    V1、V2 各算一次 featured_letters（426ms/場）；精選8、格8s、8s引擎
    各算一次 grid_report('8')（743ms/場）；而家每樣至多計一次。
    統一掃描 / 即時更新重檢 / 各引擎 scan 行緊同一套評估，結果必然一致。
    2026-10-25 加速：v3/v2 build_context 每場記憶化共享——grid('7'/'8'/'12')
    ＋featured_w＋fw8s_report 各自舊制自建 context（v3 每個約 0.8s），一場
    慳 3-4 秒。"""
    __slots__ = ('conn', 'mid', '_t', '_letters', '_fw', '_grids',
                 '_v3ctx', '_v2ctx')

    def __init__(self, conn, mid):
        self.conn = conn
        self.mid = mid
        self._t = None
        self._letters = None
        self._fw = None
        self._grids = {}
        self._v3ctx = None
        self._v2ctx = None

    def v3ctx(self):
        if self._v3ctx is None:
            import v3_engine
            self._v3ctx = v3_engine.build_context(self.conn, self.target())
        return self._v3ctx

    def v2ctx(self):
        if self._v2ctx is None:
            import v2_engine
            self._v2ctx = v2_engine.build_context(self.conn, self.target())
        return self._v2ctx

    def target(self):
        if self._t is None:
            self._t = screen_engine.get_target(self.conn, self.mid)
        return self._t

    def letters(self):
        if self._letters is None:
            import v2_engine
            self._letters = (v2_engine.featured_letters(
                self.conn, self.target(), ctx=self.v2ctx()) or {})
        return self._letters

    def fw(self):
        if self._fw is None:
            import v3_engine
            self._fw = v3_engine.featured_w(self.conn, self.target(),
                                            ctx=self.v3ctx())
        return self._fw

    def grid(self, g):
        if g not in self._grids:
            import fw_grid_engine
            self._grids[g] = fw_grid_engine.grid_report(
                self.conn, self.target(), g, ctx=self.v3ctx())
        return self._grids[g]

    def rep8s(self):
        """8s 報告——grid 型重用已計嘅 grid('8')（冇計過先即場計）。
        ctx 一定經 v3ctx() 攞（可能由此觸發首建），之後 grid('7'/'8'/'12')
        先有用埋同一份。"""
        import fw8s_engine
        return fw8s_engine.fw8s_report(
            self.conn, self.target(), grid8_rep=self._grids.get('8'),
            ctx=self.v3ctx())


def _qdict(conn, sql, args):
    """SELECT 明確欄位 → dict 列（用 cursor.description，唔郁 conn 嘅
    row_factory——共享連接其他代碼靠索引取值）。"""
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _closing_ctx(conn, mid):
    """該場 company12 尾盤（讓球＋大小）——剔走記錄結算用。"""
    r = conn.execute(
        "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
        "AND label='closing' AND company_id=12", (mid,)).fetchone()
    hc = gv = None
    if r:
        hc, gv = r
    ou = conn.execute(
        "SELECT total_line FROM odds_ou WHERE match_id=? AND label='closing' "
        'AND company_id=12', (mid,)).fetchone()
    return hc, gv, (ou[0] if ou else None)


def _pick_line(conn, mid):
    """該場 company12 嘅落注時讓球盤：優先 label='closing'，冇就用
    change_time 最新一筆任何 label。回傳 (handicap, giver)。"""
    r = conn.execute(
        "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
        "AND company_id=12 AND handicap IS NOT NULL "
        "ORDER BY (label='closing') DESC, change_time DESC LIMIT 1",
        (mid,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _tgt_pick_line(conn, t, mid):
    """INSERT 位用：target 入面已有 odds（同評估同一來源）就優先用佢哋嘅
    closing 盤；真係冇先查庫（_pick_line）。"""
    oc = ((t or {}).get('odds') or {}).get('closing') or {}
    if oc.get('h') is not None:
        return oc.get('h'), oc.get('g')
    return _pick_line(conn, mid)


_LINE_TXT_TO_H = {}


def _parse_pick_line_txt(s):
    """解析 fw8s_log.line（fmt_line 中文格式：主讓一球／客讓半/一／平手）
    → (handicap, giver)；解析唔到（NULL/雜質）返回 None，由 caller 對
    closing 做 fallback。reverse 表用 fmt_line 正向生成，0–20 球每 0.25
    一檔全覆蓋。"""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    if not _LINE_TXT_TO_H:
        for i in range(0, 81):
            h = i * 0.25
            _LINE_TXT_TO_H[screen_engine.fmt_line(h, 'home')[2:]] = h
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
        m = re.match(r'-?\d+(?:\.\d+)?', name)   # 冇中文名就當係數字串
        if not m:
            return None
        h = float(m.group(0))
    return (h, gv)


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


def _stored_first(shc, sgv, chc, cgv):
    """stored 落注盤優先；NULL 先 closing（一對 line 成對 fallback）。"""
    return (shc, sgv) if shc is not None else (chc, cgv)


_RM_SNAP_SQL = {
    'v3_featured': 'SELECT direction, detail FROM v3_featured '
                   'WHERE match_id=? AND result IS NULL',
    'featured': 'SELECT direction, letters FROM featured '
                'WHERE match_id=? AND result IS NULL',
    'featured_z': 'SELECT direction, letters FROM featured_z '
                  'WHERE match_id=? AND result IS NULL',
    'v1_featured': 'SELECT direction FROM v1_featured '
                   'WHERE match_id=? AND result IS NULL',
    'v1_featured_z': 'SELECT direction FROM v1_featured_z '
                     'WHERE match_id=? AND result IS NULL',
    'fw_grid_featured': "SELECT grid, direction, detail FROM fw_grid_featured "
                        "WHERE match_id=? AND result IS NULL",
    'fw8s_log': 'SELECT direction, line FROM fw8s_log '
                'WHERE match_id=? AND result IS NULL',
    'fw8x_log': 'SELECT direction, preds FROM fw8x_log '
                'WHERE match_id=? AND result IS NULL',
    'w88_log': 'SELECT market, conds, label, ou_line, hc, giver FROM w88_log '
               'WHERE match_id=? AND result IS NULL',
    'newjx_log': 'SELECT dir, conds FROM newjx_log '
                 'WHERE match_id=? AND result IS NULL',
}


def _rec_removed_acts(conn, mid, acts, pre):
    """_eval_all 嘅『移出』動作 → removed_picks（邊個工具＋當刻方向）。
    pre 係評估前快照（方向係『被剔嗰注』嘅方向，唔係而家嘅）。"""
    if not pre:
        return
    hc, gv, ou = _closing_ctx(conn, mid)
    for a in acts:
        if '移出' not in a:
            continue
        try:
            if a.startswith('格'):
                g = a[1:a.index('移出')]
                for r in pre.get('fw_grid_featured') or []:
                    if r['grid'] == g:
                        _record_removed(conn, '精選' + g, mid,
                                        side=r.get('direction'), hc=hc,
                                        giver=gv, detail=r.get('detail'))
            elif a.startswith('W'):
                for r in pre.get('v3_featured') or []:
                    _record_removed(conn, '精選W', mid, side=r.get('direction'),
                                    hc=hc, giver=gv, detail=r.get('detail'))
            elif a.startswith('V1Z'):
                for r in pre.get('v1_featured_z') or []:
                    _record_removed(conn, '精選V1Z', mid,
                                    side=r.get('direction'), hc=hc, giver=gv)
            elif a.startswith('V1'):
                for r in pre.get('v1_featured') or []:
                    _record_removed(conn, '精選V1', mid,
                                    side=r.get('direction'), hc=hc, giver=gv)
            elif a.startswith('V2Z'):
                for r in pre.get('featured_z') or []:
                    _record_removed(conn, '精選V2Z', mid,
                                    side=r.get('direction'), hc=hc, giver=gv)
            elif a.startswith('V2'):
                for r in pre.get('featured') or []:
                    _record_removed(conn, '精選V2', mid,
                                    side=r.get('direction'), hc=hc, giver=gv,
                                    detail=r.get('letters'))
            elif a.startswith('88W'):
                for r in pre.get('w88_log') or []:
                    _record_removed(conn, '88W', mid, side=r.get('market'),
                                    line=r.get('ou_line'), hc=r.get('hc'),
                                    giver=r.get('giver'),
                                    detail=r.get('conds') or r.get('label'))
            elif a.startswith('新精選'):
                for r in pre.get('newjx_log') or []:
                    _record_removed(conn, '新精選', mid, side=r.get('dir'),
                                    hc=hc, giver=gv, detail=r.get('conds'))
            elif a.startswith('8X'):
                for r in pre.get('fw8x_log') or []:
                    _record_removed(conn, '8X', mid, side=r.get('direction'),
                                    hc=hc, giver=gv, detail=r.get('preds'))
            elif a.startswith('8s'):
                for r in pre.get('fw8s_log') or []:
                    _record_removed(conn, '精選8s', mid, side=r.get('direction'),
                                    hc=hc, giver=gv, detail=r.get('line'))
        except Exception:
            pass


def _del_removed_on_add(conn, mid, acts):
    """2026-10-25 用戶規格：重新入選（新增）→ 剔走頁要刪除該場次該工具嘅
    舊剔走記錄——唔係咁嘅話，一場 合資格→不合→合→不合 會喺剔走頁出現
    兩次（detail 變咗 INSERT OR IGNORE 擋唔到）。88W 同一場多個市場係
    同一次剔走一齊記，唔受影響。"""
    tools = set()
    for a in acts:
        if '新增' in a:
            t = _act_tool_name(a)
            if t:
                tools.add(t)
    if not tools:
        return
    _migrate_removed(conn)
    for t in tools:
        conn.execute('DELETE FROM removed_picks WHERE match_id=? AND tool=?',
                     (mid, t))
    conn.commit()


def _eval_all(conn, ctx, light=False):
    """單場全系統評估＋寫表（12 張）：精選W、V1、V1Z、V2(letters 共用）、
    V2Z、格7/8/12/8s、8s紀錄、8X、88W、新精選。寫法＝upsert 合格／DELETE
    唔合格未結算列——同舊逐系統 scan 最終狀態一致。回傳動作列表。
    light=True（雲端 512MB 用）：淨做純 SQL 嘅 8X/88W/新精選——唔預熱
    v2/v3/screen 數據池，RAM 用量接近零（舊雲端循環同一水平）。"""
    import fw8s_engine
    mid = ctx.mid
    acts = []
    try:
        t = ctx.target()
    except Exception:
        t = None
    if not t:
        return acts
    # 2026-10-26 馬後炮守衛（用戶實證：8X 通知彈 2024-01-06 舊場）：
    # backfill_removed_from_alerts／watcher 等通道會對**已開賽**嘅歷史場
    # 重評，舊制 _eval_all 照樣 INSERT 各預測表——而且方向用「而家」嘅
    # 排名/近績計（開賽嗰刻根本冇呢啲數據），係純垃圾行兼觸發新預測
    # 彈窗。開賽超過 10 分鐘（同各引擎 scan grace 一致）→ 全部寫表分支
    # 跳過（唔新增亦唔刪；結算由 settle 流程負責）。kickoff 係本地時間
    # 字串，用 utcnow+8 比較（同機器時區無關）。
    _ko = t.get('kickoff')
    if _ko:
        try:
            from datetime import datetime, timedelta, timezone
            # 兩邊都用 naive 比較（kickoff 係本地時間字串）——舊制一边
            # aware（utcnow+8 tz-aware）一边 naive，全量掃描一觸即炸
            # TypeError（2026-10-26 實彈捉到），而且 except ValueError 接唔到。
            _hk_now = (datetime.now(timezone.utc)
                       + timedelta(hours=8)).replace(tzinfo=None)
            if datetime.strptime(str(_ko)[:16], '%Y-%m-%d %H:%M') \
                    < _hk_now - timedelta(minutes=10):
                return acts
        except ValueError:
            pass
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    # 2026-10-24 剔走記錄：評估前快照各表未結算列（方向＝被剔嗰注嘅方向）。
    # 只係 PK 查詢，微秒級；light（雲端）跳過。
    _pre_rm = {}
    if not light:
        try:
            for _t, _sql in _RM_SNAP_SQL.items():
                _rows = _qdict(conn, _sql, (mid,))
                if _rows:
                    _pre_rm[_t] = _rows
        except Exception:
            _pre_rm = {}
    if not light:
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
        # 落注時盤（INSERT 六張精選表一併寫入；stored-first 結算用）
        phc, pgv = _tgt_pick_line(conn, t, mid)
        # 精選W（V3 六條件）
        try:
            fw = ctx.fw()
            row = conn.execute('SELECT result FROM v3_featured WHERE match_id=?',
                               (mid,)).fetchone()
            if fw.get('pass'):
                conn.execute(
                    'INSERT INTO v3_featured(match_id,direction,detail,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?,?) '
                    'ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, detail=excluded.detail, '
                    'hc=excluded.hc, giver=excluded.giver',
                    (mid, fw['direction'], _fw_meta(fw), now, phc, pgv))
                log_v3_featured(conn, mid, fw['direction'])
                acts.append('W保留' if row else 'W新增')
            elif row and row[0] is None:
                conn.execute('DELETE FROM v3_featured WHERE match_id=?', (mid,))
                acts.append('W移出')
        except sqlite3.OperationalError:
            raise   # 寫表鎖死：交返 caller 成場重試——唔可以部分表寫咗部分冇
        except Exception:
            traceback.print_exc(limit=3)
        # V1 十六字頭（＋同主隊 Z）——letters 同 V2 共用 ctx 計算
        # 2026-10-22 統一化：V2（featured/featured_z）同一 letters 結果一併寫
        # （舊邏輯 V2 只有独立全掃先更新，單場重檢唔會同步——而家一致）
        try:
            fl = ctx.letters()
            d = fl.get('direction')
            passed = fl.get('passed_letters') or []
            meta = json.dumps({'letters': passed, 'states': fl.get('states')},
                              ensure_ascii=False)
            zpass = [e for e in (fl.get('letters') or [])
                     if e.get('mix') == '同主隊' and e['letter'] in passed]
            dz = zpass[0]['dir'] if zpass else None
            row = conn.execute('SELECT result FROM v1_featured WHERE match_id=?',
                               (mid,)).fetchone()
            if d:
                conn.execute(
                    'INSERT INTO v1_featured(match_id,direction,added_at,hc,giver) '
                    'VALUES(?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, hc=excluded.hc, '
                    'giver=excluded.giver',
                    (mid, d, now, phc, pgv))
                log_featured(conn, mid, d)
                acts.append('V1保留' if row else 'V1新增')
            elif row and row[0] is None:
                conn.execute('DELETE FROM v1_featured WHERE match_id=?', (mid,))
                acts.append('V1移出')
            row2 = conn.execute('SELECT result FROM featured WHERE match_id=?',
                                (mid,)).fetchone()
            if d:
                conn.execute(
                    'INSERT INTO featured(match_id,direction,letters,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?,?) '
                    'ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, letters=excluded.letters, '
                    'hc=excluded.hc, giver=excluded.giver',
                    (mid, d, meta, now, phc, pgv))
                log_featured(conn, mid, d)
                acts.append('V2保留' if row2 else 'V2新增')
            elif row2 and row2[0] is None:
                conn.execute('DELETE FROM featured WHERE match_id=?', (mid,))
                acts.append('V2移出')
            rowz = conn.execute('SELECT result FROM v1_featured_z WHERE match_id=?',
                                (mid,)).fetchone()
            if dz:
                conn.execute(
                    'INSERT INTO v1_featured_z(match_id,direction,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?) '
                    'ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, hc=excluded.hc, '
                    'giver=excluded.giver',
                    (mid, dz, now, phc, pgv))
                log_featured_z(conn, mid, dz)
                acts.append('V1Z保留' if rowz else 'V1Z新增')
            elif rowz and rowz[0] is None:
                conn.execute('DELETE FROM v1_featured_z WHERE match_id=?', (mid,))
                acts.append('V1Z移出')
            rowz2 = conn.execute(
                'SELECT result FROM featured_z WHERE match_id=?', (mid,)).fetchone()
            if dz:
                conn.execute(
                    'INSERT INTO featured_z(match_id,direction,letters,added_at,'
                    'hc,giver) VALUES(?,?,?,?,?,?) '
                    'ON CONFLICT(match_id) DO UPDATE SET '
                    'direction=excluded.direction, letters=excluded.letters, '
                    'hc=excluded.hc, giver=excluded.giver',
                    (mid, dz, meta, now, phc, pgv))
                log_featured_z(conn, mid, dz)
                acts.append('V2Z保留' if rowz2 else 'V2Z新增')
            elif rowz2 and rowz2[0] is None:
                conn.execute('DELETE FROM featured_z WHERE match_id=?', (mid,))
                acts.append('V2Z移出')
        except sqlite3.OperationalError:
            raise   # 寫表鎖死：成場重試——部分寫咗部分冇會令 12 表資格不一致
        except Exception:
            traceback.print_exc(limit=3)
        # 格 7／8／12／8s——grid('8') 俾 8s/grid8s/8s紀錄共用
        rep8s = None
        try:
            for g in ('7', '8', '12', '8s'):
                try:
                    rep = ctx.rep8s() if g == '8s' else ctx.grid(g)
                    if g == '8s':
                        rep8s = rep
                except Exception:
                    continue
                row = conn.execute(
                    'SELECT result FROM fw_grid_featured WHERE grid=? AND match_id=?',
                    (g, mid)).fetchone()
                if rep.get('pass'):
                    conn.execute(
                        'INSERT INTO fw_grid_featured(grid,match_id,direction,'
                        'detail,added_at,hc,giver) VALUES(?,?,?,?,?,?) '
                        'ON CONFLICT(grid,match_id) DO UPDATE SET '
                        'direction=excluded.direction, detail=excluded.detail, '
                        'hc=excluded.hc, giver=excluded.giver',
                        (g, mid, rep['direction'],
                         fw8s_engine.fw8s_meta(rep) if g == '8s'
                         else _fw_grid_meta(rep), now, phc, pgv))
                    acts.append('格%s保留' % g if row else '格%s新增' % g)
                elif row and row[0] is None:
                    conn.execute(
                        'DELETE FROM fw_grid_featured WHERE grid=? AND match_id=?',
                        (g, mid))
                    acts.append('格%s移出' % g)
        except sqlite3.OperationalError:
            raise   # 寫表鎖死：成場重試（10-24 實證：grid INSERT 鎖死被吞→半寫）
        except Exception:
            traceback.print_exc(limit=3)
    # 2026-10-22 擴充（用戶規格：更新盤口後唔合資格嘅未開賽賽事要剔走）：
    # 88W／新精選／8X／8s紀錄表——用新線重檢，唔合 → DELETE 未結算列
    # （20 分鐘循環會照新線補返；已結算歷史永不刪）。評估邏輯同各引擎
    # scan 共用 eval_match，唔會漂移。
    # 88W（一場可中多條市場方法）
    try:
        import w88_engine
        hits = w88_engine.eval_match(conn, mid)
        now_keys = {(k[1], k[2]) for k, _v in hits}
        have = {(r[0], r[1]) for r in conn.execute(
            'SELECT market, conds FROM w88_log WHERE match_id=? '
            'AND result IS NULL', (mid,))}
        for k, v in hits:
            if (k[1], k[2]) not in have:
                conn.execute(
                    'INSERT OR IGNORE INTO w88_log '
                    '(match_id, market, conds, league, label, ou_line, hc, '
                    ' giver, added_at) VALUES(?,?,?,?,?,?,?,?,?)', v)
                acts.append('88W新增')
        for mkt, conds in have - now_keys:
            conn.execute(
                'DELETE FROM w88_log WHERE match_id=? AND market=? '
                'AND conds=? AND result IS NULL', (mid, mkt, conds))
            acts.append('88W移出')
    except sqlite3.OperationalError:
        raise   # 寫表鎖死：成場重試，保證各表一致
    except Exception:
        traceback.print_exc(limit=3)
    # 新精選
    try:
        import newjx_engine
        v = newjx_engine.eval_match(conn, mid)
        row = conn.execute('SELECT result FROM newjx_log WHERE match_id=?',
                           (mid,)).fetchone()
        if v:
            if not row:
                conn.execute('INSERT OR IGNORE INTO newjx_log '
                             'VALUES(?,?,?,?,?,?,?,?,?)', v + (None, None))
                acts.append('新精選新增')
        elif row and row[0] is None:
            conn.execute('DELETE FROM newjx_log WHERE match_id=?', (mid,))
            acts.append('新精選移出')
    except sqlite3.OperationalError:
        raise   # 寫表鎖死：成場重試，保證各表一致
    except Exception:
        traceback.print_exc(limit=3)
    # 8X
    try:
        import fw8x
        got = fw8x.eval_match(conn, mid)
        row = conn.execute('SELECT result FROM fw8x_log WHERE match_id=?',
                           (mid,)).fetchone()
        if got:
            if not row:
                info, f = got
                conn.execute(
                    'INSERT OR IGNORE INTO fw8x_log VALUES(?,?,?,?,?,?,?,?,?,'
                    '?,?,?,?,?,?,?)',
                    (mid, info['kickoff'], info['league'], info['home'],
                     info['away'], f['dir'], json.dumps(
                         f['preds'], ensure_ascii=False),
                     f.get('hit'), f.get('n'), info['hc'], info['gv'],
                     info['cho'], info['cao'], time.time(), None, 0))
                acts.append('8X新增')
        elif row and row[0] is None:
            conn.execute('DELETE FROM fw8x_log WHERE match_id=?', (mid,))
            acts.append('8X移出')
    except sqlite3.OperationalError:
        raise   # 寫表鎖死：成場重試，保證各表一致
    except Exception:
        traceback.print_exc(limit=3)
    # 8s 紀錄表（fw8s_log；rep8s 喺上面格循環已計，唔使重計。light 模式冇
    # 格報告，成塊跳過——否則會誤刪雲端由本機同步嚟嘅 8s 入選）
    if not light:
        try:
            row = conn.execute('SELECT result FROM fw8s_log WHERE match_id=?',
                               (mid,)).fetchone()
            if rep8s and rep8s.get('pass'):
                if not row:
                    oc = (t.get('odds') or {}).get('closing') or {}
                    fw8s_engine.log_picks(conn, [{
                        'match_id': mid,
                        'league': t.get('league'),
                        'mode': 'direct',
                        'conds': [(rep8s.get('formula') or '8s公式')
                                  .split('：', 1)[-1]],
                        'line': (screen_engine.fmt_line(oc.get('h'),
                                                        oc.get('g'))
                                 if oc.get('h') is not None else None),
                        'odds': ('主{}/客{}'.format(oc.get('ho'), oc.get('ao'))
                                 if oc.get('ho') is not None else None),
                    }])
                    acts.append('8s新增')
            elif row and row[0] is None:
                conn.execute('DELETE FROM fw8s_log WHERE match_id=?', (mid,))
                acts.append('8s移出')
        except sqlite3.OperationalError:
            raise   # 寫表鎖死：成場重試，保證各表一致
        except Exception:
            traceback.print_exc(limit=3)
    # 2026-10-22 用戶規格：入選/剔走即時推送＋清顯示快取（剔走 100% 唔殘留）
    if any(('新增' in a or '移出' in a) for a in acts):
        try:
            _notify_tool(conn, mid, acts)
        except Exception:
            pass
        _bust_tool_caches()
        # 2026-10-25 即時推送：有新增/剔走即刻廣播（SSE client 1-3 秒內刷新）
        _publish_event({'type': 'reval', 'mid': mid, 'acts': acts})
    # 2026-10-25 用戶規格：重新入選（新增）→ 剔走頁刪除該場次該工具嘅舊記錄，
    # 保證同一場次只出現一次。放喺記錄剔走之前——同一 pass 又有新增又有移出
    # 時，最後留下嘅係最新嗰條剔走記錄。
    if any('新增' in a for a in acts):
        try:
            _del_removed_on_add(conn, mid, acts)
        except Exception:
            pass
    # 2026-10-24 剔走專頁：『移出』動作連方向記錄落 removed_picks
    if _pre_rm and any('移出' in a for a in acts):
        try:
            _rec_removed_acts(conn, mid, acts, _pre_rm)
        except Exception:
            pass
    return acts


def _pick_revalidate(conn, mid):
    """單場精選資格重檢（評估-only：唔重抓、唔清歷史池——歷史場次尾盤
    永不變，變嘅只有目標場自己）。回傳動作列表。
    2026-10-24：雲端（DISABLE_UPDATE）自動行 light 模式——掣嘅 sweep
    所有重檢通道（watcher/批量/掃描 sweep）都經呢度；重模式會載 300MB 數據池
    容器 OOM（用戶問『手機係咪一樣得』先發現嘅漏洞）。雲端 light 淨做
    純 SQL 系統；W/V1/V2/格 呢啲由本機 30 分鐘 delta 同步補返。"""
    # 2026-10-25：用戶確認 Render 係 2GB/1C 付費實例——512MB OOM 迴避（05ad3a8）
    # 已過時：池本來就喺雲端預熱（v3 API 要用），light 慳唔到 RAM。
    # 兩邊行 full：雲端 W/V1/V2/格資格唔使再等本機 sync。
    return _eval_all(conn, _EvalCtx(conn, mid), light=False)


def _requalify_sweep(mids, cap=300):
    """2026-10-22 用戶規格「即時更新全部賠率後工具要有動作」：對成批更新咗
    盤口嘅場次，逐場用新線重檢八個精選系統資格——仍合→保留；唔合→剔走
    （DELETE 未結算列，20 分鐘循環照新線補返）；新合→加入。回傳計數俾
    /api/fetch-batch-status 顯示「保留X・移出Y・新增Z」。逐場短事務＋鎖重試，
    唔會同爬蟲長寫交易死鎖。上限 cap 場防止極端批量拖垮成個流程。"""
    summary = {'checked': 0, 'kept': 0, 'removed': 0, 'added': 0}
    if not mids:
        return summary
    _scanlog('⚡ 即時更新後資格重檢：%d 場' % len(list(mids)))
    for mid in list(mids)[:cap]:
        for _attempt in range(3):
            try:
                conn = db()
                try:
                    acts = _pick_revalidate(conn, mid)
                    conn.commit()
                    _scanlog_match(conn, mid, acts)
                finally:
                    conn.close()
                summary['checked'] += 1
                for a in acts:
                    if '移出' in a:
                        summary['removed'] += 1
                    elif '新增' in a:
                        summary['added'] += 1
                    else:
                        summary['kept'] += 1
                break
            except sqlite3.OperationalError:
                time.sleep(4)
            except Exception:
                traceback.print_exc(limit=3)
                break
    if len(mids) > cap:
        summary['capped'] = len(mids) - cap
    if summary['removed'] or summary['added']:
        _scanlog('⚡ 重檢搞掂：剔走 %d・新增 %d' % (summary['removed'],
                                               summary['added']))
    return summary


# ============ 2026-10-22 統一掃描（用戶：掃描／更新預測／更新結果 合一且快） ============
# 舊邏輯：11 個系統各自全掃描——同一場重複計 featured_letters（V1+V2）、
# grid_report('8')（精選8+格8s+8s引擎）、features（8X/88W/新精選），
# 80 場 × ~4.8 秒/系統組合 ≈ 6 分鐘。新邏輯：一場一次過評全部系統
# （_EvalCtx 共享計算），加「變動跳過」——盤口冇新資料兼開賽>24h 嘅場
# 冇可能變資格，直接跳。手动掃描通常只涉及近期變動嘅 5-15 場，秒級完成。
_unified_scan = {'running': False, 'phase': '', 'done': 0, 'total': 0,
                 'added': 0, 'removed': 0, 'kept': 0, 'error': None,
                 'full': False, 'last': None, 'passes': 0,
                 'triggered_by': None}
# 2026-10-25 掃描排隊（用戶規格：log 要見到邊個工具觸發緊＋邊個排緊隊）：
# 統一掃描行緊時，新請求唔再靜默丟棄——入隊，跑完依家嗰輪逐個消化。
# 根治（用戶 10-25 指示 #4）：隊列同熔斷補跑登記全部落碟
# engine/_scan_state.json——APP 重啟唔再靜默消失。
_SCAN_QUEUE = []
_SCAN_STATE_PATH = os.path.join(PARENT_DIR, 'engine', '_scan_state.json')


def _load_scan_state():
    try:
        d = json.load(open(_SCAN_STATE_PATH, encoding='utf-8'))
        for q in d.get('queue') or []:
            _SCAN_QUEUE.append(q)
        if d.get('pend_hours') is not None:
            _FBATCH_PEND['hours'] = d['pend_hours']
            _FBATCH_PEND['ts'] = float(d.get('pend_ts') or 0.0)
    except Exception:
        pass


def _save_scan_state():
    try:
        with open(_SCAN_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'queue': _SCAN_QUEUE,
                       'pend_hours': _FBATCH_PEND.get('hours'),
                       'pend_ts': _FBATCH_PEND.get('ts')},
                      f, ensure_ascii=False)
    except OSError:
        pass
_LAST_UNIFIED = [None]   # 上次統一掃描完成時間（HK，文字）；None＝未掃過
_UNIFIED_RUNNING = [False]   # 掃描進行中：after_odds_update 讓路＋釘住數據池
_UNIFIED_STATE_PATH = os.path.join(PARENT_DIR, 'engine',
                                   '_unified_state.json')


def _load_unified_state():
    """掃描進度落碟（2026-10-22 D6 體檢修正）：_LAST_UNIFIED 舊係純記憶體——
    每次重開機第一 pass 都係全量 189 場（20 分鐘），期間其他請求捱饿。
    落到 engine/_unified_state.json：重開後照舊增量（除非超過 6 小時週期）。"""
    try:
        d = json.load(open(_UNIFIED_STATE_PATH, encoding='utf-8'))
        if d.get('ts'):
            _LAST_UNIFIED[0] = d['ts']
        _unified_scan['passes'] = int(d.get('passes') or 0)
    except Exception:
        pass


def _save_unified_state():
    try:
        with open(_UNIFIED_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'ts': _LAST_UNIFIED[0],
                       'passes': _unified_scan['passes']}, f)
    except Exception:
        pass


_load_unified_state()

# acts 字首 → 工具計數桶（注意次序：V1Z 先於 V1、格8s 先於格8）
_ACT_BUCKET = (('W', 'v3'), ('V2Z', 'featz'), ('V2', 'feat'),
               ('V1Z', 'v1z'), ('V1', 'v1'),
               ('格7', 'f7'), ('格8s', 'f8s'), ('格8', 'f8'),
               ('格12', 'f12'), ('88W', 'w88'), ('新精選', 'jx'),
               ('8X', 'x8'), ('8s', 's8log'))


def _act_bucket(a):
    for p, b in _ACT_BUCKET:
        if a.startswith(p):
            return b
    return None


def _pin_pools():
    """掃描期間釘住三座數據池（2026-10-22 實測：爬蟲每寫一場盤口就
    invalidate 池（ts=0），掃描中反覆重建 → 53 秒/場）。掃描用開工時嘅
    池（最多舊一個掃描週期，分鐘級誤差），掃描完先正常失效。"""
    import screen_engine
    import v2_engine
    import v3_engine
    now = time.time()
    for c in (screen_engine._pool_cache, v2_engine._v2_pool,
              v3_engine._v3_pool):
        try:
            if c.get('df') is not None:
                c['ts'] = now
        except Exception:
            pass


def _never_evaluated(conn, mid):
    return not conn.execute(
        'SELECT 1 WHERE EXISTS(SELECT 1 FROM v3_featured WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM featured WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM v1_featured WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM fw_grid_featured WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM fw8x_log WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM w88_log WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM newjx_log WHERE match_id=?) '
        'OR EXISTS(SELECT 1 FROM fw8s_log WHERE match_id=?) LIMIT 1',
        (mid, mid, mid, mid, mid, mid, mid, mid)).fetchone()


def _unified_candidates(conn, full):
    """變動跳過揀選（2026-10-22c 修正：用『尾盤簽名』而唔係 fetched_at——
    爬蟲fleet 24/7 重抓，fetched_at 場場都新，舊規則令增量變全量）。
    full/首次＝全部；否則淨揀 ①開賽 4 小時內 ②未評估過（冇簽名）③尾盤
    （讓球/讓球方/主水/客水）同上次評估時唔同。排名驅動變化由每 6 小時
    全量兜住。回傳 (mids, sigs)——sigs 俾 job 寫返落 _unified_snap。"""
    rows = conn.execute(
        "SELECT m.id, m.kickoff, oc.handicap, oc.giver, oc.home_odds, "
        'oc.away_odds FROM matches m '
        "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
        "AND oc.company_id=12 AND oc.handicap IS NOT NULL "
        "WHERE m.home_score IS NULL "
        "AND m.kickoff >= datetime('now','+8 hours') "
        'ORDER BY m.kickoff').fetchall()
    snaps = {}
    try:
        snaps = dict(conn.execute(
            'SELECT match_id, sig FROM _unified_snap'))
    except sqlite3.Error:
        conn.execute('''CREATE TABLE IF NOT EXISTS _unified_snap(
            match_id INTEGER PRIMARY KEY, sig TEXT, ts TEXT)''')

    def _sig(hc, gv, ho, ao):
        return '%s|%s|%s|%s' % (hc, gv, ho, ao)

    if full or not _LAST_UNIFIED[0]:
        return ([r[0] for r in rows],
                {r[0]: _sig(r[2], r[3], r[4], r[5]) for r in rows})
    from datetime import datetime, timedelta
    soon = (datetime.now() + timedelta(hours=8 + 4)
            ).strftime('%Y-%m-%d %H:%M')
    out, sigs = [], {}
    for mid, ko, hc, gv, ho, ao in rows:
        s = _sig(hc, gv, ho, ao)
        if ko <= soon or snaps.get(mid) != s:
            out.append(mid)
            sigs[mid] = s
    return out, sigs


def _unified_scan_job(mids=None, full=False, light=False, tool=None):
    """一場一次過評 12 表＋結算。各工具自家狀態 dict 同步更新——
    所有舊進度條／scan-status 接口照舊有得顯示，前端零改動。
    light=True（雲端 512MB）：淨做純 SQL 系統（8X/88W/新精選），
    唔預熱數據池，RAM 接近零——同舊雲端三循環總負載相若。
    tool（2026-10-25）＝邊個工具觸發（log 歸因）；queue 喺 finally 消化。"""
    st = _unified_scan
    if st['running']:
        return False
    st.update(running=True, error=None, phase='準備中…', done=0,
              added=0, removed=0, kept=0, full=full, stop=False)
    st['triggered_by'] = tool or st.get('triggered_by') or '未知'
    _UNIFIED_RUNNING[0] = True
    _NOTIFY_N[0] = 0   # 本輪推送計數（>15 批量標讀防洗版）
    _scanlog('🔎 統一掃描開始（%s｜觸發：%s）'
             % ('全量' if full else '增量', st['triggered_by']))
    tools = (_feat_scan, _v1_scan, _v3_scan,
             _fw_grid_scan['7'], _fw_grid_scan['8'],
             _fw_grid_scan['12'], _fw_grid_scan['8s'])
    for d in tools:
        d['running'] = True
        d['done'] = 0
        d['error'] = None
    _bg_scan_info['newjx'] = {
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    _bg_scan_info['w88'] = {
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    try:
        # 同更新工序互斥：全量更新／即時更新行緊就等佢完（最多 15 分鐘）——
        # 否則佢每寫一場就觸發逐場重檢＋invalidate 池，雙方鬥慢（實測
        # 53 秒/場嘅元兇之一）。更新完咗下一個循環會補返。
        t_wait = time.time()
        while (_update_state['running'] or _fbatch_state['running']) \
                and time.time() - t_wait < 900:
            st['phase'] = '等緊更新工序完成…'
            _scanlog('⏸ 等緊更新工序完成，篩描排緊隊…')
            _bg_scan_info['newjx']['error'] = (
                '等緊更新工序完成，掃描排緊隊（等佢搞掂自動開跑）')
            _bg_scan_info['w88']['error'] = (
                '等緊更新工序完成，掃描排緊隊（等佢搞掂自動開跑）')
            time.sleep(15)
        _bg_scan_info['newjx']['error'] = None
        _bg_scan_info['w88']['error'] = None
        conn = db()
        try:
            # 開工前預熱三座池並釘住——掃描期間爬蟲寫盤口唔會觸發重建
            import screen_engine
            import v2_engine
            import v3_engine
            screen_engine.load_pool(conn)
            v2_engine.load_v2_pool(conn)
            v3_engine.load_v3_pool(conn)
            _pin_pools()
            cands = list(mids) if mids is not None else None
            sigs = {}
            if cands is None:
                cands, sigs = _unified_candidates(conn, full)
            elif not light:
                # 外部指定場次：同樣記低簽名（之後先至跳得過佢）
                for _mid in cands:
                    _c = conn.execute(
                        "SELECT handicap, giver, home_odds, away_odds "
                        "FROM odds_asian WHERE match_id=? AND label='closing' "
                        'AND company_id=12', (_mid,)).fetchone()
                    if _c:
                        sigs[_mid] = '%s|%s|%s|%s' % _c
            st['total'] = len(cands)
            for d in tools:
                d['total'] = len(cands)
            cnt = {}
            # 2026-10-25 提速＋解鎖：逐場獨立 commit（舊制 20 場先 commit
            # 一次＝寫鎖一拎 2 分鐘，掃描期間成個 APP 冇反應——單場 commit
            # 將寫鎖縮到秒級，爬蟲/頁面請求穿插到行）。注意 _eval_all 每場
            # 自己寫 UPSERT，COMMIT 頻率係吞吐同鎖競爭嘅平衡點。
            for i, mid in enumerate(cands):
                if st.get('stop'):
                    _scanlog('🛑 收到停止指令——完成手頭呢場後收工'
                             '（已搞 %d/%d 場）' % (i, len(cands)))
                    break
                st['phase'] = '統一掃描 %d/%d' % (i + 1, len(cands))
                acts = None
                for _att in range(4):
                    try:
                        acts = _eval_all(conn, _EvalCtx(conn, mid),
                                         light=light)
                        break
                    except sqlite3.OperationalError:
                        time.sleep(4)
                if acts is None:
                    continue
                _committed = False
                for _att in range(4):
                    try:
                        _scanlog_match(conn, mid, acts)
                        conn.commit()   # 逐場 commit：寫鎖秒級釋放（舊制 20 場一鎖）
                        _committed = True
                        break
                    except sqlite3.OperationalError:
                        time.sleep(4)   # 爬蟲/頁面寫撞鎖——等 4 秒重試
                if not _committed:
                    print('[scan] ⚠️ 場次 %d commit 4 次重試後仍鎖死——'
                          '簽名未寫，下輪會補評' % mid, flush=True)
                _s = sigs.get(mid)
                if _s:
                    _snap_done = False
                    for _att in range(4):
                        try:
                            conn.execute(
                                'INSERT INTO _unified_snap(match_id,sig,ts) '
                                'VALUES(?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                                'sig=excluded.sig, ts=excluded.ts',
                                (mid, _s,
                                 time.strftime('%Y-%m-%d %H:%M:%S')))
                            conn.commit()
                            _snap_done = True
                            break
                        except sqlite3.OperationalError:
                            time.sleep(4)   # 2026-10-26：無重試會成個 pass 炸冧
                    if not _snap_done:
                        # 2026-10-26 實彈：2962699 簽名靜默漏寫——之後永遠
                        # 被簽名跳過。必須留聲（下輪冇簽名=會補評，但要知道發生過）
                        print('[scan] ⚠️ 場次 %d 簽名 4 次重試後仍鎖死' % mid,
                              flush=True)
                for a in acts:
                    if '移出' in a:
                        st['removed'] += 1
                    elif '新增' in a:
                        st['added'] += 1
                    else:
                        st['kept'] += 1
                    b = _act_bucket(a)
                    if b and '新增' in a:
                        cnt[b] = cnt.get(b, 0) + 1
                st['done'] = i + 1
                for d in tools:
                    d['done'] = i + 1
                if (i + 1) % 10 == 0:
                    _pin_pools()   # 爬蟲可能喺上一批之間 invalidate 咗池
                time.sleep(0.1)   # 讓 GIL 俾 HTTP 線程
            conn.commit()
            _scanlog('🧮 篩描完成 %d 場：新增 %d・剔走 %d——而家結算已完場結果…'
                     % (len(cands), st['added'], st['removed']))
            st['phase'] = '結算已完場結果…'
            try:
                settled = _settle_finished_tools(conn)
            except sqlite3.OperationalError:
                # 爬蟲fleet 24/7 寫庫——結算撞鎖唔應該搞到成個 pass 報錯
                # （評估已完成；結算交返 15 分鐘巡邏循環補）
                time.sleep(5)
                try:
                    settled = _settle_finished_tools(conn)
                except Exception:
                    settled = {}
            except Exception:
                settled = {}
            import v3_engine
            hk = v3_engine.hk_now_str()
            _LAST_UNIFIED[0] = hk
            st['last'] = hk
            st['passes'] += 1
            st['error'] = None
            _save_unified_state()
            _bump_data_epoch('tools')   # 2026-10-26：重算完，工具頁快取作廢
            _feat_scan.update(running=False, added=cnt.get('feat', 0),
                              added_z=cnt.get('featz', 0), error=None)
            _v1_scan.update(running=False, added=cnt.get('v1', 0),
                            added_z=cnt.get('v1z', 0), error=None)
            _v3_scan.update(running=False, added=cnt.get('v3', 0),
                            error=None)
            _fw_grid_scan['7'].update(running=False, added=cnt.get('f7', 0),
                                      error=None)
            _fw_grid_scan['8'].update(running=False, added=cnt.get('f8', 0),
                                      error=None)
            _fw_grid_scan['12'].update(running=False,
                                       added=cnt.get('f12', 0), error=None)
            _fw_grid_scan['8s'].update(running=False,
                                       added=cnt.get('f8s', 0), error=None)
            _bg_scan_info['newjx'].update(
                added=cnt.get('jx', 0),
                settled=settled.get('newjx_log', 0), error=None)
            _bg_scan_info['w88'].update(
                added=cnt.get('w88', 0),
                settled=settled.get('w88_log', 0),
                added_8x=cnt.get('x8', 0),
                settled_8x=settled.get('fw8x_log', 0), error=None)
            print('[unified] 掃描 %d 場（%s）：新增 %d・移出 %d・保留 %d｜'
                  '結算 %s' % (len(cands), 'light' if light else 'full',
                             st['added'], st['removed'],
                             st['kept'], settled or '無'), flush=True)
            _scanlog('✅ 收工：新增 %d・剔走 %d・保留 %d｜結算 %s'
                     % (st['added'], st['removed'], st['kept'],
                        settled or '無'))
            if _NOTIFY_N[0] > 15:
                # 批量入選（重啟後首次全掃常見）：推送保留做歷史記錄，
                # 但一次過標晒已讀——唔會 100+ 紅點洗版；之後增量輪
                # 先逐條彈（嗰啲至係『新發生』事件）
                conn.execute('UPDATE alerts SET read=1 WHERE read=0')
                conn.commit()
                print('[unified] 本輪 %d 條推送已批量標讀（防洗版）'
                      % _NOTIFY_N[0], flush=True)
        finally:
            conn.close()
    except Exception:
        traceback.print_exc(limit=3)
        st['error'] = traceback.format_exc(limit=2)[-300:]
        for d in tools:
            d['error'] = st['error']
            d['running'] = False
        _bg_scan_info['newjx']['error'] = st['error']
        _bg_scan_info['w88']['error'] = st['error']
    finally:
        # 2026-10-25：重爬重算補好波（本地；雲端 demand-driven 唔做）
        try:
            if not DISABLE_UPDATE:
                _conn = db()
                try:
                    _mids = [r[0] for r in _conn.execute(
                        "SELECT m.id FROM matches m WHERE m.home_score IS NULL "
                        "AND m.kickoff >= datetime('now','+8 hours') "
                        "AND m.kickoff <= datetime('now','+8 hours','+48 hours') "
                        "AND EXISTS(SELECT 1 FROM odds_ou o WHERE o.match_id=m.id "
                        "AND o.label='closing') LIMIT 150")]
                finally:
                    _conn.close()
                if _mids:
                    _hb_refresh_mids(_mids)
            # 2026-10-25 即時同步：重抓重算搞掂→即刻推上雲
            _sync_push_now(force=True)
        except Exception:
            pass
        st['running'] = False
        st['phase'] = ''
        _UNIFIED_RUNNING[0] = False
        if not st['error']:
            for d in tools:
                d['running'] = False
        # 2026-10-25：消化排隊請求（有人排緊就即刻接力，唔等 5 分鐘循環）。
        # 用戶撳咗停止就唔接力——停就要停得徹底。
        if not st.get('stop'):
            try:
                _drain_scan_queue()
            except Exception:
                pass
    return True


_LAST_FULL_DATE = [None]   # 2026-10-26：全量掃描每晚一次（凌晨窗）


def _unified_loop():
    """每 20 分鐘統一掃描（變動跳過）；每 18 次（約 6 小時）做一次全量，
    兜實排名變化等冇盤口訊號嘅資格變動。雲端（DISABLE_UPDATE）永久行
    light 模式：淨做純 SQL 系統——512MB 預熱數據池會 OOM。"""
    time.sleep(180)
    while True:
        try:
            # 2026-10-25：2GB/1C 付費版——雲端統一掃描行 full（512MB 限制已過時）
            # 2026-10-26 效能（用戶「太慢」優化 #3）：全量限定凌晨 03:00–06:00
            # （HKT）一晚一次——日間全量同爬取/頁面請求搶 1C CPU；日間變動
            # 由簽名跳過＋20 秒轉盤 watcher 兜住。
            light = False
            full = False
            try:
                from datetime import datetime as _dt, timedelta as _td,                     timezone as _tz
                _hk = _dt.now(_tz.utc) + _td(hours=8)
                if _hk.hour in (3, 4, 5):
                    _d = _hk.strftime('%Y-%m-%d')
                    if _LAST_FULL_DATE[0] != _d:
                        full = True
                        _LAST_FULL_DATE[0] = _d
            except Exception:
                pass
            _unified_scan_job(full=full, light=light, tool='5分鐘定時循環')
        except Exception:
            traceback.print_exc(limit=3)
        # 2026-10-24：20 分鐘→5 分鐘——爬蟲直接寫新盤唔會即刻通知
        # APP（after_odds_update 只有 APP 通道先掛），工具表滯後最多
        # 20 分鐘；增量 pass 有變動簽名跳過（成本秒級～分鐘級），
        # 5 分鐘將工具表滯後縮到 ≤5 分鐘。full 照舊每 18 pass。
        time.sleep(300)


def _drain_scan_queue():
    """統一掃描收工後消化排隊請求（一個一個嚟——每個請求跑完先再 pop
    下一個，避免同時開兩輪互鬥）。"""
    while _SCAN_QUEUE and not _unified_scan['running']:
        req = _SCAN_QUEUE.pop(0)
        _save_scan_state()
        _scanlog('🚀 執行排隊請求：%s（重剩 %d 個排緊）'
                 % (req['tool'], len(_SCAN_QUEUE)))
        _unified_scan['triggered_by'] = req['tool']
        _t = threading.Thread(target=_unified_scan_job,
                              kwargs={'mids': req['mids'], 'full': req['full'],
                                      'tool': req['tool']},
                              daemon=True)
        _JOB_THREADS['統一掃描'] = _t
        _t.start()
        return


def _scan_meta_extra():
    """2026-10-25：各工具 scan-status 共用附加欄位——邊個工具觸發緊統一
    掃描＋排隊緊嘅請求（用戶規格：log/進度要見到歸因同排隊）。"""
    return {'triggered_by': _unified_scan.get('triggered_by'),
            'queue': [q['tool'] for q in _SCAN_QUEUE],
            'unified_running': _unified_scan['running'],
            'unified_done': _unified_scan['done'],
            'unified_total': _unified_scan['total']}

def _start_unified_scan(mids=None, full=False, light=None, tool='未知'):
    """所有「重新掃描」掣共用入口：得閒即開；行緊就入隊（2026-10-25 用戶
    規格——舊制繁忙時靜默丟棄，用戶以為掃描冇反應），跑完依家嗰輪自動接力，
    每個請求都記低邊個工具觸發，log 全程可見。"""
    if light is None:
        # 2026-10-25：預設 full（付費 2GB 實例）；仍可按需要傳 light=True
        light = False
    if _unified_scan['running']:
        key = (tool, bool(full), bool(mids))
        if not any((q['tool'], q['full'], q['mids'] is not None) == key
                   for q in _SCAN_QUEUE):
            _SCAN_QUEUE.append({'tool': tool, 'full': bool(full),
                                'mids': mids,
                                'ts': time.strftime('%H:%M:%S')})
            _save_scan_state()
            _scanlog('⏳ %s 掃描排隊緊（等依家嗰輪搞掂自動接力；前面仲有 %d 個）'
                     % (tool, len(_SCAN_QUEUE) - 1))
        return {'running': True, 'started': False, 'queued': True}
    _unified_scan['triggered_by'] = tool
    _save_scan_state()
    _t = threading.Thread(target=_unified_scan_job,
                          kwargs={'mids': mids, 'full': full, 'light': light,
                                  'tool': tool},
                          daemon=True)
    _JOB_THREADS['統一掃描'] = _t
    _t.start()
    return {'running': True, 'started': True}


# ============ 2026-10-22 完場賽事主動結算（用戶規格：完場後自動更新到
# 精選W／精選7／8／8s／8X／88W／新精選／精選12／半全場／V1／V2／好波 全部結果） ============
def _settle_finished_tools(conn):
    """主動幫全部精選/紀錄表「已完場但未結算」嘅列做結算——語義逐字對齊
    各頁 lazy settle（pick_result＋入選方向＋落注時盤 stored hc/giver；
    legacy 冇 stored 先對 company12 closing；好波=總入球對
    大小線），只係由「被讀先結」變「循環主動全結」。回傳 {表: 結算場數}。
    只 UPDATE result IS NULL 嘅列，歷史永不改動。"""
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    out = {}
    try:
        _migrate_pick_line_cols(conn)
    except sqlite3.Error:
        pass

    def _dir_tbl(tbl):
        # featured 系五張平表：direction up/down + 落注時盤（NULL 先 closing）
        rows = conn.execute(
            'SELECT x.match_id, x.direction, x.hc, x.giver, '
            'o.handicap, o.giver, m.home_score, m.away_score FROM %s x '
            'JOIN matches m ON m.id=x.match_id LEFT JOIN odds_asian o '
            "ON o.match_id=x.match_id AND o.label='closing' "
            'AND o.company_id=12 WHERE x.result IS NULL '
            'AND m.home_score IS NOT NULL' % tbl).fetchall()
        n = 0
        for mid, d, shc, sgv, chc, cgv, hs, aws in rows:
            if d not in ('up', 'down'):
                continue
            hc, gv = _stored_first(shc, sgv, chc, cgv)
            r = pick_result(hc, gv, hs, aws)
            if r is None:
                continue
            res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up')
                                        else 'L')
            conn.execute('UPDATE %s SET result=?, settled_at=? '
                         'WHERE match_id=?' % tbl, (res, now, mid))
            n += 1
        if n:
            out[tbl] = n
            conn.commit()   # 每表即 commit：一個表出錯唔會拖累其他表

    for _t in ('featured', 'featured_z', 'v1_featured', 'v1_featured_z',
               'v3_featured'):
        try:
            _dir_tbl(_t)
        except sqlite3.Error:
            pass
    # fw_grid_featured：PK 係 (grid, match_id)——同一場可入多格，方向各自計
    try:
        rows = conn.execute(
            'SELECT x.grid, x.match_id, x.direction, x.hc, x.giver, '
            'o.handicap, o.giver, m.home_score, m.away_score '
            'FROM fw_grid_featured x '
            'JOIN matches m ON m.id=x.match_id LEFT JOIN odds_asian o '
            "ON o.match_id=x.match_id AND o.label='closing' "
            'AND o.company_id=12 WHERE x.result IS NULL '
            'AND m.home_score IS NOT NULL').fetchall()
        n = 0
        for g, mid, d, shc, sgv, chc, cgv, hs, aws in rows:
            if d not in ('up', 'down'):
                continue
            hc, gv = _stored_first(shc, sgv, chc, cgv)
            r = pick_result(hc, gv, hs, aws)
            if r is None:
                continue
            res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up')
                                        else 'L')
            conn.execute(
                'UPDATE fw_grid_featured SET result=?, settled_at=? '
                'WHERE grid=? AND match_id=?', (res, now, g, mid))
            n += 1
        if n:
            out['fw_grid_featured'] = n
            conn.commit()
    except sqlite3.Error:
        pass
    # fw8s_log：重用現有實作
    try:
        n = _fw8s_settle(conn)
        if n:
            out['fw8s_log'] = n
    except Exception:
        traceback.print_exc(limit=3)
    # 引擎自帶結算（語义同各頁一致）
    try:
        import fw8x
        n = fw8x.settle(conn)
        if n:
            out['fw8x_log'] = n
    except Exception:
        traceback.print_exc(limit=3)
    try:
        import w88_engine
        n = w88_engine.settle(conn)
        if n:
            out['w88_log'] = n
    except Exception:
        traceback.print_exc(limit=3)
    try:
        import newjx_engine
        n = newjx_engine.settle(conn)
        if n:
            out['newjx_log'] = n
    except Exception:
        traceback.print_exc(limit=3)
    # 好波（語義同 api_lg_hit_rates lazy 結算：走＝P 唔計入分母）
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS hb_pick_log(
            match_id INTEGER PRIMARY KEY, league TEXT, side TEXT, rate REAL,
            line REAL, added_at TEXT, result TEXT, settled_at TEXT)''')
        cur = conn.execute(
            "UPDATE hb_pick_log AS h SET result = CASE "
            "WHEN m.home_score + m.away_score = h.line THEN 'P' "
            "WHEN (m.home_score + m.away_score > h.line) = (h.side = '大') "
            "THEN 'W' ELSE 'L' END, settled_at = ? "
            'FROM matches m WHERE h.match_id = m.id '
            'AND m.home_score IS NOT NULL AND h.result IS NULL', (now,))
        if cur.rowcount:
            out['hb_pick_log'] = cur.rowcount
    except sqlite3.Error:
        pass
    # feat_hourly（語義同 _result_settle.py）：用每行自己影低嘅 ah_handicap/
    # ah_giver（嗰個鐘頭嘅盤＝落注時盤）；NULL 先對 closing
    try:
        rows = conn.execute(
            'SELECT x.id, x.match_id, x.direction, x.ah_handicap, x.ah_giver '
            'FROM feat_hourly x JOIN matches m ON m.id=x.match_id '
            'WHERE x.settled=0 AND m.home_score IS NOT NULL').fetchall()
        n = 0
        for rid, mid, direction, shc, sgv in rows:
            if direction not in ('up', 'down'):
                continue
            r = conn.execute(
                'SELECT o.handicap, o.giver, m.home_score, m.away_score '
                'FROM matches m LEFT JOIN odds_asian o ON o.match_id=m.id '
                "AND o.label='closing' AND o.company_id=12 "
                'WHERE m.id=?', (mid,)).fetchone()
            if not r or r[2] is None:
                continue
            hc, gv = _stored_first(shc, sgv, r[0], r[1])
            pr = pick_result(hc, gv, r[2], r[3])
            if pr is None:
                continue
            res = 'P' if pr == 'P' else ('W' if (pr == 'A')
                                         == (direction == 'up') else 'L')
            conn.execute('UPDATE feat_hourly SET settled=1, result=? '
                         'WHERE id=?', (res, rid))
            n += 1
        if n:
            out['feat_hourly'] = n
    except sqlite3.Error:
        pass
    if out:
        try:
            conn.commit()
        except sqlite3.Error:
            pass
    return out


def _settle_finished_tools_bg():
    """_settle_finished_tools 嘅背景包裝：自己開 conn、鎖重試，
    唔阻呼叫方（掃描/更新流程行完即走）。"""
    def _bg():
        for _att in range(3):
            try:
                conn = db()
                try:
                    r = _settle_finished_tools(conn)
                finally:
                    conn.close()
                if r:
                    print('[settle-all] ' + ', '.join(
                        '%s %d' % kv for kv in r.items()), flush=True)
                _bump_data_epoch()   # 2026-10-26：結算完，全部快取作廢
                return
            except sqlite3.OperationalError:
                time.sleep(5)
            except Exception:
                traceback.print_exc(limit=3)
                return
    threading.Thread(target=_bg, daemon=True).start()


def _crawl_live_scores(limit_leagues=6, budget=50):
    """2026-10-22「重新掃瞄掣加即時爬進行中賽果」：開賽 0-3 小時仲未有
    比分嘅場次，按聯賽即場重抓現行賽季檔——同賽果巡邏同一條 save_season
    通道（真係寫入 matches.home_score）。限聯賽數同秒數，best-effort，
    爬唔到都唔會影響後續掃描。回傳成功補到賽果嘅聯賽數。"""
    try:
        conn = db()
        try:
            rows = conn.execute(
                'SELECT DISTINCT s.titan_id FROM matches m '
                'JOIN seasons s ON s.id=m.season_id '
                'WHERE m.home_score IS NULL '
                "AND m.kickoff > datetime('now','+8 hours','-3 hours') "
                "AND m.kickoff <= datetime('now','+8 hours') "
                'LIMIT ?', (limit_leagues,)).fetchall()
        finally:
            conn.close()
        if not rows:
            return 0
        crawler, fetcher = get_crawler()
        conn = db()
        try:
            fixed = 0
            t0 = time.time()
            for (tid,) in rows:
                if time.time() - t0 > budget:
                    break
                try:
                    seasons = crawler.get_season_list(fetcher, tid)
                    if not seasons:
                        continue
                    prefix = crawler.resolve_prefix(fetcher, tid)
                    if not prefix:
                        continue
                    url = (f'https://zq.titan007.com/jsData/matchResult/'
                           f'{seasons[0]}/{prefix}.js?version=1')
                    text = fetcher.get(url)
                    if not text or text.lstrip().startswith('<'):
                        continue
                    parsed = crawler.parse_season_js(text)
                    crawler.save_season(conn, tid, seasons[0], 1, parsed)
                    fixed += 1
                except Exception:
                    pass
            if fixed:
                try:
                    conn.commit()
                except sqlite3.Error:
                    pass
            return fixed
        finally:
            conn.close()
    except Exception:
        traceback.print_exc(limit=3)
        return 0


def _scan_extras():
    """所有「重新掃描」掣嘅公共尾巴（2026-10-22 用戶規格）：
    ①先即時爬進行中賽事賽果 ②主動結算全部精選表已完場列。
    背景執行、best-effort——唔阻掃描本身，唔會令掣冇反應。"""
    def _bg():
        try:
            _crawl_live_scores()
        except Exception:
            traceback.print_exc(limit=3)
        _settle_finished_tools_bg()
    threading.Thread(target=_bg, daemon=True).start()


# ============ 一鍵更新賽事（賽果＋新場次＋已存在場次嘅盤口及賠率全部刷新） ============
# 手動掣 / 每次開 APP 自動觸發（30 分鐘內只會自動跑一次）
_update_state = {'running': False, 'phase': '', 'last_done': 0.0,
                 'last_result': None, 'error': None}
UPDATE_THROTTLE_SEC = 1800
# 雲端（Render）伺服器 IP 被 titan007 長期封鎖（datacenter IP），佢哋直接爬唔到數據；
# 雲端數據靠本機每日朝早推送（更新雲端數據.py → GitHub Release → 重新部署）。
# DISABLE_UPDATE=1 就唔再每 30 秒白探主機——否則頂部常駐「上次更新失敗」警告，
# 誤導用戶以為 APP 壞（2026-09-30 用戶投訴）。
DISABLE_UPDATE = os.environ.get('DISABLE_UPDATE') == '1'


def _update_worker():
    import crawler
    with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
        cfg = json.load(f)
    # 限流跟 config.json 分層休息方案（2026-09-29），唔再禁用休息
    conn = sqlite3.connect(DB_PATH, timeout=180)
    ok = False
    # 開工前快測數據主機；死緊就即刻收工，唔好逐場捱逾時（watchdog 會自動重試）
    if not crawler.data_host_probe():
        _update_state['error'] = (
            '連唔到 titan007 數據主機——多數係封咗你而家嘅 IP（例如爬蟲用得多），'
            '唔係 APP 壞。每 30 秒自動重試；換個網絡（手機 USB 分享/重開 router '
            '攞新 IP）即自動恢復，現有數據唔受影響照用')
        _update_state['running'] = False
        _update_state['phase'] = ''
        _update_state['last_done'] = time.time()
        print('[update] 數據主機中斷，本次更新跳過（自動重試緊）', flush=True)
        conn.close()
        return
    try:
        # ① 賽果＋新場次＋最近三日補盤口（各聯賽現行賽季檔）
        st = crawler.recent_update(
            conn, cfg, days=3,
            progress=lambda m: _update_state.update(phase=m))
        _update_state['last_result'] = st
        # ② 已存在場次嘅盤口＋賠率全部強制刷新：
        #    未開賽（最近開賽排先）＋ 進行中（開賽後 4 小時內、未有賽果）——
        #    進行中場次尾盤會隨賽事變化，必須一齊刷新（用戶 2026-09-28 指示）
        todo = conn.execute(
            'SELECT m.id, m.kickoff FROM matches m '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff >= datetime('now','+8 hours','-4 hours') "
            'AND EXISTS(SELECT 1 FROM odds_asian o '
            'WHERE o.match_id=m.id AND o.company_id=12) '
            'ORDER BY m.kickoff').fetchall()
        fetcher = crawler.Fetcher(conn, cfg)
        n_ok = n_fail = 0
        outage_streak = 0   # 連續斷線計數：代理池太殘時唔好逐場捱逾時拖死成條線
        for i, (mid, ko) in enumerate(todo):
            if _update_state.get('stop'):
                print('[update] 🛑 收到停止指令——完成手頭呢場後收工'
                      '（已搞 %d/%d 場）' % (i, len(todo)), flush=True)
                break
            _update_state['phase'] = (
                f'刷新已存在場次嘅盤口及賠率 {i + 1}/{len(todo)}')
            try:
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    n_ok += 1
                    outage_streak = 0
                else:
                    n_fail += 1
                    outage_streak = (outage_streak + 1
                                     if getattr(fetcher, 'last_outage', False)
                                     else 0)
            except Exception:
                n_fail += 1
                outage_streak += 1
            if outage_streak >= 12:
                print('[update] 連線連續逾時 12 場，中止本次更新'
                      '（代理池太差，半小時後自動重試）', flush=True)
                break
        st['odds_refresh'] = f'{n_ok} 場成功 / {n_fail} 場失敗'
        # ②b 未開賽但完全冇盤口嘅場次（未來72小時內）補爬——
        # 舊快照/新插入場次可能從未抓過賠率，冇呢步佢哋永遠冇盤口顯示
        todo2 = conn.execute(
            'SELECT m.id, m.kickoff FROM matches m '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff >= datetime('now','+8 hours') "
            "AND m.kickoff <= datetime('now','+8 hours','+72 hours') "
            'AND NOT EXISTS(SELECT 1 FROM odds_asian o '
            'WHERE o.match_id=m.id AND o.company_id=12) '
            # 有咗 Crown 後備嘅都唔再重複爬
            'AND NOT EXISTS(SELECT 1 FROM odds_asian o3 '
            'WHERE o3.match_id=m.id AND o3.company_id=3) '
            'ORDER BY m.kickoff').fetchall()
        n_fill_ok = n_fill_fail = n_fill_crown = 0
        outage_streak2 = 0
        for i, (mid, ko) in enumerate(todo2):
            _update_state['phase'] = (
                f'補爬缺少盤口嘅場次 {i + 1}/{len(todo2)}')
            try:
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    n_fill_ok += 1
                    outage_streak2 = 0
                elif crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 3):
                    # 易胜博未開盤 → Crown(3) 暫代（顯示用，V3 分析照舊 12）；
                    # 記入 line12_track：之後每日用 12BET 再試直至成功
                    n_fill_crown += 1
                    _line12_record(conn, mid)
                    outage_streak2 = 0
                else:
                    n_fill_fail += 1
                    outage_streak2 = (outage_streak2 + 1
                                      if getattr(fetcher, 'last_outage', False)
                                      else 0)
            except Exception:
                n_fill_fail += 1
                outage_streak2 += 1
            if outage_streak2 >= 12:
                print('[update] 補爬階段連線連續逾時 12 場，中止本次更新'
                      '（代理池太差，半小時後自動重試）', flush=True)
                break
        st['odds_fill'] = (f'{n_fill_ok} 場成功 / {n_fill_fail} 場失敗'
                           + (f' / {n_fill_crown} 場 Crown 後備'
                              if n_fill_crown else ''))
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
    if DISABLE_UPDATE:
        return {'ok': False, 'disabled': True,
                'error': '雲端唔做全量更新（太重）——用「⚡ 即時更新全部」或窗口掣，'
                         '盤口賠率經代理池即時抓；賽果每 15 分鐘自動補'}
    if _update_state['running']:
        return {'ok': False, 'running': True, 'error': '更新進行中'}
    _update_state['stop'] = False
    # 跨工作互鎖（2026-10-02）：窗口／即時更新進行緊就唔好開全量
    if _win_state['running'] or _fbatch_state['running']:
        return {'ok': False, 'running': True,
                'error': '另一個更新進行中，等佢完成先'}
    if (auto and _update_state['last_done']
            and time.time() - _update_state['last_done'] < UPDATE_THROTTLE_SEC):
        return {'ok': True, 'skipped': True}
    _update_state.update(running=True, phase='準備中…', error=None,
                         last_result=None)
    _t = threading.Thread(target=_update_worker, daemon=True)
    _JOB_THREADS['全量更新'] = _t
    _t.start()
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
    '24h': ("AND m.kickoff <= datetime('now','+8 hours','+24 hours')", '未來24小時'),
    '30m': ("AND m.kickoff <= datetime('now','+8 hours','+30 minutes')", '未來30分鐘'),
    '10m': ("AND m.kickoff <= datetime('now','+8 hours','+10 minutes')", '未來10分鐘'),
}


def _window_update_worker(win):
    import crawler
    with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
        cfg = json.load(f)
    # 同 fbatch（2026-10-24）：人手觸發要快閃失敗，唔好逐場長等重試——
    # 全死環境每個 get() 最壞 ~11 分鐘，預設 3 次重試會將窗口掣變僵屍
    cfg['blocked_retry_times'] = 1
    # 限流跟 config.json 分層休息方案（2026-09-29），唔再禁用休息
    cond, wname = WIN_SQL[win]
    conn = sqlite3.connect(DB_PATH, timeout=180)
    try:
        todo = conn.execute(
            'SELECT m.id, m.kickoff FROM matches m '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff >= datetime('now','+8 hours') " + cond + ' '
            'ORDER BY m.kickoff').fetchall()
        if _win_state.get('stop'):
            print('[win] 🛑 收到停止指令——收工', flush=True)
            return
        _win_state.update(total=len(todo), done=0, ok=0, fail=0)
        fetcher = crawler.Fetcher(conn, cfg)
        fail_streak = 0
        for i, (mid, ko) in enumerate(todo):
            _win_state['phase'] = f'更新{wname}盤口及賠率 {i + 1}/{len(todo)}'
            try:
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    _win_state['ok'] += 1
                    fail_streak = 0
                else:
                    _win_state['fail'] += 1
                    fail_streak += 1
            except Exception:
                _win_state['fail'] += 1
                fail_streak += 1
            _win_state['done'] += 1   # 舊制漏咗——進度條一直 0/N
            if fail_streak >= 5:
                # 同 fbatch 斷路器：5 連敗＝真封鎖——收工留清楚訊息，
                # 唔再逐場捱晒變僵屍（殭屍會鎖死「即時更新」掣）
                _win_state['error'] = (
                    '連頭 5 場都爬唔到——titan007 而家真係封緊。'
                    '等 30 秒週期重試／換網絡後再撳')
                _scanlog('窗口更新遇真封鎖（5 連敗）——已收工')
                print('[win-update] 5 連敗收工', flush=True)
                break
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
        # 2026-10-25 即時同步：窗口更新搞掂→冷卻制即推（5 分鐘內多次窗口
        # 更新淨推一次）
        _sync_push_now()
    # 注意：唔喺度重掃精選——快速掣只係更新指定窗口嘅盤口，掃精選係另一個掣嘅工作；
    # 否則撳「更新未來10分鐘」都會觸發全庫精選重掃（用戶投訴「都係全部掃」嘅根因）。


def do_update_window(win):
    """人手撳『更新未來24／30／10』：後台起線程逐場抓，立即回應 started"""
    if win not in WIN_SQL:
        return {'ok': False, 'error': '未知窗口'}
    if _win_state['running']:
        return {'ok': False, 'running': True, 'error': '窗口更新進行中'}
    # 跨工作互鎖：全量更新／即時更新進行緊都唔好開窗口爬——並行爬取會
    # 同時搶代理池＋sqlite 寫入，512MB 實機同 0.5C CPU 都頂唔順（2026-10-02）
    if _update_state['running'] or _fbatch_state['running']:
        return {'ok': False, 'running': True,
                'error': '另一個更新進行中，等佢完成先'}
    # 2026-10-25 修正：重置停止旗標——舊制撳過「■停止」後 stop=True 殘留，
    # 之後每次窗口更新喺 worker 開工即退，掣永久失效（app 重啟先復活）。
    _win_state.update(running=True, window=win, error=None,
                      phase='準備中…', done=0, total=0, stop=False)
    _t = threading.Thread(target=_window_update_worker, daemon=True,
                          args=(win,))
    _JOB_THREADS['窗口更新'] = _t
    _t.start()
    return {'ok': True, 'started': True}


def win_status():
    d = dict(_win_state)
    d['window_name'] = WIN_SQL.get(d['window'], ('', ''))[1]
    return d


# ============ ⚡ 一鍵即時更新：主頁可見場次全部強制重抓最新盤口及賠率 ============
# 用戶 2026-10-01 要求：賠率/盤口要有鍵即刻更新＋每場顯示最後更新時間。
# 同窗口更新分別：呢個係「畫面上列出的場次全部強制重抓最新」，已經有賠率嘅都會刷新；
# 窗口更新只係補指定時段，唔會重抓已有嘅。
_fbatch_state = {'running': False, 'phase': '', 'done': 0, 'total': 0,
                 'ok': 0, 'fail': 0, 'last': None, 'error': None,
                 'reval': None, 'ou_ok': 0, 'x2_ok': 0}


_FBATCH_PEND = {'hours': None, 'ts': 0.0}
_load_scan_state()   # 2026-10-25 根治：重啟讀返排隊＋熔斷登記（用戶指示 #4）


def _fetch_batch_worker(hours, ids=None):
    """ids（2026-10-24）：手機直爬核對後，淨重抓『唔一致/本機缺失』嘅場——
    手機數據照舊唔直接寫庫（用戶 10-02 定調），由伺服器代理池重抓落庫，
    跟住行同一套資格重檢＋結算。hours 模式照舊＝成個時間窗。"""
    import crawler
    with open(os.path.join(crawler.BASE_DIR, 'config.json'), encoding='utf-8') as f:
        cfg = json.load(f)
    # 人手觸發要快閃失敗，唔好逐場長等重試
    cfg['blocked_retry_times'] = 1
    conn = sqlite3.connect(DB_PATH, timeout=180)
    try:
        # 開工前快測數據主機；死緊即刻收工俾清楚訊息（同 _update_worker 邏輯）
        # 2026-10-24 突破封鎖：探測改『軟啟動』——12 個代理抽樣假陰性太高
        # （池 395 個部分燒咗，輪轉爬其實行得通，實測每小時經代理落
        # 273 筆）。探測只作警告照樣開爬；頭 5 場連滅先判真封鎖＋登記補跑。
        if not crawler.data_host_probe():
            print('[fbatch] 前置探測未過——照樣開爬（代理輪轉可能有活得）',
                  flush=True)
        if ids:
            marks = ','.join('?' * len(ids))
            todo = conn.execute(
                'SELECT m.id, m.kickoff FROM matches m '
                'WHERE m.id IN (%s) ORDER BY m.kickoff' % marks,
                tuple(ids)).fetchall()
        else:
            cap = "AND m.kickoff <= datetime('now','+8 hours', ?) " if hours else ""
            args = (f'+{hours} hours',) if hours else ()
            todo = conn.execute(
                'SELECT m.id, m.kickoff FROM matches m '
                'WHERE m.home_score IS NULL '
                "AND m.kickoff >= datetime('now','+8 hours') " + cap +
                'ORDER BY m.kickoff', args).fetchall()
        _fbatch_state.update(total=len(todo), done=0, ok=0, fail=0)
        fetcher = crawler.Fetcher(conn, cfg)
        ok_mids = []
        changed_mids = []
        fail_streak = 0
        for i, (mid, ko) in enumerate(todo):
            if _fbatch_state.get('stop'):
                print('[fbatch] 🛑 收到停止指令——完成手頭呢場後收工'
                      '（已搞 %d/%d 場）' % (i, len(todo)), flush=True)
                break
            _fbatch_state['phase'] = f'即時更新盤口及賠率 {i + 1}/{len(todo)}'
            got = False
            try:
                before = _closing_now(conn, mid)
                if crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12):
                    got = True
                elif not conn.execute(
                        'SELECT 1 FROM odds_asian WHERE match_id=? AND company_id=12',
                        (mid,)).fetchone()                         and crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 3):
                    # 易胜博未開盤 → Crown(3) 後備
                    got = True
            except Exception:
                got = False
            if got:
                _fbatch_state['ok'] += 1
                ok_mids.append(mid)
                fail_streak = 0
                # （2026-10-25 after_odds_update 已刪除：門檻後備路徑零價值；
                #  watcher 無條件覆蓋每次成功爬取）
                # 2026-10-24 用戶規格「更新所有盤口」：讓球搞掂→順手重抓
                # 大小+歐盤（同一 changeDetail 家族頁、同一 Fetcher；失敗
                # 唔影響讓球結果，唔計入 fail——兩個市場獨立計數顯示）
                try:
                    ko_dt = dt.datetime.strptime(ko, '%Y-%m-%d %H:%M')
                    import crawl_extra
                    for _mk in ('ou', '1x2'):
                        try:
                            if crawl_extra.task_odds_page(
                                    conn, fetcher, mid, ko_dt, _mk) is True:
                                _fbatch_state[
                                    'ou_ok' if _mk == 'ou' else 'x2_ok'] += 1
                        except Exception:
                            pass
                except Exception:
                    pass
                if _line_changed(before, _closing_now(conn, mid)):
                    changed_mids.append(mid)
            else:
                _fbatch_state['fail'] += 1
                fail_streak += 1
                if _fbatch_state.get('stop'):
                    fail_streak = 0   # 人手停止唔算熔斷，唔登記補跑
                if fail_streak >= 5:
                    # 頭 5 場連滅＝真封鎖（唔係輪轉陣痛）——收工＋登記補跑
                    _FBATCH_PEND['hours'] = hours
                    _FBATCH_PEND['ts'] = time.time()
                    _save_scan_state()
                    _fbatch_state['error'] = (
                        '連頭 5 場都爬唔到——titan007 而家真係封緊。'
                        '已登記：復活後自動補跑，唔使再撳')
                    _scanlog('⚡ 更新賠率遇真封鎖（5 連敗）——已登記自動補跑')
                    print('[fbatch] 5 連敗收工（已登記補跑）', flush=True)
                    break
            _fbatch_state['done'] += 1
        _fbatch_state['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if fail_streak < 5:
            _fbatch_state['error'] = None
            print(f"[fbatch] 完成：{_fbatch_state['ok']} 成 / "
                  f"{_fbatch_state['fail']} 敗 / 共 {len(todo)} 場", flush=True)
        else:
            # 熔斷收工：保留已設嘅 error/補跑登記（2026-10-24 修正——
            # 舊制跌落嚟會被 error=None 洗走，仲印『完成』誤導）
            print('[fbatch] 熔斷收工：%d 成 / %d 敗（總 %d）——已登記補跑'
                  % (_fbatch_state['ok'], _fbatch_state['fail'], len(todo)),
                  flush=True)
        # 2026-10-22 用戶規格「更新後要有動作同反應」＋2026-10-24 收窄：
        # 資格重檢淨做「尾盤有重大變動」＋「從未評估過」嘅場——冇變動嘅
        # 場重評結果都一樣（規則確定性），慳返 sweep 上限名額俾真係要
        # 重檢嘅場（舊制 ok_mids 全上，48h 窗一爆 300 場 cap 就靜默甩尾）。
        _fbatch_state['phase'] = '重檢各精選系統資格…'
        try:
            sweep_mids = list(changed_mids)
            for mid in ok_mids:
                if mid not in changed_mids and _never_evaluated(conn, mid):
                    sweep_mids.append(mid)
            _fbatch_state['reval'] = _requalify_sweep(sweep_mids)
            print('[fbatch] 資格重檢（變盤%d＋新評估%d）：%s'
                  % (len(changed_mids), len(sweep_mids) - len(changed_mids),
                     _fbatch_state['reval']), flush=True)
        except Exception:
            traceback.print_exc(limit=3)
            _fbatch_state['reval'] = None
        # 順手主動結算（完場場次嘅結果即刻寫入全部工具表）
        try:
            _settle_finished_tools_bg()
        except Exception:
            pass
        # 2026-10-25：批量更新埋好波——變盤場次有 OU 線嘅即刻重算（快取保護）
        try:
            _hb_refresh_mids(changed_mids)
        except Exception:
            pass
        # 2026-10-25 即時同步：用戶撳嘅批量搞掂→即刻推上雲（唔使等 30 分鐘）
        _sync_push_now(force=True)
    except Exception:
        traceback.print_exc()
        _fbatch_state['error'] = traceback.format_exc(limit=3)
    finally:
        conn.close()
        _fbatch_state['running'] = False
        _fbatch_state['phase'] = ''


def do_fetch_batch(hours=0, ids=None):
    """人手撳『⚡ 即時更新全部』：可見場次全部重抓最新盤口賠率（後台線程）。
    2026-10-02 起雲端都用到：titan007 封嘅係 IP 段（屋企＋datacenter），
    代理池嘅公共代理任何來源都行得通——proxy_pool.json 隨映像焗入，
    Fetcher 自動行代理通道，所以唔再按 DISABLE_UPDATE 拒絕。
    ids（2026-10-24）：指定場次清單（手機直爬 mismatch 通道），跳時間窗。"""
    if _fbatch_state['running']:
        return {'ok': False, 'running': True, 'error': '即時更新進行中'}
    if _win_state['running'] or _update_state['running']:
        return {'ok': False, 'running': True, 'error': '另一個更新進行中，等佢完成先'}
    if _unified_scan['running']:
        return {'ok': False, 'running': True,
                'error': '統一掃描進行中，等佢完成先（佢會順便重檢資格）'}
    hours = max(0, int(hours or 0))
    if ids:
        try:
            ids = [int(x) for x in ids][:500]
        except (TypeError, ValueError):
            ids = None
        if ids is not None and not ids:
            ids = None
    _fbatch_state.update(running=True, error=None, phase='準備中…',
                         done=0, total=0, reval=None, ou_ok=0, x2_ok=0,
                         stop=False)
    _t = threading.Thread(target=_fetch_batch_worker, daemon=True,
                          args=(hours, ids))
    _JOB_THREADS['即時更新批量'] = _t
    _t.start()
    return {'ok': True, 'started': True}


def fbatch_status():
    d = dict(_fbatch_state)
    return d


# ============ 🔄 「重抓重算」掣（2026-10-21 用戶指示） ============
# 一掣做晒：① crawl16 重抓 asian/OU 缺口 → ② crawl_mk 重抓歐盤/半場/角球
# → ③ requalify_after_backfill.py 重算八個精選系統資格
# （featured 家族＋格 7/8/12/8s＋新精選＋88W＋8s/8X 回測庫重建＋對比報告）。
# 只限本機：雲端冇 crawl16/proxy_pool（偵測到就話俾用戶知）。
_regrab_state = {'running': False, 'step': '', 'phase': '',
                 'started': None, 'last': None, 'error': None, 'mode': None}


def _regrab_worker():
    import subprocess
    st = _regrab_state
    base = os.path.dirname(os.path.abspath(__file__))      # screen_app/（雲端＝repo 根）
    root = os.path.dirname(base)                            # 工作區根
    os.makedirs(os.path.join(root, 'logs'), exist_ok=True)
    if st.get('mode') == 'cloud':
        # 雲端：P1/P1b 千祈唔好用 subprocess——子進程會再載一次 v2/v3 數據池
        # （約230MB），app 本體＋子進程加埋爆 512MB 上限，10-22 實測 OOM
        # 冧到 502。改為喺 app 進程內行（pool 同 scan job 共用一份，
        # scan job 日日咁載，實證裝得落）。P2/P3 回查腳本唔載 pool 好輕，
        # 照用佢哋內置嘅 subprocess 隔離。P4/P5 回測庫重建太重，雲端跳過。
        st.update(step='requalify',
                  phase='☁️ 雲端重算八個精選系統資格（P1→P1b→P2→P3→P6；'
                        '歷史缺口重抓同回測庫重建要喺本機撳）')
        logf = open(os.path.join(root, 'logs', 'regrab_requalify.log'),
                    'a', encoding='utf-8')
        logf.write('\n===== 重抓重算（雲端） %s =====\n'
                   % time.strftime('%Y-%m-%d %H:%M:%S'))
        logf.flush()
        logf.close()   # P2/P3 子進程會自己開返寫
        R = None
        try:
            # 等開機 warmup 起完數據池先好開始——開機即刻撈會同 warmup
            # 同時起兩座池，512MB 唔夠分（10-22 兩次 502 就係咁冧嘅）；
            # 池係模組級共用，warmup 起完呢邊即刻用得，零額外記憶體。
            t_wait = time.time()
            while not _pool_ready['done'] and time.time() - t_wait < 900:
                st['phase'] = '☁️ 等緊開機數據池就緒（通常幾分鐘）…'
                time.sleep(10)
            if base not in sys.path:
                sys.path.insert(0, base)
            import requalify_after_backfill as R
            R.CLOUD = True
            before = R.before_stats()
            R.state(status='running', phase='START', error=None,
                    mode='cloud',
                    before=json.dumps(before, ensure_ascii=False))
            R.phase_p1()
            R.phase_p1b()
            R.phase_p2()
            R.phase_p3()
            R.phase_p6(before)
            R.state(status='done', phase='DONE')
            st['rc_requalify'] = 0
        except Exception:
            traceback.print_exc()
            st['error'] = traceback.format_exc(limit=3)
            try:
                if R:
                    R.state(status='error', error=st['error'])
            except Exception:
                pass
        st.update(running=False, phase='',
                  last=time.strftime('%Y-%m-%d %H:%M:%S'))
        print('[regrab] 雲端重算完成 rc=%s'
              % {k: v for k, v in st.items() if k.startswith('rc_')},
              flush=True)
        return
    steps = [
        ('crawl16', '① 重抓 asian/OU 缺口（斷點續跑）',
         [sys.executable, 'crawl16.py', '--proxy-only'], root),
        ('crawl_mk', '② 重抓 歐盤/半場/角球（斷點續跑）',
         [sys.executable, 'crawl_mk.py', '--proxy-only'], root),
        ('requalify', '③ 重算八個精選系統資格（P1–P6）',
         [sys.executable, 'requalify_after_backfill.py'], base),
    ]
    for key, label, cmd, cwd in steps:
        st.update(step=key, phase=label)
        try:
            logf = open(os.path.join(root, 'logs', 'regrab_%s.log' % key),
                        'a', encoding='utf-8')
            logf.write('\n===== 重抓重算 %s =====\n' %
                       time.strftime('%Y-%m-%d %H:%M:%S'))
            logf.flush()
            # 2026-10-26 三源輪換正式啟用（用戶規格：每100個requests轉下一個
            # 來源，titan→7M→捷報順序輪換）——crawl16 行內嘅 SourceRotator 見
            # 到 FA_ROTATE=1 先會介入 fetch（預設 off 唔影響其他啟動方式）。
            env = dict(os.environ, FA_ROTATE='1')
            p = subprocess.Popen(cmd, cwd=cwd, stdout=logf,
                                 stderr=subprocess.STDOUT, env=env)
            while p.poll() is None:
                time.sleep(3)
            logf.close()
            st['rc_' + key] = p.returncode
        except Exception:
            traceback.print_exc()
            st['error'] = traceback.format_exc(limit=3)
            break
    st.update(running=False, phase='',
              last=time.strftime('%Y-%m-%d %H:%M:%S'))
    print('[regrab] 完成 rc=%s' % {k: st.get('rc_' + k)
                                   for k in ('crawl16', 'crawl_mk',
                                             'requalify')}, flush=True)


def do_regrab_recompute():
    """人手撳『🔄 重抓重算』。本機＝重抓缺口＋重算；雲端＝重算資格（P1–P3+P6）。"""
    if _regrab_state['running']:
        return {'ok': False, 'running': True, 'error': '重抓重算進行中'}
    base = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(base)
    local_mode = os.path.exists(os.path.join(root, 'crawl16.py'))
    if _win_state['running'] or _update_state['running'] \
            or _fbatch_state['running']:
        return {'ok': False, 'running': True,
                'error': '另一個更新進行中，等佢完成先'}
    _regrab_state.update(running=True, error=None, step='',
                         phase='準備中…',
                         mode=('local' if local_mode else 'cloud'),
                         started=time.strftime('%Y-%m-%d %H:%M:%S'))
    threading.Thread(target=_regrab_worker, daemon=True).start()
    return {'ok': True, 'started': True, 'mode': _regrab_state['mode']}


def regrab_status():
    d = dict(_regrab_state)
    try:
        rs = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'logs', 'requalify_state.json')
        if os.path.exists(rs):
            d['requalify'] = json.load(open(rs, encoding='utf-8'))
    except Exception:
        pass
    return d



# ============ 📱 手機直爬「核對」端點（2026-10-02 用戶定調） ============
# 架構：本機係數據主人（獨立爬＋每日 06:17 推送上雲端）；手機自己爬嘅盤口賠率
# **唔寫入任何庫**（唔使亦唔應該更新本機／雲端）——呢個端點只做「核對」：
# 收到手機爬嘅七時點後，同雲端（＝本機鏡像）嘅尾盤比較，結果連裝置名寫入
# crawl_log（📋 工作記錄見到），並回傳 agree 俾手機即場顯示 ✓／⚠。
# 權責：唔一致時**以本機為準**（分析/推算全部行雲端本機數據；手機爬嘅
# 只係自己熒幕嘅即時顯示）——本機每日推送會自動覆蓋雲端，權責天然兌現。
_PHONE_LABELS = ('initial', 'pre_4h', 'pre_30m', 'pre_15m', 'pre_10m',
                 'pre_5m', 'closing')


def do_phone_odds(body):
    """核對手機直爬結果 vs 雲端（本機鏡像）尾盤。只記錄，唔寫 odds_asian。"""
    try:
        mid = int(body.get('id') or 0)
        if not mid:
            return {'ok': False, 'error': '缺少場次 id（手機直爬協議欄位）'}
        device = str(body.get('device') or '手機')[:30]
        snaps = body.get('snaps') or {}
        close = snaps.get('closing') or snaps.get('initial') or {}
        conn = db()
        try:
            srv = conn.execute(
                "SELECT handicap, giver, home_odds, away_odds FROM odds_asian "
                "WHERE match_id=? AND label='closing' "
                "ORDER BY CASE company_id WHEN 12 THEN 0 ELSE 1 END LIMIT 1",
                (mid,)).fetchone()
            agree = None
            try:
                hv = float(close.get('hv'))
                ho = float(close.get('ho'))
                ao = float(close.get('ao'))
                if srv and srv[0] is not None:
                    s_hv = (-srv[0] if srv[1] == 'home'
                            else (0.0 if not srv[1] else srv[0]))
                    agree = (abs(hv - s_hv) < 0.01 and srv[2] is not None
                             and srv[3] is not None
                             and abs(ho - srv[2]) < 0.011
                             and abs(ao - srv[3]) < 0.011)
            except (TypeError, ValueError):
                agree = None
            mark = '✓一致' if agree else ('⚠與本機不同（以本機為準）'
                                          if agree is False else '本機未有尾盤')
            try:
                import crawler
                crawler.log(conn, 'INFO',
                            f'📱 {device} 手機直爬核對場次 {mid}：{mark}')
            except Exception:
                pass
        finally:
            conn.close()
        phone_push_note(device, 1 if agree else 0)
        return {'ok': True, 'agree': agree, 'checked': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


_phone_push = {'ts': 0.0, 'device': '', 'total': 0}   # 狀態列顯示「邊個做緊」


def do_phone_odds_debug(body):
    """診斷（2026-10-10）：手機直爬全部「未開盤/失敗」——攞到手機實際收到嘅
    頁面頭 600 字／原生錯誤，寫入 crawl_log 供追查（titan007 改頁面 vs IP 被餵空殼）。"""
    try:
        mid = body.get('id')
        device = str(body.get('device') or '手機')[:30]
        head = str(body.get('head') or '')[:600]
        ln = body.get('len')
        err = body.get('err')
        conn = db()
        try:
            import crawler
            crawler.log(conn, 'WARN',
                        '📱DEBUG %s 場次%s len=%s err=%s 頁首: %s'
                        % (device, mid, ln, err, head[:300]))
        finally:
            conn.close()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def phone_push_note(device, written):
    _phone_push['ts'] = time.time()
    _phone_push['device'] = str(device)[:30]
    _phone_push['total'] += int(written or 0)


# ============ V2 背景排程：每日 12 時起每 2 小時自動更新未來 24 小時盤口直至尾盤 ============
# （12/14/16/18/20/22/24 時；賽果補抓沿用 15 分鐘巡邏。手機 APP 閂咗都會行——
#   排程喺 Render 伺服器端，唔係手機端。）
_sched_state = {'last_mark': '', 'runs': 0, 'last_error': None}


def _v2_sched_job():
    import datetime as dt
    while True:
        time.sleep(300)   # 每 5 分鐘睇一次
        try:
            now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
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


def _cloud_overnight_job():
    """2026-10-20：雲端凌晨 00–12 點冇排程爬蟲（V2 窗口更新 12 時先開），凌晨開賽
    場次嘅尾盤會停舊幾個鐘——用戶 10-20 00:39 截圖投訴「尾盤無更新到」嘅元兇
    （莫沙 vs 卡利亞里 00:30 開賽，雲端尾盤仲係 22 時嘅主讓平/半，市場其實
    已經走咗去平手）。呢個 loop 每 15 分鐘經代理池補抓未來 60 分鐘內開賽、
    尾盤舊過 10 分鐘嘅場次（通常幾場至十幾場，0.5C 頂得住）；do_fetch 內部
    已含尾盤變動→資格重檢。只喺雲端（DISABLE_UPDATE）行。"""
    while True:
        time.sleep(900)
        try:
            if not DISABLE_UPDATE:
                continue
            conn = db()
            rows = conn.execute(
                'SELECT m.id, m.kickoff FROM matches m '
                'WHERE m.home_score IS NULL '
                "AND m.kickoff >= datetime('now','+8 hours','-5 minutes') "
                "AND m.kickoff <= datetime('now','+8 hours','+60 minutes') "
                'AND EXISTS(SELECT 1 FROM odds_asian o '
                'WHERE o.match_id=m.id AND o.company_id=12) '
                "AND (SELECT MAX(o.fetched_at) FROM odds_asian o "
                "WHERE o.match_id=m.id AND o.company_id=12) "
                "< datetime('now','+8 hours','-10 minutes') "
                'ORDER BY m.kickoff LIMIT 40').fetchall()
            conn.close()
            for mid, _ko in rows:
                try:
                    do_fetch(mid, True)
                except Exception:
                    pass
                time.sleep(3)
        except Exception:
            pass


# ---------- 熱數據增量同步（2026-10-20）----------
# sync_delta.py（PC 每 30 分鐘）推 gzipped sqlite 過嚟，呢度逐表 INSERT OR REPLACE。
# 所有熱表都有自然主鍵（matches.id / 各表 match_id 或複合 PK），replace 冇重複。
SYNC_KEY = os.environ.get('SYNC_KEY') or 'fa-sync-2026'
# 白名單：只接受呢啲表，防人亂推野入嚟
_INGEST_WHITELIST = {
    'matches', 'match_derived', 'match_prestandings', 'match_goals',
    'match_corners', 'odds_asian', 'odds_ou', 'odds_ou_half', 'odds_ah_half',
    'odds_1x2', 'odds_1x2_half', 'odds_1x2_all', 'odds_corner',
    'v3_featured', 'v3_featured_log', 'featured', 'featured_log',
    'featured_z', 'featured_z_log', 'v1_featured', 'v1_featured_z',
    'fw_grid_featured', 'hb_pick_log', 'k3_log', 'newjx_log', 'w88_log',
    'fw8s_log', 'fw8x_log', 'fw8x_bt2026', 'fw8s_bt', 'feat_hourly',
    # 2026-10-21：參考表——精選8s 新引擎用 competitions 做 req_name→name_tc 映射
    'competitions', 'seasons', 'teams',
    # 2026-10-22：工具入選/剔走推送——本機寫入，經 delta 同步到雲端，
    # 手機版都收到（用戶：所有工具新入選＋勾走都要推送信息）
    'alerts',
    # 2026-10-26：第三/四源交叉表——歷史回填（16,224 場）同三源一致性
    # 數據都要上雲端（用戶：手機端都要更新）。全量上傳之外，delta 同步
    # 都帶埋佢哋，之後 ns/7M 新聯賽抓取手機先跟到。
    'xsrc_matches', 'xsrc_team_alias',
}

# 2026-10-25：細表「全量 replace」清單（同 sync_delta.py FULL_TABLES 對應）——
# delta 呢啲表係本地全量 dump，所以雲端要行 true replace：唔喺 delta PK
# 集合嘅雲端行一律 DELETE。舊制淨係 INSERT OR REPLACE，本機資格重檢剔走
# 咗嘅 pick 永遠殘留雲端，手機 pending 清單越積越多同本機對唔上（10-24
# 實證：featured 雲端 pending 64 vs 本機 8）。場次範圍表（matches/
# odds_*/match_*）同 feat_hourly 唔入此列——delta 只係 36h-72h 窗口，
# 誤用會清走雲端歷史行。
_DELTA_FULL_REPLACE = frozenset({
    'v3_featured', 'v3_featured_log', 'featured', 'featured_log',
    'featured_z', 'featured_z_log', 'v1_featured', 'v1_featured_z',
    'fw_grid_featured', 'hb_pick_log', 'k3_log', 'newjx_log',
    'w88_log', 'fw8s_log', 'fw8x_log', 'fw8x_bt2026', 'fw8s_bt',
    'tool_bt', 'competitions', 'seasons', 'teams',
})


# ============ 2026-10-22 工具入選/剔走推送（用戶規格） ============
# 邊度觸發：_eval_all 每次評估完（統一掃描/即時更新重檢/逐場重檢全部
# 經呢度）有「新增」→ 🆕 推送；有「移出」→ ❌ 推送。好波喺 _hb_log_pick
# 記錄時推送。12 小時內同場同工具同類型唔重複推（防閃爍洗版）。
_NOTIFY_N = [0]   # 本輪統一掃描產生嘅推送數（消化洪水用）


# ============ 2026-10-23 篩選進行中 Log（用戶：加/減場次時要知道做緊乜） ============
from collections import deque
_SCAN_LOG = {'seq': 0, 'items': deque(maxlen=300)}


def _scanlog(msg):
    """寫一條篩選進度日誌（記憶體滾動 300 條，面板實時拉取）。
    只記事件（新增/剔走/階段轉換），『保留』唔逐條記（結尾計數）。"""
    try:
        _SCAN_LOG['seq'] += 1
        _SCAN_LOG['items'].append({
            'id': _SCAN_LOG['seq'],
            'ts': time.strftime('%H:%M:%S'),
            'msg': msg})
    except Exception:
        pass


def _scanlog_match(conn, mid, acts):
    """一場嘅動作列表 → 一條人話日誌（🆕綠新增／❌紅剔走）。"""
    try:
        news = [a for a in acts if '新增' in a]
        rems = [a for a in acts if '移出' in a]
        if not news and not rems:
            return
        info = conn.execute(
            'SELECT ht.name_tc, at.name_tc FROM matches m '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
            (mid,)).fetchone()
        vs = '%s vs %s' % (info[0], info[1]) if info else '場次%d' % mid
        parts = []
        if news:
            parts.append('🆕新增 ' + '、'.join(news))
        if rems:
            parts.append('❌剔走 ' + '、'.join(rems))
        _scanlog('%s｜%s' % (vs, '｜'.join(parts)))
    except Exception:
        pass


def api_scanlog(after=0, limit=60):
    items = [x for x in _SCAN_LOG['items'] if x['id'] > after]
    if limit and len(items) > limit:
        items = items[-limit:]
    st = _unified_scan
    return {'running': bool(st['running']),
            'progress': {'done': st['done'], 'total': st['total'],
                         'added': st['added'], 'removed': st['removed'],
                         'phase': st['phase']},
            'items': items}


_ACT_TOOL = (('W', '精選W'), ('V2Z', '精選V2Z'), ('V2', '精選V2'),
             ('V1Z', '精選V1Z'), ('V1', '精選V1'),
             ('格7', '精選7'), ('格8s', '精選8s'), ('格8', '精選8'),
             ('格12', '精選12'), ('88W', '88W'), ('新精選', '新精選'),
             ('8X', '8X'), ('8s', '精選8s'))


def _act_tool_name(a):
    for p, n in _ACT_TOOL:
        if a.startswith(p):
            return n
    return None


def _notify_window_ok(conn, mid):
    """2026-10-25 用戶規格：彈出通知淨限主頁窗口（即時至未來7日）嘅未賽場次——
    歷史 backlog 重檢／遠期（>7日）場次嘅入選/剔走唔再彈通知（用戶投訴更新時
    彈出唔知邊日嘅賽事）。下界寬 1 小時：啱啱開賽未結束嘅場照通知；已完賽
    （有比分）唔彈——完場後嘅資格變動冇推送意義。場次唔喺庫（理論上唔會，
    異常情況）照彈——寧彈唔好漏。"""
    try:
        if conn.execute('SELECT 1 FROM matches WHERE id=?',
                        (mid,)).fetchone() is None:
            return True
        return conn.execute(
            "SELECT 1 FROM matches WHERE id=? AND home_score IS NULL "
            "AND kickoff >= datetime('now','+8 hours','-1 hours') "
            "AND kickoff <= datetime('now','+8 hours','+7 days')",
            (mid,)).fetchone() is not None
    except Exception:
        return True   # 查唔到唔阻塞通知（寧彈唔好漏）


def _notify_tool(conn, mid, acts):
    """將 _eval_all 嘅動作列表翻成推送（🆕入選／❌剔走）。best-effort：
    任何錯誤都唔影響掃描本體。同場同工具同類型 12 小時內去重。"""
    news = [a for a in acts if '新增' in a]
    rems = [a for a in acts if '移出' in a]
    if not news and not rems:
        return
    # 2026-10-25 用戶規格：窗口外（歷史/遠期）場次唔彈通知
    if not _notify_window_ok(conn, mid):
        return
    try:
        info = conn.execute(
            'SELECT c.req_name, m.kickoff, ht.name_tc, at.name_tc '
            'FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
            (mid,)).fetchone()
        lg = ko = home = away = ''
        if info:
            lg, ko, home, away = info
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool TEXT, kind TEXT, match_id INTEGER, league TEXT,
            home TEXT, away TEXT, kickoff TEXT, msg TEXT, ts TEXT,
            read INTEGER DEFAULT 0)''')
        rows = []
        for a in news:
            t = _act_tool_name(a)
            if t:
                rows.append((t, 'new', mid, lg, home, away, ko, now))
        for a in rems:
            t = _act_tool_name(a)
            if t:
                rows.append((t, 'removed', mid, lg, home, away, ko, now))
        for tool, kind, _mid, _lg, h, aw, _ko, ts in rows:
            dup = conn.execute(
                'SELECT 1 FROM alerts WHERE tool=? AND kind=? AND match_id=? '
                "AND ts >= datetime('now','localtime','-12 hours') LIMIT 1",
                (tool, kind, _mid)).fetchone()
            if dup:
                continue
            verb = '🆕 新入選' if kind == 'new' else '❌ 已剔走（唔合資格）'
            msg = '%s %s：%s vs %s（%s｜%s）' % (verb, tool, h, aw, _lg,
                                              (_ko or '')[5:16])
            conn.execute(
                'INSERT INTO alerts(tool,kind,match_id,league,home,away,'
                'kickoff,msg,ts) VALUES(?,?,?,?,?,?,?,?,?)',
                (tool, kind, _mid, _lg, h, aw, _ko, msg, ts))
        conn.commit()
        _NOTIFY_N[0] += len(rows)
    except Exception:
        pass


# ============ 2026-10-25 分聯賽/杯賽實測報表（用戶規格：全部工具頁頂部） ============
# 每個工具一張表：2026 總成績（場/贏/輸/走/勝率）＋最近七日同口徑；聯賽以
# 最近七日勝率最佳排先；2026<60% 且 近7日<60% 由前端 highlight（warn 旗標）。
# 數據源＝各工具實測記錄表（結算 result 列），即時 SQL——賽事一結算下次
# 請求即反映（冇快取＝冇 staleness 問題）。七日系 settled_at（結算時間）。
_LG_REPORT = {
    # tool:      (table,            extra_where,      settle_col, league_direct)
    'w':   ('v3_featured',     None,              'settled_at', False),
    'v1':  ('v1_featured',     None,              'settled_at', False),
    'v1z': ('v1_featured_z',   None,              'settled_at', False),
    'v2':  ('featured',        None,              'settled_at', False),
    'v2z': ('featured_z',      None,              'settled_at', False),
    'g7':  ('fw_grid_featured', "grid='7'",       'settled_at', False),
    'g8':  ('fw_grid_featured', "grid='8'",       'settled_at', False),
    'g12': ('fw_grid_featured', "grid='12'",      'settled_at', False),
    '8s':  ('fw8s_log',        None,              'settled_at', True),
    # fw8x_log 有 kickoff 欄；settled 好多行係 NULL（結算行 result 有值但
    # settled 未填），2026/七日窗口行 kickoff（完場日≈結算日，語義一致）
    '8x':  ('fw8x_log',        None,              'settled',    True),
    '88w': ('w88_log',         None,              'settled_at', True),
    'jx':  ('newjx_log',       None,              'settled_at', True),
    'hb':  ('hb_pick_log',     None,              'settled_at', True),
}


def api_league_report(tool):
    """分聯賽實測報表：2026 總成績＋最近七日（結算口徑：勝率=贏÷(贏+輸)，走唔計）。
    冇自己 league 欄嘅表（精選W/V1/V2/格）join matches→seasons→competitions
    攞 req_name（同全庫歸組口徑一致）。"""
    spec = _LG_REPORT.get(tool)
    if not spec:
        return {'error': 'unknown tool', 'tools': sorted(_LG_REPORT)}
    table, extra, scol, direct = spec
    conn = db()
    try:
        tabs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tabs:
            return {'ok': True, 'tool': tool, 'rows': []}
        where = [("t.result IS NOT NULL"),
                 ("t.kickoff >= '2026-01-01'" if table == 'fw8x_log'
                  else "t.%s >= '2026-01-01'" % scol)]
        if extra:
            where.append(extra)
        wsql = ' AND '.join(where)
        if direct:
            lgsel = 't.league'
        else:
            lgsel = ("COALESCE(c.req_name, t.match_id || '')")
        base = ("FROM %s t " % table) + ('' if direct else
               "JOIN matches m ON m.id=t.match_id "
               "JOIN seasons s ON s.id=m.season_id "
               "JOIN competitions c ON c.titan_id=s.titan_id ")
        dcol = 'kickoff' if table == 'fw8x_log' else scol
        y = conn.execute(
            "SELECT %s, COUNT(*), SUM(t.result='W'), SUM(t.result='L'), "
            "SUM(t.result='P') %s WHERE %s GROUP BY 1" %
            (lgsel, base, wsql)).fetchall()
        d7 = conn.execute(
            "SELECT %s, COUNT(*), SUM(t.result='W'), SUM(t.result='L'), "
            "SUM(t.result='P') %s WHERE %s AND t.%s >= "
            "datetime('now','-7 days') GROUP BY 1" %
            (lgsel, base, wsql, dcol)).fetchall()
    finally:
        conn.close()
    y7 = {r[0]: r[1:] for r in d7}
    rows = []
    for lg, n, w, l, p in y:
        n = n or 0
        w = w or 0
        l = l or 0
        p = p or 0
        dec = w + l
        hit = round(w / dec * 100, 1) if dec else None
        r7 = y7.get(lg)
        n7 = w7 = l7 = p7 = hit7 = None
        if r7:
            n7, w7, l7, p7 = (r7[0] or 0), (r7[1] or 0), (r7[2] or 0), (r7[3] or 0)
            dec7 = w7 + l7
            hit7 = round(w7 / dec7 * 100, 1) if dec7 else None
        rows.append({'league': lg or '（未知）', 'n': n, 'w': w, 'l': l, 'p': p,
                     'hit': hit, 'n7': n7, 'w7': w7, 'l7': l7, 'p7': p7,
                     'hit7': hit7,
                     'warn': bool(hit is not None and hit < 60.0
                                   and hit7 is not None and hit7 < 60.0)})
    # 排序：最近七日勝率最佳排先（無 7 日數據排最尾），同分按 2026 總勝率
    rows.sort(key=lambda r: (r['hit7'] is None, -(r['hit7'] or 0),
                             -(r['hit'] or 0), r['league']))
    return {'ok': True, 'tool': tool, 'rows': rows,
            'now': time.strftime('%Y-%m-%d %H:%M')}


# ============ 2026-10-24 「剔走」專頁（用戶規格） ============
# 工具剔走唔合資格場次時，同時記錄落 removed_picks：邊個工具預測、
# 預測方向、完場後實際結果（驗證剔走邏輯嘅真實命中率）。頁面：
# /api/removed/full → 頂部各工具統計＋逐場清單。
def _migrate_removed(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS removed_picks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool TEXT NOT NULL, match_id INTEGER NOT NULL,
        league TEXT, home TEXT, away TEXT, kickoff TEXT,
        side TEXT, line REAL, hc REAL, giver TEXT,
        detail TEXT, rate REAL,
        removed_at TEXT, result TEXT, settled_at TEXT,
        backfilled INTEGER DEFAULT 0,
        UNIQUE(tool, match_id, detail))''')
    conn.commit()


def _record_removed(conn, tool, mid, side=None, line=None, hc=None,
                    giver=None, detail=None, rate=None, backfilled=0):
    """剔走記錄（best-effort；主流程唔受影響）。detail 做 UNIQUE 一部分
    —— 同一場同一工具嘅同一條預測唔會重複記（88W 一場可多條）。"""
    try:
        _migrate_removed(conn)
        info = conn.execute(
            'SELECT c.req_name, m.kickoff, ht.name_tc, at.name_tc '
            'FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
            (mid,)).fetchone()
        lg = ko = hm = aw = ''
        if info:
            lg, ko, hm, aw = info
        conn.execute(
            'INSERT OR IGNORE INTO removed_picks(tool,match_id,league,'
            'home,away,kickoff,side,line,hc,giver,detail,rate,removed_at,'
            'backfilled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (tool, mid, lg, hm, aw, ko, side, line, hc, giver,
             detail or '', rate,
             time.strftime('%Y-%m-%d %H:%M:%S'), backfilled))
        conn.commit()
    except Exception:
        pass


def _settle_removed_picks(conn):
    """剔走頁嘅結算：方向 up/down 對讓球盤；大/細 對大小線（用記錄時
    嘅 line——剔走當刻嘅盤，唔係而家嘅，咁先係『剔走咗嗰注嘅真實結果』）。
    w88 嘅 side 直接係市場（ah_up/over/...）：行 w88 結算語義。"""
    try:
        _migrate_removed(conn)
    except Exception:
        return 0
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    rows = conn.execute(
        'SELECT id, match_id, side, line, hc, giver FROM removed_picks '
        'WHERE result IS NULL AND match_id IN '
        '(SELECT id FROM matches WHERE home_score IS NOT NULL)').fetchall()
    for rid, mid, side, line, hc, giver in rows:
        try:
            hs, aws = conn.execute(
                'SELECT home_score, away_score FROM matches WHERE id=?',
                (mid,)).fetchone()
            res = None
            if side in ('up', 'down') and hc is not None:
                r = pick_result(hc, giver, hs, aws)
                if r is not None:
                    res = 'P' if r == 'P' else ('W' if (r == 'A')
                                                == (side == 'up') else 'L')
            elif side in ('大', '細') and line is not None:
                tot = hs + aws
                if tot != line:
                    res = 'W' if (tot > line) == (side == '大') else 'L'
                else:
                    res = 'P'
            elif side in ('ah_up', 'ah_down', 'draw', 'even', 'over',
                          'under', 'home_win', 'away_win'):
                try:
                    import w88_engine
                    mkt = 'ah_up' if side in ('up',) else side
                    if side in ('up', 'down'):
                        mkt = 'ah_up' if side == 'up' else 'ah_down'
                    ol = line if mkt in ('over', 'under') else None
                    _hc = hc if mkt in ('ah_up', 'ah_down') else None
                    res = w88_engine._settle_one(mkt, hs, aws, _hc, giver,
                                                 ol)
                except Exception:
                    res = None
            if res:
                conn.execute(
                    'UPDATE removed_picks SET result=?, settled_at=? '
                    'WHERE id=?', (res, now, rid))
                n += 1
        except Exception:
            pass
    if n:
        conn.commit()
    return n


_TOOL_TABLE_FOR_ACT = (('W', 'v3_featured'), ('V2Z', 'featured_z'),
                       ('V2', 'featured'), ('V1Z', 'v1_featured_z'),
                       ('V1', 'v1_featured'),
                       ('格8s', None), ('格8', None), ('格7', None),
                       ('格12', None),
                       ('8X', 'fw8x_log'), ('88W', None), ('新精選', None),
                       ('8s', 'fw8s_log'))


def api_removed_full(limit=300):
    """剔走頁數據：頂部各工具統計＋逐場清單（最新排先）。"""
    conn = db()
    try:
        _migrate_removed(conn)
        _settle_removed_picks(conn)
        stats = {}
        for tool, n, w, l, p in conn.execute(
                "SELECT tool, COUNT(*), SUM(result='W'), SUM(result='L'),"
                " SUM(result='P') FROM removed_picks GROUP BY tool"):
            n = n or 0
            w = w or 0
            l = l or 0
            p = p or 0
            dec = w + l
            stats[tool] = {
                'n': n, 'w': w, 'l': l, 'p': p,
                'hit': round(w / dec * 100, 1) if dec else None,
                'miss': round(l / dec * 100, 1) if dec else None}
        rows = conn.execute(
            'SELECT tool, match_id, league, home, away, kickoff, side,'
            ' detail, rate, removed_at, result, settled_at, backfilled,'
            ' line, hc, giver FROM removed_picks '
            'ORDER BY id DESC LIMIT ?', (max(1, min(int(limit), 500)),)
        ).fetchall()
        return {'stats': stats,
                'rows': [{'tool': r[0], 'match_id': r[1], 'league': r[2],
                          'home': r[3], 'away': r[4], 'kickoff': r[5],
                          'side': r[6], 'detail': r[7], 'rate': r[8],
                          'removed_at': r[9], 'result': r[10],
                          'settled_at': r[11], 'backfilled': r[12],
                          'line': r[13], 'hc': r[14], 'giver': r[15]}
                         for r in rows]}
    finally:
        conn.close()


def backfill_removed_from_alerts():
    """2026-10-24 從 alerts log 回填已剔走場次（alerts 有 tool/match/
    league/home/away/kickoff）。方向用而家重評近似（backfilled=1 標記）——
    舊剔走冇留存當刻方向，如實標注。"""
    conn = db()
    n = 0
    try:
        _migrate_removed(conn)
        rows = conn.execute(
            "SELECT tool, match_id, league, home, away, kickoff, ts "
            "FROM alerts WHERE kind='removed'").fetchall()
        for tool, mid, lg, hm, aw, ko, ts in rows:
            if conn.execute(
                    'SELECT 1 FROM removed_picks WHERE tool=? AND match_id=?',
                    (tool, mid)).fetchone():
                continue
            side = detail = None
            try:
                if tool == '好波':
                    r = conn.execute(
                        'SELECT side, rule FROM hb_pick_log WHERE match_id=?',
                        (mid,)).fetchone()
                    if r:
                        side, detail = r
                else:
                    t2 = _pick_revalidate(conn, mid)
                    for a in (t2 or []):
                        if '移出' in a:
                            continue
            except Exception:
                pass
            _record_removed(conn, tool, mid, side=side, detail=detail,
                            backfilled=1)
            conn.execute(
                'UPDATE removed_picks SET removed_at=? WHERE tool=? '
                'AND match_id=? AND backfilled=1', (ts, tool, mid))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


# 2026-10-25 「🔧 修復」掣根治：工序線程登記表——修復時對 running 狀態做
# 存活檢查，死咗嘅（線程消失但狀態僵屍=True，例如進程內異常）自動歸零。
_JOB_THREADS = {}


def api_repair():
    """「🔧 修復」掣嘅伺服器端：清晒顯示/計算快取＋僵屍工序歸零＋數據主機
    快測。舊制 repairConn 淨係重試連線——數據有問題時撳咗等於冇撳（用戶：
    「修復掣又係廢」）。回傳做咗嘅動作清單俾前端顯示。"""
    acts = []
    try:
        _bust_tool_caches()
        acts.append('顯示快取（入選徽章/精選/8s）已清')
    except Exception:
        pass
    try:
        _v3_item_cache.clear()
        acts.append('45項分析快取已清')
    except Exception:
        pass
    try:
        _LG_HIT_CACHE['ts'] = 0.0
        _LG_HIT_CACHE['data'] = None
        acts.append('聯賽命中率快取已清')
    except Exception:
        pass
    for name, st in (('即時更新批量', _fbatch_state), ('全量更新', _update_state),
                     ('窗口更新', _win_state), ('統一掃描', _unified_scan)):
        try:
            if st.get('running'):
                t = _JOB_THREADS.get(name)
                if t is None or not t.is_alive():
                    st.update(running=False, phase='', error=None)
                    acts.append('僵屍工序「%s」已歸零' % name)
                else:
                    acts.append('「%s」正常進行中' % name)
        except Exception:
            pass
    try:
        import crawler as _cr
        alive = _cr.data_host_probe(timeout=4)   # 修復掣要快——代理抽樣抽中殘代
        # 理時成個掣等到傻；4 秒封頂，中斷都有 30 秒睇門狗兜
        acts.append('數據主機：' + ('正常' if alive else '中斷——30 秒自動重試緊'))
    except Exception:
        pass
    return {'ok': True, 'acts': acts}


# ============ 2026-10-25 即時推送（SSE）============
# 用戶規格：「唔係等動作唔知幾耐先反映——係即時！」輪詢制最細都要 60 秒；
# 而家係 server-push：重算有實質變動（新增/剔走）→ 即刻推俾全部開緊嘅頁面，
# 開到畫面嘅使用者 1-3 秒內見到新結果。SSE 行 ThreadingHTTPServer 冇問題，
# Render proxy 靠 X-Accel-Buffering: no 防緩衝。
_EVENT_SUBS = []
_EVENT_LOCK = threading.Lock()


def _publish_event(evt):
    """廣播俾所有已連接嘅 SSE client（queue 滿/已斷嘅自動清）。"""
    dead = []
    with _EVENT_LOCK:
        for q in _EVENT_SUBS:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _EVENT_SUBS.remove(q)
            except ValueError:
                pass


def api_events(handler):
    """GET /api/events——SSE 長連線。20 秒 ping 一次防代理斷線。"""
    import queue as _q
    q = _q.Queue(maxsize=200)
    with _EVENT_LOCK:
        _EVENT_SUBS.append(q)
    try:
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Connection', 'keep-alive')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.end_headers()
        handler.wfile.write(b': ok\n\n')
        handler.wfile.flush()
        while True:
            try:
                evt = q.get(timeout=20)
                data = json.dumps(evt, ensure_ascii=False)
                handler.wfile.write(b'data: ' + data.encode('utf-8') + b'\n\n')
            except _q.Empty:
                handler.wfile.write(b': ping\n\n')
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    except Exception:
        pass
    finally:
        with _EVENT_LOCK:
            if q in _EVENT_SUBS:
                _EVENT_SUBS.remove(q)


def _bust_tool_caches():
    """有入選/剔走動作就即刻清相關顯示快取——用戶規格『剔走咗 100%
    唔會再喺篩選列表出現』：DB 已 DELETE，快取都唔准殘留（舊制 fw8s
    600s／featured 300s TTL 會令被剔場次殘留數分鐘）。"""
    try:
        for c in (_FW8S_CACHE, _SRCS_CACHE):
            c['ts'] = 0.0
            c['data'] = None
        for cache in (_featured_full_cache, _v1_featured_full_cache):
            for k in (False, True):
                cache[k]['ts'] = 0.0
                cache[k]['data'] = None
        # 2026-10-25：45 項分析快取同步清——入選/剔走後分析結果即刻跟新線
        # （舊制 10 分鐘 TTL 會顯示過期推論，影響精確度）
        _v3_item_cache.clear()
        _bump_data_epoch('tools')   # 2026-10-26：工具頁快取作廢（report 唔使）
    except Exception:
        pass


def api_alerts(limit=50, unread_only=False):
    conn = db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool TEXT, kind TEXT, match_id INTEGER, league TEXT,
            home TEXT, away TEXT, kickoff TEXT, msg TEXT, ts TEXT,
            read INTEGER DEFAULT 0)''')
        if unread_only:
            rows = conn.execute(
                'SELECT id, tool, kind, msg, ts FROM alerts WHERE read=0 '
                'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            n = conn.execute('SELECT COUNT(*) FROM alerts WHERE read=0'
                             ).fetchone()[0]
        else:
            rows = conn.execute(
                'SELECT id, tool, kind, msg, ts FROM alerts '
                'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            n = conn.execute('SELECT COUNT(*) FROM alerts WHERE read=0'
                             ).fetchone()[0]
        return {'unread': n,
                'items': [{'id': r[0], 'tool': r[1], 'kind': r[2],
                           'msg': r[3], 'ts': r[4]} for r in rows]}
    finally:
        conn.close()


def api_alerts_read():
    conn = db()
    try:
        conn.execute('UPDATE alerts SET read=1 WHERE read=0')
        conn.commit()
        return {'ok': True}
    finally:
        conn.close()


def _apply_delta(path):
    """套用增量 sqlite：白名單內嘅表 INSERT OR REPLACE，回傳每表行數。
    用雙連線（delta 獨立開）而唔係 ATTACH——Windows 下 ATTACH 新檔
    好易被防毒短鎖搞到成盤失敗。

    2026-10-26：busy_timeout 120s——雲端自己嘅爬取/重算寫鎖會同 ingest
    撞（19:56/20:03 連續 database is locked 停同步）；busy_timeout 令
    SQLite 內部退避重試。"""
    conn = db()
    try:
        conn.execute('PRAGMA busy_timeout=120000')
    except Exception:
        pass
    rep = {}
    try:
        src = sqlite3.connect(path, timeout=30)
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            if t not in _INGEST_WHITELIST:
                rep[t] = 'skip（唔喺白名單）'
                continue
            tq = '"%s"' % t.replace('"', '""')
            dst_cols = {r[1] for r in conn.execute(f'PRAGMA table_info({tq})')}
            src_info = src.execute(f'PRAGMA table_info({tq})').fetchall()
            cols = [r[1] for r in src_info]
            if not dst_cols:
                # 2026-10-21：雲端舊庫冇嘅表（如 competitions）——照源庫 schema 即場起表
                create_sql = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (t,)).fetchone()[0]
                conn.execute(create_sql)
                conn.commit()
                dst_cols = {r[1] for r in conn.execute(f'PRAGMA table_info({tq})')}
            if set(cols) - dst_cols:
                # 2026-10-24：由 skip 改自動補欄（additive only——淨係加缺少嘅
                # 欄，唔郁原有）。本機新加欄位（如今日精選表 hc/giver）雲端
                # 唔會再成表停同步。boot 遷移都做同樣升級，雙重保險。
                # 2026-10-26 修正：欄名一律雙引號——數字開頭欄名（xsrc_team_alias
                # 嘅 7m_name）裸寫會炸「unrecognized token」停晒成個同步。
                for _cid, cname, ctype, *_r in src_info:
                    if cname in dst_cols:
                        continue
                    conn.execute(f'ALTER TABLE {tq} ADD COLUMN '
                                 f'"{cname.replace(chr(34), chr(34)*2)}" '
                                 f'{ctype or "TEXT"}')
                    dst_cols.add(cname)
                conn.commit()
            collist = ','.join('"%s"' % c.replace('"', '""') for c in cols)
            ph = ','.join('?' * len(cols))
            n = 0
            while True:
                rows = src.execute(
                    f'SELECT {collist} FROM {tq} LIMIT 2000 OFFSET {n}').fetchall()
                if not rows:
                    break
                conn.executemany(
                    f'INSERT OR REPLACE INTO {tq} ({collist}) VALUES ({ph})',
                    rows)
                n += len(rows)
            if t in _DELTA_FULL_REPLACE:
                # true replace：delta 有呢啲表嘅全量內容——雲端 PK 唔喺
                # delta 集合嘅行要 DELETE（將本機嘅剔走/移除同步落雲端，
                # 手機 pending 先同本機一致）。PK 用 PRAGMA declared pk；
                # 臨時表裝 delta PK＋unique index，DELETE 用 NOT EXISTS probe。
                pk_info = [r for r in src_info if r[5]]
                if pk_info and not (set(cols) - dst_cols):
                    conn.execute('DROP TABLE IF EXISTS temp._sync_delta_pk')
                    conn.execute(
                        'CREATE TEMP TABLE temp._sync_delta_pk (%s)'
                        % ', '.join('"%s" %s' % (r[1], r[2] or '')
                                    for r in pk_info))
                    pk_list = ', '.join('"%s"' % r[1] for r in pk_info)
                    conn.executemany(
                        'INSERT INTO temp._sync_delta_pk (%s) VALUES (%s)'
                        % (pk_list, ', '.join('?' * len(pk_info))),
                        src.execute('SELECT %s FROM %s' % (pk_list, t)))
                    conn.execute(
                        # SQLite 唔畀 CREATE INDEX 用 schema 限定表名——
                        # 全部 unqualified，temp 表自然解析到 temp 庫
                        'CREATE UNIQUE INDEX IF NOT EXISTS '
                        '_sync_delta_pk_i ON _sync_delta_pk (%s)'
                        % pk_list)
                    conds = ' AND '.join(
                        '{t}."{c}" IS p."{c}"'.format(t=t, c=r[1])
                        for r in pk_info)
                    cur = conn.execute(
                        'DELETE FROM {t} WHERE NOT EXISTS (SELECT 1 FROM '
                        'temp._sync_delta_pk p WHERE {c})'.format(t=t, c=conds))
                    if cur.rowcount:
                        print('[ingest] %s：清走雲端多餘 %d 行（本機已剔走）'
                              % (t, cur.rowcount), flush=True)
            rep[t] = n
        src.close()
        conn.commit()
        # 同步完即刻清相關快取（唔等 TTL），手機即刻見到新 pick
        for _c in (_featured_full_cache, _v1_featured_full_cache):
            try:
                with _c['lock']:
                    for _k in (False, True):
                        if isinstance(_c.get(_k), dict):
                            _c[_k]['ts'] = 0.0
            except Exception:
                pass
        for _name in ('_FW8S_RULES_CACHE', '_FW8S_BT_CACHE'):
            try:
                globals()[_name]['ts'] = 0.0
                globals()[_name]['data'] = None
            except Exception:
                pass
        for _name in ('_check_combo_cache', '_v2_item_cache', '_v3_item_cache',
                      '_pk_full_cache'):
            try:
                globals()[_name].clear()
            except Exception:
                pass
    finally:
        conn.close()
    print(f'[ingest] {rep}', flush=True)
    return rep


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
        if d or dz:
            try:
                _migrate_pick_line_cols(conn)
            except sqlite3.Error:
                pass
            phc, pgv = _pick_line(conn, mid)
        if d:
            conn.execute(
                'INSERT INTO featured(match_id, direction, added_at, hc, giver) '
                'VALUES(?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                'direction=excluded.direction, hc=excluded.hc, '
                'giver=excluded.giver',
                (mid, d, time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
            log_featured(conn, mid, d)
        elif row and row[0] is None:
            conn.execute('DELETE FROM featured WHERE match_id=?', (mid,))
            removed = True
        if dz:
            conn.execute(
                'INSERT INTO featured_z(match_id, direction, added_at, hc, '
                'giver) VALUES(?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                'direction=excluded.direction, hc=excluded.hc, '
                'giver=excluded.giver',
                (mid, dz, time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
            log_featured_z(conn, mid, dz)
        elif rowz and rowz[0] is None:
            conn.execute('DELETE FROM featured_z WHERE match_id=?', (mid,))
            removed_z = True
        conn.commit()
        # 2026-10-25 即時推送：單場重整有實質變動（加入/更新/剔走）都廣播
        _acts = []
        if d:
            _acts.append('V2' + ('更新' if row else '新增'))
        elif removed:
            _acts.append('V2移出')
        if dz:
            _acts.append('V2Z' + ('更新' if rowz else '新增'))
        elif removed_z:
            _acts.append('V2Z移出')
        if _acts:
            _publish_event({'type': 'reval', 'mid': mid, 'acts': _acts})
    finally:
        conn.close()
    return {'ok': True, 'direction': d, 'direction_z': dz,
            'removed': removed, 'removed_z': removed_z}


# ============ 我的選擇（上/下盤 記錄 + 勝出率統計） ============

def init_picks():
    # 用戶爬蟲全速行緊時 DB 長期鎖住：成個 init 包重試（最多 10 次×20 秒），
    # 唔會再一撞 locked 就殺死 server 啟動（2026-10-05 修正）
    for attempt in range(10):
        try:
            _init_picks_once()
            return
        except sqlite3.OperationalError:
            if attempt == 9:
                raise
            time.sleep(20)


def _init_picks_once():
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
    # 舊版本（2026-09-26）：V1 七重準則精選／精選Z，同 V2 完全分開
    conn.execute('''CREATE TABLE IF NOT EXISTS v1_featured(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, result TEXT, settled_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS v1_featured_z(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, result TEXT, settled_at TEXT)''')
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
    # V3（2026-09-28）：精選W＝1A、1I、19同主客、19+互換、31、35 全部同方向≥50%；
    # 過往紀錄完全獨立（同 V1／V2 分開）
    conn.execute('''CREATE TABLE IF NOT EXISTS v3_featured(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        detail TEXT, added_at TEXT, result TEXT, settled_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS v3_featured_log(
        match_id INTEGER PRIMARY KEY, direction TEXT NOT NULL,
        added_at TEXT, handicap REAL, giver TEXT,
        home_odds REAL, away_odds REAL)''')
    conn.execute(
        "INSERT OR IGNORE INTO v3_featured_log("
        "match_id, direction, added_at, handicap, giver, home_odds, away_odds) "
        "SELECT f.match_id, f.direction, f.added_at, oc.handicap, oc.giver, "
        "oc.home_odds, oc.away_odds FROM v3_featured f "
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
        gap = d.get('gap')
        if isinstance(gap, dict):
            gap = gap.get('text')
        return {'line': d.get('line'), 'n': d.get('n'),
                'up_r': d.get('up_r'), 'down_r': d.get('down_r'),
                'push_r': d.get('push_r'), 'gap': gap}

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
    """對單場跑篩查，抽出 1/5/8/12/15/18 摘要 及 14/17 差距（5 分鐘快取）"""
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

    def _fallback(row):
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

    def build(row):
        try:
            return _build_pick_rec(row, now_str)
        except Exception:
            traceback.print_exc()
            return _fallback(row)

    # 並行起唄——單線程每場 5–8 秒，幾十場必超時；
    # 超時未完成用 fallback 補底（brief=None），唔丟 row
    out = [r or _fallback(row)
           for r, row in zip(_map_bounded(build, rows), rows)]
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
    每場跑 v2_engine.featured_letters——任一字頭 A–P 合格（2X 有方向 且 13X 同方向
    ＋30 同盤同方向>49.99%＋33 同盤同方向>49.99%；①②率各自獨立）即入選；
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
            "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','+8 hours') "
            "ORDER BY m.kickoff").fetchall()
        _feat_scan['total'] = len(rows)
        import v2_engine
        v2_engine.load_v2_pool(conn)   # 預熱數據池
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
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
                phc, pgv = _tgt_pick_line(conn, t, mid)
                if d and not conn.execute(
                        'SELECT 1 FROM featured WHERE match_id=?', (mid,)).fetchone():
                    conn.execute(
                        'INSERT INTO featured(match_id, direction, letters, added_at,'
                        'hc, giver) VALUES(?,?,?,?,?,?)',
                        (mid, d, meta, time.strftime('%Y-%m-%d %H:%M:%S'),
                         phc, pgv))
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
                        'INSERT INTO featured_z(match_id, direction, letters, '
                        'added_at, hc, giver) VALUES(?,?,?,?,?,?)',
                        (mid, dz, meta, time.strftime('%Y-%m-%d %H:%M:%S'),
                         phc, pgv))
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
        conn.execute('DELETE FROM v1_featured WHERE match_id IN '
                     '(SELECT id FROM matches WHERE home_score IS NULL)')
        conn.execute('DELETE FROM v1_featured_z WHERE match_id IN '
                     '(SELECT id FROM matches WHERE home_score IS NULL)')
        conn.execute('DELETE FROM v3_featured WHERE match_id IN '
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
        try:
            import v3_engine
            v3_engine.invalidate_pool()       # V3 池（對調欄）都要清
            with _v3_item_cache_lock:
                _v3_item_cache.clear()          # V3 單項／summary 快取一併清
        except Exception:
            pass
        print('[update] 重算精選…', flush=True)
        if not _feat_scan['running']:
            _scan_thread(_featured_scan_job)
        if not _v1_scan['running']:
            threading.Thread(target=_v1_scan_job, daemon=True).start()
        if not _v3_scan['running']:
            _scan_thread(_v3_featured_scan_job)
    except Exception:
        import traceback
        traceback.print_exc()


_HIST_HIT_CACHE = {'ts': 0.0, 'data': {}}


def _hist_hit(tbl):
    """2026-10-24：合資格場次顯示『呢類訊號嘅歷史命中率』——用對應表嘅
    已結算結果計（5 分鐘快取）。tbl ∈ featured/featured_z/v1_featured/
    v1_featured_z。"""
    now = time.time()
    # 2026-10-24 修正：舊制快取命中但呢張表未計過 → 照舊返回空（每頁第一張
    # 卡先計一張表，其他表 5 分鐘內全部食零——V1/V2 交替開就中招）。
    # 而家：快取只在『呢張表已計過』先生效，否則即場補計。
    if now - _HIST_HIT_CACHE['ts'] < 300             and tbl in _HIST_HIT_CACHE['data']:
        return _HIST_HIT_CACHE['data'][tbl]
    conn = db()
    try:
        n, w, l = conn.execute(
            "SELECT COUNT(*), SUM(result='W'), SUM(result='L') FROM %s "
            "WHERE result IN ('W','L')" % tbl).fetchone()
        d = {'n': n or 0,
             'hit': round(w / (w + l) * 100, 1) if (w or 0) + (l or 0) else None}
    finally:
        conn.close()
    _HIST_HIT_CACHE['data'][tbl] = d
    _HIST_HIT_CACHE['ts'] = now
    return d



def _build_featured_rec(row, tbl, now):
    """精選單場卡片：基本資料＋自動結算（未結算嘅以入選方向計 贏/輸/走）＋brief＋check＋letters
    結算用落注時影低嘅 stored hc/giver；legacy 冇 stored 先對 closing。"""
    (mid, d, res, added, ko, hs, aws, hh, ha, fg, h, a, lg, hc, gv, ho, ao,
     letters, hr_, ar_, phc, pgv) = row
    _hh = _hist_hit(tbl)
    rec = {'id': mid, 'direction': d, 'added_at': added, 'kickoff': ko,
           'hist_hit': _hh['hit'], 'hist_n': _hh['n'],
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
        shc, sgv = _stored_first(phc, pgv, hc, gv)
        r = pick_result(shc, sgv, hs, aws)
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
    if hh is not None:
        rec['ht'] = f'{hh}-{ha}'
    if fg in ('home', 'away'):
        rec['first_goal'] = fg
    rec['brief'] = _screen_brief(mid)
    rec['check'] = _check_combo_for(mid, d)     # 中咗 Check 下先邊條組合
    return rec


# 雙 z 槽（False=正選／True=同主隊）——單槽舊版 z 交替會互相踢走重建，
# 連環請求 stacking build 30 秒逾時（2026-10-08 V1/V2「用唔到」復發元兇）。
# 後台 refresher 每 150 秒預熱；請求只讀快取，冷啟動先 inline build 一次。
_featured_full_cache = {False: {'ts': 0.0, 'data': None},
                        True: {'ts': 0.0, 'data': None},
                        'lock': threading.Lock(), 'build_lock': threading.Lock()}
_FEATURED_FULL_TTL = 300


def get_featured_full(z=False):
    """非阻塞讀快取：後台 _featured_cache_refresher 每 150 秒重建兩個 z 槽。"""
    slot = _featured_full_cache[z]
    with _featured_full_cache['lock']:
        if slot['data'] is not None:
            return slot['data']
    with _featured_full_cache['build_lock']:
        with _featured_full_cache['lock']:
            if slot['data'] is not None:
                return slot['data']
        data = _get_featured_full_impl(z)
        with _featured_full_cache['lock']:
            slot.update(ts=time.time(), data=data)
        return data


def _get_featured_full_impl(z=False):
    """精選全量：未開賽順時間排先；已完場最新排先（永不刪除）。
    z=True 讀精選Z（featured_z）。順便結算新完場場次（以入選方向計 贏/輸/走）。"""
    tbl = 'featured_z' if z else 'featured'
    conn = db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        _migrate_pick_line_cols(conn)
    except sqlite3.Error:
        pass
    pending_rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.added_at, m.kickoff, '
        'm.home_score, m.away_score, m.half_home, m.half_away, '
        '(SELECT g.side FROM match_goals g WHERE g.match_id=m.id '
        "ORDER BY g.seq LIMIT 1) AS fg, ht.name_tc, at.name_tc, c.req_name, "
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, f.letters, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank), '
        'f.hc, f.giver '
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
        'm.home_score, m.away_score, m.half_home, m.half_away, '
        '(SELECT g.side FROM match_goals g WHERE g.match_id=m.id '
        "ORDER BY g.seq LIMIT 1) AS fg, ht.name_tc, at.name_tc, c.req_name, "
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, f.letters, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank), '
        'f.hc, f.giver '
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

    def _fallback(row):
        (mid, d, res, added, ko, hs, aws, hh, ha, fg, h, a, lg, hc, gv,
         ho, ao, letters, hr_, ar_, _phc, _pgv) = row
        rec = {'id': mid, 'direction': d, 'result': res, 'added_at': added,
               'kickoff': ko, 'home': h, 'away': a, 'league': lg,
               'rank_home': hr_, 'rank_away': ar_,
               'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
               'odds': f'主{ho}/客{ao}' if ho is not None else None,
               'played': ko < now, 'brief': None, 'check': None,
               'letters': None}
        if hs is not None:
            rec['score'] = f'{hs}-{aws}'
        if hh is not None:
            rec['ht'] = f'{hh}-{ha}'
        if fg in ('home', 'away'):
            rec['first_goal'] = fg
        return rec

    def build(row):
        try:
            return _build_featured_rec(row, tbl, now)
        except Exception:
            traceback.print_exc()
            return _fallback(row)

    # 並行起唄——單線程逐場 5–8 秒，幾十場必超時
    # 超時未完成嘅列用 _fallback 補底（brief=None）——唔丟row，頁面永遠齊場
    pending = [r or _fallback(row)
               for r, row in zip(_map_bounded(build, pending_rows), pending_rows)]
    played = [r or _fallback(row)
              for r, row in zip(_map_bounded(build, played_rows), played_rows)]
    wins = sum(1 for r in played if r.get('result') == 'W')
    losses = sum(1 for r in played if r.get('result') == 'L')
    pushes = sum(1 for r in played if r.get('result') == 'P')
    stats = {'total': len(pending) + len(played), 'pending': len(pending),
             'played': len(played), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': pending, 'played': played,
            'scan': {k: _feat_scan[k] for k in ('running', 'done', 'total', 'added', 'last', 'error')}}


# ============ 舊版本：V1 七重準則精選（同 V2 十六字頭完全分開） ============

_v1_scan = {'running': False, 'done': 0, 'total': 0, 'added': 0, 'added_z': 0,
            'last': None, 'error': None}


def _v1_scan_job():
    """V1（舊版本分頁）掃描：入選規則＝V2 而家嘅十六字頭規則（逆向重塑），
    每場跑 v2_engine.featured_letters，結果寫入 v1_featured / v1_featured_z
    （同 V2 表完全分開，方便新舊對照）。卡片用 V1 舊項目名展示，並標明對應 V2 規則。"""
    global _v1_scan
    if _v1_scan['running']:
        return
    _v1_scan.update(running=True, done=0, added=0, added_z=0, error=None)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM matches m "
            "JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 AND oc.handicap IS NOT NULL "
            "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','+8 hours') "
            "ORDER BY m.kickoff").fetchall()
        _v1_scan['total'] = len(rows)
        import v2_engine
        v2_engine.load_v2_pool(conn)   # 預熱數據池
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
        added = added_z = 0
        for (mid,) in rows:
            try:
                t = screen_engine.get_target(conn, mid)
                if not t:
                    continue
                fl = v2_engine.featured_letters(conn, t) or {}
                d = fl.get('direction')
                passed = fl.get('passed_letters') or []
                phc, pgv = _tgt_pick_line(conn, t, mid)
                if d and not conn.execute(
                        'SELECT 1 FROM v1_featured WHERE match_id=?', (mid,)).fetchone():
                    conn.execute(
                        'INSERT INTO v1_featured(match_id, direction, added_at,'
                        'hc, giver) VALUES(?,?,?,?,?)',
                        (mid, d, time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
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
                        'SELECT 1 FROM v1_featured_z WHERE match_id=?', (mid,)).fetchone():
                    conn.execute(
                        'INSERT INTO v1_featured_z(match_id, direction, added_at,'
                        'hc, giver) VALUES(?,?,?,?,?)',
                        (mid, dz, time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
                    log_featured_z(conn, mid, dz)
                    conn.commit()
                    added_z += 1
            except Exception:
                pass
            _v1_scan['done'] += 1
        _v1_scan['added'] = added
        _v1_scan['added_z'] = added_z
        _v1_scan['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        _v1_scan['error'] = str(e)
    finally:
        conn.close()
        _v1_scan['running'] = False


_v1_featured_full_cache = {False: {'ts': 0.0, 'data': None},
                           True: {'ts': 0.0, 'data': None},
                           'lock': threading.Lock(), 'build_lock': threading.Lock()}


def get_v1_featured_full(z=False):
    """V1 版同 get_featured_full：後台 refresher 預熱，請求零阻塞。"""
    slot = _v1_featured_full_cache[z]
    with _v1_featured_full_cache['lock']:
        if slot['data'] is not None:
            return slot['data']
    with _v1_featured_full_cache['build_lock']:
        with _v1_featured_full_cache['lock']:
            if slot['data'] is not None:
                return slot['data']
        data = _get_v1_featured_full_impl(z)
        with _v1_featured_full_cache['lock']:
            slot.update(ts=time.time(), data=data)
        return data


def _get_v1_featured_full_impl(z=False):
    """V1 精選全量：結構同 get_featured_full 一樣（冇 letters）。
    z=True 讀 v1_featured_z。"""
    tbl = 'v1_featured_z' if z else 'v1_featured'
    conn = db()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        _migrate_pick_line_cols(conn)
    except sqlite3.Error:
        pass
    base = (
        'SELECT f.match_id, f.direction, f.result, f.added_at, m.kickoff, '
        'm.home_score, m.away_score, m.half_home, m.half_away, '
        '(SELECT g.side FROM match_goals g WHERE g.match_id=m.id '
        "ORDER BY g.seq LIMIT 1) AS fg, ht.name_tc, at.name_tc, c.req_name, "
        'oc.handicap, oc.giver, oc.home_odds, oc.away_odds, NULL, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank), '
        'f.hc, f.giver '
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
        "AND ar.scope='total' AND ar.grp='' ")
    pending_rows = conn.execute(
        base + 'WHERE m.kickoff >= ? ORDER BY m.kickoff', (now,)).fetchall()
    played_rows = conn.execute(
        base + 'WHERE m.kickoff < ? ORDER BY m.kickoff DESC', (now,)).fetchall()
    conn.close()

    def _fallback(row):
        (mid, d, res, added, ko, hs, aws, hh, ha, fg, h, a, lg, hc, gv,
         ho, ao, _letters, hr_, ar_, _phc, _pgv) = row
        rec = {'id': mid, 'direction': d, 'result': res, 'added_at': added,
               'kickoff': ko, 'home': h, 'away': a, 'league': lg,
               'rank_home': hr_, 'rank_away': ar_,
               'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
               'odds': f'主{ho}/客{ao}' if ho is not None else None,
               'played': ko < now, 'brief': None, 'check': None,
               'letters': None}
        if hs is not None:
            rec['score'] = f'{hs}-{aws}'
        if hh is not None:
            rec['ht'] = f'{hh}-{ha}'
        if fg in ('home', 'away'):
            rec['first_goal'] = fg
        return rec

    def build(row):
        try:
            return _build_featured_rec(row, tbl, now)
        except Exception:
            traceback.print_exc()
            return _fallback(row)

    # 超時未完成嘅列用 _fallback 補底（brief=None）——唔丟row，頁面永遠齊場
    pending = [r or _fallback(row)
               for r, row in zip(_map_bounded(build, pending_rows), pending_rows)]
    played = [r or _fallback(row)
              for r, row in zip(_map_bounded(build, played_rows), played_rows)]
    wins = sum(1 for r in played if r.get('result') == 'W')
    losses = sum(1 for r in played if r.get('result') == 'L')
    pushes = sum(1 for r in played if r.get('result') == 'P')
    stats = {'total': len(pending) + len(played), 'pending': len(pending),
             'played': len(played), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': pending, 'played': played,
            'scan': {k: _v1_scan[k] for k in ('running', 'done', 'total', 'added', 'added_z', 'last', 'error')}}


def do_v1_featured_refresh(mid):
    """V1 單場重新整理：重抓最新盤口賠率，用 V2 十六字頭規則重算該場資格。"""
    st = do_fetch(mid, True)
    if not st.get('ok'):
        return {'ok': False, 'error': st.get('error', '抓取賠率失敗'), 'fetch': st}
    conn = db()
    try:
        screen_engine._pool_cache['ts'] = 0
        try:
            import v2_engine
            v2_engine.invalidate_pool()
        except Exception:
            pass
        d = dz = None
        t = None
        try:
            t = screen_engine.get_target(conn, mid)
            if t:
                fl = v2_engine.featured_letters(conn, t) or {}
                d = fl.get('direction')
                passed = fl.get('passed_letters') or []
                if passed:
                    zpass = [e for e in (fl.get('letters') or [])
                             if e.get('mix') == '同主隊' and e['letter'] in passed]
                    if zpass:
                        dz = zpass[0]['dir']
        except Exception:
            pass
        row = conn.execute('SELECT result FROM v1_featured WHERE match_id=?',
                           (mid,)).fetchone()
        rowz = conn.execute('SELECT result FROM v1_featured_z WHERE match_id=?',
                            (mid,)).fetchone()
        removed = removed_z = False
        if d or dz:
            try:
                _migrate_pick_line_cols(conn)
            except sqlite3.Error:
                pass
            phc, pgv = _tgt_pick_line(conn, t, mid)
        if d:
            conn.execute(
                'INSERT INTO v1_featured(match_id, direction, added_at, hc, giver) '
                'VALUES(?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                'direction=excluded.direction, hc=excluded.hc, '
                'giver=excluded.giver',
                (mid, d, time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
            log_featured(conn, mid, d)
        elif row and row[0] is None:
            conn.execute('DELETE FROM v1_featured WHERE match_id=?', (mid,))
            removed = True
        if dz:
            conn.execute(
                'INSERT INTO v1_featured_z(match_id, direction, added_at, hc, '
                'giver) VALUES(?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET '
                'direction=excluded.direction, hc=excluded.hc, '
                'giver=excluded.giver',
                (mid, dz, time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
            log_featured_z(conn, mid, dz)
        elif rowz and rowz[0] is None:
            conn.execute('DELETE FROM v1_featured_z WHERE match_id=?', (mid,))
            removed_z = True
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'direction': d, 'direction_z': dz,
            'removed': removed, 'removed_z': removed_z}


# ============ 過往賽果（已完場＋尾盤 → 上/下/走盤＋五項命中率統計＋篩選＋走勢） ============

def get_results(limit=1000, league=None, hc=None, gv=None, scope='featured'):
    """過往賽果（2026-09-20 起 = 精選場次專頁）。
    scope='featured'：只計精選已開賽場次；scope='all'：全部已完場（舊版口徑）。
    五項命中率：上盤命中率（方向=上嘅精選命中）、下盤命中率（方向=下）、
    總命中率／精選命中率（全部精選已結算）、我的選擇命中率。"""
    conn = db()
    featured = (scope in ('featured', 'featuredz'))
    tbl = 'featured_z' if scope == 'featuredz' else 'featured'
    where = ("WHERE m.kickoff < datetime('now','+8 hours')" if featured
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
        lg_where = "WHERE m.kickoff < datetime('now','+8 hours')"
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


def _idx_rates(idx, res):
    """行號陣列 → {n, up_r, down_r}（分母剔除走盤），idx=None 或空 → None"""
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
                'cur_up_water': uw,
                'target': {'home': t.get('home'), 'away': t.get('away'),
                           'giver': (T or {}).get('g'),
                           'line': screen_engine.fmt_line(T['h'], T.get('g'))
                           if T and T.get('h') is not None else None}}
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
            # 有上限快取（同 _v3_item_cache，2026-10-02）
            if len(_v2_item_cache) > 2000:
                _v2_item_cache.clear()
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


# ============ V3（2026-09-28 規格定稿）：45 項場次分析／精選W／Check 下先 ============
# 注意：V3 項目號同 V1 規則號完全隔離（V1 規則只喺「舊版本」分頁用，唔可以混）。

_v3_scan = {'running': False, 'done': 0, 'total': 0, 'added': 0,
            'last': None, 'error': None}
_v3_item_cache = {}
_v3_item_cache_lock = threading.Lock()
V3_ITEM_CACHE_TTL = 600


_STALE_LIVE_HOURS = 26   # 開賽逾 26 小時仍無比分 → 唔再當「進行中」（取消/延期/數據缺失）


_SRCS_CACHE = {'ts': 0.0, 'data': {}}


def _attach_srcs(ms):
    """2026-10-22 用戶規格「主頁每個數據講明由邊個篩選出嚟」：標記每場
    入選咗邊啲精選系統（m['srcs'] = ['W','V2','F8',...]），前端砌可見
    來源徽章。
    2026-10-22b 優化：13 條細查詢合一條 UNION ALL（鎖等待由 13 次變 1
    次）＋60 秒記憶體快取（主頁 60 秒自動刷新會不停打呢度）＋短
    busy_timeout（爬蟲鎖庫時快閃降級——冇徽章唔會冇反應）。包含已完場
    （事後回查都係真實入選紀錄）。"""
    if not ms:
        return
    ids = [m['id'] for m in ms if m.get('id')]
    if not ids:
        return
    now = time.time()
    cache = _SRCS_CACHE['data'] if now - _SRCS_CACHE['ts'] < 60 else {}
    miss = [i for i in ids if i not in cache]
    if miss:
        ph = ','.join('?' * len(miss))
        parts = []
        for flag, sql in (
                ('W', 'SELECT match_id FROM v3_featured WHERE match_id IN (%s)'),
                ('V2', 'SELECT match_id FROM featured WHERE match_id IN (%s)'),
                ('V2Z', 'SELECT match_id FROM featured_z WHERE match_id IN (%s)'),
                ('V1', 'SELECT match_id FROM v1_featured WHERE match_id IN (%s)'),
                ('V1Z', 'SELECT match_id FROM v1_featured_z WHERE match_id IN (%s)'),
                ('F7', "SELECT match_id FROM fw_grid_featured WHERE grid='7' AND match_id IN (%s)"),
                ('F8', "SELECT match_id FROM fw_grid_featured WHERE grid='8' AND match_id IN (%s)"),
                ('F12', "SELECT match_id FROM fw_grid_featured WHERE grid='12' AND match_id IN (%s)"),
                ('F8S', "SELECT match_id FROM fw_grid_featured WHERE grid='8s' AND match_id IN (%s)"),
                ('S8', 'SELECT match_id FROM fw8s_log WHERE match_id IN (%s)'),
                ('X8', 'SELECT match_id FROM fw8x_log WHERE match_id IN (%s)'),
                ('W88', 'SELECT match_id FROM w88_log WHERE match_id IN (%s)'),
                ('JX', 'SELECT match_id FROM newjx_log WHERE match_id IN (%s)')):
            parts.append("SELECT match_id, '%s' AS f FROM (%s)" % (flag, sql % ph))
        conn = sqlite3.connect(DB_PATH, timeout=8)
        try:
            conn.execute('PRAGMA busy_timeout=3000')
            for mid, f in conn.execute(' UNION ALL '.join(parts),
                                       miss * len(parts)):
                e = cache.setdefault(mid, {})
                if f not in e:
                    e[f] = True
        except sqlite3.Error:
            pass   # 鎖庫：今次冇徽章，下個循環補
        finally:
            conn.close()
        _SRCS_CACHE['ts'] = now
        _SRCS_CACHE['data'] = cache
    for m in ms:
        s = cache.get(m.get('id'))
        if s:
            m['srcs'] = sorted(s.keys())


def api_v3_list(hours=48, fin_hours=120, fin_cat='all'):
    """V3 主頁清單：即將開賽（唔設上限由前端 hours 控制）＋已開賽（預設 120h，
    可用 fin_hours/fin_cat 回查）。每場附 state（scheduled/live/finished/unknown，
    香港時間判定）；unknown＝開賽逾 26 小時仍無比分——用戶投訴已完場場次
    滯留「進行中」，過期無比分一律唔再標進行中。"""
    import v3_engine
    from datetime import datetime, timedelta
    up = upcoming(hours)
    pl = played(fin_hours, fin_cat)
    now = v3_engine.hk_now_str()
    stale = (datetime.now() + timedelta(hours=8 - _STALE_LIVE_HOURS)
             ).strftime('%Y-%m-%d %H:%M')
    for m in up:
        m['state'] = 'scheduled'
    for m in pl:
        m['state'] = 'finished' if m.get('score') else (
            'unknown' if (m.get('kickoff') or '') <= stale else 'live')
    _attach_srcs(up)
    return {'upcoming': up, 'played': pl, 'now': now}


def api_v3_target(mid):
    """V3 場次分析頁頭：7 時點盤口水位＋狀態＋排名（輕量版，唔跑成個 screen）。"""
    import v3_engine
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}

        def box(label):
            T = screen_engine.tline(t.get('odds') or {}, label)
            if not T or T.get('h') is None:
                return None
            return {'line': screen_engine.fmt_line(T['h'], T.get('g')),
                    'ho': T.get('ho'), 'ao': T.get('ao'), 'g': T.get('g'),
                    'v': (-T['h'] if T.get('g') == 'home'
                          else (0.0 if not T.get('g') else T['h']))}

        def crown_box(label):
            # 易胜博(12) 未開盤 → Crown(3) 後備，標明來源（顯示用；V3 分析照舊用 12）
            r = conn.execute(
                'SELECT handicap, giver, home_odds, away_odds FROM odds_asian '
                'WHERE match_id=? AND company_id=3 AND label=?',
                (mid, label)).fetchone()
            if not r or r[0] is None:
                return None
            return {'line': screen_engine.fmt_line(r[0], r[1]),
                    'ho': r[2], 'ao': r[3], 'g': r[1], 'src': 'crown',
                    'v': (-r[0] if r[1] == 'home'
                          else (0.0 if not r[1] else r[0]))}

        boxes = {}
        for _lb, _key in (('closing', 'close'), ('initial', 'init'),
                          ('pre_4h', 'h4'), ('pre_30m', 'h30'),
                          ('pre_15m', 'h15'), ('pre_10m', 'h10'),
                          ('pre_5m', 'h5')):
            _b = box(_lb)
            boxes[_key] = _b if _b is not None else crown_box(_lb)

        hr = (t.get('pre') or {}).get('home_total_rank')
        ar = (t.get('pre') or {}).get('away_total_rank')
        if not hr:
            r = conn.execute("SELECT rank FROM standings WHERE season_id=? AND team_id=? "
                             "AND scope='total' AND grp=''",
                             (t['season_id'], t['home_id'])).fetchone()
            hr = r[0] if r else None
        if not ar:
            r = conn.execute("SELECT rank FROM standings WHERE season_id=? AND team_id=? "
                             "AND scope='total' AND grp=''",
                             (t['season_id'], t['away_id'])).fetchone()
            ar = r[0] if r else None
        sc = conn.execute('SELECT home_score, away_score, half_home, half_away '
                          'FROM matches WHERE id=?', (mid,)).fetchone()
        odds_at = conn.execute(
            'SELECT MAX(fetched_at) FROM odds_asian WHERE match_id=?',
            (mid,)).fetchone()[0]
        fg = conn.execute(
            'SELECT side FROM match_goals WHERE match_id=? ORDER BY seq LIMIT 1',
            (mid,)).fetchone()
        score = None
        state = 'scheduled'
        if sc and sc[0] is not None:
            score = f'{sc[0]}-{sc[1]}'
            state = 'finished'
        elif t['kickoff'] <= v3_engine.hk_now_str():
            state = 'live'
        tgt = {'id': t['id'], 'home': t['home'], 'away': t['away'],
               'rank_home': hr, 'rank_away': ar,
               'league': t.get('league'), 'category': t.get('category'),
               'kickoff': t['kickoff'], 'state': state, 'score': score,
               'odds_at': odds_at,
               'close': boxes['close'], 'init': boxes['init'],
               'h4': boxes['h4'], 'h30': boxes['h30'],
               'h15': boxes['h15'], 'h10': boxes['h10'],
               'h5': boxes['h5']}
        if sc and sc[2] is not None:
            tgt['ht'] = f'{sc[2]}-{sc[3]}'
        if sc and sc[0] is not None and fg and fg[0] in ('home', 'away'):
            tgt['first_goal'] = fg[0]
        return {'ok': True, 'target': tgt}
    finally:
        conn.close()


def api_v3_item(mid, no):
    """V3 單項計算（10 分鐘快取）"""
    import v3_engine
    key = (mid, str(no))
    now = time.time()
    with _v3_item_cache_lock:
        hit = _v3_item_cache.get(key)
        if hit and now - hit[0] < V3_ITEM_CACHE_TTL:
            return hit[1]
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}
        out = v3_engine.compute_item_v3(conn, t, str(no))
        out.setdefault('ok', 'error' not in out)
        with _v3_item_cache_lock:
            # 有上限快取：舊實作無界增長（服務開幾日就儲晒全部場次×45項），
            # 爆滿就清一次重計（同 _check_combo_cache 一貫手法，2026-10-02）
            if len(_v3_item_cache) > 2000:
                _v3_item_cache.clear()
            _v3_item_cache[key] = (now, out)
        return out
    finally:
        conn.close()


def _match_result(mid):
    """完場賽事嘅賽果結算（A=上盤贏 B=下盤贏 P=走）——Check 下先結果總結用。
    每次即場查（唔入快取），啱啱完場嘅場次唔會食到舊快取冇賽果。"""
    conn = db()
    try:
        row = conn.execute('SELECT home_score, away_score FROM matches WHERE id=?',
                           (mid,)).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None
        oc = conn.execute(
            "SELECT handicap, giver FROM odds_asian WHERE match_id=? AND company_id=12 "
            "ORDER BY (label='closing') DESC, label DESC LIMIT 1", (mid,)).fetchone()
        if not oc or oc[0] is None:
            return {'score': f'{row[0]}-{row[1]}', 'r': None}
        return {'score': f'{row[0]}-{row[1]}',
                'r': pick_result(oc[0], oc[1], row[0], row[1])}
    finally:
        conn.close()


def api_match_tools(mid):
    """2026-10-25 詳情頁「工具預測」（用戶規格，唔簡化到睇唔明）：
    呢場有預測嘅每個工具——工具名＋預測內容（方向/隊/盤/賠率/結果）＋
    ① 工具全庫命中率 ② 同一聯賽命中率 ③ 逐篩選法命中率（全庫＋同聯賽；
    部份工具有多個篩選法：精選W＝每條條件、字頭工具＝每個字頭、格工具＝每格、
    8X＝每條因子、88W＝每個 market+conds 組合、好波＝每條規則）。
    「同聯賽」＝同 competitions.req_name（主頁/詳情一貫口徑）。
    命中率＝(贏+0.5×走)÷總場；樣本細會照顯示 n，等用戶自己判斷可信程度。"""
    conn = db()
    try:
        base = conn.execute(
            'SELECT c.req_name, ht.name_tc, at.name_tc, '
            'oc.handicap, oc.giver, oc.home_odds, oc.away_odds '
            'FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            "LEFT JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
            'AND oc.company_id=12 WHERE m.id=?', (mid,)).fetchone()
        if not base:
            return {'error': '找不到賽事'}
        league, home, away, hc, giver, ho, ao = base
        line = screen_engine.fmt_line(hc, giver) if hc is not None else None
        odds = (f'主{ho}/客{ao}' if ho is not None and ao is not None else None)

        def _hit(t):
            w, l, p = t
            n = w + l + p
            return {'w': w, 'l': l, 'p': p, 'n': n,
                    'hit': round((w + 0.5 * p) / n * 100) if n else None}

        def _bump(dic, name, res):
            t = dic.setdefault(name, [0, 0, 0])
            t[0 if res == 'W' else (1 if res == 'L' else 2)] += 1

        def _tally(payload_col, table, where='', params=()):
            """回 {method: (全庫hit, 同聯賽hit)}：逐行解析 payload（JSON）入面嘅
            篩選法名單，全庫同同聯賽分開計。"""
            rows = conn.execute(
                f'SELECT t.result, c.req_name, t.{payload_col} FROM {table} t '
                'JOIN matches m ON m.id=t.match_id '
                'JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id '
                f'WHERE t.result IS NOT NULL {where}', params).fetchall()
            a, g = {}, {}
            for res, rl, payload in rows:
                try:
                    ms = _methods(payload)
                except Exception:
                    continue
                for name in ms:
                    if not name:
                        continue
                    _bump(a, name, res)
                    if rl == league:
                        _bump(g, name, res)
            return ({k: _hit(v) for k, v in a.items()},
                    {k: _hit(v) for k, v in g.items()})

        def _plain_rates(table, where='', params=()):
            rows = conn.execute(
                f'SELECT t.result, c.req_name FROM {table} t '
                'JOIN matches m ON m.id=t.match_id '
                'JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id '
                f'WHERE t.result IS NOT NULL {where}', params).fetchall()
            a, g = [0, 0, 0], [0, 0, 0]
            for res, rl in rows:
                a[0 if res == 'W' else (1 if res == 'L' else 2)] += 1
                if rl == league:
                    g[0 if res == 'W' else (1 if res == 'L' else 2)] += 1
            return _hit(a), _hit(g)

        def _side(dirn, gv=None):
            # up＝上盤＝讓球方；down＝下盤＝受讓方。
            # 2026-10-25 修正：優先用 pick 本身 stored 嘅 giver（落注時盤）——
            # 尾盤事後轉向嘅場，用而家尾盤 giver 會砌錯預測隊。
            g = gv if gv in ('home', 'away') else giver
            if dirn == 'up':
                return away if g == 'away' else home
            if dirn == 'down':
                return home if g == 'away' else away
            return ''

        tools = []

        def add(key, name, is_z, pick, methods_all, methods_lg, gv=None):
            """pick=(dir欄值, added_at, result, payload)；methods_*={法:hit}。
            本場 pick 用咗嘅篩選法先至列出；gv=pick 嘅 stored giver
            （冇就用而家尾盤——2026-10-25 顯示準確性修正）。"""
            dirn, added_at, result, payload = pick
            used = []
            try:
                used = [m for m in _methods(payload) if m]
            except Exception:
                used = []
            if not used and payload and isinstance(payload, str) \
                    and payload not in ('[]', '{}', ''):
                used = [payload]
            mrows = []
            for mname in used:
                ma = methods_all.get(mname)
                ml = methods_lg.get(mname)
                if ma is None and ml is None:
                    continue
                mrows.append({'name': mname, 'all': ma, 'league': ml})
            tools.append({
                'key': key, 'name': name, 'z': bool(is_z),
                'dir': dirn, 'team': _side(dirn, gv),
                'line': line, 'odds': odds,
                'added_at': added_at, 'result': result,
                'all': None, 'league': None,   # 下填工具級
                'methods': mrows})

        def tool_rate_fill(idx, table, where='', params=()):
            ar, lgr = _plain_rates(table, where, params)
            tools[idx]['all'] = ar
            tools[idx]['league'] = lgr

        # ---- 精選W（條件喺 detail JSON conds[].note）----
        row = conn.execute(
            'SELECT direction, added_at, result, detail, giver '
            'FROM v3_featured WHERE match_id=?', (mid,)).fetchone()
        if row:
            def _methods(payload):
                d = json.loads(payload or '{}')
                return [c.get('note') or c.get('name') or ''
                        for c in (d.get('conds') or [])]
            ma, ml = _tally('detail', 'v3_featured')
            add('W', '精選W', False, row[:4], ma, ml, gv=row[4])
            tool_rate_fill(len(tools) - 1, 'v3_featured')

        # ---- 字頭工具 V1/V2（＋Z 槽）：篩選法＝合格字頭 ----
        # 注意：letters 淨係 featured/featured_z 有——V1 同 V2 共用同一套字頭
        # 邏輯（_eval_all 一併寫），所以 V1 嘅篩選法名單同源借過嚟，命中率
        # 都係照 featured 家族計（同一數據唔重複起爐灶）。
        def _letters_methods(payload):
            d = json.loads(payload or '{}')
            return list((d.get('letters') or {}).get('letters') or [])
        for key, name, tbl, is_z in (
                ('V2', '精V2', 'featured', False),
                ('V2Z', '精V2Z', 'featured_z', True)):
            row = conn.execute(
                f'SELECT direction, added_at, result, letters, giver FROM {tbl} '
                'WHERE match_id=?', (mid,)).fetchone()
            if not row:
                continue
            _methods = _letters_methods
            ma, ml = _tally('letters', tbl)
            add(key, name, is_z, row[:4], ma, ml, gv=row[4])
            tool_rate_fill(len(tools) - 1, tbl)
        for key, name, tbl, is_z, letters_tbl in (
                ('V1', '精V1', 'v1_featured', False, 'featured'),
                ('V1Z', '精V1Z', 'v1_featured_z', True, 'featured_z')):
            row = conn.execute(
                f'SELECT direction, added_at, result, giver FROM {tbl} '
                'WHERE match_id=?', (mid,)).fetchone()
            if not row:
                continue
            lt_row = conn.execute(
                f'SELECT letters FROM {letters_tbl} WHERE match_id=?',
                (mid,)).fetchone()
            _methods = _letters_methods
            ma, ml = _tally('letters', letters_tbl)
            add(key, name, is_z,
                (row[0], row[1], row[2], lt_row[0] if lt_row else None),
                ma, ml, gv=row[3])
            tool_rate_fill(len(tools) - 1, tbl)

        # ---- 格工具 7/8/12/8s：篩選法＝入選格（detail cells[].name）----
        def _cells_methods(payload):
            d = json.loads(payload or '{}')
            return [c.get('name') or c.get('cell') or ''
                    for c in (d.get('cells') or [])]
        for g, label in (('7', '精選7'), ('8', '精選8'),
                         ('12', '精選12'), ('8s', '格8s')):
            row = conn.execute(
                "SELECT direction, added_at, result, detail, giver "
                "FROM fw_grid_featured WHERE grid=? AND match_id=?", (g, mid)).fetchone()
            if not row:
                continue
            _methods = _cells_methods
            ma, ml = _tally('detail', 'fw_grid_featured',
                            "AND t.grid=?", (g,))
            add('F' + g, label, False, row[:4], ma, ml, gv=row[4])
            tool_rate_fill(len(tools) - 1, 'fw_grid_featured',
                           'AND t.grid=?', (g,))

        # ---- 8s 公式引擎：篩選法＝conds 中文法名 ----
        def _arr_methods(payload):
            return [str(x) for x in json.loads(payload or '[]')]
        row = conn.execute(
            'SELECT direction, added_at, result, conds FROM fw8s_log '
            'WHERE match_id=?', (mid,)).fetchone()
        if row:
            _methods = _arr_methods
            ma, ml = _tally('conds', 'fw8s_log')
            add('S8', '精選8s', False, row, ma, ml)
            tool_rate_fill(len(tools) - 1, 'fw8s_log')

        # ---- 8X：篩選法＝每條因子 preds ----
        row = conn.execute(
            "SELECT direction, datetime(created_ts, 'unixepoch', '+8 hours'), "
            "result, preds, giver FROM fw8x_log WHERE match_id=?", (mid,)).fetchone()
        if row:
            _methods = _arr_methods
            ma, ml = _tally('preds', 'fw8x_log')
            add('X8', '8X', False, row[:4], ma, ml, gv=row[4])
            tool_rate_fill(len(tools) - 1, 'fw8x_log')

        # ---- 88W：一場可以中多個 market+conds 組合——每個組合一條（篩選法）----
        rows = conn.execute(
            'SELECT market, conds, added_at, result FROM w88_log '
            'WHERE match_id=?', (mid,)).fetchall()
        if rows:
            _methods = lambda payload: [payload]   # payload 預製好組合名
            all_rows = conn.execute(
                'SELECT t.result, c.req_name, '
                "t.market || '＋' || t.conds FROM w88_log t "
                'JOIN matches m ON m.id=t.match_id '
                'JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id '
                'WHERE t.result IS NOT NULL').fetchall()
            a, g = {}, {}
            for res, rl, combo in all_rows:
                _bump(a, combo, res)
                if rl == league:
                    _bump(g, combo, res)
            ma = {k: _hit(v) for k, v in a.items()}
            ml = {k: _hit(v) for k, v in g.items()}
            ar, lgr = _plain_rates('w88_log')
            for mkt, conds, added_at, result in rows:
                tools.append({
                    'key': 'W88', 'name': '88W', 'z': False,
                    'dir': None, 'team': '',
                    'line': line, 'odds': odds,
                    'added_at': added_at, 'result': result,
                    'all': ar, 'league': lgr,
                    'methods': [{'name': f'{mkt}＋{conds}',
                                 'all': ma.get(f'{mkt}＋{conds}'),
                                 'league': ml.get(f'{mkt}＋{conds}')}],
                    'market': mkt})

        # ---- 新精選：篩選法＝16 欄位組合（conds 陣列）----
        row = conn.execute(
            'SELECT dir, added_at, result, conds FROM newjx_log '
            'WHERE match_id=?', (mid,)).fetchone()
        if row:
            dirn, added_at, result, conds = row
            _methods = lambda payload: [
                '＋'.join(str(x) for x in json.loads(payload or '[]'))]
            ma, ml = _tally('conds', 'newjx_log')
            add('JX', '新精選', False,
                (dirn, added_at, result, conds), ma, ml, gv=row[4])
            tool_rate_fill(len(tools) - 1, 'newjx_log')

        # ---- 好波（大小球）：預測＝大/細；篩選法＝規則 rule ----
        row = conn.execute(
            'SELECT side, added_at, result, rule FROM hb_pick_log '
            'WHERE match_id=?', (mid,)).fetchone()
        if row:
            side, added_at, result, rule = row
            _methods = lambda payload: [payload] if payload else []
            ma, ml = _tally('rule', 'hb_pick_log')
            add('HB', '好波', False,
                (side, added_at, result, rule), ma, ml)
            tools[-1]['all'], tools[-1]['league'] = _plain_rates('hb_pick_log')

        # ---- K3：篩選法＝conds ----
        row = conn.execute(
            'SELECT NULL, added_at, result, conds FROM k3_log '
            'WHERE match_id=?', (mid,)).fetchone()
        if row:
            _methods = _arr_methods
            ma, ml = _tally('conds', 'k3_log')
            add('K3', 'K3', False, row, ma, ml)
            tool_rate_fill(len(tools) - 1, 'k3_log')

        return {'ok': True, 'league': league, 'tools': tools}
    finally:
        conn.close()



def api_v3_summary(mid):
    """V3 一頁過：45 項適用情況＋精選W 六條件結果＋完場賽果（詳情頁頂＋Check 下先用）"""
    import v3_engine
    key = ('v3sum', mid)
    now = time.time()
    with _v3_item_cache_lock:
        hit = _v3_item_cache.get(key)
    if hit and now - hit[0] < V3_ITEM_CACHE_TTL:
        out = hit[1]
    else:
        conn = db()
        try:
            t = screen_engine.get_target(conn, mid)
            if not t:
                return {'error': '找不到賽事'}
            out = {'ok': True,
                   'applicability': v3_engine.item_applicability_v3(conn, t),
                   'featured_w': v3_engine.featured_w(conn, t),
                   'twin': v3_engine.twin_lookback(conn, t)}
            with _v3_item_cache_lock:
                _v3_item_cache[key] = (now, out)
        finally:
            conn.close()
    out = dict(out)
    out['result'] = _match_result(mid)
    return out


def api_v3_combo(mid, sel):
    """V3 組合篩查：第 1–38 項多選（AND），分全庫／同一聯賽／聯賽類"""
    import v3_engine
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}
        out = v3_engine.combo_v3(conn, t, sel)
        out.setdefault('ok', 'error' not in out)
        return out
    finally:
        conn.close()


def log_v3_featured(conn, mid, direction):
    """V3 過往紀錄（2026-09-28）：精選W 一出現即自動影低當刻尾盤快照。
    計一場：同一 match_id 保留首次 added_at，尾盤快照同方向更新為最新。"""
    row = conn.execute(
        "SELECT handicap, giver, home_odds, away_odds FROM odds_asian "
        "WHERE match_id=? AND company_id=12 "
        "ORDER BY (label='closing') DESC, label DESC LIMIT 1", (mid,)).fetchone()
    hc, gv, ho, ao = row if row else (None, None, None, None)
    conn.execute(
        'INSERT INTO v3_featured_log(match_id, direction, added_at, handicap, '
        'giver, home_odds, away_odds) VALUES(?,?,?,?,?,?,?) '
        'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
        'handicap=excluded.handicap, giver=excluded.giver, '
        'home_odds=excluded.home_odds, away_odds=excluded.away_odds',
        (mid, direction, time.strftime('%Y-%m-%d %H:%M:%S'), hc, gv, ho, ao))


def _fw_meta(fw):
    return json.dumps({'conds': fw.get('conds'), 'gap30': fw.get('gap30'),
                       'lookback': fw.get('lookback')},
                      ensure_ascii=False, default=_jdefault)


def _v3_featured_scan_job():
    """精選W 掃描：1A、1I、19同主客、19+互換、31、35 全部同方向≥50% 先入選。
    入選寫 v3_featured（conds/gap30 JSON）＋v3_featured_log（過往紀錄，計一場）。"""
    global _v3_scan
    if _v3_scan['running']:
        return
    _v3_scan.update(running=True, done=0, added=0, error=None, near=[])
    conn = db()
    try:
        import v3_engine
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM matches m "
            "JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 AND oc.handicap IS NOT NULL "
            "WHERE m.home_score IS NULL AND m.kickoff >= ? ORDER BY m.kickoff",
            (v3_engine.hk_now_str(),)).fetchall()
        _v3_scan['total'] = len(rows)
        v3_engine.load_v3_pool(conn)   # 預熱 V3 數據池
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
        added = 0
        for (mid,) in rows:
            try:
                t = screen_engine.get_target(conn, mid)
                if not t:
                    continue
                fw = v3_engine.featured_w(conn, t)
                if fw.get('pass'):
                    d = fw['direction']
                    phc, pgv = _tgt_pick_line(conn, t, mid)
                    conn.execute(
                        'INSERT INTO v3_featured(match_id, direction, detail, '
                        'added_at, hc, giver) VALUES(?,?,?,?,?,?) '
                        'ON CONFLICT(match_id) DO UPDATE SET '
                        'direction=excluded.direction, detail=excluded.detail, '
                        'hc=excluded.hc, giver=excluded.giver',
                        (mid, d, _fw_meta(fw), time.strftime('%Y-%m-%d %H:%M:%S'),
                         phc, pgv))
                    log_v3_featured(conn, mid, d)
                    conn.commit()
                    added += 1
                elif (fw.get('any5') or fw.get('any4')) \
                        and len(_v3_scan['near']) < 60:
                    # Any5／Any4 近合格：記低俾精選W 頁展示
                    info = conn.execute(
                        'SELECT m.kickoff, ht.name_tc, at.name_tc, c.req_name, '
                        'oc.handicap, oc.giver '
                        'FROM matches m '
                        'JOIN teams ht ON ht.titan_id=m.home_id '
                        'JOIN teams at ON at.titan_id=m.away_id '
                        'JOIN seasons s ON s.id=m.season_id '
                        'JOIN competitions c ON c.titan_id=s.titan_id '
                        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
                        "AND oc.label='closing' AND oc.company_id=12 "
                        'WHERE m.id=?', (mid,)).fetchone()
                    if info:
                        # 每條件嘅方向/% 一併帶上，前端 Any5/Any4 先顯示到
                        # 同六項全中一樣嘅 % 細節（用戶 2026-09-28 指示）；
                        # any_lb＝Any5/Any4 首個組合嘅四口子回查（2026-09-29）
                        slim_conds = [{'dir': c.get('dir'), 'note': c.get('note'),
                                       'oc': c.get('oc'), 'zone_r': c.get('zone_r')}
                                      for c in (fw.get('conds') or [])]
                        _v3_scan['near'].append({
                            'id': mid, 'kickoff': info[0], 'home': info[1],
                            'away': info[2], 'league': info[3],
                            'any5': fw.get('any5') or [],
                            'any4': fw.get('any4') or [],
                            'any_lb': fw.get('any_lb') or {},
                            'conds': slim_conds})
            except Exception:
                import traceback
                traceback.print_exc()
            _v3_scan['done'] += 1
        _v3_scan['added'] = added
        _v3_scan['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        _v3_scan['error'] = str(e)
    finally:
        conn.close()
        _v3_scan['running'] = False


def do_v3_featured_refresh(mid):
    """精選W 單場重新整理：重抓該場最新盤口賠率，重算精選W 資格。
    仍合準則 → 保留／更新方向；唔再合 → 未結算移出；已完場歷史保留。"""
    import v3_engine
    st = do_fetch(mid, True)
    if not st.get('ok'):
        return {'ok': False, 'error': st.get('error', '抓取賠率失敗'), 'fetch': st}
    conn = db()
    try:
        screen_engine._pool_cache['ts'] = 0
        v3_engine.invalidate_pool()
        with _v3_item_cache_lock:
            _v3_item_cache.clear()
        t = screen_engine.get_target(conn, mid)
        fw = v3_engine.featured_w(conn, t) if t else {'pass': False}
        row = conn.execute('SELECT result FROM v3_featured WHERE match_id=?',
                           (mid,)).fetchone()
        removed = False
        if fw.get('pass'):
            d = fw['direction']
            try:
                _migrate_pick_line_cols(conn)
            except sqlite3.Error:
                pass
            phc, pgv = _tgt_pick_line(conn, t, mid)
            conn.execute(
                'INSERT INTO v3_featured(match_id, direction, detail, added_at, '
                'hc, giver) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
                'detail=excluded.detail, hc=excluded.hc, giver=excluded.giver',
                (mid, d, _fw_meta(fw), time.strftime('%Y-%m-%d %H:%M:%S'),
                 phc, pgv))
            log_v3_featured(conn, mid, d)
        elif row and row[0] is None:
            conn.execute('DELETE FROM v3_featured WHERE match_id=?', (mid,))
            removed = True
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'pass': bool(fw.get('pass')),
            'direction': fw.get('direction'), 'removed': removed}


def _build_v3_rec(row, now):
    """精選W 單場卡片：基本資料＋狀態＋自動結算（以入選方向計 贏/輸/走）＋conds/gap30
    結算用落注時影低嘅 stored hc/giver；legacy 冇 stored 先對 closing。"""
    (mid, d, res, detail, added, ko, hs, aws, hh, ha, fg, h, a, lg, hc, gv,
     ho, ao, hr_, ar_, shc, sgv, sho, sao, sadded, phc, pgv) = row
    rec = {'id': mid, 'direction': d, 'added_at': added, 'kickoff': ko,
           'home': h, 'away': a, 'league': lg, 'rank_home': hr_, 'rank_away': ar_,
           'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
           'odds': f'主{ho}/客{ao}' if ho is not None else None,
           # 上盤水位（讓球方水位；平手盤＝主隊水位）→ 前端 highlight 今場水位段
           'up_odds': (ho if gv in ('home', None, 'none') else ao)
                      if ho is not None else None,
           'giver': gv,
           'home_odds': ho, 'away_odds': ao,
           'state': 'finished' if hs is not None else (
               'live' if ko <= now else 'scheduled')}
    # 2026-10-20：入選/上次檢定時嘅尾盤 vs 而家尾盤——有重大變動就標示，
    # 等用戶知道張卡嘅六條件係邊條線下面評出嚟（資格透明度）
    if shc is not None:
        rec['line_at_pick'] = (screen_engine.fmt_line(shc, sgv)
                               + (f' 主{sho}/客{sao}'
                                  if sho is not None else ''))
        rec['snap_at'] = sadded
        moved = False
        if hc is not None:
            if abs(abs(shc) - abs(hc)) >= _LINE_MOVE_H - 1e-9:
                moved = True
            elif (sgv or None) != (gv or None) and (shc or hc):
                moved = True
            else:
                for x, y in ((sho, ho), (sao, ao)):
                    if x is not None and y is not None \
                            and abs(x - y) >= _LINE_MOVE_W:
                        moved = True
                        break
        rec['line_moved'] = moved
    if detail:
        try:
            det = json.loads(detail)
            rec['conds'] = det.get('conds')
            rec['gap30'] = det.get('gap30')
            rec['lookback'] = det.get('lookback')
        except Exception:
            pass
    if hs is not None:
        pkh, pkg = _stored_first(phc, pgv, hc, gv)
        r = pick_result(pkh, pkg, hs, aws)
        if res is None and r is not None:
            res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
            c2 = db()
            c2.execute('UPDATE v3_featured SET result=?, settled_at=? WHERE match_id=?',
                       (res, now, mid))
            c2.commit()
            c2.close()
        rec['score'] = f'{hs}-{aws}'
        rec['result'] = res
    if hh is not None:
        rec['ht'] = f'{hh}-{ha}'
    if fg in ('home', 'away'):
        rec['first_goal'] = fg
    return rec


def get_v3_featured_full():
    """精選W 全量：未開賽（順開賽時間）→ 進行中 → 已完場（最新排先，永不刪除）。
    頁頂統計：場數／命中場數／命中率。"""
    import v3_engine
    conn = db()
    try:
        _migrate_pick_line_cols(conn)
    except sqlite3.Error:
        pass
    now = v3_engine.hk_now_str()
    rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.detail, f.added_at, '
        'm.kickoff, m.home_score, m.away_score, m.half_home, m.half_away, '
        '(SELECT g.side FROM match_goals g WHERE g.match_id=m.id '
        'ORDER BY g.seq LIMIT 1) AS fg, ht.name_tc, at.name_tc, '
        'c.req_name, oc.handicap, oc.giver, oc.home_odds, oc.away_odds, '
        'COALESCE(ps.home_total_rank, hr.rank), COALESCE(ps.away_total_rank, ar.rank), '
        # 2026-10-20：入選/上次檢定時嘅尾盤快照（v3_featured_log）——同而家尾盤
        # 對比，變動大就喺卡片出警告（資格透明度，用戶 10-20 投訴修正）
        'vl.handicap, vl.giver, vl.home_odds, vl.away_odds, vl.added_at, '
        'f.hc, f.giver '
        'FROM v3_featured f JOIN matches m ON m.id=f.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'LEFT JOIN v3_featured_log vl ON vl.match_id=f.match_id '
        'LEFT JOIN match_prestandings ps ON ps.match_id=m.id '
        "LEFT JOIN standings hr ON hr.season_id=m.season_id AND hr.team_id=m.home_id "
        "AND hr.scope='total' AND hr.grp='' "
        "LEFT JOIN standings ar ON ar.season_id=m.season_id AND ar.team_id=m.away_id "
        "AND ar.scope='total' AND ar.grp='' "
        'ORDER BY m.kickoff').fetchall()
    conn.close()
    recs = []
    for r in rows:
        try:
            recs.append(_build_v3_rec(r, now))
        except Exception:
            traceback.print_exc()
    up = sorted([r for r in recs if r['state'] == 'scheduled'],
                key=lambda r: r['kickoff'])
    live = sorted([r for r in recs if r['state'] == 'live'],
                  key=lambda r: r['kickoff'])
    fin = sorted([r for r in recs if r['state'] == 'finished'],
                 key=lambda r: r['kickoff'], reverse=True)
    played_all = live + fin
    wins = sum(1 for r in played_all if r.get('result') == 'W')
    losses = sum(1 for r in played_all if r.get('result') == 'L')
    pushes = sum(1 for r in played_all if r.get('result') == 'P')
    stats = {'total': len(recs), 'pending': len(up), 'live': len(live),
             'played': len(played_all), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    return {'stats': stats, 'pending': up, 'live': live, 'played': played_all,
            'near': sorted(_v3_scan.get('near', []),
                          key=lambda r: r.get('kickoff') or ''),
            'scan': {k: _v3_scan[k] for k in
                     ('running', 'done', 'total', 'added', 'last', 'error')}}


def get_v3_featlog():
    """V3 過往紀錄：精選W 自動紀錄嘅尾盤快照（永久保留，計一場）。
    結算以 log 影低嘅尾盤快照計，唔係而家嘅 closing。"""
    import v3_engine
    conn = db()
    now = v3_engine.hk_now_str()
    rows = conn.execute(
        'SELECT l.match_id, l.direction, l.added_at, l.handicap, l.giver, '
        'l.home_odds, l.away_odds, m.kickoff, m.home_score, m.away_score, '
        'ht.name_tc, at.name_tc, c.req_name '
        'FROM v3_featured_log l JOIN matches m ON m.id=l.match_id '
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


# ============ 精選7／精選8／精選12＋Check 一下（2026-09-28 減格實驗定稿） ============
# 7 格＝1I、3A、3I、7、19、21、25（驗證期下盤70.0%，851場合格）
# 8 格＝1I、1O、3A、3I、7、19、21、25（驗證期下盤70.0%，531場合格）
# 12 格＝全部 12 格（驗證期樣本太少，命中率參考為主）
_fw_grid_scan = {g: {'running': False, 'done': 0, 'total': 0, 'added': 0,
                     'last': None, 'error': None} for g in ('7', '8', '12', '8s')}
GRID_NAMES = {'7': '精選7', '8': '精選8', '12': '精選12', '8s': '精選8s'}
GRID_DEFS = {'7': '1I、3A、3I、7、19、21、25',
             '8': '1I、1O、3A、3I、7、19、21、25',
             '12': '1A、1G、1I、1O、3A、3G、3I、3O、7、19、21、25（全部12格）',
             '8s': '專搵上盤：6 個聯賽獨立公式（智利甲/美冠聯＝格共識；歐霸盃/澳維超/瑞典盃/歐國聯＝同向&客讓0.75）'}


def _fw_grid_meta(rep):
    """detail JSON 瘦身版：12 格方向＋嚴格回查＋fail 原因。"""
    slim_cells = [{'cell': c['cell'], 'name': c['name'], 'dir': c['dir'],
                   'n': c['n'], 'up_r': c.get('up_r'), 'down_r': c.get('down_r')}
                  for c in rep.get('cells', [])]
    return json.dumps({'cells': slim_cells, 'strict': rep.get('strict'),
                       'fail': rep.get('fail'), 'grid': rep.get('grid'),
                       'grid_cells': rep.get('grid_cells')},
                      ensure_ascii=False)


def _fw_grid_scan_job(g):
    """精選7/8/12 掃描：未開賽逐場跑格引擎，合格寫 fw_grid_featured。"""
    st = _fw_grid_scan[g]
    if st['running']:
        return
    st.update(running=True, done=0, added=0, error=None)
    conn = db()
    try:
        import fw_grid_engine
        import fw8s_engine
        import v3_engine
        conn.execute('''CREATE TABLE IF NOT EXISTS fw_grid_featured(
            grid TEXT NOT NULL, match_id INTEGER NOT NULL, direction TEXT NOT NULL,
            detail TEXT, added_at TEXT, result TEXT, settled_at TEXT,
            hc REAL, giver TEXT,
            PRIMARY KEY(grid, match_id))''')
        conn.commit()
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
        rows = conn.execute(
            "SELECT DISTINCT m.id FROM matches m "
            "JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 AND oc.handicap IS NOT NULL "
            "WHERE m.home_score IS NULL AND m.kickoff >= ? ORDER BY m.kickoff",
            (v3_engine.hk_now_str(),)).fetchall()
        st['total'] = len(rows)
        v3_engine.load_v3_pool(conn)   # 預熱 V3 數據池
        added = 0
        for (mid,) in rows:
            try:
                t = screen_engine.get_target(conn, mid)
                if not t:
                    continue
                rep = (fw8s_engine.fw8s_report(conn, t) if g == '8s'
                       else fw_grid_engine.grid_report(conn, t, g))
                if rep.get('pass'):
                    d = rep['direction']
                    phc, pgv = _tgt_pick_line(conn, t, mid)
                    conn.execute(
                        'INSERT INTO fw_grid_featured(grid, match_id, direction, '
                        'detail, added_at, hc, giver) VALUES(?,?,?,?,?,?) '
                        'ON CONFLICT(grid, match_id) DO UPDATE SET '
                        'direction=excluded.direction, detail=excluded.detail, '
                        'hc=excluded.hc, giver=excluded.giver',
                        (g, mid, d,
                         fw8s_engine.fw8s_meta(rep) if g == '8s'
                         else _fw_grid_meta(rep),
                         time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
                    conn.commit()
                    added += 1
            except Exception:
                traceback.print_exc()
            st['done'] += 1
        st['added'] = added
        st['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        st['error'] = str(e)
    finally:
        conn.close()
        st['running'] = False


def _build_fw_grid_rec(row, now, g):
    """精選7/8/12 單場卡片（結構同 _build_v3_rec）。
    結算用落注時影低嘅 stored hc/giver；legacy 冇 stored 先對 closing。"""
    (mid, d, res, detail, added, ko, hs, aws, hh, ha, fg, h, a, lg, hc, gv,
     ho, ao, phc, pgv) = row
    rec = {'id': mid, 'direction': d, 'added_at': added, 'kickoff': ko,
           'home': h, 'away': a, 'league': lg, 'grid': g,
           'line': screen_engine.fmt_line(hc, gv) if hc is not None else None,
           'odds': f'主{ho}/客{ao}' if ho is not None else None,
           'up_odds': (ho if gv in ('home', None, 'none') else ao)
                      if ho is not None else None,
           'giver': gv, 'home_odds': ho, 'away_odds': ao,
           'state': 'finished' if hs is not None else (
               'live' if ko <= now else 'scheduled')}
    if detail:
        try:
            det = json.loads(detail)
            rec['cells'] = det.get('cells')
            rec['strict'] = det.get('strict')
        except Exception:
            pass
    if hs is not None:
        pkh, pkg = _stored_first(phc, pgv, hc, gv)
        r = pick_result(pkh, pkg, hs, aws)
        if res is None and r is not None:
            res = 'P' if r == 'P' else ('W' if (r == 'A') == (d == 'up') else 'L')
            c2 = db()
            c2.execute('UPDATE fw_grid_featured SET result=?, settled_at=? '
                       'WHERE grid=? AND match_id=?', (res, now, g, mid))
            c2.commit()
            c2.close()
        rec['score'] = f'{hs}-{aws}'
        rec['result'] = res
    if hh is not None:
        rec['ht'] = f'{hh}-{ha}'
    if fg in ('home', 'away'):
        rec['first_goal'] = fg
    return rec


def get_fw_grid_full(g):
    """精選7/8/12 全量：未開賽→進行中→已完場（最新排先，永不刪除）。"""
    import v3_engine
    conn = db()
    conn.execute('''CREATE TABLE IF NOT EXISTS fw_grid_featured(
        grid TEXT NOT NULL, match_id INTEGER NOT NULL, direction TEXT NOT NULL,
        detail TEXT, added_at TEXT, result TEXT, settled_at TEXT,
        hc REAL, giver TEXT,
        PRIMARY KEY(grid, match_id))''')
    conn.commit()
    try:
        _migrate_pick_line_cols(conn)
    except sqlite3.Error:
        pass
    now = v3_engine.hk_now_str()
    rows = conn.execute(
        'SELECT f.match_id, f.direction, f.result, f.detail, f.added_at, '
        'm.kickoff, m.home_score, m.away_score, m.half_home, m.half_away, '
        '(SELECT g.side FROM match_goals g WHERE g.match_id=m.id '
        'ORDER BY g.seq LIMIT 1) AS fg, ht.name_tc, at.name_tc, '
        'c.req_name, oc.handicap, oc.giver, oc.home_odds, oc.away_odds, '
        'f.hc, f.giver '
        'FROM fw_grid_featured f JOIN matches m ON m.id=f.match_id '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id '
        'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
        "AND oc.label='closing' AND oc.company_id=12 "
        'WHERE f.grid=? ORDER BY m.kickoff', (g,)).fetchall()
    conn.close()
    recs = []
    for r in rows:
        try:
            recs.append(_build_fw_grid_rec(r, now, g))
        except Exception:
            traceback.print_exc()
    up = sorted([r for r in recs if r['state'] == 'scheduled'],
                key=lambda r: r['kickoff'])
    live = sorted([r for r in recs if r['state'] == 'live'],
                  key=lambda r: r['kickoff'])
    fin = sorted([r for r in recs if r['state'] == 'finished'],
                 key=lambda r: r['kickoff'], reverse=True)
    played_all = live + fin
    wins = sum(1 for r in played_all if r.get('result') == 'W')
    losses = sum(1 for r in played_all if r.get('result') == 'L')
    pushes = sum(1 for r in played_all if r.get('result') == 'P')
    stats = {'total': len(recs), 'pending': len(up), 'live': len(live),
             'played': len(played_all), 'wins': wins, 'losses': losses,
             'pushes': pushes,
             'hit_rate': wins / (wins + losses) if (wins + losses) else None}
    st = _fw_grid_scan[g]
    return {'stats': stats, 'pending': up, 'live': live, 'played': played_all,
            'grid': g, 'grid_name': GRID_NAMES[g], 'grid_def': GRID_DEFS[g],
            'scan': {k: st[k] for k in
                     ('running', 'done', 'total', 'added', 'last', 'error')}}


def do_fw_grid_refresh(mid, g):
    """精選7/8/12/8s 單場重新整理：重抓賠率→重算資格；唔再合→未結算移出。"""
    import fw_grid_engine
    import fw8s_engine
    st = do_fetch(mid, True)
    if not st.get('ok'):
        return {'ok': False, 'error': st.get('error', '抓取賠率失敗'), 'fetch': st}
    conn = db()
    try:
        screen_engine._pool_cache['ts'] = 0
        import v3_engine
        v3_engine.invalidate_pool()
        with _v3_item_cache_lock:
            _v3_item_cache.clear()
        t = screen_engine.get_target(conn, mid)
        if g == '8s':
            rep = fw8s_engine.fw8s_report(conn, t) if t else {'pass': False}
        else:
            rep = fw_grid_engine.grid_report(conn, t, g) if t else {'pass': False}
        row = conn.execute('SELECT result FROM fw_grid_featured '
                           'WHERE grid=? AND match_id=?', (g, mid)).fetchone()
        removed = False
        if rep.get('pass'):
            d = rep['direction']
            try:
                _migrate_pick_line_cols(conn)
            except sqlite3.Error:
                pass
            phc, pgv = _tgt_pick_line(conn, t, mid)
            conn.execute(
                'INSERT INTO fw_grid_featured(grid, match_id, direction, '
                'detail, added_at, hc, giver) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(grid, match_id) DO UPDATE SET '
                'direction=excluded.direction, detail=excluded.detail, '
                'hc=excluded.hc, giver=excluded.giver',
                (g, mid, d,
                 fw8s_engine.fw8s_meta(rep) if g == '8s' else _fw_grid_meta(rep),
                 time.strftime('%Y-%m-%d %H:%M:%S'), phc, pgv))
        elif row and row[0] is None:
            conn.execute('DELETE FROM fw_grid_featured WHERE grid=? AND match_id=?',
                         (g, mid))
            removed = True
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'pass': bool(rep.get('pass')),
            'direction': rep.get('direction'), 'removed': removed}


def get_fwcheck_full(g):
    """Check 一下 7/8/12：離線預計算嘅 8 情境回測表（_fwcheck{g}.json）。"""
    p = os.path.join(BASE_DIR, f'_fwcheck{g}.json')
    if not os.path.exists(p):
        return {'error': f'未有 Check 一下{g} 數據（要喺電腦跑一次 _fw_check_grid.py '
                         f'再同步上雲端）'}
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def get_fw12_audit():
    """精選12 全庫重篩永久紀錄（fw_screen_audit.db grid='12'）。
    本機讀實時審計庫；雲端冇審計庫就讀 fw12_audit.json 快照
    （tools/fw12_snapshot.py 產生，跟 repo 永久保留）。
    2026-10-19 用戶規格：用精選12掃全庫，合資格嘅展示結果在頁面，永久保留。"""
    import glob as _glob
    # 1) 實時審計庫（本機）
    cands = []
    _ws = os.path.dirname(BASE_DIR)          # screen_app 上一層 = 工作區
    cands.append(os.path.join(_ws, 'fw_screen_audit.db'))
    cands.append(os.path.join(BASE_DIR, 'fw_screen_audit.db'))
    cands.extend(sorted(_glob.glob('/data/fw_screen_audit.db'),
                        reverse=True))
    out = None
    for adb in cands:
        if not os.path.isfile(adb):
            continue
        # 掃描進行中寫入方會鎖 WAL——ro 撞鎖就轉 immutable（讀到嘅係最近
        # checkpoint 快照，展示用途足夠），唔會彈「尚未完成」假訊息
        a = None
        for uri in ('file:%s?mode=ro' % adb, 'file:%s?immutable=1' % adb):
            try:
                a = sqlite3.connect(uri, uri=True, timeout=8)
                a.execute('SELECT 1').fetchone()
                break
            except sqlite3.OperationalError:
                try:
                    a.close()
                except Exception:
                    pass
                a = None
        if a is None:
            continue
        try:
            tot = a.execute("SELECT COUNT(*) FROM fw_screen_audit "
                            "WHERE grid='12'").fetchone()[0]
            if tot == 0:
                a.close()
                continue
            rows = a.execute(
                "SELECT f.match_id, f.kickoff, f.league, f.category, "
                "f.direction, f.result, f.line_h, f.line_g, f.up_water, "
                "f.close_ho, f.close_ao, f.pass, f.detail, f.settled_at "
                "FROM fw_screen_audit f WHERE f.grid='12' AND f.pass=1 "
                "ORDER BY f.kickoff DESC").fetchall()
            a.close()
            # 用主庫補球隊名＋比數
            conn = db()
            names = {}
            for r in rows:
                m = conn.execute(
                    "SELECT ht.name_tc, at.name_tc, m.home_score, "
                    "m.away_score FROM matches m "
                    "JOIN teams ht ON ht.titan_id=m.home_id "
                    "JOIN teams at ON at.titan_id=m.away_id "
                    "WHERE m.id=?", (r[0],)).fetchone()
                names[r[0]] = m
            conn.close()
            items = []
            for r in rows:
                nm = names.get(r[0]) or (None, None, None, None)
                d = None
                try:
                    d = json.loads(r[12]) if r[12] else None
                except Exception:
                    d = None
                items.append({
                    'mid': r[0], 'kickoff': r[1], 'league': r[2],
                    'category': r[3], 'direction': r[4], 'result': r[5],
                    'line_h': r[6], 'line_g': r[7], 'up_water': r[8],
                    'close_ho': r[9], 'close_ao': r[10],
                    'home': nm[0], 'away': nm[1],
                    'hs': nm[2], 'aws': nm[3], 'cells': (d or {}).get('cells'),
                    'settled_at': r[13]})
            a2 = None
            for uri in ('file:%s?mode=ro' % adb, 'file:%s?immutable=1' % adb):
                try:
                    a2 = sqlite3.connect(uri, uri=True, timeout=8)
                    a2.execute('SELECT 1').fetchone()
                    break
                except sqlite3.OperationalError:
                    try:
                        a2.close()
                    except Exception:
                        pass
                    a2 = None
            if a2 is None:
                raise sqlite3.OperationalError('audit db locked')
            sm = a2.execute(
                "SELECT COUNT(*), SUM(pass), "
                "SUM(CASE WHEN pass=1 AND result='W' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pass=1 AND result='L' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pass=1 AND result='P' THEN 1 ELSE 0 END) "
                "FROM fw_screen_audit WHERE grid='12'").fetchone()
            lg_rows = a2.execute(
                "SELECT league, COUNT(*), "
                "SUM(CASE WHEN result='W' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN result='L' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN result='P' THEN 1 ELSE 0 END) "
                "FROM fw_screen_audit WHERE grid='12' AND pass=1 "
                "GROUP BY league ORDER BY 2 DESC").fetchall()
            a2.close()
            screened, npass, w, l, p = sm
            decided = (w or 0) + (l or 0)
            out = {
                'source': 'live',
                'screened': screened, 'pass': npass or 0,
                'w': w or 0, 'l': l or 0, 'p': p or 0,
                'hit_rate': round(100.0 * w / decided, 1) if decided else None,
                'leagues': [{'league': x[0], 'n': x[1], 'w': x[2] or 0,
                             'l': x[3] or 0, 'p': x[4] or 0,
                             'hit_rate': (round(100.0 * (x[2] or 0) /
                                                ((x[2] or 0) + (x[3] or 0)), 1)
                                          if ((x[2] or 0) + (x[3] or 0)) else None)}
                            for x in lg_rows],
                'items': items}
            break
        except sqlite3.OperationalError:
            continue
    if out is None:
        # 2) 快照 JSON（雲端永久保留用）
        for pth in (os.path.join(BASE_DIR, 'fw12_audit.json'),
                    os.path.join(os.path.dirname(BASE_DIR), 'fw12_audit.json')):
            if os.path.isfile(pth):
                with open(pth, encoding='utf-8') as f:
                    out = json.load(f)
                out['source'] = 'snapshot'
                break
    if out is None:
        out = {'error': '精選12全庫重篩尚未完成或未有數據'}
    # 3) 進度（掃描狀態）
    try:
        stf = os.path.join(BASE_DIR, 'logs', 'fw_screen12_state.json')
        if os.path.isfile(stf):
            out['scan'] = json.load(open(stf, encoding='ascii'))
    except Exception:
        pass
    return out


def api_fwcheck_target(mid, g):
    """Check 一下 7/8/12 單場檢驗：格方向＋情境歸類＋嗰個情境嘅歷史開出率。"""
    import fw_grid_engine
    conn = db()
    try:
        t = screen_engine.get_target(conn, mid)
        if not t:
            return {'error': '找不到賽事'}
        rep = fw_grid_engine.grid_report(conn, t, g)
        out = {'ok': True, 'grid': g, 'report': rep}
        key = (rep.get('scenario') or {}).get('key')
        if key:
            tbl = get_fwcheck_full(g)
            for s in (tbl.get('scenarios') or []):
                if f"{s['dir']}|{s['mode_gap']}|{s['d50_gap']}" == key:
                    out['scenario_stats'] = s
                    break
        return out
    finally:
        conn.close()


# ============ 大小規則（2026-10-09：分聯賽 O/U 規則版，接入 15 分鐘循環） ============
# 引擎係純 SQL＋規則評估、無狀態表：每次掃描全量重算，結果放 module-level cache
# （同 z 槽思路——頁面請求永遠讀快取，刷新交畀掃描循環），已完場卡自動結算。
_ou_rules_scan = {'running': False, 'done': 0, 'total': 0, 'added': 0,
                  'last': None, 'error': None}
_ou_rules_cache = {'ts': 0.0, 'data': None}
_OU_RULES_TTL = 960   # 掃描循環 900 秒，TTL 稍長啲兜住循環延遲


def _ou_rules_scan_job():
    """大小規則掃描：未來48小時場次＋近7日已完場場次行規則引擎，結果入 cache。"""
    st = _ou_rules_scan
    if st['running']:
        return
    st.update(running=True, error=None)
    try:
        import ou_rules_engine
        data = ou_rules_engine.scan(48)
        stats = data.get('stats') or {}
        st['done'] = st['total'] = stats.get('scanned', 0)
        st['added'] = stats.get('upcoming', 0)
        _ou_rules_cache.update(ts=time.time(), data=data)
        st['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        st['error'] = traceback.format_exc()[-400:]
    finally:
        st['running'] = False


def get_ou_rules_full():
    """大小規則全量：cache 新鮮直讀；過期／冷啟動即場重建（引擎好快，毫秒級）。
    已完場卡讀取時自動結算——呢個係無狀態設計，規則卡一換即刻全量重評。"""
    if _ou_rules_cache['data'] is None or \
            time.time() - _ou_rules_cache['ts'] > _OU_RULES_TTL:
        _ou_rules_scan_job()   # 有 running 閘，唔會同背景掃描疊行
    data = _ou_rules_cache['data']
    if data is None:
        return {'upcoming': [], 'played': [], 'stats': {}, 'scan': dict(_ou_rules_scan)}
    out = dict(data)
    out['scan'] = {k: _ou_rules_scan[k] for k in
                   ('running', 'done', 'total', 'added', 'last', 'error')}
    return out


# ============ 半全場方法（2026-10-10：分聯賽 HT/FT 方法版，接入 15 分鐘循環） ============
# 引擎係純 SQL＋方法評估、無狀態表：每次掃描全量重算，結果放 module-level cache
# （同大小規則思路——頁面請求永遠讀快取，刷新交畀掃描循環），已完場卡自動結算。
_htft_rules_scan = {'running': False, 'done': 0, 'total': 0, 'added': 0,
                    'last': None, 'error': None}
_htft_rules_cache = {'ts': 0.0, 'data': None}
_HTFT_RULES_TTL = 960   # 掃描循環 900 秒，TTL 稍長啲兜住循環延遲


def _htft_rules_scan_job():
    """半全場方法掃描：未來48小時場次＋近7日已完場場次行 HT/FT 方法引擎，結果入 cache。"""
    st = _htft_rules_scan
    if st['running']:
        return
    st.update(running=True, error=None)
    try:
        import htft_rules_engine
        data = htft_rules_engine.scan(48)
        stats = data.get('stats') or {}
        st['done'] = st['total'] = stats.get('scanned', 0)
        st['added'] = stats.get('upcoming', 0)
        _htft_rules_cache.update(ts=time.time(), data=data)
        st['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        st['error'] = traceback.format_exc()[-400:]
    finally:
        st['running'] = False


def get_htft_rules_full():
    """半全場方法全量：cache 新鮮直讀；過期／冷啟動即場重建（引擎好快，毫秒級）。
    已完場卡讀取時自動結算——呢個係無狀態設計，方法卡一換即刻全量重評。"""
    if _htft_rules_cache['data'] is None or \
            time.time() - _htft_rules_cache['ts'] > _HTFT_RULES_TTL:
        _htft_rules_scan_job()   # 有 running 閘，唔會同背景掃描疊行
    data = _htft_rules_cache['data']
    if data is None:
        return {'upcoming': [], 'played': [], 'stats': {}, 'scan': dict(_htft_rules_scan)}
    out = dict(data)
    out['scan'] = {k: _htft_rules_scan[k] for k in
                   ('running', 'done', 'total', 'added', 'last', 'error')}
    # 2026-10-21：四項目標（主和/主客/客和/客主）細分 幾中幾＋%
    _bt = {}
    for _r in out.get('played') or []:
        _t, _s = _r.get('target'), _r.get('settle')
        if not _t or _s not in ('贏', '輸'):
            continue
        _e = _bt.setdefault(_t, {'n': 0, 'w': 0, 'l': 0,
                                 'name': _r.get('target_name') or _t})
        _e['n'] += 1
        _e['w' if _s == '贏' else 'l'] += 1
    out['by_target'] = _bt
    return out


# ============ 📊 預測結果統計（完場場次逐來源結算一覽，2026-10-11 新增） ============
# 一頁過睇晒每場比賽各來源（上下盤／精選V2／V1／精選W／精選7/8/12／我的選擇／好波大小）
# 方向＋結算（贏/輸/走）。結算口徑：讓球 source 對落注時盤（各表 stored
# hc/giver；legacy 冇 stored 先對 company12 closing 亞盤
# （odds_asian company_id=12 label='closing'）＋全場比分；direction up＝揀讓球方
# （上盤）。用戶揀嘅場次對「揀嗰刻」影低嘅 pick 線結算。
_PREDSTATS_TTL = 120
_PREDSTATS_CACHE = {'key': None, 'ts': 0.0, 'data': None}

_predstats_lock = threading.Lock()
# source key → 中文名（前端 stat chip 同表頭共用）
_PREDSTATS_SOURCES = (
    ('main', '上下盤'), ('v2', '精選V2'), ('v1', 'V1'), ('v3', '精選W'),
    ('f7', '精選7'), ('f8', '精選8'), ('f12', '精選12'),
    ('user', '我的選擇'), ('ou', '好波大小'))


def _ps_settle_dir(direction, hc, gv, hs, aws):
    """以入選方向計 贏/輸/走（direction='up'＝揀讓球方/上盤）。
    featured 系列 result 欄已有值就先用；冇先對尾盤計。冇線/冇方向返回 None。"""
    r = pick_result(hc, gv, hs, aws)
    if r is None or direction not in ('up', 'down'):
        return None
    if r == 'P':
        return '走'
    return '贏' if (r == 'A') == (direction == 'up') else '輸'


def _ps_side_verdict(side, hc, gv, hs, aws):
    """prediction_log 讓球方向結算：上盤=讓球方、下盤=受让方；
    主勝盤/客勝盤（平手）＝揀主/客隊。返回 贏/輸/走 或 None。"""
    r = pick_result(hc, gv, hs, aws)
    if r is None:
        return None
    if side == '上盤':
        pg = True
    elif side == '下盤':
        pg = False
    elif side == '主勝盤':
        pg = (gv != 'away')      # 主隊=讓球方，除非客讓
    elif side == '客勝盤':
        pg = (gv == 'away')
    else:
        return None
    if r == 'P':
        return '走'
    return '贏' if (r == 'A') == pg else '輸'


def _ps_htft_cell(hh, ha, hs, aws):
    """9 格類型 HH..AA（半場結果+全場結果，H=主/D=和/A=客）；半場冇資料返回 None。
    半場領先方最終唔贏：XH={HD,HA}／XA={AD,AA}。"""
    if hh is None or ha is None:
        return None
    hl_ = 'H' if hh > ha else ('A' if ha > hh else 'D')
    fl_ = 'H' if hs > aws else ('A' if aws > hs else 'D')
    return hl_ + fl_


def get_predstats(days=30, lg=''):
    """鎖＋舊錄兜底（2026-10-15 第二輪 debug）——同 _oupred_tables 同款：
    TTL 到期時一個先建，其他人食舊錄，唔再 N 線程同時全庫掃描互搶。
    起緊樓期間帶新參數嘅請求會暫食舊 key 嘅錄（好過排隊假死）。"""
    now = time.time()
    days = int(days or 30)
    lg = (lg or '').strip()
    key = (days, lg)
    c = _PREDSTATS_CACHE
    if c['data'] is not None and c['key'] == key             and now - c['ts'] < _PREDSTATS_TTL:
        return c['data']
    if _predstats_lock.acquire(blocking=False):
        try:
            return _predstats_build(days, lg)   # 內部有 cache 複查
        finally:
            _predstats_lock.release()
    if c['data'] is not None:
        return c['data']
    with _predstats_lock:
        return _predstats_build(days, lg)


def _predstats_build(days=30, lg=''):
    """完場場次（近 days 日，lg 前綴過濾，最多 500 場 desc）逐來源結算。
    days<=0 ＝全部；lg 為空 ＝全部聯賽。"""
    now = time.time()
    days = int(days or 30)
    lg = (lg or '').strip()
    key = (days, lg)
    if _PREDSTATS_CACHE['data'] is not None \
            and _PREDSTATS_CACHE['key'] == key \
            and now - _PREDSTATS_CACHE['ts'] < _PREDSTATS_TTL:
        return _PREDSTATS_CACHE['data']
    conn = db()
    try:
        cond = ['m.home_score IS NOT NULL']
        args = []
        if days > 0:
            cond.append("m.kickoff >= datetime('now','+8 hours', ?)")
            args.append(f'-{days} days')
        lg_cond = ''
        if lg:
            lg_cond = ' AND c.req_name LIKE ?'
            args.append(lg + '%')
        rows = conn.execute(
            'SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc, '
            'm.home_score, m.away_score, m.half_home, m.half_away, '
            'oc.handicap, oc.giver FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'LEFT JOIN odds_asian oc ON oc.match_id=m.id '
            "AND oc.label='closing' AND oc.company_id=12 "
            'AND oc.handicap IS NOT NULL '
            'WHERE ' + ' AND '.join(cond) + lg_cond +
            ' ORDER BY m.kickoff DESC LIMIT 500', args).fetchall()
        mids = [r[0] for r in rows]

        _tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        def _map1(sql):
            if not mids:
                return {}
            _t = sql.split('FROM')[1].strip().split()[0]
            if _t not in _tbls:          # 雲端舊庫缺表→當冇紀錄（如有原則）
                return {}
            ph = ','.join('?' * len(mids))
            tail = (' AND match_id IN (' if ' WHERE ' in sql.upper()
                    else ' WHERE match_id IN (') + ph + ')'
            return {r[0]: r[1:] for r in conn.execute(
                sql + tail, mids).fetchall()}

        # 各來源方向／已結算 result（有 result 用 result，冇先對落注時盤計，
        # legacy 冇 stored 先 closing）
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
        m_main = _map1("SELECT match_id, side, label, pct, den FROM prediction_log"
                       " WHERE label NOT LIKE '%大小%'")
        m_ou = _map1("SELECT match_id, side, label FROM prediction_log"
                     " WHERE label LIKE '%大小%'")
        m_feat = _map1('SELECT match_id, direction, result, hc, giver'
                       ' FROM featured')
        m_v1 = _map1('SELECT match_id, direction, result, hc, giver'
                     ' FROM v1_featured')
        m_v3 = _map1('SELECT match_id, direction, result, hc, giver'
                     ' FROM v3_featured')
        m_fwg = _map1('SELECT match_id, direction, result, grid, hc, giver'
                      ' FROM fw_grid_featured')
        m_user = _map1('SELECT match_id, choice, pick_handicap, pick_giver'
                       ' FROM user_picks')
        ou_lines = {}
        if m_ou:
            ph = ','.join('?' * len(mids))
            ou_lines = {r[0]: r[1] for r in conn.execute(
                "SELECT match_id, total_line FROM odds_ou"
                " WHERE company_id=12 AND label='closing'"
                f' AND match_id IN ({ph})', mids).fetchall()}

        fwg = {}
        for mid, d, res, g, shc, sgv in ((k, *v) for k, v in m_fwg.items()):
            fwg.setdefault(g or '?', {})[mid] = (d, res, shc, sgv)

        agg = {k: {'n': 0, 'hit': 0, 'miss': 0, 'push': 0, 'rate': None}
               for k, _ in _PREDSTATS_SOURCES}

        def _bump(k, v):
            if v is None:
                return
            a = agg[k]
            a['n'] += 1
            if v == '贏':
                a['hit'] += 1
            elif v == '輸':
                a['miss'] += 1
            else:
                a['push'] += 1

        matches = []
        for (mid, ko, lgname, home, away, hs, aws, hh, ha, hc, gv) in rows:
            cell = _ps_htft_cell(hh, ha, hs, aws)
            src = {k: None for k, _ in _PREDSTATS_SOURCES}
            # 上下盤（prediction_log 讓球列）
            if mid in m_main:
                side, label, p_pct, p_den = m_main[mid]
                v = _ps_side_verdict(side, hc, gv, hs, aws)
                disp = {'上盤': '上', '下盤': '下',
                        '主勝盤': '主', '客勝盤': '客'}.get(side, side)
                src['main'] = {'side': disp, 'pct': p_pct, 'den': p_den,
                               'label': label, 'verdict': v}
                _bump('main', v)
            # 好波大小（prediction_log label 含「大小」；對 company12 closing
            # 大小線結算——目前庫中未有此類列，預留顯示 —）
            if mid in m_ou:
                o_side = m_ou[mid][0] or ''
                line = ou_lines.get(mid)
                v = None
                pick_over = '大' in o_side
                pick_under = '小' in o_side
                if line is not None and (pick_over or pick_under):
                    diff = (hs + aws) - line
                    if diff == 0:
                        v = '走'
                    elif pick_over:
                        v = '贏' if diff > 0 else '輸'
                    else:
                        v = '贏' if diff < 0 else '輸'
                src['ou'] = {'side': ('大' if pick_over else ('小' if pick_under
                                                              else o_side)),
                             'verdict': v}
                _bump('ou', v)
            # 精選系列（featured/v1/v3 + fw grid 7/8/12）
            for k, tbl in (('v2', m_feat), ('v1', m_v1), ('v3', m_v3)):
                if mid not in tbl:
                    continue
                d, res, shc, sgv = tbl[mid]
                v = {'W': '贏', 'L': '輸', 'P': '走'}.get(res)
                if v is None:
                    pkh, pkg = _stored_first(shc, sgv, hc, gv)
                    v = _ps_settle_dir(d, pkh, pkg, hs, aws)
                src[k] = {'side': ('上' if d == 'up' else ('下' if d == 'down'
                                                           else d)),
                          'verdict': v}
                _bump(k, v)
            for g, k in (('7', 'f7'), ('8', 'f8'), ('12', 'f12')):
                if mid not in fwg.get(g, {}):
                    continue
                d, res, shc, sgv = fwg[g][mid]
                v = {'W': '贏', 'L': '輸', 'P': '走'}.get(res)
                if v is None:
                    pkh, pkg = _stored_first(shc, sgv, hc, gv)
                    v = _ps_settle_dir(d, pkh, pkg, hs, aws)
                src[k] = {'side': ('上' if d == 'up' else ('下' if d == 'down'
                                                           else d)),
                          'verdict': v}
                _bump(k, v)
            # 我的選擇（對 pick 線結算）
            if mid in m_user:
                choice, p_hc, p_gv = m_user[mid]
                v = _ps_settle_dir(choice, p_hc, p_gv, hs, aws)
                src['user'] = {'side': ('上' if choice == 'up'
                                        else ('下' if choice == 'down'
                                              else choice)),
                               'verdict': v}
                _bump('user', v)
            matches.append({'id': mid, 'kickoff': ko, 'league': lgname,
                            'home': home, 'away': away, 'hs': hs, 'aws': aws,
                            'hh': hh, 'ha': ha, 'cell': cell,
                            'xh_xa': cell in ('HD', 'HA', 'AD', 'AA'),
                            'sources': src})
        for a in agg.values():
            dec = a['hit'] + a['miss']
            a['rate'] = round(a['hit'] / dec, 3) if dec else None
        # 聯賽清單：同一時段過濾（唔跟 lg 前綴，方便轉掣）
        lg_rows = conn.execute(
            'SELECT DISTINCT c.req_name FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'WHERE m.home_score IS NOT NULL' +
            (" AND m.kickoff >= datetime('now','+8 hours', ?)" if days > 0
             else '') +
            ' ORDER BY c.req_name',
            [f'-{days} days'] if days > 0 else []).fetchall()
        data = {'matches': matches, 'agg': agg,
                'leagues': [r[0] for r in lg_rows if r[0]],
                'days': days,
                'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
    finally:
        conn.close()
    _PREDSTATS_CACHE.update(key=key, ts=now, data=data)
    return data


# ============ 聯賽規則提示（>78% 規則出現即提示） ============
_LG_ALERT_SEEN = os.path.join(BASE_DIR, '_lg_alerts_seen.json')


def _lg_alert_seen_load():
    try:
        if os.path.exists(_LG_ALERT_SEEN):
            with open(_LG_ALERT_SEEN, encoding='utf-8') as f:
                return set(json.load(f) or [])
    except Exception:
        pass
    return set()


def _lg_alert_seen_save(ids):
    try:
        with open(_LG_ALERT_SEEN, 'w', encoding='utf-8') as f:
            json.dump(sorted(int(x) for x in ids), f, ensure_ascii=False)
    except Exception:
        pass


def get_lg_alerts(mark=False):
    """掃未開賽賽事，搵符合 _lg_preds.json >78% 規則嘅場次（出現即提示）。
    回傳 {'new': [...], 'all': [...]}；mark=True 將而家嘅 new 標記做已提示。
    每場一條：{id, kickoff, home, away, league, rule, direction, up_r, n}。"""
    import v3_engine
    preds = (_lgpred_load() or {}).get('leagues') or {}
    if not preds:
        return {'new': [], 'all': []}
    conn = db()
    now = v3_engine.hk_now_str()
    try:
        rows = conn.execute(
            "SELECT m.id, m.kickoff, ht.name_tc, at.name_tc, c.req_name, "
            "oi.handicap, oi.giver, oi.home_odds, oi.away_odds, "
            "oc.handicap, oc.giver, oc.home_odds, oc.away_odds "
            "FROM matches m "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "JOIN teams ht ON ht.titan_id=m.home_id "
            "JOIN teams at ON at.titan_id=m.away_id "
            "JOIN odds_asian oi ON oi.match_id=m.id AND oi.label='initial' "
            "AND oi.company_id=12 "
            "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
            "AND oc.company_id=12 "
            "WHERE m.home_score IS NULL AND m.kickoff >= ? "
            "ORDER BY m.kickoff",
            (now,)).fetchall()
    finally:
        conn.close()

    def sgn(h, g):
        return h * (1.0 if g == 'home' else -1.0 if g == 'away' else 0.0)

    alerts = []
    seen = _lg_alert_seen_load()
    for (mid, ko, h, a, lg, ih, ig, iho, iao, ch, cg, cho, cao) in rows:
        lp = preds.get(lg)
        if not lp:
            continue
        for rule in lp.get('rules') or []:
            kind = rule.get('kind')
            p = rule.get('params') or {}
            if ih is None or ch is None:
                continue
            # 平手盤 giver 喺 DB 係 NULL——同 _rule_applies 一樣當 'none'，
            # 否則 g='none' 嘅規則 lgpred 標到、🔔 又永遠彈唔出（唔一致）
            cgn = cg or 'none'
            if cgn != (p.get('g') or 'none') or abs(ch - (p.get('h') or 0)) > 1e-9:
                continue
            hit = False
            if kind == 'move' and ig is not None:
                mv = sgn(ch, cgn) - sgn(ih, ig)
                d = p.get('delta')
                hit = ((mv > 0.001) if d == '讓深'
                       else (mv < -0.001) if d == '讓淺'
                       else (abs(mv) <= 0.001))
            elif kind == 'wmove':
                if cg == 'home':
                    iw, cw = iho, cho
                elif cg == 'away':
                    iw, cw = iao, cao
                else:
                    iw = min(x for x in (iho, iao) if x is not None) \
                        if iho is not None and iao is not None else None
                    cw = min(x for x in (cho, cao) if x is not None) \
                        if cho is not None and cao is not None else None
                if iw is not None and cw is not None:
                    wd = cw - iw
                    hit = (p.get('lo', 0) - 1e-9) <= wd < (p.get('hi', 0))
            if hit:
                alerts.append({
                    'id': mid, 'kickoff': ko, 'home': h, 'away': a,
                    'league': lg, 'rule': rule.get('desc', ''),
                    'direction': rule.get('direction'),
                    'giver': cg,
                    'up_r': rule.get('up_r'), 'n': rule.get('n')})
                break   # 每場報一次就夠
    new = [x for x in alerts if x['id'] not in seen]
    if mark and new:
        seen.update(x['id'] for x in new)
        _lg_alert_seen_save(seen)
    return {'new': new, 'all': alerts}


# ============ 命中率回查（各規則喺全庫／同聯賽嘅實際命中率） ============
_fwgrid_hr_cache = {'ts': 0, 'data': None}
FWGRID_HR_CACHE_TTL = 600


def _fwgrid_hr_load():
    now = time.time()
    if _fwgrid_hr_cache['data'] is not None and \
            now - _fwgrid_hr_cache['ts'] < FWGRID_HR_CACHE_TTL:
        return _fwgrid_hr_cache['data']
    p = os.path.join(BASE_DIR, '_fwgrid_hr.json')
    data = {}
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
    _fwgrid_hr_cache.update(ts=now, data=data)
    return data


def get_fwx_hr(mid):
    """精選W（六條件）歷史入選場嘅實際命中率：全庫＋分聯賽／杯賽，
    另外新增「同今場尾盤盤口（讓球數＋讓球方）相同」嘅全庫／同聯賽口徑
    （2026-10-02：舊版全庫數字對每場都一樣，用戶要求修正）。
    走盤計入場數、命中計 0（同 _fwgrid_hr.json 口徑一致）。
    回傳 (out, line_label)。"""
    conn = db()
    try:
        cur = conn.execute(
            "SELECT oc.handicap, oc.giver FROM odds_asian oc "
            "WHERE oc.match_id=? AND oc.label='closing' AND oc.company_id=12",
            (mid,)).fetchone()
        cur_hc, cur_gv = (cur[0], cur[1]) if cur else (None, None)
        rows = conn.execute(
            'SELECT l.direction, m.home_score, m.away_score, '
            'l.handicap, l.giver, c.req_name '
            'FROM v3_featured_log l JOIN matches m ON m.id=l.match_id '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'WHERE m.home_score IS NOT NULL').fetchall()
    finally:
        conn.close()

    def bucket():
        return {'up': [0, 0], 'down': [0, 0]}

    def same_line(hc, gv):
        if cur_hc is None or hc is None:
            return False
        return abs(hc - cur_hc) < 0.001 and (gv or 'none') == (cur_gv or 'none')

    out = {'all': bucket(), 'line': bucket(),
           'leagues': {}, 'line_leagues': {}}
    for d, hs, aws, hc, gv, lg in rows:
        r = pick_result(hc, gv, hs, aws)
        if r is None:
            continue
        hit = 1 if ((r == 'A') == (d == 'up')) else 0
        scopes = [out['all']]
        if same_line(hc, gv):
            scopes.append(out['line'])
        scopes.append(out['leagues'].setdefault(lg or '', bucket()))
        if same_line(hc, gv):
            scopes.append(out['line_leagues'].setdefault(lg or '', bucket()))
        for sc in scopes:
            sc[d][0] += 1
            sc[d][1] += hit
    label = screen_engine.fmt_line(cur_hc, cur_gv) \
        if cur_hc is not None else None
    return out, label


def api_hitrate(kind, mid):
    """kind = fw7／fw8／fw12（格組合歷史命中率）或 fwx（精選W 六條件）；
    mid 用嚟搵場次所屬聯賽，回傳全庫＋同聯賽／杯賽口徑。
    每個方向 [場數, 命中]；命中率＝命中÷場數（走盤計場數唔計命中）。"""
    kind = (kind or '').lower()
    conn = db()
    try:
        row = conn.execute(
            'SELECT c.req_name FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id WHERE m.id=?',
            (mid,)).fetchone()
        lg = row[0] if row else None
    finally:
        conn.close()
    if kind in ('fw7', 'fw8', 'fw12'):
        d = (_fwgrid_hr_load() or {}).get(kind[2:], {})
        line_label = None
    elif kind == 'fwx':
        d, line_label = get_fwx_hr(mid)
    else:
        return {'error': '未知類型：' + kind}

    def pick(scope):
        scope = scope or {}
        return {'up': list(scope.get('up', [0, 0])),
                'down': list(scope.get('down', [0, 0]))}

    return {'ok': True, 'kind': kind, 'league': lg, 'line_label': line_label,
            'all': pick(d.get('all')),
            'line': pick(d.get('line')),
            'league_stats': pick((d.get('leagues') or {}).get(lg)),
            'line_league_stats': pick((d.get('line_leagues') or {}).get(lg))}


# ============ 好波（2026-10-05）：大小球＋半全場 ============
_hb_ctx = {'ctx': None, 'ts': 0}
HB_CTX_TTL = 3600
_hb_ctx_lock = threading.Lock()
# 好波預測結果快取（2026-10-14 502 風暴修復）：一頁 81 場同時打 81 個重型
# predict，並行 pandas 喺 512MB 免費實例直接 OOM／CPU 飽和，頁面成版 502
# 「用唔到」。改為伺服器端背景預先計算＋快取：用戶請求=讀快取（毫秒級），
# 計算全部由背景線程串行吸收；賠率變（sig）或資料池重建（gen +1）先失效。
_hb_pre = {}                 # mid -> {'ts', 'sig', 'gen', 'data'}
_hb_pre_lock = threading.Lock()
_hb_pre_gen = [0]            # 資料池世代：重建 +1，全快取作廢
# 10-16：上限改環境可調——512MB 雲端經常 OOM（render.yaml 設 HB_PRE_MAX=250），
# 本機 8GB 維持 500。每個 entry 連 items 陣列約 50-100KB，250 vs 500 差 ~20MB；
# 超額淘汰最舊（6h 時限本來都會清），對精確零影響、對速度影響微乎其微
# （precompute 每 4 分鐘補返最近場次）。
_HB_PRE_MAX = int(os.environ.get('HB_PRE_MAX', '500'))


def _hb_context():
    import haobao_engine
    now = time.time()
    if _hb_ctx['ctx'] is not None and now - _hb_ctx['ts'] < HB_CTX_TTL:
        return _hb_ctx['ctx']
    with _hb_ctx_lock:       # 兩個請求同時撞冷池：只准一個 build，其餘等
        now = time.time()
        if _hb_ctx['ctx'] is not None and now - _hb_ctx['ts'] < HB_CTX_TTL:
            return _hb_ctx['ctx']
        conn = db()
        try:
            ctx = haobao_engine.HB(conn)
        finally:
            conn.close()
        _hb_ctx['ctx'] = ctx
        _hb_ctx['ts'] = time.time()
        return ctx


def api_hb_list(hours=72):
    """好波主頁：即將開賽場次＋12BET 尾盤大小線／亞盤＋初盤大小線。
    2026-10-22：頁面入嚟自動掃——發現未來場次冇 closing 大小線（爬蟲
    fleet 停咗／新場次未預抓）就即刻喺 APP 進程內補抓（task_odds_page
    'ou'，12BET→Crown 後備），下次載入就有線有預測（用戶：場場未開盤，
    要自動掃同更新預測）。"""
    import v3_engine
    conn = db()
    cap = "AND m.kickoff <= datetime('now','+8 hours', ?) " if hours else ""
    args = (f'+{hours} hours',) if hours else ()
    rows = conn.execute(
        "SELECT m.id, c.req_name, m.kickoff, ht.name_tc, at.name_tc, "
        "m.round_label, oc.total_line, oc.over_odds, oc.under_odds, "
        "oi.total_line, oi.over_odds, oi.under_odds, "
        "oa.handicap, oa.giver, oa.home_odds, oa.away_odds "
        "FROM matches m JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN teams ht ON ht.titan_id=m.home_id "
        "JOIN teams at ON at.titan_id=m.away_id "
        "LEFT JOIN odds_ou oc ON oc.match_id=m.id AND oc.company_id=12 "
        "AND oc.label='closing' "
        "LEFT JOIN odds_ou oi ON oi.match_id=m.id AND oi.company_id=12 "
        "AND oi.label='initial' "
        "LEFT JOIN odds_asian oa ON oa.match_id=m.id AND oa.company_id=12 "
        "AND oa.label='closing' "
        "WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','+8 hours') "
        + cap + "ORDER BY m.kickoff", args).fetchall()
    conn.close()
    now = v3_engine.hk_now_str()
    out = []
    missing = []
    for (mid, lg, ko, h, a, rd, ln, oo, uo, iln, ioo, iuo,
         ahc, agv, aho, aao) in rows:
        if ln is None:
            missing.append((mid, ko))
        out.append({'id': mid, 'league': lg, 'kickoff': ko,
                    'home': h, 'away': a, 'round': rd,
                    'ou_line': ln, 'ou_over': oo, 'ou_under': uo,
                    'init_line': iln, 'init_over': ioo,
                    'ah_line': screen_engine.fmt_line(ahc, agv) if ahc is not None else None,
                    'ah_odds': f'主{aho}/客{aao}' if aho is not None else None,
                    'state': 'live' if ko <= now else 'scheduled'})
    if missing:
        _hb_ou_sweep(missing)
    return {'matches': out, 'now': now}


def _hb_requalify():
    """2026-10-23 好波重檢（用戶規格：已入選變唔合資格要 100% 勾走＋
    推送）：對 hb_pick_log 未結算嘅規則記錄逐場重評——規則唔再命中／
    降做參考訊號（單條<88% 冇共識）／方向改變 → DELETE 該列＋推 ❌。
    同其他 12 表工具嘅 _eval_all 語義一致：只動未結算列，歷史結算保留。
    喺 api_hb_picks 每次載入前行一次（行數少，毫秒級）。"""
    conn = db()
    removed = 0
    try:
        rows = conn.execute(
            'SELECT k.match_id, k.league, k.side, k.rule FROM hb_pick_log k '
            'JOIN matches m ON m.id=k.match_id '
            'WHERE k.result IS NULL AND k.rule IS NOT NULL '
            'AND m.home_score IS NULL').fetchall()
        if not rows:
            return 0
        try:
            import hb_ou_rules
        except Exception:
            return 0
        ctx = _hb_context()
        for mid, lg, side, rule_label in rows:
            try:
                r = conn.execute(
                    'SELECT oc.total_line, oc.over_odds, oc.under_odds, '
                    'oi.total_line, oi.over_odds, oi.under_odds, '
                    'o4.total_line, oa.handicap, oa.giver, '
                    'ph.home_total_rank, ph.away_total_rank, '
                    'm.home_id, m.away_id FROM matches m '
                    'LEFT JOIN odds_ou oc ON oc.match_id=m.id '
                    "AND oc.company_id=12 AND oc.label='closing' "
                    'LEFT JOIN odds_ou oi ON oi.match_id=m.id '
                    "AND oi.company_id=12 AND oi.label='initial' "
                    'LEFT JOIN odds_ou o4 ON o4.match_id=m.id '
                    "AND o4.company_id=12 AND o4.label='pre_4h' "
                    'LEFT JOIN odds_asian oa ON oa.match_id=m.id '
                    "AND oa.company_id=12 AND oa.label='closing' "
                    'LEFT JOIN match_prestandings ph ON ph.match_id=m.id '
                    'WHERE m.id=?', (mid,)).fetchone()
                if not r or r[0] is None:
                    still = False
                    best_side = None
                else:
                    (ln, oo, uo, iln, ioo, iuo, l4, ahc, agv, phr, par,
                     hid, aid) = r
                    H = ctx.teams.get(hid) or {}
                    A = ctx.teams.get(aid) or {}
                    sline = (ahc if agv == 'home' else
                             (-ahc if agv == 'away' else 0.0)) \
                        if ahc is not None else None
                    F = hb_ou_rules.feats(
                        line=ln, init_line=iln, over=oo, under=uo,
                        init_over=ioo, pre4h_line=l4,
                        lg_avg_total=(ctx.lg.get(lg) or {}).get('avg_total'),
                        h_avg_total=(((H.get('gf_h') or 0)
                                      + (H.get('ga_h') or 0))
                                     if H.get('n_h') else None),
                        a_avg_total=(((A.get('gf_a') or 0)
                                      + (A.get('ga_a') or 0))
                                     if A.get('n_a') else None),
                        h_ga=H.get('ga_h'), a_ga=A.get('ga_a'),
                        sline=sline, rank_h=phr, rank_a=par)
                    rp = hb_ou_rules.best_pick(
                        hb_ou_rules.eval_rules(lg, F))
                    still = bool(rp and rp[2])   # is_pick 先算合資格
                    best_side = rp[0] if rp else None
                if still and best_side == side:
                    continue
                # 唔合資格／降級／轉向 → 剔走（2026-10-24 記錄落剔走頁）
                try:
                    _k = conn.execute(
                        'SELECT side, rate, rule FROM hb_pick_log '
                        'WHERE match_id=? AND result IS NULL', (mid,)).fetchone()
                    if _k:
                        _ou = conn.execute(
                            "SELECT total_line FROM odds_ou WHERE match_id=? "
                            "AND label='closing' AND company_id=12",
                            (mid,)).fetchone()
                        _record_removed(conn, '好波', mid, side=_k[0],
                                        line=_ou[0] if _ou else None,
                                        detail=_k[2], rate=_k[1])
                except Exception:
                    pass
                conn.execute(
                    'DELETE FROM hb_pick_log WHERE match_id=? AND result '
                    'IS NULL', (mid,))
                removed += 1
                # ❌ 推送（12 小時去重）
                info = conn.execute(
                    'SELECT ht.name_tc, at.name_tc, m.kickoff FROM matches m '
                    'JOIN teams ht ON ht.titan_id=m.home_id '
                    'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
                    (mid,)).fetchone()
                dup = conn.execute(
                    "SELECT 1 FROM alerts WHERE tool='好波' AND kind="
                    "'removed' AND match_id=? AND ts >= datetime('now',"
                    "'localtime','-12 hours') LIMIT 1", (mid,)).fetchone()
                if info and not dup and _notify_window_ok(conn, mid):
                    conn.execute('''CREATE TABLE IF NOT EXISTS alerts(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool TEXT, kind TEXT, match_id INTEGER, league TEXT,
                        home TEXT, away TEXT, kickoff TEXT, msg TEXT,
                        ts TEXT, read INTEGER DEFAULT 0)''')
                    reason = '轉向' if (still and best_side != side) else \
                        '規則唔再命中'
                    conn.execute(
                        'INSERT INTO alerts(tool,kind,match_id,league,'
                        'home,away,kickoff,msg,ts) VALUES(?,?,?,?,?,?,?,?,?)',
                        ('好波', 'removed', mid, lg, info[0], info[1],
                         info[2],
                         '❌ 已剔走（唔合資格）好波：%s vs %s（%s｜%s｜%s）'
                         % (info[0], info[1], lg, (info[2] or '')[5:16],
                            reason),
                         time.strftime('%Y-%m-%d %H:%M:%S')))
            except Exception:
                traceback.print_exc(limit=2)
        if removed:
            conn.commit()
            _scanlog('⚽ 好波重檢：剔走 %d 場唔合資格' % removed)
            print('[hb-requalify] 剔走 %d 場唔合資格嘅好波入選' % removed,
                  flush=True)
    finally:
        conn.close()
    return removed


def _hb_refresh_mids(mids, limit=150):
    """2026-10-25 重爬重算補好波：crawl16/crawl_mk 補完缺口／批量更新後，對有
    OU 尾盤嘅場次重算好波（api_hb_predict 自帶賠率 signature 快取——冇變嘅場
    微秒過，變咗嘅先真計），完事 _hb_requalify 清走唔合資格。
    背景：重算（requalify_after_backfill）淨做八個精選系統；而 crawl16/mk
    舊版補完都冇 notify——好波兩頭都漏（用戶一問即中）。雲端唔做
    （DISABLE_UPDATE）：手機開好波頁時按 signature 即場重算，慳 1C。"""
    if DISABLE_UPDATE or not mids:
        return 0
    n = 0
    for mid in list(dict.fromkeys(mids))[:limit]:
        try:
            conn = db()
            try:
                has = conn.execute(
                    "SELECT 1 FROM odds_ou WHERE match_id=? AND label='closing' "
                    'LIMIT 1', (mid,)).fetchone()
            finally:
                conn.close()
            if not has:
                continue
            api_hb_predict(mid)
            n += 1
        except Exception:
            pass
    try:
        _hb_requalify()
    except Exception:
        pass
    if n:
        print('[hb-refresh] 重算好波 %d 場' % n, flush=True)
        _scanlog('⚽ 重爬重算：好波重檢 %d 場' % n)
    return n


def api_hb_picks():
    """2026-10-22 好波優化：『高機會精選』專區——淨係顯示規則命中（≥82.5%
    兩期驗證＋88% 獨立或共識門檻）嘅未開賽場次（hb_pick_log 待定列）。
    用戶規格：淨係篩選高命中率高機會，唔場場預測。"""
    _hb_requalify()   # 2026-10-23：重算剔走唔合資格＋❌推送
    conn = db()
    try:
        rows = conn.execute(
            'SELECT k.match_id, k.league, k.side, k.rate, k.line, k.rule, '
            'k.added_at, m.kickoff, ht.name_tc, at.name_tc '
            'FROM hb_pick_log k JOIN matches m ON m.id=k.match_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'WHERE k.result IS NULL AND m.home_score IS NULL '
            'AND k.rule IS NOT NULL '
            'AND m.kickoff >= datetime(\'now\',\'-3 hours\',\'+8 hours\') '
            'ORDER BY m.kickoff').fetchall()
        return {'picks': [
            {'id': r[0], 'league': r[1], 'side': r[2], 'rate': r[3],
             'line': r[4], 'rule': r[5], 'added_at': r[6], 'kickoff': r[7],
             'home': r[8], 'away': r[9]}
            for r in rows]}
    finally:
        conn.close()


_HB_OU_SWEEP = {'running': False, 'last': None, 'fixed': 0}


def _hb_ou_sweep(candidates, limit=16):
    """好波 OU 自動補抓（APP 進程內，背景）：對冇 closing 大小線嘅場次
    逐場 task_odds_page('ou')。限 limit 場／輪＋行緊就唔重開。爬完前端
    自動 reload 會帶到新線，唔使人手 refresh。"""
    if _HB_OU_SWEEP['running']:
        return
    todo = candidates[:limit]

    def _bg():
        _HB_OU_SWEEP['running'] = True
        fixed = 0
        try:
            import datetime as _dt
            from crawl_extra import task_odds_page
            crawler, fetcher = get_crawler()
            conn = db()
            try:
                for mid, ko in todo:
                    try:
                        ko_dt = _dt.datetime.strptime(ko, '%Y-%m-%d %H:%M')
                        if task_odds_page(conn, fetcher, mid, ko_dt,
                                          'ou') is True:
                            fixed += 1
                    except Exception:
                        pass
                if fixed:
                    conn.commit()
            finally:
                conn.close()
            _HB_OU_SWEEP['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
            _HB_OU_SWEEP['fixed'] = fixed
            if fixed:
                print('[hb-sweep] 補到 %d 場 OU 大小線' % fixed, flush=True)
        except Exception:
            traceback.print_exc(limit=3)
        finally:
            _HB_OU_SWEEP['running'] = False
    threading.Thread(target=_bg, daemon=True).start()


def api_hb_predict(mid):
    """好波單場（快取版）：命中快取毫秒回；miss 先計，計完入快取。
    快取有效期：賠率 signature 不變＋資料池世代不變。錯誤結果不入快取。"""
    conn = db()
    try:
        row = conn.execute(
            "SELECT total_line, over_odds, under_odds FROM odds_ou "
            "WHERE match_id=? AND company_id=12 AND label='closing'",
            (mid,)).fetchone()
        # 2026-10-25 修正（signature 缺口）：hb 推論仲用 AH 尾盤/4h線/排名/
        # 8X・新精選方向——呢啲變咗舊 sig 都當冇變，會出過期 pick。全部入 sig。
        _ah = conn.execute(
            "SELECT handicap, giver FROM odds_asian WHERE match_id=? "
            "AND company_id=12 AND label='closing'", (mid,)).fetchone()
        _o4 = conn.execute(
            "SELECT total_line FROM odds_ou WHERE match_id=? AND company_id=12 "
            "AND label='pre_4h'", (mid,)).fetchone()
        _x8 = conn.execute(
            "SELECT direction FROM fw8x_log WHERE match_id=?", (mid,)).fetchone()
        _jx = conn.execute(
            "SELECT dir FROM newjx_log WHERE match_id=?", (mid,)).fetchone()
        _ps = conn.execute(
            "SELECT home_total_rank, away_total_rank FROM match_prestandings "
            "WHERE match_id=?", (mid,)).fetchone()
    finally:
        conn.close()
    sig = ((tuple(row) if row else None),
           (tuple(_ah) if _ah else None),
           (_o4[0] if _o4 else None),
           (_x8[0] if _x8 else None),
           (_jx[0] if _jx else None),
           (tuple(_ps) if _ps else None))
    gen = _hb_pre_gen[0]
    with _hb_pre_lock:
        e = _hb_pre.get(mid)
        if e is not None and e['sig'] == sig and e['gen'] == gen:
            return e['data']
    data = _hb_predict_compute(mid)
    if data and not data.get('error'):
        with _hb_pre_lock:
            _hb_pre[mid] = {'ts': time.time(), 'sig': sig, 'gen': gen,
                            'data': data}
            if len(_hb_pre) > _HB_PRE_MAX:
                cutoff = time.time() - 6 * 3600
                for k in [k for k, v in _hb_pre.items()
                          if v['ts'] < cutoff]:
                    _hb_pre.pop(k, None)
    return data


def _hb_predict_compute(mid):
    """好波單場真正計算（無快取）：大小球 49 項分析＋共識＋精選＋半全場＋關係。"""
    conn = db()
    try:
        r = conn.execute(
            "SELECT m.home_id, m.away_id, m.home_score, m.away_score, "
            "m.half_home, m.half_away, c.req_name, m.round_label, "
            "oc.total_line, oc.over_odds, oc.under_odds, "
            "oi.total_line, oi.over_odds, oi.under_odds, "
            "o4.total_line, o4.over_odds, o4.under_odds, "
            "oa.handicap, oa.giver, ph.home_total_rank, ph.away_total_rank "
            "FROM matches m JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "LEFT JOIN odds_ou oc ON oc.match_id=m.id AND oc.company_id=12 "
            "AND oc.label='closing' "
            "LEFT JOIN odds_ou oi ON oi.match_id=m.id AND oi.company_id=12 "
            "AND oi.label='initial' "
            "LEFT JOIN odds_ou o4 ON o4.match_id=m.id AND o4.company_id=12 "
            "AND o4.label='pre_4h' "
            "LEFT JOIN odds_asian oa ON oa.match_id=m.id AND oa.company_id=12 "
            "AND oa.label='closing' LEFT JOIN match_prestandings ph ON ph.match_id=m.id WHERE m.id=?", (mid,)).fetchone()
    finally:
        conn.close()
    if not r:
        return {'error': '找不到賽事'}
    (hid, aid, hs, aws, hhs, haws, lg, rd, ln, oo, uo, iln, ioo, iuo,
     l4, o4o, o4u, ahc, agv, phr, par) = r
    ctx = _hb_context()
    # 2026-10-20 用戶：8X（第50項）＋新精選（第51項）上下盤預測作為好波大小因素
    x8dir = None
    jxdir = None
    try:
        c3 = db()
        try:
            _r = c3.execute('SELECT direction FROM fw8x_log WHERE match_id=?',
                            (mid,)).fetchone()
            x8dir = _r[0] if _r else None
            _r2 = c3.execute('SELECT dir FROM newjx_log WHERE match_id=?',
                             (mid,)).fetchone()
            jxdir = _r2[0] if _r2 else None
        finally:
            c3.close()
    except Exception:
        pass
    res = ctx.predict(hid, aid, lg, rd, ou_line=ln, ou_over=oo,
                      ou_under=uo, oi_line=iln, oi_over=ioo, oi_under=iuo,
                      ah_hc=ahc, ah_gv=agv, o4_line=l4, o4_over=o4o,
                      o4_under=o4u, x8dir=x8dir, jxdir=jxdir,
                      rank_h=phr, rank_a=par)
    res['x8dir'] = x8dir
    res['jxdir'] = jxdir
    res['match'] = {'id': mid, 'league': lg, 'home_id': hid, 'away_id': aid,
                    'score': f'{hs}-{aws}' if hs is not None else None,
                    'half': f'{hhs}-{haws}' if hhs is not None else None,
                    'round': rd,
                    'ah_line': screen_engine.fmt_line(ahc, agv)
                    if ahc is not None else None}
    # 10-22 重建：舊「同聯賽規律」路徑（hb_ou_rules.check_match／lg_rule）
    # 已移除——舊制實戰 50.9%＝擲毫，且同新挖掘規則（hb_ou_rules.eval_rules）
    # 撞模組名。舊模組同規則檔已備份做 _old_hb_ou_rules.py /
    # _old_haobao_ou_rules.json（16 聯賽 48 條雙池驗證規則，留待對照）。
    # 現時 ONLY 新挖掘規則（兩期≥82.5%，運行時全庫回測 88.4%）出預測。
    # 記低好波方向選擇（供「同聯賽命中率」統計）——規則命中先記
    try:
        _hb_log_pick(mid, lg, res.get('ou') or {})
    except Exception:
        pass
    return res


def _hb_rule_gate_update(conn):
    """2026-10-22 好波優化：live 規則健康度——按 rule 聚合 hb_pick_log
    已結算結果；n≥6 且命中率 <70% 嘅規則寫入停用名單（hb_rule_gate.json），
    eval_rules 會自動過濾佢——保證出嘅預測長期維持高命中率（用戶規格：
    淨係高命中率高機會）。檔案寫 app 目錄，雲端經 git 部署共享。"""
    rows = conn.execute(
        "SELECT rule, COUNT(*), SUM(result='W') FROM hb_pick_log "
        "WHERE rule IS NOT NULL AND result IN ('W','L') GROUP BY rule"
    ).fetchall()
    paused = {}
    stats = {}
    for rule, n, w in rows:
        n = int(n)
        w = int(w or 0)
        hit = w / n if n else 0
        stats[rule] = {'n': n, 'w': w, 'hit': round(hit, 3)}
        if n >= 6 and hit < 0.70:
            paused[rule] = {'n': n, 'hit': round(hit, 3)}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'hb_rule_gate.json')
    tmp = {'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
           'paused': paused, 'stats': stats}
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(tmp, f, ensure_ascii=False)
    except OSError:
        pass


def _hb_log_pick(mid, lg, ou):
    """好波大小選擇落庫（hb_pick_log）——2026-10-22 重建規格：ONLY
    挖掘規則命中先記錄（兩期≥82.5%）；冇命中＝觀望，唔記錄。舊 fallback
    （同聯賽規律／共識 lean／最強單項≥52%）全部已廢——舊制實戰 50.9%
    ＝擲毫，繼續記只會污染統計。完場後由 api_lg_hit_rates 懶結算；
    同場重算只更新方向，保留首次加入時間。"""
    pick = ou.get('pick')
    if not pick:
        return
    side = str(pick.get('side'))
    rate = pick.get('best')
    method = 'rule'
    # 2026-10-22：好波規則命中＝新入選推送（用戶規格：所有工具入選推送）
    try:
        conn0 = db()
        try:
            info = conn0.execute(
                'SELECT c.req_name, m.kickoff, ht.name_tc, at.name_tc '
                'FROM matches m JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id '
                'JOIN teams ht ON ht.titan_id=m.home_id '
                'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
                (mid,)).fetchone()
            dup = conn0.execute(
                "SELECT 1 FROM alerts WHERE tool='好波' AND kind='new' "
                'AND match_id=? AND ts >= datetime(\'now\',\'localtime\','
                "'-12 hours') LIMIT 1", (mid,)).fetchone()
            if info and not dup and _notify_window_ok(conn0, mid):
                conn0.execute('''CREATE TABLE IF NOT EXISTS alerts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool TEXT, kind TEXT, match_id INTEGER, league TEXT,
                    home TEXT, away TEXT, kickoff TEXT, msg TEXT, ts TEXT,
                    read INTEGER DEFAULT 0)''')
                conn0.execute(
                    'INSERT INTO alerts(tool,kind,match_id,league,home,'
                    'away,kickoff,msg,ts) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('好波', 'new', mid, info[0], info[2], info[3], info[1],
                     '🆕 新入選 好波：%s vs %s（%s｜%s｜%.1f%%）'
                     % (info[2], info[3], info[0], (info[1] or '')[5:16],
                        rate or 0),
                     time.strftime('%Y-%m-%d %H:%M:%S')))
                conn0.commit()
        finally:
            conn0.close()
    except Exception:
        pass
    conn = db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS hb_pick_log(
            match_id INTEGER PRIMARY KEY, league TEXT, side TEXT, rate REAL,
            line REAL, added_at TEXT, result TEXT, settled_at TEXT)''')
        try:
            conn.execute('ALTER TABLE hb_pick_log ADD COLUMN rule TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute('ALTER TABLE hb_pick_log ADD COLUMN method TEXT '
                         "DEFAULT 'engine'")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            'INSERT INTO hb_pick_log(match_id,league,side,rate,line,added_at,'
            'method,rule) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(match_id) '
            'DO UPDATE SET league=excluded.league, side=excluded.side, '
            'rate=excluded.rate, line=excluded.line, method=excluded.method, '
            'rule=excluded.rule',
            (mid, lg, side, rate, ou.get('line'),
             time.strftime('%Y-%m-%d %H:%M:%S'), method,
             pick.get('rule_label')))
        conn.commit()
        # 2026-10-25 用戶規格：好波重新入選 → 剔走頁刪返該場次舊剔走記錄
        # （同一場次喺剔走頁只出現一次）。
        try:
            _migrate_removed(conn)
            conn.execute(
                "DELETE FROM removed_picks WHERE match_id=? AND tool='好波'",
                (mid,))
            conn.commit()
        except Exception:
            pass
        # 2026-10-25 即時推送：好波新入選/轉方向都廣播
        _publish_event({'type': 'reval', 'mid': mid,
                        'acts': ['好波' + str(side)]})
        # 2026-10-22 優化：每次記錄後順手更新 live 規則健康度（停腐化規則）
        try:
            _hb_rule_gate_update(conn)
        except Exception:
            pass
    finally:
        conn.close()


_LG_HIT_CACHE = {'ts': 0.0, 'data': None}
_LG_HIT_TTL = 600


def api_lg_hit_rates():
    """同聯賽/杯賽命中率（2026-10-17 用戶）——六個來源逐聯賽計：
    main＝主頁上下盤（check_rows）、feat_v1/feat_v2＝精選、
    f7/f8/f12/f8s＝精選格組合（fw_grid_featured）、hb＝好波大小（hb_pick_log，
    10-17 起記錄，n<10 顯示—）。回傳 {lg: {src: [命中, 已結算n]}}，600s 快取。"""
    now = time.time()
    if _LG_HIT_CACHE['data'] is not None \
            and now - _LG_HIT_CACHE['ts'] < _LG_HIT_TTL:
        return _LG_HIT_CACHE['data']
    conn = db()
    out = {}
    try:
        # 好波選擇懶結算（走＝P 唔計入命中率分母）
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS hb_pick_log(
                match_id INTEGER PRIMARY KEY, league TEXT, side TEXT, rate REAL,
                line REAL, added_at TEXT, result TEXT, settled_at TEXT)''')
            conn.execute(
                "UPDATE hb_pick_log AS h SET result = CASE "
                "WHEN m.home_score + m.away_score = h.line THEN 'P' "
                "WHEN (m.home_score + m.away_score > h.line) = (h.side = '大') "
                "THEN 'W' ELSE 'L' END, "
                "settled_at = datetime('now','localtime') "
                "FROM matches m "
                "WHERE h.match_id = m.id AND m.home_score IS NOT NULL "
                "AND h.result IS NULL")
            conn.commit()
        except sqlite3.Error:
            pass

        def _add(src, rows):
            for lg, n, w in rows:
                if not lg:
                    continue
                out.setdefault(lg, {})[src] = [int(w or 0), int(n)]

        base = ('FROM matches m JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id ')
        try:
            _add('main', conn.execute(
                'SELECT c.req_name, COUNT(*), SUM((cr.res = \'A\') = '
                "(cr.direction = 'up')) " + base.replace(
                    'FROM matches m', 'FROM check_rows cr JOIN matches m '
                    'ON m.id = cr.match_id') +
                "WHERE cr.res IN ('A','B') GROUP BY c.req_name").fetchall())
        except sqlite3.Error:
            pass
        for tbl, src in (('v1_featured', 'feat_v1'),
                         ('featured', 'feat_v2')):
            try:
                _add(src, conn.execute(
                    'SELECT c.req_name, COUNT(*), SUM(f.result = \'W\') '
                    'FROM %s f ' % tbl + base.replace(
                        'FROM matches m', 'JOIN matches m ON m.id=f.match_id')
                    + "WHERE f.result IN ('W','L') GROUP BY c.req_name"
                    ).fetchall())
            except sqlite3.Error:
                pass
        try:
            rows = conn.execute(
                "SELECT f.grid, c.req_name, COUNT(*), SUM(f.result = 'W') "
                'FROM fw_grid_featured f JOIN matches m ON m.id = f.match_id '
                'JOIN seasons s ON s.id = m.season_id '
                'JOIN competitions c ON c.titan_id = s.titan_id '
                "WHERE f.result IN ('W','L') GROUP BY f.grid, c.req_name"
            ).fetchall()
            for g, lg, n, w in rows:
                if lg:
                    out.setdefault(lg, {})['f' + g] = [int(w or 0), int(n)]
        except sqlite3.Error:
            pass
        try:
            _add('hb', conn.execute(
                "SELECT league, COUNT(*), SUM(result = 'W') FROM hb_pick_log "
                "WHERE result IN ('W','L') GROUP BY league").fetchall())
            _hb_side = [dict(zip(('side', 'n', 'w', 'l', 'p'), r)) for r in
                        conn.execute(
                            "SELECT side, COUNT(*), SUM(result = 'W'), "
                            "SUM(result = 'L'), SUM(result = 'P') "
                            "FROM hb_pick_log GROUP BY side")]
        except sqlite3.Error:
            _hb_side = []
    finally:
        conn.close()
    data = {'lg': out, 'hb_side': _hb_side,
            'ts': time.strftime('%Y-%m-%d %H:%M:%S')}
    _LG_HIT_CACHE['ts'] = now
    _LG_HIT_CACHE['data'] = data
    return data


def _hb_precompute_worker():
    """好波預測背景預熱（2026-10-14 502 風暴修復）：
    ① 資料池到期前 15 分鐘先重建（用戶請求永不捱冷 build）＋預熱 49 項
      引擎（HBItems build 一次 ~20s，lazy 會喺首次 predict 路上爆）；
    ② 每 4 分鐘幫即將開赛场次 top-up 預測快取（每輪上限 25 場，串行，
      唔搶 CPU）——用戶開頁＝全部即時有得睇，唔再 81 個請求打爆 512MB。"""
    time.sleep(40)
    while True:
        try:
            _hb_precompute_cycle()
        except Exception as e:
            print(f'[hb-pre] 循環錯誤：{e}', flush=True)
        time.sleep(240)


def _hb_precompute_cycle():
    now = time.time()
    if (_hb_ctx['ctx'] is None
            or now - _hb_ctx['ts'] > HB_CTX_TTL - 900):
        with _hb_ctx_lock:
            now = time.time()
            stale = (_hb_ctx['ctx'] is None
                     or now - _hb_ctx['ts'] > HB_CTX_TTL - 900)
            if stale:
                import haobao_engine
                conn = db()
                try:
                    ctx = haobao_engine.HB(conn)
                finally:
                    conn.close()
                try:
                    import hb_items
                    ctx.items_engine = hb_items.HBItems(ctx.df)
                except Exception:
                    import traceback
                    traceback.print_exc()
                _hb_ctx['ctx'] = ctx
                _hb_ctx['ts'] = time.time()
                _hb_pre_gen[0] += 1
                print('[hb-pre] 好波池已重建＋49項引擎預熱完成 '
                      f'(gen={_hb_pre_gen[0]})', flush=True)
    try:
        matches = api_hb_list(72).get('matches', [])
    except Exception:
        return
    gen = _hb_pre_gen[0]
    todo = []
    with _hb_pre_lock:
        for m in matches:
            e = _hb_pre.get(m['id'])
            if e is None or e['gen'] != gen:
                todo.append(m['id'])
    for mid in todo[:25]:
        try:
            api_hb_predict(mid)
        except Exception as ex:
            print(f'[hb-pre] {mid} 預測失敗：{ex}', flush=True)


# ============ 12BET 優先政策（用戶 2026-09-29 指示） ============
# 預測工具一切以易胜博(12)盤口及水位為準；12 未開盤 → Crown(3) 暫代顯示，
# 同時記入 line12_track：完場後／每日用 12BET 再試直至成功；
# 超過 7 日仍唔得 → 每日通知一次，直至解決。
_LINE12_NOTIFY = {'date': '', 'count': 0}


def _line12_record(conn, mid):
    _line12_track(conn)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        'INSERT INTO line12_track(match_id,attempts,first_ts,last_ts,crown) '
        'VALUES(?,?,?,?,1) ON CONFLICT(match_id) DO UPDATE SET '
        'crown=1, attempts=attempts+1, last_ts=excluded.last_ts',
        (mid, 1, now, now))
    conn.commit()


def _line12_track(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS line12_track('
                 'match_id INTEGER PRIMARY KEY, attempts INTEGER DEFAULT 0, '
                 'first_ts TEXT, last_ts TEXT, crown INTEGER DEFAULT 0, '
                 'resolved INTEGER DEFAULT 0)')


def line12_daily_retry(conn, crawler, fetcher, batch=150):
    """每日用 12BET 重試 crown 暫代中嘅場次，直至成功；
    超過 7 日唔得 → 每日記錄一次（工作記錄面板會見到）。"""
    _line12_track(conn)
    rows = conn.execute(
        'SELECT t.match_id, m.kickoff FROM line12_track t '
        'JOIN matches m ON m.id=t.match_id '
        'WHERE t.resolved=0 AND t.crown=1 '
        'ORDER BY t.last_ts LIMIT ?', (batch,)).fetchall()
    fixed = 0
    for mid, ko in rows:
        try:
            crawler.crawl_odds_for_match(conn, fetcher, mid, ko, 12)
        except Exception:
            continue
        if conn.execute(
                'SELECT 1 FROM odds_asian WHERE match_id=? AND company_id=12 '
                "AND label='closing'", (mid,)).fetchone():
            conn.execute('UPDATE line12_track SET resolved=1, '
                         'last_ts=? WHERE match_id=?',
                         (time.strftime('%Y-%m-%d %H:%M:%S'), mid))
            fixed += 1
        else:
            conn.execute('UPDATE line12_track SET attempts=attempts+1, '
                         'last_ts=? WHERE match_id=?',
                         (time.strftime('%Y-%m-%d %H:%M:%S'), mid))
        # 每場即放寫鎖：下一場嘅網絡取之（crawl_odds_for_match 自己會 commit
        # 賠率寫入）唔可以帶住 line12_track 嘅交易——舊版 150 場一條交易，
        # 逐場捱代理逾時，鎖庫一個鐘（2026-10-08 獵手 stacks 實證元兇）
        conn.commit()
    conn.commit()
    stuck = conn.execute(
        'SELECT COUNT(*) FROM line12_track WHERE resolved=0 AND crown=1 '
        "AND first_ts < datetime('now','localtime','-7 days')").fetchone()[0]
    if stuck and _LINE12_NOTIFY['date'] != time.strftime('%Y-%m-%d'):
        _LINE12_NOTIFY['date'] = time.strftime('%Y-%m-%d')
        _LINE12_NOTIFY['count'] = stuck
        crawler.log(conn, 'WARN',
                    '【12BET 缺盤】超過 7 日仍用 Crown 暫代嘅場次：%d 場——'
                    '每日重試緊，直至成功為止' % stuck)
        conn.commit()
    return fixed


# ============ 健康 / 工作記錄（前端連線狀態列＋工作 Log 面板，2026-09-29） ============
_health_cache = {'ts': 0.0, 'data_host_ok': None, 'probing': False}


# ============ ⚡ 半全場逆轉統計（2026-10-02 用戶要求嘅獨立頁） ============
# 按精選組合（check_rows 歷史庫）翻查：四種「半場領先一方最終唔贏」嘅出現率
# ①半場主勝→全場和 ②半場主勝→全場客 ③半場客勝→全場和 ④半場客勝→全場主，
# 加「四項任何一項」合計率。組合鍵＝方向＋g14（同主客原盤）＋g17（計埋互換）。
_HTFT_CACHE = {'ts': 0.0, 'data': None}
_HTFT_CACHE_TTL = 600


def api_htft():
    now = time.time()
    if _HTFT_CACHE['data'] is not None \
            and now - _HTFT_CACHE['ts'] < _HTFT_CACHE_TTL:
        return _HTFT_CACHE['data']
    conn = db()
    rows = conn.execute(
        'SELECT c.direction, c.g14, c.g17, m.half_home, m.half_away, '
        'm.home_score, m.away_score FROM check_rows c '
        'JOIN matches m ON m.id = c.match_id '
        'WHERE m.home_score IS NOT NULL AND m.half_home IS NOT NULL').fetchall()
    conn.close()
    agg = {}
    tot = {'n': 0, 'c': [0, 0, 0, 0]}
    for d, g14, g17, hh, ha, hs, aws in rows:
        if hh == ha:
            continue            # 2026-10-10：半場和唔會逆轉，計入 n 只會稀釋比率
        cat = 0
        if hh > ha:                     # 半場主勝
            if hs == aws:
                cat = 1                 # 全場和
            elif aws > hs:
                cat = 2                 # 全場客
        elif ha > hh:                   # 半場客勝
            if hs == aws:
                cat = 3                 # 全場和
            elif hs > aws:
                cat = 4                 # 全場主
        key = (d or '?', g14 or '-', g17 or '-')
        a = agg.setdefault(key, {'n': 0, 'c': [0, 0, 0, 0]})
        a['n'] += 1
        tot['n'] += 1
        if cat:
            a['c'][cat - 1] += 1
            tot['c'][cat - 1] += 1

    def pack(a):
        n = a['n']
        return {'n': n,
                'p': [round(x / n, 4) for x in a['c']],
                'p_any': round(sum(a['c']) / n, 4)}

    out = []
    for (d, g14, g17), a in agg.items():
        if a['n'] < 3:                  # 樣本太少冇參考價值
            continue
        r = pack(a)
        r.update({'direction': d, 'g14': g14, 'g17': g17})
        out.append(r)
    out.sort(key=lambda r: -r['n'])
    data = {'ok': True, 'total': pack(tot), 'rows': out[:300],
            'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
    _HTFT_CACHE['data'] = data
    _HTFT_CACHE['ts'] = now
    return data


# ============ ⚡ 半全場版面：即將開賽／進行中／已完結 ＋ 歷史同類比例 ============
# 「同類」定義＝同聯賽（req_name）＋同尾盤（讓球,讓球方）；比例＝四種半場領先被
# 逆轉／追和嘅歷史出現率；進行中場次另附「同半場領先」嘅全場走勢（守和/被追和/被逆轉）。
_HTFTB_CACHE = {'ts': 0.0, 'data': None}

_htftb_lock = threading.Lock()
_HTFT_STALE_LAST = 0.0   # 上次「進行中死場」觸發補抓嘅時間（5 分鐘防重複）
_HTFTB_CACHE_X = {}      # 帶篩選參數嘅回查快取：(fin_hours, fin_cat) -> (ts, data)


def _htft_cat(hh, ha, hs, aws):
    if hh > ha:
        if hs == aws:
            return 1            # 半主全和
        if aws > hs:
            return 2            # 半主全客
    elif ha > hh:
        if hs == aws:
            return 3            # 半客全和
        if hs > aws:
            return 4            # 半客全主
    return 0


def api_htftboard(fin_hours=None, fin_cat=None):
    """鎖＋舊錄兜底（2026-10-15 第二輪 debug）——同上：一個先建其他人食舊錄。"""
    now = time.time()
    custom = fin_hours is not None or bool(fin_cat)
    if custom:
        ck = _HTFTB_CACHE_X.get((fin_hours, fin_cat))
        if ck and now - ck[0] < 300:
            return ck[1]
    elif _HTFTB_CACHE['data'] is not None             and now - _HTFTB_CACHE['ts'] < 300:
        return _HTFTB_CACHE['data']
    if _htftb_lock.acquire(blocking=False):
        try:
            return _htftboard_build(fin_hours, fin_cat)   # 內部有 cache 複查
        finally:
            _htftb_lock.release()
    if custom:
        ck = _HTFTB_CACHE_X.get((fin_hours, fin_cat))
        if ck:
            return ck[1]
    if _HTFTB_CACHE['data'] is not None:
        return _HTFTB_CACHE['data']
    with _htftb_lock:
        return _htftboard_build(fin_hours, fin_cat)


def _htftboard_build(fin_hours=None, fin_cat=None):
    """fin_hours/fin_cat：已完結區域回查篩選（2026-10-10 用戶要求 Period／
    聯賽杯賽類 filter，二選一或二選二）。帶參數用獨立 5 分鐘快取，唔污染主快取。"""
    now = time.time()
    custom = fin_hours is not None or bool(fin_cat)
    if custom:
        ck = _HTFTB_CACHE_X.get((fin_hours, fin_cat))
        if ck and now - ck[0] < 300:
            return ck[1]
    elif _HTFTB_CACHE['data'] is not None \
            and now - _HTFTB_CACHE['ts'] < 300:
        return _HTFTB_CACHE['data']
    conn = db()
    try:
        # 2026-10-10：舊版同一組查詢寫咗兩次（下面 match_rec 前嗰段會重新
        # 查過兼覆寫），白白雙倍 DB 工作量——而家淨保留呢一份。
        # base_sql 用 LEFT JOIN：冇 12BET 尾盤嘅場次照顯示（舊版 INNER JOIN
        # 搞到佢哋喺三個區域完全消失，「資料唔齊全」元兇之一）。
        hk = "datetime('now','+8 hours')"
        base_sql = (
            'SELECT m.id, m.kickoff, c.req_name, c.category, '
            'ht.name_tc, at.name_tc, '
            'm.half_home, m.half_away, m.home_score, m.away_score, '
            'o.handicap, o.giver FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            "LEFT JOIN odds_asian o ON o.match_id=m.id AND o.label='closing' "
            'AND o.company_id=12 AND o.handicap IS NOT NULL ')
        upcoming = conn.execute(
            base_sql + f'WHERE m.home_score IS NULL AND m.kickoff >= {hk} '
            'ORDER BY m.kickoff LIMIT 24').fetchall()
        live = conn.execute(
            base_sql + 'WHERE m.home_score IS NULL '
            f"AND m.kickoff < {hk} "
            "AND m.kickoff >= datetime('now','+8 hours','-3 hours') "
            'ORDER BY m.kickoff DESC LIMIT 24').fetchall()
        fh = 48
        if fin_hours:
            try:
                fh = max(1, min(int(fin_hours), 720))   # 上限 30 日
            except (TypeError, ValueError):
                fh = 48
        flim = 200 if custom else 24
        fsql = (base_sql + 'WHERE m.home_score IS NOT NULL '
                f"AND m.kickoff >= datetime('now','+8 hours','-{fh} hours') ")
        fargs = ()
        if fin_cat:
            fsql += 'AND c.category=? '
            fargs = (fin_cat,)
        fsql += f'ORDER BY m.kickoff DESC LIMIT {flim}'
        finished = conn.execute(fsql, fargs).fetchall()

        # 2026-10-10 加強完場更新：進行中區域入面開賽超過 2 小時仲未入分嘅場次
        # （實際已完場但賽果未返，就係用戶見到「已完場仲喺進行中」嘅元兇）——
        # 唔再等 8 分鐘巡邏，即刻 fire-and-forget 觸發一輪賽果補抓。5 分鐘防重複、
        # 巡邏行緊就唔叠住觸發。
        global _HTFT_STALE_LAST
        cutoff = time.strftime('%Y-%m-%d %H:%M',
                               time.localtime(time.time() + 6 * 3600))
        if any(r[8] is None and r[1] <= cutoff for r in live) \
                and time.time() - _HTFT_STALE_LAST > 300 \
                and not _result_catchup['running']:
            _HTFT_STALE_LAST = time.time()
            threading.Thread(target=_result_catchup_once, daemon=True).start()

        # 1) 只聚合「顯示場次需要」嘅 class——512MB 實機全量 fetchall 會 OOM
        # （2026-10-06 實測：成個實例 crash loop）。逐行迭代，唔使的即刻丟。
        need = {(lg, h, (g or '')) for (_i, _k, lg, _c, _h, _a, _hh, _ha,
                                        _hs, _aw, h, g)
                in (upcoming + live + finished)}
        classes = {}
        for lg, h, g, hh, ha, hs, aws in conn.execute(
                'SELECT c.req_name, o.handicap, o.giver, '
                'm.half_home, m.half_away, m.home_score, m.away_score '
                'FROM matches m '
                "JOIN odds_asian o ON o.match_id=m.id AND o.label='closing' "
                'AND o.company_id=12 '
                'JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id '
                'WHERE m.home_score IS NOT NULL AND m.half_home IS NOT NULL '
                'AND o.handicap IS NOT NULL'):
            key = (lg, h, g or '')
            if key not in need:
                continue
            a = classes.setdefault(
                key, {'n': 0, 'c': [0, 0, 0, 0], 'htH': [0, 0, 0],
                      'htA': [0, 0, 0], 'nd': 0, 'htD': [0, 0, 0]})
            # 2026-10-10 修正：半場和（hh==ha）唔會出現逆轉——舊版照計入 n
            # 做分母，將逆轉率稀釋咗四成（全庫 88,705 場入面 36,318 場半場和，
            # 顯示 14.8% vs 正確 25.0%）。而家半場和單獨計 nd＋全場走勢 htD
            # [主勝, 和, 客勝]，live 場次可以顯示半場和之後嘅走勢。
            if hh == ha:
                a['nd'] += 1
                a['htD'][0 if hs > aws else (1 if hs == aws else 2)] += 1
                continue
            cat = _htft_cat(hh, ha, hs, aws)
            a['n'] += 1
            if cat:
                a['c'][cat - 1] += 1
            if hh > ha:
                a['htH'][0 if hs > aws else (1 if hs == aws else 2)] += 1
            elif ha > hh:
                a['htA'][0 if aws > hs else (1 if hs == aws else 2)] += 1

        def pack(a):
            n = a['n']
            return {'n': n,
                    'p': [round(x / n, 4) for x in a['c']],
                    'p_any': round(sum(a['c']) / n, 4)}

        def match_rec(r):
            (mid, ko, lg, cat, h, a, hh, ha, hs, aws, hc, gv) = r
            key = (lg, hc, gv or '')
            st = classes.get(key)
            rec = {'id': mid, 'ko': ko, 'lg': lg, 'cat': cat, 'home': h,
                   'away': a,
                   'line': screen_engine.fmt_line(hc, gv)
                   if hc is not None else '—',
                   'n': st['n'] if st else 0}
            if st:
                rec['nd'] = st['nd']
                pk = pack(st)
                rec['p'], rec['p_any'] = pk['p'], pk['p_any']
                if hh is not None and hs is None:   # 進行中：同半場局面走勢
                    if hh != ha:      # 半場有領先：守勝/和/被逆轉
                        side = 'htH' if hh > ha else 'htA'
                        t = st[side]
                        tot = sum(t)
                        if tot:
                            rec['live_p'] = [round(x / tot, 4) for x in t]
                            rec['live_side'] = 'lead'
                    else:             # 半場和：全場主勝/和/客勝
                        t = st['htD']
                        tot = sum(t)
                        if tot:
                            rec['live_p'] = [round(x / tot, 4) for x in t]
                            rec['live_side'] = 'draw'
            if hh is not None:
                rec['ht'] = f'{hh}-{ha}'
            if hs is not None:
                rec['ft'] = f'{hs}-{aws}'
                rec['cat'] = _htft_cat(hh, ha, hs, aws)
            # 3) 同類例子場次（最新 4 場）
            if st:
                ex = conn.execute(
                    'SELECT m.kickoff, ht.name_tc, at.name_tc, '
                    'm.half_home, m.half_away, m.home_score, m.away_score '
                    'FROM matches m '
                    'JOIN seasons s ON s.id=m.season_id '
                    'JOIN competitions c ON c.titan_id=s.titan_id '
                    'JOIN teams ht ON ht.titan_id=m.home_id '
                    'JOIN teams at ON at.titan_id=m.away_id '
                    "JOIN odds_asian o ON o.match_id=m.id AND o.label='closing' "
                    'AND o.company_id=12 '
                    'WHERE c.req_name=? AND o.handicap=? AND o.giver IS ? '
                    'AND m.home_score IS NOT NULL AND m.half_home IS NOT NULL '
                    'ORDER BY m.kickoff DESC LIMIT 4',
                    (lg, hc, gv)).fetchall()
                rec['ex'] = [{'ko': e[0][5:16], 't': f'{e[1]} vs {e[2]}',
                              'ht': f'{e[3]}-{e[4]}', 'ft': f'{e[5]}-{e[6]}',
                              'cat': _htft_cat(e[3], e[4], e[5], e[6])}
                             for e in ex]
            return rec

        data = {'ok': True,
                'upcoming': [match_rec(r) for r in upcoming],
                'live': [match_rec(r) for r in live],
                'finished': [match_rec(r) for r in finished],
                'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
    finally:
        conn.close()
    if custom:
        _HTFTB_CACHE_X[(fin_hours, fin_cat)] = (now, data)
    else:
        _HTFTB_CACHE['data'] = data
        _HTFTB_CACHE['ts'] = now
    return data


def api_health():
    """連線狀態：伺服器就緒、數據主機（titan007）狀態、最後成功更新時間。
    data_host 探測結果快取 60 秒——前端 30 秒輪詢一次，唔會增加主機負擔。
    2026-10-10：probe 改為背景刷新（stale-while-revalidate）——probe worst case
    係 6s 直連 + 12 個代理 × 5s ≈ 66 秒；數據主機斷線時 /api/health 成支窒住，
    前端每 30 秒輪詢就輪流捱逾時（實測 55 秒）。而家即時回上次已知值，
    探測喺背景 daemon 線程做，唔阻塞任何請求。"""
    now = time.time()
    if now - _health_cache['ts'] > 60 and not _health_cache.get('probing'):
        _health_cache['probing'] = True

        def _probe():
            try:
                import crawler as _cr
                _health_cache['data_host_ok'] = bool(_cr.data_host_probe())
            except Exception:
                pass   # 保留舊值；下次再探
            finally:
                _health_cache['ts'] = time.time()
                _health_cache['probing'] = False

        threading.Thread(target=_probe, daemon=True).start()
    last_upd = None
    try:
        conn = db()
        last_upd = conn.execute(
            'SELECT MAX(updated_at) FROM matches').fetchone()[0]
        conn.close()
    except Exception:
        pass
    return {'ok': True, 'ready': bool(_pool_ready.get('done')),
            'version': SERVER_VERSION,
            'data_host_ok': _health_cache['data_host_ok'],
            'crawl_mode': 'cloud' if DISABLE_UPDATE else 'server',
            # 2026-10-22 用戶規格「確定本機 APP 讀本機資料庫」：白紙黑字標明
            # 而家讀緊邊個 DB 檔案＋係咪本機（冇 DB_PATH 環境覆寫＝本機）。
            'db_path': DB_PATH,
            'db_local': not os.environ.get('DB_PATH'),
            'last_data_update': last_upd,
            'update': {'running': _update_state['running'],
                       'phase': _update_state['phase'],
                       'error': _update_state['error'],
                       'last_done': _update_state['last_done']},
            'catchup': {k: _result_catchup[k]
                        for k in ('last', 'fixed', 'error')},
            'phone_push': dict(_phone_push),
            'line12': {'stuck': _LINE12_NOTIFY['count'],
                       'notify_date': _LINE12_NOTIFY['date']}}


def api_worklog(limit=30):
    """最近嘅 crawler 工作記錄（crawl_log 表）＋更新／窗口更新／補抓狀態。
    前端「📋 工作記錄」面板用。"""
    rows = []
    try:
        conn = db()
        rows = conn.execute(
            'SELECT ts, level, msg FROM crawl_log ORDER BY id DESC LIMIT ?',
            (max(1, min(int(limit), 100)),)).fetchall()
        conn.close()
    except Exception:
        pass
    return {'ok': True,
            'update': {'running': _update_state['running'],
                       'phase': _update_state['phase'],
                       'error': _update_state['error'],
                       'last_result': _update_state['last_result'],
                       'last_done': _update_state['last_done']},
            'window': dict(_win_state),
            'catchup': {k: _result_catchup[k]
                        for k in ('last', 'fixed', 'error')},
            'log': [{'ts': ts, 'level': lv,
                     'msg': (msg or '')[:400]} for ts, lv, msg in rows]}


# ============ 聯賽預測（逐聯賽 >78% 規則，_lg_preds.json 離線挖掘） ============
_lgpred_cache = {'ts': 0, 'data': None}
LG_PRED_CACHE_TTL = 600


def _lgpred_load():
    now = time.time()
    if _lgpred_cache['data'] is not None and \
            now - _lgpred_cache['ts'] < LG_PRED_CACHE_TTL:
        return _lgpred_cache['data']
    p = os.path.join(BASE_DIR, '_lg_preds.json')
    data = {'leagues': {}}
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
    _lgpred_cache.update(ts=now, data=data)
    return data


def _rule_applies(rule, t):
    """規則條件係咪適用於今場（用今場自身屬性對照）。"""
    kind = rule['kind']
    p = rule['params']
    odds = t.get('odds') or {}
    Tc = screen_engine.tline(odds, 'closing')
    Ti = screen_engine.tline(odds, 'initial')
    pre = t.get('pre') or {}
    hp = t.get('home_prev') or {}

    def signed(T):
        if not T or T.get('h') is None:
            return None
        return T['h'] * {'home': 1, 'away': -1, 'none': 0}.get(
            T.get('g') or 'none', 0)

    def up_water(T):
        if not T:
            return None
        g = T.get('g') or 'none'
        ho, ao = T.get('ho'), T.get('ao')
        if g == 'home':
            return ho
        if g == 'away':
            return ao
        if ho is not None and ao is not None:
            return min(ho, ao)
        return None

    if kind in ('close_l', 'close_lw', 'move', 'move_lw', 'wmove'):
        if Tc is None or Tc.get('h') is None:
            return False
        if abs(Tc['h'] - p['h']) > 1e-9 or (Tc.get('g') or 'none') != p['g']:
            return False
        uw = up_water(Tc)
        if kind in ('close_lw', 'move_lw'):
            if uw is None or not (p['lo'] <= uw < p['hi']):
                return False
        if kind in ('move', 'move_lw'):
            si, sc = signed(Ti), signed(Tc)
            if si is None:
                return False
            dd = sc - si
            mv = '讓深' if dd > 0.001 else ('讓淺' if dd < -0.001 else '不變')
            if mv != p['delta']:
                return False
        if kind == 'wmove':
            iw = up_water(Ti)
            if uw is None or iw is None:
                return False
            wd = uw - iw
            if not (p['lo'] <= wd < p['hi']):
                return False
        return True
    if kind == 'form':
        hwp, awp = pre.get('home_home_wp'), pre.get('away_away_wp')
        if hwp is None or awp is None:
            return False
        return p['hlo'] <= hwp < p['hhi'] and p['alo'] <= awp < p['ahi']
    if kind == 'rank':
        hr, ar = pre.get('home_total_rank'), pre.get('away_total_rank')
        if not hr or not ar:
            return False
        rd = ar - hr
        return p['lo'] <= rd < p['hi']
    if kind == 'goal':
        hgd, agd = pre.get('home_home_gd'), pre.get('away_away_gd')
        if hgd is None or agd is None:
            return False
        return p['hlo'] <= hgd < p['hhi'] and p['alo'] <= agd < p['ahi']
    if kind == 'form_rank':
        hwp = pre.get('home_home_wp')
        hr, ar = pre.get('home_total_rank'), pre.get('away_total_rank')
        if hwp is None or not hr or not ar:
            return False
        rd = ar - hr
        return p['flo'] <= hwp < p['fhi'] and p['lo'] <= rd < p['hi']
    if kind == 'prev_line':
        if hp.get('h') is None:
            return False
        return abs(hp['h'] - p['h']) < 1e-9 and \
            (hp.get('g') or 'none') == p['g']
    return False


def api_lgpred():
    """未開賽每場：所屬聯賽／杯賽嘅 >78% 規則邊條適用＋綜合方向。"""
    import v3_engine
    data = _lgpred_load()
    leagues = data.get('leagues') or {}
    if not leagues:
        return {'predictions': {}, 'updated': data.get('computed_at')}
    conn = db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.id, c.req_name FROM matches m "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 "
            "AND oc.handicap IS NOT NULL "
            "WHERE m.home_score IS NULL AND m.kickoff >= ? "
            'ORDER BY m.kickoff LIMIT 300',
            (v3_engine.hk_now_str(),)).fetchall()
        out = {}
        for mid, lg in rows:
            info = leagues.get(lg)
            if not info:
                continue
            t = screen_engine.get_target(conn, mid)
            if not t:
                continue
            hits = [r for r in info['rules'] if _rule_applies(r, t)]
            if not hits:
                continue
            up_n = sum(r['n'] for r in hits if r['direction'] == 'up')
            dn_n = sum(r['n'] for r in hits if r['direction'] == 'down')
            out[str(mid)] = {
                'league': lg,
                'direction': 'up' if up_n >= dn_n else 'down',
                'rules': [{'desc': r['desc'], 'direction': r['direction'],
                           'n': r['n'],
                           'rate': r['up_r'] if r['direction'] == 'up'
                           else r['down_r'],
                           'test_n': r['test']['n']} for r in hits[:3]]}
        return {'predictions': out, 'updated': data.get('computed_at')}
    finally:
        conn.close()


# ============ 同賽事盤口規則（用戶 2026-10-05：主頁每場預測上下盤%） ============
# 我設計嘅規則「同賽事盤口模型」——三因子 logit 合成：
#  F1 同賽事・聯賽獨立盤口因子（權重45%）：該聯賽「同讓球方＋同盤口線」歷史上盤率，
#     k=30 收縮→該聯賽整體上盤率（k=60 收縮→全庫整體）——每個聯賽/杯賽獨立一張表
#  F2 同賽事・聯賽水位帶因子（20%）：該聯賽「同盤口＋上盤水位帶(±0.05)」上盤率，
#     k=15 收縮→F1
#  F3 球隊近况因子（35%）：match_derived 主隊主場/全部＋客隊客場/全部 近20場贏盤率
#     → 期望上盤率 = (主贏盤率 + 1−客贏盤率)/2，k=8 收縮→50%
#  z = 0.45·logit(F1) + 0.20·logit(F2) + 0.35·logit(F3)，P(上盤) = σ(z)
_oupred_cache = {'ts': 0, 'cur': None, 'prior26': None, 'prior10': None,
                 'bt': None, 'hist': None}
_oupred_lock = threading.Lock()
OUPRED_TTL = 3600


def _lg_shrink(up, n, prior_up, prior_n, k):
    return (up + k * prior_up) / (n + k)


def _build_ou_tables(conn, before=None):
    """建 聯賽×盤口／水位帶 上盤率表。before 只計該時刻前完場（out-of-sample 回查用）。"""
    sql = (
        "SELECT c.req_name, oc.handicap, oc.giver, oc.home_odds, oc.away_odds, "
        "m.home_score, m.away_score FROM matches m "
        "JOIN seasons s ON s.id=m.season_id "
        "JOIN competitions c ON c.titan_id=s.titan_id "
        "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
        "AND oc.company_id=12 "
        "WHERE m.home_score IS NOT NULL AND oc.handicap IS NOT NULL")
    args = ()
    if before:
        sql += " AND m.kickoff < ?"
        args = (before,)
    lg_tot = {}
    line_tab = {}
    water_tab = {}
    a_tot = [0, 0]
    for lg, hc, gv, ho, ao, hs, aws in conn.execute(sql, args):
        r = pick_result(hc, gv, hs, aws)
        if r is None:
            continue
        up = 1 if r == 'A' else 0
        key = '%s|%.2f' % (gv or 'none', float(hc))
        t = lg_tot.setdefault(lg, [0, 0]); t[0] += up; t[1] += 1
        a_tot[0] += up; a_tot[1] += 1
        lt = line_tab.setdefault((lg, key), [0, 0]); lt[0] += up; lt[1] += 1
        uw = None
        if gv == 'home':
            uw = ho
        elif gv == 'away':
            uw = ao
        elif ho is not None and ao is not None:
            uw = min(ho, ao)
        if uw is not None:
            band = round(float(uw) / 0.05)
            wt = water_tab.setdefault((lg, key, band), [0, 0])
            wt[0] += up; wt[1] += 1
    lg_tot['__ALL__'] = a_tot
    return {'lg': lg_tot, 'line': line_tab, 'water': water_tab}


def _oupred_tables(force=False):
    now = time.time()
    if not force and _oupred_cache['cur'] is not None and \
            now - _oupred_cache['ts'] < OUPRED_TTL:
        return _oupred_cache
    # 2026-10-15：一個先建、其他食舊錶。舊版冇鎖冇舊錶兜底——TTL 一到期，
    # 所有並發請求同時落樓 rebuild（每次三次全庫掃描），互相搶 CPU/磁盤，
    # 主頁 /api/v3/list、/api/upcoming 集體 hang 分鐘級（seizure_stacks
    # 兩條線程同時卡在 _build_ou_tables 實證）；而家起樓期間請求照食舊錶，
    # 用家永遠唔會等 rebuild。
    if _oupred_lock.acquire(blocking=False):
        try:
            now = time.time()   # 入鎖後複查——可能上一個已經建好
            if not force and _oupred_cache['cur'] is not None and \
                    now - _oupred_cache['ts'] < OUPRED_TTL:
                return _oupred_cache
            conn = db()
            try:
                cur = _build_ou_tables(conn)
                prior26 = _build_ou_tables(conn, '2026-01-01 00:00')
                prior10 = _build_ou_tables(conn, '2026-10-01 00:00')
                _oupred_cache.update(ts=now, cur=cur, prior26=prior26,
                                     prior10=prior10, bt=None, hist=None)
            finally:
                conn.close()
        finally:
            _oupred_lock.release()
        return _oupred_cache
    if _oupred_cache['cur'] is not None:
        return _oupred_cache        # 有人起緊樓：舊錄照食
    with _oupred_lock:              # 首次啟動冇舊錶，先等佢起完
        return _oupred_cache


def _logit(p):
    p = min(max(p, 0.02), 0.98)
    import math
    return math.log(p / (1 - p))


def _sigmoid(z):
    import math
    return 1.0 / (1.0 + math.exp(-z))


def _oupred_one(tab, lg, hc, gv, ho, ao, drv):
    """單場預測。drv = match_derived row（可 None）。回傳 dict 或 None。"""
    if hc is None:
        return None
    key = '%s|%.2f' % (gv or 'none', float(hc))
    lg_tab = tab['lg']
    all_up, all_n = lg_tab.get('__ALL__', [0, 0])
    all_r = all_up / all_n if all_n else 0.5
    lu, ln_ = lg_tab.get(lg, [0, 0])
    lg_r = _lg_shrink(lu, ln_, all_r, 0, 60)
    u1, n1 = tab['line'].get((lg, key), [0, 0])
    f1 = _lg_shrink(u1, n1, lg_r, 0, 30)
    uw = None
    if gv == 'home':
        uw = ho
    elif gv == 'away':
        uw = ao
    elif ho is not None and ao is not None:
        uw = min(ho, ao)
    f2 = f1
    n2 = 0
    if uw is not None:
        band = round(float(uw) / 0.05)
        u2, n2 = tab['water'].get((lg, key, band), [0, 0])
        f2 = _lg_shrink(u2, n2, f1, 0, 15)
    # F3 球隊近况
    f3 = 0.5
    n3 = 0
    if drv:
        hw = [x for x in (drv[0], drv[1]) if x is not None]
        aw = [x for x in (drv[2], drv[3]) if x is not None]
        if hw and aw:
            hwr = sum(hw) / len(hw)
            awr = sum(aw) / len(aw)
            f3 = _lg_shrink((hwr + 1 - awr) / 2, (len(hw) + len(aw)) * 10, 0.5, 0, 8)
            n3 = len(hw) + len(aw)
    z = 0.45 * _logit(f1) + 0.20 * _logit(f2) + 0.35 * _logit(f3)
    p = _sigmoid(z)
    return {'up_r': round(p * 100, 1), 'down_r': round((1 - p) * 100, 1),
            'dir': 'up' if p >= 0.5 else 'down',
            'f': [round(f1 * 100, 1), round(f2 * 100, 1), round(f3 * 100, 1)],
            'n_line': n1, 'n_water': n2, 'n_team': n3}


def _oupred_derived_map(conn, mids):
    """match_derived 批量讀：主主場/主全部/客客場/客全部 近20場贏盤率。"""
    if not mids:
        return {}
    rows = conn.execute(
        'SELECT match_id, home_hw_winrate_home, home_hw_winrate_all, '
        'away_hw_winrate_away, away_hw_winrate_all FROM match_derived '
        'WHERE match_id IN (%s)' % ','.join('?' * len(mids)), mids).fetchall()
    return {r[0]: r[1:] for r in rows}


def _attach_oupred(out):
    """upcoming() 用：每場附上/下盤%預測（current 表＋球隊因子）。"""
    tab = _oupred_tables()['cur']
    ids = [r['id'] for r in out]
    conn = db()
    try:
        drv = _oupred_derived_map(conn, ids)
    finally:
        conn.close()
    for rec in out:
        ln = rec.get('line')
        if not ln:
            continue
        try:
            v = ln.get('v')
            gv = ln.get('giver') or 'none'
            hc = abs(float(v)) if v is not None else None
            pr = _oupred_one(tab, rec['league'], hc, gv, ln.get('ho'),
                             ln.get('ao'), drv.get(rec['id']))
            if pr:
                rec['oupred'] = pr
        except Exception:
            pass


_FW8S_WARMING = {'on': False}


def _attach_preds3(out):
    """主頁訊號帶（2026-10-16 三欄：精選8／精選8s／好波大細；2026-10-25 用戶
    規格改兩欄，好波大細落架——佢係非篩選結果殘留，同朝早清除嘅好波 chip
    同一性質；10-22 已廢嘅 lean/最強單項≥52% 共識仲殘留喺度當訊號顯示）。
    精選8 行 fw_grid_featured（格掃描寫入；雲端舊庫缺表當冇紀錄）。
    精選8s 兩源合併：fw8s_log（入選紀錄，direction 空預設 up——8s 專搵上盤）
    ＋ _FW8S_CACHE（8s 頁同款 scan_upcoming 快取）；快取舊過 600s 就背景
    刷新一次（_FW8S_WARMING 防重疊，唔阻塞主頁）。唔阻塞主頁、唔加重即時計算。"""
    ids = [r['id'] for r in out]
    if not ids:
        return
    ph = ','.join('?' * len(ids))
    conn = db()
    try:
        _tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        f8 = f8s = {}
        if 'fw_grid_featured' in _tbls:
            f8 = {r[0]: r[1] for r in conn.execute(
                "SELECT match_id, direction FROM fw_grid_featured "
                "WHERE grid='8' AND match_id IN (" + ph + ")", ids)}
        if 'fw8s_log' in _tbls:
            f8s = {r[0]: (r[1] or 'up') for r in conn.execute(
                "SELECT f.match_id, f.direction FROM fw8s_log f "
                "JOIN matches m ON m.id=f.match_id "
                "WHERE m.home_score IS NULL AND f.result IS NULL "
                "AND f.match_id IN (" + ph + ")", ids)}
    finally:
        conn.close()
    # 8s 快取補完＋背景保鮮（快取舊就 trigger 一次 scan_upcoming，下一輪有數）。
    # 雲端（DISABLE_UPDATE）唔自動暖——scan_upcoming 入面 load_v3_pool 冷 build
    # ~128MB，若撞正開機 preheat 嘅 build 會雙重疊加 OOM（512MB 實例）；
    # 雲端 8s 快取靠用戶開 8s 頁刷新，主頁 f8s 欄照顯 fw8s_log 已有紀錄。
    try:
        c8 = _FW8S_CACHE.get('data')
        if c8:
            for p in c8.get('picks') or []:
                f8s.setdefault(p.get('match_id'), 'up')
        if (not DISABLE_UPDATE
                and time.time() - _FW8S_CACHE.get('ts', 0) > 600
                and not _FW8S_WARMING.get('on')):
            _FW8S_WARMING['on'] = True

            def _warm():
                try:
                    api_fw8s_full(72)
                except Exception:
                    pass
                finally:
                    _FW8S_WARMING['on'] = False
            threading.Thread(target=_warm, daemon=True).start()
    except Exception:
        pass
    for rec in out:
        mid = rec['id']
        if mid in f8:
            rec['f8'] = ('上' if f8[mid] == 'up'
                         else ('下' if f8[mid] == 'down' else f8[mid]))
        if mid in f8s:
            rec['f8s'] = ('上' if f8s[mid] == 'up'
                          else ('下' if f8s[mid] == 'down' else f8s[mid]))


def api_twin(mid):
    """👯 搵孖生（2026-10-13）：全庫／同聯賽 搵歷史上盤口完全相同嘅完場場次。
    六項條件全部相同先算孖生：
      1. 上一次對賽尾盤讓球盤（讓球數＋讓球方）100% 同
      2. 上一次對賽贏盤方（上/下/走）相同
      3. 初盤（讓球＋讓球方 100% 同；主、客水位各 ±0.03）
      4. 開賽前四小時盤口（同上）
      5. 尾盤（同上）
      6. 尾盤大小球（總入球線 100% 同；大、細賠率各 ±0.03）
    盤口來源同主頁一致：易胜博(12)，無則 Crown(3)；大小球用易胜博(12)。
    只計已完場（有比分）且開賽時間早過目標場次嘅孖生。"""
    conn = db()
    try:
        t = conn.execute(
            "SELECT m.kickoff, m.home_id, m.away_id, c.titan_id, c.req_name, "
            "ht.name_tc, at.name_tc FROM matches m "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "JOIN teams ht ON ht.titan_id=m.home_id "
            "JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?", (mid,)).fetchone()
        if not t:
            return {'error': '找不到賽事'}
        tko, thid, taid, tcomp, tlg, thn, tan = t

        def snap(label):
            # 易胜博(12) 優先，Crown(3) 後備——同主頁盤口顯示邏輯
            r = conn.execute(
                'SELECT handicap, giver, home_odds, away_odds FROM odds_asian '
                'WHERE match_id=? AND company_id=12 AND label=?',
                (mid, label)).fetchone()
            if r is None:
                r = conn.execute(
                    'SELECT handicap, giver, home_odds, away_odds FROM odds_asian '
                    'WHERE match_id=? AND company_id=3 AND label=?',
                    (mid, label)).fetchone()
            return r

        init, h4, close = snap('initial'), snap('pre_4h'), snap('closing')
        ou = conn.execute(
            'SELECT total_line, over_odds, under_odds FROM odds_ou '
            'WHERE match_id=? AND company_id=12 AND label=?', (mid, 'closing')
        ).fetchone()
        lack = []
        if init is None:
            lack.append('初盤')
        if h4 is None:
            lack.append('開賽前四小時盤口')
        if close is None:
            lack.append('尾盤')
        if ou is None:
            lack.append('尾盤大小球')
        if lack:
            return {'error': '該場仲未有' + '、'.join(lack) + '資料，齊咗先可以搵孖生'}

        def prev_h2h(hid, aid, before, excl):
            # 兩隊上一次交鋒（完場、有尾盤讓球盤），主客次序唔計
            return conn.execute(
                "SELECT m.home_score, m.away_score, "
                "COALESCE(o12.handicap, o3.handicap), "
                "COALESCE(o12.giver, o3.giver) FROM matches m "
                "LEFT JOIN odds_asian o12 ON o12.match_id=m.id "
                "AND o12.company_id=12 AND o12.label='closing' "
                "LEFT JOIN odds_asian o3 ON o3.match_id=m.id "
                "AND o3.company_id=3 AND o3.label='closing' "
                "WHERE m.home_score IS NOT NULL AND m.kickoff < ? AND m.id != ? "
                "AND ((m.home_id=? AND m.away_id=?) OR (m.home_id=? AND m.away_id=?)) "
                "ORDER BY m.kickoff DESC LIMIT 1",
                (before, excl, hid, aid, aid, hid)).fetchone()

        th = prev_h2h(thid, taid, tko, mid)
        if th is None or th[2] is None:
            return {'error': '搵唔到兩隊上一次對賽（或對賽無尾盤盤口），對賽條件用唔到，搵唔到孖生'}
        t_hc, t_gv = th[2], th[3]
        t_hres = pick_result(t_hc, t_gv, th[0], th[1])

        rows = conn.execute(
            "SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc, "
            "m.home_id, m.away_id, m.home_score, m.away_score "
            "FROM matches m "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "JOIN teams ht ON ht.titan_id=m.home_id "
            "JOIN teams at ON at.titan_id=m.away_id "
            "LEFT JOIN odds_asian oc12 ON oc12.match_id=m.id "
            "AND oc12.company_id=12 AND oc12.label='closing' "
            "LEFT JOIN odds_asian oc3 ON oc3.match_id=m.id "
            "AND oc3.company_id=3 AND oc3.label='closing' "
            "LEFT JOIN odds_asian oi12 ON oi12.match_id=m.id "
            "AND oi12.company_id=12 AND oi12.label='initial' "
            "LEFT JOIN odds_asian oi3 ON oi3.match_id=m.id "
            "AND oi3.company_id=3 AND oi3.label='initial' "
            "LEFT JOIN odds_asian o412 ON o412.match_id=m.id "
            "AND o412.company_id=12 AND o412.label='pre_4h' "
            "LEFT JOIN odds_asian o43 ON o43.match_id=m.id "
            "AND o43.company_id=3 AND o43.label='pre_4h' "
            "JOIN odds_ou ou ON ou.match_id=m.id "
            "AND ou.company_id=12 AND ou.label='closing' "
            "WHERE m.home_score IS NOT NULL AND m.kickoff < ? AND m.id != ? "
            "AND COALESCE(oc12.handicap, oc3.handicap) = ? "
            "AND COALESCE(oc12.giver, oc3.giver) IS ? "
            "AND ABS(COALESCE(oc12.home_odds, oc3.home_odds) - ?) <= 0.030000001 "
            "AND ABS(COALESCE(oc12.away_odds, oc3.away_odds) - ?) <= 0.030000001 "
            "AND COALESCE(oi12.handicap, oi3.handicap) = ? "
            "AND COALESCE(oi12.giver, oi3.giver) IS ? "
            "AND ABS(COALESCE(oi12.home_odds, oi3.home_odds) - ?) <= 0.030000001 "
            "AND ABS(COALESCE(oi12.away_odds, oi3.away_odds) - ?) <= 0.030000001 "
            "AND COALESCE(o412.handicap, o43.handicap) = ? "
            "AND COALESCE(o412.giver, o43.giver) IS ? "
            "AND ABS(COALESCE(o412.home_odds, o43.home_odds) - ?) <= 0.030000001 "
            "AND ABS(COALESCE(o412.away_odds, o43.away_odds) - ?) <= 0.030000001 "
            "AND ou.total_line = ? "
            "AND ABS(ou.over_odds - ?) <= 0.030000001 AND ABS(ou.under_odds - ?) <= 0.030000001",
            (tko, mid,
             close[0], close[1], close[2], close[3],
             init[0], init[1], init[2], init[3],
             h4[0], h4[1], h4[2], h4[3],
             ou[0], ou[1], ou[2])).fetchall()

        names = {r[0]: r[1] for r in conn.execute(
            'SELECT titan_id, name_tc FROM teams')}
        all_cnt = {'A': 0, 'B': 0, 'P': 0}
        lg_cnt = {'A': 0, 'B': 0, 'P': 0}
        twins = []
        for (cid, cko, clg, chn, can, chid, caid, chs, cas) in rows:
            ch = prev_h2h(chid, caid, cko, cid)
            if ch is None or ch[2] is None:
                continue
            if not (ch[2] == t_hc and (ch[3] == t_gv or (ch[3] is None
                                                         and t_gv is None))):
                continue
            if pick_result(ch[2], ch[3], ch[0], ch[1]) != t_hres:
                continue
            res = pick_result(close[0], close[1], chs, cas)
            same_lg = None
            ccomp = conn.execute(
                'SELECT c.titan_id FROM matches m JOIN seasons s ON s.id=m.season_id '
                'JOIN competitions c ON c.titan_id=s.titan_id WHERE m.id=?',
                (cid,)).fetchone()
            same_lg = bool(ccomp and ccomp[0] == tcomp)
            all_cnt[res] += 1
            if same_lg:
                lg_cnt[res] += 1
            twins.append({'id': cid, 'ko': cko, 'lg': clg,
                          'h': names.get(chid, ''), 'a': names.get(caid, ''),
                          'score': f'{chs}-{cas}', 'r': res, 'same_lg': same_lg})

        def _stat(cnt):
            n = cnt['A'] + cnt['B'] + cnt['P']
            return {'n': n, 'up': cnt['A'], 'down': cnt['B'], 'push': cnt['P'],
                    'up_pct': round(100 * cnt['A'] / n, 1) if n else 0,
                    'down_pct': round(100 * cnt['B'] / n, 1) if n else 0,
                    'push_pct': round(100 * cnt['P'] / n, 1) if n else 0}

        def _fmt(hc, gv, ho, ao):
            return (screen_engine.fmt_line(hc, gv)
                    + (f'　主{ho:.2f}/客{ao:.2f}'
                       if ho is not None and ao is not None else ''))

        hres_txt = {'A': '上盤贏', 'B': '下盤贏', 'P': '走盤'}[t_hres]
        twins.sort(key=lambda x: x['ko'], reverse=True)
        return {'ok': True,
                'league': tlg,
                'criteria': [
                    f'上一次對賽尾盤：{screen_engine.fmt_line(t_hc, t_gv)}（{hres_txt}）',
                    f'初盤：{_fmt(*init)}',
                    f'開賽前四小時：{_fmt(*h4)}',
                    f'尾盤：{_fmt(*close)}',
                    f'尾盤大小球：{ou[0]:g}　大{ou[1]:.2f}/細{ou[2]:.2f}'],
                'all': _stat(all_cnt), 'league_stat': _stat(lg_cnt),
                'list': twins[:80], 'list_total': len(twins)}
    finally:
        conn.close()


_twin_cache = {}
_twin_cache_ttl = 120


def _oupred_backtest():
    """回查資料庫命中%：用 2026 前嘅表，對 2026 年完場場次逐場預測再對賽果。"""
    c = _oupred_tables()
    if c['bt'] is not None:
        return c['bt']
    conn = db()
    try:
        rows = conn.execute(
            "SELECT m.id, c.req_name, oc.handicap, oc.giver, oc.home_odds, "
            "oc.away_odds, m.home_score, m.away_score, "
            "d.home_hw_winrate_home, d.home_hw_winrate_all, "
            "d.away_hw_winrate_away, d.away_hw_winrate_all "
            "FROM matches m JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
            "AND oc.company_id=12 "
            "LEFT JOIN match_derived d ON d.match_id=m.id "
            "WHERE m.home_score IS NOT NULL AND m.kickoff >= '2026-01-01' "
            "AND oc.handicap IS NOT NULL").fetchall()
        tab = c['prior26']
        hits = 0; tot = 0; up_hits = 0; up_tot = 0; dn_hits = 0; dn_tot = 0
        per_lg = {}
        for (mid, lg, hc, gv, ho, ao, hs, aws, h1, h2, a1, a2) in rows:
            pr = _oupred_one(tab, lg, hc, gv, ho, ao, (h1, h2, a1, a2))
            if not pr:
                continue
            r = pick_result(hc, gv, hs, aws)
            if r in (None, 'P'):
                continue
            hit = (r == 'A') == (pr['dir'] == 'up')
            tot += 1; hits += 1 if hit else 0
            if pr['dir'] == 'up':
                up_tot += 1; up_hits += 1 if hit else 0
            else:
                dn_tot += 1; dn_hits += 1 if hit else 0
            L = per_lg.setdefault(lg, [0, 0])
            L[0] += 1 if hit else 0; L[1] += 1
        bt = {'n': tot,
              'hit_r': round(hits / tot * 100, 1) if tot else None,
              'up': {'n': up_tot,
                     'hit_r': round(up_hits / up_tot * 100, 1) if up_tot else None},
              'down': {'n': dn_tot,
                       'hit_r': round(dn_hits / dn_tot * 100, 1) if dn_tot else None},
              'per_league': sorted(
                  ({'league': k, 'n': v[1],
                    'hit_r': round(v[0] / v[1] * 100, 1)} for k, v in per_lg.items()
                   if v[1] >= 30), key=lambda x: -x['n'])[:20]}
        c['bt'] = bt
        return bt
    finally:
        conn.close()


def _oupred_history():
    """2026-10-01 起完場場次：規則預測（10月前嘅表，out-of-sample）vs 實際賽果。"""
    c = _oupred_tables()
    if c['hist'] is not None:
        return c['hist']
    conn = db()
    try:
        rows = conn.execute(
            "SELECT m.id, c.req_name, m.kickoff, ht.name_tc, at.name_tc, "
            "oc.handicap, oc.giver, oc.home_odds, oc.away_odds, "
            "m.home_score, m.away_score, "
            "d.home_hw_winrate_home, d.home_hw_winrate_all, "
            "d.away_hw_winrate_away, d.away_hw_winrate_all "
            "FROM matches m JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "JOIN teams ht ON ht.titan_id=m.home_id "
            "JOIN teams at ON at.titan_id=m.away_id "
            "JOIN odds_asian oc ON oc.match_id=m.id AND oc.label='closing' "
            "AND oc.company_id=12 "
            "LEFT JOIN match_derived d ON d.match_id=m.id "
            "WHERE m.home_score IS NOT NULL AND m.kickoff >= '2026-10-01' "
            "AND oc.handicap IS NOT NULL "
            "ORDER BY m.kickoff DESC LIMIT 500").fetchall()
        tab = c['prior10']
        out = []
        hits = 0; tot = 0
        for (mid, lg, ko, h, a, hc, gv, ho, ao, hs, aws,
             h1, h2, a1, a2) in rows:
            pr = _oupred_one(tab, lg, hc, gv, ho, ao, (h1, h2, a1, a2))
            if not pr:
                continue
            r = pick_result(hc, gv, hs, aws)
            if r is None:
                continue
            hit = None
            if r != 'P':
                hit = ((r == 'A') == (pr['dir'] == 'up'))
                tot += 1; hits += 1 if hit else 0
            out.append({'id': mid, 'league': lg, 'kickoff': ko, 'home': h,
                        'away': a, 'score': '%d-%d' % (hs, aws),
                        'pred_up': pr['up_r'], 'pred_dir': pr['dir'],
                        'actual': r, 'hit': hit})
        hist = {'rows': out,
                'stats': {'n': tot,
                          'hits': hits,
                          'hit_r': round(hits / tot * 100, 1) if tot else None}}
        c['hist'] = hist
        return hist
    finally:
        conn.close()


# ============ 精選8s（2026-10-14）：專搵上盤嘅篩選工具 ============
# 基礎訊號：check 格共識方向=上 → 全庫 75.9%（2021–26 逐年 70–82%，
# 前後半段 77/75%）。每聯賽獨立公式（fw8s_league_rules.json）：
# direct 直選／refine 精煉／watch 觀望。引擎詳情見 fw8s_engine.py。
_FW8S_CACHE = {'ts': 0.0, 'data': None, 'hours': 72}
_FW8S_RULES_CACHE = {'ts': 0.0, 'data': None}


def _fw8s_settle(conn):
    """幫 fw8s_log 未結算場次結算（上盤方向='up'：A=贏 B=輸 P=走）。
    用落注時影低嘅 line（中文盤文字）解析；解析唔到/NULL 先對 closing。"""
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''CREATE TABLE IF NOT EXISTS fw8s_log(
        match_id INTEGER PRIMARY KEY, league TEXT, direction TEXT,
        mode TEXT, conds TEXT, line TEXT, odds TEXT,
        added_at TEXT, result TEXT, settled_at TEXT)''')
    rows = conn.execute(
        'SELECT f.match_id, m.home_score, m.away_score, f.line, '
        'o.handicap, o.giver FROM fw8s_log f '
        'JOIN matches m ON m.id=f.match_id '
        'LEFT JOIN odds_asian o ON o.match_id=f.match_id '
        "AND o.label='closing' AND o.company_id=12 "
        'WHERE f.result IS NULL AND m.home_score IS NOT NULL').fetchall()
    for mid, hs, aws, ltxt, chc, cgv in rows:
        pl = _parse_pick_line_txt(ltxt)
        hc, gv = pl if pl is not None else (chc, cgv)
        r = pick_result(hc, gv, hs, aws)
        if r is None:
            continue
        res = 'P' if r == 'P' else ('W' if r == 'A' else 'L')
        conn.execute('UPDATE fw8s_log SET result=?, settled_at=? '
                     'WHERE match_id=?', (res, now, mid))
    if rows:
        conn.commit()
    return len(rows)


def api_fw8s_full(hours=72, force=False):
    """精選8s 主頁：未來場次掃描（入選＋觀望）＋每聯賽歷史統計。
    寫入（記錄入選＋結算）放背景執行——爬蟲長寫交易鎖庫，同步寫會 500。
    force=1（人手「重新整理」）：先觸發「即時更新全部」重抓盤口賠率，
    等佢搞掂（上限 70 秒）先重新篩選——唔係齋跳過快取。"""
    import fw8s_engine
    now = time.time()
    refreshed = None
    if force:
        st = do_fetch_batch(hours=min(int(hours or 6), 24))
        if st.get('started'):
            t0 = time.time()
            while (time.time() - t0 < 70
                   and _fbatch_state['running']
                   and not _fbatch_state.get('error')):
                time.sleep(2)
            refreshed = {'ok': _fbatch_state.get('ok', 0),
                         'fail': _fbatch_state.get('fail', 0),
                         'total': _fbatch_state.get('total', 0),
                         'done': (not _fbatch_state['running']),
                         'err': _fbatch_state.get('error')}
        elif st.get('running'):
            refreshed = {'busy': True}
        elif st.get('disabled'):
            refreshed = {'disabled': True}
        else:
            refreshed = {'err': st.get('error')}
    if (not force and _FW8S_CACHE['data'] is not None
            and _FW8S_CACHE['hours'] == hours
            and now - _FW8S_CACHE['ts'] < 600):
        return _FW8S_CACHE['data']
    conn = db()
    try:
        picks, watched, stats = fw8s_engine.scan_upcoming(conn, hours)
        rules = fw8s_engine.load_rules()
        modes = {'direct': 0, 'refine': 0, 'watch': 0}
        for r in rules.values():
            modes[r.get('mode', 'watch')] = modes.get(r.get('mode', 'watch'), 0) + 1
        data = {'ok': True, 'picks': picks, 'watched': watched,
                'stats': stats,
                'summary': {'hours': hours, 'picks': len(picks),
                            'watched': len(watched),
                            'direct_leagues': modes.get('direct', 0),
                            'refine_leagues': modes.get('refine', 0),
                            'watch_leagues': modes.get('watch', 0)},
                'settled_now': 0,
                'refreshed': refreshed,
                'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
        _FW8S_CACHE['data'] = data
        _FW8S_CACHE['ts'] = now
        _FW8S_CACHE['hours'] = hours
    finally:
        conn.close()

    import threading

    def _bg(recs):
        for attempt in range(6):
            try:
                c2 = db()
                try:
                    fw8s_engine.log_picks(c2, recs)
                    _fw8s_settle(c2)
                finally:
                    c2.close()
                return
            except sqlite3.OperationalError:
                time.sleep(5)
            except Exception:
                return
    try:
        threading.Thread(target=_bg, args=(list(picks),),
                         daemon=True).start()
    except Exception:
        pass
    return data


def api_fw8s_rules():
    """每聯賽公式＋分段回查：整體／逐年命中率（check_rows 口徑）。"""
    now = time.time()
    if _FW8S_RULES_CACHE['data'] is not None \
            and now - _FW8S_RULES_CACHE['ts'] < 3600:
        return _FW8S_RULES_CACHE['data']
    import fw8s_engine
    conn = db()
    try:
        rules = fw8s_engine.load_rules()
        rows = conn.execute("""
            SELECT c.req_name, m.kickoff, cr.res FROM check_rows cr
            JOIN matches m ON m.id=cr.match_id
            JOIN seasons s ON s.id=m.season_id
            JOIN competitions c ON c.titan_id=s.titan_id
            WHERE cr.direction='up'""").fetchall()
        per = {}
        for lg, ko, res in rows:
            e = per.setdefault(lg, {'A': 0, 'B': 0, 'P': 0, 'years': {}})
            e[res] = e.get(res, 0) + 1
            y = (ko or '????')[:4]
            ye = e['years'].setdefault(y, {'A': 0, 'B': 0})
            if res in ('A', 'B'):
                ye[res] += 1
        out = []
        for lg, e in per.items():
            eff = e['A'] + e['B']
            r = rules.get(lg, {})
            years = {y: {'n': v['A'] + v['B'],
                         'up_r': round(v['A'] / (v['A'] + v['B']), 4)
                         if v['A'] + v['B'] else None}
                     for y, v in sorted(e['years'].items())}
            out.append({'league': lg, 'n': eff,
                        'up_r': round(e['A'] / eff, 4) if eff else None,
                        'mode': r.get('mode', 'watch'),
                        'conds': r.get('conds'),
                        'refine': r.get('refine'),
                        'years': years})
        # 公式聯賽但 check_rows 未有紀錄（如澳維超／歐國聯）：照列出，命中率待補
        for rn, r in rules.items():
            if rn not in per:
                out.append({'league': rn, 'n': 0, 'up_r': None,
                            'mode': r.get('mode', 'watch'),
                            'conds': r.get('conds'),
                            'refine': r.get('refine'), 'years': {}})
        out.sort(key=lambda x: -x['n'])
        tot_a = sum(e['A'] for e in per.values())
        tot_b = sum(e['B'] for e in per.values())
        data = {'ok': True, 'rows': out,
                'total': {'n': tot_a + tot_b,
                          'up_r': round(tot_a / (tot_a + tot_b), 4)
                          if tot_a + tot_b else None},
                'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
        _FW8S_RULES_CACHE['data'] = data
        _FW8S_RULES_CACHE['ts'] = now
        return data
    finally:
        conn.close()


def api_fw8s_history():
    """精選8s 過往入選紀錄（已結算 W/L/P）。"""
    conn = db()
    try:
        try:
            _fw8s_settle(conn)
        except sqlite3.OperationalError:
            pass  # 爬蟲鎖庫：今次略過，下次入嚟會再結算
        rows = conn.execute(
            'SELECT f.match_id, f.league, f.mode, f.conds, f.line, f.odds, '
            'f.added_at, f.result, f.settled_at, m.kickoff, m.home_score, '
            'm.away_score, ht.name_tc, at.name_tc '
            'FROM fw8s_log f JOIN matches m ON m.id=f.match_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'ORDER BY m.kickoff DESC LIMIT 200').fetchall()
        out = []
        for (mid, lg, mode, conds, line, odds, added, res, settled, ko,
             hs, aws, h, a) in rows:
            out.append({'id': mid, 'league': lg, 'mode': mode,
                        'conds': json.loads(conds) if conds else None,
                        'line': line, 'odds': odds, 'added_at': added,
                        'result': res, 'settled_at': settled,
                        'kickoff': ko,
                        'score': f'{hs}-{aws}' if hs is not None else None,
                        'home': h, 'away': a})
        n = len(out)
        w = sum(1 for r in out if r['result'] == 'W')
        l = sum(1 for r in out if r['result'] == 'L')
        p = sum(1 for r in out if r['result'] == 'P')
        eff = w + l
        by_mode = [dict(zip(('mode', 'n', 'w', 'l', 'p'), r)) for r in
                   conn.execute(
                       "SELECT mode, COUNT(*), SUM(result='W'), "
                       "SUM(result='L'), SUM(result='P') "
                       "FROM fw8s_log GROUP BY mode")]
        return {'ok': True, 'rows': out,
                'stats': {'n': n, 'w': w, 'l': l, 'p': p,
                          'hit_r': round(w / eff * 100, 1) if eff else None,
                          'by_mode': by_mode}}
    finally:
        conn.close()


_FW8S_BT_CACHE = {'ts': 0.0, 'data': None}


def api_fw8s_bt():
    """全庫 8 格共識實跑回測：每獨立聯賽/杯賽場次＋下盤命中率%。
    數據來自 fw8s_bt.db（fw8s_backtest.py 向量版產生，同 grid_report 口徑，
    抽樣對照一致率 99%）。上盤共識全庫 0 次係真實結果，唔係 bug。"""
    now = time.time()
    if _FW8S_BT_CACHE['data'] is not None \
            and now - _FW8S_BT_CACHE['ts'] < 3600:
        return _FW8S_BT_CACHE['data']
    bt_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)),
                           'fw8s_bt.db')
    if not os.path.exists(bt_path):
        return {'ok': False, 'error': '未找到 fw8s_bt.db——先喺本機跑 fw8s_backtest.py'}
    bt = sqlite3.connect('file:%s?mode=ro' % bt_path, uri=True, timeout=10)
    try:
        rows = bt.execute(
            "SELECT league, direction, COUNT(*), "
            "SUM(CASE WHEN res='A' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN res='B' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN res='P' THEN 1 ELSE 0 END) "
            "FROM fw8s_bt WHERE direction IS NOT NULL "
            "GROUP BY league, direction").fetchall()
        total_eval = bt.execute(
            'SELECT COUNT(*) FROM fw8s_bt').fetchone()[0]
        up_n = bt.execute(
            "SELECT COUNT(*) FROM fw8s_bt WHERE direction='up'").fetchone()[0]
    finally:
        bt.close()
    per = {}
    for lg, d, n, a, b, p in rows:
        e = per.setdefault(lg, {'up': None, 'down': None})
        eff = (a or 0) + (b or 0)
        win = (b or 0) if d == 'down' else (a or 0)
        e[d] = {'n': n, 'a': a or 0, 'b': b or 0, 'p': p or 0,
                'hit_r': round(win / eff, 4) if eff else None}
        # hit_r＝該方向「開出」比例：down → B 為贏（下盤開出）
    out = []
    for lg, e in per.items():
        out.append({'league': lg, **e})
    out.sort(key=lambda x: -((x['down'] or {'n': 0})['n']))
    tot_a = sum((x['down'] or {}).get('a', 0) for x in out)
    tot_b = sum((x['down'] or {}).get('b', 0) for x in out)
    tot_p = sum((x['down'] or {}).get('p', 0) for x in out)
    tot_n = sum((x['down'] or {}).get('n', 0) for x in out)
    eff = tot_a + tot_b
    data = {'ok': True, 'rows': out, 'total_eval': total_eval,
            'up_consensus_n': up_n,
            'total': {'n': tot_n, 'a': tot_a, 'b': tot_b, 'p': tot_p,
                      'down_r': round(tot_b / eff, 4) if eff else None},
            'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
    _FW8S_BT_CACHE['data'] = data
    _FW8S_BT_CACHE['ts'] = now
    return data


def api_oupred_panel():
    """規則說明＋回查命中%＋每聯賽獨立因子（n 最大嘅 15 個聯賽嘅盤口因子示例）。"""
    bt = _oupred_backtest()
    tab = _oupred_tables()['cur']
    lines = []
    for (lg, key), (up, n) in tab['line'].items():
        if n >= 50:
            lines.append({'league': lg, 'key': key, 'n': n,
                          'up_r': round(up / n * 100, 1)})
    lines.sort(key=lambda x: -x['n'])
    return {'rule': {
                'name': '同賽事盤口模型',
                'factors': [
                    {'name': '同賽事・聯賽獨立盤口因子', 'w': 45,
                     'desc': '該聯賽同讓球方＋同盤口線嘅歷史上盤率'
                             '（k=30 收縮→聯賽整體→全庫；每聯賽/杯賽獨立）'},
                    {'name': '同賽事・聯賽水位帶因子', 'w': 20,
                     'desc': '該聯賽同盤口＋上盤水位帶(±0.05)上盤率'
                             '（k=15 收縮→盤口因子）'},
                    {'name': '球隊近况因子', 'w': 35,
                     'desc': '主隊主場/全部＋客隊客場/全部近20場贏盤率'
                             '（match_derived，k=8 收縮→50%）'}],
                'formula': 'z = 0.45·logit(F1) + 0.20·logit(F2) + 0.35·logit(F3)，'
                           'P(上盤) = σ(z)；≥50% 指上盤，否則指下盤'},
            'backtest': bt,
            'league_factors': lines[:15]}


# ============ 精選系列每小時自動紀錄（用戶 2026-10-05） ============
# 無論有無人手撳掃描：伺服器每小時自動行 W/7/8/12 掃描，將「合資格或曾合資格」
# 嘅賽事寫入獨立表 feat_hourly（永久保留，唔影響大資料庫賽事紀錄）。
# 曾合資格但尾盤重算唔再合 → final_eligible=0，標記「最後不合資格」。
_feat_hourly = {'running': False, 'last': None, 'error': None,
                'stop': False, 'runs': 0}


def _fh_conn():
    """feat_hourly 專用連線：busy timeout 120 秒（用戶爬蟲/定時更新長事務搶鎖）"""
    return sqlite3.connect(DB_PATH, timeout=120)


def _feat_hourly_migrate(conn):
    """建 feat_hourly（冇就建）＋補新欄位。2026-10-10 加盤口賠率快照
    （亞盤+大小 closing 易勝博）——用戶要求精選系列每兩小時紀錄連盤口賠率。"""
    conn.execute('''CREATE TABLE IF NOT EXISTS feat_hourly(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, kind TEXT NOT NULL, match_id INTEGER NOT NULL,
        eligible INTEGER NOT NULL, direction TEXT, detail TEXT,
        kickoff TEXT, league TEXT, home TEXT, away TEXT,
        final_eligible INTEGER, result TEXT, settled INTEGER DEFAULT 0,
        ah_handicap REAL, ah_giver TEXT, ah_home REAL, ah_away REAL,
        ou_line REAL, ou_over REAL, ou_under REAL,
        UNIQUE(ts, kind, match_id))''')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(feat_hourly)')}
    for c, t in (('ah_handicap', 'REAL'), ('ah_giver', 'TEXT'),
                 ('ah_home', 'REAL'), ('ah_away', 'REAL'),
                 ('ou_line', 'REAL'), ('ou_over', 'REAL'),
                 ('ou_under', 'REAL')):
        if c not in cols:
            conn.execute('ALTER TABLE feat_hourly ADD COLUMN %s %s' % (c, t))
    # 2026-10-22：/api/feathourly 加咗 45 日窗口（kickoff 條件）——冇索引每個
    # kind 都全表掃 30 萬行；複合索引一勞永逸（建一次，秒級）
    conn.execute('CREATE INDEX IF NOT EXISTS idx_feat_hourly_kind_ko '
                 'ON feat_hourly(kind, kickoff)')


def _feat_hourly_log(conn, ts):
    """將「合資格或曾合資格」場次寫入 feat_hourly（核心記錄邏輯）。
    每條紀錄同時記低當時尾盤盤口賠率快照（closing 易勝博亞盤＋大小）。"""
    _feat_hourly_migrate(conn)
    kinds = (('W', 'v3_featured', None), ('7', 'fw_grid_featured', '7'),
             ('8', 'fw_grid_featured', '8'), ('12', 'fw_grid_featured', '12'))
    logged = 0
    for kind, table, g in kinds:
        if g:
            now_elig = dict(conn.execute(
                'SELECT match_id, direction FROM %s WHERE grid=?' % table,
                (g,)).fetchall())
        else:
            now_elig = dict(conn.execute(
                'SELECT match_id, direction FROM %s' % table).fetchall())
        prev = {r[0] for r in conn.execute(
            'SELECT DISTINCT match_id FROM feat_hourly '
            'WHERE kind=? AND eligible=1', (kind,))}
        union = set(now_elig) | prev
        if not union:
            continue
        mids = sorted(union)
        info = dict((r[0], r[1:]) for r in conn.execute(
            'SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc '
            'FROM matches m JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'WHERE m.id IN (%s)' % ','.join('?' * len(mids)), mids))
        # 尾盤盤口賠率快照（closing 易勝博亞盤＋大小）
        snap = {}
        ph = ','.join('?' * len(mids))
        for mid, hc, gv, ho, ao in conn.execute(
                "SELECT match_id, handicap, giver, home_odds, away_odds "
                "FROM odds_asian WHERE label='closing' AND company_id=12 "
                'AND match_id IN (%s)' % ph, mids):
            snap[mid] = [hc, gv, ho, ao, None, None, None]
        for mid, tl, ov, un in conn.execute(
                "SELECT match_id, total_line, over_odds, under_odds "
                "FROM odds_ou WHERE label='closing' AND company_id=12 "
                'AND match_id IN (%s)' % ph, mids):
            s = snap.setdefault(mid, [None, None, None, None,
                                      None, None, None])
            s[4], s[5], s[6] = tl, ov, un
        for mid in mids:
            ko, lg, h, a = info.get(mid, ('', '', '', ''))
            s = snap.get(mid, [None] * 7)
            conn.execute(
                'INSERT INTO feat_hourly(ts,kind,match_id,'
                'eligible,direction,kickoff,league,home,away,'
                'ah_handicap,ah_giver,ah_home,ah_away,'
                'ou_line,ou_over,ou_under) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(ts,kind,match_id) DO UPDATE SET '
                'eligible=excluded.eligible, direction=excluded.direction,'
                'ah_handicap=excluded.ah_handicap,'
                'ah_giver=excluded.ah_giver,ah_home=excluded.ah_home,'
                'ah_away=excluded.ah_away,ou_line=excluded.ou_line,'
                'ou_over=excluded.ou_over,ou_under=excluded.ou_under',
                (ts, kind, mid, 1 if mid in now_elig else 0,
                 now_elig.get(mid), ko, lg, h, a, *s))
            logged += 1
            if logged % 200 == 0:
                conn.commit()   # 分段提交：呢啲 INSERT 同結算共享一條交易，
                # 舊版鎖到成輪寫完先放（2026-10-08 獵手抓到嘅元兇之一）
    return logged


def _feat_hourly_settle(conn):
    """結算已完場未結算：賽果 W/L/P（用自己影低嘅 ah_handicap/ah_giver，
    NULL 先對 closing）＋尾盤重算最後資格。"""
    pend = conn.execute(
        'SELECT f.id, f.match_id, f.kind, f.direction, f.ah_handicap, '
        'f.ah_giver FROM feat_hourly f '
        'JOIN matches m ON m.id=f.match_id '
        'WHERE f.settled=0 AND m.home_score IS NOT NULL').fetchall()
    if not pend:
        return 0
    import v3_engine
    import fw_grid_engine
    v3_engine.load_v3_pool(conn)
    cache = {}
    for fid, mid, kind, d, shc, sgv in pend:
        r = conn.execute(
            "SELECT oc.handicap, oc.giver, m.home_score, m.away_score "
            "FROM matches m LEFT JOIN odds_asian oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 "
            'WHERE m.id=?', (mid,)).fetchone()
        res = None
        final_elig = None
        if r and r[2] is not None:
            hc, gv = _stored_first(shc, sgv, r[0], r[1])
            pr = pick_result(hc, gv, r[2], r[3])
            if pr == 'P':
                res = 'P'
            elif pr:
                res = 'W' if (pr == 'A') == (d == 'up') else 'L'
            if mid not in cache:
                t = screen_engine.get_target(conn, mid)
                if t:
                    if kind == 'W':
                        cache[mid] = bool(
                            v3_engine.featured_w(conn, t).get('pass'))
                    else:
                        cache[mid] = bool(fw_grid_engine.grid_report(
                            conn, t, kind).get('pass'))
                else:
                    cache[mid] = None
            final_elig = cache[mid]
        conn.execute(
            'UPDATE feat_hourly SET settled=1, result=?, '
            'final_eligible=? WHERE id=?', (res, final_elig, fid))
        # 每場即時提交：第一個 UPDATE 已開寫交易，舊版一路 commit 到
        # 成個循環尾——場次一多就鎖庫幾十分鐘（2026-10-08 獵手實證：
        # _feat_hourly_settle 係歷次寫鎖僵死元兇）
        conn.commit()
    return len(pend)


def _feat_hourly_tick():
    if _feat_hourly['running']:
        return
    _feat_hourly['running'] = True
    try:
        # 2026-10-12：每小時截斷 WAL。教訓：WAL 曾膨脹至 519MB（夜間崩潰殘留），
        # 令全部查詢慢到癱瘓、Heavy-GET 塞滿、app 假死（seizure）。截斷後即時復原。
        try:
            _c = db()
            _c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            _c.close()
        except Exception:
            pass
        ts = time.strftime('%Y-%m-%d %H:%M')
        # 1) 先記錄（保證一定有紀錄，掃描幾慢都唔會空窗）
        # 用戶爬蟲＋定時更新長事務成日鎖 DB：成段 retry 最多 20 次×8 秒
        for attempt in range(20):
            conn = None
            try:
                conn = _fh_conn()
                _feat_hourly_log(conn, ts)
                conn.commit()          # 記錄同結算分開交易，各鎖各放
                _feat_hourly_settle(conn)
                conn.commit()
                conn.close()
                break
            except sqlite3.OperationalError:
                if conn:
                    try:
                        conn.rollback()
                        conn.close()
                    except Exception:
                        pass
                if attempt == 19:
                    raise
                time.sleep(8)
        # 2) 行四個掃描更新合資格狀態（reuse 現有掃描工作；已 running 會自動跳過）
        try:
            _v3_featured_scan_job()
        except Exception:
            traceback.print_exc()
        for g in ('7', '8', '12'):
            try:
                _fw_grid_scan_job(g)
            except Exception:
                traceback.print_exc()
        # 3) 掃描完再記一次（今輪有新合資格場次即時入帳）＋結算
        for attempt in range(20):
            conn = None
            try:
                conn = _fh_conn()
                _feat_hourly_log(conn, ts)
                conn.commit()          # 記錄同結算分開交易，各鎖各放
                _feat_hourly_settle(conn)
                conn.commit()
                conn.close()
                break
            except sqlite3.OperationalError:
                if conn:
                    try:
                        conn.rollback()
                        conn.close()
                    except Exception:
                        pass
                if attempt == 19:
                    raise
                time.sleep(8)
        _feat_hourly['last'] = ts
        _feat_hourly['runs'] = _feat_hourly.get('runs', 0) + 1
        _feat_hourly['error'] = None
    except Exception as e:
        traceback.print_exc()
        _feat_hourly['error'] = str(e)
    finally:
        _feat_hourly['running'] = False


def _line_move_watch_loop():
    """2026-10-24 盤口變動即時重算（用戶：『加快到即時更新！點解要 5 分鐘』）：
    爬蟲一寫新盤（無論 APP 定 fleet 通道）就 append 場次去 _line_moves.txt；
    呢度每 20 秒讀一次，對每場即刻 _reval_submit（同 after_odds_update 同一套
    重檢：12 表資格剔走/新增＋推送＋快取清除）。工具表滯後由『最遲 20 分鐘
    （後備 5 分鐘）』→『≤20 秒』。5 分鐘統一循環降後備（漏網/排名變化兜底）。
    2026-10-25：600s/場節流已廢（用戶指示 100% 重算），此 docstring 同步更
    正；路徑根治見下方註釋。"""
    time.sleep(120)   # 等開機
    import os as _os
    # 2026-10-25 路徑根治（吹水審計第十輪捉到嘅假承諾管道）：crawler 嘅
    # notify_line_move 用 __file__ 相對路徑寫——邊份 crawler.py copy 被
    # import 就寫去邊（本機正本=倉庫根；雲端=/app）。舊制 watcher 淨讀
    # engine/_line_moves.txt，寫入同讀取永遠打唔到：root 檔積壓過百場，
    # APP 自動重抓/shenbo/crawl_mk 觸發嘅轉盤通知全部冇 20 秒重算，得 5
    # 分鐘後備兜住（對用戶「任何變動 100% 重算」承諾係假嘅）。而家全部
    # 可能寫入點行匀，邊個有貨讀邊個；冇貨先 fallback 舊 engine 路徑。
    fpath = None
    for _cand in (_os.path.join(PARENT_DIR, '_line_moves.txt'),
                  _os.path.join(BASE_DIR, '_line_moves.txt'),
                  _os.path.join(PARENT_DIR, 'engine', '_line_moves.txt'),
                  _os.path.join(BASE_DIR, 'engine', '_line_moves.txt')):
        if _os.path.exists(_cand):
            fpath = _cand
            break
    if fpath is None:
        fpath = _os.path.join(PARENT_DIR, '_line_moves.txt')
    while True:
        time.sleep(20)
        try:
            if not _os.path.exists(fpath):
                continue
            with open(fpath, 'r', encoding='ascii') as f:
                mids = [int(x) for x in f.read().split() if x.strip().isdigit()]
            if not mids:
                continue
            # 2026-10-25 修正（用戶：搵晒阻止重算嘅物件）：舊制「讀完即刻清空
            # + 淨做前 40」會將超額通知**靜默丟棄**——批量 149 場時逾百場永遠
            # 冇重算！而家：全做；單 tick 超過 300 先將剩餘寫返落檔下輪做，
            # 一個通知都唔准失。
            overflow = []
            if len(mids) > 300:
                overflow = mids[300:]
                mids = mids[:300]
            with open(fpath, 'w', encoding='ascii') as f:
                for _m in overflow:
                    f.write('%d\n' % _m)
            seen = set()
            for mid in mids:
                if mid in seen:
                    continue
                seen.add(mid)
                # 2026-10-25 用戶指示：**任何水位/盤口變動 100% 重算，冇節流**——
                # 10 分鐘內第二次變動代表賽事真係郁緊，係更精確嘅資訊，跳過係傻。
                # （舊制 600s/場節流同 after_odds_update 一併廢除；2 線池本身
                #  已經係並發上限，RAM 有底。）
                _reval_submit(mid)
        except Exception:
            pass


_AOD_LOG = os.path.join(PARENT_DIR, 'logs', 'auto_odds.log')


def _aod_log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(_AOD_LOG, 'a', encoding='utf-8') as _f:
            _f.write(line + '\n')
    except OSError:
        pass


def _auto_odds_refresh_loop():
    """2026-10-25 補上自動化最後缺口：即將開賽場次嘅讓球賠率**週期自動重抓**。
    舊制重抓幾乎全靠人手撳掣——撳完之後賠率擺到舊，5 分鐘統一掃描只能
    用舊線重評（變動跳過＝全部略過），「盤轉咗但冇人撳掣＝永遠唔重算唔
    出預測」嘅風險實際存在（用戶一語道中）。而家每 15 分鐘一輪：未來 24
    小時內、讓球賠率舊過 30 分鐘（或從未抓到）嘅場，順開賽序重抓最多 40
    場——crawl_odds_for_match 成功即 notify_line_move → watcher 20 秒內
    重算 12 工具＋好波。本機＋雲端都開（雲端行代理池，單場順序輕量）。
    連首飾都唔使撳：開賽前一日開始自動新鮮，資格自動浮現。"""
    time.sleep(300)
    import crawler as _cr
    while True:
        try:
            conn = db()
            try:
                rows = conn.execute(
                    "SELECT m.id, m.kickoff FROM matches m "
                    "WHERE m.home_score IS NULL "
                    "AND m.kickoff >= datetime('now','+8 hours','+20 minutes') "
                    "AND m.kickoff <= datetime('now','+8 hours','+24 hours') "
                    "AND COALESCE((SELECT MAX(a.fetched_at) FROM odds_asian a "
                    "  WHERE a.match_id=m.id AND a.company_id=12), '2000-01-01') "
                    # 2026-10-25 第七輪實彈捉 bug：fetched_at 存香港時間，必須
                    # '+8 hours' 先同庫內 UTC 比較——舊制差 8 個鐘，全部場永遠
                    # 「新鮮」，循環出世以嚟零爬行（心跳 0 場嘅真兇）
                    "  < datetime('now','+8 hours','-30 minutes') "
                    'ORDER BY m.kickoff LIMIT 40').fetchall()
            finally:
                conn.close()
            if rows:
                with open(os.path.join(_cr.BASE_DIR, 'config.json'),
                          encoding='utf-8') as _f:
                    cfg = json.load(_f)
                cfg['blocked_retry_times'] = 1   # 自動輪快閃失敗，下輪再補
                conn = sqlite3.connect(DB_PATH, timeout=180)
                try:
                    fetcher = _cr.Fetcher(conn, cfg)
                    n = 0
                    for mid, ko in rows:
                        try:
                            if _cr.crawl_odds_for_match(conn, fetcher, mid,
                                                        ko, 12):
                                n += 1
                            elif not conn.execute(
                                    'SELECT 1 FROM odds_asian WHERE match_id=? '
                                    'AND company_id=12', (mid,)).fetchone():
                                _cr.crawl_odds_for_match(conn, fetcher, mid,
                                                         ko, 3)
                        except Exception:
                            pass
                    _aod_log('自動重抓 %d/%d 場（24h 窗口，舊>30min）'
                             % (n, len(rows)))
                    if n:
                        _sync_push_now()   # 有實際更新→冷卻制即推上雲
                finally:
                    conn.close()
            else:
                # 2026-10-25 第五輪審計：每輪都留心跳（連 0 場都記）——冇 log
                # 分唔出「健康冇嘢做」定「循環死咗」，哨兵要有聲。
                # 第六輪修正：寫檔案（logs/auto_odds.log）——pythonw 冇 stdout，
                # print 心跳會好似 crawl_mk 咁「靜靜哋死咗都冇人知」。
                _aod_log('心跳：本輪 0 場需重抓（24h 窗全部新鮮）')
        except Exception:
            traceback.print_exc(limit=2)
        time.sleep(900)


_SYNC_PUSH_LAST = [0.0]


def _sync_push_now(force=False, cooldown=300):
    """2026-10-25 事件驅動同步：批量/regrab/窗口更新搞掂後**即刻推上雲**——
    舊制要等 30 分鐘循環，「更新完手機要等半粒鐘」正係呢個制嘅缺陷。force
    （用戶撳嘅批量/regrab）無視冷卻；自動觸發（窗口/auto-odds）5 分鐘冷卻
    防連環推送。雲端唔推（佢係接收方）。"""
    if DISABLE_UPDATE:
        return
    now = time.time()
    if not force and now - _SYNC_PUSH_LAST[0] < cooldown:
        return
    _SYNC_PUSH_LAST[0] = now

    def _bg():
        try:
            import subprocess
            subprocess.run(
                [sys.executable,
                 os.path.join(PARENT_DIR, 'sync_delta.py'), '--push'],
                timeout=600, capture_output=True)
            print('[sync-now] 事件驅動推送完成', flush=True)
            # 2026-10-25 代理池熱同步：最靚 400 個驗證代理推俾雲端（Fetcher
            # mtime 重載，手機爬行即提速）
            try:
                import urllib.request
                _gp = os.path.join(PARENT_DIR, 'logs', 'good_proxies.log')
                _lines = [l.strip() for l in open(_gp, encoding='ascii',
                                                  errors='ignore')
                          if l.strip().startswith('http')][-400:]
                if _lines:
                    _req = urllib.request.Request(
                        'https://football-analysis-uk0s.onrender.com'
                        '/api/admin/pool',
                        data=json.dumps(_lines).encode(),
                        headers={'X-Sync-Key': SYNC_KEY,
                                 'Content-Type': 'application/json'},
                        method='POST')
                    _r = urllib.request.urlopen(_req, timeout=60)
                    _line = '[%s] 代理池已推雲：%s' % (
                        time.strftime('%Y-%m-%d %H:%M:%S'),
                        _r.read().decode()[:60])
                    print(_line, flush=True)
                    # pythonw 冇 stdout——寫檔案先係硬證據
                    with open(os.path.join(PARENT_DIR, 'logs',
                                           'sync_delta.log'), 'a',
                              encoding='utf-8') as _lf:
                        _lf.write(_line + '\n')
            except Exception as _e:
                _eline = '[%s] 代理池推送失敗：%s' % (
                    time.strftime('%Y-%m-%d %H:%M:%S'), str(_e)[:80])
                print(_eline, flush=True)
                try:
                    with open(os.path.join(PARENT_DIR, 'logs',
                                           'sync_delta.log'), 'a',
                              encoding='utf-8') as _lf:
                        _lf.write(_eline + '\n')
                except OSError:
                    pass
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()


def _sync_delta_loop():
    """2026-10-24：熱數據增量同步（PC→雲端）改由 APP 自己每 30 分鐘推——
    原本靠 Windows 計劃任務 FA_SyncDelta30m，但佢間歇性失敗（LastTaskResult
    101）令手機數據停滯（呢單嘢：手機顯示舊盤 伊高斯 一球 vs 實際球半，
    就係 sync 停咗 2.5 小時）。APP 有 keepalive 睇住，可靠得多。雲端唔開
    （DISABLE_UPDATE）。"""
    time.sleep(300)   # 等開機穩定先推第一輪
    while True:
        try:
            import subprocess
            r = subprocess.run(
                [sys.executable,
                 os.path.join(PARENT_DIR, 'sync_delta.py'), '--push'],
                timeout=600, capture_output=True)
            if r.returncode != 0:
                print('[sync-delta] 推送失敗 rc=%s %s'
                      % (r.returncode, (r.stderr or b'')[:200]), flush=True)
        except Exception:
            traceback.print_exc(limit=2)
        time.sleep(1800)


def _result_settle_loop():
    """賽果回填+精選結算（2026-10-10 用戶規格）：開賽逾120分鐘冇比分→爬賽果；
    已完場嘅精選/7/8/12 紀錄補結算。每 15 分鐘一次；雲端爬唔到安全跳過（NO_DATA），
    結算照做（比分經每日推庫到達後自動補）。
    2026-10-22：subprocess 之後加 _settle_finished_tools_bg——子進程淨結算
    feat_hourly，其餘精選表（featured/v1/v3/格/fw8s/8X/88W/新精選/好波）
    由呢度主動全結算（用戶規格：完場後自動更新到全部工具結果）。"""
    time.sleep(300)   # 等啟動穩定先
    while True:
        try:
            import subprocess
            subprocess.run([sys.executable,
                            os.path.join(PARENT_DIR, '_result_settle.py'), '40'],
                           timeout=600, capture_output=True)
        except Exception:
            pass
        try:
            _settle_finished_tools_bg()
            _bump_data_epoch()   # 2026-10-26：本輪結算完，全部快取作廢
        except Exception:
            pass
        time.sleep(900)


def _feat_hourly_loop():
    time.sleep(180)   # 等伺服器起好、數據池熱身先
    while not _feat_hourly['stop']:
        try:
            _feat_hourly_tick()
        except Exception:
            traceback.print_exc()
        time.sleep(7200)   # 2026-10-10 用戶指定：每兩小時自動更新及紀錄


def get_feat_hourly():
    """每小時紀錄頁：逐 kind 統計＋逐場資格時間線。
    2026-10-22 修正「頁面等足兩分鐘都打唔開」：feat_hourly 已累積 32 萬行
    （每 2 小時快照），全表讀出砌 JSON 成 30MB+——改為只取近 45 日開賽場次
    （監控頁用途＝睇近期資格變化；舊場次結果喺過往紀錄／事後回查睇）。
    加埋每 kind 場數上限做雙保險，頁面回復秒開。"""
    conn = db()
    _feat_hourly_migrate(conn)
    cats = dict(conn.execute('SELECT req_name, category FROM competitions'))
    kinds = ('W', '7', '8', '12')
    out = {}
    for kind in kinds:
        rows = conn.execute(
            'SELECT f.match_id, f.kickoff, f.league, f.home, f.away, '
            'f.direction, f.eligible, f.ts, f.result, f.final_eligible, '
            'f.settled, f.ah_handicap, f.ah_giver, f.ah_home, f.ah_away, '
            'f.ou_line, f.ou_over, f.ou_under '
            'FROM feat_hourly f WHERE f.kind=? '
            "AND f.kickoff >= datetime('now','+8 hours','-45 days') "
            'ORDER BY f.match_id, f.ts', (kind,)).fetchall()
        matches = {}
        for (mid, ko, lg, h, a, d, elig, ts, res, fin, st,
             ahc, agv, aho, aaw, oul, ouo, ouu) in rows:
            M = matches.setdefault(mid, {'id': mid, 'kickoff': ko,
                                         'league': lg, 'home': h, 'away': a,
                                         'direction': d, 'hist': []})
            M['hist'].append({'ts': ts, 'elig': elig})
            # 最新非空盤口賠率快照（每兩小時紀錄會逐次更新覆寫）
            if ahc is not None or oul is not None:
                M['snap'] = {'ahc': ahc, 'agv': agv, 'aho': aho, 'aaw': aaw,
                             'oul': oul, 'ouo': ouo, 'ouu': ouu}
            if res is not None:
                M['result'] = res
            if fin is not None:
                M['final_eligible'] = fin
            M['settled'] = st
        recs = list(matches.values())
        for M in recs:
            M['ever'] = any(x['elig'] for x in M['hist'])
            M['now_elig'] = bool(M['hist'] and M['hist'][-1]['elig'])
            M['snaps'] = len(M['hist'])
            M['cat'] = cats.get(M['league'], '')
        # 雙保險：每 kind 最多留 1500 場（最新開賽排先），頁面同回應包都 bounded
        recs.sort(key=lambda M: M['kickoff'], reverse=True)
        recs = recs[:1500]
        played = [M for M in recs if M.get('result')]
        wins = sum(1 for M in played if M['result'] == 'W')
        losses = sum(1 for M in played if M['result'] == 'L')
        pushes = sum(1 for M in played if M['result'] == 'P')
        dropped = [M for M in recs if M.get('final_eligible') == 0]
        out[kind] = {
            'name': '精選W' if kind == 'W' else '精選' + kind,
            'window_days': 45,
            'stats': {'tracked': len(recs),
                      'now_eligible': sum(1 for M in recs if M['now_elig']),
                      'ever_eligible': sum(1 for M in recs if M['ever']),
                      'dropped_final': len(dropped),
                      'played': len(played), 'wins': wins, 'losses': losses,
                      'pushes': pushes,
                      'hit_r': round(wins / (wins + losses) * 100, 1)
                      if (wins + losses) else None},
            'matches': recs}
    st = {k: _feat_hourly.get(k) for k in ('running', 'last', 'error', 'runs')}
    conn.close()
    return {'kinds': out, 'scan': st}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        # 2026-10-26：快取分派器用——捕獲首個回應（code+body），之後照常送出
        if getattr(self, '_get_capture', None) is not None and                 isinstance(self._get_capture, list):
            self._get_capture.append((code, body))
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass   # 客戶端中途斷線（10053）——正常現象，唔再刷屏 stderr

    def do_GET(self):
        u = urlparse(self.path)
        # 2026-10-05：路由比對統一去尾斜線——APK／書籤用 /haobao/（trailing
        # slash）會跌穿去 404，就算新映像部署成功好波頁都開唔到。
        # 直接改寫 u.path，後面成條 dispatch 鏈自動用 stripped 版
        if u.path != '/' and u.path.endswith('/'):
            u = u._replace(path=u.path.rstrip('/'))
        # 重型 GET 並發閘（2026-10-06 記憶體審計）：超額請求排隊，排唔到回 429。
        # 2026-10-15：入閘前再過 RSS 軟上限——記憶體緊絀時直接卸載，
        # 阻止在途請求疊加峰值撞爆 512MB（OOM 殺容器 = 全體 502 十幾分鐘）。
        if u.path.startswith(_HEAVY_GET_PREFIXES):
            if _MEM_GUARD and _rss_mb() > _MEM_SOFT_MB:
                self._send(429, json.dumps(
                    {'error': '系統繁忙，請 30 秒後重試'}, ensure_ascii=False))
                return
            if not _HEAVY_GET_SEM.acquire(timeout=_HEAVY_GET_TIMEOUT):
                self._send(429, json.dumps(
                    {'error': '系統繁忙，請 30 秒後重試'}, ensure_ascii=False))
                return
            try:
                self._do_GET(u)
            finally:
                _HEAVY_GET_SEM.release()
        else:
            self._do_GET(u)

    def _do_GET(self, u):
        # 2026-10-26：白名單重型 GET 先查快取；miss 行真身並捕獲 200 回應
        # （_send 攔截）寫入。非 200（例如 500）一律唔快取。
        if u.path in _GET_CACHE_ROUTES:
            key = u.path + '?' + u.query
            _tag = _GET_CACHE_ROUTES[u.path]
            ep = _GET_CACHE['epochs'].get(_tag, 0)
            try:
                with _GET_CACHE['lock']:
                    ent = _GET_CACHE['data'].get(key)
                if ent and ent[0] == _tag and ent[1] == ep and                         time.time() - ent[2] < _GET_CACHE_TTL:
                    self._send(200, ent[3])
                    return
            except Exception:
                ent = None
            self._get_capture = None
            try:
                self._get_capture = []
                self._do_GET_inner(u)
            finally:
                cap = self._get_capture
                self._get_capture = None
                if cap and cap[0][0] == 200:
                    try:
                        with _GET_CACHE['lock']:
                            if len(_GET_CACHE['data']) >= _GET_CACHE_MAX:
                                _GET_CACHE['data'].clear()
                            _GET_CACHE['data'][key] = (_tag, ep, time.time(), cap[0][1])
                    except Exception:
                        pass
            return
        self._do_GET_inner(u)

    def _do_GET_inner(self, u):
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
        if u.path == '/api/crawl/state':
            self._send(200, json.dumps(api_crawl_state(), ensure_ascii=False))
            return
        if u.path == '/api/crawl/relay':
            self._send(200, json.dumps(api_crawl_relay_fetch(),
                                       ensure_ascii=False))
            return
        if u.path == '/api/probe':
            try:
                self._send(200, json.dumps(api_probe_hosts(),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/export/odds':
            q = parse_qs(u.query)
            after = int((q.get('after_rowid') or ['0'])[0] or 0)
            limit = int((q.get('limit') or ['5000'])[0] or 5000)
            try:
                self._send(200, json.dumps(api_export_odds(after, limit),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/ready':
            self._send(200, json.dumps({
                'ready': _pool_ready['done'], 'version': SERVER_VERSION,
                'started': _started, 'error': _pool_ready['err']},
                ensure_ascii=False))
            return
        if u.path == '/api/health':
            try:
                self._send(200, json.dumps(api_health(), ensure_ascii=False,
                                           default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/haobao':
            with open(os.path.join(STATIC_DIR, 'hb.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
            return
        if u.path == '/api/hb/list':
            try:
                q = parse_qs(u.query)
                hours = int(q.get('hours', ['72'])[0])
                self._send(200, json.dumps(api_hb_list(hours), ensure_ascii=False,
                                           default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/hb/predict':
            try:
                q = parse_qs(u.query)
                mid = int(q.get('id', ['0'])[0])
                self._send(200, json.dumps(api_hb_predict(mid), ensure_ascii=False,
                                           default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/hb/picks':
            # 2026-10-22 好波高機會精選專區（規則命中嘅未開賽場次）
            try:
                self._send(200, json.dumps(api_hb_picks(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/oupred/panel':
            try:
                self._send(200, json.dumps(api_oupred_panel(), ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/oupred/history':
            try:
                self._send(200, json.dumps(_oupred_history(), ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/twin':
            # 👯 搵孖生：全庫／同聯賽 搵歷史上六項盤口完全相同嘅完場場次
            try:
                q = parse_qs(u.query)
                mid = int(q.get('id', ['0'])[0])
                now = time.time()
                hit = _twin_cache.get(mid)
                if hit and now - hit[0] < _twin_cache_ttl:
                    self._send(200, hit[1])
                    return
                body = json.dumps(api_twin(mid), ensure_ascii=False)
                _twin_cache[mid] = (now, body)
                self._send(200, body)
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/fw8s':
            with open(os.path.join(STATIC_DIR, 'fw8s.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
            return
        if u.path == '/api/fw8s/full':
            # 精選8s：未來場次上盤入選＋觀望＋每聯賽統計（10分鐘快取）
            try:
                q = parse_qs(u.query)
                hours = int(q.get('hours', ['72'])[0])
                force = q.get('force', ['0'])[0] == '1'
                self._send(200, json.dumps(api_fw8s_full(hours, force),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fw8s/rules':
            # 精選8s：每聯賽公式＋逐年分段回查
            try:
                self._send(200, json.dumps(api_fw8s_rules(), ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/88w':
            with open(os.path.join(STATIC_DIR, 'w88.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
            return
        if u.path == '/api/w88/full':
            # 88W：三市場逐聯賽規則庫＋K3雙數和波預測＋統計
            if not w88_engine:
                self._send(500, json.dumps({'error': 'w88_engine 模組未載入'},
                                           ensure_ascii=False))
                return
            try:
                self._send(200, json.dumps(w88_engine.full(db()),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/newjx':
            with open(os.path.join(STATIC_DIR, 'newjx.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
            return
        if u.path == '/api/newjx/full':
            # 新精選：16欄位全組合挖掘嘅逐聯賽上下盤最高命中方法庫
            if not newjx_engine:
                self._send(500, json.dumps({'error': 'newjx_engine 模組未載入'},
                                           ensure_ascii=False))
                return
            try:
                self._send(200, json.dumps(newjx_engine.full(db()),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fw8s/history':
            # 精選8s：過往入選紀錄（自動結算）
            try:
                self._send(200, json.dumps(api_fw8s_history(), ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fw8s/bt':
            # 精選8s：全庫 8 格共識實跑回測（fw8s_bt.db，每聯賽場次+%）
            try:
                self._send(200, json.dumps(api_fw8s_bt(), ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/feathourly':
            try:
                self._send(200, json.dumps(get_feat_hourly(), ensure_ascii=False))
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/worklog':
            try:
                q = parse_qs(u.query)
                limit = int(q.get('limit', ['30'])[0])
                self._send(200, json.dumps(api_worklog(limit), ensure_ascii=False,
                                           default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/lg_hit_rates':
            self._send(200, json.dumps(api_lg_hit_rates(), ensure_ascii=False))
            return
        # 2026-10-25 分聯賽實測報表（全部工具頁頂部共用；即時 SQL 冇快取）
        if u.path == '/api/league_report':
            q = parse_qs(u.query)
            self._send(200, json.dumps(
                api_league_report(q.get('tool', [''])[0]), ensure_ascii=False))
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
        if u.path == '/api/htft':
            try:
                self._send(200, json.dumps(api_htft(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/htftboard':
            try:
                _q = parse_qs(u.query)
                self._send(200, json.dumps(api_htftboard(
                    _q.get('fin_hours', [None])[0] or None,
                    _q.get('fin_cat', [None])[0] or None),
                    ensure_ascii=False))
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
        if u.path == '/api/fetch-batch-status':
            self._send(200, json.dumps(fbatch_status(), ensure_ascii=False))
            return
        if u.path == '/api/events':
            # 2026-10-25 即時推送（SSE）——長連線，行完先 return
            try:
                api_events(self)
            except Exception:
                pass
            return
        if u.path == '/api/scan-status':
            # 2026-10-22 通用掃描狀態（newjx／w88 背景作業真實進度）：
            # ?kind=newjx|w88 → {running, added, settled, error, started_at}
            try:
                q = parse_qs(u.query)
                kind = str(q.get('kind', [''])[0])
                if kind not in ('newjx', 'w88'):
                    self._send(400, json.dumps(
                        {'error': 'kind 只可以係 newjx / w88'}, ensure_ascii=False))
                    return
                self._send(200, json.dumps(_bg_scan_public(kind),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/scanlog':
            # 2026-10-23 篩選進行中 Log（用戶：加/減場次要見到做緊乜）
            # ?after=N 增量拉取（面板每 3 秒 poll 一次）
            try:
                q = parse_qs(u.query)
                after = int(q.get('after', ['0'])[0] or 0)
                lim = int(q.get('limit', ['60'])[0] or 60)
                self._send(200, json.dumps(
                    api_scanlog(max(0, after), max(1, min(lim, 120))),
                    ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/removed/full':
            # 2026-10-24 剔走專頁：各工具統計＋逐場清單
            try:
                q = parse_qs(u.query)
                lim = int(q.get('limit', ['300'])[0] or 300)
                self._send(200, json.dumps(
                    api_removed_full(lim), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/alerts':
            # 2026-10-22 工具入選/剔走推送：?unread=1 淨未讀；?limit=N
            try:
                q = parse_qs(u.query)
                lim = int(q.get('limit', ['50'])[0] or 50)
                unread = q.get('unread', ['0'])[0] in ('1', 'true')
                self._send(200, json.dumps(
                    api_alerts(max(1, min(lim, 200)), unread),
                    ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/regrab-recompute':
            # 🔄 重抓重算（2026-10-21）：GET 輪詢進度
            try:
                self._send(200, json.dumps(regrab_status(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
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
            # 2026-10-25 附加掃描歸因＋排隊（前端進度條顯示「邊個觸發」用）；
            # _feat_scan 原欄位不動，附加 key 向後兼容
            _st = dict(_feat_scan)
            _st['triggered_by'] = _unified_scan.get('triggered_by')
            _st['queue'] = [q['tool'] for q in _SCAN_QUEUE]
            _st['unified_running'] = _unified_scan['running']
            _st['unified_done'] = _unified_scan['done']
            _st['unified_total'] = _unified_scan['total']
            self._send(200, json.dumps(_st, ensure_ascii=False))
            return
        if u.path == '/api/v1/featured/full':
            try:
                qs = parse_qs(u.query)
                z = qs.get('z', [''])[0] in ('1', 'true')
                self._send(200, json.dumps(get_v1_featured_full(z=z), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v1/featured/scan-status':
            _st = dict(_v1_scan)
            _st.update(_scan_meta_extra())
            self._send(200, json.dumps(_st, ensure_ascii=False))
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
        if u.path == '/api/v3/list':
            try:
                q = parse_qs(u.query)
                hours = int(q.get('hours', ['48'])[0])
                fin_hours = int(q.get('fin_hours', ['120'])[0])
                fin_cat = q.get('fin_cat', ['all'])[0]
                if fin_cat not in ('all', '聯賽', '杯賽'):
                    fin_cat = 'all'
                self._send(200, json.dumps(
                    api_v3_list(hours, fin_hours, fin_cat), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v3/target':
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                self._send(200, json.dumps(api_v3_target(mid), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/match/tools':
            # 2026-10-25 詳情頁「工具預測」：各工具命中情況＋逐篩選法命中率
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                self._send(200, json.dumps(api_match_tools(mid),
                                           ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v3/summary':
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                self._send(200, json.dumps(api_v3_summary(mid), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v3/item':
            try:
                qs = parse_qs(u.query)
                mid = int(qs.get('id', ['0'])[0])
                no = qs.get('no', [''])[0]
                self._send(200, json.dumps(api_v3_item(mid, no), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v3/featured/full':
            try:
                self._send(200, json.dumps(get_v3_featured_full(), ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v3/featured/scan-status':
            self._send(200, json.dumps(_v3_scan, ensure_ascii=False))
            return
        if u.path == '/api/v3/featlog':
            try:
                self._send(200, json.dumps(get_v3_featlog(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fw12/audit':
            try:
                self._send(200, json.dumps(get_fw12_audit(),
                                           ensure_ascii=False,
                                           default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/8x/list':
            try:
                if not fw8x:
                    self._send(500, json.dumps({'error': 'fw8x 模組未載入'},
                                               ensure_ascii=False))
                    return
                st = fw8x.stats_and_list(db())
                # 2026 全年回測摘要（fw8x_backtest_2026.py 產生；冇檔案就略過）。
                # 路徑兩個候選：本機 fw8x.py 喺 screen_app/（JSON 喺上一層），
                # 雲端 fw8x.py 喺 repo 根（JSON 同層）。
                try:
                    import os as _os
                    _base = _os.path.dirname(_os.path.abspath(fw8x.__file__))
                    for _btp in (_os.path.join(_base, 'fw8x_bt2026_summary.json'),
                                 _os.path.join(_base, '..', 'fw8x_bt2026_summary.json')):
                        _btp = _os.path.abspath(_btp)
                        if _os.path.isfile(_btp):
                            st['bt'] = json.load(open(_btp, encoding='utf-8'))
                            break
                except Exception:
                    pass
                self._send(200, json.dumps(st, ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/8x/new':
            try:
                if not fw8x:
                    self._send(500, json.dumps({'error': 'fw8x 模組未載入'},
                                               ensure_ascii=False))
                    return
                q = parse_qs(u.query)
                since = float(q.get('since', ['0'])[0])
                self._send(200, json.dumps(fw8x.newsince(db(), since),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fwgrid/full':
            try:
                q = parse_qs(u.query)
                g = q.get('g', ['7'])[0]
                if g not in ('7', '8', '12', '8s'):
                    self._send(400, json.dumps({'error': 'g 只可以係 7/8/12/8s'}))
                    return
                self._send(200, json.dumps(get_fw_grid_full(g), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fwgrid/scan-status':
            q = parse_qs(u.query)
            g = q.get('g', ['7'])[0]
            self._send(200, json.dumps(_fw_grid_scan.get(g, {}), ensure_ascii=False))
            return
        if u.path == '/api/fwcheck/full':
            try:
                q = parse_qs(u.query)
                g = q.get('g', ['7'])[0]
                if g not in ('7', '8', '12'):
                    self._send(400, json.dumps({'error': 'g 只可以係 7/8/12'}))
                    return
                self._send(200, json.dumps(get_fwcheck_full(g), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/fwcheck/target':
            try:
                q = parse_qs(u.query)
                g = q.get('g', ['7'])[0]
                mid = int(q.get('id', ['0'])[0])
                if g not in ('7', '8', '12'):
                    self._send(400, json.dumps({'error': 'g 只可以係 7/8/12'}))
                    return
                self._send(200, json.dumps(api_fwcheck_target(mid, g),
                                           ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/lgpred':
            try:
                self._send(200, json.dumps(api_lgpred(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/hitrate':
            try:
                q = parse_qs(u.query)
                kind = (q.get('kind') or [''])[0]
                mid = int((q.get('id') or ['0'])[0] or 0)
                self._send(200, json.dumps(api_hitrate(kind, mid),
                                           ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/cond_hr':
            try:
                _out = None
                try:
                    _conn = db()
                    try:
                        if _conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='cond_hr'").fetchone():
                            _out = {}
                            for _r in _conn.execute(
                                    'SELECT engine,cond,market,n,w,l,p '
                                    'FROM cond_hr'):
                                _out.setdefault(_r[0], {}) \
                                    .setdefault(_r[1], {})[_r[2]] = \
                                    [_r[3], _r[4], _r[5], _r[6]]
                    finally:
                        _conn.close()
                except Exception:
                    _out = None
                if not _out:
                    _snap = os.path.join(STATIC_DIR, 'cond_hr.json')
                    if os.path.isfile(_snap):
                        _out = json.load(open(_snap, encoding='utf-8'))
                self._send(200, json.dumps(_out or {},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/lgalerts':
            try:
                q = parse_qs(u.query)
                mark = (q.get('mark') or [''])[0] in ('1', 'true')
                self._send(200, json.dumps(get_lg_alerts(mark=mark),
                                           ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/ou_rules':
            try:
                self._send(200, json.dumps(get_ou_rules_full(),
                                           ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/ou_rules/scan-status':
            self._send(200, json.dumps(_ou_rules_scan, ensure_ascii=False))
            return
        if u.path == '/api/htft_rules':
            try:
                self._send(200, json.dumps(get_htft_rules_full(),
                                           ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/htft_rules/scan-status':
            self._send(200, json.dumps(_htft_rules_scan, ensure_ascii=False))
            return
        if u.path == '/api/predstats':
            try:
                qs = parse_qs(u.query)
                days = int(qs.get('days', ['30'])[0])
                lg = qs.get('lg', [''])[0]
                self._send(200, json.dumps(get_predstats(days, lg),
                                           ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        self._send(404, '{}')

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != '/' and u.path.endswith('/'):
            u = u._replace(path=u.path.rstrip('/'))
        if u.path == '/api/admin/pool':
            # 2026-10-25 代理池熱同步：本機連 sync 推埋最新「生產驗證過」代理
            # 俾雲端——雲端映像焗入嘅池係舊貨（10-24），手機爬得慢嘅元兇。
            # Fetcher 睇 mtime 自動重載，落地即生效。
            try:
                if self.headers.get('X-Sync-Key') != SYNC_KEY:
                    self._send(403, json.dumps({'ok': False, 'error': 'key'},
                                               ensure_ascii=False))
                    return
                n = int(self.headers.get('Content-Length', 0))
                pool = json.loads(self.rfile.read(n) or b'[]')
                import crawler as _cr
                with open(os.path.join(_cr.BASE_DIR, 'proxy_pool.json'),
                          'w', encoding='utf-8') as _f:
                    json.dump({'proxies': pool}, _f)
                print('[pool] 代理池已更新：%d 個' % len(pool), flush=True)
                self._send(200, json.dumps({'ok': True, 'n': len(pool)},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/admin/ingest':
            # 熱數據增量同步入口（sync_delta.py 每 30 分鐘推一次）
            # 收 gzipped sqlite，內含同 schema 嘅熱表，全部 INSERT OR REPLACE
            try:
                if self.headers.get('X-Sync-Key') != SYNC_KEY:
                    self._send(403, json.dumps({'error': 'forbidden'},
                                               ensure_ascii=False))
                    return
                # 2026-10-26 generation 計數（用戶批准）：防舊批次遲到覆蓋新
                # 數據（本機重啟/雙通道競速嘅 ABA 問題）。注意：校驗一定要
                # 喺讀完 request body 之後先至做——否則拒絕時 body 未讀完，
                # server 提早回應會 cut 線（10053），客戶端收唔到 409。
                _gen = None
                _dup = False
                _hash_hdr = self.headers.get('X-Sync-Hash') or ''
                if self.headers.get('X-Sync-Gen') is not None:
                    try:
                        _gen = int(self.headers.get('X-Sync-Gen'))
                    except (TypeError, ValueError):
                        _gen = None
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 100 * 1024 * 1024:
                    self._send(400, json.dumps({'error': 'bad size'},
                                               ensure_ascii=False))
                    return
                import gzip as _gz
                _body = self.rfile.read(n)
                raw = _gz.decompress(_body)
                # generation 校驗（body 讀完先驗——上面註釋講咗原因）
                if _gen is not None:
                    _mconn = db()
                    try:
                        _mconn.execute('CREATE TABLE IF NOT EXISTS sync_meta('
                                       'k TEXT PRIMARY KEY, v TEXT)')
                        _r1 = _mconn.execute(
                            "SELECT v FROM sync_meta WHERE k='last_gen'"
                        ).fetchone()
                        _last = int(_r1[0]) if _r1 else 0
                        _r2 = _mconn.execute(
                            "SELECT v FROM sync_meta WHERE k='last_hash'"
                        ).fetchone()
                        _lhash = _r2[0] if _r2 else ''
                    finally:
                        _mconn.close()
                    if _gen < _last or (_gen == _last
                                        and _hash_hdr != _lhash):
                        self._send(409, json.dumps(
                            {'ok': False, 'stale': True,
                             'last_gen': _last}, ensure_ascii=False))
                        return
                    _dup = (_gen == _last and _hash_hdr == _lhash)
                tmp = os.path.join(PARENT_DIR, '_ingest_tmp.sqlite')
                try:
                    with open(tmp, 'wb') as f:
                        f.write(raw)
                    rep = _apply_delta(tmp)
                    # generation 計數：應用成功先至 commit 號碼（崩潰喺中間
                    # 都安全——下一個更大 gen 照受理；冇上到香港嘅同 gen
                    # 重試會行冪等分支）
                    if _gen is not None and not _dup:
                        _mconn = db()
                        try:
                            _mconn.execute('PRAGMA busy_timeout=120000')
                        except Exception:
                            pass
                        try:
                            _mconn.execute('CREATE TABLE IF NOT EXISTS '
                                           'sync_meta(k TEXT PRIMARY KEY, '
                                           'v TEXT)')
                            _mconn.execute(
                                "INSERT OR REPLACE INTO sync_meta(k,v) VALUES"
                                "('last_gen',?), ('last_hash',?)",
                                (str(_gen), _hash_hdr))
                            _mconn.commit()
                        finally:
                            _mconn.close()
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if _dup:
                    self._send(200, json.dumps(
                        {'ok': True, 'duplicate': True, 'gen': _gen},
                        ensure_ascii=False))
                else:
                    try:
                        rep['gen'] = _gen
                    except Exception:
                        pass
                    self._send(200, json.dumps(rep, ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/8x/scan':
            try:
                if not fw8x:
                    self._send(500, json.dumps({'error': 'fw8x 模組未載入'},
                                               ensure_ascii=False))
                    return
                c = db()
                a = fw8x.scan(c)
                s = fw8x.settle(c)
                c.close()
                _scan_extras()   # 2026-10-22：順手爬進行中賽果＋主動全表結算
                self._send(200, json.dumps({'added': a, 'settled': s},
                                           ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/w88/scan':
            # 88W 一鍵掃描預測（2026-10-19）：K3 雙數和波＋8X 上/下盤公式
            # 一次過 scan＋settle，三個市場全包
            try:
                if not w88_engine:
                    self._send(500, json.dumps({'error': 'w88_engine 模組未載入'},
                                               ensure_ascii=False))
                    return
                started = _start_unified_scan(tool='88W掃描掣')
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps(
                    {'running': True, 'started': started.get('started')},
                    ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/newjx/scan':
            # 新精選一鍵掃描預測（2026-10-19）：逐聯賽上下盤方法 scan＋settle
            try:
                if not newjx_engine:
                    self._send(500, json.dumps({'error': 'newjx_engine 模組未載入'},
                                               ensure_ascii=False))
                    return
                started = _start_unified_scan(tool='新精選掃描掣')
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps(
                    {'running': True, 'started': started.get('started')},
                    ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/repair':
            # 2026-10-25 「🔧 修復」掣：清快取＋僵屍歸零＋主機快測
            try:
                self._send(200, json.dumps(api_repair(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/crawl/stop':
            try:
                self._send(200, json.dumps(do_crawl_stop(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/crawl/run':
            try:
                self._send(200, json.dumps(do_crawl_run(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/crawl/relay-state':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(api_crawl_relay_state(body),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
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
        if u.path == '/api/fetch-batch':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_fetch_batch(int(body.get('hours', 0) or 0),
                                   ids=body.get('ids') or None),
                    ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/regrab-recompute':
            # 🔄 重抓重算（2026-10-21）：POST 開始 / GET 輪詢進度
            try:
                if self.command == 'POST':
                    n = int(self.headers.get('Content-Length', 0))
                    if n:
                        self.rfile.read(n)
                    self._send(200, json.dumps(
                        do_regrab_recompute(), ensure_ascii=False))
                else:
                    self._send(200, json.dumps(
                        regrab_status(), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/phone-odds':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(do_phone_odds(body), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/phone-odds-debug':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(do_phone_odds_debug(body), ensure_ascii=False))
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
                # 2026-10-25：行緊都照樣 call——入面會排隊（舊制喺度靜默跳過）
                # full=1（2026-10-26 用戶規格「真跑全跑」）：跳過變動簽名，
                # 所有未賽場次逐場重評 12 系統。mids=[...]：指定場次清单
                # （用戶規格：重跑資料庫所有賽事——已賽場次都逐場重評，純讀取
                # 重算唔會改賽果；歷史紀錄 idempotent 補齊）。
                _mids = body.get('mids')
                _st = _start_unified_scan(
                    full=bool(body.get('full')),
                    mids=[int(x) for x in _mids] if _mids else None,
                    tool=body.get('tool') or 'V2/精選W掃描掣')
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps(
                    {'started': _st.get('started', False),
                     'running': True,
                     'reset': bool(body.get('reset'))},
                    ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/check/scan':
            try:
                if not _check_scan['running']:
                    _scan_thread(_check_scan_job)
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps({'started': True, 'running': True},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v1/featured/scan':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                if body.get('reset'):
                    conn = db()
                    conn.execute('DELETE FROM v1_featured')
                    conn.execute('DELETE FROM v1_featured_z')
                    conn.commit()
                    conn.close()
                # 2026-10-25：行緊都照樣 call——入面會排隊
                _st = _start_unified_scan(tool=body.get('tool') or 'V1掃描掣')
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps(
                    {'started': _st.get('started', False),
                     'running': True,
                     'reset': bool(body.get('reset'))},
                    ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v1/featured/refresh':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_v1_featured_refresh(int(body.get('id'))), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/alerts/read':
            # 2026-10-22 推送全部標記已讀（撳過鐘仔面板就清）
            try:
                self._send(200, json.dumps(api_alerts_read(),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
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
        if u.path == '/api/fwgrid/scan':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            g = str(body.get('g') or '7')
            if g not in ('7', '8', '12', '8s'):
                self._send(400, json.dumps({'ok': False, 'error': 'g 只可以係 7/8/12/8s'}))
                return
            try:
                # 2026-10-25：行緊都照樣 call——入面會排隊
                _start_unified_scan(tool='格%s掃描掣' % g)
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps({'ok': True}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/fwgrid/refresh':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            g = str(body.get('g') or '7')
            try:
                self._send(200, json.dumps(
                    do_fw_grid_refresh(int(body.get('id')), g), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/ou_rules/scan':
            try:
                if not _ou_rules_scan['running']:
                    _scan_thread(_ou_rules_scan_job)
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps({'ok': True}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/htft_rules/scan':
            try:
                if not _htft_rules_scan['running']:
                    _scan_thread(_htft_rules_scan_job)
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps({'ok': True}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
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
        if u.path == '/api/v3/combo':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    api_v3_combo(int(body.get('id')), body.get('sel') or []),
                    ensure_ascii=False, default=_jdefault))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)},
                                           ensure_ascii=False))
            return
        if u.path == '/api/v3/featured/scan':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                if body.get('reset'):
                    conn = db()
                    conn.execute('DELETE FROM v3_featured WHERE match_id IN '
                                 '(SELECT id FROM matches WHERE home_score IS NULL)')
                    conn.commit()
                    conn.close()
                # 2026-10-25：行緊都照樣 call——入面會排隊
                _start_unified_scan(tool=body.get('tool') or '精選W掃描掣')
                _scan_extras()   # 2026-10-22：爬進行中賽果＋主動全表結算
                self._send(200, json.dumps({'started': True, 'running': True,
                                            'reset': bool(body.get('reset'))},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        if u.path == '/api/v3/featured/refresh':
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                self._send(200, json.dumps(
                    do_v3_featured_refresh(int(body.get('id'))), ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(500, json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
            return
        self._send(404, '{}')


def _install_seizure_hunter():
    """寫鎖 seizure 獵手（2026-10-08）：每 60 秒試搶寫鎖；搶唔到即 dump
    全部線程棧去 logs/seizure_stacks.txt——三宗「殺 APP 即解鎖」事件都搵唔到
    邊句 SQL 持有交易，呢個直接俾案發現場。發現後可拆走。"""
    def hunt():
        import sys
        last_dump = 0.0
        while True:
            time.sleep(60)
            conn = None
            try:
                conn = sqlite3.connect(DB_PATH, timeout=3)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute('ROLLBACK')
                    conn.close()
                    conn = None
                    continue
                except sqlite3.OperationalError:
                    pass
                if time.time() - last_dump < 240:
                    continue
                last_dump = time.time()
                frames = sys._current_frames()
                out = []
                for tid, fr in frames.items():
                    out.append('===== thread %d =====' % tid)
                    out.extend(x.rstrip() for x in traceback.format_stack(fr))
                path = os.path.join(BASE_DIR, 'logs',
                                    'seizure_stacks.txt')
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as fp:
                    fp.write('\n'.join(out))
            except Exception:
                pass
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
    threading.Thread(target=hunt, daemon=True).start()



def _featured_cache_refresher():
    """後台每 150 秒重建 V1/V2 featured 兩個 z 槽——頁面請求永遠讀快取、
    唔使等 build（2026-10-08 深夜：TTL 過期連環請求 stacking build 再現
    30 秒逾時；反正精選掃描本身都係週期性工作，搬入後台做最乾淨）。"""
    # 2026-10-16（第三擊）：雲端直接唔開後台刷新——兩次實測容器都係喺
    # boot+15 分鐘（即呢度第一輪 4 個 full build）嗰陣 OOM 冧機，時間差
    # 唔到 1 分鐘。雲端改為純 lazy：第一個精選頁請求用 build_lock 單個建，
    # 之後靠頁面 TTL。本機（8GB）維持後台每 600s 刷新。
    if DISABLE_UPDATE:
        print('[featured-refresh] 雲端：後台刷新停用（512MB 首輪 build 實測 '
              'OOM），改為頁面請求先建', flush=True)
        return
    time.sleep(60)
    while True:
        for impl, cache in ((_get_featured_full_impl, _featured_full_cache),
                            (_get_v1_featured_full_impl,
                             _v1_featured_full_cache)):
            for z in (False, True):
                try:
                    data = impl(z)
                    with cache['lock']:
                        cache[z].update(ts=time.time(), data=data)
                except Exception:
                    pass
        time.sleep(600)



def _warmup():
    """伺服器啟動時在背景預先載入歷史數據池，用戶第一擊唔使等。
    2026-10-10：V3 池（內部即 V2 共享池）都要建好先好 set ready——否則
    ready=true 之後主頁 /api/v3/list 喺 V3 池 build 嘅幾分鐘內全部排隊
    窒住（本機記憶體緊絀實測 hang 超過 90 秒），用戶以為入唔到；重複雙擊
    .cmd 仲會觸發舊 launcher 殺 server 重開，boot 永遠完唔成。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        screen_engine.load_pool(conn)
        import v3_engine
        v3_engine.load_v3_pool(conn)
        conn.close()
        _pool_ready['done'] = True
        print('[warmup] 歷史數據池已就緒（V1+V2/V3）', flush=True)
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


def _seed_v3_featured_import():
    """開機種入精選W 歷史回溯（_fw_import.jsonl，2026-08-01 起合格場次＋賽果）。
    同 tools/import_fw_backtest.py 同一口徑；雲端硬碟係舊 DB（冇歷史），
    種入後「事後回查／過往紀錄／命中率」即刻有 2026-08-01 起嘅統計。
    已種過（有任何 imported 列）就跳過，可重複開機冇副作用。"""
    p = os.path.join(BASE_DIR, '_fw_import.jsonl')
    if not os.path.exists(p):
        return False
    try:
        conn = db()
        n_imp = int(conn.execute(
            "SELECT COUNT(*) FROM v3_featured WHERE detail LIKE '%\"imported\": 1%'"
        ).fetchone()[0])
        if n_imp > 0:
            conn.close()
            return True
        rows = []
        with open(p, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    if r.get('pass'):
                        rows.append(r)
        if not rows:
            conn.close()
            return False
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            _migrate_pick_line_cols(conn)
        except sqlite3.Error:
            pass
        n_ins = 0
        for r in rows:
            mid = int(r['id'])
            d = r['direction']
            res = r['res']
            result = 'P' if res == 'P' else (
                'W' if (res == 'A') == (d == 'up') else 'L')
            detail = json.dumps({'imported': 1, 'conds': None, 'gap30': None,
                                 'lookback': r.get('lookback')},
                                ensure_ascii=False)
            oc = conn.execute(
                "SELECT handicap, giver, home_odds, away_odds FROM odds_asian "
                "WHERE match_id=? AND company_id=12 "
                "ORDER BY (label='closing') DESC, change_time DESC LIMIT 1",
                (mid,)).fetchone()
            hc, gv, ho, ao = oc if oc else (None, None, None, None)
            cur = conn.execute(
                'INSERT INTO v3_featured(match_id, direction, detail, '
                'added_at, result, settled_at, hc, giver) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(match_id) DO NOTHING',
                (mid, d, detail, r['kickoff'], result, now, hc, gv))
            n_ins += cur.rowcount
            conn.execute(
                'INSERT INTO v3_featured_log(match_id, direction, added_at, '
                'handicap, giver, home_odds, away_odds) '
                'VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
                'handicap=excluded.handicap, giver=excluded.giver, '
                'home_odds=excluded.home_odds, away_odds=excluded.away_odds',
                (mid, d, r['kickoff'], hc, gv, ho, ao))
        conn.commit()
        conn.close()
        print(f'[fw-import] 已種入 {n_ins} 場精選W 歷史回溯（事後回查有晒歷史）',
              flush=True)
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
    2026-10-11：10 日窗口擴到 30 日——過期無比分場次繼續自動重抓一個月；
    有「未結算紀錄」引用嘅場次（精選W／精選／精選Z／格組合／我的選擇）
    不受日數限制兼排最前。每輪最多 30 個聯賽，其餘下輪再補，避免一次太重。"""
    conn = db()
    _feat_ref = (
        'SELECT match_id FROM v3_featured WHERE result IS NULL '
        'UNION SELECT match_id FROM v3_featured_log '
        'UNION SELECT match_id FROM featured WHERE result IS NULL '
        'UNION SELECT match_id FROM featured_log '
        'UNION SELECT match_id FROM featured_z WHERE result IS NULL '
        'UNION SELECT match_id FROM featured_z_log '
        'UNION SELECT match_id FROM fw_grid_featured WHERE result IS NULL '
        'UNION SELECT match_id FROM user_picks')
    rows = conn.execute(
        'SELECT m.id, s.titan_id FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'WHERE m.home_score IS NULL '
        # SQLite datetime modifier 唔支援複合單位（'2 hours 30 minutes'→NULL，
        # 會令條件永假、巡邏永遠 0 候選）——用 '-150 minutes'
        "AND m.kickoff <= datetime('now','+8 hours','-150 minutes') "
        # 30 日內自動重抓；精選相關場次不受日數限制（永不過期）
        "AND (m.kickoff >= datetime('now','+8 hours','-30 days') "
        '     OR m.id IN (%s)) '
        'ORDER BY CASE WHEN m.id IN (%s) THEN 0 ELSE 1 END, '
        'm.kickoff' % (_feat_ref, _feat_ref)).fetchall()
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
        if i >= 30:
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
    if fixed:
        crawler.log(conn, 'INFO',
                    f'賽果補抓：補上 {fixed} 場賽果（涉及 {min(len(by_lg), 30)} '
                    f'個聯賽，候補 {len(rows)} 場）')
    conn.close()
    return fixed


_CATCHUP_INTERVAL = 1800 if DISABLE_UPDATE else 480   # 本機 8 分鐘（2026-10-10 由15分鐘加快——用戶投訴已完場場次滯留「進行中」）；雲端 0.5C 保持 30 分鐘節流


def _result_catchup_job():
    """每 15 分鐘巡一次（雲端 30 分鐘）。賽果更新後，精選自動結算／我的選擇勝負讀取時自動跟上。"""
    while True:
        time.sleep(_CATCHUP_INTERVAL)
        if _result_catchup['running']:
            continue
        _result_catchup['running'] = True
        try:
            _result_catchup['fixed'] = _result_catchup_once()
            # 2026-10-22：賽果一補到，即刻主動結算全部精選表（完場自動更新到所有工具）
            try:
                _settle_finished_tools_bg()
            except Exception:
                pass
            # 12BET 優先政策：每日（巡邏節奏）用 12BET 重試 crown 暫代場次
            try:
                _cr, _fw = get_crawler()
                conn2 = db()
                line12_daily_retry(conn2, _cr, _fw)
                conn2.close()
            except Exception:
                pass
            # 大小規則：跟住巡邏節奏每 15 分鐘重掃（引擎純讀+毫秒級；
            # scan 閘保證同其他全庫掃描錯開，撞閘就等下個循環）
            try:
                _scan_thread(_ou_rules_scan_job)
            except Exception:
                pass
            # 半全場方法：同一巡邏節奏重掃（引擎同大小規則一樣純讀+毫秒級）
            try:
                _scan_thread(_htft_rules_scan_job)
            except Exception:
                pass
            _result_catchup['last'] = time.strftime('%Y-%m-%d %H:%M:%S')
            _result_catchup['error'] = None
        except Exception:
            _result_catchup['error'] = traceback.format_exc()[-400:]
        finally:
            _result_catchup['running'] = False


def _auto_scans():
    """開機自動補數：featured 空咗（雲端重部署會清磁碟）就重掃精選；
    check_rows 太少（種入失敗）就自動開始全庫回測；
    v3_featured（精選W）空咗就自動補掃（事後回查唔會空住）。
    唔阻塞服務。"""
    try:
        conn = db()
        n_feat = int(conn.execute('SELECT COUNT(*) FROM featured').fetchone()[0])
        n_chk = int(conn.execute('SELECT COUNT(*) FROM check_rows').fetchone()[0])
        n_v3 = int(conn.execute('SELECT COUNT(*) FROM v3_featured').fetchone()[0])
        # 表可能喺全新庫未建（CREATE 原本喺掃描函數入面）——計數前確保存在，
        # 否則 SELECT 一抛錯成個 _auto_scans 跳走，後面 fw 補掃都冇埋
        conn.execute('''CREATE TABLE IF NOT EXISTS fw_grid_featured(
            grid TEXT NOT NULL, match_id INTEGER NOT NULL, direction TEXT NOT NULL,
            detail TEXT, added_at TEXT, result TEXT, settled_at TEXT,
            PRIMARY KEY(grid, match_id))''')
        n_fw = int(conn.execute(
            'SELECT COUNT(*) FROM fw_grid_featured').fetchone()[0])
        conn.commit()
        conn.close()
        if n_feat == 0 and not _feat_scan['running']:
            print('[auto] featured 空白，開始重掃精選', flush=True)
            _scan_thread(_featured_scan_job)
        if n_chk < 1000 and not _check_scan['running']:
            print('[auto] check_rows 不足，開始全庫回測', flush=True)
            _scan_thread(_check_scan_job)
        if n_v3 == 0 and not _v3_scan['running']:
            print('[auto] v3_featured 空白，開始補掃精選W', flush=True)
            _scan_thread(_v3_featured_scan_job)
        # 精選 7/8/12 格組合：重部署後種子庫格表空白，開機排隊補掃
        # （掃描閘保證同其他掃錯開；512MB 機 1 worker 行慢啲但唔會爆）
        if n_fw == 0:
            for g in ('7', '8', '12'):
                if not _fw_grid_scan[g]['running']:
                    _scan_thread(_fw_grid_scan_job, g)
        # 大小規則：無狀態表，開機即全量掃一次填 cache（空結果都照掃，
        # 規則卡 ou_rules.json 一換即刻全量重評）
        if not _ou_rules_scan['running']:
            _scan_thread(_ou_rules_scan_job)
        # 半全場方法：同一思路，開機即全量掃一次填 cache
        if not _htft_rules_scan['running']:
            _scan_thread(_htft_rules_scan_job)
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
    # Windows 控制台預設 cp950——開機訊息/異常中文 print 會 UnicodeEncodeError
    # 連原本的錯誤都睇唔到（2026-10-22 stderr 實測）。強制 UTF-8。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    # 雲端：數據庫放永久硬碟（/data），映像每次部署焗入最新種子。
    # 2026-09-30 事故：1GB 硬碟複製 908MB 中途爆滿 → 截斷庫 → 之後每次部署
    # 開機即「database disk image is malformed」崩潰死循環。所以開機必須
    # 校驗硬碟庫：冇庫／損壞／截斷／種子較新 → 則刪舊檔後由映像種子重新複製
    # （先刪舊檔先夠位；複製前清 WAL，舊 WAL 配新庫會數據損壞）。
    seed = os.path.join(BASE_DIR, 'football.db')   # Dockerfile 焗入嘅快照
    if os.path.exists(seed) and os.path.abspath(seed) != os.path.abspath(DB_PATH):
        import shutil

        def _db_ok(path):
            try:
                if os.path.getsize(path) < os.path.getsize(seed) * 0.9:
                    return False
                # 驗證標記：上次開機 quick_check 通過後記低 size+mtime；
                # 庫未變過（mtime 一樣）就信佢，唔使每個重啟都全庫掃一次——
                # 908MB 嘅 PRAGMA quick_check 喺 512MB 實例上係 OOM 溫床
                # （2026-10-02 凌晨雲端反覆重啟嘅元兇之一）
                vm_path = path + '.verified'
                sig = f"{os.path.getsize(path)}:{os.path.getmtime(path)}"
                try:
                    with open(vm_path, encoding='ascii') as vf:
                        if vf.read().strip() == sig:
                            return True
                except OSError:
                    pass
                c = sqlite3.connect(path, timeout=10)
                r = c.execute('PRAGMA quick_check').fetchone()
                c.close()
                if r and r[0] == 'ok':
                    try:
                        with open(vm_path, 'w', encoding='ascii') as vf:
                            vf.write(sig)
                    except OSError:
                        pass
                    return True
                return False
            except sqlite3.Error:
                return False

        # 2026-10-11 重大事故修正：seed 覆蓋邏輯原本喺本地都會触发——朝早記憶體
        # 壓力令 DB 短暫 malformed，APP 重啟時 _db_ok 回 False，就咁刪走 2.1GB
        # 生產庫用 12KB 種子覆蓋（用咗 _frozen_v3.db 先救返）。seed import 只限
        # 雲端容器（DB_PATH 係嗰邊顯式設定）；本地永遠唔准掂 DB 檔案。
        need_copy = False
        if os.environ.get('DB_PATH'):
            need_copy = not os.path.exists(DB_PATH)
            if not need_copy:
                # 2026-10-16：改以 DB_TAG（映像焗入嘅 release 標籤）判斷種子新唔新——
                # 舊邏輯用 mtime，但雲端背景爬蟲成日寫 /data 庫，mtime 永遠新過種子，
                # 結果每日新種子從未落到硬碟（10-12 起雲端數據停更嘅元兇）。
                _tag = os.environ.get('DB_TAG')
                if _tag:
                    _applied = None
                    try:
                        with open(DB_PATH + '.tag', encoding='ascii') as tf:
                            _applied = tf.read().strip()
                    except OSError:
                        pass
                    need_copy = (_applied != _tag) or not _db_ok(DB_PATH)
                else:
                    need_copy = not _db_ok(DB_PATH) \
                        or os.path.getmtime(seed) > os.path.getmtime(DB_PATH)
        if need_copy:
            for ext in ('-wal', '-shm', '-journal'):
                junk = DB_PATH + ext
                if os.path.exists(junk):
                    try:
                        os.remove(junk)
                    except OSError:
                        pass
            if os.path.exists(DB_PATH):
                try:
                    os.remove(DB_PATH)   # 先刪舊檔騰位，908MB 庫喺 1GB 硬碟啱啱夠
                except OSError:
                    pass
            shutil.copy(seed, DB_PATH)
            # 記低已套用嘅 release tag——下次開機 tag 一樣就唔使再複製
            if os.environ.get('DB_TAG'):
                try:
                    with open(DB_PATH + '.tag', 'w', encoding='ascii') as tf:
                        tf.write(os.environ['DB_TAG'])
                except OSError:
                    pass
            print('[boot] 硬碟數據庫冇/損壞/過舊，已由映像種子重新複製到 '
                  f'{DB_PATH}', flush=True)
    port = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 7100))
    host = os.environ.get('HOST', '127.0.0.1')
    try:
        httpd = Server((host, port), Handler)
    except OSError:
        print(f'[錯誤] {port} 埠已被佔用——已有篩查APP伺服器在運行。')
        print('如要重開，請先用工作管理員結束舊嘅 python/app.py 進程。')
        sys.exit(1)
    print(f'篩查 APP v{SERVER_VERSION}：http://localhost:{port}')
    # 2026-10-05：初始化全部 async 化——Render 健康檢查喺 container 起後
    # 很短時間內就要 /api/ready 有反應，blocking boot（init_picks 撞鎖重試
    # 最壞 200 秒＋seed import）曾經搞到每次部署 health check 逾時 rollback。
    # 而家 serve_forever 前只做 bind；下列步驟喺背景線程順序執行，
    # 期間 /api/ready 回 ready=False（前端本來就有 loading 機制等佢）。
    def _boot_init():
        _migrate_db()
        init_picks()
        _seed_check_rows()
        _seed_v3_featured_import()
        # 2026-10-16：_auto_scans（OU/HTFT 全庫掃等）推遲 4 分鐘——新鮮 DB 落碟後
        # 開機即同 warmup/preheat 並行，512MB 實例實測 3-6 分鐘 OOM 死循環；
        # 掃描係 cache 填充，遲 4 分鐘對用戶零影響
        threading.Timer(240, _auto_scans).start()
    threading.Thread(target=_boot_init, daemon=True).start()
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=_featured_cache_refresher, daemon=True).start()
    _install_seizure_hunter()
    # 預熱（順序執行，唔好並行——並行峰值會疊加爆 512MB）：
    # 30 秒後 background 先算半全場快取，跟住建好波池；用戶撳到即刻有，
    # 亦避免冷啟動超過 Render proxy 60 秒逾時
    def _preheat_all():
        # 2026-10-16：雲端（DISABLE_UPDATE）skip htftboard＋精選 full 預熱——
        # 兩個都係 512MB 上嘅大型 build（htftboard 冷 build 56 秒），開機 +30s
        # 同 warmup 池疊加係新鮮 DB 後 OOM 死循環嘅元兇；改為第一次請求先建
        # （用戶捱一次冷啟動，好過成個容器冧機）。本機（8GB）維持預熱。
        if not DISABLE_UPDATE:
            try:
                api_htftboard()
            except Exception:
                pass
        try:
            conn = db()
            try:
                import haobao_engine
                haobao_engine.HB(conn)
            finally:
                conn.close()
        except Exception:
            pass
        try:
            # V2/V3 共享池（~128MB）預熱：2026-10-06 部署後實測，冷池第一擊喺
            # 0.5C 要build幾分鐘，Render proxy 60 秒逾時全程 502——預熱後用戶
            # 永遠唔會捱冷 build（load_v3_pool 內部共用 V2 池，一個 call 暖晒）
            conn = db()
            try:
                import v3_engine
                v3_engine.load_v3_pool(conn)
            finally:
                conn.close()
        except Exception:
            pass
        if not DISABLE_UPDATE:
            try:
                # 精選兩張 full 表預熱（順便填 60 秒快取＋完成自動結算）
                get_featured_full()
                get_v1_featured_full()
            except Exception:
                pass
        try:
            # 好波預測背景預熱（2026-10-14）：池提早重建＋49項引擎預熱
            # ＋逐場預測 top-up 入快取——用戶開好波頁全部即時顯示，
            # 唔再 81 個並行 predict 打爆 512MB 免費實例（成版 502）。
            threading.Thread(target=_hb_precompute_worker, daemon=True).start()
        except Exception:
            pass
        try:
            # 常駐池物件（V1 池／V2/V3 池／好波池／半全場快取）移入永久代：
            # gc 唔再逐次掃，減少 GC CPU 尖峰同記憶體碎片化（2026-10-06 記憶體審計）
            import gc
            gc.collect()
            gc.freeze()
            print('[preheat] 常駐物件已 gc.freeze', flush=True)
        except Exception:
            pass
        # 2026-10-10：開機自動更新（全量爬取）搬入 preheat 完先跑——同數據池
        # build 並行會將開機凍結窗口拖長幾倍（本機「直頭入唔到」調查元兇之一）。
        # _boot_auto_update 係模組層 def（下面先定義），call 時先解析，安全。
        if not DISABLE_UPDATE:
            threading.Thread(target=_boot_auto_update, daemon=True).start()
    threading.Timer(30, _preheat_all).start()
    # 賽果補抓：2026-10-17 新架構——本機（電腦）全權負責賽果爬取＋更新，
    # 雲端補抓停用（remove 512MB 容器最後一個常駐網絡工作：代理逾時堆積
    # 同不定期重啟嘅可疑元兇；13:49 單宗重啟前 18 分鐘正係補抓寫庫）。
    # 雲端賽果同精選結算跟每日推送。本機 8 分鐘節奏照舊。
    if not DISABLE_UPDATE:
        threading.Thread(target=_result_catchup_job, daemon=True).start()
    if not DISABLE_UPDATE:
        threading.Thread(target=_v2_sched_job, daemon=True).start()
    else:
        print('[boot] 雲端代理模式：賽果補抓 30 分鐘一次；'
              'V2 定時窗口更新唔開（0.5C 搶資源）；全量自動更新照舊閂', flush=True)
        threading.Thread(target=_cloud_overnight_job, daemon=True).start()
        print('[boot] 雲端凌晨尾盤補抓已啟動（每 15 分鐘：未來 60 分鐘場次）',
              flush=True)
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
                    stale = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None) - t).total_seconds() > 12 * 3600
                except Exception:
                    stale = True
            # 賽果新鮮 ≠ 即將開賽場次有盤口：未來24小時場次缺尾盤都當 stale，
            # 否則 backfill 一 bump updated_at，開機就會跳過更新，即場場次永遠冇賠率
            if not stale:
                conn = db()
                miss = conn.execute(
                    "SELECT COUNT(*) FROM matches m WHERE m.home_score IS NULL "
                    "AND m.kickoff >= datetime('now','+8 hours','-1 hours') "
                    "AND m.kickoff <= datetime('now','+8 hours','+24 hours') "
                    "AND NOT EXISTS(SELECT 1 FROM odds_asian o "
                    "WHERE o.match_id=m.id AND o.company_id=12 "
                    "AND o.label='closing')").fetchone()[0]
                conn.close()
                if miss:
                    stale = True
                    print('[boot] 未來24小時有 %d 場缺尾盤，照樣觸發更新' % miss,
                          flush=True)
            if stale:
                print('[boot] 數據快照過舊（%s），自動開始更新…' % latest, flush=True)
                # 人手模式跳過 30 分鐘節流：boot 先至行一次，唔會狂loop；
                # auto=True 會因上次失敗更新嘅 last_done 誤判跳過
                do_update(auto=False)
            else:
                print('[boot] 數據新鮮（%s），跳過自動更新' % latest, flush=True)
        except Exception:
            import traceback
            traceback.print_exc()
    # 數據商斷線自動重試：每 30 秒探測 titan007 數據主機（2026-09-29 修訂：
    # 取消強制 5 分鐘等待）；
    # 發現復活即自動觸發全量更新——用戶無需理會，連線問題自己搞掂
    def _data_host_watchdog():
        import crawler as _cr
        down_since = None
        last_auto = 0.0   # 2026-10-22：復活自動更新節流——titan007 晚晚
        # 間歇性抖動（HTTP/2 PROTOCOL_ERROR／<30 秒斷線），舊制每次抖完都
        # 開一次 40 分鐘全量更新，統一掃描不斷讓路變相癱瘓（實測一晚 6+ 次）
        while True:
            time.sleep(30)
            try:
                alive = _cr.data_host_probe()
                if alive and down_since is not None:
                    down_min = int(time.time() - down_since) // 60
                    down_since = None
                    if down_min < 3:
                        # 抖動級中斷：數據新鮮度影響微乎其微，唔開全量
                        print(f'[watchdog] titan007 抖動（中斷 {down_min} 分鐘'
                              f'）——跳過自動全量更新', flush=True)
                        continue
                    if time.time() - last_auto < 3600:
                        print(f'[watchdog] titan007 復活（中斷 {down_min} 分鐘）'
                              f'——但 1 小時內已自動更新過，跳過', flush=True)
                        continue
                    last_auto = time.time()
                    print(f'[watchdog] titan007 數據主機已復活（曾中斷 '
                          f'{down_min} 分鐘），自動開始全量更新…', flush=True)
                    if _FBATCH_PEND['hours'] is not None:
                        _scanlog('⚡ 主機復活——自動補跑您之前撳嘅'
                                 '「更新賠率＋重算資格」')
                        do_fetch_batch(_FBATCH_PEND['hours'])
                        _FBATCH_PEND['hours'] = None
                        _save_scan_state()
                    # 行人手模式（auto=False）跳過 30 分鐘節流——復活觸發每次中斷
                    # 只會有一次，但之前失敗嘅更新更新咗 last_done，auto 模式會
                    # 誤判做「啱啱更新過」而跳過（復活後數據其實未刷新嘅漏洞）
                    do_update(auto=False)
                elif not alive and down_since is None:
                    down_since = time.time()
                    print('[watchdog] titan007 數據主機暫時中斷，'
                          '每 30 秒自動重試直至復活', flush=True)
            except Exception:
                pass
    if DISABLE_UPDATE:
        print('[boot] 雲端代理模式：賽果補抓 30 分鐘一次經代理池；'
              '全量自動更新＋30 秒睇門狗照舊閂（全量由電腦每日推送）', flush=True)
    else:
        threading.Thread(target=_data_host_watchdog, daemon=True).start()
        # 開機自動更新已由 _preheat_all 完結後接力（2026-10-10），唔再 +5 秒搶資源
    # 精選系列每小時自動紀錄（2026-10-05）：只喺本機行——tick 會跑四個全庫
    # 掃描（V3＋7/8/12），喺 0.5C 雲端實機要成粒鐘 CPU，直接令 Render health
    # check 超時無限 restart（2026-10-06 實測）。雲端嘅紀錄靠每日朝早由本機
    # 推送（符合「本機係數據主人」原則）。
    if not DISABLE_UPDATE:
        threading.Thread(target=_feat_hourly_loop, daemon=True).start()
        threading.Thread(target=_result_settle_loop, daemon=True).start()
        threading.Thread(target=_sync_delta_loop, daemon=True).start()
    # 2026-10-25：line-move watcher 改為兩邊都開——舊制淨本機開，雲端爬蟲
    # 寫咗新盤冇人重檢（20 秒即時重算喺手機一直冇行過），要靠 20 分鐘
    # 循環或者本機 sync。雲端 RAM 安全：_pick_revalidate 喺 DISABLE_UPDATE
    # 自動行 light 模式（05ad3a8 實證 RAM 接近零），共用池 max_workers=1。
    threading.Thread(target=_line_move_watch_loop, daemon=True).start()
    # 2026-10-25：即將開賽賠率週期自動重抓（兩邊都開——雲端代理池行得通）
    threading.Thread(target=_auto_odds_refresh_loop, daemon=True).start()
    if not DISABLE_UPDATE:
        def _removed_backfill_boot():
            time.sleep(150)
            try:
                nb = backfill_removed_from_alerts()
                if nb:
                    print('[boot] 剔走頁回填：%d 場（由 alerts log）' % nb,
                          flush=True)
            except Exception:
                traceback.print_exc(limit=2)
        threading.Thread(target=_removed_backfill_boot,
                         daemon=True).start()
        print('[boot] 熱數據同步循環已啟動（每 30 分鐘推送雲端）', flush=True)
    else:
        print('[boot] 雲端：每小時精選紀錄＋掃描唔開（0.5C 搶資源會 health check '
              'restart loop）；紀錄由本機每小時做、每日推送', flush=True)
    # 2026-10-25 開機自愈（兩邊都行——首版誤放喺本機分支，雲端冇跑到，審計
    # #42 驗證捉到）：removed_picks 歷史重複（每 工具+場次+方向 留最新）＋
    # 窗口外 alerts 垃圾（舊制無窗口過濾嘅遺留）。冇備份——兩個清理都係刪
    # 「邏輯上唔應該存在」嘅行，留最新留窗口內，唔蝕資料。
    def _db_selfheal_boot():
        try:
            conn = db()
            try:
                cur = conn.execute('''DELETE FROM removed_picks WHERE id
                    NOT IN (SELECT MAX(id) FROM removed_picks
                    GROUP BY tool, match_id, side)''')
                n1 = cur.rowcount
                cur = conn.execute('''DELETE FROM alerts WHERE match_id IN
                    (SELECT m.id FROM matches m WHERE m.kickoff <
                    datetime('now','+8 hours','-1 hours') OR m.kickoff >
                    datetime('now','+8 hours','+7 days') OR
                    m.home_score IS NOT NULL)''')
                n2 = cur.rowcount
                conn.commit()
                if n1 or n2:
                    print('[boot] DB 自愈：剔走重複 %d 行＋窗口外通知 %d 行'
                          % (n1, n2), flush=True)
            finally:
                conn.close()
        except Exception:
            pass
    threading.Thread(target=_db_selfheal_boot, daemon=True).start()
    # 2026-10-22 統一循環（本機＋雲端都開）：取代 8X／88W／新精選三個各自
    # 獨立循環——一場一次過評全部系統（共享計算）＋變動跳過，約 6 小時一次
    # 全量。冇佢嘅話手機版 8X/88W/新精選永遠唔會自動有預測。
    threading.Thread(target=_unified_loop, daemon=True).start()
    print('[boot] 統一背景循環已啟動（每 20 分鐘變動掃描＋全表結算）',
          flush=True)

    # 2026-10-25 效能修正（深層審計#42）：oupred 模型表冷啟動 rebuild 要幾分鐘
    # （3 次全庫掃描），開機後第一擊主頁 v3/list 會卡喺 45 秒單飛閘後 429。
    # 而家 warmup 完成後喺背景起表；起緊期間請求照行（冇表就跳過 oupred 欄），
    # 用家永遠唔使等 rebuild——同 _oupred_tables 嘅「起樓期間食舊錶」設計夾埋。
    def _oupred_preheat():
        try:
            t = 0.0
            while not _pool_ready['done'] and t < 1800:
                time.sleep(5)
                t += 5
            _oupred_tables(force=True)
            print('[boot] oupred 模型表預熱完成', flush=True)
        except Exception:
            pass
    threading.Thread(target=_oupred_preheat, daemon=True).start()
    # 2026-10-12 雲端爬蟲節點：ENABLE_CLOUD_CRAWL=1 時喺背景線程探測 titan007
    # 各主機並爬佢通到嘅市場（寫 /data/football.db；本機 IP 被硬封鎖，雲端
    # 出口 IP 直達唔使代理）。本機（冇呢個 env）永遠唔會行呢段。
    if os.environ.get('ENABLE_CLOUD_CRAWL') == '1':
        def _cloud_crawl_boot():
            try:
                sys.path.insert(0, os.path.join(BASE_DIR, 'engine'))
                import cloud_crawl
                cloud_crawl.main()
            except Exception:                          # noqa: BLE001
                print('[cloud-crawl] 啟動失敗：\n' + traceback.format_exc(),
                      flush=True)
        threading.Thread(target=_cloud_crawl_boot, daemon=True).start()
        print('[boot] 雲端爬蟲節點已啟用（背景探測+爬取）', flush=True)
    httpd.serve_forever()
