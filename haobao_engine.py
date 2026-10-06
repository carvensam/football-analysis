# -*- coding: utf-8 -*-
"""好波引擎（2026-10-05）：大小球＋半全場預測。

因子層（每項獨立計，再同全庫混合）：
  1. 聯賽／杯賽（req_name）獨立：場均入球、大球率、半場入球率、半全場 9 格矩陣
  2. 盃賽階段（league × round_label）獨立：場均入球因子
  3. 球隊獨立：主場入/失、客場入/失（向聯賽均值收縮）
  4. 莊家因子：12BET 尾盤大小線＋初盤→尾盤變化（線升/降、大球水位升/降）
  5. 全庫基準：所有機率嘅 prior，樣本細嘅聯賽向佢混合
輸出：大/細/走%、預期入球、最可能波膽、信心；半全場 9 格；上下盤×大小球關係。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

HK_TZ = timezone(timedelta(hours=8))

_pool = {'df': None, 'ts': 0}
_lock = threading.Lock()
POOL_TTL = 5400

HT_SHARE_ALL = 0.45          # 半場入球佔全場比例（全庫校準用後備）
MIN_LG_N = 60                # 聯賽樣本下限（低過向全庫混合）


def hk_now():
    return datetime.now(HK_TZ).replace(tzinfo=None)


def load_pool(conn, max_age=POOL_TTL):
    """好波池：全部有完場比分嘅場次＋12BET 尾盤/初盤大小線＋尾盤亞盤。"""
    now = time.time()
    if _pool['df'] is not None and now - _pool['ts'] < max_age:
        return _pool['df']
    with _lock:
        now = time.time()
        if _pool['df'] is not None and now - _pool['ts'] < max_age:
            return _pool['df']
        _pool['df'] = None
        import gc
        gc.collect()
        df = pd.read_sql_query(
            "SELECT m.id, m.kickoff, c.req_name AS league, m.round_label, "
            "m.home_id, m.away_id, m.home_score, m.away_score, "
            "m.half_home, m.half_away, "
            "oc.total_line AS ou_line, oc.over_odds AS ou_over, "
            "oc.under_odds AS ou_under, "
            "oi.total_line AS oi_line, oi.over_odds AS oi_over, "
            "oi.under_odds AS oi_under, "
            "oa.handicap AS ah_hc, oa.giver AS ah_gv "
            "FROM matches m "
            "JOIN seasons s ON s.id=m.season_id "
            "JOIN competitions c ON c.titan_id=s.titan_id "
            "LEFT JOIN odds_ou oc ON oc.match_id=m.id "
            "AND oc.label='closing' AND oc.company_id=12 "
            "LEFT JOIN odds_ou oi ON oi.match_id=m.id "
            "AND oi.label='initial' AND oi.company_id=12 "
            "LEFT JOIN odds_asian oa ON oa.match_id=m.id "
            "AND oa.label='closing' AND oa.company_id=12 "
            "WHERE m.home_score IS NOT NULL",
            conn,
            # 讀取當場直接落細型——先生成 fat object df 再轉嘅話，
            # 轉換前嗰下瞬間 ~200MB 會同 V1 池並存爆 512MB（2026-10-06 實測）
            dtype={'league': 'category', 'round_label': 'category',
                   'ah_gv': 'category',
                   'home_score': 'Int16', 'away_score': 'Int16',
                   'half_home': 'Int16', 'half_away': 'Int16',
                   'ou_line': 'float32', 'ou_over': 'float32',
                   'ou_under': 'float32', 'oi_line': 'float32',
                   'oi_over': 'float32', 'oi_under': 'float32',
                   'ah_hc': 'float32'})
        df['total'] = df['home_score'] + df['away_score']
        df['kickoff'] = df['kickoff'].astype(str)
        _pool['df'] = df
        _pool['ts'] = time.time()
        print(f'[hb_pool] {len(df)} 場，{df.memory_usage(deep=True).sum() // 1e6} MB',
              flush=True)
        gc.collect()
        return df


# ---------- 基礎統計 ----------

def _shrink(rate, n, prior, k=8.0):
    """向 prior 收縮：樣本越細越靠 prior。"""
    if n <= 0 or rate is None or np.isnan(rate):
        return prior
    return (n * rate + k * prior) / (n + k)


def lg_stats(df):
    """每聯賽：場數、場均入球、場均主/客入球、有 OU 場次嘅大球率（按線）、
    半場場均、HTFT 矩陣。回傳 dict[league]。"""
    out = {}
    g = df.groupby('league')
    for lg, sub in g:
        n = len(sub)
        st = {'n': n,
              'avg_total': float(sub['total'].mean()),
              'avg_home': float(sub['home_score'].mean()),
              'avg_away': float(sub['away_score'].mean()),
              'ht_share': None, 'htft': None}
        ht = sub[sub['half_home'].notna()]
        if len(ht) >= 20:
            ht_tot = ht['half_home'] + ht['half_away']
            st['ht_share'] = float(ht_tot.mean() / max(sub['total'].mean(), 0.01))
            # HTFT 9 格矩陣
            m9 = np.zeros((3, 3))
            for hh, ha, fh, fa in zip(ht['half_home'], ht['half_away'],
                                      ht['home_score'], ht['away_score']):
                h = 0 if hh > ha else (1 if hh == ha else 2)
                f = 0 if fh > fa else (1 if fh == fa else 2)
                m9[h, f] += 1
            st['htft'] = m9 / max(m9.sum(), 1)
            st['ht_n'] = int(m9.sum())
        out[lg] = st
    return out


def ou_rates_by_line(df):
    """全庫＋按聯賽：每條尾盤線嘅大球率（剔走）。回傳 (all_dict, lg_dict)。
    key = round(line*2)/2。"""
    d = df[df['ou_line'].notna()]
    all_ = {}
    lg_ = {}
    for lg, sub in [('__ALL__', d)] + list(d.groupby('league')):
        tab = {}
        for ln, s2 in sub.groupby(np.round(sub['ou_line'] * 2) / 2):
            o = int((s2['total'] > s2['ou_line']).sum())
            u = int((s2['total'] < s2['ou_line']).sum())
            if o + u >= 10:
                tab[float(ln)] = {'over_r': o / (o + u), 'n': o + u}
        (all_ if lg == '__ALL__' else lg_)[lg] = tab
    return all_, lg_


def stage_factors(df):
    """(league, round_label) 場均入球因子（相對聯賽均值，向 1 收縮）。"""
    out = {}
    for (lg, rd), sub in df[df['round_label'].notna() &
                            (df['round_label'] != '')].groupby(['league', 'round_label']):
        if len(sub) < 6:
            continue
        lg_avg = df[df['league'] == lg]['total'].mean()
        if not lg_avg or np.isnan(lg_avg):
            continue
        f = float(sub['total'].mean() / lg_avg)
        n = len(sub)
        out[(lg, rd)] = {'factor': _shrink(f, n, 1.0, k=10.0), 'n': n}
    return out


def team_factors(df):
    """每隊：主場 入/失 場均、客場 入/失 場均（各隊獨立）。"""
    home = df.groupby('home_id').agg(
        n_h=('home_score', 'size'), gf_h=('home_score', 'mean'),
        ga_h=('away_score', 'mean'))
    away = df.groupby('away_id').agg(
        n_a=('away_score', 'size'), gf_a=('away_score', 'mean'),
        ga_a=('home_score', 'mean'))
    out = {}
    for tid, r in home.iterrows():
        out.setdefault(tid, {}).update(
            {'n_h': int(r['n_h']), 'gf_h': float(r['gf_h']), 'ga_h': float(r['ga_h'])})
    for tid, r in away.iterrows():
        out.setdefault(tid, {}).update(
            {'n_a': int(r['n_a']), 'gf_a': float(r['gf_a']), 'ga_a': float(r['ga_a'])})
    return out


# ---------- 泊松工具 ----------

def _pois_pmf(lam, kmax=10):
    from math import exp, factorial
    return np.array([exp(-lam) * lam ** k / factorial(k) for k in range(kmax + 1)])


def poisson_ou_probs(lam, line, kmax=14):
    """P(大/細/走) for total ~ Poisson(lam) vs line（線係 0.25 倍數）。
    x.5 線冇走盤；x.25＝半注 x 半注 x+0.5；x.75＝半注 x+0.5 半注 x+1。"""
    from math import exp, factorial
    p = [exp(-lam) * lam ** k / factorial(k) for k in range(kmax + 1)]
    p.append(1.0 - sum(p))          # 尾部併入最後格
    over = under = push = 0.0
    frac = round(line % 1.0, 2)
    base = int(line)
    for k, pk in enumerate(p):
        if frac == 0.25:            # 例 2.25＝2.0 半注＋2.5 半注
            if k > base:
                over += pk          # 2.0 部分贏＋2.5 部分贏
            elif k == base:
                push += pk * 0.5    # 2.0 部分走
                under += pk * 0.5   # 2.5 部分輸
            else:
                under += pk
        elif frac == 0.75:          # 例 2.75＝2.5 半注＋3.0 半注
            if k >= base + 2:
                over += pk
            elif k == base + 1:
                over += pk * 0.5    # 2.5 部分贏
                push += pk * 0.5    # 3.0 部分走
            else:
                under += pk
        elif frac == 0.5:           # 例 2.5：冇走盤
            if k > base:
                over += pk
            else:
                under += pk
        else:                       # 整數線（2.0/3.0）：恰好=走
            if k > base:
                over += pk
            elif k == base:
                push += pk
            else:
                under += pk
    return over, under, push


def top_scorelines(lh, la, topn=3):
    ph = _pois_pmf(lh, 8)
    pa = _pois_pmf(la, 8)
    cells = []
    for h in range(7):
        for a in range(7):
            cells.append((ph[h] * pa[a], h, a))
    cells.sort(reverse=True)
    return [[h, a, round(p * 100, 1)] for p, h, a in cells[:topn]]


def htft_matrix(lh, la, ht_share, emp=None, emp_n=0, kmax=6):
    """9 格概率。泊松分層：HT ~ Poi(λ×ht_share)，2H ~ Poi(λ×(1−ht_share))。
    再同聯賽實證矩陣混合（權重 = emp_n/(emp_n+200)）。"""
    h1, a1 = lh * ht_share, la * ht_share
    h2, a2 = lh * (1 - ht_share), la * (1 - ht_share)
    p1h, p1a = _pois_pmf(h1, kmax), _pois_pmf(a1, kmax)
    p2h, p2a = _pois_pmf(h2, kmax), _pois_pmf(a2, kmax)
    m9 = np.zeros((3, 3))
    # 累積 HT 各比分 × 2H 各比分
    for hh in range(kmax + 1):
        for ha in range(kmax + 1):
            pht = p1h[hh] * p1a[ha]
            if pht < 1e-9:
                continue
            ht_r = 0 if hh > ha else (1 if hh == ha else 2)
            for s2h in range(kmax + 1):
                for s2a in range(kmax + 1):
                    p2 = p2h[s2h] * p2a[s2a]
                    if p2 < 1e-9:
                        continue
                    fh, fa = hh + s2h, ha + s2a
                    ft_r = 0 if fh > fa else (1 if fh == fa else 2)
                    m9[ht_r, ft_r] += pht * p2
    m9 = m9 / max(m9.sum(), 1e-12)
    if emp is not None and emp_n > 0:
        w = emp_n / (emp_n + 200.0)
        m9 = (1 - w) * m9 + w * emp
    out = {}
    names = ['H', 'D', 'A']
    for i in range(3):
        for j in range(3):
            out[f'{names[i]}{names[j]}'] = round(float(m9[i, j]) * 100, 1)
    return out


# ---------- 全庫基準 ----------

class HB:
    """好波上下文：load_pool 之後 build() 一次，之後 predict(match) 直接用。"""

    def __init__(self, conn):
        df = load_pool(conn)
        self.df = df
        self.all_avg_total = float(df['total'].mean())
        self.all_avg_home = float(df['home_score'].mean())
        self.all_avg_away = float(df['away_score'].mean())
        ht = df[df['half_home'].notna()]
        self.all_ht_share = float(
            (ht['half_home'] + ht['half_away']).mean() / self.all_avg_total) \
            if len(ht) >= 100 else HT_SHARE_ALL
        # 全庫 HTFT 矩陣
        m9 = np.zeros((3, 3))
        for hh, ha, fh, fa in zip(ht['half_home'], ht['half_away'],
                                  ht['home_score'], ht['away_score']):
            m9[0 if hh > ha else 1 if hh == ha else 2,
                0 if fh > fa else 1 if fh == fa else 2] += 1
        self.all_htft = m9 / max(m9.sum(), 1)
        self.all_htft_n = int(m9.sum())
        self.lg = lg_stats(df)
        self.ou_all, self.ou_lg = ou_rates_by_line(df)
        self.stages = stage_factors(df)
        self.teams = team_factors(df)
        # 全庫 上下盤×大小球 關係
        self.rel_all = self._ah_ou_relation(df)
        # 自我學習模型（hb_learn 訓練產出；冇就照舊行泊松混合）
        self.models, self.model_rules = {}, {}
        try:
            import hb_learn
            self.models, self.model_rules = hb_learn.load_models(conn)
            self._fctx = hb_learn.build_ctx(df, self.lg, self.stages,
                                            self.teams)
        except Exception:
            self._fctx = None

    def _ah_ou_relation(self, sub):
        d = sub[sub['ou_line'].notna() & sub['ah_hc'].notna()]
        tab = {'n': len(d), 'up_over': 0, 'up_under': 0,
               'down_over': 0, 'down_under': 0, 'up_push': 0, 'down_push': 0,
               'push_over': 0, 'push_under': 0, 'push_push': 0}
        for _, r in d.iterrows():
            if r['ah_gv'] == 'home':
                m = r['home_score'] - r['ah_hc'] - r['away_score']
            elif r['ah_gv'] == 'away':
                m = r['away_score'] - r['ah_hc'] - r['home_score']
            else:
                m = r['home_score'] - r['away_score']
            o = r['total'] - r['ou_line']
            ah = 'up' if m > 0 else ('down' if m < 0 else 'push')
            ou = 'over' if o > 0 else ('under' if o < 0 else 'push')
            tab[f'{ah}_{ou}'] += 1
        return tab

    def relation_for(self, league):
        """聯賽版關係（樣本細自動用全庫頂）＋全庫版。"""
        sub = self.df[self.df['league'] == league]
        lg_rel = self._ah_ou_relation(sub)
        return lg_rel, self.rel_all

    # ---------- 預測 ----------

    def predict(self, home_id, away_id, league, round_label,
                ou_line=None, ou_over=None, ou_under=None,
                oi_line=None, oi_over=None, ah_hc=None):
        """回傳 {'ou':..., 'htft':..., 'relation':..., 'factors':...}"""
        L = self.lg.get(league)
        lg_n = L['n'] if L else 0
        # 聯賽基準（樣本細向全庫混合）
        w_lg = lg_n / (lg_n + MIN_LG_N)
        avg_total = w_lg * (L['avg_total'] if L else self.all_avg_total) \
            + (1 - w_lg) * self.all_avg_total
        avg_home = w_lg * (L['avg_home'] if L else self.all_avg_home) \
            + (1 - w_lg) * self.all_avg_home
        avg_away = w_lg * (L['avg_away'] if L else self.all_avg_away) \
            + (1 - w_lg) * self.all_avg_away
        ht_share = (L.get('ht_share') or self.all_ht_share) if L \
            else self.all_ht_share

        # 1) 球隊層：poisson 攻守法
        H = self.teams.get(home_id) or {}
        A = self.teams.get(away_id) or {}
        lg_home_gf = max(avg_home, 0.01)
        lg_away_gf = max(avg_away, 0.01)
        att_h = _shrink(H.get('gf_h'), H.get('n_h', 0),
                        lg_home_gf, k=6.0) / lg_home_gf
        def_h = _shrink(H.get('ga_h'), H.get('n_h', 0),
                        lg_away_gf, k=6.0) / lg_away_gf
        att_a = _shrink(A.get('gf_a'), A.get('n_a', 0),
                        lg_away_gf, k=6.0) / lg_away_gf
        def_a = _shrink(A.get('ga_a'), A.get('n_a', 0),
                        lg_home_gf, k=6.0) / lg_home_gf
        lam_h_team = avg_home * att_h * def_a
        lam_a_team = avg_away * att_a * def_h

        # 2) 聯賽＋階段層
        sf = self.stages.get((league, round_label))
        stage_f = sf['factor'] if sf else 1.0
        lam_total_lg = avg_total * stage_f
        lam_home_lg = lam_total_lg * (avg_home / max(avg_total, 0.01))
        lam_away_lg = lam_total_lg * (avg_away / max(avg_total, 0.01))

        # 3) 莊家層：尾盤線 → implied 場均入球（全庫該線嘅實際場均）
        lam_bm = None
        if ou_line is not None:
            key = float(np.round(ou_line * 2) / 2)
            sub2 = self.df[self.df['ou_line'].notna()]
            sub2 = sub2[np.round(sub2['ou_line'] * 2) / 2 == key]
            if len(sub2) >= 30:
                lam_bm = float(sub2['total'].mean())
        # 混合三層（球隊 45%／聯賽階段 25%／莊家 30%；冇莊家就頭兩層 6:4）
        if lam_bm is not None:
            lam_home = 0.45 * lam_h_team + 0.25 * lam_home_lg + 0.30 * (lam_bm * avg_home / max(avg_total, 0.01))
            lam_away = 0.45 * lam_a_team + 0.25 * lam_away_lg + 0.30 * (lam_bm * avg_away / max(avg_total, 0.01))
        else:
            lam_home = 0.6 * lam_h_team + 0.4 * lam_home_lg
            lam_away = 0.6 * lam_a_team + 0.4 * lam_away_lg
        lam = lam_home + lam_away

        # 4) 大/細/走：泊松模型 × 聯賽實證（按尾盤線）
        out_ou = {
            'line': ou_line, 'over_odds': ou_over, 'under_odds': ou_under,
            'init_line': oi_line, 'init_over': oi_over,
            'lambda': round(lam, 2),
            'lambda_home': round(lam_home, 2),
            'lambda_away': round(lam_away, 2),
            'expected_score': [round(lam_home, 1), round(lam_away, 1)],
            'layers': {
                'team': round(lam_h_team + lam_a_team, 2),
                'league_stage': round(lam_home_lg + lam_away_lg, 2),
                'bookmaker': round(lam_bm, 2) if lam_bm is not None else None,
            },
            'top_scores': top_scorelines(lam_home, lam_away),
        }
        if ou_line is not None:
            po, pu, pp = poisson_ou_probs(lam, ou_line)
            # 聯賽實證（按線）
            key = float(np.round(ou_line * 2) / 2)
            emp = None
            lg_tab = self.ou_lg.get(league, {})
            e = lg_tab.get(key)
            w_emp = 0.0
            if e:
                w_emp = e['n'] / (e['n'] + 80.0)
                emp = e['over_r']
            a_tab = self.ou_all.get('__ALL__', {}).get(key)
            emp_all = a_tab['over_r'] if a_tab else 0.5
            base = emp if emp is not None else emp_all
            over_r = (1 - w_emp) * po + w_emp * base
            over_r = 0.5 * over_r + 0.5 * po        # 傳統：模型主導，實證修正
            model_r = None
            model_src = None
            # 自我學習層：逐聯賽邏輯迴歸（冇就用全局）；權重由回測自動修正
            if self._fctx is not None and self.models:
                try:
                    import hb_learn
                    prow = {'league': league, 'round_label': round_label,
                            'home_id': home_id, 'away_id': away_id,
                            'ou_line': ou_line, 'oi_line': oi_line,
                            'ou_over': ou_over, 'ou_under': ou_under,
                            'ah_hc': ah_hc}
                    f = hb_learn.features_for_row(prow, self._fctx)
                    if f is not None:
                        mdl = self.models.get(league) \
                            or self.models.get('__ALL__')
                        if mdl is not None:
                            model_r = float(
                                hb_learn._predict_prob(mdl, f[None, :])[0])
                            model_src = league if league in self.models \
                                else '全庫'
                except Exception:
                    model_r = None
            if model_r is not None:
                # 學習模型主導 70%，傳統泊松/實證 30%
                over_r = 0.7 * model_r + 0.3 * over_r
            under_r = 1.0 - over_r - pp
            out_ou['over_r'] = round(over_r * 100, 1)
            out_ou['under_r'] = round(under_r * 100, 1)
            out_ou['push_r'] = round(pp * 100, 1)
            out_ou['lean'] = '大' if over_r > under_r else '細'
            out_ou['empirical_over'] = round(base * 100, 1)
            out_ou['empirical_n'] = e['n'] if e else 0
            if model_r is not None:
                out_ou['model_over'] = round(model_r * 100, 1)
                out_ou['model_src'] = model_src
            # 信心：層間一致度＋樣本
            lams = [x for x in (lam_h_team + lam_a_team,
                                lam_home_lg + lam_away_lg, lam_bm) if x]
            spread = (max(lams) - min(lams)) / max(np.mean(lams), 0.01) if len(lams) >= 2 else 0.3
            conf = '高' if (spread < 0.12 and lg_n >= 200) else \
                   '中' if spread < 0.25 else '低'
            out_ou['confidence'] = conf
            # 莊家訊號（誘盤/減損）：初→尾走勢＋學習權重方向
            bsig = {}
            if oi_line is not None:
                lm = round(ou_line - oi_line, 2)
                bsig['line_move'] = lm
                if ou_over is not None and oi_over is not None:
                    bsig['over_odds_move'] = round(ou_over - oi_over, 2)
                if lm > 0:
                    bsig['move_txt'] = f'升盤 +{lm:g}（莊家調高大球門檻'
                elif lm < 0:
                    bsig['move_txt'] = f'降盤 {lm:g}（莊家調低大球門檻'
                else:
                    bsig['move_txt'] = '盤口不變'
                # 水位走勢補完句：大球水位跌=莊家減大球賠付（怕大）
                if bsig.get('over_odds_move'):
                    om = bsig['over_odds_move']
                    if om < -0.04:
                        bsig['move_txt'] += '｜大球水位下調＝莊家減損防大'
                    elif om > 0.04:
                        bsig['move_txt'] += '｜大球水位上調＝誘大之嫌'
                    else:
                        bsig['move_txt'] += '｜水位平穩'
                bsig['move_txt'] += '）'
            # 學習權重對走勢訊號嘅敏感度（正=升盤偏向大球）
            if model_r is not None and league in self.models:
                try:
                    w = self.models[league]['w']
                    j = hb_learn.FEATURE_NAMES.index('line_move')
                    s = w[j]
                    bsig['w_line_move'] = round(float(s), 3)
                    bsig['w_txt'] = ('升盤偏向大球' if s > 0.15 else
                                     '升盤偏向細球' if s < -0.15 else
                                     '走勢訊號弱')
                except Exception:
                    pass
            if bsig:
                out_ou['bookmaker_signal'] = bsig
            # 適用規則（挖掘出嚟、命中率高嘅單特徵規則）
            if self._fctx is not None:
                try:
                    import hb_learn
                    prow2 = {'league': league, 'round_label': round_label,
                             'home_id': home_id, 'away_id': away_id,
                             'ou_line': ou_line, 'oi_line': oi_line,
                             'ou_over': ou_over, 'ou_under': ou_under,
                             'ah_hc': ah_hc}
                    f2 = hb_learn.features_for_row(prow2, self._fctx)
                    if f2 is not None:
                        hit_rules = []
                        for r in (self.model_rules.get(league) or []) + \
                                 (self.model_rules.get('__ALL__') or []):
                            try:
                                j_s, ge_s, q_s = r.get('cond', '|').split('|')
                                j = int(j_s); q = float(q_s); ge = ge_s == '>='
                                ok = f2[j] >= q if ge else f2[j] < q
                                if ok:
                                    hit_rules.append(r)
                            except Exception:
                                continue
                        hit_rules.sort(key=lambda r: -r['hit_rate'])
                        if hit_rules:
                            out_ou['rules'] = [
                                {'desc': r['desc'],
                                 'hit_rate': round(r['hit_rate'] * 100, 1),
                                 'n': r['n']} for r in hit_rules[:3]]
                except Exception:
                    pass

        # 5) 半全場 9 格
        emp_m = L.get('htft') if L else None
        emp_n = L.get('ht_n', 0) if L else 0
        if emp_m is None or emp_n < 100:
            emp_m, emp_n = self.all_htft, self.all_htft_n
        cells = htft_matrix(lam_home, lam_away, ht_share, emp_m, emp_n)
        top_cell = max(cells.items(), key=lambda kv: kv[1])

        # 6) 上下盤 × 大小球關係
        lg_rel, all_rel = self.relation_for(league)

        def rel_fmt(t):
            n = t['n']
            def pct(a, b):
                return round(a / (a + b) * 100, 1) if (a + b) else None
            return {
                'n': n,
                'up_over_r': pct(t['up_over'], t['up_under']),
                'up_under_r': pct(t['up_under'], t['up_over']),
                'down_over_r': pct(t['down_over'], t['down_under']),
                'down_under_r': pct(t['down_under'], t['down_over']),
                'up_over': t['up_over'], 'up_under': t['up_under'],
                'down_over': t['down_over'], 'down_under': t['down_under'],
            }

        return {
            'ou': out_ou,
            'htft': {'cells': cells, 'top': {'cell': top_cell[0], 'pct': top_cell[1]},
                     'ht_avg_total': round(avg_total * ht_share, 2)},
            'relation': {'league': rel_fmt(lg_rel), 'all': rel_fmt(all_rel)},
            'factors': {
                'league': {'name': league, 'n': lg_n,
                           'avg_total': round(avg_total, 2),
                           'avg_home': round(avg_home, 2),
                           'avg_away': round(avg_away, 2)},
                'stage': {'round': round_label,
                          'factor': round(sf['factor'], 3) if sf else None,
                          'n': sf['n'] if sf else 0},
                'home_team': {k: (round(v, 2) if isinstance(v, float) else v)
                              for k, v in H.items()},
                'away_team': {k: (round(v, 2) if isinstance(v, float) else v)
                              for k, v in A.items()},
            },
        }
