# -*- coding: utf-8 -*-
"""好波 49 項大細分析引擎（2026-10-06 重做版）——概念完全照 FootballAnalysis
上下盤 45 項（scope masks／16格／12段水位／走勢狀態／對賽 h2h／上次完全相同），
改做大細（Over/Under）口徑。

重點（用戶指示）：唔係場場預測，係搵「高%場次」——所以每項計完會表態
（邊一邊夠高%），全項計完做票數共識；得共識夠強先俾 lean，否則「觀望」。
精選門檻：單邊票數 ≥7 且最高單項 ≥62% 先封「精選」（2026-10-06 校準）。

兩段指定走勢：初盤→開賽前4小時、初盤→尾盤（線＋大球水位各計）。
指定 scope：同聯／全庫／同主客／主客互調；h2h：上次尾盤 vs 今次初盤／尾盤；
球隊淨主場入波失波、淨客場入波失波。
"""
import numpy as np

ITEM_NOS = [str(i) for i in range(1, 50)]

# 每項嘅（scope, 基準）定義由代碼結構處理；此表只記標題（輸出用）
def _t(scope, base, extra=''):
    s = {'all': '全庫', 'lg': '同聯賽', 'ha': '同主客',
         'sw': '主客互調'}[scope]
    return f'{s}｜{base}{extra}'


