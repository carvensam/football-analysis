# -*- coding: utf-8 -*-
"""精選8s 引擎（2026-10-14）：專搵上盤嘅篩選工具。

方法論（全部經分段回查驗證，詳見 _fw8s_rules.py）：
  基礎訊號 = check 格共識方向（①同初盤&尾盤 ⑤對賽尾盤 ⑧主主場/客客場入失球
  ⑫排名±2＋同尾盤 ⑮＋同水位區 ⑱排名差±1＋同尾盤；方向揀選同
  _featured_direction）——共識=上嘅歷史場次 2021–2026 上盤率 75.9%，
  逐年 70–82%，前後半段 77/75%。
  每聯賽獨立公式（fw8s_league_rules.json）：
    direct：共識上即入選（該聯賽歷史≥65%，n≥10）
    refine：共識上＋精煉條件（該聯賽歷史<65%，挖條件補上）
    watch ：歷史唔夠/太差，唔入選（介面照顯示觀望狀態）
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

import screen_engine

_GMAP = {'home': 1, 'away': -1, 'none': 0}
_EMPTY_IDX = np.zeros(0, dtype=np.int64)

RULES_PATH = Path(__file__).resolve().parent / 'fw8s_league_rules.json'
_rules_cache = {'ts': 0.0, 'rules': {}}
_rules_lock = threading.Lock()


def load_rules():
    now = time.time()
    if now - _rules_cache['ts'] < 300:
        return _rules_cache['rules']
    with _rules_lock:
        if now - _rules_cache['ts'] < 300:
            return _rules_cache['rules']
        rules = {}
        if RULES_PATH.exists():
            rules = json.loads(RULES_PATH.read_text(encoding='utf-8'))
        _rules_cache['rules'] = rules
        _rules_cache['ts'] = time.time()
        return rules


def save_rules(rules):
    with _rules_lock:
        RULES_PATH.write_text(json.dumps(rules, ensure_ascii=False, indent=1),
                              encoding='utf-8')
        _rules_cache['rules'] = rules
        _rules_cache['ts'] = time.time()


# ---------- 方向計算（同 app.py _check_scan_job 一致嘅口徑） ----------

def _idx_rates(idx, res):
    if idx is None or idx.size == 0:
        return None
    n = int(idx.size)
    r = res[idx]
    a = int((r == 1).sum())
    p = int((r == 0).sum())
    eff = n - p
    return {'n': n, 'up_r': a / eff if eff else None,
            'down_r': (eff - a) / eff if eff else None}


def _lg_rates(idx, res, lg, t_lg):
    if idx is None or idx.size == 0:
        return None
    return _idx_rates(idx[lg[idx] == t_lg], res)


def _featured_direction(b, gates=True):
    """同 app._featured_direction（七重準則，2026-09-19 最終版）。"""
    if not b:
        return None

    def rate(oc, d):
        if not oc:
            return None
        return oc.get('up_r') if d == 'up' else oc.get('down_r')

    def ok(oc_all, oc_lg, d):
        for oc in (oc_all, oc_lg):
            r = rate(oc, d)
            if r is not None and r >= 0.5:
                return True
        return False

    def dir_pick(d):
        found = False
        for k in ('i1', 'i5'):
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
    for k in ('i1', 'i5', 'i8'):
        it = b.get(k) or {}
        if not ok(it.get('all'), it.get('lg'), d):
            return None
    i12 = b.get('i12') or {}
    if not ok(i12.get('cur'), (i12.get('lg') or {}).get('cur'), d):
        return None
    i15 = b.get('i15') or {}
    if not ok(i15.get('zone'), i15.get('lg_zone'), d):
        return None
    i18 = b.get('i18') or {}
    if not ok(i18.get('all'), i18.get('lg'), d):
        return None
    return d


class _PoolIdx:
    """歷史池索引（照抄 _check_scan_job 預建索引邏輯，O(組) 查詢）。"""

    def __init__(self, df):
        n = len(df)
        self.n = n
        self.res = df['res'].map({'A': 1, 'B': -1, 'P': 0}).fillna(0).to_numpy()
        self.lg = df['league'].astype('category').cat.codes.to_numpy()
        self.categories = list(df['league'].astype('category').cat.categories)
        self.lg_code = {nm: i for i, nm in enumerate(self.categories)}
        self.i_ho = df['i_ho'].to_numpy()
        self.i_ao = df['i_ao'].to_numpy()
        self.c_h = df['c_h'].to_numpy()
        self.c_ho = df['c_ho'].to_numpy()
        self.c_ao = df['c_ao'].to_numpy()
        self.c_gc = df['c_g'].fillna('none').map(_GMAP).to_numpy()
        self.pv_ho = df['pv_ho'].to_numpy()
        self.pv_ao = df['pv_ao'].to_numpy()
        self.pv_gc = df['pv_g'].fillna('none').map(_GMAP).to_numpy()
        self.zone = df['up_zone'].to_numpy()
        ihl = df['i_h'].tolist()
        igl = df['i_g'].fillna('none').map(_GMAP).tolist()
        chl = df['c_h'].tolist()
        cgl = self.c_gc.tolist()
        pvl = df['pv_h'].tolist()
        pgl = self.pv_gc.tolist()
        self.g1 = {}
        for i in range(n):
            self.g1.setdefault((ihl[i], igl[i], chl[i], cgl[i]), []).append(i)
        for k in self.g1:
            self.g1[k] = np.asarray(self.g1[k], dtype=np.int64)
        self.g5 = {}
        for i in range(n):
            h5 = pvl[i]
            if h5 == h5:
                self.g5.setdefault((h5, pgl[i]), []).append(i)
        for k in self.g5:
            self.g5[k] = np.asarray(self.g5[k], dtype=np.int64)
        hh_p = df['home_home_p'].to_numpy()
        aa_p = df['away_away_p'].to_numpy()
        v8 = (hh_p > 0) & (aa_p > 0)
        self.i8v = np.nonzero(v8)[0]
        self.hh_gf = df['home_home_gf'].to_numpy()
        self.hh_ga = df['home_home_ga'].to_numpy()
        self.hh_gd = df['home_home_gd'].to_numpy()
        self.aa_gf = df['away_away_gf'].to_numpy()
        self.aa_ga = df['away_away_ga'].to_numpy()
        self.aa_gd = df['away_away_gd'].to_numpy()
        hhr = df['home_home_rank'].to_numpy()
        aar = df['away_away_rank'].to_numpy()
        self.hhr = hhr
        self.aar = aar
        v12 = ~(np.isnan(hhr) | np.isnan(aar))
        self.g12 = {}
        for i in np.nonzero(v12)[0].tolist():
            self.g12.setdefault((int(hhr[i]), int(aar[i])), []).append(i)
        for k in self.g12:
            self.g12[k] = np.asarray(self.g12[k], dtype=np.int64)
        gap = hhr - aar
        self.g18 = {}
        for i in np.nonzero(v12)[0].tolist():
            self.g18.setdefault(int(gap[i]), []).append(i)
        for k in self.g18:
            self.g18[k] = np.asarray(self.g18[k], dtype=np.int64)

    def direction(self, t):
        """t: dict（尾盤 h/g/ho/ao、聯賽名、排名、入失球、已賽場數）。
        回傳 'up'/'down'/None。"""
        t_h, t_g = t['c_h'], _GMAP.get(t['c_g'] or 'none', 0)
        t_ho, t_ao = t['c_ho'], t['c_ao']
        t_lg = self.lg_code.get(t['league'], -1)
        cand = self.g1.get((t_h, t_g, t_h, t_g))
        if cand is not None and t_ho == t_ho and t_ao == t_ao:
            w = (np.abs(self.i_ho[cand] - t_ho) <= 0.03) & \
                (np.abs(self.i_ao[cand] - t_ao) <= 0.03) & \
                (np.abs(self.c_ho[cand] - t_ho) <= 0.03) & \
                (np.abs(self.c_ao[cand] - t_ao) <= 0.03)
            idx1 = cand[w]
        else:
            idx1 = None
        cand5 = self.g5.get((t_h, t_g))
        if cand5 is not None and t_ho == t_ho and t_ao == t_ao:
            w5 = (np.abs(self.pv_ho[cand5] - t_ho) <= 0.03) & \
                 (np.abs(self.pv_ao[cand5] - t_ao) <= 0.03)
            idx5 = cand5[w5]
        else:
            idx5 = None
        brief = {'i1': {'all': _idx_rates(idx1, self.res),
                        'lg': _lg_rates(idx1, self.res, self.lg, t_lg)},
                 'i5': {'all': _idx_rates(idx5, self.res),
                        'lg': _lg_rates(idx5, self.res, self.lg, t_lg)}}
        d = _featured_direction(brief, gates=False)
        if d:
            if t['hh_p'] > 0 and t['aa_p'] > 0:
                m8 = (np.abs(self.hh_gf[self.i8v] - t['hh_gf']) <= 3) & \
                     (np.abs(self.hh_ga[self.i8v] - t['hh_ga']) <= 3) & \
                     (np.abs(self.hh_gd[self.i8v] - t['hh_gd']) <= 3) & \
                     (np.abs(self.aa_gf[self.i8v] - t['aa_gf']) <= 3) & \
                     (np.abs(self.aa_ga[self.i8v] - t['aa_ga']) <= 3) & \
                     (np.abs(self.aa_gd[self.i8v] - t['aa_gd']) <= 3)
                idx8 = self.i8v[m8]
                brief['i8'] = {'all': _idx_rates(idx8, self.res),
                               'lg': _lg_rates(idx8, self.res, self.lg, t_lg)}

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
            hhr, aar = t['hhr'], t['aar']
            if hhr == hhr and aar == aar:    # 非 NaN
                r1, r2 = int(hhr), int(aar)
                cells = []
                for di in range(-2, 3):
                    for dj in range(-2, 3):
                        cc = self.g12.get((r1 + di, r2 + dj))
                        if cc is not None:
                            cells.append(cc)
                idx12 = np.unique(np.concatenate(cells)) if cells else _EMPTY_IDX
                m15 = idx12[(self.c_h[idx12] == t_h) & (self.c_gc[idx12] == t_g)] \
                    if idx12.size else _EMPTY_IDX
                brief['i12'] = {'n': int(idx12.size),
                                'cur': _idx_rates(m15, self.res),
                                'lg': {'cur': _lg_rates(m15, self.res, self.lg,
                                                        t_lg)}}
                up_water = t.get('up_water')
                if up_water is not None:
                    t_zone = screen_engine.zone_idx(up_water)
                    m15z = m15[self.zone[m15] == t_zone] if m15.size \
                        else _EMPTY_IDX
                    brief['i15'] = {'zone': _idx_rates(m15z, self.res),
                                    'lg_zone': _lg_rates(m15z, self.res, self.lg,
                                                         t_lg)}
                g0 = int(hhr - aar)
                c18 = []
                for dg in range(-1, 2):
                    cc = self.g18.get(g0 + dg)
                    if cc is not None:
                        c18.append(cc)
                idx18 = np.unique(np.concatenate(c18)) if c18 else _EMPTY_IDX
                m18 = idx18[(self.c_h[idx18] == t_h) & (self.c_gc[idx18] == t_g)] \
                    if idx18.size else _EMPTY_IDX
                brief['i18'] = {'all': _idx_rates(m18, self.res),
                                'lg': _lg_rates(m18, self.res, self.lg, t_lg)}
            d = _featured_direction(brief)
        return d


# ---------- 即將開賽場次 ----------

TARGET_SQL = """
SELECT m.id, m.season_id, m.kickoff, c.req_name AS league,
       m.home_score AS hs, m.away_score AS aws,
       m.home_id, m.away_id,
       m.home_rank_disp AS hr, m.away_rank_disp AS ar,
       COALESCE(c12.handicap, c3.handicap) AS c_h,
       COALESCE(c12.giver,    c3.giver)    AS c_g,
       COALESCE(c12.home_odds, c3.home_odds) AS c_ho,
       COALESCE(c12.away_odds, c3.away_odds) AS c_ao,
       COALESCE(i12.handicap, i3.handicap) AS i_h,
       COALESCE(i12.giver,    i3.giver)    AS i_g,
       COALESCE(i12.home_odds, i3.home_odds) AS i_ho,
       COALESCE(i12.away_odds, i3.away_odds) AS i_ao,
       COALESCE(a30.handicap, b30.handicap) AS m30_h,
       COALESCE(a30.giver,    b30.giver)    AS m30_g,
       COALESCE(a30.home_odds, b30.home_odds) AS m30_ho,
       COALESCE(a5.handicap, b5.handicap) AS m5_h,
       COALESCE(a5.giver,    b5.giver)    AS m5_g,
       COALESCE(a5.home_odds, b5.home_odds) AS m5_ho,
       COALESCE(a5.away_odds, b5.away_odds) AS m5_ao,
       ou.total_line AS ou_l, ou.over_odds AS ou_o,
       ht.name_tc AS home, at.name_tc AS away,
       m.is_neutral
