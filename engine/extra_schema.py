# -*- coding: utf-8 -*-
"""擴充資料庫結構：新增盤口/角球/衍生數據表。

新增：
  matches.is_neutral          中立場（0=不是, 1=中立場, NULL=未知）；來源 1x2d js 的 neutrality 欄
  match_corners               每場角球 4 項（全場主/客、半場主/客；總數相加可得）
  odds_ou                     全場大小球七時點（易胜博 company_id=12）
  odds_ah_half                半場亞洲盤七時點（易胜博）
  odds_ou_half                半場大小球七時點（易胜博）
  odds_1x2                    全場歐洲主客和七時點（易胜博）
  odds_1x2_half               半場歐洲主客和七時點（Crown company_id=3）
  odds_corner                 全場角球讓球七時點（Crown）
  odds_1x2_all                百家歐指初盤+即時（含概率/返還率/凱利指數）
  match_derived               離線衍生：近六場同賽事戰績積分、近20場贏盤率（尾盤結算）
  extra_tasks                 擴充爬蟲工作隊列（match_id × market，斷點續爬）
  hkjc_*（六表）              香港馬會（company_id=48）盤口：全/半場亞洲盤、大小球、歐洲主客和
                              line_idx=0 主盤，1/2/3…=選擇2/3/4（馬會大小球/亞盤可能有多盤）
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS match_corners (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id),
    corner_ft_home INTEGER, corner_ft_away INTEGER,
    corner_ht_home INTEGER, corner_ht_away INTEGER
);

-- 以下五表結構同 odds_asian 思路：label = initial/pre_4h/pre_30m/pre_15m/pre_10m/pre_5m/closing
CREATE TABLE IF NOT EXISTS odds_ou (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    total_line REAL NOT NULL,        -- 大小球分界線（如 2.5）
    over_odds REAL NOT NULL,
    under_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, company_id)
);

CREATE TABLE IF NOT EXISTS odds_ah_half (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    handicap REAL NOT NULL,          -- 絕對值 ≥ 0
    giver TEXT,                      -- 'home'/'away'/NULL(平手)
    home_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, company_id)
);

CREATE TABLE IF NOT EXISTS odds_ou_half (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    total_line REAL NOT NULL,
    over_odds REAL NOT NULL,
    under_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, company_id)
);

CREATE TABLE IF NOT EXISTS odds_1x2 (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    home_odds REAL NOT NULL,
    draw_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, company_id)
);

CREATE TABLE IF NOT EXISTS odds_1x2_half (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    home_odds REAL NOT NULL,
    draw_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, company_id)
);

CREATE TABLE IF NOT EXISTS odds_corner (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    handicap REAL NOT NULL,          -- 絕對值 ≥ 0
    giver TEXT,                      -- 'home'/'away'/NULL(平手)
    home_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, company_id)
);

-- 百家歐指（每場每公司一列）：初盤 + 即時(尾盤)，含概率、返還率、凱利指數
CREATE TABLE IF NOT EXISTS odds_1x2_all (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    company_id INTEGER NOT NULL,
    company_name TEXT,
    init_home REAL, init_draw REAL, init_away REAL,
    init_prob_home REAL, init_prob_draw REAL, init_prob_away REAL,
    init_payout REAL,
    init_kelly_home REAL, init_kelly_draw REAL, init_kelly_away REAL,
    cur_home REAL, cur_draw REAL, cur_away REAL,
    cur_prob_home REAL, cur_prob_draw REAL, cur_prob_away REAL,
    cur_payout REAL,
    cur_kelly_home REAL, cur_kelly_draw REAL, cur_kelly_away REAL,
    change_time TEXT,
    PRIMARY KEY (match_id, company_id)
);

-- 離線衍生（derive.py 計算，無需爬取）
CREATE TABLE IF NOT EXISTS match_derived (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id),
    -- 近六場同賽事（開賽前，全部場地）勝/和/負/積分
    home_last6_w INTEGER, home_last6_d INTEGER, home_last6_l INTEGER, home_last6_pts INTEGER,
    away_last6_w INTEGER, away_last6_d INTEGER, away_last6_l INTEGER, away_last6_pts INTEGER,
    -- 近20場贏盤率（尾盤結算；走盤剔除；分母=有尾盤盤口的場數）
    home_hw_winrate_home REAL,    -- 主隊近20場「主場」賽事贏盤率
    home_hw_winrate_all REAL,     -- 主隊近20場「主+客」全部賽事贏盤率
    away_hw_winrate_away REAL,    -- 客隊近20場「客場」賽事贏盤率
    away_hw_winrate_all REAL      -- 客隊近20場「主+客」全部賽事贏盤率
);

-- 擴充爬蟲工作隊列：每場 8 項任務
CREATE TABLE IF NOT EXISTS extra_tasks (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    market TEXT NOT NULL,           -- ou / ah_half / ou_half / 1x2 / 1x2_half / corner / 1x2all / corners_stat
    status INTEGER NOT NULL DEFAULT 0,  -- 0=未做 1=完成 2=無數據
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (match_id, market)
);

-- 香港馬會（HKJC，changeDetail company_id=48）六盤口表。
-- line_idx：0=主盤；1/2/3…=同場另一條盤線（選擇2、3、4）。1x2 無盤線概念，恒為 0。
CREATE TABLE IF NOT EXISTS hkjc_asian (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    line_idx INTEGER NOT NULL DEFAULT 0,
    handicap REAL NOT NULL,
    giver TEXT,                      -- 'home'/'away'/NULL(平手)
    home_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, line_idx)
);

CREATE TABLE IF NOT EXISTS hkjc_ou (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    line_idx INTEGER NOT NULL DEFAULT 0,
    total_line REAL NOT NULL,
    over_odds REAL NOT NULL,
    under_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, line_idx)
);

CREATE TABLE IF NOT EXISTS hkjc_ah_half (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    line_idx INTEGER NOT NULL DEFAULT 0,
    handicap REAL NOT NULL,
    giver TEXT,
    home_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, line_idx)
);

CREATE TABLE IF NOT EXISTS hkjc_ou_half (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    line_idx INTEGER NOT NULL DEFAULT 0,
    total_line REAL NOT NULL,
    over_odds REAL NOT NULL,
    under_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, line_idx)
);

CREATE TABLE IF NOT EXISTS hkjc_1x2 (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    line_idx INTEGER NOT NULL DEFAULT 0,
    home_odds REAL NOT NULL,
    draw_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, line_idx)
);

CREATE TABLE IF NOT EXISTS hkjc_1x2_half (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    label TEXT NOT NULL,
    line_idx INTEGER NOT NULL DEFAULT 0,
    home_odds REAL NOT NULL,
    draw_odds REAL NOT NULL,
    away_odds REAL NOT NULL,
    change_time TEXT,
    PRIMARY KEY (match_id, label, line_idx)
);

-- 每場入球時間（live.titan007.com/detail/{id}cn.htm 事件時間軸）。
-- minute 原文保存：'45+3'=上半場補時第3分鐘（不是48分鐘），'90+N'=下半場補時；
-- period：1=上半場（含45+N） 2=下半場（含90+N） 3=加時（91+，可有可無）。
-- goal_type：入球 / 乌龙 / 点球。side：入球一方（主/客）。seq=第N球（按時間序）。
CREATE TABLE IF NOT EXISTS match_goals (
    match_id INTEGER NOT NULL REFERENCES matches(id),
    seq INTEGER NOT NULL,
    minute TEXT NOT NULL,
    period INTEGER,
    side TEXT NOT NULL,             -- 'home'/'away'
    goal_type TEXT,                 -- '入球'/'乌龙'/'点球'
    player TEXT,
    PRIMARY KEY (match_id, seq)
);
"""