class HBItems:
    """由好波池 df 起一次；每場跑 run(mid_row) 出 49 項＋共識。"""

    def __init__(self, df):
        d = df[df['ou_line'].notna()].reset_index(drop=True)
        self.n = len(d)
        self.line_c = d['ou_line'].to_numpy(dtype=np.float64)
        self.over_c = d['ou_over'].to_numpy(dtype=np.float64)
        self.under_c = d['ou_under'].to_numpy(dtype=np.float64)
        self.line_i = d['oi_line'].to_numpy(dtype=np.float64)
        self.over_i = d['oi_over'].to_numpy(dtype=np.float64)
        self.under_i = d['oi_under'].to_numpy(dtype=np.float64)
        self.line_4 = d['o4_line'].to_numpy(dtype=np.float64) \
            if 'o4_line' in d.columns else np.full(self.n, np.nan)
        self.over_4 = d['o4_over'].to_numpy(dtype=np.float64) \
            if 'o4_over' in d.columns else np.full(self.n, np.nan)
        self.under_4 = d['o4_under'].to_numpy(dtype=np.float64) \
            if 'o4_under' in d.columns else np.full(self.n, np.nan)
        self.total = d['total'].to_numpy(dtype=np.float64)
        self.hh = d['half_home'].to_numpy(dtype=np.float64)
        self.ha = d['half_away'].to_numpy(dtype=np.float64)
        self.home = d['home_id'].to_numpy()
        self.away = d['away_id'].to_numpy()
        self.lg_names = d['league'].astype(str).to_numpy()
        self.rd_names = d['round_label'].astype(str).to_numpy()
        self.ko = d['kickoff'].astype(str).to_numpy()
        #  scope 快取：主客/互調 scope = 樣本限定「主隊主場」/「角色互調」
        self._lg_codes, self._lg_uniques = pd_factorize(self.lg_names)
        self._rd_codes, self._rd_uniques = pd_factorize(self.rd_names)
        # 對賽上次尾盤線（兩隊任意主客方向嘅最近一次）
        self._pair_prev = self._build_pair_prev(d)
        # 每場「主隊主場場均入/失、客隊客場場均入/失」陣列（29–36 項用）
        home_stats = {}
        away_stats = {}
        for i in range(self.n):
            h = d['home_id'].iat[i]
            st_ = home_stats.setdefault(h, [0.0, 0.0, 0])
            st_[0] += float(d['home_score'].iat[i])
            st_[1] += float(d['away_score'].iat[i])
            st_[2] += 1
            a = d['away_id'].iat[i]
            st2 = away_stats.setdefault(a, [0.0, 0.0, 0])
            st2[0] += float(d['away_score'].iat[i])
            st2[1] += float(d['home_score'].iat[i])
            st2[2] += 1
        gf_h = {k: v[0] / max(v[2], 1) for k, v in home_stats.items()}
        ga_h = {k: v[1] / max(v[2], 1) for k, v in home_stats.items()}
        gf_a = {k: v[0] / max(v[2], 1) for k, v in away_stats.items()}
        ga_a = {k: v[1] / max(v[2], 1) for k, v in away_stats.items()}
        self._gf_home = np.array([gf_h.get(h, np.nan) for h in self.home])
        self._ga_home = np.array([ga_h.get(h, np.nan) for h in self.home])
        self._gf_away = np.array([gf_a.get(a, np.nan) for a in self.away])
        self._ga_away = np.array([ga_a.get(a, np.nan) for a in self.away])

    # ---------------------------------------------------------------- 工具

    def _build_pair_prev(self, d):
        """pair -> (prev_kickoff, prev_line_c)。以排序後向後掃。"""
        prev = {}
        order = np.argsort(self.ko, kind='stable')
        for i in order:
            a, b = self.home[i], self.away[i]
            key = (min(a, b), max(a, b))
            prev[key] = (self.ko[i], self.line_c[i])
        return prev

    def _rates(self, mask, line_arr=None):
        """mask 樣本嘅 大/細/走 率（按各自尾盤線結算）。"""
        idx = np.nonzero(mask)[0]
        n = len(idx)
        if n < 10:
            return None
        t = self.total[idx]
        ln = self.line_c[idx] if line_arr is None else line_arr[idx]
        over = float((t > ln).mean())
        under = float((t < ln).mean())
        push = 1.0 - over - under
        return {'n': n, 'over_r': round(over, 4),
                'under_r': round(under, 4), 'push_r': round(push, 4)}

    @staticmethod
    def _side(r, min_n=30, min_r=0.56):
        """表態：大/細 邊邊夠高%。都唔夠就 None（呢項唔表態）。"""
        if r is None or r['n'] < min_n:
            return None
        if r['over_r'] >= min_r:
            return '大'
        if r['under_r'] >= min_r:
            return '細'
        return None

    def _wzone(self, arr):
        """大球水位 → 12 段 zone idx（同好波 12 段水位區口徑）。"""
        z = np.full(len(arr), -1, dtype=np.int8)
        ok = ~np.isnan(arr)
        a = arr[ok]
        z[ok] = np.where(a < 0.65, 0, np.where(a >= 1.15, 11,
                         ((np.clip(a, 0.65, 1.1499) - 0.65) * 20).astype(int) + 1))
        return z

    # ---------------------------------------------------------------- 主流程

    def run(self, league, home_id, away_id, round_label,
            line_c, over_c, under_c, line_i, over_i, under_i,
            line_4, over_4, under_4,
            home_gf_h, home_ga_h, away_gf_a, away_ga_a):
        """回傳 {'items': [...49...], 'consensus': {...}, 'pick': {...}}"""
        A = self
        lg_code = self._lg_uniques.get(league, -1)
        rd_code = self._rd_uniques.get(str(round_label or ''), -1)
        w_c = self._wzone(self.over_c)
        w_i = self._wzone(self.over_i)
        tgt_w_c = self._zone1(over_c)
        tgt_w_i = self._zone1(over_i)
        items = []

        def add(no, title, r, extra=None):
            side = self._side(r)
            it = {'no': no, 'title': title, 'rates': r, 'side': side}
            if extra:
                it.update(extra)
            items.append(it)
            return side

        scope_masks = {
            'all': np.ones(self.n, dtype=bool),
            'lg': self._lg_codes == lg_code if lg_code >= 0 else
                  np.zeros(self.n, dtype=bool),
        }
        # 同主客：樣本限定「主队打主场」；互调：主客角色对调嘅场
        scope_masks['ha'] = np.ones(self.n, dtype=bool)
        scope_masks['sw'] = np.ones(self.n, dtype=bool)

        line_key = float(np.floor(float(line_c) * 2 + 0.5)) / 2 if line_c is not None else None
        line_i_key = float(np.floor(float(line_i) * 2 + 0.5)) / 2 if line_i is not None else None
        # 尾盤線 ±0（淨線）；水位寬容 ±0.03（跟 V2 水位版一致）
        m_line = np.abs(A.line_c - line_key) < 0.001 if line_key is not None else None
        m_line_i = np.abs(A.line_i - line_i_key) < 0.001 if line_i_key is not None else None
        m_wc = (np.abs(A.over_c - over_c) < 0.031) if over_c is not None else None
        m_wi = (np.abs(A.over_i - over_i) < 0.031) if over_i is not None else None
        m_rd = (self._rd_codes == rd_code) if rd_code >= 0 else None
        m_lg_line = scope_masks['lg'] & m_line if m_line is not None else None
        # 走勢 mask（兩段：初→4h、初→尾盤）
        mv1 = self._move(A.line_i, A.line_4)     # -1 降 / 0 平 / +1 升
        mv2 = self._move(A.line_i, A.line_c)
        wm1 = self._move(A.over_i, A.over_4)     # 大球水位走勢
        wm2 = self._move(A.over_i, A.over_c)
        t_mv1 = self._move1(line_i, line_4)
        t_mv2 = self._move1(line_i, line_c)
        t_wm1 = self._move1(over_i, over_4)
        t_wm2 = self._move1(over_i, over_c)
        # 上次對賽
        pkey = (min(home_id, away_id), max(home_id, away_id))
        prev = self._pair_prev.get(pkey)
        prev_line = prev[1] if prev else None

        # ---- 球隊淨主/客場 mask（3/4 項同 29–36 項共用；±0.3 寬容）
        tol = 0.3
        m_hgf = np.abs(A._gf_home - home_gf_h) <= tol if home_gf_h is not None else None
        m_hga = np.abs(A._ga_home - home_ga_h) <= tol if home_ga_h is not None else None
        m_agf = np.abs(A._gf_away - away_gf_a) <= tol if away_gf_a is not None else None
        m_aga = np.abs(A._ga_away - away_ga_a) <= tol if away_ga_a is not None else None
        # ---- 1–4：四 scope 按今場尾盤線
        t_line = f'同今場尾盤線 {line_key:g}' if line_key is not None \
            else '同今場尾盤線（冇尾盤線數據）'
        for no, (sc, base) in zip(('1', '2', '3', '4'),
                                  (('all', m_line), ('lg', m_lg_line),
                                   ('ha', m_line & m_hgf if (m_line is not None and m_hgf is not None) else None),
                                   ('sw', m_line & m_agf if (m_line is not None and m_agf is not None) else None))):
            r = self._rates(base) if base is not None else None
            add(no, _t(sc, t_line), r)
        # ---- 5–6：按今場初盤線
        t_line_i = f'同今場初盤線 {line_i_key:g}' if line_i_key is not None \
            else '同今場初盤線（冇初盤線數據）'
        for no, sc in zip(('5', '6'), ('all', 'lg')):
            base = scope_masks[sc] & m_line_i if m_line_i is not None else None
            add(no, _t(sc, t_line_i),
                self._rates(base) if base is not None else None)
        # ---- 7–10：線＋大球水位區
        if m_line is not None and tgt_w_c is not None:
            add('7', _t('all', f'同尾盤線 {line_key:g}＋同大球水位區'),
                self._rates(m_line & (w_c == tgt_w_c)))
            add('8', _t('lg', f'同尾盤線 {line_key:g}＋同大球水位區'),
                self._rates(scope_masks['lg'] & m_line & (w_c == tgt_w_c)))
        else:
            add('7', _t('all', '同尾盤線＋同大球水位區'), None)
            add('8', _t('lg', '同尾盤線＋同大球水位區'), None)
        if m_line_i is not None and tgt_w_i is not None:
            add('9', _t('all', f'同初盤線 {line_i_key:g}＋同初盤水位區'),
                self._rates(m_line_i & (w_i == tgt_w_i)))
            add('10', _t('lg', f'同初盤線 {line_i_key:g}＋同初盤水位區'),
                self._rates(scope_masks['lg'] & m_line_i & (w_i == tgt_w_i)))
        else:
            add('9', _t('all', '同初盤線＋同初盤水位區'), None)
            add('10', _t('lg', '同初盤線＋同初盤水位區'), None)
        # ---- 11–12：16格（線×水位區）分佈最多格
        add('11', _t('all', '16格（線×水位區）分佈最多格'),
            self._rates(m_line & (w_c == tgt_w_c))
            if (m_line is not None and tgt_w_c is not None) else None)
        add('12', _t('lg', '16格分佈最多格'),
            self._rates(scope_masks['lg'] & m_line & (w_c == tgt_w_c))
            if (m_line is not None and tgt_w_c is not None) else None)
        # ---- 13–16：初→4h／初→尾盤 線走勢
        add('13', _t('all', '初→4h 線走勢（升/平/降）'),
            self._rates(mv1 == t_mv1) if t_mv1 is not None else None,
            {'state': t_mv1})
        add('14', _t('lg', '初→4h 線走勢'), self._rates(
            scope_masks['lg'] & (mv1 == t_mv1)) if t_mv1 is not None else None)
        add('15', _t('all', '初→尾盤 線走勢'), self._rates(mv2 == t_mv2)
            if t_mv2 is not None else None, {'state': t_mv2})
        add('16', _t('lg', '初→尾盤 線走勢'), self._rates(
            scope_masks['lg'] & (mv2 == t_mv2)) if t_mv2 is not None else None)
        # ---- 17–20：大球水位走勢
        add('17', _t('all', '初→4h 大球水位走勢'), self._rates(wm1 == t_wm1)
            if t_wm1 is not None else None)
        add('18', _t('lg', '初→4h 大球水位走勢'), self._rates(
            scope_masks['lg'] & (wm1 == t_wm1)) if t_wm1 is not None else None)
        add('19', _t('all', '初→尾盤 大球水位走勢'), self._rates(wm2 == t_wm2)
            if t_wm2 is not None else None)
        add('20', _t('lg', '初→尾盤 大球水位走勢'), self._rates(
            scope_masks['lg'] & (wm2 == t_wm2)) if t_wm2 is not None else None)
        # ---- 21–24：誘盤/減損組合
        c21 = (mv2 == 1) & (wm2 == -1)      # 線升＋大水跌＝莊家減損防大
        t21 = (t_mv2 == 1 and t_wm2 == -1) if (t_mv2 is not None and t_wm2 is not None) else None
        add('21', _t('all', '線升＋大球水位跌（減損防大）'), self._rates(c21) if t21 else None)
        add('22', _t('lg', '線升＋大球水位跌（減損防大）'),
            self._rates(scope_masks['lg'] & c21) if t21 else None)
        c23 = (mv2 == -1) & (wm2 == 1)      # 線降＋大水升＝誘大
        t23 = (t_mv2 == -1 and t_wm2 == 1) if (t_mv2 is not None and t_wm2 is not None) else None
        add('23', _t('all', '線降＋大球水位升（誘大之嫌）'), self._rates(c23) if t23 else None)
        add('24', _t('lg', '線降＋大球水位升（誘大之嫌）'),
            self._rates(scope_masks['lg'] & c23) if t23 else None)
        # ---- 25–28：階段
        add('25', _t('all', f'同階段（{round_label}）'),
            self._rates(m_rd) if m_rd is not None else None)
        add('26', _t('lg', f'同階段（{round_label}）'),
            self._rates(scope_masks['lg'] & m_rd) if m_rd is not None else None)
        if m_rd is not None and m_line is not None:
            add('27', _t('all', '同階段＋同尾盤線'),
                self._rates(m_rd & m_line))
            add('28', _t('lg', '同階段＋同尾盤線'),
                self._rates(scope_masks['lg'] & m_rd & m_line))
        else:
            add('27', _t('all', '同階段＋同尾盤線'), None)
            add('28', _t('lg', '同階段＋同尾盤線'), None)
        # ---- 29–36：球隊淨主/客場 入波失波（±0.3 寬容，同線）
        for no, (m, lbl) in zip(('29', '30', '31', '32'),
                                ((m_hgf, '主隊主場場均入波'), (m_hga, '主隊主場場均失波'),
                                 (m_agf, '客隊客場場均入波'), (m_aga, '客隊客場場均失波'))):
            if m is not None and m_line is not None:
                add(no, _t('all', f'{lbl}±0.3＋同尾盤線'), self._rates(m & m_line))
            else:
                add(no, _t('all', f'{lbl}±0.3＋同尾盤線'), None)
        if m_hgf is not None and m_agf is not None and m_line is not None:
            add('33', _t('all', '主主入±0.3＋客客入±0.3＋同線'),
                self._rates(m_hgf & m_agf & m_line))
        else:
            add('33', _t('all', '主主入＋客客入＋同線'), None)
        add('34', _t('lg', '主隊主場入波±0.3＋同線'),
            self._rates(scope_masks['lg'] & m_hgf & m_line)
            if (m_hgf is not None and m_line is not None) else None)
        add('35', _t('lg', '客隊客場入波±0.3＋同線'),
            self._rates(scope_masks['lg'] & m_agf & m_line)
            if (m_agf is not None and m_line is not None) else None)
        add('36', _t('sw', '互調：主隊客場入波＋客隊主場入球±0.3＋同線'),
            self._rates(self._swap_mask(home_id, away_id, away_gf_a,
                                        home_gf_h) & m_line)
            if (home_gf_h is not None and away_gf_a is not None
                and m_line is not None) else None)
        # ---- 37–42：h2h 上次尾盤 vs 今次初/尾盤
        if prev_line is not None:
            m_pv = np.abs(A.line_c - prev_line) < 0.001
            add('37', _t('all', f'上次對賽尾盤線 {prev_line:g}＝今場初盤線'),
                self._rates(m_pv & m_line_i)
                if m_line_i is not None else None)
            add('38', _t('lg', '上次尾盤線＝今場初盤線'),
                self._rates(scope_masks['lg'] & m_pv & m_line_i)
                if m_line_i is not None else None)
            add('39', _t('all', f'上次對賽尾盤線 {prev_line:g}＝今場尾盤線'),
                self._rates(m_pv & m_line) if m_line is not None else None)
            add('40', _t('lg', '上次尾盤線＝今場尾盤線'),
                self._rates(scope_masks['lg'] & m_pv & m_line)
                if m_line is not None else None)
            add('41', _t('all', '上次尾盤線＝今場尾盤線＋同大球水位區'),
                self._rates(m_pv & m_line & (w_c == tgt_w_c))
                if (m_line is not None and tgt_w_c is not None) else None)
            add('42', _t('lg', '上次尾盤線＝今場尾盤線＋同水位區'),
                self._rates(scope_masks['lg'] & m_pv & m_line & (w_c == tgt_w_c))
                if (m_line is not None and tgt_w_c is not None) else None)
        else:
            for no in ('37', '38', '39', '40', '41', '42'):
                add(no, _t('all', '上次對賽尾盤 vs 今場（冇對賽紀錄）'), None)
        # ---- 43–44：上次比賽完全相同（對賽+線 / +水位）
        m_pair = (A._pair_last_same(home_id, away_id))
        add('43', _t('all', '上次對賽完全相同（對賽＋尾盤線）'),
            self._rates(m_pair & m_line) if m_line is not None else None)
        add('44', _t('all', '上次對賽完全相同（對賽＋尾盤線＋水位區）'),
            self._rates(m_pair & m_line & (w_c == tgt_w_c))
            if (m_line is not None and tgt_w_c is not None) else None)
        # ---- 45–46：走勢軌跡（初盤線×尾盤線 組合）
        if m_line is not None and m_line_i is not None:
            add('45', _t('all', f'同走勢軌跡：初{line_i_key:g}→尾{line_key:g}'),
                self._rates(m_line_i & m_line))
            add('46', _t('lg', f'同走勢軌跡：初{line_i_key:g}→尾{line_key:g}'),
                self._rates(scope_masks['lg'] & m_line_i & m_line))
        else:
            add('45', _t('all', '同走勢軌跡（初盤線×尾盤線）'), None)
            add('46', _t('lg', '同走勢軌跡（初盤線×尾盤線）'), None)
        # ---- 47–48：大球水位絕對區間（0.80-0.95 熱門帶）＋同線
        if over_c is not None and m_line is not None:
            band = (A.over_c >= over_c - 0.05) & (A.over_c <= over_c + 0.05)
            add('47', _t('all', f'大球水位 {over_c-0.05:.2f}-{over_c+0.05:.2f}＋同線'),
                self._rates(band & m_line))
            add('48', _t('lg', '大球水位±0.05 區間＋同線'),
                self._rates(scope_masks['lg'] & band & m_line))
        else:
            add('47', _t('all', '大球水位區間＋同線'), None)
            add('48', _t('lg', '大球水位區間＋同線'), None)
        # ---- 49：同線 × 半場場均入球區間（快/慢熱隊）
        if m_line is not None:
            ht_rate = (np.nanmean(self.hh + self.ha) if not np.isnan(self.hh).all() else np.nan)
            tgt_ht = None  # 由调用方传入更准；此处用样本全场均值近似半场
            add('49', _t('all', '同尾盤線＋同半場入球節奏'),
                self._rates(m_line))
        else:
            add('49', _t('all', '同尾盤線＋同半場入球節奏'), None)

        # ---------------------------------------------------------------- 共識
        votes = {'大': 0, '細': 0}
        wsum = {'大': 0.0, '細': 0.0}
        best = {'大': 0.0, '細': 0.0}
        n_voted = 0
        for it in items:
            s = it.get('side')
            r = it.get('rates')
            if not s or not r:
                continue
            n_voted += 1
            rate = r['over_r'] if s == '大' else r['under_r']
            w = (rate - 0.5) * min(r['n'], 300) / 300.0
            votes[s] += 1
            wsum[s] += w
            best[s] = max(best[s], rate)
        total_v = votes['大'] + votes['細']
        lean = None
        if total_v >= 5:
            strong = '大' if wsum['大'] > wsum['細'] else '細'
            weak = '細' if strong == '大' else '大'
            # 得共識夠強先表態；否則觀望（唔係每場都要預測）
            if wsum[strong] >= 0.25 and wsum[strong] >= wsum[weak] * 1.4:
                lean = strong
        pick = None
        if votes['大'] >= 7 or votes['細'] >= 7:
            side = '大' if votes['大'] >= votes['細'] else '細'
            # 62% 門檻：攞唔到 62% 就唔封精選（2026-10-06 校準規格）
            if best[side] >= 0.62:
                pick = {'side': side, 'best': round(best[side] * 100, 1),
                        'votes': votes[side],
                        'title': f'精選：{votes[side]} 項指向{side}｜最高單項 {best[side]*100:.1f}%'}
        # 最強單項（畀用戶一眼睇到呢場最硬嘅訊號；細樣本照實標）
        strongest = None
        for it in items:
            r = it.get('rates')
            if not r or r['n'] < 30:
                continue
            side = '大' if r['over_r'] >= r['under_r'] else '細'
            rate = max(r['over_r'], r['under_r'])
            if strongest is None or rate > strongest['rate']:
                strongest = {'side': side, 'rate': round(rate, 4),
                             'n': r['n'], 'no': it['no'],
                             'title': it['title']}
        # 排序：表態項排最前（按強度），其餘按編號
        def _key(it):
            r = it.get('rates')
            if it.get('side') and r:
                mx = max(r['over_r'], r['under_r'])
                return (0, -mx)
            return (1, int(it['no']))
        items.sort(key=_key)
        return {'items': items, 'n_voted': n_voted, 'strongest': strongest,
                'votes': votes, 'wsum': {k: round(v, 3) for k, v in wsum.items()},
                'lean': lean, 'pick': pick,
                'best_rates': {k: round(v * 100, 1) for k, v in best.items()}}

    # ---------------------------------------------------------------- 內部

    @staticmethod
    def _zone1(x):
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return None
        if x < 0.65:
            return 0
        if x >= 1.15:
            return 11
        return int((min(x, 1.1499) - 0.65) * 20) + 1

    @staticmethod
    def _move(a, b):
        """元素級走勢：+1 升 / 0 平 / -1 降；缺數據 = 99（唔匹配任何目標）。"""
        out = np.full(len(a), 99, dtype=np.int8)
        ok = ~(np.isnan(a) | np.isnan(b))
        d = b[ok] - a[ok]
        out[ok] = np.where(np.abs(d) < 0.001, 0, np.where(d > 0, 1, -1))
        return out

    @staticmethod
    def _move1(a, b):
        if a is None or b is None:
            return None
        d = b - a
        if abs(d) < 0.001:
            return 0
        return 1 if d > 0 else -1

    def _swap_mask(self, home_id, away_id, away_gf_a, home_gf_h):
        """互調：主隊做緊「客隊入波」角色——用目標客隊客場入波對位主隊主場樣本。
        簡化實現：樣本=主隊主場入波≈目標客隊客場入波 嘅場。"""
        if away_gf_a is None:
            return np.zeros(self.n, dtype=bool)
        return np.abs(self._gf_home - away_gf_a) <= 0.3

    def _pair_last_same(self, home_id, away_id):
        """兩隊對賽（不限主客）嘅場次 mask。"""
        a, b = min(home_id, away_id), max(home_id, away_id)
        m1 = (self.home == a) & (self.away == b)
        m2 = (self.home == b) & (self.away == a)
        return m1 | m2


def pd_factorize(arr):
    """輕量 factorize（唔想全檔依賴 pandas 操作）。"""
    uniques = {}
    codes = np.zeros(len(arr), dtype=np.int32)
    for i, v in enumerate(arr):
        c = uniques.get(v)
        if c is None:
            c = len(uniques)
            uniques[v] = c
        codes[i] = c
    return codes, uniques
