/* FootballAnalysis V3 前端（2026-09-28 全面重建）
   V3 項目號：1–6 時點組／7–13 上賽vs今場時點／14–20 水位段版／21–45（見 V3_TITLES）
   V1 規則號只喺「舊版本」分頁用，同 V3 完全隔離。 */
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function pct(v){ return v==null ? '—' : (v*100).toFixed(1)+'%'; }
function rn(name, rank){ return rank ? `(${rank}) ${name}` : name; }
/* 2026-10-21：逐條件命中率——每種預測每個細規則嘅單獨歷史命中率（/api/cond_hr） */
let _CHR = {};
async function loadCondHr(){
  if (!Object.keys(_CHR).length){
    try { _CHR = await jget('/api/cond_hr') || {}; } catch(e) { _CHR = {}; }
  }
}
function condHrTxt(engine, cond, mkt){
  const m = _CHR[engine] && _CHR[engine][cond];
  if (!m) return '';
  const a = (mkt && m[mkt]) || m[Object.keys(m)[0]];
  if (!a || !a[0]) return '';
  const dec = a[1] + a[2], r = dec ? a[1] / dec : null;
  return ` <span style="color:var(--dim)">單計${r == null ? '—' : (r*100).toFixed(1)+'%'}(n=${a[0]})</span>`;
}
let toastTimer = null;
function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = 'none'; }, 2500);
}
async function _jf(url, opt, tries, timeoutMs){
  /* 伺服器更新緊／重啟中會吐 HTML 維護頁——檢查 Content-Type／首字元 '<'，
     指數退避重試，最後先抛人話錯誤（2026-09-29 修復「Unexpected token '<'」）。
     2026-09-30：GET 重試加碼至 4 次（2+4+8+16+32≈62 秒）——Render 免費版
     閒置瞓著後冷啟動要 30-60 秒，舊預算 6 秒內就放棄，搞到每次開 APP
     精選都「載入失敗」；POST 維持 1 次避免雙重提交。
     2026-10-15：每個嘗試加硬逾時（AbortController）——手機網絡凍結／雲端容器
     死透時，fetch 可以無限期 hang（「撳極無反應」元兇）；逾時當網絡故障
     行原有退避重試。POST 全部係「觸發背景作業即回」型（狀態另輪詢），
     120 秒頂天夠晒；GET 30 秒。 */
  let backoff = 2000;
  const maxTries = tries ?? 4;
  const TO = timeoutMs ?? 30000;
  for (let i = 0; ; i++) {
    if (window._jfRetryHook) { try { window._jfRetryHook(i + 1); } catch (_) {} }
    const ctl = new AbortController();
    const timer = TO > 0 ? setTimeout(() => ctl.abort(), TO) : null;
    try {
      const r = await fetch(url, Object.assign({}, opt, {signal: ctl.signal}));
      const ct = r.headers.get('Content-Type') || '';
      const looksHtml = ct.indexOf('json') === -1;
      if ((r.status >= 500 || looksHtml) && i < maxTries) {
        await new Promise(x => setTimeout(x, backoff)); backoff *= 2; continue;
      }
      const txt = await r.text();
      if (txt.charAt(0) === '<' && i < maxTries) {
        await new Promise(x => setTimeout(x, backoff)); backoff *= 2; continue;
      }
      try { return JSON.parse(txt); }
      catch (e) {
        if (i < maxTries) {
          await new Promise(x => setTimeout(x, backoff)); backoff *= 2; continue;
        }
        throw new Error('伺服器更新緊，請稍候 30 秒再試（或撳頂部「🔧 修復」）');
      }
    } catch (e) {
      markConnBad();
      const timedOut = e && e.name === 'AbortError';
      if (i >= maxTries) {
        if (timedOut)
          throw new Error('伺服器回應逾時——佢可能重緊啟，請稍候再試（或撳頂部「🔧 修復」）');
        if (e instanceof TypeError)
          throw new Error('連唔到伺服器——請檢查網絡，或撳頂部「🔧 修復」掣');
        throw e;
      }
      await new Promise(x => setTimeout(x, backoff)); backoff *= 2;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}
const jget = url => _jf(url, {}, 4, 30000);
async function jpost(url, body){
  return _jf(url, {method:'POST', headers:{'Content-Type':'application/json'},
                   body: JSON.stringify(body || {})}, 1, 120000);
}

/* ---------- 狀態 ---------- */
async function pollReady(){
  // 150×3s=450s：要覆蓋開機 V1+V3 數據池 build（本機記憶體緊絀時試過近 3 分鐘，
  // 2026-10-10「直頭入唔到」調查後 ready 改為等 V3 池，呢度係用戶睇到嘅上限）
  for (let i = 0; i < 150; i++) {
    try {
      const r = await jget('/api/ready');
      if (r.ready) { setStatus(`✓ 數據池就緒｜V3 v${r.version}`, false); return true; }
      setStatus(`⏳ 伺服器預熱中（歷史數據載入）…`, false);
    } catch (e) { setStatus('⏳ 連接伺服器中…', false); }
    await new Promise(x => setTimeout(x, 3000));
  }
  setStatus('✗ 數據池超時，請重新整理', true);
  return false;
}
function setStatus(t, isErr){
  const el = $('#status');
  el.innerHTML = isErr ? `<span class="err">${esc(t)}</span>` : esc(t);
  /* 同步去開機喚醒畫面（2026-10-15） */
  const wt = $('#wakeTxt');
  if (wt) {
    wt.textContent = t;
    wt.style.color = isErr ? '#e05c5c' : '#e6b800';
  }
}
/* ---------- 連線狀態列（2026-09-29）：🟢 正常／🟠 數據主機中斷／🔴 斷線 + 自行修復 ---------- */
let _connState = 'ok';
function _setConnChip(cls, txt){
  const el = $('#connChip');
  if (!el) return;
  el.className = cls;
  el.textContent = txt;
}
function markConnBad(){
  if (_connState === 'bad') return;
  _connState = 'bad';
  _setConnChip('bad', '🔴 伺服器斷線');
}
async function refreshConnChip(){
  /* 2026-10-24 版本號顯示（用戶：手機冇新掣——等佢一眼睇出係咪舊快取） */
  try {
    jget('/api/ready').then(r => {
      const v = $('#verChip');
      if (v && r && r.version) v.textContent = 'v' + r.version;
    }).catch(() => {});
  } catch (e) {}
  try {
    const h = await jget('/api/health');
    _connState = 'ok';
    const upd = h.last_data_update ? h.last_data_update.slice(5, 16) : '—';
    const pc = window.PhoneCrawl;
    if (pc) {
      pc.modeOn = (h.crawl_mode === 'cloud' && pc.avail);
      pc.lastPushAgo = (h.phone_push && h.phone_push.ts)
        ? Math.max(1, Math.round(Date.now() / 1000 - h.phone_push.ts)) : null;
      pc.lastPusher = (h.phone_push && h.phone_push.device) || '';
    }
    if (pc && pc.modeOn)
      _setConnChip('ok', `🟢 📱手機直爬模式（裝置 ${pc.device}）・伺服器數據 ${upd}`);
    else if (h.data_host_ok === false)
      _setConnChip('warn', `🟠 數據主機中斷・最後更新 ${upd}（30 秒後自動重試）`);
    else
      _setConnChip('ok', `🟢 連線正常・數據更新於 ${upd}`);
  } catch (e) {
    markConnBad();
  }
}
async function repairConn(){
  toast('🔧 修復緊：伺服器快取＋工序狀態檢查…');
  _setConnChip('', '⏳ 修復緊…');
  let rep = null;
  try { rep = await jpost('/api/repair', {}); } catch (e) {}
  if (rep && rep.acts && rep.acts.length) toast('🔧 ' + rep.acts.join('｜'));
  else if (rep && rep.error) toast('🔧 伺服器修復失敗：' + rep.error);
  try {
    const ok = await pollReady();
    await refreshUpdInfo();
    await refreshConnChip();
    if (curPage === 'home') loadHome();
    toast(ok ? '🔧 修復完成' : '🔧 伺服器未就緒，30 秒後會再自動試');
  } catch (e) {
    toast('🔧 修復失敗：' + e.message);
    refreshConnChip();
  }
}
/* ---------- 新一輪工作 LOG：爬取→重算→工具頁→新場／剔走，全部項目 ---------- */
function _workLogEvent(e){
  const n = v => v == null ? '—' : v;
  const match = e.match_text || '整體工序';
  let text = '', cls = '';
  if (e.kind === 'crawl') {
    text = `爬取盤口及賠率｜${match}｜${(e.markets || []).join('＋') || e.tool || '盤口'}｜` +
      `已爬取 ${n(e.done)}／總爬取 ${n(e.total)}｜${e.status || ''}`;
  } else if (e.kind === 'recalc') {
    text = `爬取後重算｜${e.tool || '全部工具'}｜${match}｜` +
      `進度 ${n(e.done)}/${n(e.total)}｜${e.status || ''}`;
  } else if (e.kind === 'push') {
    text = `已推送至工具頁｜${(e.pages || []).join('、') || '未標頁面'}｜${match}`;
  } else if (e.kind === 'new') {
    text = `【新場】${e.tool || '未標工具'}．${e.direction || '方向未記錄'}．${match}`;
    cls = 'NEW';
  } else if (e.kind === 'removed') {
    text = `【剔走】${e.tool || '未標工具'}．${e.direction || '方向未記錄'}．${match}`;
    cls = 'ERR';
  } else {
    text = `${e.tool ? e.tool + '｜' : ''}${e.message || e.status || match}`;
    cls = e.kind === 'error' ? 'ERR' : '';
  }
  if (e.message && !['status', 'error'].includes(e.kind)) text += `｜${e.message}`;
  return `<div class="wl-row ${cls}"><span class="wl-ts">${esc((e.ts || '').slice(5, 19))}</span>${esc(text)}</div>`;
}
async function refreshWorkLog(){
  const box = $('#workLogBox');
  if (!box || !box.open) return;
  let d;
  try { d = await jget('/api/worklog'); }
  catch (e) { $('#workLog').innerHTML = '<div class="err">工作 LOG 載入失敗：' + esc(String(e)) + '</div>'; return; }
  const job = d.job || {}, events = d.events || [];
  $('#workLogSum').textContent = job.id
    ? `${job.source || ''}｜${job.started_at || ''}｜本輪 ${events.length} 項全部顯示`
    : '未有新工作記錄';
  let live = '';
  if (d.active) {
    const a = d.active;
    live += `<div class="wl-live">進行中：${esc(a.source || '')}｜${esc(a.phase || '')}｜` +
      `已爬取 ${a.done == null ? '—' : a.done}／總爬取 ${a.total == null ? '—' : a.total}</div>`;
  }
  if (d.recalc && d.recalc.running) {
    const r = d.recalc;
    live += `<div class="wl-live">爬取後重算中：${esc(r.tool || '全部工具')}｜${esc(r.match || '')}｜` +
      `${r.done || 0}/${r.total || 0}｜觸發：${esc(r.triggered_by || '')}</div>`;
  }
  if (d.error) live += `<div class="wl-row ERR">${esc(d.error)}</div>`;
  $('#workLog').innerHTML = live + (events.map(_workLogEvent).join('') ||
    '<div class="note">暫無新工作項目；執行更新後，呢度會逐項顯示完整流程。</div>');
}
async function refreshUpdInfo(){
  try {
    const s = await jget('/api/update-status');
    const w = await jget('/api/update-window-status');
    let t = '';
    if (s.running) t += `⟳更新中：${s.phase || ''} `;
    else if (s.error) t += `⚠️ 上次更新失敗：${s.error}`;
    else if (s.last_done) {
      const ago = s.last_done_ago != null ? Math.round(s.last_done_ago / 60) + '分鐘前' : '';
      let detail = '';
      const lr = s.last_result;
      if (lr && typeof lr === 'object') {
        const bits = [];
        if (lr.seasons != null) bits.push(`賽季檔 ${lr.seasons}`);
        if (lr.odds != null) bits.push(`補盤口 ${lr.odds} 場`);
        if (lr.odds_refresh) bits.push(`刷新盤口 ${lr.odds_refresh}`);
        if (lr.odds_fill) bits.push(`補爬盤口 ${lr.odds_fill}`);
        detail = '（' + bits.join('｜') + '）';
      }
      t += `✓ 上次更新完成${ago ? `（${ago}）` : ''}${detail}`;
    }
    if (w.running) t += `｜⚡${w.window_name || ''}更新中 ${w.done}/${w.total}`;
    else if (w.error) t += `｜⚡窗口更新失敗：${w.error}`;
    else if (w.last) t += `｜⚡上次窗口更新（${w.window_name || ''}）：成 ${w.ok || 0}／敗 ${w.fail || 0}` +
      (!w.total ? '（呢個時段冇即將開賽場次）' : `／共 ${w.total} 場`);
    $('#updInfo').textContent = t;
    $$('#btnUpdate,#btnWin24,#btnWin30,#btnWin10').forEach(b => {
      b.disabled = !!(s.running || w.running);
    });
    _updBusy = !!(s.running || w.running);
    return _updBusy;
  } catch (e) { return false; }
}
let _updBusy = false;

/* ---------- 分頁 ---------- */
let curPage = 'home';
function goto(pg){
  curPage = pg;
  $$('.pg').forEach(el => el.classList.remove('on'));
  $('#pg-' + pg).classList.add('on');
  $$('#tabs .tab').forEach(b => b.classList.toggle('on', b.dataset.pg === pg));
  ({featw: () => loadFeaturedW(),
    check: () => loadCheckPage(),
    fw7: () => loadFwGrid('7'),
    fw8: () => loadFwGrid('8'),
    fw12: () => loadFwGrid('12'),
    review: () => loadReview(),
    htft: () => loadHtft(),
    picks: () => loadPicksPage(),
    featlog: () => loadFeatlog(),
    oupred: () => loadOupred(),
    feathourly: () => loadFeatHourly(),
    ou: () => loadOuRules(),
    htftm: () => loadHtftRules(),
    pstat: () => loadPredStats(),
    haobao: () => {
      // 2026-10-26：分聯賽表喺 iframe 外面（父文件）mount——iframe 內 league7.js
      // 執行異常（零錯誤零盒），父文件掛載同其他工具頁同款機制。
      League7.mount('hb', $('#hbLg7Mount'));
      const f = $('#hbFrame');
      if (f && !f.src) f.src = '/haobao/';
    },
    fw8s: () => {
      League7.mount('8s', $('#fw8sLg7Mount'));
      const f = $('#fw8sFrame');
      if (f && !f.src) f.src = '/fw8s';
    },
    w88: () => {
      League7.mount('88w', $('#w88Lg7Mount'));
      const f = $('#w88Frame');
      if (f && !f.src) f.src = '/88w';
    },
    newjx: () => {
      League7.mount('jx', $('#newjxLg7Mount'));
      const f = $('#jxFrame');
      if (f && !f.src) f.src = '/newjx';
    },
    x8: () => loadX8(),
    fw12lib: () => loadFw12Lib(),
    v1: () => loadV1(),
    removed: () => loadRemoved(),
    settings: () => loadSettings(),
    home: () => loadHome()}[pg] || (() => {}))();
  window.scrollTo(0, 0);
}
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('.tab');
  if (b && b.dataset.pg) goto(b.dataset.pg);
});

