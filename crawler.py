# -*- coding: utf-8 -*-
"""
足球歷史數據爬蟲（titan007 球探網）
- 對賽資料：排名、主/客場入球失球（總榜、主場榜、客場榜）
- 亞洲盤賠率：初盤、賽前4小時、30分鐘、15分鐘、10分鐘、5分鐘、尾盤
- 比賽結果及盤口勝負判定（主勝盤/客勝盤/走盤）
- 限流：每 700 次請求休息 15 分鐘；被封自動休息重試
- 斷點續爬：每場比賽完成後立即寫庫，重新啟動自動由未完成處繼續
用法：
  python crawler.py                # 正常流程（自動續爬）
  python crawler.py --league 36 --season 2025-2026 --limit 10   # 測試
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import traceback

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding='utf-8')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-HK,zh-TW;q=0.9,zh;q=0.8',
    'Referer': 'https://zq.titan007.com/',
}

# ---------------------------------------------------------------- 讓球文字轉數值
_CN_NUM = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def line_to_float(text):
    """把 titan007 盤口文字轉為帶方向數值（正數=主隊受讓，負數=主隊讓球，0=平手）。
    '*' 前綴與『受/受让』同義 = 主隊受讓。
    例如：'平手'->0  '半球'->-0.5  '受半球'/'受让半球'/'*半球'->+0.5  '一球/球半'->-1.25"""
    t = text.strip()
    if not t:
        return None
    t = t.replace('受让', '受')          # 簡體頁用「受让」
    recv = t.startswith('受') or t.startswith('*')
    if recv:
        t = t[1:]
    if t == '平手':
        v = 0.0
    else:
        # 純數字盤口（如 '0'、'0.5'、'0.5/1'）
        if re.match(r'^-?\d+(\.\d+)?(/\d+(\.\d+)?)?$', t):
            vals = [float(x) for x in t.split('/')]
            v = sum(vals) / len(vals)
            return v if recv else -v
        parts = t.split('/')
        vals = []
        for p in parts:
            p = p.strip()
            if p == '平手':
                vals.append(0.0)
                continue
            m = re.match(r'^([一二两三四五六七八九十])?(球半|半球|球)?$', p)
            if not m:
                return None
            n = _CN_NUM.get(m.group(1), 0) if m.group(1) else 0
            u = m.group(2)
            if u == '半球':
                vals.append(n + 0.5 if n else 0.5)
            elif u == '球半':
                vals.append(n + 0.5 if n else 1.5)
            elif u == '球':
                vals.append(n if n else None)
            else:  # 只有數字（如 三）
                vals.append(n if n else None)
            if vals[-1] is None:
                return None
        v = sum(vals) / len(vals)
    return v if recv else -v


def line_float_to_text(v):
    """數值轉回文字（-0.5 -> 主讓半球；+0.25 -> 主受平/半）"""
    if v is None:
        return ''
    recv = v > 0
    a = abs(v)
    whole = int(a)
    frac = round(a - whole, 2)
    if frac == 0.25:
        s = '平手/半球' if whole == 0 else f'{_num_cn(whole)}球/{_num_cn(whole)}球半'
    elif frac == 0.5:
        s = '半球' if whole == 0 else f'{_num_cn(whole)}球半'
    elif frac == 0.75:
        s = '半球/一球' if whole == 0 else f'{_num_cn(whole)}球半/{_num_cn(whole + 1)}球'
    elif frac == 0:
        s = '平手' if whole == 0 else f'{_num_cn(whole)}球'
    else:
        s = str(v)
    return ('受' + s) if recv else s


def _num_cn(n):
    return {0: '', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五',
            6: '六', 7: '七', 8: '八', 9: '九', 10: '十'}.get(n, str(n))


# ---------------------------------------------------------------- 數據庫
SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions (
    titan_id   INTEGER PRIMARY KEY,
    req_name   TEXT NOT NULL,
    name_tc    TEXT,
    name_sc    TEXT,
    name_en    TEXT,
    category   TEXT NOT NULL CHECK (category IN ('聯賽','杯賽')),
    sub_of     INTEGER,
    enabled    INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS seasons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    titan_id    INTEGER NOT NULL REFERENCES competitions(titan_id),
    season_label TEXT NOT NULL,
    is_current  INTEGER DEFAULT 0,
    match_count_expected INTEGER DEFAULT 0,
    match_count_done     INTEGER DEFAULT 0,
    verified    INTEGER DEFAULT 0,
    UNIQUE (titan_id, season_label)
);
CREATE TABLE IF NOT EXISTS teams (
    titan_id INTEGER PRIMARY KEY,
    name_tc  TEXT,
    name_sc  TEXT,
    name_en  TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY,
    season_id     INTEGER NOT NULL REFERENCES seasons(id),
    kickoff       TEXT NOT NULL,
    home_id       INTEGER NOT NULL REFERENCES teams(titan_id),
    away_id       INTEGER NOT NULL REFERENCES teams(titan_id),
    home_score    INTEGER,
    away_score    INTEGER,
    half_home     INTEGER,
    half_away     INTEGER,
    round_label   TEXT,
    status        TEXT,
    home_rank_disp TEXT,
    away_rank_disp TEXT,
    handicap_init_disp TEXT,
    handicap_now_disp  TEXT,
    odds_done     INTEGER DEFAULT 0,   -- 0=未取盤口 1=完成 2=無盤口數據
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_matches_season ON matches(season_id);
CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(home_id, away_id);
CREATE TABLE IF NOT EXISTS standings (
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    team_id   INTEGER NOT NULL REFERENCES teams(titan_id),
    scope     TEXT NOT NULL CHECK (scope IN ('total','home','away')),
    grp       TEXT NOT NULL DEFAULT '',
    rank      INTEGER,
    played    INTEGER, win INTEGER, draw INTEGER, lose INTEGER,
    gf        INTEGER, ga INTEGER, gd INTEGER, points INTEGER,
    updated_at TEXT,
    PRIMARY KEY (season_id, team_id, scope, grp)
);
CREATE TABLE IF NOT EXISTS odds_asian (
    match_id    INTEGER NOT NULL REFERENCES matches(id),
    label       TEXT NOT NULL CHECK (label IN
        ('initial','pre_4h','pre_30m','pre_15m','pre_10m','pre_5m','closing')),
    company_id  INTEGER NOT NULL,
    handicap    REAL,          -- 讓球數（絕對值，不帶方向）
    giver       TEXT CHECK (giver IN ('home','away')),  -- 讓球方；NULL=平手盤無讓球方
    home_odds   REAL,
    away_odds   REAL,
    change_time TEXT,          -- 該盤口的實際變化時間
    PRIMARY KEY (match_id, label, company_id)
);
CREATE TABLE IF NOT EXISTS match_prestandings (
    -- 開賽前對賽數據：該場開踢前，主/客隊在所屬排名圈的總計/主場/客場
    -- 排名與入球/失球（由已入庫完場賽事推算，與對賽頁面同口徑）
    match_id INTEGER PRIMARY KEY REFERENCES matches(id),
    home_total_rank INTEGER, home_total_gf INTEGER, home_total_ga INTEGER,
    home_home_rank  INTEGER, home_home_gf  INTEGER, home_home_ga  INTEGER,
    home_away_rank  INTEGER, home_away_gf  INTEGER, home_away_ga  INTEGER,
    away_total_rank INTEGER, away_total_gf INTEGER, away_total_ga INTEGER,
    away_home_rank  INTEGER, away_home_gf  INTEGER, away_home_ga  INTEGER,
    away_away_rank  INTEGER, away_away_gf  INTEGER, away_away_ga  INTEGER
);
CREATE TABLE IF NOT EXISTS crawl_state (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS crawl_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, msg TEXT
);
DROP VIEW IF EXISTS v_match_full;
CREATE VIEW v_match_full AS
SELECT m.id AS match_id,
       c.req_name AS competition, c.category, s.season_label, s.is_current,
       m.kickoff, m.round_label,
       ht.name_tc AS home_team, at.name_tc AS away_team,
       m.home_score, m.away_score, m.half_home, m.half_away,
       oi.handicap  AS init_handicap,  oi.giver  AS init_giver,  oi.home_odds  AS init_home_odds,  oi.away_odds  AS init_away_odds,
       o4.handicap  AS h4_handicap,    o4.giver  AS h4_giver,    o4.home_odds  AS h4_home_odds,    o4.away_odds  AS h4_away_odds,
       o30.handicap AS m30_handicap,   o30.giver AS m30_giver,   o30.home_odds AS m30_home_odds,   o30.away_odds AS m30_away_odds,
       o15.handicap AS m15_handicap,   o15.giver AS m15_giver,   o15.home_odds AS m15_home_odds,   o15.away_odds AS m15_away_odds,
       o10.handicap AS m10_handicap,   o10.giver AS m10_giver,   o10.home_odds AS m10_home_odds,   o10.away_odds AS m10_away_odds,
       o5.handicap  AS m5_handicap,    o5.giver  AS m5_giver,    o5.home_odds  AS m5_home_odds,    o5.away_odds  AS m5_away_odds,
       oc.handicap  AS closing_handicap, oc.giver AS closing_giver, oc.home_odds AS closing_home_odds, oc.away_odds AS closing_away_odds,
       CASE WHEN m.home_score IS NULL OR oc.handicap IS NULL THEN NULL
            WHEN oc.giver = 'home' AND m.home_score - m.away_score > oc.handicap THEN '主勝盤'
            WHEN oc.giver = 'home' AND m.home_score - m.away_score < oc.handicap THEN '客勝盤'
            WHEN oc.giver = 'home' THEN '走盤'
            WHEN m.home_score - m.away_score + oc.handicap > 0 THEN '主勝盤'
            WHEN m.home_score - m.away_score + oc.handicap < 0 THEN '客勝盤'
            ELSE '走盤' END AS handicap_result
FROM matches m
JOIN seasons s  ON s.id = m.season_id
JOIN competitions c ON c.titan_id = s.titan_id
JOIN teams ht ON ht.titan_id = m.home_id
JOIN teams at ON at.titan_id = m.away_id
LEFT JOIN odds_asian oi  ON oi.match_id  = m.id AND oi.label = 'initial'  AND oi.company_id = 12
LEFT JOIN odds_asian o4  ON o4.match_id  = m.id AND o4.label = 'pre_4h'   AND o4.company_id = 12
LEFT JOIN odds_asian o30 ON o30.match_id = m.id AND o30.label = 'pre_30m'  AND o30.company_id = 12
LEFT JOIN odds_asian o15 ON o15.match_id = m.id AND o15.label = 'pre_15m'  AND o15.company_id = 12
LEFT JOIN odds_asian o10 ON o10.match_id = m.id AND o10.label = 'pre_10m'  AND o10.company_id = 12
LEFT JOIN odds_asian o5  ON o5.match_id  = m.id AND o5.label  = 'pre_5m'   AND o5.company_id = 12
LEFT JOIN odds_asian oc  ON oc.match_id  = m.id AND oc.label  = 'closing'  AND oc.company_id = 12;
"""


