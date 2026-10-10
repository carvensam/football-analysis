# -*- coding: utf-8 -*-
"""半全場方法引擎（2026-10-10）——分聯賽 HT/FT 方法版預測（「半全場方法」頁）。

方法卡：htft_rules.json（_htft_miner.py 貪婪挖掘定稿，15 條方法 12 個聯賽）。
特徵原語（z6/mv5/lineb/pz/depth_b/atoms）同 _htft_miner.py 逐字一致——礦工模組
import 即行主流程，所以只可以抄函數，唔可以直接 import。

scan(hours=48) 回傳：
  upcoming: 未來 N 小時已排程場次，逐場攞 company 12 盤口快照（全場 O/U 初盤/
            尾盤/臨場＋亞盤＋半場 O/U＋半場亞盤＋歐盤＋半場歐盤），原子集合
            命中方法→每個命中方法獨立一張 pick 卡（HT/FT 唔設方向衝突觀望）；
  played:   近 7 日已完場場次，同樣逐場評；已 fire 方法帶 half_home/half_away/
            home_score/away_score、判定 cell（HH..AA：半場主客×全場主客）同
            settle（贏／輸，口徑同礦工：cell ∈ TARGETS[target]，
            XH={HD,HA}、XA={AD,AA}）。
引擎係純 SQL＋方法評估、無狀態表——每次 scan 全量重算，結果放 app.py module cache。
"""
import json
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB = os.environ.get('DB_PATH') or os.path.join(PARENT_DIR, 'football.db')
RULES_PATH = os.path.join(BASE_DIR, 'htft_rules.json')

# 尾盤快照優先序：closing 冇就用臨場前最新一份（同 ou_rules_engine 口徑）
TAIL_LABELS = ('closing', 'pre_5m', 'pre_10m', 'pre_15m', 'pre_30m', 'pre_4h')
PRE4H_LABEL = 'pre_4h'
PLAYED_DAYS = 7   # 已完場結算窗口（近 7 日）

# ---------------------------------------------------------------- 特徵原語
# 以下六個同 _htft_miner.py 逐字一致（礦工 import 會行主流程，唔可以 import）
def z6(x):
    if x is None: return None
    if x < 0.75: return '<0.75'
    if x < 0.85: return '0.75-0.85'
    if x < 0.95: return '0.85-0.95'
    if x < 1.05: return '0.95-1.05'
    if x < 1.15: return '1.05-1.15'
    return '>=1.15'

def mv5(a, b):
    if a is None or b is None: return None
    d = b - a
    if d > 0.375: return '大升'
    if d > 0.125: return '升'
    if d >= -0.125: return '平'
    if d >= -0.375: return '降'
    return '大降'

def lineb(x, fmt='%.2f'):
    return fmt % (round(x * 4) / 4) if x is not None else None

def pz(p):
    if p < 0.30: return '<30%'
    if p < 0.40: return '30-40%'
    if p < 0.50: return '40-50%'
    if p < 0.60: return '50-60%'
    if p < 0.70: return '60-70%'
    return '>=70%'

def depth_b(x):
    if x < 0.25: return '平手'
    if x < 0.75: return '淺'
    if x < 1.25: return '中'
    if x < 1.75: return '深'
    return '超深'