/* ---------- ⚡ 半全場逆轉統計（獨立頁） ---------- */
let htftHours = 48, htftCat = 'all';   // 已完結回查篩選（2026-10-10，二選一或二選二）
function htftSetHours(v){ htftHours = +v; loadHtft(); }
function htftSetCat(v){ htftCat = v; loadHtft(); }
const HTFT_CAT = ['—', '①半主全和', '②半主全客', '③半客全和', '④半客全主'];
function _htftStatsRow(r){
  if (!r.n) return '<span class="note">同類歷史樣本不足</span>';
  const pc = v => pct(v);
  const nd = r.nd ? `＋半場和${r.nd}場` : '';
  return `<span title="有半場領先嘅同類場次中：①半場主勝→全場和 ②半主→全客 ③半客→全和 ④半客→全主">` +
    `同類(n=${r.n}${nd})：①${pc(r.p[0])} ②${pc(r.p[1])} ③${pc(r.p[2])} ④${pc(r.p[3])}｜` +
    `<b style="color:var(--gold2)">任何一項 ${pc(r.p_any)}</b></span>`;
}
function _htftMatchCard(r){
  let h = `<details class="htft-card"><summary>` +
    `<span class="ko">${esc((r.ko || '').slice(5, 16))}</span> ` +
    `<span class="lg">${esc(r.lg)}</span> ` +
    `<b>${esc(r.home)} <span style="color:var(--dim)">vs</span> ${esc(r.away)}</b> ` +
    `<span class="tag dim">${esc(r.line)}</span> `;
  if (r.ht) h += `<span class="sc">半場 ${esc(r.ht)}</span> `;
  if (r.ft) h += `<span class="sc">全場 ${esc(r.ft)}</span> `;
  if (r.cat) h += `<span class="tag" style="color:var(--gold2);border-color:#6b5510">${HTFT_CAT[r.cat]}</span>`;
  else if (r.ft) h += `<span class="tag dim">唔屬四項</span>`;
  h += `<br><span class="sub">${_htftStatsRow(r)}</span>`;
  if (r.live_p){
    if (r.live_side === 'draw'){
      h += `<br><span class="sub" style="color:var(--up)">同類半場和走勢：` +
           `主勝 ${pct(r.live_p[0])}｜和 ${pct(r.live_p[1])}｜客勝 ${pct(r.live_p[2])}</span>`;
    } else {
      const lead = (r.ht || '').split('-');
      const homeLead = +lead[0] > +lead[1];
      h += `<br><span class="sub" style="color:var(--up)">同類同半場${homeLead ? '主' : '客'}領先走勢：` +
           `${homeLead ? '主勝' : '客勝'} ${pct(r.live_p[0])}｜和 ${pct(r.live_p[1])}｜` +
           `${homeLead ? '被逆轉（客勝）' : '被逆轉（主勝）'} ${pct(r.live_p[2])}</span>`;
    }
  }
  h += `</summary>`;
  if (r.ex && r.ex.length){
    h += `<div class="body"><table class="ck-table"><tr><th>日期</th><th>場次</th><th>半場</th><th>全場</th><th>類別</th></tr>` +
      r.ex.map(e => `<tr><td>${esc(e.ko)}</td><td>${esc(e.t)}</td><td>${esc(e.ht)}</td><td>${esc(e.ft)}</td>` +
        `<td>${e.cat ? HTFT_CAT[e.cat] : '—'}</td></tr>`).join('') + `</table></div>`;
  } else {
    h += `<div class="body"><div class="note">冇同類歷史場次</div></div>`;
  }
  h += `</details>`;
  return h;
}
async function loadHtft(){
  const box = $('#htftBody');
  box.innerHTML = '<div class="note">載入中…</div>';
  let b, d;
  try {
    const q = (htftHours !== 48 || htftCat !== 'all')
      ? `?fin_hours=${htftHours}` + (htftCat !== 'all'
        ? '&fin_cat=' + encodeURIComponent(htftCat) : '')
      : '';
    b = await jget('/api/htftboard' + q);
    d = await jget('/api/htft');
  } catch (e) { box.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  let h = '';
  h += `<div class="day-h">🔴 進行中（${(b.live || []).length} 場）</div>`;
  h += (b.live || []).map(_htftMatchCard).join('') || '<div class="note">而家冇進行中場次。</div>';
  h += `<div class="day-h">⏰ 即將開賽（未來48小時・${(b.upcoming || []).length} 場）</div>`;
  h += (b.upcoming || []).map(_htftMatchCard).join('') || '<div class="note">冇即將開賽場次。</div>';
  /* 2026-10-10：已完結回查篩選——Period／聯賽杯賽類，獨立可疊加（二選一或二選二） */
  const finTitle = htftHours >= 720 ? '近30日' : htftHours >= 168 ? '近7日'
    : htftHours >= 72 ? '近3日' : `最近${htftHours}小時`;
  h += `<div class="statbar">已完結回查：Period
    <select onchange="htftSetHours(this.value)">
      <option value="24"${htftHours===24?' selected':''}>最近24小時</option>
      <option value="48"${htftHours===48?' selected':''}>最近48小時</option>
      <option value="72"${htftHours===72?' selected':''}>近3日</option>
      <option value="168"${htftHours===168?' selected':''}>近7日</option>
      <option value="720"${htftHours===720?' selected':''}>近30日</option>
    </select>
    ｜類型
    <select onchange="htftSetCat(this.value)">
      <option value="all"${htftCat==='all'?' selected':''}>全部</option>
      <option value="聯賽"${htftCat==='聯賽'?' selected':''}>聯賽</option>
      <option value="杯賽"${htftCat==='杯賽'?' selected':''}>杯賽</option>
    </select>
    <span class="hint">（兩個篩選可單用可疊用）</span></div>`;
  h += `<div class="day-h">✅ 已完結（${finTitle}${htftCat !== 'all' ? '・' + esc(htftCat) : ''}・${(b.finished || []).length} 場）</div>`;
  h += (b.finished || []).map(_htftMatchCard).join('') || '<div class="note">呢個條件下冇已完結場次。</div>';
  /* 組合統計表（原有） */
  const t = d.total || {n: 0, p: [0, 0, 0, 0], p_any: 0};
  h += `<div class="day-h">📊 精選組合歷史統計（check_rows 全庫）</div>`;
  h += '<table class="ck-table"><tr><th>組合（原盤／互換）</th><th>方向</th><th>n</th>' +
    '<th>①半主全和</th><th>②半主全客</th><th>③半客全和</th><th>④半客全主</th><th>任何一項</th></tr>';
  h += `<tr class="hl"><td><b>⭐ 全部精選</b></td><td>—</td><td><b>${t.n}</b></td>` +
    t.p.map(x => `<td>${pct(x)}</td>`).join('') +
    `<td><b style="color:var(--gold2)">${pct(t.p_any)}</b></td></tr>`;
  for (const r of d.rows || []) {
    h += `<tr><td>${esc(r.g14)}／${esc(r.g17)}</td>` +
      `<td>${r.direction === 'up' ? '<span class="r-up">上</span>' : '<span class="r-down">下</span>'}</td>` +
      `<td>${r.n}</td>` + r.p.map(x => `<td>${pct(x)}</td>`).join('') +
      `<td><b>${pct(r.p_any)}</b></td></tr>`;
  }
  h += '</table>';
  h += `<div class="note">更新於 ${esc(b.updated || '')}｜同類＝同聯賽＋同尾盤（讓球/讓球方）；例子場次＝同類最近 4 場。資料每 5 分鐘自動更新（撳返個分頁即刷新）。</div>`;
  box.innerHTML = h;
}
/* ---------- 主頁 ---------- */
let homeData = {upcoming: [], played: []};
/* 已完賽回查篩選（2026-10-11，二選一或二選二，同半全場頁一樣玩法） */
let playedHours = 120, playedCat = 'all';
function playedSetHours(v){ playedHours = +v; loadHome(); }
function playedSetCat(v){ playedCat = v; loadHome(); }
/* 指定聯賽（2026-10-11 用戶規格：揀聯賽類可以再揀指定聯賽，例如英超）——
   聯賽清單由已載入數據提取，純 client-side 過濾，撳完即時變唔使等 fetch */
let playedLg = 'all';
function playedSetLg(v){ playedLg = v; renderHome(); }
async function loadHome(){
  $('#homeList').innerHTML = '<div class="note">⏳ 載入緊：賽事清單（即將開賽＋已完賽）…</div>';
  LoadX.show('載入賽事清單');
  const hours = $('#hoursSel').value;
  let q = '/api/v3/list?hours=' + hours;
  if (playedHours !== 120 || playedCat !== 'all'){
    q += '&fin_hours=' + playedHours;
    if (playedCat !== 'all') q += '&fin_cat=' + encodeURIComponent(playedCat);
  }
  try {
    window._jfRetryHook = n => {
      $('#homeList').innerHTML = '<div class="note">⏳ 伺服器重啟緊／網絡不穩，自動重試中（第 '
        + n + ' 次，最長約 90 秒）…</div>';
    };
    const d = await jget(q);
    window._jfRetryHook = null;
    homeData = d;
    LoadX.hide();
    jget('/api/lgpred').then(p => { lgPred = p.predictions || {}; })
      .catch(() => {});
    renderHome();
    fillCheckSelect($('#ckMatch'), '— 揀場次 —');
    checkLgAlerts();
  } catch (e) {
    window._jfRetryHook = null;
    LoadX.hide();
    $('#homeList').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>';
  }
}

/* ---------- 聯賽規則提示（>78% 規則出現即提示） ---------- */
let lgAlerts = [];
async function checkLgAlerts(){
  let d;
  try { d = await jget('/api/lgalerts'); } catch (e) { return; }
  lgAlerts = d.all || [];
  const n = (d.new || []).length;
  const bell = $('#alertBell'), cnt = $('#alertCnt');
  if (n > 0) {
    cnt.style.display = 'inline';
    cnt.textContent = n;
    bell.style.color = '#ffd34d';
    if (!checkLgAlerts._toastAt || Date.now() - checkLgAlerts._toastAt > 60000) {
      toast('🎯 ' + n + ' 場新提示：有賽事符合 >78% 聯賽規則！');
      checkLgAlerts._toastAt = Date.now();
    }
  } else {
    cnt.style.display = 'none';
    bell.style.color = '';
  }
}
function renderAlertPanel(){
  const box = $('#alertList');
  if (!lgAlerts.length) {
    box.innerHTML = '<div class="note">暫無符合 >78% 聯賽規則嘅未開賽賽事。</div>';
    return;
  }
  box.innerHTML = lgAlerts.map(x => {
    const aud = x.giver === 'home' ? {up: x.home, down: x.away}
      : x.giver === 'away' ? {up: x.away, down: x.home}
      : {up: x.home, down: x.away};
    const dteam = x.direction === 'up' ? aud.up : aud.down;
    return `<div class="mrow" data-mid="${x.id}" style="display:block;padding:8px 4px;border-bottom:1px solid #222a3a;cursor:pointer">
      <div><b>${esc((x.kickoff || '').slice(5, 16))}</b>　${esc(x.league)}</div>
      <div>${esc(x.home)} <span class="r">vs</span> ${esc(x.away)}</div>
      <div class="sub">規則：${esc(x.rule)}</div>
      <div>方向：<b class="${x.direction === 'up' ? 'r-up' : 'r-down'}">${x.direction === 'up' ? '上盤' : '下盤'}（${esc(dteam)}）</b>
        <span style="color:var(--dim)">（歷史 ${pct(x.up_r)} 上・${x.n}場基數）</span></div>
    </div>`;
  }).join('');
  [...box.querySelectorAll('.mrow')].forEach(r => r.onclick = () => {
    $('#alertPanel').style.display = 'none';
    openDetail(+r.dataset.mid);
  });
}
$('#alertBell').onclick = async () => {
  const p = $('#alertPanel');
  if (p.style.display === 'none') {
    renderAlertPanel();
    p.style.display = 'block';
    try { await jget('/api/lgalerts?mark=1'); } catch (e) {}
    checkLgAlerts();
  } else {
    p.style.display = 'none';
  }
};
$('#alertClose').onclick = () => { $('#alertPanel').style.display = 'none'; };
/* ---------- 2026-10-22 工具入選/剔走推送（📣） ---------- */
let _toolBellSeen = 0;   // 已知最新 id——輪詢見到新 id 就 badge＋瀏覽器通知
let _toolBellNotifOn = false;
async function checkToolBell(first){
  let d;
  try { d = await jget('/api/alerts?limit=50'); } catch (e) { return; }
  if (!d) return;
  const cnt = $('#toolBellCnt');
  if (d.unread > 0){
    cnt.style.display = 'inline';
    cnt.textContent = d.unread > 99 ? '99+' : d.unread;
  } else {
    cnt.style.display = 'none';
  }
  const items = d.items || [];
  const fresh = first ? [] : items.filter(x => x.id > _toolBellSeen);
  _toolBellSeen = Math.max(_toolBellSeen, items.length ? items[0].id : 0);
  if (_toolBellNotifOn && fresh.length && window.Notification
      && Notification.permission === 'granted'){
    for (const x of fresh.slice(0, 3)){
      try { new Notification(' Football Analysis', { body: x.msg }); } catch (e) {}
    }
  }
}
function renderToolBellPanel(){
  jget('/api/alerts?limit=50').then(d => {
    const box = $('#toolBellList');
    if (!d || !d.items.length){
      box.innerHTML = '<div class="note">暫無推送。統一掃描/即時更新時，新入選同被剔走會喺度出現。</div>';
      return;
    }
    box.innerHTML = d.items.map(x =>
      `<div class="wl-row ${x.kind === 'removed' ? 'ERR' : ''}" style="${x.kind === 'new' ? '' : 'color:#ff9d9d'}">` +
      `<span class="wl-ts">${esc((x.ts || '').slice(5, 16))}</span>${esc(x.msg)}</div>`
    ).join('');
  }).catch(() => {});
}
$('#toolBell').onclick = async () => {
  const p = $('#toolBellPanel');
  if (p.style.display === 'none'){
    renderToolBellPanel();
    p.style.display = 'block';
    try { await fetch('/api/alerts/read', {method: 'POST'}); } catch (e) {}
    checkToolBell(true);
  } else {
    p.style.display = 'none';
  }
};
$('#toolBellClose').onclick = () => { $('#toolBellPanel').style.display = 'none'; };
$('#toolBellNotif').onchange = async function(){
  _toolBellNotifOn = this.checked;
  if (this.checked && window.Notification && Notification.permission === 'default'){
    try { await Notification.requestPermission(); } catch (e) {}
  }
};
function _stateTag(st){
  if (st === 'live') return '<span class="tag live">進行中</span>';
  if (st === 'finished') return '<span class="tag dim">完場</span>';
  if (st === 'unknown') return '<span class="tag dim" title="開賽逾26小時仍無比分——可能取消/延期或數據缺失，唔再當進行中">未知</span>';
  return '';
}
/* 半場賽果＋首隊入球 標籤（2026-10-02：所有已完結場次顯示） */
function _htftTag(m){
  let s = '';
  if (m.ht) s += ` <span class="tag dim" title="半場賽果">半${esc(m.ht)}</span>`;
  if (m.first_goal) {
    const g = m.first_goal === 'home' ? (m.home || '主') : (m.away || '客');
    s += ` <span class="tag" style="color:var(--gold2);border-color:#6b5510" title="第一隊入球">首球:${esc(String(g).slice(0, 6))}</span>`;
  }
  return s;
}
function fmtAt(s){
  // 'YYYY-MM-DD HH:MM:SS' → 今日顯示 HH:MM；隔日顯示 MM-DD HH:MM
  if (!s) return '';
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  const today = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return s.slice(0, 10) === today ? s.slice(11, 16) : s.slice(5, 16);
}
function fmtLine(v){
  /* 帶方向盤口數值轉文字（同 screen_engine.fmt_line）：-0.5→主讓半球 +0.25→客讓平/半 */
  const h = Math.abs(v);
  const giver = v < 0 ? 'home' : (v > 0 ? 'away' : null);
  const names = {0:'平手', 0.25:'平/半', 0.5:'半球', 0.75:'半/一', 1:'一球',
                 1.25:'一/球半', 1.5:'球半', 1.75:'球半/兩', 2:'兩球',
                 2.25:'兩/兩球半', 2.5:'兩球半'};
  if (!giver || h === 0) return '平手';
  const r = Math.round(h * 100) / 100;
  let name = names[r];
  if (!name) {
    const whole = Math.floor(r), frac = Math.round((r - whole) * 100) / 100;
    const cn = ['','一','二','三','四','五','六'][whole] || (whole + '');
    const fmap = {0:'', 0.25:'平/半', 0.5:'半球', 0.75:'半/一'};
    name = frac ? (cn || '') + (whole ? '/' : '') + (fmap[frac] || '') : cn + '球';
  }
  return (giver === 'home' ? '主讓' : '客讓') + name;
}
/* ---------- 🎯 我想投注（分享單，2026-10-02） ----------
 * 任何賽事顯示位撳「🎯投注」→ 揀該場盤口±0.25 → 入金額 → 撳「分享」→
 * 入水位 → 再撳「分享」出完整文字。文字格式：
 *   日-月 聯賽/杯賽名 主隊 vs 客隊 選擇盤口 水位 金額
 */
const BetSlip = {
  m: null, baseV: 0,
  btn(m, label){
    const d = esc(JSON.stringify({id: m.id, ko: m.kickoff || '',
                                  lg: m.league || '', h: m.home || '',
                                  a: m.away || ''}));
    return `<button class="bet-betbtn" data-bet="${d}">${label || '🎯投注'}</button>`;
  },
  async open(preset){
    this.m = preset;
    this.baseV = 0;
    this.market = null;
    $('#betMeta').textContent =
      `${(preset.ko || '').slice(5, 16)}　${preset.lg}　${preset.h} vs ${preset.a}`;
    $('#betMsg').textContent = '';
    $('#betAmt').value = localStorage.getItem('bet_amt') || '';
    $('#betWater').value = '';
    try {
      const r = await jget('/api/v3/target?id=' + preset.id);
      const t = r && r.target;
      const c = t && (t.close || t.init);
      this.market = c || null;
      if (c && typeof c.v === 'number') this.baseV = c.v;
    } catch (e) {}
    const q = x => Math.round(x * 100) / 100;
    const base = q(this.baseV);
    const opts = [...new Set([q(base - 0.25), base, q(base + 0.25)])];
    const m = preset;
    /* 每個盤：讓球方＝上盤；平手盤：上盤＝主隊（注明名稱） */
    const sides = v => {
      const giver = v < 0 ? 'home' : (v > 0 ? 'away' : null);
      const up = giver === 'away' ? m.a : m.h;
      const dn = giver === 'away' ? m.h : m.a;
      return { up, dn };
    };
    const optHTML = (v, side) => {
      const s = sides(v);
      const t = side === 'up' ? s.up : s.dn;
      const sel = (v === base && side === 'up') ? ' checked' : '';
      /* 下盤用受讓方角度寫：主/客讓 → 「受讓」；平手維持「平手」（用戶 2026-10-02 指正） */
      const lineTxt = side === 'down'
        ? fmtLine(v).replace(/^主讓|^客讓/, '受讓')
        : fmtLine(v);
      return `<label><input type="radio" name="betline" value="${v}|${side}"${sel}>` +
             `<span><b>${side === 'up' ? '上盤' : '下盤'}（${esc(t)}）</b><br>` +
             `${esc(lineTxt)}</span></label>`;
    };
    $('#betLines').innerHTML =
      `<div class="bet-sec-t">上盤（讓球方；平手＝主隊）</div><div class="bet-lines">` +
      opts.map(v => optHTML(v, 'up')).join('') + `</div>` +
      `<div class="bet-sec-t">下盤（受讓方；平手＝客隊）</div><div class="bet-lines">` +
      opts.map(v => optHTML(v, 'down')).join('') + `</div>`;
    const setWater = () => {
      const sel = this.sel(), w = this.marketWater(sel.v, sel.side);
      if (w != null) $('#betWater').value = w;
    };
    setWater();
    $$('input[name=betline]').forEach(r => r.addEventListener('change', setWater));
    $('#betModal').classList.add('on');
    setTimeout(() => { if (!$('#betAmt').value) $('#betAmt').focus(); }, 80);
  },
  marketWater(v, side){
    const c = this.market;
    if (!c) return null;
    const upHome = v <= 0;
    const homeSelected = side === 'up' ? upHome : !upHome;
    return homeSelected ? c.ho : c.ao;
  },
  currentOdds(){
    const typed = $('#betWater').value.trim();
    if (typed) return typed;
    const sel = this.sel();
    return this.marketWater(sel.v, sel.side);
  },
  sel(){
    const r = document.querySelector('input[name=betline]:checked');
    if (!r) return { v: 0, side: 'up' };
    const [v, side] = r.value.split('|');
    return { v: +v, side };
  },
  txt(withWater){
    const m = this.m;
    if (!m) return '';
    const ko = m.ko || '';
    const dm = ko.slice(8, 10) + '-' + ko.slice(5, 7);
    const hhmm = ko.slice(11, 16);
    const amt = '$' + $('#betAmt').value.trim();
    const { v, side } = this.sel();
    let lineTxt = fmtLine(v);
    if (side === 'down') lineTxt = lineTxt.replace(/^主讓|^客讓/, '受讓');
    const core = `${dm} ${hhmm} ${m.lg} ${m.h} vs ${m.a} ${lineTxt} ` +
                 (side === 'down' ? '下盤' : '上盤');
    return withWater
      ? `${core} ${$('#betWater').value.trim()} ${amt}`
      : `${core}、${amt}`;
  },
  close(){ $('#betModal').classList.remove('on'); },
};
document.addEventListener('click', ev => {
  const b = ev.target.closest('[data-bet]');
  if (!b) return;
  ev.stopPropagation();
  try { BetSlip.open(JSON.parse(b.dataset.bet)); } catch (e) {}
});
$('#betClose').onclick = () => BetSlip.close();
$('#betModal').addEventListener('click', ev => {
  if (ev.target.id === 'betModal') BetSlip.close();
});
$('#betShareNoW').onclick = async () => {
  const amount = Number($('#betAmt').value.trim());
  if (!Number.isFinite(amount) || amount <= 0) {
    toast('請先輸入有效本金'); $('#betAmt').focus(); return;
  }
  const sel = BetSlip.sel(), odds = BetSlip.currentOdds();
  if (odds == null || !Number.isFinite(Number(odds)) || Number(odds) <= 0) {
    toast('未能讀取本場賠率，請自行輸入有效賠率'); $('#betWater').focus(); return;
  }
  const btn = $('#betShareNoW');
  btn.disabled = true;
  try {
    const r = await jpost('/api/bets', {id: BetSlip.m.id, choice: sel.side,
      line: sel.v, odds: Number(odds), stake: amount});
    if (!r.ok) { toast(r.error || '新增至我的選擇失敗'); return; }
    localStorage.setItem('bet_amt', String(amount));
    localStorage.setItem('bet_water', String(odds));
    shareText(BetSlip.txt(false));
    BetSlip.close();
    refreshPicksMap();
    toast('已新增至我的選擇');
  } catch (e) {
    toast('新增失敗：' + e.message);
  } finally {
    btn.disabled = false;
  }
};
$('#betShare').onclick = () => {
  if (!$('#betAmt').value.trim()) { toast('請先輸入金額'); $('#betAmt').focus(); return; }
  localStorage.setItem('bet_amt', $('#betAmt').value.trim());
  const w = $('#betWater').value.trim();
  if (!w) {   /* 第一段：提示輸入水位，再撳一次先真分享 */
    toast('請輸入水位（如 0.85），再撳一次「分享」');
    $('#betWater').focus();
    $('#betMsg').textContent = '等緊水位…';
    return;
  }
  localStorage.setItem('bet_water', w);
  $('#betMsg').textContent = '';
  shareText(BetSlip.txt(true));
  BetSlip.close();
};
/* ---------- 👯 搵孖生（2026-10-13） ----------
 * 主頁每場「👯搵孖生」掣 → /api/twin → 全庫＋同聯賽各搵歷史上
 * 六項盤口完全相同（盤口 100% 同、賠率 ±0.03）嘅完場場次，
 * 展示上/下/走盤%。條件：上仗對賽盤口＋贏盤方、初盤、開賽前四小時、
 * 尾盤、尾盤大小球（盤口＋大細賠率）。
 */
const Twin = {
  btn(m, label){
    const d = esc(JSON.stringify({id: m.id, ko: m.kickoff || '',
                                  lg: m.league || '', h: m.home || '',
                                  a: m.away || ''}));
    return `<button class="twin-btn" data-twin="${d}" title="全資料庫＋同聯賽，搵盤口完全相同嘅孖生場次">👯${label || '搵孖生'}</button>`;
  },
  async open(d){
    $('#twinMeta').textContent =
      `${(d.ko || '').slice(5, 16)}　${d.lg}　${d.h} vs ${d.a}`;
    $('#twinBody').innerHTML =
      '<div class="note">⏳ 搵緊孖生…（全資料庫掃描，約需幾秒）</div>';
    $('#twinModal').classList.add('on');
    let r;
    try {
      r = await jget('/api/twin?id=' + d.id);
    } catch (e) {
      $('#twinBody').innerHTML = `<div class="note">⚠ 查詢失敗：${esc(e.message)}</div>`;
      return;
    }
    if (!r || r.error) {
      $('#twinBody').innerHTML =
        `<div class="note">⚠ ${esc((r && r.error) || '未知錯誤')}</div>`;
      return;
    }
    const crit = (r.criteria || []).map(c =>
      `<div class="twin-crit">・${esc(c)}</div>`).join('');
    $('#twinBody').innerHTML =
      `<div class="twin-crit-t">孖生條件（全部相同）</div>${crit}` +
      `<div class="twin-cards">${this._card('🌍 全資料庫', r.all)}` +
      `${this._card('🏆 同聯賽・杯賽（' + (r.league || '') + '）', r.league_stat)}</div>` +
      this._list(r.list || [], r.list_total || 0);
  },
  _card(title, s){
    if (!s || !s.n) {
      return `<div class="twin-card"><h4>${esc(title)}</h4>` +
             `<div class="note">0 場孖生</div></div>`;
    }
    const u = s.up_pct, dn = s.down_pct, p = s.push_pct;
    return `<div class="twin-card"><h4>${esc(title)}　<b>${s.n}場</b></h4>` +
      `<div class="twin-pct">` +
      (u > 0 ? `<span class="up" style="width:${u}%">上${u}%</span>` : '') +
      (p > 0 ? `<span class="pw" style="width:${p}%">走${p}%</span>` : '') +
      (dn > 0 ? `<span class="dn" style="width:${dn}%">下${dn}%</span>` : '') +
      `</div>` +
      `<div class="twin-nums">上盤 ${s.up} 場・下盤 ${s.down} 場・走盤 ${s.push} 場</div>` +
      `</div>`;
  },
  _list(list, total){
    if (!total) return '';
    const badge = {A: '<span class="twin-r r-up">上</span>',
                   B: '<span class="twin-r r-down">下</span>',
                   P: '<span class="twin-r r-pw">走</span>'};
    const rows = list.map(t =>
      `<div class="twin-row${t.same_lg ? ' same' : ''}">` +
      `<span class="ko">${esc((t.ko || '').slice(0, 16))}</span>` +
      `<span class="lg">${esc(t.lg)}${t.same_lg ? ' 🏆' : ''}</span>` +
      `<span class="tm">${esc(t.h)} <span class="vs">vs</span> ${esc(t.a)}</span>` +
      `<span class="sc">${esc(t.score)}</span>${badge[t.r] || ''}</div>`).join('');
    const more = total > list.length
      ? `<div class="note">…仲有 ${total - list.length} 場（只列最近 ${list.length} 場）</div>` : '';
    return `<div class="twin-crit-t">孖生場次（最近 ${Math.min(list.length, total)} 場・🏆=同聯賽）</div>` +
           `<div class="twin-list">${rows}</div>${more}`;
  },
  close(){ $('#twinModal').classList.remove('on'); },
};
document.addEventListener('click', ev => {
  const b = ev.target.closest('[data-twin]');
  if (!b) return;
  ev.stopPropagation();
  try { Twin.open(JSON.parse(b.dataset.twin)); } catch (e) {}
});
$('#twinClose').onclick = () => Twin.close();
$('#twinModal').addEventListener('click', ev => {
  if (ev.target.id === 'twinModal') Twin.close();
});
/* ---------- 同聯賽/杯賽命中率（2026-10-17 用戶）：六源速查 ----------
 * 後端 /api/lg_hit_rates（600s 快取）回 {lg: {src: [命中, n]}}。
 * 渲染用 slot：先放空位，load 完一次過填——同步渲染同動態列表都合用。 */
const LgHit = {
  rates: null,
  _p: null,
  load(){
    if (this._p) return this._p;
    this._p = jget('/api/lg_hit_rates').then(d => {
      this.rates = (d && d.lg) || {};
      _fillLgSlots();
      return this.rates;
    }).catch(() => { this.rates = {}; return {}; });
    return this._p;
  },
  fmt(v){ return (v && v[1] >= 5) ? Math.round(v[0] / v[1] * 100) + '%' : null; },
  chips(lg, keys){
    const r = (this.rates || {})[lg];
    if (!r) return '';
    const names = {main: '主', feat_v1: '精V1', feat_v2: '精V2',
                   f7: '精7', f8: '精8', f12: '精12', f8s: '精8s', hb: '波'};
    const parts = [];
    for (const k of keys){
      const v = r[k], f = v && this.fmt(v);
      if (f) parts.push(names[k] + f + (v[1] < 20 ? '<small style="opacity:.65">·n' + v[1] + '</small>' : ''));
    }
    return parts.length
      ? '<span style="font-size:10px;color:var(--gold2)" title="同聯賽/杯賽命中率（贏÷(贏+輸)，走唔計）">🏆 ' + parts.join('｜') + '</span>'
      : '';
  }
};
function _lgRateSlot(lg, keys){
  return '<span class="lgr-slot" data-lg="' + esc(lg || '') + '" data-keys="' + keys.join(',') + '"></span>';
}
function _fillLgSlots(){
  if (!LgHit.rates) return;
  document.querySelectorAll('.lgr-slot').forEach(el => {
    if (el._lgrDone) return;
    const c = LgHit.chips(el.dataset.lg, (el.dataset.keys || '').split(','));
    if (c){ el.innerHTML = c; el._lgrDone = true; }
  });
}
LgHit.load();
setInterval(() => { LgHit._p = null; LgHit.rates = null; LgHit.load(); }, 900000);

const HOME_TOOL_FALLBACK = [
  ['W','精選W','精選W頁'], ['V1','精V1','舊版V1/V2頁'],
  ['V1Z','精V1Z','舊版V1/V2頁'], ['V2','精V2','舊版V1/V2頁'],
  ['V2Z','精V2Z','舊版V1/V2頁'], ['F7','精選7','精選7頁'],
  ['F8','精選8','精選8頁'], ['F12','精選12','精選12頁'],
  ['F8S','格8s','精選8s頁'], ['S8','精選8s','精選8s頁'],
  ['X8','8X','8X頁'], ['W88','88W','88W頁'], ['JX','新精選','新精選頁'],
  ['HB','好波','好波頁'], ['K3','K3（舊紀錄）','88W頁']
].map(x => ({key:x[0], name:x[1], page:x[2]}));
function _homeToolDefs(){ return homeData.tools && homeData.tools.length ? homeData.tools : HOME_TOOL_FALLBACK; }
function _homeRate(r){
  return r && r.n ? `${Number(r.hit).toFixed(1)}%（n=${r.n}）` : '—（n=0）';
}
function _homeRateTitle(r){
  return r ? `贏${r.w}／輸${r.l}／走${r.p}｜命中率=(贏+0.5×走)÷總場` : '';
}
function _homeToolCell(def, picks){
  const pass = picks.length > 0;
  const title = pass ? picks.map(p => `${p.direction}｜${p.filter || '未記錄篩選條件'}｜${p.page}`).join('\n') : '本場未入選';
  const result = pass ? picks.map(p => {
    const res = {W:'｜命中', L:'｜未中', P:'｜走盤'}[p.result] || '';
    const filter = picks.length > 1 && p.filter ? `［${p.filter}］` : '';
    return `<div class="ht-result" title="${esc(p.filter || '')}">${esc(p.direction)}${esc(filter + res)}</div>`;
  }).join('') : '<div class="ht-muted">未入選</div>';
  const rates = (field, label) => pass ? picks.map(p =>
    `<div class="ht-rate" title="${esc(_homeRateTitle(p[field]))}">${label}：${_homeRate(p[field])}</div>`
  ).join('') : `<div class="ht-rate ht-muted">${label}：—</div>`;
  return `<div class="home-tool${pass ? ' is-pass' : ''}" title="${esc(title)}">` +
    `<div class="ht-name">${esc(def.name)}</div><div class="ht-label">篩選結果</div>${result}` +
    rates('history', '相同歷史') + rates('recent14', '近14日') + `</div>`;
}
function _mrow(m, playedSec){
  const pc = window.PhoneCrawl ? PhoneCrawl.lineFor(m.id) : null;
  const pcMark = pc ? (pc.agree === true ? ' <span style="color:var(--up);font-size:10px">✓同本機</span>'
                      : (pc.agree === false ? ' <span style="color:var(--down);font-size:10px" title="手機直爬同本機數據唔一致——分析/推算以本機（雲端）為準">⚠唔同·本機為準</span>' : '')) : '';
  const line = pc
    ? `<div class="ln"><b>${esc(fmtLine(pc.line))}</b><span class="tag" style="font-size:10px;color:var(--gold2)">📱直爬</span>${pcMark}<br>主${pc.ho.toFixed(2)}/客${pc.ao.toFixed(2)}</div>`
    : (m.line ? `<div class="ln"><b>${esc(m.line.line)}</b>${m.line.src === 'crown' ? '<span class="tag dim" style="font-size:10px">Crown</span>' : ''}<br>主${m.line.ho != null ? m.line.ho.toFixed(2) : '—'}/客${m.line.ao != null ? m.line.ao.toFixed(2) : '—'}</div>` : '<div class="ln">無盤</div>');
  const od = playedSec ? '' : ((m.has_odds || (m.line && m.line.src === 'crown'))
    ? '<span class="od has">已有賠率</span>' : '<span class="od">未獲取賠率</span>');
  const oud = (playedSec && m.ou && m.ou.line != null)
    ? `<div class="ln" title="大小球尾盤（易勝博）"><b>大${esc(String(m.ou.line))}</b><br>` +
      `${m.ou.over != null ? m.ou.over.toFixed(2) : '—'}/${m.ou.under != null ? m.ou.under.toFixed(2) : '—'}</div>` : '';
  const rfb = (playedSec && m.score) ? '' : `<button class="rfb" data-fb="${m.id}" data-ko="${esc(m.kickoff || '')}" title="即時更新呢場最新賠率同盤口">⟳</button>`;
  const sc = m.score ? `<span class="sc">${esc(m.score)}</span>${_htftTag(m)}` : (m.ht ? _htftTag(m) : '');
  const home = `${m.home || '—'}${m.rank_home ? ` (${m.rank_home})` : ''}`;
  const away = `${m.away || '—'}${m.rank_away ? ` (${m.rank_away})` : ''}`;
  const updated = pc ? pc.at : m.odds_at;
  const grouped = {};
  for (const p of (m.tool_picks || [])) (grouped[p.key] || (grouped[p.key] = [])).push(p);
  const tools = _homeToolDefs().map(def => _homeToolCell(def, grouped[def.key] || [])).join('');
  return `<div class="mrow" data-mid="${m.id}">
    <div class="m-main">
      <div class="m-fix"><span><b>日期</b> ${esc((m.kickoff || '').slice(0, 10) || '—')}</span>` +
      `<span><b>時間</b> ${esc((m.kickoff || '').slice(11, 16) || '—')}</span>` +
      `<span><b>賽事類別</b> ${esc([m.category || m.cat, m.league].filter(Boolean).join('｜') || '—')}</span></div>
      <div class="tm"><span><b>主場</b> ${esc(home)}</span><span class="r">vs</span><span><b>客場</b> ${esc(away)}</span>${_stateTag(m.state)}${sc}</div>
      <div class="m-update"><b>最後更新時間</b> ${esc(updated || '未有記錄')}${pc ? '（手機直爬）' : ''}</div>
      <div class="m-actions">${line}${oud}${od}${rfb}${BetSlip.btn(m)}${Twin.btn(m)}</div>
    </div>
    <div class="home-tools">${tools}</div>
  </div>`;
}
let lgPred = {};
function renderHome(){
  let h = `<div class="day-h">🏟 即將開賽（${homeData.upcoming.length} 場）</div>`;
  if (!homeData.upcoming.length) h += '<div class="note">呢個時段冇即將開賽嘅賽事。</div>';
  let lastDay = null;
  for (const m of homeData.upcoming) {
    const d = (m.kickoff || '').slice(0, 10);
    if (d !== lastDay) { h += `<div class="day-h" style="background:#141a24;color:var(--dim);border-left-color:var(--accent)">📅 ${esc(d)}</div>`; lastDay = d; }
    h += _mrow(m, false);
  }
  /* 2026-10-11：已完賽回查篩選——Period／聯賽杯賽類／指定聯賽，獨立可疊加 */
  const pdTitle = playedHours >= 720 ? '近30日' : playedHours >= 168 ? '近7日'
    : `最近${playedHours}小時`;
  const lgs = [...new Set(homeData.played.map(m => m.league))].sort();
  if (playedLg !== 'all' && !lgs.includes(playedLg)) playedLg = 'all';
  const prow = playedLg === 'all' ? homeData.played
    : homeData.played.filter(m => m.league === playedLg);
  h += `<div class="day-h">📼 已開賽（${pdTitle}${playedCat !== 'all' ? '・' + esc(playedCat) : ''}${playedLg !== 'all' ? '・' + esc(playedLg) : ''}・${prow.length} 場・按日期分類，撳入去照睇 45 項分析）</div>`;
  h += `<div class="statbar">已完賽回查：Period
    <select onchange="playedSetHours(this.value)">
      <option value="24"${playedHours===24?' selected':''}>最近24小時</option>
      <option value="48"${playedHours===48?' selected':''}>最近48小時</option>
      <option value="120"${playedHours===120?' selected':''}>最近120小時</option>
      <option value="168"${playedHours===168?' selected':''}>近7日</option>
      <option value="720"${playedHours===720?' selected':''}>近30日</option>
    </select>
    ｜類型
    <select onchange="playedSetCat(this.value)">
      <option value="all"${playedCat==='all'?' selected':''}>全部</option>
      <option value="聯賽"${playedCat==='聯賽'?' selected':''}>聯賽</option>
      <option value="杯賽"${playedCat==='杯賽'?' selected':''}>杯賽</option>
    </select>
    ｜聯賽
    <select onchange="playedSetLg(this.value)">
      <option value="all"${playedLg==='all'?' selected':''}>全部</option>
      ${lgs.map(g => `<option value="${esc(g)}"${playedLg===g?' selected':''}>${esc(g)}</option>`).join('')}
    </select>
    <span class="hint">（三個篩選可單用可疊用）</span></div>`;
  lastDay = null;
  for (const m of prow) {
    const d = (m.kickoff || '').slice(0, 10);
    if (d !== lastDay) { h += `<div class="day-h" style="background:#141a24;color:var(--dim);border-left-color:var(--accent)">📅 ${esc(d)}</div>`; lastDay = d; }
    h += _mrow(m, true);
  }
  $('#homeList').innerHTML = h;
  _fillLgSlots();
  // 2026-10-13：撳「🎯投注」/「👯搵孖生」（document 委託）時唔好連帶開詳情頁
  $$('#homeList .mrow').forEach(r => r.onclick = ev => {
    if (ev.target.closest('[data-bet],[data-twin],[data-fb],.rfb')) return;
    openDetail(+r.dataset.mid);
  });
  // 每場「⟳」掣：手機直爬模式由手機經原生接口直接抓；否則經伺服器
  $$('#homeList .rfb').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    if (b.disabled) return;
    b.disabled = true;
    b.textContent = '…';
    try {
      if (window.PhoneCrawl && PhoneCrawl.modeOn) {
        const m = homeData.upcoming.find(x => x.id === +b.dataset.fb)
               || homeData.played.find(x => x.id === +b.dataset.fb);
        if (!m) { toast('搵唔到場次資料'); b.disabled = false; b.textContent = '⟳'; return; }
        const r = await PhoneCrawl.crawlOne(m.id, m.kickoff, m.line || null);
        toast(r.snaps
          ? (r.agree === false ? '📱 手機已更新：⚠ 同本機唔同（分析以本機為準）'
                               : `📱 手機已直爬更新（${r.at.slice(11, 16)}）`)
          : (r.noOdds ? '該場仲未開盤' : '手機直爬失敗：' + (r.err || '')));
      } else {
        const r = await jpost('/api/fetch', {id: +b.dataset.fb, force: true});
        toast(r.ok ? '已更新最新賠率同盤口' : ('更新失敗：' + (r.error || '未知')));
      }
    } catch (e) { toast('更新失敗：' + e.message); }
    loadHome();
  });
}
$('#hoursSel').onchange = loadHome;
$('#btnReload').onclick = loadHome;
$('#btnFetchAll').onclick = async function(){
  this.disabled = true;
  try {
    const list = homeData.upcoming.filter(m => !m.has_odds);
    if (!list.length) { toast('全部場次都已有賠率'); this.disabled = false; return; }
    let ok = 0, fail = 0, cached = 0;
    $('#updInfo').textContent = `獲取賠率中 0/${list.length}…`;
    LoadX.show('補爬缺賠率場次', list.length);
    for (const m of list) {
      try {
        const r = await jpost('/api/fetch', {id: m.id});
        if (r.cached) cached++; else r.ok ? ok++ : fail++;
      } catch (e) { fail++; }
      $('#updInfo').textContent = `獲取賠率中 ${ok + fail + cached}/${list.length}…`;
      LoadX.prog(ok + fail + cached, list.length);
    }
    LoadX.hide();
    $('#updInfo').textContent = `獲取完成：成功 ${ok}｜失敗 ${fail}${cached ? `｜${cached} 場兩分鐘內已獲取過（限流，唔使重複）` : ''}`;
  } finally {
    this.disabled = false;
    loadHome();
  }
};
$('#btnUpdate').onclick = async function(){
  this.disabled = true;
  try {
    const r = await jpost('/api/update', {});
    if (r.error) toast(r.error);
    pollUpdateLoop();
  } catch (e) {
    toast('更新失敗：' + e.message);
    this.disabled = false;
  }
};
$('#btnWin24').onclick = () => winUpdate('24h');
$('#btnWin30').onclick = () => winUpdate('30m');
$('#btnWin10').onclick = () => winUpdate('10m');
async function winUpdate(win){
  try {
    const r = await jpost('/api/update-window', {window: win});
    if (r.error) toast(r.error);
    pollUpdateLoop();
  } catch (e) { toast('窗口更新失敗：' + e.message); }
}
/* ⚡ 一鍵即時更新：畫面上列出的場次全部強制重抓最新賠率同盤口。
   手機直爬模式（雲端伺服器＋Android APP）：由手機經原生接口逐場直爬 titan007；
   伺服器模式（本機）：伺服器端批量（代理池） */
let fbPoll = null;
function serverFetchBatch(btn, ids){
  /* 伺服器通道（代理池／chrome 指紋）：雲端 0.5C 都實測 52/52 成功
   * （2026-10-12）——手機直爬俾 titan007 封 IP 時嘅正式後備。
   * ids（2026-10-24）：手機直爬核對完，淨係重抓『同本機唔一致/本機缺失』
   * 嘅場——咁手機撳掣都真係更新到落庫＋重算，唔再係齋核對空殼。 */
  btn.disabled = true;
  const body = {hours: +($('#hoursSel').value || 0)};
  if (ids && ids.length) body.ids = ids;
  jpost('/api/fetch-batch', body)
    .then(function(r){
      if (r.error && !r.running) { toast(r.error); btn.disabled = false; return; }
      LoadX.show('更新賠率＋重算資格');
      pollFbatch();
    })
    .catch(function(e){ toast('即時更新失敗：' + e.message); btn.disabled = false; });
}
$('#btnOddsNow').onclick = async function(){
  if (this.disabled) return;
  if (window.PhoneCrawl && PhoneCrawl.modeOn) { phoneBatchAll(this); return; }
  serverFetchBatch(this);
};
/* 🔄 重抓重算（2026-10-21）：重抓全部缺口盤口 → 重算八個精選系統資格
 * 2026-10-22 修復「撳完無反應」＋一撳即跑：
 * ① 唔用 confirm()——部分手機內嵌瀏覽器/WebView 會靜默封殺 confirm；
 *    亦唔要兩撳確認——用戶指示：手機一撳就 update（加入合資格／踢走唔合格）。
 * ② 後端錯誤唔再淨係一閃即逝嘅 toast，同時寫落 #updInfo 長駐顯示。
 * ③ 雲端真係跑到：雲端模式＝重算八個精選資格（歷史缺口重抓留俾本機）。 */