def db_connect(cfg):
    conn = sqlite3.connect(os.path.join(BASE_DIR, cfg['db_path']))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute('PRAGMA table_info(odds_asian)')]
    if 'giver' not in cols:                      # 舊庫升級
        conn.execute("ALTER TABLE odds_asian ADD COLUMN giver TEXT")
        conn.commit()
    return conn


def log(conn, level, msg):
    ts = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('INSERT INTO crawl_log(ts,level,msg) VALUES (?,?,?)', (ts, level, msg))
    conn.commit()
    print(f'[{ts}] {level}: {msg}', flush=True)


# ---------------------------------------------------------------- 爬取器
class Fetcher:
    """帶限流與被封偵測的抓取器"""

    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.req_count = int(self._state('req_count', '0'))
        self.last_req_time = 0.0

    def _state(self, k, default=None):
        r = self.conn.execute('SELECT v FROM crawl_state WHERE k=?', (k,)).fetchone()
        return r[0] if r else default

    def _set_state(self, k, v):
        self.conn.execute('INSERT INTO crawl_state(k,v) VALUES(?,?) '
                          'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))
        self.conn.commit()

    def _rest(self, minutes, reason):
        log(self.conn, 'WARN', f'{reason}，休息 {minutes} 分鐘後繼續…')
        time.sleep(minutes * 60)
        log(self.conn, 'INFO', '休息完畢，繼續爬取')

    def get(self, url, encoding='utf-8', referer=None, is_odds_page=False):
        delay = self.cfg['request_delay_sec']
        max_req = self.cfg['max_requests_before_rest']
        for attempt in range(self.cfg.get('blocked_retry_times', 3) + 1):
            # 請求間隔
            wait = delay - (time.time() - self.last_req_time)
            if wait > 0:
                time.sleep(wait)
            # 每 N 次請求強制休息（跨重啟累計）
            if self.req_count >= max_req:
                self._rest(self.cfg['rest_minutes'], f'已達 {max_req} 次請求（反爬蟲限流）')
                self.req_count = 0
                self._set_state('req_count', 0)
            headers = {'Referer': referer} if referer else {}
            try:
                r = self.s.get(url, headers=headers, timeout=30)
                self.last_req_time = time.time()
                self.req_count += 1
                self._set_state('req_count', self.req_count)
            except requests.RequestException as e:
                log(self.conn, 'WARN', f'連線錯誤 {e}，休息 {self.cfg["rest_minutes"]} 分鐘')
                self._rest(self.cfg['rest_minutes'], '連線異常')
                continue
            if r.status_code != 200:
                log(self.conn, 'WARN', f'HTTP {r.status_code}：{url}')
                if r.status_code in (403, 429, 442):
                    self._rest(self.cfg['rest_minutes'], f'疑似被封（HTTP {r.status_code}）')
                    continue
                return None
            r.encoding = encoding
            text = r.text
            if is_odds_page and ('頁面不存在' in text or '页面不存在' in text):
                return 'NO_DATA'
            if len(text) < 50 and ('jsData' in url or 'vip.titan007' in url):
                self._rest(self.cfg['rest_minutes'],
                           f'回應異常過短（疑似被封）：{url}')
                continue
            return text
        return None


# ---------------------------------------------------------------- 賽季檔案解析
def js_array_to_py(s):
    """把 JS 陣列字串安全轉成 Python 物件。
    JS 陣列允許空槽位（如 [1,,2] -> 1, undefined, 2），Python eval 會語法錯誤，
    這裡統一轉成 None。"""
    s = re.sub(r'(?<=,)(?=,)', 'None', s)   # ,,   -> ,None,
    s = re.sub(r'\[(,)', '[None,', s)       # [,   -> [None,
    s = re.sub(r',(\])', ',None]', s)       # ,]   -> ,None]
    return eval(s.replace('null', 'None'))


def parse_season_js(text):
    """解析賽季檔（聯賽 s<id>.js / 杯賽 c<id>.js）。
    回傳：meta, teams, total, home, away, kinds, blocks
    - kinds: 杯賽階段ID -> 繁體名（如 {'27245': '聯賽階段'}）
    - blocks: [(jh鍵, 列)]；G 開頭=賽事（可能兩回合巢狀），S 開頭=榜單"""
    def grab(var):
        m = re.search(r'var %s = (\[.*?\]);' % var, text, re.S)
        if not m:
            return None
        return js_array_to_py(m.group(1))

    meta = grab('arrLeague') or grab('arrCup')
    teams = grab('arrTeam') or []
    total = grab('totalScore') or []
    home = grab('homeScore') or []
    away = grab('guestScore') or []

    kinds = {}
    if grab('arrCupKind'):
        for k in grab('arrCupKind'):
            kinds[str(k[0])] = k[3]      # 階段繁體名

    blocks = []
    for m in re.finditer(r'jh\["([^"]+)"\]\s*=\s*(\[.*?\]);', text, re.S):
        blocks.append((m.group(1), js_array_to_py(m.group(2))))
    return meta, teams, total, home, away, kinds, blocks


def is_match_row(r):
    return (isinstance(r, list) and len(r) >= 7
            and isinstance(r[0], int) and isinstance(r[1], int)
            and isinstance(r[3], str)
            and re.match(r'^\d{4}-\d{2}-\d{2}', r[3]))


def iter_match_rows(rows):
    """展開賽事列：支援兩回合巢狀結構 [主隊,客隊,...,[場次1],[場次2]]"""
    for r in rows:
        if is_match_row(r):
            yield r
        elif isinstance(r, list) and any(isinstance(x, list) for x in r):
            for x in r:
                if is_match_row(x):
                    yield x


def stage_label(key, kinds):
    """jh 鍵 -> 可讀階段名：G27245A -> '聯賽階段 組A'；S27245A -> '聯賽階段'；
    R_3 -> 'R_3'"""
    m = re.match(r'^[GS](\d+)([A-Z]?)$', key)
    if m:
        base = kinds.get(m.group(1), m.group(1))
        if key.startswith('S') or not m.group(2):
            return base
        return base + f' 組{m.group(2)}'
    return key


def parse_cup_standings(rows, grp):
    """杯賽 S 區塊榜單：[rank,team,played,w,d,l,gf,ga,gd,points,...]"""
    out = []
    for r in rows:
        try:
            out.append({'team_id': r[1], 'scope': 'total', 'grp': grp,
                        'rank': r[0], 'played': r[2], 'win': r[3],
                        'draw': r[4], 'lose': r[5], 'gf': r[6], 'ga': r[7],
                        'gd': r[8], 'points': r[9]})
        except (IndexError, TypeError):
            continue
    return out


def parse_standings(rows, scope):
    """total 榜欄位：[color,rank,team,x,played,w,d,l,gf,ga,gd,...,points(16)]
    home/away 榜欄位不同：[color,team,played,w,d,l,gf,ga,gd,...,points(14)]，無官方排名，
    排名在此按 points→gd→gf 自行計算"""
    out = []
    for r in rows:
        try:
            if scope == 'total':
                out.append({
                    'team_id': r[2], 'scope': scope, 'rank': r[1],
                    'played': r[4], 'win': r[5], 'draw': r[6], 'lose': r[7],
                    'gf': r[8], 'ga': r[9], 'gd': r[10], 'points': r[16],
                })
            else:
                out.append({
                    'team_id': r[1], 'scope': scope, 'rank': None,
                    'played': r[2], 'win': r[3], 'draw': r[4], 'lose': r[5],
                    'gf': r[6], 'ga': r[7], 'gd': r[8], 'points': r[14],
                })
        except (IndexError, TypeError):
            continue
    if scope != 'total':
        out.sort(key=lambda s: (-(s['points'] or 0), -(s['gd'] or 0),
                                -(s['gf'] or 0)))
        for i, s in enumerate(out, 1):
            s['rank'] = i
    return out


# ---------------------------------------------------------------- 亞洲盤頁面解析
CELL_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.S | re.I)
ROW_RE = re.compile(r'<TR[^>]*>(.*?)</TR>', re.S | re.I)
TAG_RE = re.compile(r'<[^>]+>')