def atoms(r):
    (mid, ko, lg, hh, ha, hs, aws, cl, co, cu, il, io_, iu, f4l, f4o, f4u,
     ahl, aho, agv, ahil, ohl, oho, ohu, ahl2, agv2, aho2, x1, x2, x3,
     xh1, xh2, xh3) = r
    A = set()
    if cl is not None: A.add('尾線=' + lineb(cl))
    if il is not None: A.add('初線=' + lineb(il))
    m = mv5(il, cl)
    if m: A.add('線變=' + m)
    for k, v in (('尾水', z6(co)), ('初水', z6(io_)), ('尾細水', z6(cu)),
                 ('細初水', z6(iu)), ('水變', mv5(io_, co)), ('細水變', mv5(iu, cu)),
                 ('臨線變', mv5(f4l, cl)), ('臨水變', mv5(f4o, co)),
                 ('臨細水變', mv5(f4u, cu))):
        if v: A.add('%s=%s' % (k, v))
    if co is not None and cu is not None and co > 0 and cu > 0:
        qo, qu = 1.0 / co, 1.0 / cu
        A.add('隱大率=' + pz(qo / (qo + qu)))
    if ahl is not None and agv in ('home', 'away'):
        sgn = ahl if agv == 'home' else -ahl
        A.add('亞向=' + ('主讓' if sgn > 0.05 else ('客讓' if sgn < -0.05 else '平手')))
        A.add('亞深=' + depth_b(abs(sgn)))
        if aho is not None: A.add('亞主水=' + z6(aho))
        if ahil is not None:
            isgn = ahil if agv == 'home' else -ahil
            m2 = mv5(isgn, sgn)
            if m2: A.add('亞線變=' + m2)
    if ohl is not None: A.add('半線=' + lineb(ohl, '%.2f'))
    if oho is not None: A.add('半大水=' + z6(oho))
    if ahl2 is not None and agv2 in ('home', 'away'):
        s2 = ahl2 if agv2 == 'home' else -ahl2
        A.add('半讓=' + ('主讓' if s2 > 0.05 else ('客讓' if s2 < -0.05 else '平手'))
              + depth_b(abs(s2)))
        if aho2 is not None: A.add('半讓水=' + z6(aho2))
    if x1 and x2 and x3 and x1 > 0 and x2 > 0 and x3 > 0:
        s = 1.0 / x1 + 1.0 / x2 + 1.0 / x3
        A.add('歐主=' + pz((1.0 / x1) / s))
        A.add('歐和=' + pz((1.0 / x2) / s))
        A.add('歐客=' + pz((1.0 / x3) / s))
    if xh1 and xh2 and xh3 and xh1 > 0 and xh2 > 0 and xh3 > 0:
        s = 1.0 / xh1 + 1.0 / xh2 + 1.0 / xh3
        A.add('半歐主=' + pz((1.0 / xh1) / s))
        A.add('半歐和=' + pz((1.0 / xh2) / s))
    return A

# 目標：9 格 + 2 個 spec 貓（半場領先方最終唔贏）——同礦工逐字一致
CELLS = ('HH', 'HD', 'HA', 'DH', 'DD', 'DA', 'AH', 'AD', 'AA')
TARGETS = {c: {c} for c in CELLS}
TARGETS['XH'] = {'HD', 'HA'}   # 半主→全唔贏
TARGETS['XA'] = {'AD', 'AA'}   # 半客→全唔贏

def cell(hh, ha, hs, aws):
    h = 0 if hh > ha else (1 if hh == ha else 2)
    f = 0 if hs > aws else (1 if hs == aws else 2)
    return 'HDHADDAAHAD HAA'[0] if False else CELLS[h * 3 + f]