$('#btnRegrab').onclick = async function(){
  if (this.disabled) return;
  this.disabled = true;
  $('#updInfo').textContent = '🔄 重抓重算啟動中…';
  const r = await fetch('/api/regrab-recompute', {method: 'POST'})
    .then(x => x.json()).catch(() => null);
  if (!r || !r.ok) {
    const msg = (r && r.error) || '連線失敗';
    toast(msg);
    $('#updInfo').textContent = '🔄 重抓重算：' + msg;
    this.disabled = false;
    return;
  }
  if (r.mode === 'cloud')
    $('#updInfo').textContent = '🔄 雲端重算緊：合資格加入／唔合資格踢走（新精選＋88W 全年回查都做埋）…';
  LoadX.show('重抓缺口＋重算資格');
  pollRegrab();
};
async function pollRegrab(){
  const st = await fetch('/api/regrab-recompute').then(x => x.json()).catch(() => null);
  if (!st) { setTimeout(pollRegrab, 8000); return; }
  if (st.error) {
    $('#updInfo').textContent = '🔄 重抓重算出錯：' + st.error;
    $('#btnRegrab').disabled = false;
    return;
  }
  LoadX.hide();
  if (!st.running) {
    if (!st.last) {
      /* 從未跑過（或伺服器剛重啟、進行中狀態已流失）——唔好假報「完成」 */
      $('#updInfo').textContent = '';
      $('#btnRegrab').disabled = false;
      return;
    }
    $('#updInfo').textContent = '🔄 重抓重算完成（' + st.last + '）——八個精選系統資格已重算';
    $('#btnRegrab').disabled = false;
    toast('🔄 重抓重算完成');
    loadHome();
    return;
  }
  $('#updInfo').textContent = '🔄 重抓重算進行中：' + (st.phase || '') +
    (st.requalify && st.requalify.phase ? '（' + st.requalify.phase + '）' : '');
  setTimeout(pollRegrab, 8000);
}
async function phoneBatchAll(btn){
  btn.disabled = true;
  /* 即將開賽全部 ＋ 進行中（已開賽未有賽果）——後者尾盤即最新參考盤 */
  const list = homeData.upcoming.concat(homeData.played.filter(m => !m.score));
  if (!list.length) { toast('冇可更新場次'); btn.disabled = false; return; }
  /* 開爬前網絡自測：WiFi IP 俾 titan007 封嗰陣，舊版會傻更更逐場白等 20 秒
   * 逾時爬成十幾分鐘先發現全滅（2026-10-11/12 連續兩日 ✗N 嘅元兇）。
   * 自測唔過即刻講清楚原因，唔爬。 */
  if (PhoneCrawl.netTest) {
    $('#updInfo').textContent = `📱 直爬自測緊（${PhoneCrawl.VERSION || '?'}）…`;
    const nt = await PhoneCrawl.netTest();
    if (!nt.ok) {
      /* 2026-10-12 修正（用戶：「完全 UPDATE 唔到盤口同賠率」）：自測失敗
       * 唔再齋報錯就走——自動轉用伺服器通道（代理池）照樣更新到落庫，
       * 唔使用戶轉網絡／重裝 APK。手機直爬只係顯示加速通道，唔係唯一通道。 */
      $('#updInfo').textContent =
        `📱 直爬通道唔通（${nt.err || '無回應'}${nt.ms ? '・' + nt.ms + 'ms' : ''}，` +
        `titan007 封咗你而家嘅網絡 IP）——自動改用伺服器通道更新…`;
      toast('手機直爬唔通，轉用伺服器通道');
      btn.disabled = false;
      serverFetchBatch(btn);
      return;
    }
  }
  $('#updInfo').textContent =
    `📱 裝置 ${PhoneCrawl.device}（${PhoneCrawl.VERSION || '?'}）開始直爬 ${list.length} 場（手機網絡 → titan007）…`;
  try {
    LoadX.show('📱 手機直爬', list.length);
    const r = await PhoneCrawl.crawlAll(list, st => {
      $('#updInfo').textContent =
        `📱 手機直爬緊 ${st.done}/${list.length} 場（✓${st.ok} ✗${st.fail} 未開盤${st.noOdds}）…` +
        (window.PhoneCrawl && PhoneCrawl._stop ? '（停止緊，下一場退出）' : '');
      LoadX.prog(st.done, list.length);
    }, m => m.line || null);
    LoadX.hide();
    /* 2026-10-24 用戶規格「更新賠率＋重算資格 一鍵＝更新所有盤口及賠率
     * 及所有工具重算」：舊制手機直爬淨係核對顯示（本機數據為準，唔寫庫），
     * 撳完掣雲端賠率同工具資格零變動＝空殼。而家：直爬完→唔一致/本機缺失/
     * 手機網絡錯嘅場，交俾伺服器代理池重抓落庫（手機數據依然唔直接寫
     * 庫，遵守 10-02 定調）→ 自動行 12 工具資格重檢＋結算。全部一致先至
     * 真係「唔使更新」。未開盤嘅場跳過（titan007 都係空頁，白燒配額）。 */
    if (!r.stopped && r.ok > 0) {
      const stale = list.filter(m => {
        const rr = PhoneCrawl.res[m.id];
        return rr && !rr.noOdds && rr.agree !== true;
      });
      if (stale.length) {
        const agreeN = list.filter(m => {
          const rr = PhoneCrawl.res[m.id];
          return rr && rr.agree === true;
        }).length;
        $('#updInfo').textContent =
          `📱 直爬完成：${agreeN} 場一致｜${stale.length} 場唔一致/本機缺失/` +
          `手機爬唔到——伺服器緊重抓+重算…`;
        serverFetchBatch(btn, stale.map(m => m.id));
        return;
      }
      $('#updInfo').textContent =
        `📱 直爬完成：全部 ${r.ok} 場同伺服器一致——賠率已係最新，` +
        `12 工具資格不變（裝置 ${PhoneCrawl.device}）`;
      loadHome();
      refreshCurrentTool();
      return;
    }
    let msg = r.stopped
      ? `📱 手機直爬已按要求停止（做到 ${r.done} 場：✓${r.ok} ✗${r.fail}）`
      : `📱 手機直爬完成：更新 ${r.ok}｜失敗 ${r.fail}｜未開盤 ${r.noOdds}——` +
        `已即時顯示＋同步雲端（裝置 ${PhoneCrawl.device}）`;
    if (r.fail > 0) {
      msg += `｜錯誤範例：${(r.errs || []).join('；') || '無回應'}` +
        '。全部場次都失敗＝手機而家嘅網絡連唔到 titan007（多數係 WiFi IP 俾封）——轉用流動數據再撳一次；盤口會繼續用伺服器數據顯示。';
    }
    $('#updInfo').textContent = msg;
    loadHome();
    refreshCurrentTool();
    /* 自測過咗但爬落嚟全部失敗（網絡中途轉死／被封）——都係自動轉伺服器通道，
     * 唔使用戶白撳（2026-10-12） */
    if (!r.stopped && r.ok === 0 && r.fail > 0) {
      toast('手機直爬全部失敗，自動改用伺服器通道');
      serverFetchBatch(btn);
    }
  } finally {
    btn.disabled = false;
  }
}
async function pollFbatch(){
  clearTimeout(fbPoll);
  let st;
  try { st = await jget('/api/fetch-batch-status'); }
  catch (e) { fbPoll = setTimeout(pollFbatch, 5000); return; }
  if (st.running) {
    $('#updInfo').textContent =
      `⚡ ${st.phase || '即時更新中'}（成功 ${st.ok}・失敗 ${st.fail}）…`;
    if (st.total) LoadX.prog(st.done, st.total);
    fbPoll = setTimeout(pollFbatch, 3000);
    return;
  }
  LoadX.hide();
  $('#btnOddsNow').disabled = false;
  LoadX.hide();
  if (st.error) { toast(st.error); return; }
  if (st.last) {
    let msg = `⚡ 即時更新完成（${st.last}）：成功 ${st.ok}｜失敗 ${st.fail}` +
          `｜大小 ${st.ou_ok || 0}｜歐盤 ${st.x2_ok || 0}`;
    /* 2026-10-22：顯示八系統資格重檢結果——更新後唔合資格嘅場次已即時剔走 */
    const rv = st.reval;
    if (rv && (rv.checked || rv.removed || rv.added)) {
      msg += `｜資格重檢 ${rv.checked} 場：保留 ${rv.kept}・剔走唔合資格 ${rv.removed}・新加入 ${rv.added}` +
             (rv.capped ? `（另有 ${rv.capped} 場下輪補）` : '');
    }
    msg += '，各精選工具已一齊更新';
    $('#updInfo').textContent = msg;
    loadHome();
    refreshCurrentTool();
  } else {
    /* 從未跑完過（或伺服器剛重啟、進行中狀態已流失）——唔好靜默當冇事 */
    $('#updInfo').textContent = '⚡ 更新進度已流失（伺服器可能重啟過）——請再撳一次';
  }
}
/* 2026-10-22：「即時更新全部」／重抓完成後，令當前工具頁有動作——
   同頁 tab 重跑 loader；iframe 工具（好波/8s/88W/新精選）reload 內頁。 */
function refreshCurrentTool(){
  try {
    const loaders = {featw: () => loadFeaturedW(),
      check: () => loadCheckPage(),
      review: () => loadReview(), htft: () => loadHtft(),
      picks: () => loadPicksPage(), featlog: () => loadFeatlog(),
      oupred: () => loadOupred(), feathourly: () => loadFeatHourly(),
      ou: () => loadOuRules(), htftm: () => loadHtftRules(),
      pstat: () => loadPredStats(), x8: () => loadX8(),
      fw12lib: () => loadFw12Lib(), v1: () => loadV1(),
      removed: () => loadRemoved()};
    if (loaders[curPage]) return loaders[curPage]();
    const frames = {haobao: '#hbFrame', fw8s: '#fw8sFrame',
                    w88: '#w88Frame', newjx: '#jxFrame'};
    // 2026-10-26：分聯賽表改喺 iframe 外面（父文件）mount——iframe 內
    // league7.js 喺用戶瀏覽器入面執行異常（零錯誤零盒），父文件掛載
    // 同其餘 9 個工具頁同款機制，穩定可靠。
    const lg7map = {haobao: ['hb', '#hbLg7Mount'], fw8s: ['8s', '#fw8sLg7Mount'],
                    w88: ['88w', '#w88Lg7Mount'], newjx: ['jx', '#newjxLg7Mount']};
    if (lg7map[curPage]) League7.mount(lg7map[curPage][0], $(lg7map[curPage][1]));
    const sel = frames[curPage];
    const f = sel && $(sel);
    if (f && f.src) {
      try { f.contentWindow.location.reload(); return; } catch (e) {}
      f.src = f.src;
    }
  } catch (e) {}
}
let updPoll = null;
async function pollUpdateLoop(){
  clearTimeout(updPoll);
  const busy = await refreshUpdInfo();
  if (busy) { updPoll = setTimeout(pollUpdateLoop, 4000); return; }
  loadHome();
  if (curPage === 'featw') loadFeaturedW();
  if (curPage === 'review') loadReview();
  if (curPage === 'v1') loadV1();
  if (/^fw\d+$/.test(curPage)) loadFwGrid(curPage.slice(2));
}

/* 2026-10-25 即時推送（SSE）：伺服器一重算完（有新增/剔走）即刻推過嚟，
 * 開到嘅頁面 1-3 秒內自動刷新——唔使等 60 秒輪詢。3 秒合流防批量連環刷爆。
 * EventSource 內建斷線重連；Render proxy 靠 X-Accel-Buffering 直通。 */
let _es = null, _esLastFire = 0;
function startEvents(){
  if (_es || typeof EventSource === 'undefined') return;
  try { _es = new EventSource('/api/events'); } catch (e) { return; }
  _es.onmessage = function(ev){
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (!d || d.type !== 'reval') return;
    const now = Date.now();
    if (now - _esLastFire < 3000) return;
    _esLastFire = now;
    refreshWorkLog();
    if (document.hidden) return;
    if (curPage === 'home' && !_updBusy) loadHome();
    else if (curPage === 'review') loadReview();
    else if (curPage === 'featlog') loadFeatlog();
    else if (curPage === 'detail' && curMatch && d.mid === curMatch.id) {
      loadMatchTools(d.mid);
      openDetailRefreshLight(d.mid);
    } else refreshCurrentTool();
    toast('🔄 即時更新：' + (d.acts || []).slice(0, 3).join('、'));
  };
}
/* 詳情頁輕量刷新：淨重撈會變嘅區塊（工具卡＋徽章來源）唔成張重繪 */
function openDetailRefreshLight(mid){
  try {
    jget('/api/v3/target?id=' + mid).then(r => {
      if (!r || r.error || !r.target) return;
      curMatch = r.target;
    }).catch(() => {});
  } catch (e) {}
}
startEvents();

/* 2026-10-25 統一載入狀態列（用戶規格：所有載入中都要話你知載入緊咩、
 * 進度%、尚餘時間）。兩種模式：有總數→「項目 done/total（%）｜尚餘 Xs」；
 * 冇總數（單一請求）→「項目…（已用 Ns）」——冇速率就唔顯示時間，唔呃人。 */
const LoadX = (() => {
  let bar = null, timer = null, t0 = 0, label = '', total = 0, done = 0;
  let samples = [];
  function ensure() {
    if (bar) return bar;
    bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;left:50%;bottom:56px;transform:translateX(-50%);' +
      'background:#0e1626ee;border:1px solid #3d6fb4;color:#dce7f5;border-radius:10px;' +
      'padding:6px 14px;font-size:13px;z-index:9990;display:none;box-shadow:0 2px 10px rgba(0,0,0,.5);' +
      'max-width:94vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    document.body.appendChild(bar);
    return bar;
  }
  function eta() {
    const now = Date.now();
    samples = samples.filter(x => now - x[0] < 20000);
    if (samples.length < 2) return null;
    const r = (samples[samples.length - 1][1] - samples[0][1]) /
              ((samples[samples.length - 1][0] - samples[0][0]) / 1000);
    if (r <= 0) return null;
    const rem = (total - done) / r;
    if (!isFinite(rem) || rem < 0) return null;
    return rem < 90 ? Math.round(rem) + 's' : '~' + Math.round(rem / 60) + ' 分鐘';
  }
  function render() {
    const el = ensure();
    const sec = ((Date.now() - t0) / 1000).toFixed(0);
    let txt = '⏳ ' + label;
    if (total > 0) {
      const pct = Math.min(100, Math.round(done / total * 100));
      txt += ' ' + done + '/' + total + ' (' + pct + '%)';
      const e = eta();
      txt += e ? '｜尚餘 ' + e : '｜已用 ' + sec + 's';
    } else {
      txt += '…（已用 ' + sec + 's）';
    }
    el.textContent = txt;
  }
  return {
    show(lbl, tot) {
      label = lbl; total = tot || 0; done = 0; t0 = Date.now();
      samples = [[t0, 0]];
      ensure().style.display = 'block';
      clearInterval(timer); timer = setInterval(render, 500);
      render();
    },
    prog(d, t) { done = d; if (t != null) total = t; samples.push([Date.now(), d]); render(); },
    hide() { clearInterval(timer); timer = null; if (bar) bar.style.display = 'none'; }
  };
})();

/* ---------- 場次分析（詳情頁） ---------- */
let curMatch = null;
async function openDetail(mid){
  goto('detail');
  $('#detBody').innerHTML = '<div class="note">⏳ 載入緊：場次詳情（盤口七時點＋分析）…</div>';
  LoadX.show('載入場次詳情');
  $('#detMsg').textContent = '';
  let res;
  try { res = await jget('/api/v3/target?id=' + mid); }
  catch (e) { LoadX.hide(); $('#detBody').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (res.error) { LoadX.hide(); $('#detBody').innerHTML = '<div class="err">' + esc(res.error) + '</div>'; return; }
  curMatch = res.target;
  const t = res.target;
  setV3UD(t);
  const lb = (title, o) => `<div class="linebox"><div class="lb-t">${title}</div>` +
    (o ? `<div class="lb-v">${esc(o.line)}${o.src === 'crown' ? '<span class="tag dim" style="font-size:10px">Crown</span>' : ''}<br><small>主${o.ho != null ? o.ho.toFixed(2) : '—'}/客${o.ao != null ? o.ao.toFixed(2) : '—'}</small></div>` : '<div class="lb-v" style="color:var(--dim)">無數據</div>') + '</div>';
  const stateTxt = t.state === 'finished' ? '已完場' : t.state === 'live' ? '進行中' : '未開賽';
  let h = `<div class="tcard">
    <h2>${esc(rn(t.home, t.rank_home))} <span style="color:var(--dim)">vs</span> ${esc(rn(t.away, t.rank_away))}</h2>
    <div class="meta">${esc(t.league)}｜${esc(t.category || '')}　${esc(t.kickoff)}　<span class="tag ${t.state === 'live' ? 'live' : 'dim'}">${stateTxt}</span>${t.score ? `　<b class="sc" style="font-size:18px">${esc(t.score)}</b>${_htftTag(t)}` : ''}　<span class="od at" title="賠率/盤口最後更新：${esc(t.odds_at || '')}">🕒 賠率更新 ${esc(t.odds_at ? fmtAt(t.odds_at) : '—')}</span></div>
    <div class="lines">
      ${lb('尾盤（檢查基準）', t.close)}${lb('初盤', t.init)}${lb('開賽前4小時', t.h4)}
      ${lb('開賽前30分鐘', t.h30)}${lb('開賽前15分鐘', t.h15)}${lb('開賽前10分鐘', t.h10)}${lb('開賽前5分鐘', t.h5)}
    </div>
    <div id="detLgPred"></div>${BetSlip.btn(t, '🎯 我想投注')}</div>
    <div class="tcard">
      <h2 style="color:var(--gold2)">🧰 工具預測<span style="font-weight:400;font-size:12px;color:var(--dim)">（每工具＋逐篩選法嘅全庫／同聯賽命中率；ⓩ＝Z 槽同主隊組合）</span></h2>
      <div id="toolPredBox"><div class="note">工具預測載入中…</div></div>
    </div>
    <div class="tcard" id="v3sec">
      <h2 style="color:var(--gold2)">⚽ V3 場次分析（45 項）</h2>
      <div id="v3Sum" class="note">精選W 檢查載入中…</div>
      <div id="v3Items"><div class="note">項目清單載入中…</div></div>
    </div>
    <div class="tcard" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      ${BetSlip.btn(t, '🎯 我想投注')}
      <span class="hint">揀盤口＋本金＋賠率；「不連水位分享」會先新增至我的選擇</span>
    </div>`;
  $('#detBody').innerHTML = h;
  LoadX.hide();
  initV3Section(t.id);
  renderDetLgPred(t.id);
  loadMatchTools(t.id);
}
/* 2026-10-25 詳情頁「🧰 工具預測」（用戶規格，唔簡化到睇唔明）：
 * 每個有預測嘅工具列出：工具名（Z 槽標明 ⓩ）＋預測（方向/隊/盤/賠率/結果）
 * ＋工具全庫命中率＋同聯賽命中率＋逐篩選法命中率（全庫＋同聯賽；
 * 精選W＝每條條件、字頭工具＝每個字頭、格＝每格、8X＝每條因子、
 * 88W＝每個 market+conds 組合、好波＝每條規則）。數據 /api/match/tools。 */
async function loadMatchTools(mid){
  const box = $('#toolPredBox');
  if (!box) return;
  let d;
  try { d = await jget('/api/match/tools?id=' + mid); }
  catch (e) { box.innerHTML = '<div class="err">工具預測載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (d.error || !d.tools || !d.tools.length){
    box.innerHTML = '<div class="note">呢場暫時冇任何工具有預測。（撳「⚡ 更新賠率＋重算資格」即時重檢資格後再睇）</div>';
    return;
  }
  box.innerHTML = d.tools.map(_toolPredHtml).join('');
}
function _rateBox(r, lab){
  if (!r || !r.n) return `<div class="tp-rate"><span class="lab">${lab}</span><span class="big" style="color:var(--dim)">—</span><div class="sub">無樣本</div></div>`;
  const cls = r.n < 10 ? 'tp-small' : (r.hit >= 65 ? 'tp-hi' : (r.hit < 50 ? 'tp-lo' : ''));
  const warn = r.n < 10 ? ' ⚠️樣本少' : '';
  return `<div class="tp-rate ${cls}" title="${lab}：${r.w}贏/${r.l}輸/${r.p}走・共${r.n}場（命中率＝(贏+0.5×走)÷總場）">
    <span class="lab">${lab}${warn}</span>
    <span class="big">${r.hit}%</span>
    <span class="sub">${r.w}贏/${r.l}輸/${r.p}走・n=${r.n}</span></div>`;
}
function _methRateTxt(r){
  if (!r || !r.n) return '—（無樣本）';
  const pct = `<b style="color:${r.n < 10 ? 'var(--gold2)' : (r.hit >= 65 ? '#7ee2a0' : (r.hit < 50 ? '#ff9d9d' : 'inherit'))}">${r.hit}%</b>`;
  return `${pct}<small>(n=${r.n})</small>`;
}
function _toolPredHtml(t){
  const dirTxt = t.dir === 'up' ? '上盤' : t.dir === 'down' ? '下盤'
    : t.dir === '大' ? '大球' : t.dir === '細' ? '細球'
    : (t.key === 'W88' ? '組合' : (t.dir || '—'));
  const res = !t.result ? '<span style="color:var(--dim)">未結算</span>'
    : t.result === 'W' ? '<b class="r-up">✅ 命中</b>'
    : t.result === 'L' ? '<b class="r-down">❌ 未中</b>'
    : '<b>➖ 走盤</b>';
  const zChip = t.z ? '<span class="tp-z" title="Z 槽＝同主隊字頭組合（舊版 V1/V2 嘅獨立篩選槽，命中統計獨立計）">ⓩ Z 槽</span>' : '';
  const meth = (t.methods || []).map(m =>
    `<div class="tp-mrow"><span class="tp-mname" title="${esc(m.name)}">${esc(m.name)}</span>` +
    `<span class="tp-mrate">全庫 ${_methRateTxt(m.all)}｜同聯賽 ${_methRateTxt(m.league)}</span></div>`
  ).join('');
  return `<div class="tp-card">
    <div class="tp-head"><span class="tp-name">${esc(t.name)}</span>${zChip}
      <span>預測：<span class="tp-dir ${t.dir === 'up' || t.dir === '大' ? 'r-up' : 'r-down'}">${esc(dirTxt)}${t.team ? '（' + esc(t.team) + '）' : ''}</span></span>
      ${t.line ? `<span class="tp-line">${esc(t.line)}${t.odds ? ' ' + esc(t.odds) : ''}</span>` : ''}
      <span class="tp-res">${res}${t.added_at ? ` <small style="color:var(--dim)">${esc(String(t.added_at).slice(0, 16))}入選</small>` : ''}</span>
    </div>
    <div class="tp-rates">${_rateBox(t.all, '全庫命中率')}${_rateBox(t.league, '同聯賽命中率')}</div>
    ${meth ? `<div class="tp-meth"><div class="tp-mhead">篩選法（本場用咗嘅；每個法嘅全庫／同聯賽命中率）：</div>${meth}</div>` : ''}
  </div>`;
}
/* 🎯 聯賽預測明細：>78% 規則適用嘅場次，喺詳情頂部列出每條規則（lgPred＝主頁已載入嘅 /api/lgpred） */
function renderDetLgPred(mid){
  const box = $('#detLgPred');
  if (!box) return;
  const pr = lgPred[mid];
  if (!pr) { box.innerHTML = ''; return; }
  const cls = pr.direction === 'up' ? 'r-up' : 'r-down';
  box.innerHTML = `<div class="gaprow" style="margin-top:8px"><span class="lab">🎯 聯賽規則</span>` +
    `方向：<b class="${cls}">${pr.direction === 'up' ? '上盤' : '下盤'}（${esc(_dirTeam(pr.direction))}）</b>　` +
    `<span style="color:var(--dim);font-size:12px">呢場符合 ${esc(pr.league || '')} 嘅 >78% 命中率規則${(pr.rules || []).length > 1 ? `（${pr.rules.length} 條，逐條獨立睇）` : ''}：</span></div>` +
    (pr.rules || []).map(r =>
      `<div class="note" style="margin:2px 0">・${esc(r.desc)}：${r.direction === 'up' ? '上' : '下'} ${Math.round(100 * (r.rate || 0))}%（${r.n}場基數）</div>`).join('');
}
$('#btnBack').onclick = () => goto('home');
$('#btnDetFetch').onclick = async function(){
  if (!curMatch) return;
  this.disabled = true;
  $('#detMsg').textContent = '獲取賠率中…';
  const r = await jpost('/api/fetch', {id: curMatch.id});
  $('#detMsg').textContent = r.ok ? '已獲取，重新整理中…' : '獲取失敗：' + (r.error || '未知');
  this.disabled = false;
  if (r.ok) openDetail(curMatch.id);
};
$('#btnDetRefresh').onclick = async function(){
  if (!curMatch) return;
  this.disabled = true;
  $('#detMsg').textContent = '強制更新中…';
  const r = await jpost('/api/fetch', {id: curMatch.id, force: true});
  $('#detMsg').textContent = r.ok ? '已更新，重新計算中…' : '更新失敗：' + (r.error || '未知');
  this.disabled = false;
  if (r.ok) openDetail(curMatch.id);
};
$('#btnDetShare').onclick = () => {
  if (!curMatch) return;
  shareText(detailShareText(curMatch));
};
function detailShareText(t){
  const L = [];
  L.push(`【V3場次分析】${t.league} ${(t.kickoff || '').slice(5, 16)}`);
  L.push(`${t.home} vs ${t.away}${t.score ? '（' + t.score + '）' : ''}`);
  const c = t.close;
  L.push(`尾盤 ${c ? c.line : '—'}${c ? ' 主' + c.ho?.toFixed(2) + '/客' + c.ao?.toFixed(2) : ''}`);
  L.push(`本場：上盤＝${V3UD ? V3UD.up : '—'}｜下盤＝${V3UD ? V3UD.down : '—'}`);
  return L.join('\n');
}

/* ---------- 我的選擇 ---------- */
let picksMap = {};
async function refreshPicksMap(){
  try {
    const d = await jget('/api/picks');
    picksMap = {};
    for (const p of d.picks) picksMap[p.id] = p.choice;
    $('#pkCnt').textContent = d.stats.total ? `(${d.stats.total})` : '';
  } catch (e) {}
}
function _betLineOptions(selected){
  let h = '';
  for (let i = -20; i <= 20; i++) {
    const v = i / 4;
    h += `<option value="${v}"${Number(selected) === v ? ' selected' : ''}>${esc(fmtLine(v))}（${v}）</option>`;
  }
  return h;
}
function _betForm(p, confirmed){
  return `<div class="bet-form" data-betid="${p.bet_id}"${confirmed ? ' style="display:none"' : ''}>
    <div class="bet-grid">
      <label>主場隊伍<input data-f="home" value="${esc(p.home)}"></label>
      <label>客場隊伍<input data-f="away" value="${esc(p.away)}"></label>
      <label>方向<select data-f="choice"><option value="up"${p.choice === 'up' ? ' selected' : ''}>上盤</option><option value="down"${p.choice === 'down' ? ' selected' : ''}>下盤</option></select></label>
      <label>盤口<select data-f="line">${_betLineOptions(p.line)}</select></label>
      <label>賠率<input data-f="odds" type="number" min="0.01" step="0.01" value="${esc(p.odds)}"></label>
      <label>本金<input data-f="stake" type="number" min="0" step="0.01" value="${esc(p.stake)}"></label>
    </div>
    <div class="toolbar">${confirmed
      ? '<button class="btn gold bet-save">保存更新（即時重算盈利）</button>'
      : '<button class="btn gold bet-confirm">確認</button><button class="btn warn bet-delete">刪除</button>'}</div>
  </div>`;
}
function _betProfit(p){
  if (p.profit == null) return '<span class="hint">賽事未完，待計盈利</span>';
  const v = Number(p.profit), cls = v > 0 ? 'r-up' : (v < 0 ? 'r-down' : '');
  return `<b class="bet-profit ${cls}">盈利 ${v > 0 ? '+' : ''}${v.toFixed(2)}</b>`;
}
function _betCard(p, confirmed){
  const result = {W:'贏',L:'輸',P:'走盤',HW:'半贏',HL:'半輸',X:'無法判定'}[p.result] || '待賽';
  return `<div class="fcard bet-card">
    <div class="f-top"><span class="f-time">${esc((p.kickoff || '').slice(5, 16))}　${esc(p.league || '')}</span>
      <span class="f-teams">${esc(p.home)} vs ${esc(p.away)}</span>
      ${p.score ? `<span class="f-score">${esc(p.score)}</span>` : ''}
      <span class="tag ${p.choice}">${p.choice === 'up' ? '上盤' : '下盤'}＝${esc(p.choice_team)}</span>
      ${confirmed ? `<span class="tag dim">${result}</span>${_betProfit(p)}<button class="btn bet-edit">編輯</button>` : '<span class="tag live">待確認</span>'}
    </div>
    <div class="gaprow"><span class="lab">注單</span>${esc(p.line_text)}｜賠率 ${esc(p.odds)}｜本金 ${esc(p.stake)}${confirmed ? `｜確認於 ${esc(p.confirmed_at || '')}` : ''}</div>
    ${_betForm(p, confirmed)}
  </div>`;
}
function _betPayload(form){
  return {bet_id: +form.dataset.betid,
    home: form.querySelector('[data-f="home"]').value.trim(),
    away: form.querySelector('[data-f="away"]').value.trim(),
    choice: form.querySelector('[data-f="choice"]').value,
    line: Number(form.querySelector('[data-f="line"]').value),
    odds: Number(form.querySelector('[data-f="odds"]').value),
    stake: Number(form.querySelector('[data-f="stake"]').value)};
}
async function _betSave(form){
  const r = await jpost('/api/bets/edit', _betPayload(form));
  if (!r.ok) { toast(r.error || '更新失敗'); return false; }
  return true;
}
async function loadPicksPage(){
  $('#pkList').innerHTML = '<div class="note">⏳ 載入緊：我的選擇…</div>';
  let d;
  try { d = await jget('/api/picks/full'); }
  catch (e) { $('#pkList').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats;
  $('#pkStats').innerHTML = `<div class="statbar"><span>待確認 <b>${s.pending}</b></span>
    <span>已確認 <b>${s.confirmed}</b></span><span>總盈利 <b>${s.profit > 0 ? '+' : ''}${Number(s.profit).toFixed(2)}</b></span>
    <span>贏 ${s.wins}｜半贏 ${s.half_wins}｜輸 ${s.losses}｜半輸 ${s.half_losses}｜走 ${s.pushes}</span></div>`;
  let h = '';
  if (d.weeks.length) {
    h += '<div class="day-h">每星期總數（星期一12:01至下星期一12:00）</div>' + d.weeks.map(w =>
      `<div class="statbar"><span>星期 ${esc(w.week)}</span><span>${w.n} 場</span>` +
      `<span>總盈利 <b>${w.profit > 0 ? '+' : ''}${Number(w.profit).toFixed(2)}</b></span>` +
      `${w.pending_settlement ? `<span>${w.pending_settlement} 場待結算</span>` : ''}` +
      `<button class="btn warn" data-delweek="${esc(w.week)}">刪全週</button></div>`).join('');
  }
  h += `<div class="day-h">待確認（${d.pending.length}）</div>`;
  h += d.pending.length ? d.pending.map(p => _betCard(p, false)).join('')
    : '<div class="note">沒有待確認注單。主頁「投注」→「不連水位分享」會新增到這裡。</div>';
  h += `<div class="day-h">已確認賽事（${d.confirmed.length}）</div>`;
  if (!d.confirmed.length) h += '<div class="note">尚未有已確認賽事。</div>';
  for (const day of d.days) {
    const rows = d.confirmed.filter(p => p.day === day.day);
    h += `<div class="statbar"><span>${esc(day.day)}（12:01至翌日12:00）</span><span>${day.n} 場</span>` +
      `<span>每日總盈利 <b>${day.profit > 0 ? '+' : ''}${Number(day.profit).toFixed(2)}</b></span>` +
      `${day.pending_settlement ? `<span>${day.pending_settlement} 場待結算</span>` : ''}` +
      `<button class="btn warn" data-delday="${esc(day.day)}">刪全日</button></div>`;
    h += rows.map(p => _betCard(p, true)).join('');
  }
  $('#pkList').innerHTML = h;
  $$('#pkList .bet-confirm').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try {
      const form = b.closest('.bet-form');
      if (await _betSave(form)) {
        const r = await jpost('/api/bets/confirm', {bet_id: +form.dataset.betid});
        if (r.ok) { toast('已確認，移至下方已確認賽事'); loadPicksPage(); refreshPicksMap(); }
        else toast(r.error || '確認失敗');
      }
    } catch (e) { toast('確認失敗：' + e.message); } finally { b.disabled = false; }
  });
  $$('#pkList .bet-delete').forEach(b => b.onclick = async () => {
    if (!window.confirm('確定刪除這場待確認注單？此操作不可復原。')) return;
    b.disabled = true;
    try {
      const form = b.closest('.bet-form');
      const r = await jpost('/api/bets/delete', {bet_id: +form.dataset.betid});
      if (r.ok) {
        await Promise.all([loadPicksPage(), refreshPicksMap()]);
        toast(r.deleted === 1 ? '已刪除該場待確認注單'
                              : '該場已不在待確認列，已更新列表');
      } else {
        toast(r.error || '刪除失敗，請稍後重試');
      }
    } catch (e) { toast('刪除失敗：' + e.message); } finally { b.disabled = false; }
  });
  $$('#pkList .bet-edit').forEach(b => b.onclick = () => {
    const form = b.closest('.bet-card').querySelector('.bet-form');
    const show = form.style.display === 'none';
    form.style.display = show ? '' : 'none';
    b.textContent = show ? '收起' : '編輯';
  });
  $$('#pkList .bet-save').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { if (await _betSave(b.closest('.bet-form'))) {
      toast('已更新；如已有賽果，盈利已即時重算'); loadPicksPage(); refreshPicksMap();
    } } catch (e) { toast('更新失敗：' + e.message); } finally { b.disabled = false; }
  });
  const del = async (url, key, label, btn) => {
    if (!window.confirm(`確定${label}？此操作不可復原。`)) return;
    btn.disabled = true;
    try {
      const r = await jpost(url, key);
      if (r.ok) { toast(`已刪除 ${r.deleted} 筆已確認紀錄`); loadPicksPage(); refreshPicksMap(); }
      else toast(r.error || '刪除失敗');
    } catch (e) { toast('刪除失敗：' + e.message); } finally { btn.disabled = false; }
  };
  $$('#pkList [data-delday]').forEach(b => b.onclick = () =>
    del('/api/bets/delete-day', {day: b.dataset.delday}, `刪除 ${b.dataset.delday} 全日已確認紀錄`, b));
  $$('#pkList [data-delweek]').forEach(b => b.onclick = () =>
    del('/api/bets/delete-week', {week: b.dataset.delweek}, `刪除 ${b.dataset.delweek} 全週已確認紀錄`, b));
}