def parse_odds_html(html, kickoff):
    """解析 changeDetail/handicap.aspx 頁面，回傳排序後的賽前賠率時間線：
    [(change_datetime, handicap, home_odds, away_odds), ...]（依時間升序）"""
    timeline = []
    for rowm in ROW_RE.finditer(html):
        cells = [TAG_RE.sub('', c).strip() for c in CELL_RE.findall(rowm.group(1))]
        if len(cells) < 6:
            continue
        _minute, _score, home_odds, line, away_odds, change_time = cells[:6]
        status = cells[6] if len(cells) > 6 else ''
        if status == '滚' or '滚' in status:      # 滾球（走地）資料不計
            continue
        try:
            ho = float(home_odds)
            ao = float(away_odds)
        except ValueError:
            continue                                  # 封盤或空白列
        hv = line_to_float(line)
        if hv is None:
            continue
        m = re.match(r'(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', change_time)
        if not m:
            continue
        mo, d, h, mi = map(int, m.groups())
        # 推算年份（變化時間不會晚於開賽）
        year = kickoff.year
        cand = dt.datetime(year, mo, d, h, mi)
        if cand > kickoff:
            cand = dt.datetime(year - 1, mo, d, h, mi)
        timeline.append((cand, hv, ho, ao))
    timeline.sort(key=lambda x: x[0])
    return timeline