FROM matches m
JOIN seasons s ON s.id = m.season_id
JOIN competitions c ON c.titan_id = s.titan_id
JOIN teams ht ON ht.titan_id = m.home_id
JOIN teams at ON at.titan_id = m.away_id
LEFT JOIN odds_asian c12 ON c12.match_id = m.id AND c12.label = 'closing' AND c12.company_id = 12
LEFT JOIN odds_asian c3  ON c3.match_id  = m.id AND c3.label  = 'closing' AND c3.company_id  = 3
LEFT JOIN odds_asian i12 ON i12.match_id = m.id AND i12.label = 'initial' AND i12.company_id = 12
LEFT JOIN odds_asian i3  ON i3.match_id  = m.id AND i3.label  = 'initial' AND i3.company_id  = 3
LEFT JOIN odds_asian a30 ON a30.match_id = m.id AND a30.label = 'pre_30m' AND a30.company_id = 12
LEFT JOIN odds_asian b30 ON b30.match_id = m.id AND b30.label = 'pre_30m' AND b30.company_id = 3
LEFT JOIN odds_asian a5  ON a5.match_id  = m.id AND a5.label  = 'pre_5m'  AND a5.company_id  = 12
LEFT JOIN odds_asian b5  ON b5.match_id  = m.id AND b5.label  = 'pre_5m'  AND b5.company_id  = 3
LEFT JOIN odds_ou ou ON ou.match_id = m.id AND ou.label = 'closing' AND ou.company_id = 12
WHERE m.home_score IS NULL AND m.kickoff >= datetime('now','+8 hours')
AND m.kickoff <= datetime('now','+8 hours', ?)
AND COALESCE(c12.handicap, c3.handicap) IS NOT NULL
ORDER BY m.kickoff
"""


def _num(v, default=np.nan):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cnt(*vals):
    """非 NaN 數值相加（None/NaN 當 0）。"""
    return int(sum(0.0 if (v is None or v != v) else v for v in vals))


def _fill_pre(conn, targets):
    """即場計每場開賽前狀態（calc_target_pre 同 prematch 同口徑），
    補上 build_features／方向計算需要嘅欄位。match_prestandings 表
    只係賽後回填，即將開赛場次一定要即場計。
    效能：按球季分組， season 賽果一次過載入，按開賽時間順序邊行邊
    累積狀態——O(球季場數＋目標場數)，唔係每場重新掃成季。"""
    import prematch
    cols = {'htw': 'home_total_w', 'htd': 'home_total_d', 'htl': 'home_total_l',
            'atw': 'away_total_w', 'atd': 'away_total_d', 'atl': 'away_total_l',
            'hhw': 'home_home_w', 'hhd': 'home_home_d', 'hhl': 'home_home_l',
            'aaw': 'away_away_w', 'aad': 'away_away_d', 'aal': 'away_away_l',
            'hhr': 'home_home_rank', 'aar': 'away_away_rank',
            'hh_gf': 'home_home_gf', 'hh_ga': 'home_home_ga',
            'hh_gd': 'home_home_gd',
            'aa_gf': 'away_away_gf', 'aa_ga': 'away_away_ga',
            'aa_gd': 'away_away_gd'}
    for c in cols:
        targets[c] = np.nan
    # 一次過載所有相關球季賽果（避免每季一條 SQL 全表掃描）
    sids = ','.join(str(int(s)) for s in targets['season_id'].unique())
    all_rows = conn.execute(
        'SELECT season_id, id, kickoff, home_id, away_id, home_score, '
        f'away_score FROM matches WHERE home_score IS NOT NULL '
        f'AND season_id IN ({sids}) ORDER BY kickoff, id').fetchall()
    by_season = {}
    for r in all_rows:
        by_season.setdefault(r[0], []).append(r)
    for sid, grp in targets.groupby('season_id'):
        rows = [(r[1], r[2], r[3], r[4], r[5], r[6]) for r in by_season.get(int(sid), [])]
        stats = {}
        for _id, _ko, hid, aid, _hs, _aws in rows:
            stats.setdefault(hid, prematch._new_stats())
            stats.setdefault(aid, prematch._new_stats())
        g_sorted = grp.sort_values('kickoff')
        ri = 0
        for i, trow in g_sorted.iterrows():
            while ri < len(rows) and rows[ri][1] < trow['kickoff']:
                _, _, hid, aid, hs, aws = rows[ri]
                prematch._apply(stats, hid, 'total', hs, aws)
                prematch._apply(stats, hid, 'home', hs, aws)
                prematch._apply(stats, aid, 'total', aws, hs)
                prematch._apply(stats, aid, 'away', aws, hs)
                ri += 1
            snap = prematch._snapshot(stats)
            pre = {}
            for side, tid in (('home', int(trow['home_id'])),
                              ('away', int(trow['away_id']))):
                for scope in ('total', 'home', 'away'):
                    info = snap.get(tid, {}).get(scope)
                    if info:
                        rank, gf, ga, w, d, l, pts = info
                    else:
                        rank, gf, ga, w, d, l = None, 0, 0, 0, 0, 0
                    gd = gf - ga
                    pre[f'{side}_{scope}_rank'] = rank
                    pre[f'{side}_{scope}_gf'] = gf
                    pre[f'{side}_{scope}_ga'] = ga
                    pre[f'{side}_{scope}_gd'] = gd
                    pre[f'{side}_{scope}_w'] = w
                    pre[f'{side}_{scope}_d'] = d
                    pre[f'{side}_{scope}_l'] = l
            for c, pk in cols.items():
                targets.loc[i, c] = pre.get(pk)
    return targets


def scan_upcoming(conn, hours=72):
    """掃未來 `hours` 小時：逐場計共識方向，
    按各聯賽公式過濾，回傳 (picks, watched, stats)。"""
    import pandas as pd
    df_pool, _ = screen_engine.load_pool(conn)
    idx = _PoolIdx(df_pool)
    rules = load_rules()
    targets = pd.read_sql(TARGET_SQL, conn, params=(f'+{hours} hours',))
    if not len(targets):
        return [], [], league_stats(conn)
    targets = _fill_pre(conn, targets)
    # 統一用 build_features 之後嘅 tf 做迭代（cond 遮罩同列對齊）
    import _mine8s
    tf = _mine8s.build_features(targets.copy())
    cond_masks = {}
    refine_leagues = {lg: r for lg, r in rules.items()
                      if r.get('mode') == 'refine' and r.get('conds')}
    if refine_leagues:
        for k, nm, m in _mine8s.cond_defs(tf):
            cond_masks[k] = m.to_numpy()
    picks, watched = [], []
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for i, row in tf.iterrows():
        t = {
            'c_h': None if row['c_h'] != row['c_h'] else float(row['c_h']),
            'c_g': row['c_g'],
            'c_ho': _num(row['c_ho']), 'c_ao': _num(row['c_ao']),
            'league': row['league'],
            'hh_p': _cnt(row['hhw'], row['hhd'], row['hhl']),
            'aa_p': _cnt(row['aaw'], row['aad'], row['aal']),
            'hh_gf': _num(row['hh_gf']), 'hh_ga': _num(row['hh_ga']),
            'hh_gd': _num(row['hh_gd']),
            'aa_gf': _num(row['aa_gf']), 'aa_ga': _num(row['aa_ga']),
            'aa_gd': _num(row['aa_gd']),
            'hhr': _num(row['hhr']), 'aar': _num(row['aar']),
        }
        if t['c_h'] is None:
            continue
        gv = t['c_g'] or 'none'
        if gv == 'home':
            t['up_water'] = t['c_ho']
        elif gv == 'away':
            t['up_water'] = t['c_ao']
        else:
            ho, ao = t['c_ho'], t['c_ao']
            t['up_water'] = min(ho, ao) if ho == ho and ao == ao else None
        try:
            d = idx.direction(t)
        except Exception:
            d = None
        rule = rules.get(row['league'], {})
        mode = rule.get('mode', 'watch')
        od_ok = t['c_ho'] == t['c_ho'] and t['c_ao'] == t['c_ao']
        od = (f"主{t['c_ho']:.2f}/客{t['c_ao']:.2f}" if od_ok else None)
        rec = {'id': int(row['id']), 'kickoff': row['kickoff'],
               'league': row['league'], 'home': row['home'],
               'away': row['away'],
               'line': screen_engine.fmt_line(t['c_h'], t['c_g']),
               'odds': od, 'direction': d, 'mode': mode,
               'hist_n': rule.get('hist_n', 0),
               'hist_up_r': rule.get('hist_up_r'),
               'conds': rule.get('conds') if mode == 'refine' else None,
               'refine': rule.get('refine') if mode == 'refine' else None,
               'scanned_at': now}
        if d == 'up' and mode in ('direct', 'refine'):
            if mode == 'refine':
                miss = [c for c in (rule.get('conds') or [])
                        if c not in cond_masks or not cond_masks[c][i]]
                if miss:
                    rec['mode'] = 'refine_miss'
                    rec['miss_conds'] = miss
                    watched.append(rec)
                    continue
            picks.append(rec)
            _log_pick(conn, rec)
        elif d == 'up':
            watched.append(rec)
    stats = league_stats(conn)
    return picks, watched, stats


def _log_pick(conn, rec):
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS fw8s_log(
            match_id INTEGER PRIMARY KEY, league TEXT, direction TEXT,
            mode TEXT, conds TEXT, line TEXT, odds TEXT,
            added_at TEXT, result TEXT, settled_at TEXT)''')
        conn.execute(
            'INSERT INTO fw8s_log(match_id, league, direction, mode, conds, '
            'line, odds, added_at) VALUES(?,?,?,?,?,?,?,?) '
            'ON CONFLICT(match_id) DO UPDATE SET direction=excluded.direction, '
            'mode=excluded.mode, conds=excluded.conds, line=excluded.line, '
            'odds=excluded.odds',
            (rec['id'], rec['league'], 'up', rec['mode'],
             json.dumps(rec.get('conds') or [], ensure_ascii=False),
             rec.get('line'), rec.get('odds'), rec['scanned_at']))
        conn.commit()
    except Exception:
        pass


def league_stats(conn):
    """每聯賽歷史共識上命中率（check_rows 分段口徑）＋公式狀態。"""
    rows = conn.execute("""
        SELECT c.req_name, cr.res, COUNT(*) FROM check_rows cr
        JOIN matches m ON m.id = cr.match_id
        JOIN seasons s ON s.id = m.season_id
        JOIN competitions c ON c.titan_id = s.titan_id
        WHERE cr.direction = 'up'
        GROUP BY c.req_name, cr.res""").fetchall()
    per = {}
    for lg, res, n in rows:
        e = per.setdefault(lg, {'A': 0, 'B': 0, 'P': 0})
        e[res] = n
    rules = load_rules()
    out = {}
    for lg, e in per.items():
        eff = e['A'] + e['B']
        r = rules.get(lg, {})
        out[lg] = {'hist_n': eff, 'hist_up_r': round(e['A'] / eff, 4) if eff else None,
                   'mode': r.get('mode', 'watch'),
                   'conds': r.get('conds')}
    return out