/* ================= V3 場次分析渲染器 ================= */
const V3_TP = ['初盤', '開賽前4小時', '開賽前30分鐘', '開賽前15分鐘', '開賽前10分鐘', '開賽前5分鐘'];
const V3_TITLES = {};
for (let i = 0; i < 6; i++)
  V3_TITLES[String(i + 1)] = `相同${V3_TP[i]}及相同尾盤的盤口&水位（A–H 淨主淨客／I–P 連互換 ×8範圍＋12段水位）`;
for (let i = 0; i < 7; i++) {
  V3_TITLES[String(7 + i)] = `對上一次對賽尾盤 對 今場${V3_TP[i] || '尾盤'}（盤口100%一樣＋水位±3%・同主客/對調/綜合）`;
  V3_TITLES[String(14 + i)] = `對上一次對賽尾盤 對 今場${V3_TP[i] || '尾盤'}（12段水位 × 同主客/對調/綜合 × 同聯賽/聯賽類/全庫）`;
}
Object.assign(V3_TITLES, {
  '21': '主隊主場勝和負比例 及 客隊客場（每項±7%）→ 盤口分佈＋12段水位',
  '22': '主隊主場勝和負比例 及 客隊客場（每項±7%）→ 12段水位',
  '23': '主隊主客場總和勝和負比例 及 客隊總和（每項±7%）→ 盤口分佈＋12段水位',
  '24': '主隊主客場總和勝和負比例 及 客隊總和（每項±7%）→ 12段水位',
  '25': '主隊主場入球/失球/得失球差 及 客隊客場（各±3球）→ 盤口分佈＋12段水位',
  '26': '主隊主場入球/失球/得失球差 及 客隊客場（各±3球）→ 12段水位',
  '27': '主隊主場排名 及 客隊客場排名（各±2）→ 盤口分佈＋各盤口水位分佈',
  '28': '主隊主客場總排名 及 客隊總排名（各±2）→ 盤口分佈＋各盤口水位分佈',
  '29': '上賽完全相同（主客一樣＋讓/受讓＋讓球數）→ 今場主場盤口分佈＋各盤口水位分佈',
  '30': '29樣本：分佈最多盤口 及 上盤勝率最接近50%盤口 與今場尾盤淺深淨差距（全部水位區）',
  '31': '主隊主場排名 − 客隊客場排名 差距淨值（±1）→ 盤口分佈＋各盤口水位分佈',
  '32': '31樣本：分佈最多盤口 及 最接近50%盤口 與今場尾盤淺深淨差距',
  '33': '主隊主客場總排名 − 客隊總排名 差距淨值（±1）→ 盤口分佈＋各盤口水位分佈',
  '34': '33樣本：分佈最多盤口 及 最接近50%盤口 與今場尾盤淺深淨差距',
  '35': '主隊主場入球/失球/得失球差 及 客隊客場（各±3球）→ 盤口分佈＋各盤口水位分佈',
  '36': '35樣本：分佈最多盤口 及 最接近50%盤口 與今場尾盤淺深淨差距',
  '37': '主隊主客場總和入球/失球/得失球差 及 客隊總和（各±3球）→ 盤口分佈＋各盤口水位分佈',
  '38': '37樣本：分佈最多盤口 及 最接近50%盤口 與今場尾盤淺深淨差距',
});
for (let i = 0; i < 6; i++)
  V3_TITLES[String(39 + i)] = `首14分鐘入球%（相同${V3_TP[i]}及相同尾盤・A–P 16格）`;
Object.assign(V3_TITLES, {
  '45': '首14分鐘入球%（排名差距主／排名差距總／入失球主／入失球總 四小項）',
});
const V3_COMBO_OPTS = [];
for (let n = 1; n <= 38; n++) V3_COMBO_OPTS.push(String(n));

/* 全場通用：上/下盤對應邊隊（平手盤：上盤＝平手主隊） */
let V3UD = null;
function setV3UD(tg){
  if (!tg || !tg.home) { V3UD = null; return; }
  const g = tg.close ? tg.close.g : null;
  if (g === 'home')      V3UD = {up: tg.home, down: tg.away, push: false};
  else if (g === 'away') V3UD = {up: tg.away, down: tg.home, push: false};
  else                   V3UD = {up: tg.home, down: tg.away, push: true};
}
function _un(){ return V3UD ? V3UD.up : '上盤'; }
function _dn(){ return V3UD ? V3UD.down : '下盤'; }
/* 逐卡設定：清單頁一頁過渲染多場賽事時，每張卡渲染前用自己場次嘅資料覆寫
   全域 V3UD（2026-10-02 精選W 曾用殘留舊隊名——波蘭場顯示蘇里南/危地馬拉嘅根因） */