SNAPSHOTS = [('initial', None), ('pre_4h', 240), ('pre_30m', 30),
             ('pre_15m', 15), ('pre_10m', 10), ('pre_5m', 5), ('closing', 0)]


def derive_snapshots(timeline, kickoff):
    """由時間線提取七個時點的盤口。
    pre_N = 開賽前 N 分鐘或之前最後一次變盤；closing = 開賽前最後一次變盤"""
    if not timeline:
        return {}
    out = {}
    out['initial'] = timeline[0]
    for label, mins in SNAPSHOTS[1:]:
        if label == 'closing':
            cutoff = kickoff + dt.timedelta(minutes=2)
        else:
            cutoff = kickoff - dt.timedelta(minutes=mins)
        cands = [t for t in timeline if t[0] <= cutoff]
        if cands:
            out[label] = cands[-1]
    return out


# ---------------------------------------------------------------- 主流程
def upsert_team(conn, tid, name_tc, name_sc, name_en):
    conn.execute(
        'INSERT INTO teams(titan_id,name_tc,name_sc,name_en) VALUES(?,?,?,?) '
        'ON CONFLICT(titan_id) DO UPDATE SET '
        'name_tc=COALESCE(excluded.name_tc,name_tc),'
        'name_sc=COALESCE(excluded.name_sc,name_sc),'
        'name_en=COALESCE(excluded.name_en,name_en)',
        (tid, name_tc, name_sc, name_en))