# 用戶 2026-09-29 指定優先次序（1st 易胜博七時點 → 2nd 角球統計 → 3rd 百家歐指
# → 4th 入球時間 → 5th 馬會六盤）。表達式同 crawl_extra 派任 SQL 完全一致，
# 用嚟建 partial expression index——冇索引每次派任都要 sort 過百萬行（0.2s/次，
# 幾條線道互搶 _TASK_LOCK 直接冧 throughput）。
EXTRA_PRIORITY = (
    (('ou', 'ah_half', 'ou_half', '1x2', '1x2_half', 'corner', 'asian'), 1),
    (('corners_stat',), 2),
    (('1x2all',), 3),
    (('goals',), 4),
    (('hkjc_asian', 'hkjc_ou', 'hkjc_ah_half', 'hkjc_ou_half',
      'hkjc_1x2', 'hkjc_1x2_half'), 5),
)
PRIORITY_CASE_SQL = 'CASE market ' + ' '.join(
    "WHEN '%s' THEN %d" % (m, t) for ms, t in EXTRA_PRIORITY for m in ms) + ' END'

# 2026-10-12：領任務正規化欄位用嘅市場次序——同 crawl_extra.MARKET_ORDER
# 完全一致（權威定義嗰邊，改嗰邊要同步改呢邊），market_ord 實體欄位令
# claim ORDER BY 完全行索引（冇 expression 冇 TEMP B-TREE）。
MARKET_ORD_LIST = ['asian', 'ou', 'ah_half', 'ou_half', '1x2', '1x2_half',
                   'corner', 'hkjc_asian', 'hkjc_ou', 'hkjc_ah_half',
                   'hkjc_ou_half', 'hkjc_1x2', 'hkjc_1x2_half',
                   '1x2all', 'corners_stat', 'goals']