function _udSetByLine(line, home, away){
  const g = !line ? null
    : line.indexOf('主讓') === 0 ? 'home'
    : line.indexOf('客讓') === 0 ? 'away' : 'none';
  setV3UD({home, away, close: g ? {g} : null});
}
/* 方向對應嘅隊名：上盤→上盤隊、下盤→下盤隊（修正 2026-10-02：之前下盤都show上盤隊名） */
function _dirTeam(d){ return d === 'up' ? _un() : _dn(); }
function _udLegend(){
  return `<div class="note" style="margin:2px 0 6px">本場：上盤＝<b>${esc(_un())}</b>｜下盤＝<b>${esc(_dn())}</b>${V3UD && V3UD.push ? '（今場平手盤：上盤＝平手主隊）' : ''}</div>`;
}
/* 上/下盤兩個%一齊寫明；較大嗰邊綠色、較細嗰邊紅色（平手/冇資料唔上色） */
function _udrates(u, d) {
  if (u == null && d == null) return '—';
  const us = u != null ? pct(u) : '—', ds = d != null ? pct(d) : '—';
  let uc = '', dc = '';
  if (u != null && d != null) {
    if (u > d) { uc = 'r-up'; dc = 'r-down'; }
    else if (d > u) { uc = 'r-down'; dc = 'r-up'; }
  }
  return `<b class="${uc}">上(${esc(_un())}) ${us}</b>／<b class="${dc}">下(${esc(_dn())}) ${ds}</b>`;
}
function _winCls(u, d, side) {
  if (u == null || d == null || u === d) return '';
  return ((side === 'u') === (u > d)) ? 'r-up' : 'r-down';
}
function _ocTxt(o){
  return o ? `${_udrates(o.up_r, o.down_r)}　走 ${pct(o.push_r)}（${o.push}場）<span style="color:var(--dim)">｜分母 ${o.n} 場</span>` : '—';
}
const V3_ZONES12 = ['≤0.64','0.65-0.69','0.70-0.74','0.75-0.79','0.80-0.84','0.85-0.89','0.90-0.94','0.95-0.99','1.00-1.04','1.05-1.09','1.10-1.14','≥1.15'];
function v3Water12Tbl(w, curZ, zones12) {
  if (!w) return '';
  const ZL = zones12 || V3_ZONES12;
  let h = `<table class="ck-table"><thead><tr><th>上盤水位段<br><small style="color:var(--dim)">上盤＝${esc(_un())}</small></th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>`;
  (w.zones || []).forEach((z, i) => {
    if (!z || !z.n) return;
    const hit = (i === curZ) ? ' class="hl"' : '';
    const zn = z.zone || ZL[i] || String(i);
    h += `<tr${hit}><td>${esc(zn)}${i === curZ ? ' ◀今場' : ''}</td><td>${z.n}</td>` +
         `<td class="${_winCls(z.up_r, z.down_r, 'u')}">${pct(z.up_r)}</td><td class="${_winCls(z.up_r, z.down_r, 'd')}">${pct(z.down_r)}</td><td>${pct(z.push_r)}</td></tr>`;
  });
  if (curZ != null && curZ >= 0 && curZ < 12 && !(w.zones && w.zones[curZ] && w.zones[curZ].n)) {
    h += `<tr class="hl"><td>${esc(ZL[curZ])} ◀今場</td><td>0</td><td>—</td><td>—</td><td>—</td></tr>`;
  }
  h += `</tbody></table><div class="note" style="margin:2px 0 8px">水位表樣本 ${w.n} 場${curZ >= 0 ? '；<b>黃底＝今場尾盤上盤水位所在段</b>' : ''}</div>`;
  return h;
}
/* 每格 12 段水位（嵌喺 16 格表入面，用摺疊） */
function v3ZoneDetails(c, curZ, zones12){
  if (!c.zones) return '';
  const w = {n: c.n, zones: c.zones};
  return `<details style="margin:2px 0"><summary style="padding:2px 6px;font-size:11px;color:var(--dim)">▸ 12段水位</summary><div class="body" style="padding:2px 4px">${v3Water12Tbl(w, curZ, zones12)}</div></details>`;
}
function v3Cells16Tbl(cells, curZ, zones12) {
  if (!cells || !cells.length) return '';
  let h = `<table class="ck-table"><thead><tr><th>字頭</th><th>範圍</th><th>口徑</th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>`;
  for (const c of cells) {
    if (c.error) {
      h += `<tr><td><b>${c.letter}</b></td><td>${esc(c.scope)}</td><td>${c.swap ? '連互換' : '淨主淨客'}</td><td colspan="4" style="color:var(--dim)">不適用</td></tr>`;
      continue;
    }
    h += `<tr><td><b>${c.letter}</b></td><td>${esc(c.scope)}</td><td>${c.swap ? '連互換' : '淨主淨客'}</td><td>${c.n}</td>` +
         `<td class="${_winCls(c.up_r, c.down_r, 'u')}">${pct(c.up_r)}</td><td class="${_winCls(c.up_r, c.down_r, 'd')}">${pct(c.down_r)}</td><td>${pct(c.push_r)}</td></tr>`;
    if (c.zones && c.zones.some(z => z && z.n)) {
      h += `<tr><td colspan="7" style="padding:0;border:none">${v3ZoneDetails(c, curZ, zones12)}</td></tr>`;
    }
  }
  return h + '</tbody></table><div class="note">A–H＝淨主淨客（讓球方一致：主讓平半≠客讓平半）；I–P＝計埋主客互換（公式規格_主客對調 v5，雙向總返還率93.9%±0.04）。每格可展開 12 段水位；黃底＝今場尾盤上盤水位所在段。N≤5 照顯示實數。</div>';
}
function v3Cells9HTML(cells, curZ, zones12) {
  if (!cells || !cells.length) return '';
  let h = '';
  for (const c of cells) {
    const ld = c.line_dist ? `<div class="note" style="margin-top:2px">樣本自身尾盤：${esc(c.line_dist)}</div>` : '';
    h += `<details style="margin:4px 0"><summary><span class="sum-t">${esc(c.tri)} × ${esc(c.scope)}</span><span class="sub">${c.n} 場</span></summary><div class="body">${v3Water12Tbl(c, curZ, zones12)}${ld}</div></details>`;
  }
  return h;
}
function v3TriTxt(tri) {
  if (!tri) return '';
  const rows = [['same', '上賽同主客場（原盤直比）'], ['swap', '上賽主客場對調（互換對比盤）'], ['all', '綜合全部（同主客＋對調）']];
  const tbl = `<table class="ck-table"><thead><tr><th>對比口徑</th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>` +
    rows.map(([k, lab]) => {
        const o = tri[k];
        return `<tr><td>${lab}</td><td>${o ? o.n : 0}</td><td class="${o ? _winCls(o.up_r, o.down_r, 'u') : ''}">${o ? pct(o.up_r) : '—'}</td><td class="${o ? _winCls(o.up_r, o.down_r, 'd') : ''}">${o ? pct(o.down_r) : '—'}</td><td>${o ? pct(o.push_r) : '—'}</td></tr>`;
      }).join('') + '</tbody></table>';
  const notes = rows.map(([k, lab]) => {
    const o = tri[k];
    return (o && o.line_dist) ? `<div class="note" style="margin:1px 0">${lab}・樣本自身尾盤：${esc(o.line_dist)}</div>` : '';
  }).join('');
  return tbl + notes;
}
function v3ScopesTbl(scopes) {
  if (!scopes || !scopes.length) return '';
  return `<table class="ck-table"><thead><tr><th>範圍</th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>` +
    scopes.map(o => `<tr><td>${esc(o.scope)}</td><td>${o.n}</td>` +
      `<td class="${_winCls(o.up_r, o.down_r, 'u')}">${pct(o.up_r)}</td><td class="${_winCls(o.up_r, o.down_r, 'd')}">${pct(o.down_r)}</td><td>${pct(o.push_r)}</td></tr>`).join('') +
    '</tbody></table>';
}
function v3DistTbl(dist, curZ, zones12) {
  if (!dist || !dist.length) return '<div class="note">無符合條件嘅歷史場次</div>';
  let h = `<table class="ck-table"><thead><tr><th>尾盤盤口</th><th>場數</th><th>上(${esc(_un())})場</th><th>下(${esc(_dn())})場</th><th>走</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>`;
  for (const r of dist) {
    const hit = r.is_cur ? ' class="hl"' : '';
    const zones = (r.zones || []).map((z, i) => z ? {zone: zones12 ? zones12[i] : String(i), ...z} : null);
    const sub = v3Water12Tbl({n: r.n, zones}, curZ, zones12);
    h += `<tr${hit}><td>${esc(r.line)}${r.is_cur ? ' ◀今場' : ''}</td><td>${r.n}</td>` +
         `<td>${r.up}</td><td>${r.down}</td><td>${r.push}</td>` +
         `<td class="${_winCls(r.up_r, r.down_r, 'u')}">${pct(r.up_r)}</td><td class="${_winCls(r.up_r, r.down_r, 'd')}">${pct(r.down_r)}</td><td>${pct(r.push_r)}</td></tr>`;
    if (zones.some(Boolean)) {
      h += `<tr><td colspan="8" style="padding:0;border:none"><details style="margin:2px 8px"><summary style="padding:4px 8px;font-size:12px;color:var(--dim)">▸ 呢個盤口嘅 12 段水位細分</summary><div class="body">${sub}</div></details></td></tr>`;
    }
  }
  return h + '</tbody></table>';
}
function _gapStr(g){
  if (!g) return '';
  if (typeof g === 'object') return g.text || g.gap_txt || JSON.stringify(g);
  return String(g);
}
function v3GapCard(pack, curZ) {
  if (!pack) return '<div class="note">樣本不足</div>';
  const cz = curZ != null ? curZ
    : (pack.cur_zone12 != null && pack.cur_zone12 >= 0 ? pack.cur_zone12 : -1);
  const zonesTbl = pack.zones ? v3Water12Tbl({n: pack.n, zones: pack.zones}, cz, null) : '';
  return `<div class="linebox"><div class="lb-t">${esc(pack.line)}｜${pack.n} 場</div>
    <div class="lb-v">${_udrates(pack.up_r, pack.down_r)}／走 ${pct(pack.push_r)}</div>
    <div class="lb-v" style="color:var(--gold2)">${esc(_gapStr(pack.gap))}</div>
    ${pack.zones ? `<details style="margin:4px 0"><summary style="padding:2px 6px;font-size:11px;color:var(--dim)">▸ 全部水位區嘅盤口勝出率及場次</summary><div class="body">${zonesTbl}</div></details>` : ''}</div>`;
}
function v3F14Cells(cells) {
  if (!cells || !cells.length) return '';
  let h = '<table class="ck-table"><thead><tr><th>字頭</th><th>範圍</th><th>混合</th><th>有記錄場數</th><th>主隊入%</th><th>客隊入%</th><th>冇入%</th></tr></thead><tbody>';
  for (const c of cells) {
    h += `<tr><td><b>${c.letter}</b></td><td>${esc(c.scope)}</td><td>${esc(c.mix)}</td><td>${c.n}</td>` +
         `<td class="r-up">${pct(c.home_r)}</td><td class="r-down">${pct(c.away_r)}</td><td>${pct(c.none_r)}</td></tr>`;
  }
  return h + '</tbody></table>';
}
function v3F14Stats(st) {
  if (!st) return '<div class="note">無數據</div>';
  if (!st.n) return '<div class="note">暫無符合條件又有首14分鐘記錄嘅場次</div>';
  return `<div class="linebox"><div class="lb-t">樣本 ${st.n} 場</div>
    <div class="lb-v">主隊先入 ${pct(st.home_r)}（${st.home}）｜客隊先入 ${pct(st.away_r)}（${st.away}）｜冇入球 ${pct(st.none_r)}（${st.none}）</div></div>`;
}
function v3ComboHTML() {
  const opts = V3_COMBO_OPTS.map(n =>
    `<label style="display:inline-block;margin:2px 8px 2px 0"><input type="checkbox" class="v3cb" value="${n}"> ${n}. ${esc(V3_TITLES[n] || '')}</label>`).join('');
  return `<h4 style="margin:12px 0 4px">組合篩查（剔第 1–38 項中多項，AND；最多 12 項）</h4>
    <div style="max-height:180px;overflow:auto;border:1px solid var(--line);padding:6px;border-radius:6px">${opts}</div>
    <div style="margin-top:8px"><button class="btn accent v3combo-go">提交組合篩查</button>
    <span class="note v3combo-msg" style="margin-left:8px"></span></div>
    <div class="v3combo-res" style="margin-top:8px"></div>`;
}
function v3WireCombo(mid, bodyEl) {
  const go = bodyEl.querySelector('.v3combo-go');
  if (!go) return;
  const boxes = [...bodyEl.querySelectorAll('.v3cb')];
  boxes.forEach(cb => cb.onchange = () => {
    if (boxes.filter(b => b.checked).length > 12) cb.checked = false;
  });
  go.onclick = async () => {
    const sel = boxes.filter(b => b.checked).map(b => b.value);
    const msg = bodyEl.querySelector('.v3combo-msg');
    const res = bodyEl.querySelector('.v3combo-res');
    if (!sel.length) { msg.textContent = '請先剔選至少一項'; return; }
    go.disabled = true; msg.textContent = '篩查中…';
    let r;
    try { r = await jpost('/api/v3/combo', {id: mid, sel}); }
    catch (e) { res.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; go.disabled = false; return; }
    go.disabled = false;
    if (r.error) { msg.textContent = ''; res.innerHTML = `<div class="err">${esc(r.error)}</div>`; return; }
    msg.textContent = `已剔 ${sel.length} 項：${sel.join('、')}`;
    const scopeRow = (o) => o
      ? `<td>${o.n}</td><td>${o.up}</td><td>${o.down}</td><td>${o.push}</td><td class="${_winCls(o.up_r, o.down_r, 'u')}">${pct(o.up_r)}</td><td class="${_winCls(o.up_r, o.down_r, 'd')}">${pct(o.down_r)}</td><td>${pct(o.push_r)}</td>`
      : '<td colspan="7">不適用</td>';
    res.innerHTML = `<table class="ck-table"><thead><tr><th>範圍</th><th>賽事總數(分母)</th><th>上(${esc(_un())})場</th><th>下(${esc(_dn())})場</th><th>走</th><th>上盤(${esc(_un())})率</th><th>下盤(${esc(_dn())})率</th><th>走盤率</th></tr></thead><tbody>` +
      (r.scopes || []).map(o => `<tr><td>${esc(o.scope)}</td>${scopeRow(o)}</tr>`).join('') +
      '</tbody></table>' + v3Water12Tbl(r.water ? r.water.all : null, r.cur_zone12, r.zones12);
  };
}
async function v3LoadItem(mid, no, bodyEl) {
  bodyEl.innerHTML = '<div class="note">計算中…（首次約 1-3 秒）</div>';
  let it;
  try { it = await jget('/api/v3/item?id=' + mid + '&no=' + no); }
  catch (e) { bodyEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (it.error) {
    bodyEl.innerHTML = _udLegend() +
      (it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '') +
      `<div class="err">${esc(it.error)}</div>`;
    return;
  }
  const curZ = it.cur_zone12 != null ? it.cur_zone12 : -1;
  const zones12 = it.zones12 || null;
  let h = _udLegend();
  if (it.ref) h += `<div class="note">參照：${esc(it.ref)}</div>`;
  if (it.mix_note) h += `<div class="note">${esc(it.mix_note)}</div>`;
  if (it.data_note) h += `<div class="err" style="background:#4a3a10;border-color:#a08c2a">${esc(it.data_note)}</div>`;
  if (it.oc) h += `<div class="note" style="margin:6px 0">結果（全條件樣本）：${_ocTxt(it.oc)}</div>`;
  if (it.scopes) h += '<h4 style="margin:10px 0 4px">分範圍結果（同一聯賽／聯賽類／全庫）</h4>' + v3ScopesTbl(it.scopes);
  if (it.tri) h += v3TriTxt(it.tri);
  if (it.cells && it.cells.length && it.cells[0].letter) {
    h += (Number(it.no) >= 39 ? v3F14Cells(it.cells) : v3Cells16Tbl(it.cells, curZ, zones12));
  }
  if (it.cells && it.cells.length && it.cells[0].tri) h += v3Cells9HTML(it.cells, curZ, zones12);
  if (it.dist && it.dist.length) {
    h += '<h4 style="margin:10px 0 4px">盤口分佈（每個盤口可展開 12 段水位細分；黃底＝同今場尾盤一樣嘅盤口）</h4>';
    h += v3DistTbl(it.dist, curZ, zones12);
  }
  if (it.water) {
    for (const [k, lab] of [['all', '全庫']]) {
      if (!it.water[k]) continue;
      h += `<h4 style="margin:10px 0 4px">12段水位表・${lab}</h4>` + v3Water12Tbl(it.water[k], curZ, zones12);
    }
  }
  if (it.mode || it.d50) {
    h += '<h4 style="margin:10px 0 4px">分佈最多盤口</h4>' + v3GapCard(it.mode, curZ);
    h += '<h4 style="margin:10px 0 4px">上盤勝率最接近 50% 嘅盤口（至少5場）</h4>' + v3GapCard(it.d50, curZ);
  }
  if (it.parts) {
    for (const p of it.parts) {
      h += `<h4 style="margin:10px 0 4px">${esc(p.name)}</h4>` + v3F14Stats(p.stats);
    }
  }
  if (it.stats) h += v3F14Stats(it.stats);
  bodyEl.innerHTML = h;
}
/* 45 項 accordion（詳情頁同精選W 卡片共用） */
function buildV3ItemsAccordion(mid, box, applicability) {
  let h = '';
  for (let n = 1; n <= 45; n++) {
    const no = String(n);
    const ok = applicability ? applicability[no] : true;
    h += `<details class="v3-item" data-no="${no}"><summary><span class="sum-t">${no}. ${esc(V3_TITLES[no] || '')}</span>` +
         (ok ? '' : '<span class="sub">不適用</span>') + '</summary><div class="body"></div></details>';
  }
  h += `<details class="v3-item v3-combo" data-no="combo"><summary><span class="sum-t">＋ 組合篩查（第 1–38 項多選）</span></summary><div class="body">${v3ComboHTML()}</div></details>`;
  box.innerHTML = h;
  box.querySelectorAll('details.v3-item').forEach(d => {
    d.addEventListener('toggle', () => {
      if (d.open && !d.dataset.loaded) {
        d.dataset.loaded = '1';
        if (d.dataset.no === 'combo') v3WireCombo(mid, d.querySelector('.body'));
        else v3LoadItem(mid, d.dataset.no, d.querySelector('.body'));
      }
    });
  });
}
async function initV3Section(mid) {
  const box = document.getElementById('v3Items');
  const sumEl = document.getElementById('v3Sum');
  if (!box) return;
  let s;
  try { s = await jget('/api/v3/summary?id=' + mid); }
  catch (e) { sumEl.innerHTML = '<span class="err">V3 載入失敗：' + esc(String(e)) + '</span>'; return; }
  if (s.error) { sumEl.innerHTML = '<span class="err">' + esc(s.error) + '</span>'; return; }
  const fw = s.featured_w || {};
  const dirTxt = fw.pass ? (fw.direction === 'up' ? '上盤' : '下盤') : null;
  // 今場上盤水位區（highlight 用）：讓球方水位；平手盤用主隊水位
  const _c = curMatch && curMatch.close;
  const curZ = _c ? _zone12Idx(_c.g === 'away' ? _c.ao : _c.ho) : -1;
  sumEl.innerHTML = _udLegend() +
    `<div style="font-size:15px;margin-bottom:4px">🏆 精選W 檢查（1A、1I、19同主客、19+互換、31、35 全部同方向≥50%）：${dirTxt ? `<b class="r-up">✅ 合格 → ${dirTxt}(${esc(_dirTeam(fw.direction))})</b>` : `<span style="color:var(--dim)">❌ 未合格${fw.fail_note ? '（' + esc(fw.fail_note) + '）' : ''}</span>`}</div>` +
    (fw.lookback ? _fwLookbackHTML(fw.lookback, curZ) : '') +
    (s.twin ? _lb4HTML(s.twin, curZ, '🧬 孖生回查',
      '三節點盤口水位（初盤/開賽前4小時/尾盤，盤口100%＋水位±0.03）＋上賽盤口相同（可互換）＋主客入球差相同嘅歷史場次・四個口徑') : '') +
    (fw.any_lb ? _anyLbHTML(fw.any_lb, curZ) : '');
  buildV3ItemsAccordion(mid, box, s.applicability);
}

/* ================= 精選W ================= */
/* Any5／Any4 每條件嘅 % 細節（同六項全中格式一致：上下盤%＋場次＋同水位區%） */
function _anyDetail(x, conds){
  return (x.which || []).map(nm => {
    const c = (conds || []).find(c => (c.note || '').split('：')[0] === nm);
    if (!c || !c.oc) return `<span style="color:var(--dim)">${esc(nm)}：無數據</span>`;
    const zr = c.zone_r ? ` <span style="color:#ffd766">同水位區【${esc(c.zone_r.zone || '')}】${_udrates(c.zone_r.up_r, c.zone_r.down_r)}｜${c.zone_r.n}場</span>` : '';
    return `<span>${esc(nm)}：${_ocTxt(c.oc)}${zr}</span>`;
  }).join('<br>');
}
function _fwCondCell(c){
  if (!c) return '<span style="color:var(--dim)">—</span>';
  if (!c.oc) return `<span style="color:var(--dim)">${esc(c.note || '無數據')}</span>`;
  const dirTag = c.dir ? (c.dir === 'up' ? `<b class="r-up">↑上</b>` : `<b class="r-down">↓下</b>`) : '<span style="color:var(--dim)">無方向</span>';
  const zr = c.zone_r ? `<br><span style="color:#ffd766">同水位區【${esc(c.zone_r.zone || '')}】${_udrates(c.zone_r.up_r, c.zone_r.down_r)}｜${c.zone_r.n}場</span>` : '';
  return `${dirTag}<br>${_ocTxt(c.oc)}${zr}`;
}
/* 水位值 → 12段水位 index（同 v3_engine.ZONES12 一致：≤0.64 / 0.65-0.69 … ≥1.15） */
function _zone12Idx(w){
  if (w == null || isNaN(w)) return -1;
  if (w <= 0.64) return 0;
  if (w >= 1.15) return 11;
  return Math.round((w - 0.65) / 0.05) + 1;
}
function _fwGapTxt(g, curZ){
  if (!g || g.error) return '<span style="color:var(--dim)">不適用</span>';
  return `<span style="color:var(--dim)">今場尾盤</span> ${esc(g.cur_line || '—')}<br>` +
    `<span style="color:var(--dim)">分佈最多</span> ` + v3GapCard(g.mode, curZ) +
    `<span style="color:var(--dim)">最接近50%</span> ` + v3GapCard(g.d50, curZ);
}
/* ---------- 四口子回查渲染（2026-09-29）----------
   每口徑：綜合上/下/走 ＋ 12 段水位區域。兼容舊 detail JSON（oc 直放無 water）。 */
const LB_SCOPES = [['all', '全庫（連互換）'], ['pure', '淨主淨客（全庫）'],
                   ['lg_all', '同聯賽/杯（連互換）'], ['lg_pure', '淨主淨客（同聯賽/杯）']];
const LB_ZONES12 = ['≤0.64', '0.65-0.69', '0.70-0.74', '0.75-0.79', '0.80-0.84', '0.85-0.89',
                    '0.90-0.94', '0.95-0.99', '1.00-1.04', '1.05-1.09', '1.10-1.14', '≥1.15'];
function _lbOc(scope){
  if (!scope) return null;
  if ('oc' in scope) return scope.oc;   // 新格式（oc 可能係 null＝冇樣本）
  return scope;                          // 舊格式（oc 直放，冇 water）
}
function _lbWater(scope){ return scope && scope.water; }
function _lbZonesHTML(lb, curZ){
  const rows = [];
  for (let z = 0; z < 12; z++) {
    let tds = '', any = false;
    for (const [k] of LB_SCOPES) {
      const w = _lbWater(lb[k]);
      const zr = w && (w.zones || [])[z];
      if (zr && zr.n > 0) {
        any = true;
        tds += `<td>上${pct(zr.up_r)} 下${pct(zr.down_r)}<br><span style="color:var(--dim)">${zr.n}場</span></td>`;
      } else tds += '<td style="color:var(--dim)">—</td>';
    }
    if (!any) continue;
    const cur = (curZ === z) ? ' class="cur"' : '';
    rows.push(`<tr${cur}><td${cur}>${LB_ZONES12[z]}${curZ === z ? ' ◀今場' : ''}</td>${tds}</tr>`);
  }
  if (!rows.length) return '';
  return `<details style="margin-top:4px"><summary><span class="sum-t" style="font-size:13px">各水位區域（12 段・黃底＝今場上盤水位區）</span></summary>` +
    `<div class="checkwrap"><table class="lb4-tbl"><thead><tr><th>水位區</th>` +
    LB_SCOPES.map(([, lab]) => `<th>${lab}</th>`).join('') +
    `</tr></thead><tbody>${rows.join('')}</tbody></table></div></details>`;
}
/* 四口子回查主渲染：lb＝{all,pure,lg_all,lg_pure}，curZ＝今場上盤水位區 index（-1 無） */
function _lb4HTML(lb, curZ, title, note){
  if (!lb) return '';
  const cell = (k, lab) => {
    const o = _lbOc(lb[k]);
    return `<div class="gi"><div class="t">${lab}</div><div class="v">` +
      (o ? `${_udrates(o.up_r, o.down_r)}<span style="color:var(--dim)">｜走${pct(o.push_r)}｜${o.n}場</span>`
         : '<span style="color:var(--dim)">—</span>') + `</div></div>`;
  };
  return `<div class="gaprow"><span class="lab">${title}</span><span class="note">${note || ''}</span></div>
    <div class="grid6">${LB_SCOPES.map(([k, lab]) => cell(k, lab)).join('')}</div>` +
    _lbZonesHTML(lb, curZ);
}
/* 回查實際：同初盤&尾盤樣本（盤口100%一樣＋水位±0.03）四個口徑嘅實際上下盤%、場數、各水位區 */
function _fwLookbackHTML(lb, curZ){
  return _lb4HTML(lb, curZ == null ? -1 : curZ, '回查實際',
    '同初盤&尾盤樣本（盤口100%一樣＋水位±0.03）嘅實際開出結果・四個口徑');
}
/* Any5／Any4 首個組合嘅「相同情況」回查 */
function _anyLbHTML(anyLb, curZ){
  if (!anyLb) return '';
  let h = '';
  if (anyLb.any5)
    h += _lb4HTML(anyLb.any5, curZ == null ? -1 : curZ, '⚡ Any 5 相同情況回查',
      '上述 5 條件同時成立嘅歷史場次・四個口徑（綜合＋各水位區域）');
  if (anyLb.any4)
    h += _lb4HTML(anyLb.any4, curZ == null ? -1 : curZ, '🔸 Any 4 相同情況回查',
      '上述 4 條件同時成立嘅歷史場次・四個口徑（綜合＋各水位區域）');
  return h;
}
/* ---------- 六條件規律說明（1A／1I／19／19+互換／31／35 常駐說明，唔迫埋一齊） ---------- */
const FW_COND_DEFS = [
  ['1A', '同初盤&水位＋同尾盤（淨主淨客・全庫）',
   '歷史場次【初盤】及【尾盤】同今場尾盤：讓球數＋讓球方 100% 相同，主、客水位各 ±0.03；只計主隊作主場、客隊作客場（淨主淨客）。'],
  ['1I', '同上但計埋主客互換（全庫）',
   '條件同 1A，但歷史場次計埋主客互換版（對調換算：讓球 s+0.5、水位用 HR 模型逆推）。'],
  ['19', '上賽完全相同（只計同主、同客）',
   '今場主隊對上一次比賽：主/客角色＋讓/受讓＋讓球數完全相同，兼且方向同今場（該場主隊當時都係主場）。'],
  ['19＋互換', '上賽完全相同（同主客＋對調）',
   '條件同 19，但計埋今場主隊上次作客（角色對調、換算後盤口相同）嘅場次。'],
  ['31', '主主場排名−客客場排名差距淨值（±1）',
   '歷史場次「主隊主場排名 − 客隊客場排名」嘅差距，同今場差距（±1 容差）相同。'],
  ['35', '主主場入失球 及 客客場（各±3）',
   '歷史場次主隊主場入球/失球/得失球差 及 客隊客場（各 ±3 球）同今場相同。'],
];
function _fwLegendHTML(){
  return `<details style="margin-top:6px"><summary><span class="sum-t">📖 六條件規律說明（1A／1I／19／19+互換／31／35——每條都要全庫該方向 ≥50% 先有方向，六條一致先入選）</span></summary><div class="body">` +
    FW_COND_DEFS.map(([nm, t, d]) =>
      `<div class="leg-row"><span class="leg-nm">${esc(nm)}</span><div><b>${esc(t)}</b><div class="sub">${esc(d)}</div></div></div>`
    ).join('') + `</div></details>`;
}
/* 精選7/8/12 格名對照（同 fw_grid_engine.CELL_NAMES） */
const FW_CELL_NAMES = {
  '1A': '同初盤&水位＋同尾盤（淨主淨客·全庫）', '1G': '同初盤&水位＋同尾盤（淨主淨客·同一聯賽）',
  '1I': '同初盤&水位＋同尾盤（互換·全庫）', '1O': '同初盤&水位＋同尾盤（互換·同一聯賽）',
  '3A': '同30分鐘&水位＋同尾盤（淨主淨客·全庫）', '3G': '同30分鐘&水位＋同尾盤（淨主淨客·同一聯賽）',
  '3I': '同30分鐘&水位＋同尾盤（互換·全庫）', '3O': '同30分鐘&水位＋同尾盤（互換·同一聯賽）',
  '7': '同初盤純盤口', '19': '上賽完全相同（同主客）',
  '21': '主主場及客客場勝和負±7%', '25': '主主場及客客場入失球±3%',
};
function _fwgLegendHTML(cells){
  return `<details style="margin-top:6px"><summary><span class="sum-t">📖 格規律說明（${cells.map(esc).join('、')}）</span></summary><div class="body">` +
    cells.map(cn => `<div class="leg-row"><span class="leg-nm">${esc(cn)}</span><div>${esc(FW_CELL_NAMES[cn] || '')}</div></div>`).join('') +
    `</div></details>`;
}
/* 命中率回查：呢套規則歷史入選場喺全庫／同聯賽嘅實際命中率（/api/hitrate） */
const _hrCache = {};
async function _fillHrBlocks(root){
  if (!root) return;
  const els = root.querySelectorAll('.hrb:not([data-done])');
  for (const el of els) {
    const kind = el.dataset.kind, mid = el.dataset.mid;
    if (!kind || !mid) continue;
    el.dataset.done = '1';
    const key = kind + '|' + mid;
    let d = _hrCache[key];
    if (!d) {
      el.innerHTML = '<div class="note" style="font-size:12px">⏳ 載入緊：同聯賽命中率…</div>';
      try { d = await jget(`/api/hitrate?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(mid)}`); }
      catch (e) { d = {error: String(e)}; }
      _hrCache[key] = d;
    }
    if (!d || d.error) {
      el.innerHTML = `<div class="note" style="font-size:12px;color:var(--dim)">命中率：${esc((d && d.error) || '載入失敗')}</div>`;
      continue;
    }
    const fmt = o => (o && o[0]) ? `${pct(o[1] / o[0])}（${o[0]}場）` : '—';
    const row = (lab, o) => `<div style="font-size:13px;line-height:1.7">${lab}　上盤 <b>${fmt(o.up)}</b>｜下盤 <b>${fmt(o.down)}</b></div>`;
    /* fwx（精選W）：加「同今場尾盤盤口」口徑——舊版全庫數字對每場都一樣（2026-10-02 修正） */
    const isFx = kind === 'fwx';
    const fxLine = isFx && d.line && d.line.up && d.line.up[0] + d.line.down[0] > 0;
    el.innerHTML = `<div class="gaprow" style="margin-top:6px"><span class="lab">命中率</span>` +
      `<span style="color:var(--dim);font-size:12px">呢套規則歷史入選場實際開出（走盤計場數、唔計命中）${fxLine ? '；🎯＝尾盤盤口同今場一樣嘅入選場' : ''}</span></div>` +
      (fxLine ? row(`🎯 全庫・同今場盤口（${esc(d.line_label || '')}）`, d.line) : '') +
      (isFx && d.line_league_stats && d.line_league_stats.up[0] + d.line_league_stats.down[0] > 0
        ? row(`🎯 同聯賽・同盤口${d.league ? '（' + esc(d.league) + '）' : ''}`, d.line_league_stats) : '') +
      row('🌍 全資料庫（全部入選場）', d.all) +
      row(`🏆 同聯賽/杯${d.league ? '（' + esc(d.league) + '）' : ''}`, d.league_stats);
  }
}
function _fwCard(p){
  _udSetByLine(p.line, p.home, p.away);
  const dName = p.direction === 'up' ? '上盤' : '下盤';
  const ud = _fwUD(p);
  const res = p.state === 'scheduled' ? '<span style="color:var(--dim)">未開賽</span>'
    : p.result === 'W' ? '<b class="r-up">✅ 命中</b>'
    : p.result === 'L' ? '<b class="r-down">❌ 未中</b>'
    : p.result === 'P' ? '<b>➖ 走盤</b>'
    : p.state === 'live' ? '<span class="tag live">進行中</span>'
    : '<span style="color:var(--dim)">待結算</span>';
  const conds = p.conds || [];
  return `<div class="fcard" data-mid="${p.id}">
    <div class="f-top">
      <span class="f-time">${esc((p.kickoff || '').slice(5, 16))}　${esc(p.league)}</span>
      <span class="f-teams">${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}</span>
      ${p.score ? `<span class="f-score">${esc(p.score)}${p.ht ? `<span style="color:var(--dim);font-weight:400">（半${esc(p.ht)}）</span>` : ''}${p.first_goal ? ` <span style="color:var(--gold2)">首球${p.first_goal === 'home' ? '主' : '客'}</span>` : ''}</span>` : (p.ht ? `<span class="f-score" style="color:var(--dim)">半${esc(p.ht)}</span>` : '')}
      <span class="tag ${p.direction}">${dName}</span>
      <span>${res}</span>
      <span class="f-btns">
        <button class="btn ft-refresh">⟳ 重新整理</button>
        <button class="btn ft-share">⇗ 分享</button>${BetSlip.btn(p)}
      </span>
    </div>
    <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
    ${p.line_moved ? `<div class="gaprow" style="border:1px solid #d8a03c;background:rgba(216,160,60,.12);border-radius:8px;padding:6px 8px"><span class="lab">⚠️ 尾盤已變</span>入選/檢定時（${esc((p.snap_at || '').slice(5, 16) || '—')}）：<b>${esc(p.line_at_pick || '—')}</b> → 而家：<b>${esc(p.line || '—')} ${esc(p.odds || '')}</b>　條件已按新尾盤自動重檢</div>` : ''}
    <div class="gaprow"><span class="lab">本場</span>上盤＝<b>${esc(ud.up)}</b>｜下盤＝<b>${esc(ud.down)}</b>${ud.push ? '（平手盤：上盤＝平手主隊）' : ''}</div>
    <div class="grid6">${conds.map((c, i) => `<div class="gi"><div class="t">條件${i + 1}</div><div class="v">${_fwCondCell(c)}<div class="sub">${esc(c.note || '')}</div></div></div>`).join('')}</div>
    ${p.lookback ? _fwLookbackHTML(p.lookback, _zone12Idx(p.up_odds)) : ''}
    <div class="hrb" data-kind="fwx" data-mid="${p.id}"></div>
    <div class="gaprow"><span class="lab">30 淺深</span>${_fwGapTxt(p.gap30, _zone12Idx(p.up_odds))}</div>
    <details style="margin-top:6px"><summary><span class="sum-t">⚽ 場次分析 45 項（全資料）</span></summary><div class="body v3-fullitems"><div class="note">載入中…</div></div></details>
  </div>`;
}
function _fwUD(p){
  return _upDownOf(p.line, p.home, p.away) || {up: '上盤', down: '下盤', push: false};
}
async function loadFeaturedW(){
  const listEl = $('#fwList');
  listEl.innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget('/api/v3/featured/full'); }
  catch (e) { listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats;
  $('#fwCnt').textContent = s.pending ? `(${s.pending})` : '';
  $('#fwStats').innerHTML = `<div class="statbar">
    <span>場數 <b>${s.total}</b></span><span>未開賽 <b>${s.pending}</b></span>
    <span>進行中 <b>${s.live}</b></span><span>已完場 <b>${s.played}</b></span>
    <span>命中 <b class="r-up">${s.wins}</b></span><span>未中 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>
    <div class="note">入選準則（六條件全部同方向≥50%）：①1A 同初盤&水位＋同尾盤（淨主淨客・全庫）②1I 同上但計埋主客互換 ③19 上賽完全相同（只計同主、同客）④19＋互換 ⑤31 主主場排名−客客場排名差距淨值（±1）⑥35 主主場入失球 及 客客場（各±3）。每條件展示盤口%＋同水位區%。合格後展示方向＋30 項淺深分析。已完場場次永久保留（事後回查頁）。Any 5／Any 4 近合格場附「相同情況」四口子回查（綜合＋各水位區域）。</div>` +
    _fwLegendHTML();
  League7.mount('w', $('#fwStats'));
  const scan = d.scan || {};
  $('#fwScanInfo').textContent = scan.running
    ? `掃描中 ${scan.done}/${scan.total}…`
    : (scan.last ? `上次掃描：${scan.last}｜新增 ${scan.added || 0}` : '');
  let h = '';
  if (d.pending.length) h += `<div class="day-h">🔜 未開賽（${d.pending.length} 場・順開賽時間）</div>` + d.pending.map(_fwCard).join('');
  if (d.live.length) h += `<div class="day-h">🔴 進行中（${d.live.length} 場）</div>` + d.live.map(_fwCard).join('');
  if (d.played.length) h += `<div class="day-h">📼 已完場（${d.played.length} 場・最新排先・永久保留）</div>` + d.played.map(_fwCard).join('');
  if (!h) h = '<div class="note">暫無精選W場次。撳「🔍 重新掃描精選W」即刻篩過。</div>';
  const near = d.near || [];
  if (near.length) {
    const nearCard = n => {
      _udSetByLine(n.line, n.home, n.away);
      const dirName = x => x === 'up' ? '上盤' : '下盤';
      const a5 = (n.any5 || []).map(x =>
        `<div>⚡ Any 5 → <b class="${x.dir === 'up' ? 'r-up' : 'r-down'}">${dirName(x.dir)}(${esc(_dirTeam(x.dir))})</b>（${esc(x.which.join('、'))}）<div class="sub">${_anyDetail(x, n.conds)}</div></div>`).join('');
      const a4 = (n.any4 || []).map(x =>
        `<div>🔸 Any 4 → <b class="${x.dir === 'up' ? 'r-up' : 'r-down'}">${dirName(x.dir)}(${esc(_dirTeam(x.dir))})</b>（${esc(x.which.join('、'))}）<div class="sub">${_anyDetail(x, n.conds)}</div></div>`).join('');
      const anyLb = _anyLbHTML(n.any_lb, -1);
      return `<div class="fcard" data-mid="${n.id}">
        <div class="f-top">
          <span class="ko">${esc((n.kickoff || '').slice(5, 16))}</span>
          <span class="lg">${esc(n.league || '')}</span>
          <span style="flex:1">${esc(n.home)} vs ${esc(n.away)}</span>
        </div>
        <div class="sub" style="margin:4px 0">${a5}${a4}</div>
        ${anyLb}
      </div>`;
    };
    h += `<div class="day-h">⚠️ Any 5／Any 4 近合格（${near.length} 場・未完全滿足六條件）</div>` + near.map(nearCard).join('');
  }
  listEl.innerHTML = h;
  _fillHrBlocks(listEl);
  [...listEl.querySelectorAll('.mrow')].forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
  [...listEl.querySelectorAll('.fcard')].forEach(c => {
    const mid = +c.dataset.mid;
    const rec = [...d.pending, ...d.live, ...d.played].find(x => x.id === mid);
    if (!rec) {
      // 近合格卡（Any5/Any4）：淨係可以撳入詳情
      c.querySelector('.f-top').onclick = () => openDetail(mid);
      return;
    }
    const ud = _fwUD(rec);
    c.querySelector('.ft-refresh').onclick = async ev => {
      ev.stopPropagation();
      toast('重新整理中…');
      const r = await jpost('/api/v3/featured/refresh', {id: mid});
      toast(r.ok ? (r.pass ? '仍合精選W準則' : (r.removed ? '已移出精選W' : '已更新')) : ('失敗：' + (r.error || '')));
      loadFeaturedW();
    };
    c.querySelector('.ft-share').onclick = async ev => {
      ev.stopPropagation();
      shareText(fwShareText(rec, ud));
    };
    const det = c.querySelector('details');
    det.addEventListener('toggle', async () => {
      if (det.open && !det.dataset.loaded) {
        det.dataset.loaded = '1';
        const bodyEl = det.querySelector('.v3-fullitems');
        bodyEl.innerHTML = '<div class="note">載入中…</div>';
        const s = await jget('/api/v3/summary?id=' + mid);
        setV3UD({home: rec.home, away: rec.away, close: rec.line ? {g: rec.line.indexOf('主讓') === 0 ? 'home' : rec.line.indexOf('客讓') === 0 ? 'away' : 'none'} : null});
        buildV3ItemsAccordion(mid, bodyEl, s.applicability);
      }
    });
    c.querySelector('.f-top').onclick = ev => {
      if (!ev.target.closest('.btn')) openDetail(mid);
    };
  });
}
function fwShareText(p, ud){
  const L = [];
  L.push(`【精選W】${p.league} ${(p.kickoff || '').slice(5, 16)}`);
  L.push(`${p.home} vs ${p.away}${p.score ? '（' + p.score + '）' : ''}`);
  L.push(`方向：${p.direction === 'up' ? '上盤' : '下盤'}｜尾盤 ${p.line || '—'} ${p.odds || ''}`);
  L.push(`本場：上盤＝${ud.up}｜下盤＝${ud.down}`);
  (p.conds || []).forEach((c, i) => {
    if (c.oc) L.push(`條件${i + 1}：上${pct(c.oc.up_r)} 下${pct(c.oc.down_r)}（${c.oc.n}場）`);
  });
  if (p.result) L.push(`結果：${p.result === 'W' ? '✅命中' : p.result === 'L' ? '❌未中' : '➖走'}`);
  return L.join('\n');
}
$('#btnFwScan').onclick = async function(){
  this.disabled = true;
  try {
    await jpost('/api/v3/featured/scan', {tool: '精選W掃描掣'});
    pollScanJob('fw', '/api/v3/featured/scan-status', '#btnFwScan', '#fwScanInfo', loadFeaturedW);
  } catch (e) {
    toast('掃描失敗：' + e.message);
    this.disabled = false;
  }
};

/* ---------- 精選7／8／12（減格實驗格組合） ---------- */
/* 格組合定稿（2026-09-28）：必填格 7/19/21/25 先有共識，其餘格全部同方向先入選 */
const FW_GRID_DEFS = {
  '7': {cells: ['1I', '3A', '3I', '7', '19', '21', '25'],
        note: '驗證期下盤70.0%・851場合格'},
  '8': {cells: ['1I', '1O', '3A', '3I', '7', '19', '21', '25'],
        note: '驗證期下盤70.0%・531場合格'},
  '12': {cells: ['1A', '1G', '1I', '1O', '3A', '3G', '3I', '3O', '7', '19', '21', '25'],
        note: '全部12格・極嚴格'}};
function _fwgCell(c, active){
  const on = active.includes(c.cell);
  const dr = c.dir === 'up' ? c.up_r : c.dir === 'down' ? c.down_r : null;
  const cls = c.dir === 'up' ? 'r-up' : c.dir === 'down' ? 'r-down' : '';
  const pctTxt = dr != null ? pct(dr) : (c.n ? '±' : '—');
  const fullName = FW_CELL_NAMES[c.cell] || c.name || '';
  return `<div class="gi${on ? ' on' : ''}" title="${esc(fullName)}" style="${on ? 'outline:2px solid #c9a227;' : 'opacity:.55;'}">` +
    `<div class="t">${esc(c.cell)}${on ? '●' : ''}</div>` +
    `<div class="v"><b class="${cls}">${c.dir === 'up' ? '上' : c.dir === 'down' ? '下' : '—'} ${pctTxt}</b>` +
    `<div class="sub">${c.n}場${!on && fullName ? '｜' + esc(fullName) : ''}</div></div></div>`;
}
/* 12 格方向格仔（黃框＝該格組合用到嘅格）；精選7/8/12 卡同 Check 單場檢驗共用 */
function _fwgCellsHTML(cells, gridCells){
  return (cells || []).map(c => _fwgCell(c, gridCells)).join('');
}
function _fwgStrict(s){
  if (!s) return '';
  const rows = ['29', '31', '33', '35'].map(rn => {
    const o = s[rn];
    if (!o) return `<div class="gi"><div class="t">${rn}</div><div class="v">無樣本</div></div>`;
    return `<div class="gi"><div class="t">${rn} 同盤口</div><div class="v">` +
      `<b class="${o.up >= o.down ? 'r-up' : 'r-down'}">${o.up >= o.down ? '上' : '下'} ${pct(o.up >= o.down ? o.up_r : o.down_r)}</b>` +
      `<div class="sub">${o.n}場</div></div></div>`;
  }).join('');
  return `<div class="grid6" style="margin-top:6px">${rows}</div>`;
}
function _fwGridCard(p, g){
  const def = FW_GRID_DEFS[g];
  const ud = _fwUD(p);
  const dn = p.direction === 'up' ? '上盤' : '下盤';
  const resBadge = p.result === 'W' ? '<b class="r-up">✅命中</b>'
    : p.result === 'L' ? '<b class="r-down">❌未中</b>'
    : p.result === 'P' ? '<b>➖走</b>' : '';
  return `<div class="fcard" data-mid="${p.id}">
    <div class="f-top">
      <div class="f-head"><span class="ko">${esc((p.kickoff || '').slice(5, 16))}</span>
        <span class="lg">${esc(p.league || '')}</span>
        <span class="tag ${p.direction}">${dn}</span>
        <button class="btn ft-refresh">⟳</button>
        <button class="btn gold ft-share">⇗</button></div>
      <div class="teams">${esc(p.home)} <span class="vs">vs</span> ${esc(p.away)}${p.score ? ` <b>${esc(p.score)}</b>` : ''} ${resBadge}</div>
      <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
    ${p.line_moved ? `<div class="gaprow" style="border:1px solid #d8a03c;background:rgba(216,160,60,.12);border-radius:8px;padding:6px 8px"><span class="lab">⚠️ 尾盤已變</span>入選/檢定時（${esc((p.snap_at || '').slice(5, 16) || '—')}）：<b>${esc(p.line_at_pick || '—')}</b> → 而家：<b>${esc(p.line || '—')} ${esc(p.odds || '')}</b>　條件已按新尾盤自動重檢</div>` : ''}
      <div class="gaprow"><span class="lab">本場</span>上盤＝<b>${esc(ud.up)}</b>｜下盤＝<b>${esc(ud.down)}</b>${ud.push ? '（平手盤：上盤＝平手主隊）' : ''}</div>
      <div class="gaprow"><span class="lab">同聯賽</span>${_lgRateSlot(p.league, ['f' + g])}</div>
      <div class="grid6">${_fwgCellsHTML(p.cells, def.cells)}</div>
      ${_fwgStrict(p.strict)}
      <div class="hrb" data-kind="fw${g}" data-mid="${p.id}"></div>
      <details style="margin-top:6px"><summary><span class="sum-t">⚽ 場次分析 45 項（全資料）</span></summary><div class="body v3-fullitems"><div class="note">載入中…</div></div></details>
    </div></div>`;
}
async function loadFwGrid(g){
  const listEl = $('#fw' + g + 'List');
  listEl.innerHTML = '<div class="note">⏳ 載入緊：精選 ' + g + '…</div>';
  LoadX.show('載入精選 ' + g + ' 格');
  let d;
  try { d = await jget('/api/fwgrid/full?g=' + g); }
  catch (e) { LoadX.hide(); listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  LoadX.hide();
  if (d.error) { listEl.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  const s = d.stats;
  $('#fw' + g + 'Cnt').textContent = s.pending ? `(${s.pending})` : '';
  $('#fw' + g + 'Stats').innerHTML = `<div class="statbar">
    <span>場數 <b>${s.total}</b></span><span>未開賽 <b>${s.pending}</b></span>
    <span>進行中 <b>${s.live}</b></span><span>已完場 <b>${s.played}</b></span>
    <span>命中 <b class="r-up">${s.wins}</b></span><span>未中 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>
    <div class="note">入選準則：格組合 ${FW_GRID_DEFS[g].cells.join('、')}（${FW_GRID_DEFS[g].note}）。必填格 7/19/21/25 先有共識，其餘格全部同方向先入選；黃框＝該格組合用到嘅格。已完場場次永久保留。</div>` +
    _fwgLegendHTML(FW_GRID_DEFS[g].cells);
  League7.mount('g' + g, $('#fw' + g + 'Stats'));
  const scan = d.scan || {};
  $('#fw' + g + 'ScanInfo').textContent = scan.running
    ? `掃描中 ${scan.done}/${scan.total}…`
    : (scan.last ? `上次掃描：${scan.last}｜新增 ${scan.added || 0}` : '');
  let h = '';
  if (d.pending.length) h += `<div class="day-h">🔜 未開賽（${d.pending.length} 場・順開賽時間）</div>` + d.pending.map(p => _fwGridCard(p, g)).join('');
  if (d.live.length) h += `<div class="day-h">🔴 進行中（${d.live.length} 場）</div>` + d.live.map(p => _fwGridCard(p, g)).join('');
  if (d.played.length) h += `<div class="day-h">📼 已完場（${d.played.length} 場・最新排先・永久保留）</div>` + d.played.map(p => _fwGridCard(p, g)).join('');
  if (!h) h = `<div class="note">暫無精選${g}場次。撳「🔍 重新掃描精選${g}」即刻篩過。</div>`;
  listEl.innerHTML = h;
  _fillLgSlots();
  _fillHrBlocks(listEl);
  [...listEl.querySelectorAll('.fcard')].forEach(c => {
    const mid = +c.dataset.mid;
    const rec = [...d.pending, ...d.live, ...d.played].find(x => x.id === mid);
    const ud = _fwUD(rec);
    c.querySelector('.ft-refresh').onclick = async ev => {
      ev.stopPropagation();
      toast('重新整理中…');
      const r = await jpost('/api/fwgrid/refresh', {id: mid, g});
      toast(r.ok ? (r.pass ? `仍合精選${g}準則` : (r.removed ? `已移出精選${g}` : '已更新')) : ('失敗：' + (r.error || '')));
      loadFwGrid(g);
    };
    c.querySelector('.ft-share').onclick = async ev => {
      ev.stopPropagation();
      const L = [];
      L.push(`【精選${g}】${rec.league} ${(rec.kickoff || '').slice(5, 16)}`);
      L.push(`${rec.home} vs ${rec.away}${rec.score ? '（' + rec.score + '）' : ''}`);
      L.push(`方向：${rec.direction === 'up' ? '上盤' : '下盤'}｜尾盤 ${rec.line || '—'} ${rec.odds || ''}`);
      L.push(`本場：上盤＝${ud.up}｜下盤＝${ud.down}`);
      (rec.cells || []).forEach(c2 => {
        const dr = c2.dir === 'up' ? c2.up_r : c2.dir === 'down' ? c2.down_r : null;
        if (dr != null) L.push(`${c2.cell}：${c2.dir === 'up' ? '上' : '下'}${pct(dr)}（${c2.n}場）`);
      });
      if (rec.result) L.push(`結果：${rec.result === 'W' ? '✅命中' : rec.result === 'L' ? '❌未中' : '➖走'}`);
      shareText(L.join('\n'));
    };
    const det = c.querySelector('details');
    det.addEventListener('toggle', async () => {
      if (det.open && !det.dataset.loaded) {
        det.dataset.loaded = '1';
        const bodyEl = det.querySelector('.v3-fullitems');
        bodyEl.innerHTML = '<div class="note">載入中…</div>';
        const s2 = await jget('/api/v3/summary?id=' + mid);
        setV3UD({home: rec.home, away: rec.away, close: rec.line ? {g: rec.line.indexOf('主讓') === 0 ? 'home' : rec.line.indexOf('客讓') === 0 ? 'away' : 'none'} : null});
        buildV3ItemsAccordion(mid, bodyEl, s2.applicability);
      }
    });
    c.querySelector('.f-top').onclick = ev => {
      if (!ev.target.closest('.btn') && !ev.target.closest('details')) openDetail(mid);
    };
  });
}
['7', '8', '12'].forEach(g => {
  $('#btnFw' + g + 'Scan').onclick = async function(){
    this.disabled = true;
    try {
      await jpost('/api/fwgrid/scan', {g});
      pollScanJob('fwg' + g, '/api/fwgrid/scan-status?g=' + g,
                  '#btnFw' + g + 'Scan', '#fw' + g + 'ScanInfo', () => loadFwGrid(g));
    } catch (e) {
      toast('掃描失敗：' + e.message);
      this.disabled = false;
    }
  };
});

/* ---------- 大小規則（分聯賽 O/U 規則版：ou_rules.json 規則卡命中先出手） ---------- */
function _ouTierBadge(tier){
  // 觀察＝黃、種子＝灰、試行＝藍
  const map = {'觀察': 'background:#4a3a10;color:#ffd766',
               '種子': 'background:#2a2f3a;color:#9aa4b2',
               '試行': 'background:#12283a;color:#7ec8ff'};
  return `<span class="tag" style="${map[tier] || map['種子']}">${esc(tier || '—')}</span>`;
}
function _ouCard(p){
  const sideTag = p.side === '大'
    ? '<span class="tag up" style="font-size:15px">大球</span>'
    : '<span class="tag down" style="font-size:15px">細球</span>';
  const sideBig = p.side === '大'
    ? '<b class="r-up" style="font-size:22px">大</b>'
    : '<b class="r-down" style="font-size:22px">細</b>';
  const stBadge = p.settle === '贏' ? '<b class="r-up">✅贏</b>'
    : p.settle === '輸' ? '<b class="r-down">❌輸</b>'
    : p.settle === '走' ? '<b>➖走</b>' : '';
  const line2 = v => (v == null ? '—' : Number(v).toFixed(2));
  return `<div class="fcard" data-mid="${p.match_id}">
    <div class="f-top">
      <div class="f-head"><span class="ko">${esc((p.kickoff || '').slice(5, 16))}</span>
        <span class="lg">${esc(p.league || '')}</span>
        ${sideTag}${_ouTierBadge(p.tier)}
        <button class="btn gold ft-share">⇗</button></div>
      <div class="teams">${esc(p.home)} <span class="vs">vs</span> ${esc(p.away)}${p.score ? ` <b>${esc(p.score)}</b>` : ''} ${stBadge}</div>
      <div class="gaprow"><span class="lab">方向</span>${sideBig}｜規則 ${esc(p.rule_id || '')}｜${esc(p.rule_text || '')}</div>
      <div class="gaprow"><span class="lab">統計</span>訓練 ${esc(p.train || '—')}｜WF ${esc(p.wf || '—')}</div>
      ${p.note ? `<div class="gaprow"><span class="lab">備註</span>${esc(p.note)}</div>` : ''}
      <div class="gaprow"><span class="lab">尾盤</span>線 ${line2(p.line)}｜大水 ${line2(p.over_odds)}｜細水 ${line2(p.under_odds)}</div>
      ${p.goals != null ? `<div class="gaprow"><span class="lab">入球</span>全場 ${p.goals} 球（尾線 ${line2(p.line)}：${p.goals > p.line ? '大' : p.goals < p.line ? '細' : '走'}）</div>` : ''}
    </div></div>`;
}
function _ouShareText(p){
  const L = [];
  L.push(`【大小規則】${p.league} ${(p.kickoff || '').slice(5, 16)}`);
  L.push(`${p.home} vs ${p.away}${p.score ? '（' + p.score + '）' : ''}`);
  L.push(`方向：${p.side}球｜規則 ${p.rule_id}（${p.rule_text}）｜tier=${p.tier}`);
  L.push(`尾盤：線 ${p.line != null ? Number(p.line).toFixed(2) : '—'}｜大水 ${p.over_odds != null ? Number(p.over_odds).toFixed(2) : '—'}｜細水 ${p.under_odds != null ? Number(p.under_odds).toFixed(2) : '—'}`);
  if (p.settle) L.push(`結果：${p.settle === '贏' ? '✅贏' : p.settle === '輸' ? '❌輸' : '➖走'}（全場 ${p.goals} 球）`);
  return L.join('\n');
}
async function loadOuRules(){
  const listEl = $('#ouList');
  listEl.innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget('/api/ou_rules'); }
  catch (e) { listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (d.error) { listEl.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  const s = d.stats || {};
  $('#ouCnt').textContent = s.upcoming ? `(${s.upcoming})` : '';
  $('#ouStats').innerHTML = `<div class="statbar">
    <span>未開賽出手 <b>${s.upcoming || 0}</b></span><span>近7日已評 <b>${s.played || 0}</b></span>
    <span>贏 <b class="r-up">${s.wins || 0}</b></span><span>走 <b>${s.pushes || 0}</b></span>
    <span>輸 <b class="r-down">${s.losses || 0}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>
    <div class="note">出手邏輯：命中規則同方向先出，方向衝突或無規則＝觀望唔出。已完場場次按尾線自動結算（贏／走／輸），近 7 日保留。規則卡更新：${esc(d.rules_updated || '—')}。</div>`;
  const scan = d.scan || {};
  $('#ouScanInfo').textContent = scan.running
    ? '掃描中…'
    : (scan.last ? `上次掃描：${scan.last}｜出手 ${s.upcoming || 0}` : '');
  let h = '';
  if ((d.upcoming || []).length) h += `<div class="day-h">🔜 未開賽（規則命中・${d.upcoming.length} 場・順開賽時間）</div>` + d.upcoming.map(_ouCard).join('');
  if ((d.played || []).length) h += `<div class="day-h">📼 近 7 日已完場（${d.played.length} 場・自動結算）</div>` + d.played.map(_ouCard).join('');
  if (!h) h = '<div class="note">暫無場次命中大小規則。規則命中先會出現喺呢度——撳「🔍 重新掃描大小規則」即刻重評未來48小時＋近7日場次。</div>';
  listEl.innerHTML = h;
  [...listEl.querySelectorAll('.fcard')].forEach(c => {
    const mid = +c.dataset.mid;
    c.querySelector('.ft-share').onclick = async ev => {
      ev.stopPropagation();
      shareText(_ouShareText([...(d.upcoming || []), ...(d.played || [])].find(x => x.match_id === mid)));
    };
    c.querySelector('.f-top').onclick = ev => {
      if (!ev.target.closest('.btn')) openDetail(mid);
    };
  });
}
$('#btnOuScan').onclick = async function(){
  this.disabled = true;
  try {
    await jpost('/api/ou_rules/scan', {});
    pollScanJob('ou', '/api/ou_rules/scan-status',
                '#btnOuScan', '#ouScanInfo', () => loadOuRules(),
                x => `完成${x.error ? '｜錯誤：' + x.error : ''}`);
  } catch (e) {
    toast('掃描失敗：' + e.message);
    this.disabled = false;
  }
};

/* ---------- 半全場方法（分聯賽 HT/FT 方法版：htft_rules.json 方法命中先出手） ---------- */
function _htftCard(p){
  const stBadge = p.settle === '贏' ? '<b class="r-up">✅贏</b>'
    : p.settle === '輸' ? '<b class="r-down">❌輸</b>' : '';
  const winsRow = Object.entries(p.wins || {}).map(([k, v]) =>
    `${esc(k)} ${v == null ? '—' : pct(v)}`).join('｜');
  const scoreTxt = p.home_score != null
    ? ` <b>${p.home_score}-${p.away_score}</b>` : '';
  const htTxt = (p.half_home != null && p.home_score != null)
    ? `<div class="gaprow"><span class="lab">半場</span>半場 ${p.half_home}-${p.half_away}` +
      `　→　全場 ${p.home_score}-${p.away_score}　判定格 <b>${esc(p.cell || '')}</b>　${stBadge}</div>`
    : '';
  return `<div class="fcard" data-mid="${p.match_id}">
    <div class="f-top">
      <div class="f-head"><span class="ko">${esc((p.kickoff || '').slice(5, 16))}</span>
        <span class="lg">${esc(p.league || '')}</span>
        <button class="btn gold ft-share">⇗</button></div>
      <div class="teams">${esc(p.home)} <span class="vs">vs</span> ${esc(p.away)}${scoreTxt}</div>
      <div class="gaprow"><span class="lab">目標</span><b style="font-size:22px;color:var(--gold2)">${esc(p.target_name || '')}</b>　<span class="tag dim">${esc(p.target || '')}</span>${p.predicted ? '　<span class="tag" style="color:var(--gold2);border-color:#6b5510">👑 預計嗰項</span>' : ''}</div>
      <div class="gaprow"><span class="lab">方法</span>${esc(p.method_id || '')}｜${(p.conds || []).map(c => esc(c) + condHrTxt('htft', c, p.target)).join('、') || esc(p.cond_text || '')}</div>
      ${p.prob != null ? `<div class="gaprow"><span class="lab">機會率</span><b class="r-up" style="font-size:16px">${pct(p.prob)}</b>（驗證期 n=${p.prob_n || '—'}；同聯賽基率 ${pct(p.base)}）</div>` : ''}
      <div class="gaprow"><span class="lab">統計</span>訓練 ${esc(p.train || '—')}｜WF ${esc(p.wf || '—')}</div>
      ${winsRow ? `<div class="gaprow"><span class="lab">窗口</span>${winsRow}</div>` : ''}
      ${htTxt}
    </div></div>`;
}
function _htftShareText(p){
  const L = [];
  L.push(`【半全場方法】${p.league} ${(p.kickoff || '').slice(5, 16)}`);
  L.push(`${p.home} vs ${p.away}${p.home_score != null ? `（全場 ${p.home_score}-${p.away_score}）` : ''}`);
  L.push(`目標：${p.target_name}（${p.target}）${p.predicted ? '👑預計嗰項' : ''}｜方法 ${p.method_id}（${p.cond_text}）`);
  if (p.prob != null) L.push(`預計機會率：${pct(p.prob)}（驗證期 n=${p.prob_n || '—'}；基率 ${pct(p.base)}）`);
  L.push(`訓練 ${p.train || '—'}｜WF ${p.wf || '—'}`);
  if (p.settle) L.push(`結果：${p.settle === '贏' ? '✅贏' : '❌輸'}` +
    `（半場 ${p.half_home}-${p.half_away} → 全場 ${p.home_score}-${p.away_score}，格 ${p.cell}）`);
  return L.join('\n');
}
async function loadHtftRules(){
  const listEl = $('#htftmList');
  listEl.innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget('/api/htft_rules'); await loadCondHr(); }
  catch (e) { listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (d.error) { listEl.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  const s = d.stats || {};
  $('#htftmCnt').textContent = s.upcoming ? `(${s.upcoming})` : '';
  const DN = {HD: '主和', HA: '主客', AD: '客和', AH: '客主'};
  const distBar = d.dist && d.dist_total ? (() => {
    const t = d.dist_total, h = d.dist_htnd || 1;
    const cells = ['HD', 'HA', 'AD', 'AH'].map(k =>
      `<span style="${k === d.dist_best ? 'color:var(--gold2);font-weight:800' : ''}">${DN[k]} <b>${pct(d.dist[k] / t)}</b><small style="color:var(--dim)">（半場非和計 ${pct(d.dist[k] / h)}）</small></span>`).join('｜');
    return `<div class="statbar" style="margin-top:4px">📊 四項分佈（全庫 ${t.toLocaleString()} 場・半場和局已剔走）：${cells}　👉 佔比最高：<b style="color:var(--gold2)">${DN[d.dist_best]}</b></div>`;
  })() : '';
  $('#htftmStats').innerHTML = `<div class="statbar">
    <span>未開賽出手 <b>${s.upcoming || 0}</b></span><span>近7日已評 <b>${s.played || 0}</b></span>
    <span>贏 <b class="r-up">${s.wins || 0}</b></span><span>輸 <b class="r-down">${s.losses || 0}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>${distBar}
    <div class="note">四項＝主和／主客／客和／客主（半場有領先、全場冇贏到）；半場和局完全唔計。同一場多方法命中：👑＝預計機會率最高嗰項；機會率＝驗證期（2025+）命中率，已過訓練期（≤2024）＋驗證期雙重驗證，兩段都要 ≥2× 同聯賽基率先入庫。已完場按判定格自動結算，近 7 日保留。方法卡更新：${esc(d.rules_updated || '—')}。</div>`;
  /* 2026-10-21：四項目標細分（主和/主客/客和/客主）：幾中幾＋% */
  const _bt = d.by_target || {};
  const _btK = Object.keys(_bt);
  if (_btK.length){
    $('#htftmStats').insertAdjacentHTML('beforeend', '<div class="statbar" style="margin-top:4px">🎯 目標細分：' +
      _btK.map(k => { const e = _bt[k], dec = e.w + e.l, r = dec ? e.w/dec : null;
        return `<span>${esc(e.name)} <b>${e.w}中${dec}</b>${r==null?'':` ${(r*100).toFixed(1)}%`}<small style="color:var(--dim)"> n=${e.n}</small></span>`;
      }).join('｜') + '</div>');
  }
  const scan = d.scan || {};
  $('#htftmScanInfo').textContent = scan.running
    ? '掃描中…'
    : (scan.last ? `上次掃描：${scan.last}｜出手 ${s.upcoming || 0}` : '');
  let h = '';
  if ((d.upcoming || []).length) h += `<div class="day-h">🔜 未開賽（方法命中・${d.upcoming.length} 場・順開賽時間）</div>` + d.upcoming.map(_htftCard).join('');
  if ((d.played || []).length) h += `<div class="day-h">📼 近 7 日已完場（${d.played.length} 場・自動結算）</div>` + d.played.map(_htftCard).join('');
  if (!h) h = '<div class="note">暫無場次命中半全場方法。方法命中先會出現喺呢度——撳「🔍 重新掃描半全場方法」即刻重評未來48小時＋近7日場次。</div>';
  listEl.innerHTML = h;
  [...listEl.querySelectorAll('.fcard')].forEach(c => {
    const mid = +c.dataset.mid;
    c.querySelector('.ft-share').onclick = async ev => {
      ev.stopPropagation();
      shareText(_htftShareText([...(d.upcoming || []), ...(d.played || [])].find(x => x.match_id === mid)));
    };
    c.querySelector('.f-top').onclick = ev => {
      if (!ev.target.closest('.btn')) openDetail(mid);
    };
  });
}
$('#btnHtftmScan').onclick = async function(){
  this.disabled = true;
  try {
    await jpost('/api/htft_rules/scan', {});
    pollScanJob('htftm', '/api/htft_rules/scan-status',
                '#btnHtftmScan', '#htftmScanInfo', () => loadHtftRules(),
                x => `完成${x.error ? '｜錯誤：' + x.error : ''}`);
  } catch (e) {
    toast('掃描失敗：' + e.message);
    this.disabled = false;
  }
};

/* ---------- 📊 預測結果統計（完場場次逐來源結算一覽：方向＋贏/輸/走） ---------- */
const PSTAT_SRC = [['main','上下盤'],['v2','精選V2'],['v1','V1'],['v3','精選W'],
                   ['f7','精選7'],['f8','精選8'],['f12','精選12'],
                   ['user','我的選擇'],['ou','好波大小']];
const PSTAT_CELL = {HH:'主主',HD:'主和',HA:'主客',DH:'和主',DD:'和和',DA:'和客',
                    AH:'客主',AD:'客和',AA:'客客'};
let _pstatDays = 30, _pstatLg = '';
function _pstatCell(s){
  if (!s) return '—';
  const mark = s.verdict === '贏' ? ' <b class="r-up">✓</b>'
    : s.verdict === '輸' ? ' <b class="r-down">✗</b>'
    : s.verdict === '走' ? ' <span class="hint">走</span>' : '';
  let t = esc(s.side || '');
  if (s.pct != null) t += ` ${s.pct}%${s.den != null ? `(${s.den})` : ''}`;
  return t ? t + mark : '—';
}
async function loadPredStats(days, lg){
  if (days != null) _pstatDays = days;
  if (lg !== undefined) _pstatLg = lg;
  const aggEl = $('#pstatAgg'), bodyEl = $('#pstatBody');
  bodyEl.innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget(`/api/predstats?days=${_pstatDays}&lg=${encodeURIComponent(_pstatLg)}`); }
  catch (e) { bodyEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (d.error) { bodyEl.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  const sel = $('#pstatLg'), cur = _pstatLg;
  sel.innerHTML = '<option value="">全部</option>' +
    (d.leagues || []).map(x =>
      `<option value="${esc(x)}"${x === cur ? ' selected' : ''}>${esc(x)}</option>`).join('');
  sel.value = cur;
  $$('#pg-pstat [data-ps-days]').forEach(b =>
    b.classList.toggle('accent', +b.dataset.psDays === _pstatDays));
  $('#pstatCnt').textContent = d.matches && d.matches.length ? `(${d.matches.length})` : '';
  const a = d.agg || {};
  aggEl.innerHTML = '<div class="statbar">' + PSTAT_SRC.map(([k, nm]) => {
    const s = a[k] || {};
    const v = s.n
      ? `${pct(s.rate)}<small style="color:var(--dim)">（${s.n}場・贏${s.hit} 輸${s.miss} 走${s.push}）</small>`
      : '<span class="hint">—</span>';
    return `<span>${nm} <b>${v}</b></span>`;
  }).join('') + `<span class="hint">更新：${esc(d.updated || '—')}</span></div>` +
  '<div class="note">結算口徑：讓球來源對 company12 尾盤＋全場比分（上盤＝讓球方）；我的選擇對揀嗰刻嘅 pick 線；半場領先最終唔贏（主和/主客/客和/客主）黃底。命中率＝贏÷(贏+輸)。</div>';
  if (!d.matches || !d.matches.length){
    bodyEl.innerHTML = '<div class="note">呢個時段／聯賽篩選冇完場場次。</div>';
    return;
  }
  let h = '<div class="checkwrap"><table class="ck-table"><thead><tr>' +
    '<th>時間</th><th>聯賽</th><th>主 vs 客</th><th>半場→全場</th>' +
    PSTAT_SRC.map(([, nm]) => `<th>${nm}</th>`).join('') +
    '</tr></thead><tbody>';
  for (const m of d.matches){
    h += '<tr><td>' + esc((m.kickoff || '').slice(5, 16)) + '</td>' +
      `<td>${esc(m.league || '')}</td>` +
      `<td style="text-align:left;white-space:nowrap">${esc(m.home)} <span style="color:var(--dim)">vs</span> ${esc(m.away)}</td>`;
    const htTxt = m.hh != null
      ? `${m.hh}-${m.ha}→${m.hs}-${m.aws} ${PSTAT_CELL[m.cell] || esc(m.cell || '')}`
      : `—→${m.hs}-${m.aws}`;
    h += `<td${m.xh_xa ? ' class="hl"' : ''}>${htTxt}</td>`;
    for (const [k] of PSTAT_SRC)
      h += `<td>${_pstatCell((m.sources || {})[k])}</td>`;
    h += '</tr>';
  }
  bodyEl.innerHTML = h + '</tbody></table></div>';
}
$('#pg-pstat').addEventListener('click', e => {
  const b = e.target.closest('[data-ps-days]');
  if (b) loadPredStats(+b.dataset.psDays);
});
$('#pstatLg').addEventListener('change', () => loadPredStats(null, $('#pstatLg').value));

/* ---------- 主頁場次 <select> 共用填充：未開賽全部＋過去24小時已開賽（最近排先） ---------- */
function fillCheckSelect(sel, firstLabel){
  const cur = sel.value;
  const now = homeData.now || '';
  const cut = now ? _hktMinusHours(now, 24) : '';
  const recent = (homeData.played || [])
    .filter(m => !cut || (m.kickoff || '') >= cut)
    .slice()
    .sort((a, b) => (b.kickoff || '').localeCompare(a.kickoff || ''));
  sel.innerHTML = `<option value="">${firstLabel}</option>` +
    homeData.upcoming.map(m => `<option value="${m.id}">${esc((m.kickoff || '').slice(5, 16))} ${esc(m.home)} vs ${esc(m.away)}</option>`).join('') +
    (recent.length ? '<option disabled>──── 已開賽（過去24小時・最近排先）────</option>' : '') +
    recent.map(m => `<option value="${m.id}">🔴 ${esc((m.kickoff || '').slice(5, 16))} ${esc(m.home)} vs ${esc(m.away)}${m.score ? '（' + esc(m.score) + '）' : ''}</option>`).join('');
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
}
/* ---------- Check 一下 7／8／12（離線 8 情境回測表＋單場檢驗） ---------- */
async function runFwCheckOne(g, mid){
  const box = $('#fwck' + g + 'One');
  if (!mid) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="note">檢驗緊…</div>';
  let d;
  try { d = await jget(`/api/fwcheck/target?g=${g}&id=${mid}`); }
  catch (e) { box.innerHTML = '<div class="err">檢驗失敗：' + esc(String(e)) + '</div>'; return; }
  if (d.error) { box.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  const r = d.report || {};
  // 設定上下盤隊名（Check7/8/12 表格同方向標籤用）
  const tm = homeData.upcoming.find(m => m.id === +mid)
    || (homeData.played || []).find(m => m.id === +mid) || {};
  setV3UD({home: tm.home, away: tm.away,
           close: (tm.line && tm.line.line) ? {g: tm.line.line.indexOf('主讓') === 0 ? 'home' : tm.line.line.indexOf('客讓') === 0 ? 'away' : 'none'} : null});
  const sc = r.scenario || {};
  const st = d.scenario_stats;
  const dTxt = {deep: '深咗', shallow: '淺咗', same: '不變'};
  const gridCells = (r.grid_cells || []);
  const cellsHtml = _fwgCellsHTML(r.cells, gridCells);
  let scHtml;
  if (!r.pass) {
    scHtml = `<div class="note">⛔ 唔合格：${esc(r.fail || '格方向未一致')}（未入選精選${g}，以下情境只作參考）</div>`;
  }
  if (sc && sc.key && st) {
    const winDir = (st.up_r || 0) >= (st.down_r || 0) ? '上' : '下';
    const winR = Math.max(st.up_r || 0, st.down_r || 0);
    scHtml = `<div class="statbar"><span>情境：<b>格指${sc.dir === 'up' ? '上' : '下'}</b></span>` +
      `<span>現時盤 vs 最多盤口：<b>${dTxt[sc.mode_gap] || '—'}</b></span>` +
      `<span>vs 最接近50%盤口：<b>${dTxt[sc.d50_gap] || '—'}</b></span></div>` +
      `<div class="note">📊 呢個情境歷史開出：<b class="${winR >= 0.55 ? 'r-up' : ''}">${winDir} ${pct(winR)}</b>（${st.n} 場基數・走 ${st.push}）` +
      `${r.pass ? ' ✅已入選精選' + g : ''}</div>`;
  } else if (r.pass) {
    scHtml = `<div class="note">✅ 合格入選精選${g}（方向：${r.direction === 'up' ? '上盤' : '下盤'}（${esc(_dirTeam(r.direction))}））；但呢場歸唔到 8 個情境（其中一個參照盤同今場相同／數據不足）。</div>`;
  }
  box.innerHTML = `<div class="fcard"><div class="f-top">` +
    `<div class="teams">格組合檢驗：${gridCells.map(esc).join('、')}</div>` +
    _udLegend() +
    `<div class="grid6">${cellsHtml}</div>${scHtml || ''}` +
    _fwgLegendHTML(gridCells) +
    (r.lb ? _lb4HTML(r.lb, -1, '相同情況回查',
      `上述格（有數據嘅：${(r.lb.cells_used || gridCells).join('、')}）同時成立嘅歷史場次・四個口徑（綜合＋各水位區域）`) : '') +
    (r.strict ? `<div class="gaprow"><span class="lab">嚴格回查</span><span class="note">格組合＋相同尾盤盤口＋29/31/33/35 條件嘅實際開出</span></div>${_fwgStrict(r.strict)}` : '') +
    `<div class="hrb" data-kind="fw${g}" data-mid="${mid}"></div></div></div>`;
  _fillHrBlocks(box);
}
async function loadFwCheck(g){
  const body = $('#fwck' + g + 'Body');
  body.innerHTML = '<div class="note">載入中…</div>';
  fillCheckSelect($('#fwck' + g + 'Match'), '— 揀場次檢驗 —');
  $('#fwck' + g + 'Match').onchange = () => runFwCheckOne(g, $('#fwck' + g + 'Match').value || null);
  if (!$('#fwck' + g + 'Match').value && homeData.upcoming.length) {
    $('#fwck' + g + 'Match').value = String(homeData.upcoming[0].id);
  }
  runFwCheckOne(g, $('#fwck' + g + 'Match').value || null);
  let d;
  try { d = await jget('/api/fwcheck/full?g=' + g); }
  catch (e) { body.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (d.error) { body.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  $('#fwck' + g + 'Info').textContent =
    `合格 ${d.qualified} 場／全庫 ${d.M} 場｜不變剔除 ${d.excluded_same_line} 場｜計算於 ${d.computed_at}`;
  const dTxt = {deep: '深', shallow: '淺'};
  const dirTxt = {up: '上', down: '下'};
  const tot = (d.scenarios || []).reduce((a, s) => ({
    up: a.up + (s.up || 0), down: a.down + (s.down || 0),
    push: a.push + (s.push || 0), n: a.n + (s.n || 0)}), {up: 0, down: 0, push: 0, n: 0});
  const eff = tot.n - tot.push;
  const hrLine = eff > 0
    ? `<div class="statbar"><span>8 情境合計實際開出：<b class="r-up">上 ${pct(tot.up / eff)}</b></span>` +
      `<span><b class="r-down">下 ${pct(tot.down / eff)}</b></span>` +
      `<span>${tot.n} 場基數（走 ${tot.push} 唔計分母）</span></div>`
    : '';
  const rows = d.scenarios.filter(s => s.n > 0).map(s => {
    const win = s.up_r >= s.down_r ? s.up_r : s.down_r;
    const winDir = s.up_r >= s.down_r ? '上' : '下';
    return `<tr><td>格指${dirTxt[s.dir]}</td>` +
      `<td>現時盤 vs 最多盤口：${dTxt[s.mode_gap]}咗</td>` +
      `<td>現時盤 vs 最接近50%盤口：${dTxt[s.d50_gap]}咗</td>` +
      `<td class="${win >= 0.55 ? 'r-up' : ''}">${winDir} ${pct(win)}</td>` +
      `<td>${s.n} 場</td><td>走 ${s.push}</td></tr>`;
  }).join('');
  body.innerHTML = `<div class="note">情境＝格方向 × 現時盤對「分佈最多盤口」深淺 × 現時盤對「上盤勝率最接近50%盤口」深淺（參照盤＝上賽完全相同樣本，項目30語義）。以下係歷史合格場嘅實際開出比例（＝命中率回查）。</div>` +
    hrLine +
    `<table class="ck-table"><thead><tr><th>格方向</th><th>最多盤口</th><th>最接近50%盤口</th><th>開出比例</th><th>場數</th><th>走盤</th></tr></thead><tbody>${rows || '<tr><td colspan="6">暫無數據</td></tr>'}</tbody></table>`;
}

/* ---------- Check 下先 ---------- */
// 香港時間字串 'YYYY-MM-DD HH:MM:SS' 減 n 小時（自己足位運算，唔靠 client 時區）
function _hktMinusHours(hkt, n){
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(hkt || '');
  if (!m) return '';
  let Y = +m[1], Mo = +m[2], D = +m[3], H = +m[4], Mi = +m[5];
  H -= n;
  while (H < 0) { H += 24; D -= 1; }
  // 簡單向前推日（每月日數表，閏年2月29日）
  if (D < 1) {
    Mo -= 1;
    if (Mo < 1) { Mo = 12; Y -= 1; }
    const dim = [31, (Y % 4 === 0 && (Y % 100 !== 0 || Y % 400 === 0)) ? 29 : 28,
                 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][Mo - 1];
    D += dim;
  }
  const p = x => String(x).padStart(2, '0');
  return `${Y}-${p(Mo)}-${p(D)} ${p(H)}:${p(Mi)}`;
}
$('#ckMatch').onchange = () => runCheck($('#ckMatch').value || null);
async function loadCheckPage(){
  fillCheckSelect($('#ckMatch'), '— 揀場次 —');
  if (!$('#ckMatch').value && homeData.upcoming.length) {
    $('#ckMatch').value = String(homeData.upcoming[0].id);
  }
  runCheck($('#ckMatch').value || null);
}
async function runCheck(mid){
  const body = $('#ckBody');
  if (!mid) { body.innerHTML = '<div class="note">揀一場賽事（未開賽或已完場都得），即刻檢查六條件方向＋深淺分析；完場場次另有賽果總結＋45 項詳細逐項睇。</div>'; return; }
  body.innerHTML = '<div class="note">計算中…（首次約 1-3 秒）</div>';
  let s;
  try { s = await jget('/api/v3/summary?id=' + mid); }
  catch (e) { body.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (s.error) { body.innerHTML = `<div class="err">${esc(s.error)}</div>`; return; }
  const t = homeData.upcoming.find(m => m.id === +mid)
    || (homeData.played || []).find(m => m.id === +mid) || {};
  setV3UD({home: t.home, away: t.away, close: (t.line && t.line.line) ? {g: t.line.line.indexOf('主讓') === 0 ? 'home' : t.line.line.indexOf('客讓') === 0 ? 'away' : 'none'} : null});
  const fw = s.featured_w || {};
  const dirTxt = fw.pass ? (fw.direction === 'up' ? '上盤' : '下盤') : null;
  const dirName = x => x === 'up' ? '上盤' : '下盤';
  const curZ2 = _zone12Idx(t.line && t.line.line ? (t.line.line.indexOf('客讓') === 0 ? t.line.ao : t.line.ho) : null);
  let anyHtml = '';
  if (!fw.pass) {
    const a5 = (fw.any5 || []).map(x =>
      `<div>⚡ Any 5 → <b class="${x.dir === 'up' ? 'r-up' : 'r-down'}">${dirName(x.dir)}(${esc(_dirTeam(x.dir))})</b>（${esc(x.which.join('、'))}）<div class="sub">${_anyDetail(x, fw.conds)}</div></div>`).join('');
    const a4 = (fw.any4 || []).map(x =>
      `<div>🔸 Any 4 → <b class="${x.dir === 'up' ? 'r-up' : 'r-down'}">${dirName(x.dir)}(${esc(_dirTeam(x.dir))})</b>（${esc(x.which.join('、'))}）<div class="sub">${_anyDetail(x, fw.conds)}</div></div>`).join('');
    anyHtml = (a5 || a4)
      ? `<div class="gaprow"><span class="lab">近合格</span>${a5}${a4}</div>` +
        _anyLbHTML(fw.any_lb, curZ2) : '';
  }
  let h = `<div class="tcard"><h2 style="font-size:16px;color:var(--gold2)">✓ Check 下先（同精選W 六條件＋淺深分析）</h2>
    ${_udLegend()}
    <div style="font-size:15px;margin-bottom:6px">方向：${dirTxt ? `<b class="r-up">✅ 六條件一致 → ${dirTxt}(${esc(_dirTeam(fw.direction))})</b>` : `<span style="color:var(--dim)">❌ 未一致${fw.fail_note ? '（' + esc(fw.fail_note) + '）' : ''}</span>`}</div>
    ${anyHtml}
    <div class="grid6">${(fw.conds || []).map((c, i) => `<div class="gi"><div class="t">條件${i + 1}</div><div class="v">${_fwCondCell(c)}<div class="sub">${esc(c.note || '')}</div></div></div>`).join('')}</div>
    ${_fwLegendHTML()}
    ${fw.lookback ? _fwLookbackHTML(fw.lookback, curZ2) : ''}
    <div class="gaprow"><span class="lab">30 淺深</span>${_fwGapTxt(fw.gap30, curZ2)}</div>
    <div class="note">Check 下先＝對任一場即將開賽賽事，即場重算精選W 六條件（1A／1I／19同主客／19+互換／31／35）：每條件要全庫該方向≥50% 先有方向；六條方向一致即話你知邊邊係目前數據偏向。之後用 29 樣本（上賽完全相同）做淺深分析：今場尾盤 對 分佈最多盤口 及 上盤勝率最接近50%盤口，深咗／淺咗／一樣，連全部水位區嘅勝率場次。</div>
    <div class="hrb" data-kind="fwx" data-mid="${mid}"></div>
  </div>`;
  // 完場賽事：賽果總結（邊邊贏＋六條件方向有冇命中）
  if (s.result) {
    const r = s.result.r;
    const rTxt = r === 'A' ? '<b class="r-up">上盤贏</b>'
      : r === 'B' ? '<b class="r-down">下盤贏</b>'
      : r === 'P' ? '<b>➖ 走盤</b>'
      : '<span style="color:var(--dim)">冇尾盤紀錄，未能結算</span>';
    let hitTxt = '';
    if (fw.pass && r === 'P') {
      hitTxt = `｜六條件方向（${dirTxt}）<b>➖ 走盤</b>`;
    } else if (fw.pass && r) {
      const hit = (r === 'A') === (fw.direction === 'up');
      hitTxt = `｜六條件方向（${dirTxt}）<b class="${hit ? 'r-up' : 'r-down'}">${hit ? '✅ 命中' : '❌ 未中'}</b>`;
    }
    h += `<div class="tcard"><h2 style="font-size:16px;color:var(--gold2)">🏁 賽果總結</h2>
    <div style="font-size:15px">最終比分 <b>${esc(s.result.score)}</b>｜${rTxt}${hitTxt}</div>
    <div class="note">結算以尾盤盤口計（同「我的選擇」規則）：讓球方淨勝球減讓球數，>0 上盤贏、<0 下盤贏、=0 走盤。</div>
  </div>`;
  }
  h += `<div class="tcard"><h2 style="font-size:16px;color:var(--gold2)">📋 詳細項目（45 項＋組合篩查，逐項撳開睇）</h2>
    <div class="note">每項即場重算，同詳情頁一模一樣：樣本結果、16 格、盤口分佈、12 段水位表。「不適用」＝該場冇對應數據（例如冇對賽紀錄）。完場場次照計——可以逐項對照實際賽果睇邊項準。</div>
    <div id="ckItems"></div>
  </div>`;
  body.innerHTML = h;
  buildV3ItemsAccordion(+mid, $('#ckItems'), s.applicability);
  _fillHrBlocks(body);
}

/* ---------- 事後回查（精選W 賽果＋統計） ---------- */
async function loadReview(){
  $('#rvList').innerHTML = '<div class="note">⏳ 載入緊：事後回查…</div>';
  let d;
  try { d = await jget('/api/v3/featured/full'); }
  catch (e) { $('#rvList').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats;
  $('#rvStats').innerHTML = `<div class="statbar">
    <span>統計報告　場數 <b>${s.total}</b></span>
    <span>未開賽 <b>${s.pending}</b></span><span>進行中 <b>${s.live}</b></span>
    <span>已完場 <b>${s.played}</b></span>
    <span>命中場數 <b class="r-up">${s.wins}</b></span>
    <span>未中 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>
    <div class="note">事後回查＝所有精選W 入選場次嘅盤口勝負（以入選方向結算：尾盤盤口計 贏／輸／走）。已完場順日期排，最新排先，永久保留。</div>`;
  const items = [...d.live, ...d.played];
  let h = '';
  let lastDay = null;
  for (const p of items) {
    const day = (p.kickoff || '').slice(0, 10);
    if (day !== lastDay) { h += `<div class="day-h" style="background:#141a24;color:var(--dim);border-left-color:var(--accent)">📅 ${esc(day)}</div>`; lastDay = day; }
    const res = p.result === 'W' ? '<b class="r-up">✅ 命中</b>'
      : p.result === 'L' ? '<b class="r-down">❌ 未中</b>'
      : p.result === 'P' ? '<b>➖ 走盤</b>'
      : p.state === 'live' ? '<span class="tag live">進行中</span>'
      : '<span style="color:var(--dim)">待結算</span>';
    const ud = _fwUD(p);
    h += `<div class="mrow" data-mid="${p.id}">
      <span class="ko">${esc((p.kickoff || '').slice(5, 16))}</span>
      <span class="lg">${esc(p.league)}</span>
      <span class="tm">${esc(p.home)} <span class="r">vs</span> ${esc(p.away)}　<span class="r">上盤＝${esc(ud.up)}｜下盤＝${esc(ud.down)}</span></span>
      ${p.score ? `<span class="sc">${esc(p.score)}</span>` : ''}
      <span class="tag ${p.direction}">${p.direction === 'up' ? '上盤' : '下盤'}</span>
      <span>${res}</span>
      <span class="ln">${esc(p.line || '')}<br>${esc(p.odds || '')}</span></div>`;
  }
  if (!h) h = '<div class="note">暫無已開賽嘅精選W場次。</div>';
  $('#rvList').innerHTML = h;
  $$('#rvList .mrow').forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
}

/* ---------- 過往紀錄（V3 精選W 自動紀錄） ---------- */
async function loadFeatlog(){
  $('#flList').innerHTML = '<div class="note">⏳ 載入緊：過往紀錄…</div>';
  let d;
  try { d = await jget('/api/v3/featlog'); }
  catch (e) { $('#flList').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats;
  $('#flStats').innerHTML = `<div class="statbar">
    <span>總紀錄 <b>${s.total}</b></span><span>未開賽 <b>${s.pending}</b></span>
    <span>已完場 <b>${s.played}</b></span>
    <span>贏 <b class="r-up">${s.wins}</b></span><span>輸 <b class="r-down">${s.losses}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b></span></div>`;
  const card = p => {
    const res = !p.played ? '<span style="color:var(--dim)">未開賽</span>'
      : p.result === 'W' ? '<b class="r-up">✅ 命中</b>'
      : p.result === 'L' ? '<b class="r-down">❌ 未中</b>'
      : p.result === 'P' ? '<b>➖ 走</b>' : '<span style="color:var(--dim)">待結算</span>';
    const ud = _fwUD(p);
    return `<div class="mrow" data-mid="${p.id}">
      <span class="ko">${esc((p.kickoff || '').slice(5, 16))}</span>
      <span class="lg">${esc(p.league)}</span>
      <span class="tm">${esc(p.home)} <span class="r">vs</span> ${esc(p.away)}　<span class="r">上盤＝${esc(ud.up)}｜下盤＝${esc(ud.down)}</span></span>
      ${p.score ? `<span class="sc">${esc(p.score)}</span>` : ''}
      <span class="tag ${p.direction}">${p.direction === 'up' ? '上盤' : '下盤'}</span>
      <span>${res}</span>
      <span class="ln">${esc(p.line || '')}<br>${esc(p.odds || '')}<br><span style="color:var(--dim)">紀錄 ${esc((p.added_at || '').slice(5, 16))}</span></span></div>`;
  };
  let h = '';
  if (d.pending.length) h += `<div class="day-h">🔜 未開賽（${d.pending.length}）</div>` + d.pending.map(card).join('');
  if (d.played.length) h += `<div class="day-h">📼 已完場（${d.played.length}・最新排先）</div>` + d.played.map(card).join('');
  if (!h) h = '<div class="note">暫無紀錄。精選W場次一出現會自動紀錄喺度（尾盤快照，計一場）。</div>';
  $('#flList').innerHTML = h;
  $$('#flList .mrow').forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
}

/* ---------- 舊版本（V1 版面・V1 規則號同 V3 完全隔離） ---------- */
const V1_MAP_NOTE = `<div class="note">「舊版本」保留 V1 嘅精選及精選Z（V1 規則號①⑤⑧⑫⑮⑱⑭⑰同 V3 項目號係兩套唔同嘅編號，唔可以混）。
而家用嘅入選引擎係 V2 十六字頭規則逆向重塑（用 V1 舊項目名展示），對應關係：
① 同初盤及尾盤＝V2 項目1（gate①用項目2 四小時版）｜
⑤ 對上對賽尾盤＝V2 項目13・純盤口（gate②）；⑤Z 同主客＝字頭 I–P 同主隊範圍｜
⑧ 主客入失球±3＝V2 項目25／26｜
⑫ 主場客場排名±2＝V2 項目27｜
⑮ 排名±2＋同尾盤＝V2 項目27＋同尾盤條件｜
⑱ 排名差距±1＋同尾盤＝V2 項目32｜
⑭ 差距＝V2 項目30 分佈最多盤／項目33 最接近50%盤（gate③④）｜
⑰ 差距＝V2 項目30／33・上次比賽樣本版｜
✓ Check＝V2 項目31 今賽比上賽深/淺/不變＋組合統計。</div>`;

function _ocTxt2(o){ return o ? `上${pct(o.up_r)} 下${pct(o.down_r)} 走${pct(o.push_r)}｜${o.n}場` : '—'; }
function _pairTxt(b){ return b ? `全庫 ${_ocTxt2(b.all)}${b.league ? `<br><span style="opacity:.75">同聯賽 ${_ocTxt2(b.league)}</span>` : ''}` : '不適用'; }
function _scopeGap(sg){
  if (!sg) return '<span style="color:var(--dim)">—</span>';
  let h = '';
  if (sg.mode) h += `最多 <b>${esc(sg.mode.line || '')}</b>｜${sg.mode.n} 場｜上 ${pct(sg.mode.up_r)}／下 ${pct(sg.mode.down_r)}` +
    (sg.mode.gap ? `<br><span style="color:#ffd766">${esc(sg.mode.gap)}</span>` : '');
  if (sg.d50) h += (h ? '<br>' : '') + `50% <b>${esc(sg.d50.line || '')}</b>｜${sg.d50.n} 場｜上 ${pct(sg.d50.up_r)}／下 ${pct(sg.d50.down_r)}` +
    (sg.d50.gap ? `<br><span style="color:#ffd766">${esc(sg.d50.gap)}</span>` : '');
  return h || '<span style="color:var(--dim)">—</span>';
}
function _gapTxt(g){
  if (!g || g.error) return '<span style="color:var(--dim)">不適用</span>';
  return `<span style="color:var(--dim)">全庫</span> ${_scopeGap(g.all)}<br>` +
         `<span style="color:var(--dim)">同聯賽</span> ${_scopeGap(g.lg)}`;
}
function _stateZh(s){ return s === 'deep' ? '上盤深咗' : s === 'shallow' ? '上盤淺咗' : '不變'; }
function _upDownOf(line, home, away){
  if (!line) return null;
  if (line.indexOf('主讓') === 0) return {up: home, down: away, push: false};
  if (line.indexOf('客讓') === 0) return {up: away, down: home, push: false};
  return {up: home, down: away, push: true};
}
function _udLegend2(line, home, away){
  const u = _upDownOf(line, home, away);
  if (!u) return '';
  return `<div class="note" style="margin:2px 0">本場：上盤＝<b>${esc(u.up)}</b>｜下盤＝<b>${esc(u.down)}</b>${u.push ? '（平手盤：上盤＝平手主隊）' : ''}</div>`;
}
function _v1Card(p, z, tag){
  const b = p.brief || {};
  const dName = p.direction === 'up' ? '上盤' : '下盤';
  const lgRateKey = tag === 'V2' ? 'feat_v2' : 'feat_v1';
  const res = !p.played ? '<span style="color:var(--dim)">未開賽</span>'
    : p.result === 'W' ? '<b class="r-up">✅ 命中</b>'
    : p.result === 'L' ? '<b class="r-down">❌ 未中</b>'
    : p.result === 'P' ? '<b>➖ 走盤</b>' : '<span style="color:var(--dim)">待結算</span>';
  const lt = p.letters || {};
  const lts = lt.letters || [];
  const i12 = b.i12, i15 = b.i15, i18 = b.i18;
  const i12v = !i12 ? '不適用' : `n=${i12.n}` + (i12.top ? `<br>最多【${esc(i12.top.line || '')}】上${pct(i12.top.up_r)} 下${pct(i12.top.down_r)}` : '');
  const i15v = !i15 ? '不適用' : `n=${i15.n}` + (i15.zone ? `<br>今場水位【${esc(i15.zone.zone || '')}】上${pct(i15.zone.up_r)} 下${pct(i15.zone.down_r)} 走${pct(i15.zone.push_r)}` : '');
  const i18v = !i18 ? '不適用' : `全庫 ${_ocTxt2(i18.all)}<br><span style="opacity:.75">同聯賽 ${_ocTxt2(i18.lg)}</span>`;
  const chk = p.check;
  const chkTxt = !chk ? '<span style="color:var(--dim)">唔中 Check 任何一格</span>'
    : chk.error ? `<span style="color:var(--down)">Check 計算失敗：${esc(chk.error)}</span>`
    : `<b style="color:#ffd766">🎯 中咗 Check</b>：⑭${_stateZh(chk.g14)}＋⑰${_stateZh(chk.g17)} → 上 ${pct(chk.up_r)}／下 ${pct(chk.down_r)}<span style="color:var(--dim)">（${chk.n} 場）</span>`;
  return `<div class="fcard" data-mid="${p.id}">
    <div class="f-top">
      <span class="f-time">${esc((p.kickoff || '').slice(5, 16))}　${esc(p.league)}</span>
      <span class="f-teams">${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}</span>
      ${p.score ? `<span class="f-score">${esc(p.score)}${p.ht ? `<span style="color:var(--dim);font-weight:400">（半${esc(p.ht)}）</span>` : ''}${p.first_goal ? ` <span style="color:var(--gold2)">首球${p.first_goal === 'home' ? '主' : '客'}</span>` : ''}</span>` : (p.ht ? `<span class="f-score" style="color:var(--dim)">半${esc(p.ht)}</span>` : '')}
      <span class="tag ${p.direction}">${dName}</span>
      ${z ? '<span class="tag" style="background:#5b2ea6;color:#fff" title="Z 槽＝同主隊字頭組合（同正選字頭獨立篩選，命中統計獨立計）">ⓩ Z 槽</span>' : ''}
      ${p.hist_hit != null ? `<span class="tag" style="border-color:${p.hist_hit >= 80 ? '#2c5c3c' : '#6b5510'};color:${p.hist_hit >= 80 ? '#7ee2a0' : 'var(--gold2)'}" title="呢類訊號（${z ? '同主隊Z' : '正選'}字頭合格）嘅歷史實戰命中率">歷史 ${p.hist_hit}%<small style="opacity:.75">（n=${p.hist_n}）</small></span>` : ''}
      ${lts.length ? `<span class="tag lt">字頭 ${lts.join(' ')}</span>` : ''}
      <span>${res}</span>
      <span class="f-btns">
        <button class="btn ft-refresh">⟳ 重新整理</button>
        <button class="btn ft-share">⇗ 分享</button>${BetSlip.btn(p)}
      </span>
    </div>
    <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
    ${p.line_moved ? `<div class="gaprow" style="border:1px solid #d8a03c;background:rgba(216,160,60,.12);border-radius:8px;padding:6px 8px"><span class="lab">⚠️ 尾盤已變</span>入選/檢定時（${esc((p.snap_at || '').slice(5, 16) || '—')}）：<b>${esc(p.line_at_pick || '—')}</b> → 而家：<b>${esc(p.line || '—')} ${esc(p.odds || '')}</b>　條件已按新尾盤自動重檢</div>` : ''}
    <div class="gaprow"><span class="lab">同聯賽</span>${_lgRateSlot(p.league, [lgRateKey])}</div>
    ${_udLegend2(p.line, p.home, p.away)}
    <div class="grid6">
      <div class="gi"><div class="t">① 同初盤及尾盤</div><div class="v">${_pairTxt(b.i1)}</div><div class="sub">＝V2 項目1</div></div>
      <div class="gi"><div class="t">⑤${z ? 'Z' : ''} 對上對賽尾盤${z ? '（同主客）' : ''}</div><div class="v">${_pairTxt(z ? b.i5z : b.i5)}</div><div class="sub">＝V2 項目13${z ? '・字頭I–P同主隊' : ''}</div></div>
      <div class="gi"><div class="t">⑧ 主客入失球±3</div><div class="v">${_pairTxt(b.i8)}</div><div class="sub">＝V2 項目25/26</div></div>
      <div class="gi"><div class="t">⑫ 主場客場排名±2</div><div class="v">${i12v}</div><div class="sub">＝V2 項目27</div></div>
      <div class="gi"><div class="t">⑮ 排名±2＋同尾盤</div><div class="v">${i15v}</div><div class="sub">＝V2 項目27＋同尾盤</div></div>
      <div class="gi"><div class="t">⑱ 排名差距±1＋同尾盤</div><div class="v">${i18v}</div><div class="sub">＝V2 項目32</div></div>
    </div>
    <div class="gaprow"><span class="lab">⑭ 差距</span>${_gapTxt(b.g14)}<div class="sub" style="margin-top:2px">＝V2 項目30 最多盤／項目33 50%盤</div></div>
    <div class="gaprow"><span class="lab">⑰ 差距</span>${_gapTxt(b.g17)}<div class="sub" style="margin-top:2px">＝V2 項目30／33・上次比賽樣本版</div></div>
    <div class="gaprow"><span class="lab">✓ Check</span>${chkTxt}<div class="sub" style="margin-top:2px">＝V2 項目31 深淺不變＋組合</div></div>
  </div>`;
}
function _v1StatsBar(s, z, tag){
  return `<div class="statbar">
    <span>${tag || (z ? 'V1精選Z' : 'V1精選')}：未開賽 <b>${s.pending}</b></span><span>已完場 <b>${s.played}</b></span>
    <span>贏 <b class="r-up">${s.wins}</b></span><span>輸 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>`;
}
async function loadV1(){
  const listEl = $('#v1List'), zlistEl = $('#v1zList');
  const v2El = $('#v2List'), v2zEl = $('#v2zList');
  listEl.innerHTML = '<div class="note">⏳ 載入緊：V1/V2 字頭精選（Z 槽優先）…</div>';
  zlistEl.innerHTML = '<div class="note">⏳ 載入緊：V1Z…</div>';
  v2El.innerHTML = '<div class="note">⏳ 載入緊：V2…</div>';
  v2zEl.innerHTML = '<div class="note">⏳ 載入緊：V2Z…</div>';
  LoadX.show('載入舊版 V1/V2（＋Z 槽）');
  let d, dz;
  try {
    [d, dz] = await Promise.all([
      jget('/api/v1/featured/full'), jget('/api/v1/featured/full?z=1')]);
  } catch (e) {
    LoadX.hide();
    listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>';
    return;
  }
  const scan = d.scan || {};
  $('#v1ScanInfo').textContent = scan.running
    ? `掃描中 ${scan.done}/${scan.total}…`
    : (scan.last ? `上次掃描：${scan.last}｜新增 ${scan.added || 0}（Z ${scan.added_z || 0}）` : '');
  $('#v1Stats').innerHTML = _v1StatsBar(d.stats, false, 'V1精選') + V1_MAP_NOTE;
  $('#v1zStats').innerHTML = _v1StatsBar(dz.stats, true, 'V1精選Z');
  League7.mount('v1', $('#v1Stats'));
  League7.mount('v1z', $('#v1zStats'));
  const render = (dd, el, z, tag, refreshUrl) => {
    let h = '';
    if (dd.pending.length) h += `<div class="day-h">🔜 ${tag}${z ? '精選Z' : '精選'}・未開賽（${dd.pending.length} 場・順開賽時間）</div>` + dd.pending.map(p => _v1Card(p, z, tag)).join('');
    if (dd.played.length) h += `<div class="day-h">📼 ${tag}${z ? '精選Z' : '精選'}・已完場（${dd.played.length} 場・最新排先，永久保留）</div>` + dd.played.map(p => _v1Card(p, z, tag)).join('');
    if (!h) h = `<div class="note">暫無 ${tag}${z ? '精選Z' : '精選'}場次。撳「🔍 重新掃描」即刻篩過。</div>`;
    el.innerHTML = h;
    _fillLgSlots();
    [...el.querySelectorAll('.fcard')].forEach(c => {
      const mid = +c.dataset.mid;
      c.querySelector('.ft-refresh').onclick = async ev => {
        ev.stopPropagation();
        toast('重新整理中…');
        try {
          await jpost(refreshUrl, {id: mid});
          toast('完成'); loadV1();
        } catch (e) { toast('失敗：' + e.message); }
      };
      c.querySelector('.ft-share').onclick = async ev => {
        ev.stopPropagation();
        const p = [...dd.pending, ...dd.played].find(x => x.id === mid);
        if (p) shareText(v1ShareText(p, z));
      };
      c.onclick = () => openDetail(mid);
    });
  };
  render(d, listEl, false, 'V1', '/api/v1/featured/refresh');
  render(dz, zlistEl, true, 'V1', '/api/v1/featured/refresh');
  // V2（十六字頭）另外慢慢載——唔阻塞 V1 顯示
  Promise.all([jget('/api/featured/full'), jget('/api/featured/full?z=1')])
    .then(([d2, d2z]) => {
      $('#v2Stats').innerHTML = `<div class="day-h" style="margin-top:14px">🆕 V2 精選（十六字頭規則引擎——用戶 2026-09-29 要求加入；卡片標籤「字頭 X X…」＝入選時合格字頭 A–P ＋31 深淺狀態）</div>` +
        _v1StatsBar(d2.stats, false, 'V2精選');
      $('#v2zStats').innerHTML = _v1StatsBar(d2z.stats, true, 'V2精選Z');
      League7.mount('v1', $('#v1Stats'));
      League7.mount('v1z', $('#v1zStats'));
      League7.mount('v2', $('#v2Stats'));
      League7.mount('v2z', $('#v2zStats'));
      render(d2, v2El, false, 'V2', '/api/featured/refresh');
      render(d2z, v2zEl, true, 'V2', '/api/featured/refresh');
    })
    .catch(e => { v2El.innerHTML = '<div class="err">V2 載入失敗：' + esc(String(e)) + '</div>'; })
    .finally(() => LoadX.hide());
}
function v1ShareText(p, z){
  const b = p.brief || {};
  const L = [];
  L.push(`【${z ? 'V1精選Z' : 'V1精選'}】${p.league} ${(p.kickoff || '').slice(5, 16)}`);
  L.push(`${p.home} vs ${p.away}${p.score ? '（' + p.score + '）' : ''}`);
  L.push(`方向：${p.direction === 'up' ? '上盤' : '下盤'}｜尾盤 ${p.line || '—'} ${p.odds || ''}`);
  if (b.i1 && b.i1.all) L.push(`① 上${pct(b.i1.all.up_r)} 下${pct(b.i1.all.down_r)}（${b.i1.all.n}場）`);
  const lts = (p.letters || {}).letters || [];
  if (lts.length) L.push(`合格字頭：${lts.join(' ')}`);
  if (p.result) L.push(`結果：${p.result === 'W' ? '✅命中' : p.result === 'L' ? '❌未中' : '➖走'}`);
  return L.join('\n');
}
$('#btnV1Scan').onclick = async function(){
  this.disabled = true;
  try {
    await jpost('/api/v1/featured/scan', {tool: 'V1掃描掣'});
    pollScanJob('v1', '/api/v1/featured/scan-status', '#btnV1Scan', '#v1ScanInfo', loadV1,
      s => `完成｜新增 ${s.added || 0}（Z ${s.added_z || 0}）${s.error ? '｜錯誤：' + s.error : ''}`);
  } catch (e) {
    toast('掃描失敗：' + e.message);
    this.disabled = false;
  }
};
$('#btnV2Scan').onclick = async function(){
  this.disabled = true;
  try {
    await jpost('/api/featured/scan', {tool: 'V2掃描掣'});
    pollScanJob('v2', '/api/featured/scan-status', '#btnV2Scan', '#v1ScanInfo', loadV1);
  } catch (e) {
    toast('掃描失敗：' + e.message);
    this.disabled = false;
  }
};
/* 三個掃描頁共用：輪詢 scan-status，完咗解鎖掣＋回調刷新（fmtDone 可自定完成文案） */
const _scanTimers = {};
async function pollScanJob(key, url, btnSel, infoSel, onDone, fmtDone){
  clearTimeout(_scanTimers[key]);
  let s;
  try { s = await jget(url); }
  catch (e) {
    const b = $(btnSel);
    if (b) b.disabled = false;
    const info = $(infoSel);
    if (info) info.textContent = '掃描狀態讀取失敗：' + e.message + '（可再撳掣重試）';
    return;
  }
  const info = $(infoSel);
  if (s.running || s.unified_running) {
    const trig = s.triggered_by ? `｜觸發：${s.triggered_by}` : '';
    const q = (s.queue && s.queue.length) ? `｜⏳排隊：${s.queue.join('、')}` : '';
    const prog = s.unified_running ? `（統一掃描 ${s.unified_done}/${s.unified_total}）` : '';
    info.textContent = `掃描中 ${s.done}/${s.total}${prog}${trig}${q}…`;
    _scanTimers[key] = setTimeout(() => pollScanJob(key, url, btnSel, infoSel, onDone, fmtDone), 5000);
    return;
  }
  const b = $(btnSel);
  if (b) b.disabled = false;
  info.textContent = (fmtDone || (x => `完成｜新增 ${x.added || 0}${x.error ? '｜錯誤：' + x.error : ''}`))(s);
  onDone();
}

/* ---------- 🗑 剔走專頁（2026-10-24：邊個工具剔走咗邊場＋實際命中率） ---------- */
function loadRemoved(){
  $('#removedStats').innerHTML = '<div class="note">⏳ 載入緊：剔走專頁…</div>';
  $('#removedBody').innerHTML = '';
  jget('/api/removed/full?limit=300').then(d => {
    const st = d.stats || {};
    const tools = Object.keys(st).sort((a, b) => st[b].n - st[a].n);
    let tw = 0, tl = 0, tn = 0, tp = 0;
    let rows = tools.map(t => {
      const v = st[t];
      tw += v.w; tl += v.l; tn += v.n; tp += v.p;
      return `<tr><td style="white-space:nowrap"><b>${esc(t)}</b></td>` +
        `<td style="color:var(--up)"><b>${v.w}</b></td>` +
        `<td>${v.hit == null ? '—' : v.hit + '%'}</td>` +
        `<td style="color:var(--down)"><b>${v.l}</b></td>` +
        `<td>${v.miss == null ? '—' : v.miss + '%'}</td>` +
        `<td>${v.n}${v.p ? `<small style="color:var(--dim)">（走${v.p}）</small>` : ''}</td></tr>`;
    }).join('');
    const ddec = tw + tl;
    rows += `<tr style="border-top:2px solid var(--gold2)"><td><b>合計</b></td>` +
      `<td style="color:var(--up)"><b>${tw}</b></td>` +
      `<td><b>${ddec ? (tw / ddec * 100).toFixed(1) + '%' : '—'}</b></td>` +
      `<td style="color:var(--down)"><b>${tl}</b></td>` +
      `<td><b>${ddec ? (tl / ddec * 100).toFixed(1) + '%' : '—'}</b></td>` +
      `<td><b>${tn}</b>${tp ? `<small style="color:var(--dim)">（走${tp}）</small>` : ''}</td></tr>`;
    $('#removedStats').innerHTML =
      `<table class="ck-table" style="max-width:760px"><tr><th>工具</th>` +
      `<th>命中場次</th><th>命中率</th><th>唔中場次</th><th>唔中率</th><th>總場次</th></tr>${rows}</table>`;
    const items = d.rows || [];
    if (!items.length){
      $('#removedBody').innerHTML = '<div class="note">暫無剔走記錄——工具重算後踢走唔合資格場次會即時出現喺度。</div>';
      return;
    }
    $('#removedBody').innerHTML = items.map(r => {
      const badge = r.result === 'W' ? '<span class="tag w">✔ 中</span>'
        : r.result === 'L' ? '<span class="tag l">✘ 唔中</span>'
        : r.result === 'P' ? '<span class="tag p">走</span>'
        : '<span class="tag" style="color:var(--gold2);border-color:#6b5510">⏳ 待定</span>';
      const side = r.side ? `<span class="tag dim">${esc(String(r.side))}</span>` : '';
      const det = r.detail ? `<span class="rmeta" style="margin-left:4px">${esc(String(r.detail).slice(0, 60))}</span>` : '';
      const bf = r.backfilled ? '<small style="color:var(--dim)" title="由推送 log 回填——方向為近似值">〔回填〕</small>' : '';
      return `<div class="fcard"><div class="f-top">
        <span class="ko">${esc((r.kickoff || '').slice(5, 16))}</span>
        <span class="lg">${esc(r.league || '')}</span>
        <b class="tm">${esc(r.home)} <span style="color:var(--dim)">vs</span> ${esc(r.away)}</b>
        <span class="tag" style="border-color:#7a2d2d;color:#ff9d9d" title="邊個工具剔走">${esc(r.tool)}</span>
        ${side}${badge}${bf}</div>
        <div class="sub">剔走於 ${esc(r.removed_at || '')}${det}${r.rate != null ? `｜機會率 ${r.rate}%` : ''}${r.settled_at ? `｜結算 ${esc(r.settled_at)}` : ''}</div></div>`;
    }).join('');
  }).catch(e => {
    $('#removedStats').innerHTML = '<div class="err">載入失敗：' + esc(e.message || e) + '</div>';
  });
}

/* ---------- 設定 ---------- */
let settingsLoaded = false;
async function loadSettings(){
  if (settingsLoaded) return;
  try {
    const r = await fetch('/static/settings.html?v=5.1.3');
    $('#setBody').innerHTML = await r.text();
    settingsLoaded = true;
  } catch (e) {
    $('#setBody').innerHTML = '<div class="err">設定頁載入失敗：' + esc(String(e)) + '</div>';
  }
}

/* ---------- 分享 ---------- */
async function shareText(txt){
  if (navigator.share) {
    try { await navigator.share({text: txt}); return; } catch (e) { if (e && e.name === 'AbortError') return; }
  }
  try {
    await navigator.clipboard.writeText(txt);
    toast('已複製，去 WhatsApp/Line 貼上');
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = txt; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
    toast('已複製，去 WhatsApp/Line 貼上');
  }
}

/* ---------- 啟動 ---------- */
(async function boot(){
  loadSettings();
  const ok = await pollReady();
  /* 喚醒完成先收起畫面入得主頁；失敗就留喺喚醒畫面俾用戶撳重試（2026-10-15） */
  const ov = $('#wakeOv');
  if (ok) {
    if (ov) ov.style.display = 'none';
  } else if (ov) {
    ov.innerHTML = '<div style="font-size:44px">⚠️</div>' +
      '<div style="margin-top:16px;font-size:16px;color:#e05c5c">伺服器喚醒超時</div>' +
      '<div style="margin-top:8px;font-size:12px;color:#8b94a7">可能緊 rebuild 緊，等 2–3 分鐘再試</div>' +
      '<button onclick="location.reload()" style="margin-top:18px;padding:12px 34px;font-size:16px;border-radius:12px;border:none;background:#c9a227;color:#000;font-weight:900">↻ 重新喚醒</button>';
  }
  if (ok) {
    loadHome();
    refreshPicksMap();
    refreshUpdInfo();
    refreshConnChip();
    $('#btnRepair').onclick = repairConn;
    $('#workLogBox').addEventListener('toggle', () => { refreshWorkLog(); });
    refreshWorkLog();
    // 每 2 分鐘靜靜哋更新計數
    setInterval(refreshPicksMap, 120000);
    setInterval(() => { refreshUpdInfo(); refreshConnChip(); refreshWorkLog(); checkToolBell(); }, 30000);
    checkToolBell(true);
    // 主頁／事後回查／過往紀錄自動刷新（完場狀態、新場次、新結算會自己更新，2026-09-29）
    // 2026-10-25 加：工具頁（精選W/V1/V2/格）一齊自動刷新——watcher 重算後
    // bust 快取係伺服器端即時，但使用者坐喺工具頁要等到轉頁先見新結果；
    // 而家 60 秒內自動攞（輕量 loader，唔包括 iframe——免成分鐘閃爍重載）。
    setInterval(() => {
      if (document.hidden) return;
      if (curPage === 'home' && !_updBusy) loadHome();
      else if (curPage === 'review') loadReview();
      else if (curPage === 'featlog') loadFeatlog();
      else if (['featw', 'v1', 'check', 'fw7', 'fw8', 'fw12', 'fw8s',
                'picks', 'removed', 'oupred', 'feathourly', 'x8',
                // 2026-10-25 補齊「各版面即時」：iframe 工具頁一併自動刷新
                // （好波/8s/88W/新精選——舊制驚閃爍剔走咗，但用戶原則係
                // 精確度先：60 秒一次 reload 嘅閃爍係正確性應有嘅代價）
                'haobao', 'fw8s', 'w88', 'newjx']
               .indexOf(curPage) >= 0) refreshCurrentTool();
      else if (curPage === 'detail' && curMatch) {
        // 詳情頁坐喺度：🧰工具預測卡＋入選徽章每 60 秒自動重撈（API 無快取，
        // 重算後即刻反映）
        loadMatchTools(curMatch.id);
        if (curMatch.id) jget('/api/v3/target?id=' + curMatch.id)
          .then(r => { if (r && r.target && !r.error) curMatch = r.target; })
          .catch(() => {});
      }
    }, 60000);
    // 賠率變舊自動重抓（>10 分鐘）
    setInterval(async () => {
      if (curPage !== 'home' || !homeData.upcoming) return;
      const stale = homeData.upcoming.filter(m => m.has_odds && m.fetched_ago != null && m.fetched_ago > 600);
      for (const m of stale.slice(0, 5)) {
        try { await jpost('/api/fetch', {id: m.id}); } catch (e) {}
      }
      if (stale.length) loadHome();
    }, 120000);
  }
})();

/* ---------- 📐 同賽事盤口規則頁（2026-10-05） ---------- */
async function loadOupred(){
  $('#opPanel').innerHTML = '<div class="note">⏳ 載入緊：單場規律…</div>';
  $('#opHist').innerHTML = '';
  try {
    const [panel, hist] = await Promise.all([
      jget('/api/oupred/panel'), jget('/api/oupred/history')]);
    let h = `<div class="tcard"><h2>📐 ${esc(panel.rule.name)}</h2>
      <div class="meta">${esc(panel.rule.formula)}</div>`;
    h += '<table class="ck-table"><tr><th>因子</th><th>權重</th><th>說明</th></tr>';
    for (const f of panel.rule.factors)
      h += `<tr><td style="text-align:left">${esc(f.name)}</td><td>${f.w}%</td><td style="text-align:left">${esc(f.desc)}</td></tr>`;
    h += '</table>';
    const bt = panel.backtest || {};
    h += `<div class="statbar">回查資料庫（2026 年完場・out-of-sample）：
      <span>總命中率 <b>${bt.hit_r != null ? bt.hit_r + '%' : '—'}（n=${bt.n || 0}）</b></span>
      <span>指上 <b>${bt.up && bt.up.hit_r != null ? bt.up.hit_r + '%' : '—'}（n=${bt.up ? bt.up.n : 0}）</b></span>
      <span>指下 <b>${bt.down && bt.down.hit_r != null ? bt.down.hit_r + '%' : '—'}（n=${bt.down ? bt.down.n : 0}）</b></span>
    </div>`;
    if (bt.per_league && bt.per_league.length) {
      h += '<details><summary>逐聯賽回查命中率（n≥30，前 20）<span class="sub"></span></summary><div class="body"><table class="ck-table"><tr><th>聯賽</th><th>n</th><th>命中率</th></tr>';
      for (const L of bt.per_league)
        h += `<tr><td style="text-align:left">${esc(L.league)}</td><td>${L.n}</td><td class="${L.hit_r >= 50 ? 'r-up' : 'r-down'}">${L.hit_r}%</td></tr>`;
      h += '</table></div></details>';
    }
    if (panel.league_factors && panel.league_factors.length) {
      h += '<details><summary>每聯賽獨立盤口因子示例（n 最大 15 條）</summary><div class="body"><table class="ck-table"><tr><th>聯賽</th><th>盤口</th><th>n</th><th>上盤率</th></tr>';
      for (const L of panel.league_factors)
        h += `<tr><td style="text-align:left">${esc(L.league)}</td><td>${esc(L.key)}</td><td>${L.n}</td><td class="${L.up_r >= 50 ? 'r-up' : 'r-down'}">${L.up_r}%</td></tr>`;
      h += '</table></div></details>';
    }
    h += '</div>';
    $('#opPanel').innerHTML = h;
    // 歷史紀錄（10月1日起）
    const st = hist.stats || {};
    let hh = `<div class="statbar">歷史紀錄（2026-10-01 起，out-of-sample 預測 vs 實際）：
      <span>場數 <b>${st.n || 0}</b></span><span>命中 <b>${st.hits || 0}</b></span>
      <span>命中率 <b>${st.hit_r != null ? st.hit_r + '%' : '—'}</b></span></div>`;
    hh += '<table class="ck-table"><tr><th>時間</th><th>聯賽</th><th>主隊 vs 客隊</th><th>比分</th><th>預測</th><th>實際</th><th>結果</th></tr>';
    for (const r of hist.rows || []) {
      const cls = r.hit === true ? 'r-up' : (r.hit === false ? 'r-down' : '');
      const mark = r.hit === true ? '✓中' : (r.hit === false ? '✗錯' : '走盤');
      hh += `<tr><td>${esc((r.kickoff || '').slice(5, 16))}</td><td>${esc(r.league)}</td>
        <td style="text-align:left">${esc(r.home)} vs ${esc(r.away)}</td>
        <td>${esc(r.score)}</td><td>${r.pred_dir === 'up' ? '上' : '下'}${r.pred_up}%</td>
        <td>${r.actual === 'A' ? '上贏' : (r.actual === 'B' ? '下贏' : '走')}</td>
        <td class="${cls}">${mark}</td></tr>`;
    }
    hh += '</table>';
    $('#opHist').innerHTML = hh;
  } catch (e) {
    $('#opPanel').innerHTML = '<div class="err">載入失敗：' + esc(e) + '</div>';
  }
}

/* ---------- ⏰ 精選系列每小時紀錄頁（2026-10-05） ---------- */
/* ---------- ⏰ 每兩小時自動紀錄：精選W／7／8／12 資格時間線＋尾盤盤口賠率 ---------- */
let fhPeriod = 'all', fhCat = 'all';
function fhSetPeriod(v){ fhPeriod = v; loadFeatHourly(); }
function fhSetCat(v){ fhCat = v; loadFeatHourly(); }
function _fhLocalDate(offDays){
  const d = new Date(Date.now() - (offDays || 0) * 864e5);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}
function _fhPeriodOk(ko){
  if (fhPeriod === 'all') return true;
  const d = (ko || '').slice(0, 10);
  if (!d) return false;
  if (fhPeriod === 'today') return d === _fhLocalDate(0);
  if (fhPeriod === 'yesterday') return d === _fhLocalDate(1);
  return d >= _fhLocalDate(parseInt(fhPeriod, 10));
}
function _fhSnapTxt(sp){
  if (!sp) return '';
  const parts = [];
  if (sp.ahc != null){
    parts.push('亞盤 ' + (sp.ahc === 0 ? '平手'
      : fmtLine(sp.agv === 'home' ? -sp.ahc : sp.ahc))
      + ' ' + (sp.aho ?? '–') + '/' + (sp.aaw ?? '–'));
  }
  if (sp.oul != null){
    parts.push('大小 ' + sp.oul + ' ' + (sp.ouo ?? '–') + '/' + (sp.ouu ?? '–'));
  }
  return parts.length
    ? ' <span class="tag dim" title="每兩小時紀錄嘅尾盤快照（易勝博 closing）">'
      + parts.join('｜') + '</span>' : '';
}
async function loadFeatHourly(){
  $('#fhBody').innerHTML = '<div class="note">⏳ 載入緊：每小時紀錄…</div>';
  try {
    const d = await jget('/api/feathourly');
    let h = '';
    const scan = d.scan || {};
    h += `<div class="statbar">每兩小時自動紀錄：上次 <b>${esc(scan.last || '未行過')}</b>
      ｜已行 <b>${scan.runs || 0}</b> 次${scan.running ? '｜⏳ 行緊' : ''}
      ${scan.error ? '｜<span class="err">錯誤：' + esc(scan.error) + '</span>' : ''}
      <button class="btn" style="margin-left:auto" onclick="loadFeatHourly()">⟳ 重新整理</button></div>`;
    /* 2026-10-10：Period／聯賽杯賽類 Filter——兩個獨立可疊加（二選一或二選二） */
    h += `<div class="statbar">回查篩選：Period
      <select onchange="fhSetPeriod(this.value)">
        <option value="all"${fhPeriod==='all'?' selected':''}>全部</option>
        <option value="today"${fhPeriod==='today'?' selected':''}>今日</option>
        <option value="yesterday"${fhPeriod==='yesterday'?' selected':''}>昨日</option>
        <option value="3"${fhPeriod==='3'?' selected':''}>近3日</option>
        <option value="7"${fhPeriod==='7'?' selected':''}>近7日</option>
        <option value="30"${fhPeriod==='30'?' selected':''}>近30日</option>
      </select>
      ｜類型
      <select onchange="fhSetCat(this.value)">
        <option value="all"${fhCat==='all'?' selected':''}>全部</option>
        <option value="聯賽"${fhCat==='聯賽'?' selected':''}>聯賽</option>
        <option value="杯賽"${fhCat==='杯賽'?' selected':''}>杯賽</option>
      </select></div>`;
    for (const kind of ['W', '7', '8', '12']) {
      const K = d.kinds[kind];
      if (!K) continue;
      const s = K.stats;
      const list = K.matches.filter(M =>
        _fhPeriodOk(M.kickoff) && (fhCat === 'all' || M.cat === fhCat));
      h += `<div class="day-h">★ ${esc(K.name)}：追蹤中 ${s.tracked}｜而家合資格 ${s.now_eligible}｜曾合資格 ${s.ever_eligible}｜最後不合資格 ${s.dropped_final}｜已賽 ${s.played}（W${s.wins}/L${s.losses}/P${s.pushes}）${s.hit_r != null ? '｜命中率 <b style="color:var(--gold2)">' + s.hit_r + '%</b>' : ''}${list.length !== s.tracked ? `｜篩選後 ${list.length} 場` : ''}</div>`;
      for (const M of list) {
        const badges = M.hist.map(x => x.elig
          ? '<span style="color:var(--up)">✓</span>'
          : '<span style="color:var(--down)">✗</span>').join(' ');
        let tags = '';
        if (M.now_elig) tags += '<span class="tag up">合資格</span> ';
        if (M.final_eligible === 0) tags += '<span class="tag down" title="曾合資格，但尾盤重算唔再合資格">最後不合資格</span> ';
        if (M.result === 'W') tags += '<span class="tag up">中</span>';
        else if (M.result === 'L') tags += '<span class="tag down">錯</span>';
        else if (M.result === 'P') tags += '<span class="tag dim">走</span>';
        h += `<div class="fcard"><div class="f-top">
          <span class="f-time">${esc((M.kickoff || '').slice(5, 16))}</span>
          <span class="lg" style="color:var(--gold2);font-size:12px">${esc(M.league)}</span>
          <span class="f-teams">${esc(M.home)} vs ${esc(M.away)}</span>
          ${M.direction ? '<span class="tag dim">' + (M.direction === 'up' ? '上' : '下') + '</span>' : ''}
          ${_fhSnapTxt(M.snap)}
          ${tags}<span class="hint">每兩小時資格：${badges}（${M.snaps} 次）</span></div></div>`;
      }
      if (!list.length) h += '<div class="note">篩選條件下暫無紀錄。</div>';
    }
    $('#fhBody').innerHTML = h;
  } catch (e) {
    $('#fhBody').innerHTML = '<div class="err">載入失敗：' + esc(e) + '</div>';
  }
}

/* ---------- 2026-10-25 分聯賽/杯賽實測報表（用戶規格：全部工具頁頂部） ----------
 * 後端 /api/league_report?tool=X 即時 SQL（賽事一結算，下次請求即反映——冇快取
 * staleness 問題）。每張表：2026 總成績（場/贏/輸/走/勝率）＋最近七日同口徑；
 * 聯賽以近7日勝率最佳排先；2026<60% 且 近7日<60% → 淺暗紅底 highlight。 */
const League7 = {
  _c: {},
  async get(tool){
    if (this._c[tool] && Date.now() - this._c[tool].ts < 30000)
      return this._c[tool].data;
    const d = await jget('/api/league_report?tool=' + tool);
    this._c[tool] = {ts: Date.now(), data: d};
    return d;
  },
  /* 2026-10-26 手機走哂位根因：呢個表 5 欄全部 nowrap，闊過熒幕時右邊兩欄被裁、
   * 聯賽名被 ellipsis 食晒（用戶：大量文字缺失）。改做 display:table——欄位按內容
   * 對齊、任何闊度都完整顯示，外層 .lg7scroll overflow-x:auto 手指橫掃。 */
  rowHtml(r){
    const hit = r.hit != null ? r.hit.toFixed(1) + '%' : '—';
    const hit7 = r.hit7 != null ? r.hit7.toFixed(1) + '%' : '—';
    const c7 = r.hit7 != null ? (r.hit7 >= 50 ? '#3fb950' : '#f85149') : 'var(--dim)';
    return `<div style="display:table-row;${r.warn ? 'background:rgba(139,0,0,.16);' : ''}">` +
      `<span style="display:table-cell;white-space:nowrap;padding:4px 12px 4px 0;font-size:13px">${esc(r.league)}</span>` +
      `<span style="display:table-cell;white-space:nowrap;padding:4px 12px 4px 0;font-size:13px;color:var(--dim)">${r.n}場｜<b style="color:#3fb950">贏${r.w}</b>｜<b style="color:#f85149">輸${r.l}</b>${r.p ? '｜<span style="color:var(--dim)">走' + r.p + '</span>' : ''}</span>` +
      `<b class="num" style="display:table-cell;white-space:nowrap;padding:4px 0 4px 12px;text-align:right;font-size:13px;color:${r.hit != null && r.hit >= 50 ? '#3fb950' : '#f85149'}">${hit}</b>` +
      `<span style="display:table-cell;white-space:nowrap;padding:4px 12px;font-size:13px;color:var(--dim)">${r.n7 != null ? r.n7 + '場｜<b style="color:#3fb950">贏' + (r.w7 || 0) + '</b>｜<b style="color:#f85149">輸' + (r.l7 || 0) + '</b>' + (r.p7 ? '｜<span style="color:var(--dim)">走' + r.p7 + '</span>' : '') : '—'}</span>` +
      `<b class="num" style="display:table-cell;white-space:nowrap;padding:4px 0 4px 12px;text-align:right;font-size:13px;color:${c7}">${hit7}</b></div>`;
  },
  headerHtml(){
    const c = 'display:table-cell;white-space:nowrap;padding:4px 12px 4px 0;font-size:11px;color:var(--dim);font-weight:400';
    return `<div style="display:table-row">` +
      `<span style="${c}"></span>` +
      `<span style="${c}">2026 總成績</span>` +
      `<span style="${c};padding:4px 0 4px 12px;text-align:right">勝率</span>` +
      `<span style="${c}">最近七日</span>` +
      `<span style="${c};padding:4px 0 4px 12px;text-align:right">勝率</span></div>`;
  },
  async renderInto(tool, el){
    try {
      const d = await this.get(tool);
      el.innerHTML = (d.rows && d.rows.length)
        ? this.headerHtml() + d.rows.map(r => this.rowHtml(r)).join('')
        : '<div class="note">暫無 2026 已結算紀錄。</div>';
    } catch (e) {
      el.innerHTML = '<div class="err">報表載入失敗：' + esc(String(e)) + '</div>';
    }
  },
  /* 喺容器頂部掛報表卡。注意：容器 innerHTML 全部 set 完之後先 call——
   * mount 會 prepend，下次 reload 會重建，唔會疊。 */
  mount(tool, container){
    if (!container) return;
    let card = container.querySelector(':scope > .lg7card');
    if (!card){
      card = document.createElement('div');
      card.className = 'fcard lg7card';
      card.style.margin = '8px 0';
      card.innerHTML = '<div class="f-top" style="flex-wrap:wrap;gap:8px"><b>🏆 分聯賽/杯賽實測</b>' +
        '<span style="color:var(--dim);font-size:12px">2026 總成績｜最近七日（結算即更新；近7日勝率排先；雙低於60%<span style="background:rgba(139,0,0,.16)">淺暗紅</span>；表格可橫掃）</span></div>' +
        /* 橫掃容器：表格闊過熒幕時手指掃，唔再裁字（2026-10-26） */
        '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">' +
        '<div class="lg7body" style="display:table;border-collapse:collapse"><div class="note">⏳ 載入緊…</div></div></div>';
      container.prepend(card);
    }
    this.renderInto(tool, card.querySelector('.lg7body'));
  }
};

/* ---------- 🧪 8X：66 條優化公式賽前記錄＋賽後自動結算 ---------- */
let _x8Badge = 0;
function x8UpDown(r){
  /* r.sline 係 Python 口徑（+＝主讓，同 screen_engine.signed_line）；
     fmtLine 係 JS 口徑（-＝主讓，見 2914 行呼叫方式）——所以要反號。
     上盤＝讓球方；平手→上盤＝主隊（一貫定義） */
  if (r.sline == null) return {up: r.home, down: r.away, lineTxt: '無尾盤'};
  const v = -r.sline;
  const lineTxt = fmtLine(v);
  if (v < 0) return {up: r.home, down: r.away, lineTxt};
  if (v > 0) return {up: r.away, down: r.home, lineTxt};
  return {up: r.home, down: r.away, lineTxt: '平手'};
}
async function loadX8(){
  try {
    const d = await jget('/api/8x/list');
    try { window._lg7x = await League7.get('8x'); } catch (e) { window._lg7x = null; }
    await loadCondHr();
    const decided = d.w + d.l;
    const hr = decided ? d.w / decided : null;
    let h = `<div class="fcard" style="margin:8px 0"><div class="f-top" style="flex-wrap:wrap;gap:8px">
      <b>8X 真實成績</b>
      <span>總記錄 <b>${d.total}</b> 場</span>
      <span style="color:#3fb950">贏 ${d.w}</span>
      <span style="color:#f85149">輸 ${d.l}</span>
      <span style="color:var(--dim)">走 ${d.p}</span>
      <span style="color:var(--gold2)">待定 ${d.pending}</span>
      <span>命中率 <b style="color:${hr != null && hr >= 0.5 ? '#3fb950' : '#f85149'};font-size:1.2em">${hr == null ? '—' : (hr * 100).toFixed(1) + '%'}</b>${decided ? `（${d.w}/${decided}）` : ''}</span>
    </div></div>`;
    if (d.bt && d.bt.total){
      const B = d.bt;
      const bDec = B.w + B.l, bHr = bDec ? B.w / bDec : null;
      h += `<div class="fcard" style="margin:8px 0;border-color:#6b5510"><div class="f-top" style="flex-wrap:wrap;gap:8px">
        <b style="color:var(--gold2)">${esc(B.label || '2026 全年回測')}</b>
        <span>合資格 <b>${B.total}</b> 場</span>
        <span style="color:#3fb950">贏 ${B.w}</span>
        <span style="color:#f85149">輸 ${B.l}</span>
        <span style="color:var(--dim)">走 ${B.p}</span>
        <span>命中率 <b style="color:${bHr != null && bHr >= 0.5 ? '#3fb950' : '#f85149'};font-size:1.2em">${bHr == null ? '—' : (bHr * 100).toFixed(1) + '%'}</b>${bDec ? `（${B.w}/${bDec}）` : ''}</span>
        <span style="color:var(--dim);font-size:12px">更新於 ${esc(B.updated_at || '')}</span></div>
        ${B.excl && B.excl.total ? `<div class="sub" style="margin-top:2px;color:var(--dim)">🚫 已剔除命中率&lt;50%：${B.excl.leagues.map(e => esc(e.league) + ' ' + e.hit_rate + '%').join('、')} → 餘下 <b>${B.excl.total}</b> 場，命中率 <b style="color:#3fb950;font-size:1.1em">${B.excl.hit_rate}%</b></div>` : ''}
        ${(B.by_league || []).length ? (() => {
          /* 2026-10-25 用戶規格：同一表內加最近七日戰果（實測 fw8x_log 結算），
           * 聯賽以近7日勝率最佳排先；2026<60% 且 近7日<60% 淺暗紅 highlight。 */
          const r7map = {};
          if (window._lg7x && window._lg7x.rows)
            for (const r of window._lg7x.rows) r7map[r.league] = r;
          const merged = B.by_league.map(L => ({L, r7: r7map[L.league] || null}));
          merged.sort((a, b) => {
            const h7a = a.r7 && a.r7.hit7 != null ? a.r7.hit7 : -1;
            const h7b = b.r7 && b.r7.hit7 != null ? b.r7.hit7 : -1;
            if (h7b !== h7a) return h7b - h7a;
            const ra = (a.L.w + a.L.l) ? a.L.w / (a.L.w + a.L.l) : -1;
            const rb = (b.L.w + b.L.l) ? b.L.w / (b.L.w + b.L.l) : -1;
            return rb - ra;
          });
          /* 2026-10-26：同一個 League7 表格走哂位根因——改 display:table＋外層橫掃，
           * 欄位對齊、全部文字可見（詳見 League7.rowHtml 註釋）。 */
          const c8 = 'display:table-cell;white-space:nowrap;padding:4px 12px 4px 0;font-size:13px';
          return `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><div style="display:table;border-collapse:collapse">` +
            `<div style="display:table-row"><span style="${c8};font-size:11px;color:var(--dim)"></span><span style="${c8};font-size:11px;color:var(--dim)">2026 總成績</span><span style="${c8};font-size:11px;color:var(--dim);padding:4px 0 4px 12px;text-align:right">勝率</span><span style="${c8};font-size:11px;color:var(--dim)">最近七日</span><span style="${c8};font-size:11px;color:var(--dim);padding:4px 0 4px 12px;text-align:right">勝率</span></div>` +
            merged.map(({L, r7}) => {
            const dec = L.w + L.l, r2 = dec ? L.w / dec : null;
            const warn = r7 && r7.warn;
            const hit7t = r7 ? (r7.hit7 != null ? r7.hit7.toFixed(1) + '%' : '—') : '—';
            const d7t = r7 ? (r7.n7 != null ? `${r7.n7}場｜<b style="color:#3fb950">贏${r7.w7 || 0}</b>｜<b style="color:#f85149">輸${r7.l7 || 0}</b>${r7.p7 ? '｜<span style="color:var(--dim)">走' + r7.p7 + '</span>' : ''}` : '—') : '—';
            return `<div style="display:table-row;${warn ? 'background:rgba(139,0,0,.16);' : ''}">
              <span style="${c8}">${esc(L.league)}</span>
              <span style="${c8};color:var(--dim)">${L.n}場｜<b style="color:#3fb950">贏${L.w}</b>｜<b style="color:#f85149">輸${L.l}</b>${L.p ? '｜<span style="color:var(--dim)">走' + L.p + '</span>' : ''}</span>
              <b class="num" style="${c8};padding:4px 0 4px 12px;text-align:right;color:${r2 != null && r2 >= 0.5 ? '#3fb950' : '#f85149'}">${r2 == null ? '—' : (r2 * 100).toFixed(1) + '%'}</b>
              <span style="${c8};color:var(--dim)">${d7t}</span>
              <b class="num" style="${c8};padding:4px 0 4px 12px;text-align:right;color:${r7 && r7.hit7 != null ? (r7.hit7 >= 50 ? '#3fb950' : '#f85149') : 'var(--dim)'}">${hit7t}</b></div>`;
          }).join('') + '</div></div>';
        })() : ''}
        ${B.note ? `<div class="sub" style="margin-top:4px;color:var(--dim);font-size:12px">⚠️ ${esc(B.note)}</div>` : ''}
      </div>`;
    }
    if (d.by_league.length){
      h += '<div class="fcard" style="margin:8px 0"><div class="f-top" style="flex-wrap:wrap;gap:6px"><b>分聯賽</b>　';
      h += d.by_league.map(L => {
        const dec = L.w + L.l, r2 = dec ? L.w / dec : null;
        return `<span class="tag" style="border-color:#6b5510">${esc(L.league)} ${L.n}場 ` +
          `<b style="color:${r2 != null && r2 >= 0.5 ? '#3fb950' : '#f85149'}">${r2 == null ? '—' : (r2 * 100).toFixed(0) + '%'}</b></span>`;
      }).join(' ');
      h += '</div></div>';
    }
    $('#x8Stats').innerHTML = h;
    /* 2026-10-21：上/下盤方向細分：幾中幾＋% */
    if ((d.by_dir || []).length){
      $('#x8Stats').insertAdjacentHTML('afterbegin', '<div class="fcard" style="margin:8px 0"><div class="f-top" style="flex-wrap:wrap;gap:6px"><b>方向細分</b>　' +
        d.by_dir.map(m => { const dec = (m.w||0)+(m.l||0), r = dec ? m.w/dec : null;
          return `<span class="tag" style="border-color:#6b5510">${m.dir==='up'?'上盤':'下盤'} <b>${m.w||0}中${dec}</b>${r==null?'':` ${(r*100).toFixed(1)}%`}${m.p?` 走${m.p}`:''}<small style="color:var(--dim)"> n=${m.n}</small></span>`;
        }).join(' ') + '</div></div>');
    }
    let b = '';
    if (!d.rows.length) b = '<div class="note">暫無記錄。背景每 20 分鐘自動掃描未來 7 日場次，有公式全中嘅場次會自動記錄並彈窗通知。</div>';
    for (const r of d.rows){
      const ud = x8UpDown(r);
      const isUp = r.dir === 'up';
      const predTeam = isUp ? ud.up : ud.down;
      const resBadge = r.result === 'W' ? '<span class="tag" style="color:#3fb950;border-color:#3fb950">✔ 贏</span>'
        : r.result === 'L' ? '<span class="tag" style="color:#f85149;border-color:#f85149">✘ 輸</span>'
        : r.result === 'P' ? '<span class="tag dim">走盤</span>'
        : '<span class="tag" style="color:var(--gold2);border-color:#6b5510">⏳ 待定</span>';
      b += `<div class="fcard"><div class="f-top" style="flex-wrap:wrap;gap:6px">
        <span class="f-time">${esc((r.kickoff || '').slice(5, 16))}</span>
        <span class="lg" style="color:var(--gold2);font-size:12px">${esc(r.league)}</span>
        <b>${esc(r.home)} <span style="color:var(--dim)">vs</span> ${esc(r.away)}</b>
        <span class="tag dim">${esc(ud.lineTxt)}</span>
        <span class="tag dim">主 ${r.upw == null ? '—' : (+r.upw).toFixed(2)}／客 ${r.dnw == null ? '—' : (+r.dnw).toFixed(2)}</span>
        ${resBadge}</div>
        <div class="sub" style="margin-top:4px">預測：<b style="color:${isUp ? '#3fb950' : '#f85149'}">${isUp ? 'K3上盤' : '下盤'}＝${esc(predTeam)}</b>
        　回測基數 ${r.n_back || 0} 場・命中率 ${r.hit_back == null ? '—' : (r.hit_back * 100).toFixed(1) + '%'}
        　<span style="color:var(--dim)">記錄於 ${new Date(r.ts * 1000).toLocaleString('zh-HK', {hour12: false})}</span></div>` +
        ((r.preds || []).length
          ? `<div class="sub" style="margin-top:2px">公式條件：${r.preds.map(p => esc(p) + condHrTxt('k3', p, r.market)).join('｜')}</div>`
          : '') + `</div>`;
    }
    $('#x8Body').innerHTML = b;
    $('#x8Msg').textContent = `最後更新 ${new Date().toLocaleTimeString('zh-HK', {hour12: false})}`;
  } catch (e) {
    $('#x8Body').innerHTML = '<div class="err">載入失敗：' + esc(e) + '</div>';
  }
}
async function x8DoScan(){
  const btn = $('#btnX8Scan');
  btn.disabled = true; $('#x8Msg').textContent = '掃描緊…';
  try {
    const r = await jpost('/api/8x/scan');
    toast(`8X：新增 ${r.added} 場・結算 ${r.settled} 場`);
    await loadX8();
  } catch (e) { toast('8X 掃描失敗：' + e); }
  btn.disabled = false;
}
function _x8Beep(){
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [880, 1175].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f; o.type = 'sine';
      g.gain.setValueAtTime(0.001, ctx.currentTime + i * 0.25);
      g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + i * 0.25 + 0.03);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.25 + 0.22);
      o.connect(g); g.connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.25); o.stop(ctx.currentTime + i * 0.25 + 0.25);
    });
  } catch (_) {}
  if (navigator.vibrate) try { navigator.vibrate([200, 100, 200]); } catch (_) {}
}
function x8Popup(list){
  $('#x8ModalBody').innerHTML = list.map(r => {
    return `<div class="fcard" style="margin:6px 0"><div class="f-top">
      <span class="f-time">${esc((r.kickoff || '').slice(5, 16))}</span>
      <span class="lg" style="color:var(--gold2);font-size:12px">${esc(r.league)}</span>
      <b>${esc(r.home)} vs ${esc(r.away)}</b>
      <span class="tag" style="color:${r.direction === 'up' ? '#3fb950' : '#f85149'};border-color:currentColor">${r.direction === 'up' ? '上盤' : '下盤'}</span>
    </div></div>`;
  }).join('');
  $('#x8Modal').classList.add('on');
  _x8Beep();
  _x8Badge += list.length;
  const c = $('#x8Cnt'); if (c) c.textContent = _x8Badge ? '🔔' + _x8Badge : '';
}
async function x8Poll(){
  try {
    let since = parseFloat(localStorage.getItem('x8last') || '0');
    if (!since){
      /* 第一次開啟：以而家做基準，唔會彈返晒歷史 */
      since = Date.now() / 1000;
      localStorage.setItem('x8last', String(since));
      return;
    }
    const r = await fetch('/api/8x/new?since=' + since);
    if (!r.ok) return;
    const list = await r.json();
    if (list.length){
      localStorage.setItem('x8last', String(Math.max(...list.map(x => x.created_ts))));
      x8Popup(list);
    }
  } catch (_) {}
}
document.addEventListener('click', e => {
  if (e.target.id === 'btnX8Scan') x8DoScan();
  if (e.target.id === 'btnX8List') loadX8();
  if (e.target.id === 'x8ModalClose') $('#x8Modal').classList.remove('on');
});

/* ---------- 📚 精選12 全庫重篩（永久保留） ---------- */
function _fw12LineTxt(it){
  const h = it.line_h, g = it.line_g;
  if (h == null) return '—';
  const a = Math.abs(h);
  let line;
  if (a < 0.02) line = '平手';
  else if (a < 0.27) line = (g === 'home' ? '主' : '客') + '讓平半';
  else if (a < 0.52) line = (g === 'home' ? '主' : '客') + '讓半球';
  else if (a < 0.77) line = (g === 'home' ? '主' : '客') + '讓半一';
  else line = (g === 'home' ? '主' : '客') + '讓' + a.toFixed(2);
  return line;
}
async function loadFw12Lib(){
  const S = $('#fw12libStats'), L = $('#fw12libList'), G = $('#fw12libLg'), I = $('#fw12libInfo');
  try {
    const d = await (await fetch('/api/fw12/audit')).json();
    if (d.error){ S.innerHTML = '<div class="note">' + esc(d.error) + '</div>'; L.innerHTML = ''; G.innerHTML = ''; I.textContent=''; return; }
    let h = `<div class="fcard"><div class="f-top" style="flex-wrap:wrap;gap:8px">
      <b>全庫已掃 <span class="num">${d.screened}</span> 場</b>
      <span class="tag" style="border-color:var(--gold2)">合資格 <b class="num">${d.pass}</b> 場</span>
      <span class="tag" style="color:#3fb950;border-color:#3fb950">贏 ${d.w}</span>
      <span class="tag" style="color:#f85149;border-color:#f85149">輸 ${d.l}</span>
      <span class="tag dim">走 ${d.p}</span>
      <span class="tag" style="border-color:#6b5510">命中率 <b class="num" style="color:${d.hit_rate >= 50 ? '#3fb950' : '#f85149'}">${d.hit_rate == null ? '—' : d.hit_rate + '%'}</b></span></div>`;
    if (!d.scan){
      h += `<div class="sub" style="margin-top:4px;color:var(--gold2)">⏳ 全庫重篩進行中——數字會隨掃描增加，結果永久保留</div>`;
    } else if (d.scan.phase !== 'done' && d.scan.last_id){
      h += `<div class="sub" style="margin-top:4px;color:var(--gold2)">⏳ 全庫重篩進行中（已掃到 match_id ${d.scan.last_id}）——數字會隨掃描增加，結果永久保留</div>`;
    } else {
      h += `<div class="sub" style="margin-top:4px;color:var(--dim)">✅ 全庫重篩完成・結果永久保留（${d.source === 'live' ? '實時審計庫' : '雲端快照'}）</div>`;
    }
    h += '</div>';
    S.innerHTML = h;
    League7.mount('g12', $('#fw12libStats'));
    G.innerHTML = (d.leagues && d.leagues.length) ? '<div class="fcard"><div class="f-top" style="flex-wrap:wrap;gap:6px"><b>分聯賽</b>　' +
      d.leagues.map(x => {
        const dec = x.w + x.l;
        const hr = x.hit_rate;
        return `<span class="tag" style="border-color:#6b5510">${esc(x.league)} ${x.n}場 ` +
          `<b style="color:${hr != null && hr >= 50 ? '#3fb950' : '#f85149'}">${hr == null ? '—' : hr + '%'}</b></span>`;
      }).join(' ') + '</div></div>' : '';
    let b = '';
    if (!d.items || !d.items.length) b = '<div class="note">暫無合資格場次。</div>';
    for (const it of (d.items || [])){
      const isUp = it.direction === 'up';
      const upTeam = (it.line_g === 'away') ? it.away : it.home;
      const downTeam = (it.line_g === 'away') ? it.home : it.away;
      const dirTeam = isUp ? upTeam : downTeam;
      const resBadge = it.result === 'W' ? '<span class="tag" style="color:#3fb950;border-color:#3fb950">✔ 贏</span>'
        : it.result === 'L' ? '<span class="tag" style="color:#f85149;border-color:#f85149">✘ 輸</span>'
        : it.result === 'P' ? '<span class="tag dim">走盤</span>'
        : '<span class="tag" style="color:var(--gold2);border-color:#6b5510">⏳ 待定</span>';
      const score = (it.hs != null && it.aws != null) ? ` <b class="num">${it.hs}-${it.aws}</b>` : '';
      const cellsTxt = (it.cells && it.cells.length) ? it.cells.map(c => esc(c.cell || c)).join('·') : '';
      b += `<div class="fcard"><div class="f-top" style="flex-wrap:wrap;gap:6px">
        <span class="f-time">${esc((it.kickoff || '').slice(0, 16))}</span>
        <span class="lg" style="color:var(--gold2);font-size:12px">${esc(it.league)}</span>
        <b>${esc(it.home || '')} <span style="color:var(--dim)">vs</span> ${esc(it.away || '')}${score}</b>
        <span class="tag dim">${esc(_fw12LineTxt(it))}</span>
        <span class="tag dim">主 ${it.close_ho == null ? '—' : (+it.close_ho).toFixed(2)}／客 ${it.close_ao == null ? '—' : (+it.close_ao).toFixed(2)}</span>
        <span class="tag" style="color:${isUp ? '#3fb950' : '#f85149'};border-color:currentColor">${isUp ? '上盤' : '下盤'}＝${esc(dirTeam || '')}</span>
        ${resBadge}</div>
        <div class="sub" style="margin-top:4px;color:var(--dim)">12格：${cellsTxt}</div></div>`;
    }
    L.innerHTML = b;
    I.textContent = `合資格 ${d.pass} 場｜最後更新 ${new Date().toLocaleTimeString('zh-HK', {hour12: false})}`;
  } catch (e) {
    L.innerHTML = '<div class="err">載入失敗：' + esc(e) + '</div>';
  }
}

setInterval(x8Poll, 60000);