def save_season(conn, titan_id, season_label, is_current, parsed):
    meta, teams, total, home, away, kinds, blocks = parsed
    conn.execute(
        'INSERT INTO seasons(titan_id,season_label,is_current) VALUES(?,?,?) '
        'ON CONFLICT(titan_id,season_label) DO UPDATE SET is_current=excluded.is_current',
        (titan_id, season_label, is_current))
    season_id = conn.execute(
        'SELECT id FROM seasons WHERE titan_id=? AND season_label=?',
        (titan_id, season_label)).fetchone()[0]

    for t in teams:
        upsert_team(conn, t[0], t[2], t[1], t[3])

    n_matches = 0
    now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for key, rows in blocks:
        if key.startswith('S'):            # 杯賽榜單
            for st in parse_cup_standings(rows, stage_label(key, kinds)):
                _save_standing(conn, season_id, st, now)
            continue
        label = stage_label(key, kinds)
        for r in iter_match_rows(rows):
            # r = [id, leagueid, subid, 'YYYY-MM-DD HH:MM', home, away, 全場, 半場, ...]
            mid = r[0]
            score = r[6] if isinstance(r[6], str) else ''
            half = r[7] if isinstance(r[7], str) else ''
            hs = as_ = hh = ah = None
            if score and re.match(r'^\d+-\d+', score):
                hs, as_ = score.split('-')[:2]
                hs, as_ = int(hs), int(as_)
            if half and re.match(r'^\d+-\d+', half):
                hh, ah = half.split('-')[:2]
                hh, ah = int(hh), int(ah)
            conn.execute(
                'INSERT INTO matches(id,season_id,kickoff,home_id,away_id,home_score,'
                'away_score,half_home,half_away,round_label,status,home_rank_disp,'
                'away_rank_disp,handicap_init_disp,handicap_now_disp,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET '
                'home_score=excluded.home_score, away_score=excluded.away_score, '
                'half_home=excluded.half_home, half_away=excluded.half_away, '
                'status=excluded.status, updated_at=excluded.updated_at',
                (mid, season_id, r[3], r[4], r[5], hs, as_, hh, ah, label,
                 'finished' if hs is not None else 'scheduled',
                 str(r[8]) if len(r) > 8 and r[8] not in (None, '') else None,
                 str(r[9]) if len(r) > 9 and r[9] not in (None, '') else None,
                 str(r[10]) if len(r) > 10 and r[10] not in (None, '') else None,
                 str(r[11]) if len(r) > 11 and r[11] not in (None, '') else None, now))
            n_matches += 1

    for rows, scope in ((total, 'total'), (home, 'home'), (away, 'away')):
        for st in parse_standings(rows, scope):
            st['grp'] = ''
            _save_standing(conn, season_id, st, now)
    conn.commit()
    return season_id, n_matches