PRIORITY_ORD_CASE = 'CASE market ' + ' '.join(
    "WHEN '%s' THEN %d" % (m, i) for i, m in enumerate(MARKET_ORD_LIST)) + ' END'


def migrate(conn):
    cols = [r[1] for r in conn.execute('PRAGMA table_info(matches)')]
    if 'is_neutral' not in cols:
        conn.execute('ALTER TABLE matches ADD COLUMN is_neutral INTEGER')
    conn.executescript(SCHEMA)
    # 2026-10-12 領任務提速：extra_tasks 正規化排序欄位（ord=聯賽次序，
    # season_label=球季，market_ord=市場優先）——舊 claim 每次對全部
    # status=0 行做 TEMP B-TREE 排序（實測 0.5s/次，6進程×24線程並發時
    # CPU 全燒喺排序上），新 claim 變純索引掃描（<1ms）。
    tcols = [r[1] for r in conn.execute('PRAGMA table_info(extra_tasks)')]
    for c in ('ord', 'season_label', 'market_ord'):
        if c not in tcols:
            conn.execute('ALTER TABLE extra_tasks ADD COLUMN %s' % c)
    if conn.execute('SELECT 1 FROM extra_tasks WHERE ord IS NULL LIMIT 1'
                    ).fetchone():
        conn.execute(
            'UPDATE extra_tasks SET '
            'ord = COALESCE('
            '(SELECT lo.ord FROM matches mm JOIN seasons ss '
            ' ON ss.id=mm.season_id '
            ' LEFT JOIN _league_order lo ON lo.titan_id=ss.titan_id '
            ' WHERE mm.id = extra_tasks.match_id), 999999), '
            'season_label = '
            '(SELECT ss.season_label FROM matches mm JOIN seasons ss '
            ' ON ss.id=mm.season_id WHERE mm.id = extra_tasks.match_id)')
        conn.execute('UPDATE extra_tasks SET market_ord = CASE market %s END '
                     'WHERE market_ord IS NULL' % PRIORITY_ORD_CASE)
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_tasks_claim4 ON extra_tasks('
        'status, attempts, ord, season_label DESC, match_id DESC, market_ord) '
        'WHERE status=0')
    # 2026-10-12 分工爬取：雲端節點搞掂嘅任務記入嚟，本機派任時跳過
    conn.execute('CREATE TABLE IF NOT EXISTS cloud_done('
                 'match_id INTEGER NOT NULL, market TEXT NOT NULL, '
                 'ts TEXT, PRIMARY KEY(match_id, market))')
    # 派任提速索引：partial（淨 status=0）。v3 配合「插隊層先行＋層內最新場次
    # 先行」嘅 ORDER BY（attempts, 市場層, match_id DESC）——v2 把 attempts
    # 排喺市場層之後，boost 咗嘅盃賽任務會被海量普通任務 bury（2026-10-08）
    conn.execute('DROP INDEX IF EXISTS idx_tasks_pending_v1')
    conn.execute('DROP INDEX IF EXISTS idx_tasks_pending_v2')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_tasks_pending_v3 ON extra_tasks('
        'attempts, ' + PRIORITY_CASE_SQL + ', match_id DESC) WHERE status=0')
    conn.commit()


if __name__ == '__main__':
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'football.db'))
    migrate(conn)
    print('結構更新完成')
    for t in ('match_corners', 'odds_ou', 'odds_ah_half', 'odds_ou_half',
              'odds_1x2', 'odds_1x2_half', 'odds_corner', 'odds_1x2_all',
              'match_derived', 'extra_tasks',
              'hkjc_asian', 'hkjc_ou', 'hkjc_ah_half', 'hkjc_ou_half',
              'hkjc_1x2', 'hkjc_1x2_half'):
        n = conn.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
        print(f'  {t}: {n} 列')
