# -*- coding: utf-8 -*-
"""大小規則引擎（2026-10-09）——分聯賽 O/U 規則版預測（「大小規則」頁）。

規則卡：ou_rules.json（_ou_league_miner.py walk-forward 挖掘定稿，8 條規則 5 個聯賽）。
特徵原語（z6/mv5/atoms）同 _ou_league_miner.py 逐字一致——礦工模組 import 即行主流程，
所以只可以抄函數，唔可以直接 import。

scan(hours=48) 回傳：
  upcoming: 未來 N 小時已排程場次，逐場攞 company 12 盤口快照（初盤=initial；
            尾盤=closing，冇就用 pre_5m/pre_10m/pre_15m/pre_30m/pre_4h 最新一份；
            臨場=pre_4h 可冇），原子集合命中規則→出 pick 卡；
            方向衝突或無規則→觀望唔出（同礦工 apply 一致）。
  played:   近 7 日已完場場次，同樣逐場評；已 fire 嘅規則帶 settle（贏／走／輸，
            口徑同礦工 result()：大=總入球>尾線，細 相反，相等=走）。
引擎係純 SQL＋規則評估、無狀態表——每次 scan 全量重算，結果放 app.py module cache。
"""
import json
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
DB = os.environ.get('DB_PATH') or os.path.join(PARENT_DIR, 'football.db')
RULES_PATH = os.path.join(BASE_DIR, 'ou_rules.json')

# 尾盤快照優先序：closing 冇就用臨場前最新一份（礦工口徑係 closing 為主，呢個係延伸）
TAIL_LABELS = ('closing', 'pre_5m', 'pre_10m', 'pre_15m', 'pre_30m', 'pre_4h')
PRE4H_LABEL = 'pre_4h'
PLAYED_DAYS = 7   # 已完場結算窗口（近 7 日）

# ---------------------------------------------------------------- 特徵原語
# 以下三個同 _ou_league_miner.py 逐字一致（礦工 import 會行主流程，唔可以 import）
def z6(x):
    if x is None:
        return None
    if x < 0.75: return '<0.75'
    if x < 0.85: return '0.75-0.85'
    if x < 0.95: return '0.85-0.95'
    if x < 1.05: return '0.95-1.05'
    if x < 1.15: return '1.05-1.15'
    return '>=1.15'


def mv5(a, b):
    """a→b 變化 5 檔（缺數據=None）。"""
    if a is None or b is None:
        return None
    d = b - a
    if d > 0.375: return '大升'
    if d > 0.125: return '升'
    if d >= -0.125: return '平'
    if d >= -0.375: return '降'
    return '大降'


def pzone(p):
    if p < 0.42: return '<42%'
    if p < 0.46: return '42-46%'
    if p < 0.50: return '46-50%'
    if p < 0.54: return '50-54%'
    if p < 0.58: return '54-58%'
    return '>=58%'

def rateline(r):
    if r < 0.90: return '<90%'
    if r < 0.93: return '90-93%'
    if r < 0.95: return '93-95%'
    if r < 0.97: return '95-97%'
    return '>=97%'

def dzone(d):
    if d <= -0.30: return '<=-0.30'
    if d <= -0.15: return '-0.30~-0.15'
    if d <= 0.0: return '-0.15~0'
    if d <= 0.15: return '0~0.15'
    if d <= 0.30: return '0.15~0.30'
    return '>=0.30'

def depth(x):
    if x < 0.25: return '平手'
    if x < 0.75: return '淺0.25-0.75'
    if x < 1.25: return '中0.75-1.25'
    if x < 1.75: return '深1.25-1.75'
    return '超深>=1.75'