def _save_standing(conn, season_id, st, now):
    conn.execute(
        'INSERT INTO standings(season_id,team_id,scope,grp,rank,played,win,draw,'
        'lose,gf,ga,gd,points,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(season_id,team_id,scope,grp) DO UPDATE SET '
        'rank=excluded.rank,played=excluded.played,win=excluded.win,'
        'draw=excluded.draw,lose=excluded.lose,gf=excluded.gf,ga=excluded.ga,'
        'gd=excluded.gd,points=excluded.points,updated_at=excluded.updated_at',
        (season_id, st['team_id'], st['scope'], st.get('grp', ''),
         st['rank'], st['played'], st['win'], st['draw'], st['lose'],
         st['gf'], st['ga'], st['gd'], st['points'], now))


def crawl_odds_for_match(conn, fetcher, match_id, kickoff_str, company_id):
    kickoff = dt.datetime.strptime(kickoff_str, '%Y-%m-%d %H:%M')
    url = f'https://vip.titan007.com/changeDetail/handicap.aspx?id={match_id}&companyid={company_id}'
    html = fetcher.get(url, encoding='gbk', referer='https://zq.titan007.com/',
                       is_odds_page=True)
    if html is None:
        return False
    if html == 'NO_DATA':
        conn.execute('UPDATE matches SET odds_done=2 WHERE id=?', (match_id,))
        conn.commit()
        return True
    timeline = parse_odds_html(html, kickoff)
    snaps = derive_snapshots(timeline, kickoff)
    for label, _ in SNAPSHOTS:
        if label not in snaps:
            continue
        t, hv, ho, ao = snaps[label]
        abs_v = abs(hv)
        giver = None if abs_v == 0 else ('away' if hv > 0 else 'home')
        # 一致性檢查：讓球方賠率通常較低（主勝VS客勝，低者讓球）
        if giver and abs(ho - ao) > 0.51:
            giver_odds = ho if giver == 'home' else ao
            recv_odds = ao if giver == 'home' else ho
            if giver_odds > recv_odds:
                log(conn, 'WARN',
                    f'場次{match_id} {label} 讓球方賠率異常（{giver}讓{abs_v} '
                    f'主{ho}/客{ao}），請抽查')
        conn.execute(
            'INSERT INTO odds_asian(match_id,label,company_id,handicap,giver,'
            'home_odds,away_odds,change_time) VALUES(?,?,?,?,?,?,?,?) '
            'ON CONFLICT(match_id,label,company_id) DO UPDATE SET '
            'handicap=excluded.handicap,giver=excluded.giver,'
            'home_odds=excluded.home_odds,'
            'away_odds=excluded.away_odds,change_time=excluded.change_time',
            (match_id, label, company_id, abs_v, giver, ho, ao,
             t.strftime('%Y-%m-%d %H:%M')))
    conn.execute('UPDATE matches SET odds_done=1 WHERE id=?', (match_id,))
    conn.commit()
    return True


