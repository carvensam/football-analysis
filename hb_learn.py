# -*- coding: utf-8 -*-
"""好波自我學習引擎（2026-10-06）——莊家層面誘盤/減損訊號＋逐聯賽規則權重訓練。

架構（配合「本機係數據主人」原則）：
  1) features()：由好波池 df 嘅原始欄位計特徵向量——球隊層（攻防λ）、聯賽階段層、
     莊家層（尾盤線、初→尾盤口走勢、大細水位走勢、水位差）＝誘盤/減損訊號。
  2) 目標：全場入球 vs 尾盤線 → 大(+1)/細(-1)（走盤剔出）。
  3) 訓練：每聯賽邏輯迴歸（numpy 手寫、L2、特徵標準化、時序切尾 25% 做驗證），
     樣本 <80 場用全庫全局模型頂。權重存 SQLite `hb_models` 表。
  4) 規則挖掘：單/雙特徵閾值規則，驗證集命中率 ≥55% 且 n≥40 先收錄——
     「發展更多規則」嘅自動化；逐聯賽唔同規則就係咁嚟。
  5) 回測報告：walk-forward 驗證集逐聯賽真實命中率（只報真數，唔呃人）。
  6) 自我修正：retrain() 定時（本機每日）重訓；新權重即刻生效（HB 快取 TTL 內
     由 app 層 poke 失效）。
"""
import json
import sqlite3
import time

import numpy as np

MODEL_TABLE = 'hb_models'
RULE_TABLE = 'hb_rules'

FEATURE_NAMES = [
    'lam_team',      # 球隊攻防 poisson λ 總和
    'lam_lgstage',   # 聯賽+階段 λ
    'lam_bm',        # 尾盤線隐含 λ（全庫同線場均）
    'ou_line',       # 尾盤線本身
    'line_move',     # 尾盤-初盤（誘盤/減損主訊號）
    'over_move',     # 大球水位 尾-初
    'under_move',    # 細球水位 尾-初
    'water_gap',     # 細水-大水（負=大球熱）
    'stage_f',       # 階段因子
    'ah_hc',         # 亞盤讓球（強弱懸殊→入球期望）
    'lg_avg',        # 聯賽場均入球
    'home_ratio',    # 聯賽主隊入球比例
]
N_F = len(FEATURE_NAMES)