def atoms(cl, co, cu, il, io_, iu, f4l, f4o, f4u, ah=None):
    """一場嘅條件原子集合——格式同礦工 v3 atoms() 逐字一致。
    pre_4h 缺→臨場三個 atom 唔出；AH 缺 giver→成組 M3 唔出。
    ah = (handicap, home_odds, giver, initial_handicap)。"""
    A = set()
    if cl is not None:
        A.add('尾線=%.2f' % (round(cl * 4) / 4))
    if il is not None:
        A.add('初線=%.2f' % (round(il * 4) / 4))
    m = mv5(il, cl)
    if m: A.add('線變=' + m)
    for k, v in (('尾水', z6(co)), ('初水', z6(io_)), ('尾細水', z6(cu)),
                 ('細初水', z6(iu)), ('水變', mv5(io_, co)),
                 ('細水變', mv5(iu, cu)), ('臨線變', mv5(f4l, cl)),
                 ('臨水變', mv5(f4o, co)), ('臨細水變', mv5(f4u, cu))):
        if v: A.add('%s=%s' % (k, v))
    # M2 賠率結構（同礦工 v3 口徑：qo=1/odds）
    if co is not None and cu is not None and co > 0 and cu > 0:
        qo, qu = 1.0 / co, 1.0 / cu
        A.add('隱大率=' + pzone(qo / (qo + qu)))
        A.add('返還=' + rateline(1.0 / (qo + qu)))
        A.add('水差=' + dzone(co - cu))
    # M3 亞盤交叉（giver 閘同礦工一致）
    if ah:
        ahl, aho, agv, ahil = ah
        if ahl is not None and agv in ('home', 'away'):
            sgn = ahl if agv == 'home' else -ahl
            A.add('亞向=' + ('主讓' if sgn > 0.05 else ('客讓' if sgn < -0.05 else '平手')))
            A.add('亞深=' + depth(abs(sgn)))
            if aho is not None: A.add('亞主水=' + z6(aho))
            if ahil is not None:
                isgn = ahil if agv == 'home' else -ahil
                m2 = mv5(isgn, sgn)
                if m2: A.add('亞線變=' + m2)
    return A