def get_season_list(fetcher, titan_id):
    url = f'https://zq.titan007.com/jsData/LeagueSeason/sea{titan_id}.js'
    text = fetcher.get(url)
    if not text:
        return []
    m = re.search(r'arrSeason = \[(.*?)\]', text, re.S)
    if not m:
        return []
    return [s.strip().strip("'") for s in m.group(1).split(',')]


def resolve_prefix(fetcher, titan_id):
    """找出賽季數據檔前綴（如 s36 / c103 / s25_943）。
    一般聯賽 s<id>，杯賽 c<id>，子聯賽 s<subId>_<parentId>。
    結果緩存到 crawl_state，避免每次重查。"""
    cached = fetcher._state(f'prefix:{titan_id}')
    if cached:
        return cached
    for page in (f'https://zq.titan007.com/big/League/{titan_id}.html',
                 f'https://zq.titan007.com/big/SubLeague/{titan_id}.html',
                 f'https://zq.titan007.com/big/CupMatch/{titan_id}.html'):
        text = fetcher.get(page)
        if not text:
            continue
        m = re.search(r'jsData/matchResult/[^/]+/([a-z]\d+(?:_\d+)?)\.js', text)
        if m:
            fetcher._set_state(f'prefix:{titan_id}', m.group(1))
            return m.group(1)
    return None


def discover_sub_leagues(fetcher, titan_id):
    """由聯賽/杯賽主頁找出子聯賽（春季/秋季/東區/西區/分組等獨立ID）"""
    found = []
    for page in (f'https://zq.titan007.com/big/League/{titan_id}.html',
                 f'https://zq.titan007.com/big/CupMatch/{titan_id}.html'):
        text = fetcher.get(page)
        if text:
            found += [int(x) for x in
                      re.findall(r'/big/SubLeague/(\d+)\.html', text)]
    return sorted(set(found))


