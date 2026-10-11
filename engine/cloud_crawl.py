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
    while True:
        reach = probe()
        log('probe: %s' % json.dumps(reach))
        markets = pick_markets(reach)
        if not markets:
            log('冇主機通——本節點對爬取無貢獻，60 分鐘後重探')
            time.sleep(3600)
            continue
        log('本節點負責市場：%s' % ','.join(markets))
        try:
            ce.run(only=markets, worker=1)
            log('本輪完成，30 分鐘後重探+續跑')
        except Exception:                             # noqa: BLE001
            log('run 異常：\n%s' % __import__('traceback').format_exc())
            time.sleep(300)
        time.sleep(1800)


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError, OSError):
        pass
    main()
