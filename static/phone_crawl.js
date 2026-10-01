/* ============ 📱 手機直爬模組（2026-10-02） ============
 * 背景：Render 雲端 IP 俾 titan007 封死，伺服器爬唔到即時盤口；手機網絡直連
 * titan007 冇問題。Android APP（MainActivity WebView）注入 window.AndroidFetch
 * 原生接口（僅限 *.titan007.com，Java 側 GBK 解碼），呢個模組負責：
 *   1) 經原生接口抓 changeDetail/handicap.aspx（易胜博 12 嘅變盤歷史）
 *   2) 喺手機解析（同 crawler.py parse_odds_html/derive_snapshots 完全同邏輯）
 *   3) 即時更新畫面（每場 📱 標籤）＋ POST /api/phone-odds 寫入雲端庫共享
 * 冇 AndroidFetch（純瀏覽器）→ 模組自動停用，APP 照用伺服器數據。
 */
(function () {
  'use strict';

  /* ---- 盤口文字轉數值（正=主受讓／負=主讓球），與 crawler.py line_to_float 一致 ---- */
  const _CN = { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 };
  function lineToFloat(text) {
    let t = (text || '').trim();
    if (!t) return null;
    t = t.replace(/受让/g, '受');
    const recv = t.charAt(0) === '受' || t.charAt(0) === '*';
    if (recv) t = t.slice(1);
    let v;
    if (t === '平手') {
      v = 0.0;
    } else if (/^-?\d+(\.\d+)?(\/\d+(\.\d+)?)?$/.test(t)) {
      const vals = t.split('/').map(Number);
      v = vals.reduce((a, b) => a + b, 0) / vals.length;
      return recv ? v : -v;
    } else {
      const parts = t.split('/');
      const vals = [];
      for (const p0 of parts) {
        const p = p0.trim();
        if (p === '平手') { vals.push(0.0); continue; }
        const m = p.match(/^([一二两三四五六七八九十])?(球半|半球|球)?$/);
        if (!m) return null;
        const n = m[1] ? (_CN[m[1]] || 0) : 0;
        const u = m[2];
        if (u === '半球') vals.push(n ? n + 0.5 : 0.5);
        else if (u === '球半') vals.push(n ? n + 0.5 : 1.5);
        else if (u === '球') { if (!n) return null; vals.push(n); }
        else { if (!n) return null; vals.push(n); }
      }
      v = vals.reduce((a, b) => a + b, 0) / vals.length;
    }
    return recv ? v : -v;
  }

  /* ---- 解析 changeDetail 頁 → 七時點快照（與 crawler.py 完全相同） ---- */
  const SNAPS = [['initial', null], ['pre_4h', 240], ['pre_30m', 30],
                 ['pre_15m', 15], ['pre_10m', 10], ['pre_5m', 5], ['closing', 0]];
  function pad(n) { return String(n).padStart(2, '0'); }
  function fmtD(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
           `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function parseHandicapPage(html, kickoffStr) {
    if (!html || html.length < 200) return null;
    const kickoff = new Date(String(kickoffStr).replace(' ', 'T'));
    if (isNaN(kickoff)) return null;
    const timeline = [];
    const rowRe = /<TR[^>]*>([\s\S]*?)<\/TR>/gi;
    const cellRe = /<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi;
    let rm;
    while ((rm = rowRe.exec(html))) {
      const cells = [];
      const cr = new RegExp(cellRe.source, 'gi');
      let cm;
      while ((cm = cr.exec(rm[1]))) {
        cells.push(cm[1].replace(/<[^>]+>/g, '').trim());
      }
      if (cells.length < 6) continue;
      const status = cells.length > 6 ? cells[6] : '';
      if (status.indexOf('滚') !== -1) continue;          // 滾球列不計
      const ho = parseFloat(cells[2]);
      const ao = parseFloat(cells[4]);
      if (isNaN(ho) || isNaN(ao)) continue;               // 封盤/空白列
      const hv = lineToFloat(cells[3]);
      if (hv === null) continue;
      const m = cells[5].match(/(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})/);
      if (!m) continue;
      const year = kickoff.getFullYear();
      const cand = new Date(year, (+m[1]) - 1, +m[2], +m[3], +m[4]);
      if (cand > kickoff) cand.setFullYear(year - 1);     // 變盤唔會晚過開賽
      timeline.push([cand, hv, ho, ao]);
    }
    if (!timeline.length) return null;
    timeline.sort((a, b) => a[0] - b[0]);
    const out = { initial: timeline[0] };
    for (const [label, mins] of SNAPS.slice(1)) {
      const cutoff = label === 'closing'
        ? new Date(kickoff.getTime() + 120000)
        : new Date(kickoff.getTime() - mins * 60000);
      let last = null;
      for (const t of timeline) { if (t[0] <= cutoff) last = t; else break; }
      if (last) out[label] = last;
    }
    const snaps = {};
    for (const k in out) {
      const [d, hv, ho, ao] = out[k];
      snaps[k] = { t: fmtD(d), hv, ho, ao };
    }
    return snaps;
  }

  /* ---- 模組主體 ---- */
  const PhoneCrawl = {
    avail: typeof window !== 'undefined' && !!window.AndroidFetch,
    modeOn: false,              // 伺服器 crawl_mode='cloud' 且原生接口可用
    running: false,
    done: 0, ok: 0, fail: 0, noOdds: 0,
    res: {},                    // mid -> {snaps, at, err, noOdds}
    device: (function () {
      let d = null;
      try { d = localStorage.getItem('fa_device'); } catch (e) {}
      if (!d) {
        d = '手機-' + Math.random().toString(16).slice(2, 6).toUpperCase();
        try { localStorage.setItem('fa_device', d); } catch (e) {}
      }
      return d;
    })(),
    /* 撳掣入口：爬一場 */
    crawlOne(mid, kickoff) {
      return new Promise((resolve) => {
        let txt = null;
        try {
          txt = window.AndroidFetch.titanGet(
            'https://vip.titan007.com/changeDetail/handicap.aspx?id=' +
            mid + '&companyid=12');
        } catch (e) {
          this.res[mid] = { err: String(e) };
          this.fail++;
          resolve(this.res[mid]);
          return;
        }
        if (!txt || txt.indexOf('ERR:') === 0) {
          this.res[mid] = { err: txt || '無回應' };
          this.fail++;
          resolve(this.res[mid]);
          return;
        }
        const snaps = parseHandicapPage(txt, kickoff);
        if (!snaps) {
          this.res[mid] = { noOdds: true };
          this.noOdds++;
          resolve(this.res[mid]);
          return;
        }
        this.res[mid] = { snaps, at: fmtD(new Date()) };
        this.ok++;
        resolve(this.res[mid]);
        /* 寫入雲端庫共享（邊個做：device 名會入工作記錄） */
        try {
          fetch('/api/phone-odds', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: mid, device: this.device, snaps }),
          }).catch(() => {});
        } catch (e) {}
      });
    },
    /* 全部可見場次逐場爬（0.9 秒限流，跟伺服器爬蟲節奏） */
    async crawlAll(list, onTick) {
      if (this.running) return { running: true };
      this.running = true;
      this.done = 0; this.ok = 0; this.fail = 0; this.noOdds = 0;
      for (const m of list) {
        await this.crawlOne(m.id, m.kickoff);
        this.done++;
        if (onTick) onTick(this);
        await new Promise(r => setTimeout(r, 900));
      }
      this.running = false;
      return { done: true, ok: this.ok, fail: this.fail, noOdds: this.noOdds };
    },
    /* 畫面合併：有手機直爬結果就用佢（📱），否則 null 用返伺服器數據 */
    lineFor(mid) {
      const r = this.res[mid];
      if (!r || r.err || r.noOdds || !r.snaps) return null;
      const c = r.snaps.closing || r.snaps.initial;
      if (!c) return null;
      return { line: c.hv, ho: c.ho, ao: c.ao, at: r.at };
    },
  };

  if (typeof window !== 'undefined') window.PhoneCrawl = PhoneCrawl;
  if (typeof module !== 'undefined') module.exports = { PhoneCrawl, parseHandicapPage, lineToFloat };
})();