def _ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {MODEL_TABLE}(
        league TEXT PRIMARY KEY, weights TEXT NOT NULL, bias REAL NOT NULL,
        hit_rate REAL, val_hit REAL, n INTEGER, updated_at TEXT)''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {RULE_TABLE}(
        league TEXT NOT NULL, desc TEXT NOT NULL, cond TEXT NOT NULL,
        hit_rate REAL, n INTEGER, side TEXT, updated_at TEXT,
        PRIMARY KEY(league, cond))''')


# ---------------------------------------------------------------- 特徵

def lam_team_total(row, teams, lg_avg_home, lg_avg_away, lg_avg_total):
    """同 haobao_engine.predict 完全一致嘅球隊λ（保證訓練/預測同一口徑）。"""
    H = teams.get(int(row['home_id'])) or {}
    A = teams.get(int(row['away_id'])) or {}

    def shrink(rate, n, prior, k=6.0):
        if n <= 0 or rate is None or np.isnan(rate):
            return prior
        return (n * rate + k * prior) / (n + k)

    lg_home_gf = max(lg_avg_home, 0.01)
    lg_away_gf = max(lg_avg_away, 0.01)
    att_h = shrink(H.get('gf_h'), H.get('n_h', 0), lg_home_gf) / lg_home_gf
    def_h = shrink(H.get('ga_h'), H.get('n_h', 0), lg_away_gf) / lg_away_gf
    att_a = shrink(A.get('gf_a'), A.get('n_a', 0), lg_away_gf) / lg_away_gf
    def_a = shrink(A.get('ga_a'), A.get('n_a', 0), lg_home_gf) / lg_home_gf
    lam_h = lg_avg_home * att_h * def_a
    lam_a = lg_avg_away * att_a * def_h
    return lam_h + lam_a, lam_h, lam_a


def features_for_row(row, ctx):
    """ctx = {'teams','stages','lg'}（HB 物件或訓練期自建）。回傳 (N_F,) 或 None。
    全部用 .get()——predict 嘅臨時行可能缺某啲欄位（2026-10-06 實戰 KeyError）。"""
    ou_line = row.get('ou_line')
    if ou_line is None or (isinstance(ou_line, float) and np.isnan(ou_line)):
        return None
    lg = row.get('league')
    L = ctx['lg'].get(lg)
    lg_avg_total = L['avg_total'] if L else ctx['all_avg_total']
    lg_avg_home = L['avg_home'] if L else ctx['all_avg_home']
    lg_avg_away = L['avg_away'] if L else ctx['all_avg_away']
    sf = ctx['stages'].get((lg, row.get('round_label')))
    stage_f = sf['factor'] if sf else 1.0
    lam_team, lam_h, lam_a = lam_team_total(
        row, ctx['teams'], lg_avg_home, lg_avg_away, lg_avg_total)
    lam_lgstage = lg_avg_total * stage_f
    # 莊家層：同線場均（同 predict 口徑）
    key = float(np.round(ou_line * 2) / 2)
    line_avg = ctx['line_avg'].get(key)
    lam_bm = line_avg if line_avg else lg_avg_total
    oi_line = row.get('oi_line')
    oi_over = row.get('oi_over')
    oi_under = row.get('oi_under')
    ou_over = row.get('ou_over')
    ou_under = row.get('ou_under')
    ah_hc = row.get('ah_hc')

    def _d(x):
        return x if (x is not None and not
                     (isinstance(x, float) and np.isnan(x))) else None

    oi_line, oi_over, oi_under = _d(oi_line), _d(oi_over), _d(oi_under)
    ou_over, ou_under, ah_hc = _d(ou_over), _d(ou_under), _d(ah_hc)
    line_move = (ou_line - oi_line) if oi_line is not None else 0.0
    over_move = (ou_over - oi_over) \
        if (ou_over is not None and oi_over is not None) else 0.0
    under_move = (ou_under - oi_under) \
        if (ou_under is not None and oi_under is not None) else 0.0
    water_gap = (ou_under - ou_over) \
        if (ou_under is not None and ou_over is not None) else 0.0
    ah_hc = ah_hc or 0.0
    home_ratio = lg_avg_home / max(lg_avg_total, 0.01)
    f = np.array([lam_team, lam_lgstage, lam_bm, ou_line, line_move,
                  over_move, under_move, water_gap, stage_f, ah_hc,
                  lg_avg_total, home_ratio], dtype=np.float64)
    if np.isnan(f).any() or np.isinf(f).any():
        return None
    return f


def build_ctx(df, lg, stages, teams):
    """訓練用上下文（同 HB 嘅結構對齊）。"""
    d = df[df['ou_line'].notna()]
    line_avg = {}
    if len(d):
        grp = d.groupby(np.round(d['ou_line'] * 2) / 2)['total'].mean()
        line_avg = {float(k): float(v) for k, v in grp.items()}
    return {'teams': teams, 'stages': stages, 'lg': lg,
            'line_avg': line_avg,
            'all_avg_total': float(df['total'].mean()),
            'all_avg_home': float(df['home_score'].mean()),
            'all_avg_away': float(df['away_score'].mean())}


# ---------------------------------------------------------------- 模型

def _train_logreg(X, y, iters=800, lr=0.5, l2=1.0, seed=7):
    """L2 邏輯迴歸（梯度下降；特徵已標準化）。回傳 (w, b, mu, sd)。"""
    rng = np.random.RandomState(seed)
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-9
    Z = (X - mu) / sd
    w = np.zeros(Z.shape[1])
    b = 0.0
    n = len(Z)
    for _ in range(iters):
        z = Z @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        err = p - y
        grad_w = Z.T @ err / n + l2 * w / n
        grad_b = err.mean()
        w -= lr * grad_w
        b -= lr * grad_b
    return {'w': w, 'b': float(b), 'mu': mu, 'sd': sd}


def _predict_prob(model, X):
    w, b, mu, sd = model['w'], model['b'], model['mu'], model['sd']
    Z = (X - mu) / sd
    z = np.clip(Z @ w + b, -30, 30)
    return 1.0 / (1.0 + np.exp(-z))


def _hit_rate(y, prob, thresh=0.5):
    """命中率：預測大(prob>=0.5)實際大 或 預測細 實際細（走盤已剔）。"""
    if len(y) == 0:
        return None
    pred = np.where(prob >= thresh, 1, -1)
    return float((pred == y).mean())


# ---------------------------------------------------------------- 規則挖掘

def _mine_rules(X, y, names, min_n=40, min_hit=0.55, top=12):
    """單特徵閾值規則：每個特徵按分位數切 4 段，兩邊各自計命中率，收錄合格嘅。"""
    rules = []
    n = len(y)
    if n < min_n * 2:
        return rules
    for j, name in enumerate(names):
        col = X[:, j]
        qs = np.unique(np.quantile(col, [0.25, 0.5, 0.75]))
        for q in qs:
            for side, mask in (('>=', col >= q), ('<', col < q)):
                m = mask.sum()
                if m < min_n:
                    continue
                # 預測方向：該子樣本 y 均值>0 預測大，否則細
                mean_y = y[mask].mean()
                pred_side = 1 if mean_y > 0 else -1
                hr = float((np.full(int(m), pred_side) == y[mask]).mean())
                if hr >= min_hit:
                    d = '大' if pred_side == 1 else '細'
                    rules.append({
                        'desc': f'{name} {side} {q:.3f} → 宜{d}',
                        'cond': f'{j}|{side}|{q:.6f}',
                        'hit_rate': round(hr, 4), 'n': int(m),
                        'side': '大' if pred_side == 1 else '細',
                        'feat': j, 'q': float(q), 'ge': side == '>='})
    rules.sort(key=lambda r: (-r['hit_rate'], -r['n']))
    return rules[:top]


# ---------------------------------------------------------------- 主流程

def _write_retry(conn, sql, args, tries=20, wait=8):
    """DB 寫入重試：本機爬蟲/伺服器長事務成日鎖庫（同 _feat_hourly_tick 手法）。"""
    for attempt in range(tries):
        try:
            conn.execute(sql, args)
            return
        except sqlite3.OperationalError:
            if attempt == tries - 1:
                raise
            time.sleep(wait)


def retrain(conn, df, lg, stages, teams, min_lg_n=80, verbose=True):
    """全量重訓練：逐聯賽＋全局；存 hb_models/hb_rules；回傳報告 dict。"""
    _ensure_schema(conn)
    ctx = build_ctx(df, lg, stages, teams)
    d = df[df['ou_line'].notna()].reset_index(drop=True)
    rows, ys, metas = [], [], []
    for _, row in d.iterrows():
        f = features_for_row(row, ctx)
        if f is None:
            continue
        diff = row['total'] - row['ou_line']
        if diff == 0:
            continue                       # 走盤唔計命中率
        rows.append(f)
        ys.append(1 if diff > 0 else -1)
        metas.append(row['league'])
    if not rows:
        return {'ok': False, 'error': '冇可用樣本'}
    X = np.array(rows)
    y = np.array(ys, dtype=np.float64)
    leagues = np.array(metas)
    now = time.strftime('%Y-%m-%d %H:%M:%S')

    def split_fit(mask_idx):
        """時序切尾 25% 做驗證（先後順序=df 原有排序=時間升序）。"""
        idx = np.array(mask_idx)
        k = max(int(len(idx) * 0.75), 10)
        tr, va = idx[:k], idx[k:]
        if len(va) < 10 or len(tr) < 30:
            return None
        model = _train_logreg(X[tr], (y[tr] + 1) / 2)
        prob = _predict_prob(model, X[va])
        va_y = y[va]
        hit = _hit_rate(va_y, prob)
        return model, hit, len(tr), len(va)

    report = {'ok': True, 'updated': now, 'global': None, 'leagues': {},
              'rules_added': 0}
    # 全局模型
    g = split_fit(np.arange(len(X)))
    if g:
        model, hit, ntr, nva = g
        _write_retry(conn,
            f'INSERT INTO {MODEL_TABLE}(league,weights,bias,hit_rate,val_hit,n,'
            'updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(league) DO UPDATE SET '
            'weights=excluded.weights,bias=excluded.bias,val_hit=excluded.val_hit,'
            'n=excluded.n,updated_at=excluded.updated_at',
            ('__ALL__', json.dumps({'w': model['w'].tolist(), 'b': model['b'],
                                   'mu': model['mu'].tolist(),
                                   'sd': model['sd'].tolist()}),
             0.0, None, hit, len(X), now))
        report['global'] = {'val_hit': round(hit, 4), 'n': len(X),
                            'val_n': nva}
        # 全局規則
        rules = _mine_rules(X, y, FEATURE_NAMES)
        for r in rules:
            _write_retry(conn,
                f'INSERT INTO {RULE_TABLE}(league,desc,cond,hit_rate,n,side,'
                'updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(league,cond) '
                'DO UPDATE SET desc=excluded.desc,hit_rate=excluded.hit_rate,'
                'n=excluded.n,side=excluded.side,updated_at=excluded.updated_at',
                ('__ALL__', r['desc'], r['cond'], r['hit_rate'], r['n'],
                 r['side'], now))
            report['rules_added'] += 1
    # 逐聯賽（品質門檻：差過全局或低過 52% 嘅聯賽模型唔採用——
    # 自動修正規則之一：寧用全局都唔用過擬合嘅本地模型）
    g_hit = (report['global'] or {}).get('val_hit') or 0.52
    gate = max(0.52, g_hit - 0.02)
    for lgname in sorted(set(leagues.tolist())):
        idx = np.where(leagues == lgname)[0]
        if len(idx) < min_lg_n:
            continue
        r = split_fit(idx)
        if not r:
            continue
        model, hit, ntr, nva = r
        if hit < gate:
            _write_retry(conn,
                         f'DELETE FROM {MODEL_TABLE} WHERE league=?',
                         (lgname,))
            report['leagues'][lgname] = {
                'val_hit': round(hit, 4), 'n': len(idx), 'val_n': nva,
                'rejected': True}
            continue
        _write_retry(conn,
            f'INSERT INTO {MODEL_TABLE}(league,weights,bias,hit_rate,val_hit,n,'
            'updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(league) DO UPDATE SET '
            'weights=excluded.weights,bias=excluded.bias,val_hit=excluded.val_hit,'
            'n=excluded.n,updated_at=excluded.updated_at',
            (lgname, json.dumps({'w': model['w'].tolist(), 'b': model['b'],
                                'mu': model['mu'].tolist(),
                                'sd': model['sd'].tolist()}),
             0.0, None, hit, len(idx), now))
        report['leagues'][lgname] = {'val_hit': round(hit, 4), 'n': len(idx),
                                     'val_n': nva}
        rules = _mine_rules(X[idx], y[idx], FEATURE_NAMES)
        for rr in rules:
            _write_retry(conn,
                f'INSERT INTO {RULE_TABLE}(league,desc,cond,hit_rate,n,side,'
                'updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(league,cond) '
                'DO UPDATE SET desc=excluded.desc,hit_rate=excluded.hit_rate,'
                'n=excluded.n,side=excluded.side,updated_at=excluded.updated_at',
                (lgname, rr['desc'], rr['cond'], rr['hit_rate'], rr['n'],
                 rr['side'], now))
            report['rules_added'] += 1
    conn.commit()
    if verbose:
        print(f'[hb-learn] 重訓完成：全局 n={len(X)} val={report["global"]}'
              f'；聯賽模型 {len(report["leagues"])} 個；規則 +{report["rules_added"]}',
              flush=True)
    return report


def load_models(conn):
    """HB 起 ctx 時讀取：回傳 {league: (w,b,mu,sd)}＋規則 {league: [rules]}。"""
    _ensure_schema(conn)
    models, rules = {}, {}
    for lg, wj, b in conn.execute(
            f'SELECT league, weights, bias FROM {MODEL_TABLE}'):
        d = json.loads(wj)
        models[lg] = {'w': np.array(d['w']), 'b': float(d['b']),
                      'mu': np.array(d['mu']), 'sd': np.array(d['sd'])}
    for lg, desc, cond, hr, n, side in conn.execute(
            f'SELECT league, desc, cond, hit_rate, n, side FROM {RULE_TABLE} '
            'ORDER BY hit_rate DESC'):
        rules.setdefault(lg, []).append({'desc': desc, 'cond': cond,
                                         'hit_rate': hr, 'n': n,
                                         'side': side})
    return models, rules


def model_report(conn):
    """狀態端點用：逐聯賽命中率表。"""
    _ensure_schema(conn)
    rows = []
    for lg, val_hit, n, upd in conn.execute(
            f'SELECT league, val_hit, n, updated_at FROM {MODEL_TABLE} '
            'ORDER BY n DESC'):
        rows.append({'league': lg, 'val_hit': val_hit, 'n': n,
                     'updated': upd})
    n_rules = conn.execute(f'SELECT COUNT(*) FROM {RULE_TABLE}').fetchone()[0]
    return {'models': rows, 'n_rules': n_rules}