# ---------------------------------------------------------------- 規則
def load_rules():
    """讀規則卡：{聯賽req_name前綴: {"rules": [...]}}。"""
    with open(RULES_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg.get('leagues') or {}, cfg


def league_rules(leagues_cfg, req_name):
    """聯賽對應：req_name 以規則鍵做前綴匹配（同礦工 startswith 一致）。"""
    if not req_name:
        return []
    for prefix, cfg in leagues_cfg.items():
        if req_name.startswith(prefix):
            return cfg.get('rules') or []
    return []


def _conds_hit(A, line_raw, conds):
    """規則條件合取判定：'=' 用原子桶/區匹配；'>='/'<=' 只限尾線，數值比較。"""
    for key, op, val in conds:
        if op == '=':
            if '%s=%s' % (key, val) not in A:
                return False
        elif op in ('>=', '<='):
            if key != '尾線' or line_raw is None:
                return False
            try:
                v = float(val)
            except (TypeError, ValueError):
                return False
            if op == '>=' and not line_raw >= v:
                return False
            if op == '<=' and not line_raw <= v:
                return False
        else:
            return False
    return True


def pick_side(rules, A, line_raw):
    """出手邏輯（同礦工 apply）：命中規則同方向→(該方向, 命中規則)；
    方向衝突或無規則→觀望 (None, 命中規則 or [])。"""
    hit = [r for r in rules if _conds_hit(A, line_raw, r.get('conds') or [])]
    if not hit:
        return None, []
    sides = {r.get('side') for r in hit}
    if len(sides) > 1:
        return None, hit
    return hit[0].get('side'), hit


def cond_text(conds):
    """條件人類可讀：尾細水＝0.85-0.95、尾線≥3.00。"""
    op_txt = {'=': '＝', '>=': '≥', '<=': '≤'}
    return '、'.join('%s%s%s' % (k, op_txt.get(o, o), v) for k, o, v in conds)

# ---------------------------------------------------------------- 盤口快照
def snapshots(conn, mid):
    """攞 company 12 各時點快照，回傳 (initial, tail, pre4h, ah)——前三個係
    (total_line, over_odds, under_odds) 或 None；ah = (handicap, home_odds,
    giver, initial_handicap) 或 None（M3 亞盤原子用）。"""
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
    return init, tail, by.get(PRE4H_LABEL), ah


def eval_match(leagues_cfg, req_name, snaps):
    """單場評規則：回傳 (side, fired_rules, tail)。無規則/無尾盤→(None, [], None)。"""
    rules = league_rules(leagues_cfg, req_name)
    if not rules:
        return None, [], None
    init, tail, pre4h, ah = snaps
    if tail is None:
        return None, [], None
    cl, co, cu = tail
    il = io_ = iu = None
    if init:
        il, io_, iu = init
    f4l = f4o = f4u = None
    if pre4h:
        f4l, f4o, f4u = pre4h
    A = atoms(cl, co, cu, il, io_, iu, f4l, f4o, f4u, ah)
    side, hit = pick_side(rules, A, cl)
    return side, hit, tail


def build_card(row, side, fired, tail):
    """pick 卡（同任務規定格）。row 前 5 格係 (id, kickoff, 聯賽, 主, 客)；
    played 行後面可能帶埋比分，所以只攞頭 5 格。"""
    (mid, ko, lg, h, a) = row[:5]
    cl, co, cu = tail
    r0 = fired[0]
    card = {'match_id': mid, 'kickoff': ko, 'league': lg,
            'home': h, 'away': a, 'side': side,
            'rule_id': '、'.join(str(r.get('id', '?')) for r in fired),
            'rule_text': cond_text(r0.get('conds') or []),
            'tier': r0.get('tier'), 'train': r0.get('train'),
            'wf': r0.get('wf'), 'note': r0.get('note'),
            'line': cl, 'over_odds': co, 'under_odds': cu}
    if len(fired) > 1:
        # 多條同向規則一齊 fire：條件文字逐條列（rule_id 上面已併埋）
        card['rule_text'] = '；'.join(
            '%s：%s' % (r.get('id', '?'), cond_text(r.get('conds') or []))
            for r in fired)
    return card


def settle(side, total, line):
    """結算（同礦工 result 口徑）：大=total>line 贏、細 相反、相等=走。"""
    if total is None or line is None:
        return None
    s = 1 if total > line else (-1 if total < line else 0)
    if side == '細':
        s = -s
    return '贏' if s > 0 else ('輸' if s < 0 else '走')

# ---------------------------------------------------------------- 主入口
def _lg_filter(leagues_cfg):
    """規則聯賽前綴 SQL 過濾（LIKE '前綴%' ≡ startswith）——查詢只聚焦規則聯賽。"""
    if not leagues_cfg:
        return '', ()
    cond = ' OR '.join('c.req_name LIKE ?' for _ in leagues_cfg)
    return 'AND (' + cond + ')', tuple(k + '%' for k in leagues_cfg)


def scan(hours=48):
    """未來 hours 小時已排程場次＋近 7 日已完場場次，逐場評規則。

    回傳 {'upcoming': [...], 'played': [...], 'stats': {...}}；
    played 卡多帶 score/goals/settle（已 fire 規則先評）。
    """
    leagues_cfg, meta = load_rules()
    lg_sql, lg_args = _lg_filter(leagues_cfg)
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
            'm.home_score, m.away_score '
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
        w = p = l = 0
        for row in up_rows:
            try:
                side, fired, tail = eval_match(
                    leagues_cfg, row[2], snapshots(conn, row[0]))
                if side:
                    upcoming.append(build_card(row, side, fired, tail))
            except Exception:
                continue
        for row in played_rows:
            try:
                side, fired, tail = eval_match(
                    leagues_cfg, row[2], snapshots(conn, row[0]))
                if not side:
                    continue
                card = build_card(row, side, fired, tail)
                total = row[5] + row[6]
                st = settle(side, total, card['line'])
                card['score'] = '%d-%d' % (row[5], row[6])
                card['goals'] = total
                card['settle'] = st
                w += (st == '贏'); p += (st == '走'); l += (st == '輸')
                played.append(card)
            except Exception:
                continue
        stats = {'scanned': len(up_rows) + len(played_rows),
                 'upcoming': len(upcoming), 'played': len(played),
                 'wins': w, 'pushes': p, 'losses': l,
                 'hit_rate': (w / (w + l)) if (w + l) else None}
        return {'upcoming': upcoming, 'played': played, 'stats': stats,
                'rules_updated': meta.get('updated'),
                'rules_source': meta.get('source'),
                'tiers': meta.get('tiers'),
                'generated': time.strftime('%Y-%m-%d %H:%M:%S')}
    finally:
        conn.close()


if __name__ == '__main__':
    # 直接行 = 煙霧測試：python ou_rules_engine.py [hours]
    import sys
    r = scan(int(sys.argv[1]) if len(sys.argv) > 1 else 48)
    print('upcoming: %d  played: %d  stats: %s' % (
        len(r['upcoming']), len(r['played']), r['stats']))
    for c in r['upcoming'][:3] + r['played'][:3]:
        print(json.dumps(c, ensure_ascii=False))
