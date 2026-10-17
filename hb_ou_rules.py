# -*- coding: utf-8 -*-
"""好波大小「同聯賽規律」判定（2026-10-17 用戶：不同聯賽/杯賽不同方法，
目標 82.5% 命中率）。

規則來源：礦工 v3（_mine_v3_all.json，10-10 全庫雙重驗證，71 聯賽）。
本模組兩個職責：
 1. 特征工程（z6/mv5/pzone/rateline/dzone/depth/atoms）——同礦工 v3 完全一致
    （條件字串例如「尾線=2.25」「尾細水=0.85-0.95」）。
 2. check_match(conn, mid)：對一場（未開賽都得）計 atoms 一次，逐條測該聯賽
    規則（conds 全部命中先算中），回傳最強一條 (side, rate, rule)。

規則檔 haobao_ou_rules.json 由 _verify_ou_rules.py 用當前 DB 獨立重驗後生成
（雙池 82.5% 閘：同聯賽全年份池＋全庫池，方向一致，n_lg≥3）。
"""
import json
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
RULES_JSON = os.path.join(BASE, 'haobao_ou_rules.json')

ROW_SQL = (
    "SELECT m.id, m.kickoff, c.req_name,"
    " m.home_score + m.away_score,"
    " oc.total_line, oc.over_odds, oc.under_odds,"
    " oi.total_line, oi.over_odds, oi.under_odds,"
    " o4.total_line, o4.over_odds, o4.under_odds,"
    " ah.handicap, ah.home_odds, ah.away_odds, ah.giver,"
    " ahi.handicap, ahi.home_odds"
    " FROM matches m"
    " JOIN seasons s ON s.id=m.season_id"
    " JOIN competitions c ON c.titan_id=s.titan_id"
    " JOIN odds_ou oc ON oc.match_id=m.id AND oc.label='closing' AND oc.company_id=12"
    " LEFT JOIN odds_ou oi ON oi.match_id=m.id AND oi.label='initial' AND oi.company_id=12"
    " LEFT JOIN odds_ou o4 ON o4.match_id=m.id AND o4.label='pre_4h' AND o4.company_id=12"
    " LEFT JOIN odds_asian ah ON ah.match_id=m.id AND ah.label='closing' AND ah.company_id=12"
    " LEFT JOIN odds_asian ahi ON ahi.match_id=m.id AND ahi.label='initial' AND ah.company_id=12"
    " WHERE m.id = ?"
)
# 注意：上面 ahi 行尾條件寫 ah.company_id 係礦工原裝（歷史遺留），
# 為 100% 一致 runtime 照抄——佢只影響 initial asian 的 join 條件寬鬆度。


def z6(x):
    if x is None:
        return None
    if x < 0.75:
        return '<0.75'
    if x < 0.85:
        return '0.75-0.85'
    if x < 0.95:
        return '0.85-0.95'
    if x < 1.05:
        return '0.95-1.05'
    if x < 1.15:
        return '1.05-1.15'
    return '>=1.15'


def mv5(a, b):
    if a is None or b is None:
        return None
    d = b - a
    if d > 0.375:
        return '大升'
    if d > 0.125:
        return '升'
    if d >= -0.125:
        return '平'
    if d >= -0.375:
        return '降'
    return '大降'


def pzone(p):
    if p < 0.42:
        return '<42%'
    if p < 0.46:
        return '42-46%'
    if p < 0.50:
        return '46-50%'
    if p < 0.54:
        return '50-54%'
    if p < 0.58:
        return '54-58%'
    return '>=58%'


def rateline(r):
    if r < 0.90:
        return '<90%'
    if r < 0.93:
        return '90-93%'
    if r < 0.95:
        return '93-95%'
    if r < 0.97:
        return '95-97%'
    return '>=97%'


def dzone(d):
    if d <= -0.30:
        return '<=-0.30'
    if d <= -0.15:
        return '-0.30~-0.15'
    if d <= 0.0:
        return '-0.15~0'
    if d <= 0.15:
        return '0~0.15'
    if d <= 0.30:
        return '0.15~0.30'
    return '>=0.30'


def depth(x):
    if x < 0.25:
        return '平手'
    if x < 0.75:
        return '淺0.25-0.75'
    if x < 1.25:
        return '中0.75-1.25'
    if x < 1.75:
        return '深1.25-1.75'
    return '超深>=1.75'


def atoms(r):
    (_id, ko, lg, total, cl, co, cu, il, io_, iu, f4l, f4o, f4u,
     ahl, aho, aaw, agv, ahil, ahio) = r
    A = set()
    if cl is not None:
        A.add('尾線=%.2f' % (round(cl * 4) / 4))
    if il is not None:
        A.add('初線=%.2f' % (round(il * 4) / 4))
    m = mv5(il, cl)
    if m:
        A.add('線變=' + m)
    for k, v in (('尾水', z6(co)), ('初水', z6(io_)), ('尾細水', z6(cu)),
                 ('細初水', z6(iu)), ('水變', mv5(io_, co)),
                 ('細水變', mv5(iu, cu)), ('臨線變', mv5(f4l, cl)),
                 ('臨水變', mv5(f4o, co)), ('臨細水變', mv5(f4u, cu))):
        if v:
            A.add('%s=%s' % (k, v))
    if co is not None and cu is not None and co > 0 and cu > 0:
        qo, qu = 1.0 / co, 1.0 / cu
        A.add('隱大率=' + pzone(qo / (qo + qu)))
        A.add('返還=' + rateline(1.0 / (qo + qu)))
        A.add('水差=' + dzone(co - cu))
    if ahl is not None and agv in ('home', 'away'):
        sgn = ahl if agv == 'home' else -ahl
        A.add('亞向=' + ('主讓' if sgn > 0.05 else ('客讓' if sgn < -0.05 else '平手')))
        A.add('亞深=' + depth(abs(sgn)))
        if aho is not None:
            A.add('亞主水=' + z6(aho))
        if ahil is not None:
            isgn = ahil if agv == 'home' else -ahil
            m2 = mv5(isgn, sgn)
            if m2:
                A.add('亞線變=' + m2)
    return A


def load_rules():
    try:
        with open(RULES_JSON, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_match(conn, mid):
    """該場有冇中聯賽規律。回傳 (side, rate, rule) 或 None。"""
    r = conn.execute(ROW_SQL, (mid,)).fetchone()
    if not r:
        return None
    rules = load_rules().get(r[2])
    if not rules:
        return None
    A = atoms(r)
    best = None
    for rule in rules:
        conds = rule.get('conds') or []
        if conds and all(c in A for c in conds):
            rate = min(rule.get('rate_lg') or 0, rule.get('rate_all') or 0)
            if best is None or rate > best[1]:
                best = (rule['side'], rate, rule)
    return best