# ---------------------------------------------------------------- 方法卡
def load_methods():
    """讀方法卡：{'methods': [...]}，逐條 {id, lg, conds, target, target_name,
    train, wf, wins}。conds 係字串列表（如 '半讓=客讓超深'），全部係 atoms 入面嘅原子。"""
    with open(RULES_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg.get('methods') or [], cfg


def league_methods(methods, req_name):
    """聯賽對應：method.lg 做 req_name 前綴匹配（同 ou league_rules 思路）。"""
    if not req_name:
        return []
    return [m for m in methods if m.get('lg') and req_name.startswith(m['lg'])]

# ---------------------------------------------------------------- 盤口快照
def snapshots(conn, mid):
    """攞 company 12 各盤快照，砌成礦工 SQL 順序嘅 32 格 tuple 直接餓 atoms()。
    全場 O/U：initial + 尾盤（TAIL_LABELS 優先序）+ pre_4h；亞盤：closing +
    initial(handicap)；半場 O/U、半場亞盤、歐盤、半場歐盤全部淨係 closing。
    頭 7 格（ko/lg/半全場比分）atoms 用唔住，一律 None。"""
    rows = conn.execute(
        'SELECT label, total_line, over_odds, under_odds FROM odds_ou '
        'WHERE match_id=? AND company_id=12', (mid,)).fetchall()
    by = {}
    for label, tl, oo, uo in rows:
        by[label] = (tl, oo, uo)
    init = by.get('initial')
    tail = None
    for lb in TAIL_LABELS:
        s = by.get(lb)
        if s is not None and s[0] is not None:
            tail = s
            break
    pre4h = by.get(PRE4H_LABEL)
    ah = None
    ah_rows = conn.execute(
        "SELECT label, handicap, home_odds, giver FROM odds_asian "
        "WHERE match_id=? AND company_id=12 AND label IN ('closing','initial')",
        (mid,)).fetchall()
    ah_by = {lb: (hc, ho, gv) for lb, hc, ho, gv in ah_rows}
    if 'closing' in ah_by:
        ahl, aho, agv = ah_by['closing']
        ahil = ah_by.get('initial', (None, None, None))[0]
        ah = (ahl, aho, agv, ahil)
    ohl = oho = ohu = None
    r = conn.execute(
        "SELECT total_line, over_odds, under_odds FROM odds_ou_half "
        "WHERE match_id=? AND company_id=12 AND label='closing'", (mid,)).fetchone()
    if r:
        ohl, oho, ohu = r
    ahl2 = agv2 = aho2 = None
    r = conn.execute(
        "SELECT handicap, giver, home_odds FROM odds_ah_half "
        "WHERE match_id=? AND company_id=12 AND label='closing'", (mid,)).fetchone()
    if r:
        ahl2, agv2, aho2 = r
    x1 = x2 = x3 = None
    r = conn.execute(
        "SELECT home_odds, draw_odds, away_odds FROM odds_1x2 "
        "WHERE match_id=? AND company_id=12 AND label='closing'", (mid,)).fetchone()
    if r:
        x1, x2, x3 = r
    xh1 = xh2 = xh3 = None
    r = conn.execute(
        "SELECT home_odds, draw_odds, away_odds FROM odds_1x2_half "
        "WHERE match_id=? AND company_id=12 AND label='closing'", (mid,)).fetchone()
    if r:
        xh1, xh2, xh3 = r
    cl, co, cu = tail if tail else (None, None, None)
    il, io_, iu = init if init else (None, None, None)
    f4l, f4o, f4u = pre4h if pre4h else (None, None, None)
    ahl = aho = agv = ahil = None
    if ah:
        ahl, aho, agv, ahil = ah
    return (mid, None, None, None, None, None, None,
            cl, co, cu, il, io_, iu, f4l, f4o, f4u,
            ahl, aho, agv, ahil, ohl, oho, ohu, ahl2, agv2, aho2,
            x1, x2, x3, xh1, xh2, xh3)


def eval_match(methods, req_name, snaps):
    """單場評方法：回傳 (hits, tail)。conds 全部喺 atoms 度先算命中；
    多方法命中各自獨立出卡（HT/FT 唔設方向衝突觀望）。
    無方法／無尾盤→([], None)。"""
    ms = league_methods(methods, req_name)
    if not ms:
        return [], None
    tail = (snaps[7], snaps[8], snaps[9])
    if tail[0] is None:
        return [], None
    A = atoms(snaps)
    hits = [m for m in ms if all(c in A for c in (m.get('conds') or []))]
    return hits, tail


def build_card(row, m):
    """pick 卡。row 前 5 格係 (id, kickoff, 聯賽, 主, 客)；played 行後面帶埋
    半全場比分，所以只攞頭 5 格。一個 method 一張卡。"""
    (mid, ko, lg, h, a) = row[:5]
    return {'match_id': mid, 'kickoff': ko, 'league': lg,
            'home': h, 'away': a,
            'method_id': m.get('id'), 'target': m.get('target'),
            'target_name': m.get('target_name'),
            'conds': m.get('conds') or [],
            'cond_text': '、'.join(m.get('conds') or []),
            'train': m.get('train'), 'wf': m.get('wf'),
            'wins': m.get('wins') or {}}

# ---------------------------------------------------------------- 主入口
def _lg_filter(methods):
    """方法聯賽前綴 SQL 過濾（LIKE '前綴%' ≡ startswith）——查詢只聚焦方法聯賽。"""
    lgs = sorted({m.get('lg') for m in methods if m.get('lg')})
    if not lgs:
        return '', ()
    cond = ' OR '.join('c.req_name LIKE ?' for _ in lgs)
    return 'AND (' + cond + ')', tuple(k + '%' for k in lgs)


def scan(hours=48):
    """未來 hours 小時已排程場次＋近 7 日已完場場次，逐場評方法。

    回傳 {'upcoming': [...], 'played': [...], 'stats': {...}}；
    played 卡多帶 half_home/half_away/home_score/away_score/cell/settle。
    """
    methods, meta = load_methods()
    lg_sql, lg_args = _lg_filter(methods)
    conn = sqlite3.connect(DB, timeout=60)
    try:
        up_rows = conn.execute(
            'SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc '
            'FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'WHERE m.home_score IS NULL '
            "AND m.kickoff > datetime('now','+8 hours') "
            "AND m.kickoff <= datetime('now','+8 hours', ?) " + lg_sql +
            ' ORDER BY m.kickoff', ('+%d hours' % hours,) + lg_args).fetchall()
        played_rows = conn.execute(
            'SELECT m.id, m.kickoff, c.req_name, ht.name_tc, at.name_tc, '
            'm.half_home, m.half_away, m.home_score, m.away_score '
            'FROM matches m '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id '
            'WHERE m.home_score IS NOT NULL '
            "AND m.kickoff >= datetime('now','+8 hours', ?) "
            "AND m.kickoff < datetime('now','+8 hours') " + lg_sql +
            ' ORDER BY m.kickoff DESC', ('-%d days' % PLAYED_DAYS,) + lg_args).fetchall()
        upcoming, played = [], []
        w = l = 0
        for row in up_rows:
            try:
                snaps = snapshots(conn, row[0])
                hits, tail = eval_match(methods, row[2], snaps)
                for m in hits:
                    upcoming.append(build_card(row, m))
            except Exception:
                continue
        for row in played_rows:
            try:
                snaps = snapshots(conn, row[0])
                hits, tail = eval_match(methods, row[2], snaps)
                if not hits:
                    continue
                hh, ha, hs, aws = row[5], row[6], row[7], row[8]
                c = cell(hh, ha, hs, aws)
                for m in hits:
                    card = build_card(row, m)
                    card['half_home'] = hh
                    card['half_away'] = ha
                    card['home_score'] = hs
                    card['away_score'] = aws
                    card['cell'] = c
                    st = '贏' if c in TARGETS[m['target']] else '輸'
                    card['settle'] = st
                    w += (st == '贏')
                    l += (st == '輸')
                    played.append(card)
            except Exception:
                continue
        stats = {'scanned': len(up_rows) + len(played_rows),
                 'upcoming': len(upcoming), 'played': len(played),
                 'wins': w, 'losses': l,
                 'hit_rate': (w / (w + l)) if (w + l) else None}
        return {'upcoming': upcoming, 'played': played, 'stats': stats,
                'rules_updated': meta.get('updated'),
                'rules_note': meta.get('note'),
                'generated': time.strftime('%Y-%m-%d %H:%M:%S')}
    finally:
        conn.close()


if __name__ == '__main__':
    # 直接行 = 煙霧測試：python htft_rules_engine.py [hours]
    import sys
    r = scan(int(sys.argv[1]) if len(sys.argv) > 1 else 48)
    print('upcoming: %d  played: %d  stats: %s' % (
        len(r['upcoming']), len(r['played']), r['stats']))
    for c in r['upcoming'][:3] + r['played'][:3]:
        print(json.dumps(c, ensure_ascii=False))
