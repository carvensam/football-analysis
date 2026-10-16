# -*- coding: utf-8 -*-
"""雲端爬蟲節點（2026-10-12 分工架構）：開機探測 titan007 各主機，
邊啲通就爬邊啲市場——本機 IP 被 titan007 硬封鎖，但 Render/balltrend-api
呢啲雲端節點各有唔同出口 IP，總有一個通。爬完寫入 /data/football.db
（同 web app 共用），本機電腦經 /api/export/odds pull 返去 merge。

用法（render.yaml worker startCommand）：
  python engine/cloud_crawl.py
環境變數：
  DB_PATH        永久硬碟 DB 路徑（render.yaml 已設 /data/football.db）
  EXTRA_MARKETS  額外指定市場（逗號分隔；預設按探測結果自動揀）
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from curl_cffi import requests as creq  # noqa: E402

PROBE_TARGETS = {
    'zq': 'https://zq.titan007.com/jsData/LeagueSeason/sea178.js',
    'vip': ('https://vip.titan007.com/changeDetail/handicap.aspx'
            '?id=1&companyid=12'),
    'bf': 'https://bf.titan007.com/',
    'live': 'https://live.titan007.com/',
    '1x2d': 'https://1x2d.titan007.com/3077673.js',
}
# 主機 → 佢服務嘅市場（同 crawl_extra.MARKET_HOST 一致）
HOST_MARKETS = {
    'vip': ['ou', 'ah_half', 'ou_half', '1x2', '1x2_half', 'corner',
            'asian', 'hkjc_asian', 'hkjc_ou', 'hkjc_ah_half', 'hkjc_ou_half',
            'hkjc_1x2', 'hkjc_1x2_half'],
    'bf': ['corners_stat'],
    'live': ['goals'],
    '1x2d': ['1x2all'],
}


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = '[%s cloud-crawl] %s' % (ts, msg)
    try:
        with open(os.path.join(ROOT, 'cloud_crawl.log'), 'a',
                  encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    print(line, flush=True)


def probe():
    out = {}
    for name, url in PROBE_TARGETS.items():
        try:
            r = creq.get(url, timeout=8, impersonate='chrome')
            out[name] = r.status_code in (200, 404)
        except Exception:                             # noqa: BLE001
            out[name] = False
    return out


def pick_markets(reach):
    ms = []
    for host, markets in HOST_MARKETS.items():
        if reach.get(host):
            ms += markets
    extra = os.environ.get('EXTRA_MARKETS', '')
    if extra:
        ms += [m.strip() for m in extra.split(',') if m.strip()]
    # 去重兼保持 crawl_extra 認知嘅市場名
    return list(dict.fromkeys(ms))


def main():
    import crawl_extra as ce
    cfg = json.load(open(os.path.join(ROOT, 'config.json'), encoding='utf-8'))
    cfg['proxy_direct'] = True        # 本節點 IP 直達（繞過代理池邏輯）
    cfg['crawl_threads'] = int(os.environ.get('CRAWL_THREADS', '8'))
    # 10-16：每輪限時（預設 20 分鐘，CRAWL_ROUND_MINUTES=0 閂限時=舊行為）。
    # 舊版一輪 = 「ce.run 掃到隊列清晒為止」——雲端積壓以萬計，2 線爬變相
    # 24/7 霸住 web 進程嘅 RAM/CPU（512MB 同 HTTP 服務擠逼、約每 90 分鐘
    # OOM 重啟嘅主要元兇之一）。限時到就 ce.write_control('stop')（ce worker
    # 做完手頭任務就退，乾淨收爐），finally 寫返 'run' 確保下一輪唔會被停。
    # 精確/速度零影響（爬取只係保鮮；推算用嘅池唔經呢條路）。
    import threading
    round_min = int(os.environ.get('CRAWL_ROUND_MINUTES', '20'))
    while True:
        reach = probe()
        log('probe: %s' % json.dumps(reach))
        markets = pick_markets(reach)
        if not markets:
            log('冇主機通——本節點對爬取無貢獻，60 分鐘後重探')
            time.sleep(3600)
            continue
        log('本節點負責市場：%s' % ','.join(markets))
        stopper = None
        if round_min > 0:
            def _halt():
                try:
                    ce.write_control('stop')
                    log('本輪限時到（%d 分鐘），已寫 stop 收爐' % round_min)
                except Exception:                     # noqa: BLE001
                    pass
            stopper = threading.Timer(round_min * 60, _halt)
            stopper.start()
        try:
            ce.run(only=markets, worker=1)
            log('本輪完成，30 分鐘後重探+續跑')
        except Exception:                             # noqa: BLE001
            log('run 異常：\n%s' % __import__('traceback').format_exc())
            time.sleep(300)
        finally:
            if stopper is not None:
                stopper.cancel()
            try:
                ce.write_control('run')
            except Exception:                         # noqa: BLE001
                pass
        time.sleep(1800)


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError, OSError):
        pass
    main()