def run(cfg, args):
    conn = db_connect(cfg)
    fetcher = Fetcher(conn, cfg)
    leagues = json.load(open(os.path.join(BASE_DIR, 'leagues.json'), encoding='utf-8'))

    for lg in leagues:
        if not lg.get('enabled'):
            continue
        if args.league and lg['titan_id'] != args.league:
            continue
        conn.execute(
            'INSERT INTO competitions(titan_id,req_name,name_tc,name_sc,name_en,'
            'category,sub_of,enabled) VALUES(?,?,?,?,?,?,NULL,1) '
            'ON CONFLICT(titan_id) DO NOTHING',
            (lg['titan_id'], lg['req_name'], lg['name_tc'], lg['name_sc'],
             lg['name_en'], lg['category']))
        conn.commit()

        # 子聯賽發現（一次即可，已發現的記入 competitions）
        if cfg.get('discover_sub_leagues'):
            have = conn.execute('SELECT COUNT(*) FROM competitions WHERE sub_of=?',
                                (lg['titan_id'],)).fetchone()[0]
            if have == 0:
                try:
                    for sid in discover_sub_leagues(fetcher, lg['titan_id']):
                        if sid == lg['titan_id']:
                            continue
                        conn.execute(
                            'INSERT OR IGNORE INTO competitions(titan_id,req_name,'
                            'name_tc,category,sub_of,enabled) VALUES(?,?,?,?,?,1)',
                            (sid, lg['req_name'] + '（子聯賽）', None,
                             lg['category'], lg['titan_id']))
                    conn.commit()
                except Exception:
                    log(conn, 'ERROR', f"子聯賽發現失敗 {lg['titan_id']}\n" + traceback.format_exc())

        ids = [lg['titan_id']] + [
            r[0] for r in conn.execute(
                'SELECT titan_id FROM competitions WHERE sub_of=?',
                (lg['titan_id'],)).fetchall()]

        for tid in ids:
            seasons = get_season_list(fetcher, tid)
            if not seasons:
                log(conn, 'WARN', f'聯賽 {tid} 無法取得賽季列表，跳過')
                continue
            # 過去五個完整賽季 + 現行賽季
            target = seasons[:cfg['seasons_complete'] + 1]
            if not cfg.get('include_current_season'):
                target = target[1:]
            if args.season:
                target = [s for s in target if s == args.season]
            for season_label in target:
                is_current = 1 if season_label == seasons[0] else 0
                done = conn.execute(
                    'SELECT s.id, s.verified FROM seasons s WHERE s.titan_id=? '
                    'AND s.season_label=?', (tid, season_label)).fetchone()
                if done:
                    pending = conn.execute(
                        'SELECT COUNT(*) FROM matches WHERE season_id=? AND '
                        'odds_done=0 AND status="finished"', (done[0],)).fetchone()[0]
                    if done[1] and pending == 0 and not args.force:
                        continue
                log(conn, 'INFO', f'開始：{lg["req_name"]}({tid}) {season_label}')
                prefix = resolve_prefix(fetcher, tid)
                if not prefix:
                    log(conn, 'ERROR', f'無法判定數據檔前綴，跳過 {tid} {season_label}')
                    continue
                url = (f'https://zq.titan007.com/jsData/matchResult/'
                       f'{season_label}/{prefix}.js?version=1')
                text = fetcher.get(url)
                if not text or text.lstrip().startswith('<'):
                    log(conn, 'ERROR', f'賽季檔下載失敗 {tid} {season_label}')
                    continue
                try:
                    parsed = parse_season_js(text)
                except Exception:
                    log(conn, 'ERROR', f'賽季檔解析失敗 {tid} {season_label}\n'
                        + traceback.format_exc())
                    continue
                season_id, n_matches = save_season(conn, tid, season_label,
                                                   is_current, parsed)

                # 盤口（可斷點續爬）
                todo = conn.execute(
                    'SELECT id, kickoff FROM matches WHERE season_id=? AND '
                    'odds_done=0 AND status="finished" ORDER BY kickoff',
                    (season_id,)).fetchall()
                log(conn, 'INFO', f'{season_label}：賽事 {n_matches} 場，待取盤口 {len(todo)} 場')
                limit = args.limit if args.limit else None
                done_odds = 0
                for mid, kickoff in todo:
                    if limit is not None and done_odds >= limit:
                        break
                    ok = crawl_odds_for_match(conn, fetcher, mid, kickoff,
                                              cfg['company_id'])
                    if ok:
                        done_odds += 1

                # 場數核對
                expected = n_matches
                finished_db = conn.execute(
                    'SELECT COUNT(*) FROM matches WHERE season_id=? AND '
                    'home_score IS NOT NULL', (season_id,)).fetchone()[0]
                verified = 1 if expected == n_matches else 0
                conn.execute(
                    'UPDATE seasons SET match_count_expected=?, match_count_done=?, '
                    'verified=? WHERE id=?',
                    (expected, finished_db, verified, season_id))
                conn.commit()
                log(conn, 'INFO' if verified else 'WARN',
                    f'{lg["req_name"]} {season_label} 完成：檔案 {n_matches} 場 / '
                    f'已完場入庫 {finished_db} 場 / 盤口 {done_odds} 場，'
                    f'核對{"通過" if verified else "不相符，請複查"}')
    # 依已入庫賽果重建「開賽前對賽數據」（36 項）
    if not args.skip_prematch:
        try:
            import prematch
            n = prematch.build_all(conn)
            log(conn, 'INFO', f'開賽前對賽數據已重建：{n} 場')
        except Exception:
            log(conn, 'ERROR', '開賽前對賽數據重建失敗\n' + traceback.format_exc())
    log(conn, 'INFO', '本次爬取流程結束')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--league', type=int, default=None, help='只爬指定 titan007 聯賽ID')
    ap.add_argument('--season', type=str, default=None, help='只爬指定賽季，如 2025-2026')
    ap.add_argument('--limit', type=int, default=None, help='每季只取前 N 場盤口（測試用）')
    ap.add_argument('--force', action='store_true', help='忽略已完成標記重新核對')
    ap.add_argument('--prematch-only', action='store_true',
                    help='只重建開賽前對賽數據（36 項），不爬取')
    ap.add_argument('--skip-prematch', action='store_true',
                    help='爬取後不重建開賽前對賽數據')
    ap.add_argument('--resume', action='store_true',
                    help='斷點續爬（預設行為，此參數僅供桌面捷徑使用）')
    args = ap.parse_args()
    cfg = json.load(open(os.path.join(BASE_DIR, 'config.json'), encoding='utf-8'))
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    if args.prematch_only:
        conn = db_connect(cfg)
        import prematch
        n = prematch.build_all(conn)
        log(conn, 'INFO', f'開賽前對賽數據已重建：{n} 場')
        return
    run(cfg, args)


if __name__ == '__main__':
    main()
