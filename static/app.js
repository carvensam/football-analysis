/* FootballAnalysis V3 前端（2026-09-28 全面重建）
   V3 項目號：1–6 時點組／7–13 上賽vs今場時點／14–20 水位段版／21–45（見 V3_TITLES）
   V1 規則號只喺「舊版本」分頁用，同 V3 完全隔離。 */
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function pct(v){ return v==null ? '—' : (v*100).toFixed(1)+'%'; }
function rn(name, rank){ return rank ? `(${rank}) ${name}` : name; }
let toastTimer = null;
function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = 'none'; }, 2500);
}
async function _jf(url, opt, tries){
  /* 伺服器更新緊／重啟中會吐 HTML 維護頁——檢查 Content-Type／首字元 '<'，
     指數退避重試，最後先抛人話錯誤（2026-09-29 修復「Unexpected token '<'」）。
     2026-09-30：GET 重試加碼至 4 次（2+4+8+16+32≈62 秒）——Render 免費版
     閒置瞓著後冷啟動要 30-60 秒，舊預算 6 秒內就放棄，搞到每次開 APP
     精選都「載入失敗」；POST 維持 1 次避免雙重提交。 */
  let backoff = 2000;
  const maxTries = tries ?? 4;
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(url, opt);
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
      if (i >= maxTries) {
        if (e instanceof TypeError)
          throw new Error('連唔到伺服器——請檢查網絡，或撳頂部「🔧 修復」掣');
        throw e;
      }
      await new Promise(x => setTimeout(x, backoff)); backoff *= 2;
    }
  }
}
const jget = url => _jf(url, {}, 4);
async function jpost(url, body){
  return _jf(url, {method:'POST', headers:{'Content-Type':'application/json'},
                   body: JSON.stringify(body || {})}, 1);
}

/* ---------- 狀態 ---------- */
async function pollReady(){
  for (let i = 0; i < 90; i++) {
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
  toast('🔧 修復緊：重試連線…');
  _setConnChip('', '⏳ 修復緊…');
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
/* ---------- 工作記錄面板：crawl_log 最近動態 + 更新狀態 ---------- */
let _worklogLoaded = false;
async function refreshWorkLog(){
  const box = $('#workLogBox');
  if (!box || !box.open) return;
  let d;
  try { d = await jget('/api/worklog?limit=30'); }
  catch (e) { $('#workLog').innerHTML = '<div class="err">工作記錄載入失敗：' + esc(String(e)) + '</div>'; return; }
  const u = d.update || {}, w = d.window || {}, c = d.catchup || {};
  let sum = '';
  if (u.running) sum = `更新緊：${u.phase || ''}`;
  else if (u.error) sum = '⚠️ 上次更新失敗（見下）';
  else if (u.last_done) sum = '上次更新：' + Math.round((Date.now() / 1000 - u.last_done) / 60) + ' 分鐘前';
  if (w.running) sum += `${sum ? '｜' : ''}⚡窗口更新 ${w.done}/${w.total}`;
  if (c.last) sum += `${sum ? '｜' : ''}賽果補抓：${c.last.slice(5, 16)} 補 ${c.fixed || 0} 場`;
  $('#workLogSum').textContent = sum;
  let h = '';
  if (u.error) h += `<div class="wl-row ERR"><span class="wl-ts">更新</span>${esc(u.error)}</div>`;
  if (u.last_result && typeof u.last_result === 'object'){
    const lr = u.last_result;
    const bits = [];
    if (lr.seasons != null) bits.push(`賽季檔 ${lr.seasons} 個`);
    if (lr.odds != null) bits.push(`補盤口 ${lr.odds} 場${lr.odds_fail ? `（敗 ${lr.odds_fail}）` : ''}`);
    if (lr.odds_refresh) bits.push(`刷新盤口 ${lr.odds_refresh}`);
    if (lr.odds_fill) bits.push(`補爬盤口 ${lr.odds_fill}`);
    if (bits.length) h += `<div class="wl-row"><span class="wl-ts">上次更新</span>${bits.map(esc).join('｜')}</div>`;
  }
  h += (d.log || []).map(r =>
    `<div class="wl-row ${esc(r.level || '')}"><span class="wl-ts">${esc((r.ts || '').slice(5, 19))}</span>${esc(r.msg || '').replace(/\n/g, '<br>')}</div>`
  ).join('') || '<div class="note">暫無記錄</div>';
  $('#workLog').innerHTML = h;
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
    else if (w.last) t += `｜⚡上次窗口更新：成 ${w.ok || 0}／敗 ${w.fail || 0}`;
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
    fwck7: () => loadFwCheck('7'),
    fw8: () => loadFwGrid('8'),
    fwck8: () => loadFwCheck('8'),
    fw12: () => loadFwGrid('12'),
    fwck12: () => loadFwCheck('12'),
    review: () => loadReview(),
    picks: () => loadPicksPage(),
    featlog: () => loadFeatlog(),
    v1: () => loadV1(),
    settings: () => loadSettings(),
    home: () => loadHome()}[pg] || (() => {}))();
  window.scrollTo(0, 0);
}
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('.tab');
  if (b && b.dataset.pg) goto(b.dataset.pg);
});

/* ---------- 主頁 ---------- */
let homeData = {upcoming: [], played: []};
async function loadHome(){
  $('#homeList').innerHTML = '<div class="note">載入中…</div>';
  const hours = $('#hoursSel').value;
  try {
    const d = await jget('/api/v3/list?hours=' + hours);
    homeData = d;
    jget('/api/lgpred').then(p => { lgPred = p.predictions || {}; renderHome(); })
      .catch(() => {});
    renderHome();
    fillCheckSelect($('#ckMatch'), '— 揀場次 —');
    checkLgAlerts();
  } catch (e) {
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
function _stateTag(st){
  if (st === 'live') return '<span class="tag live">進行中</span>';
  if (st === 'finished') return '<span class="tag dim">完場</span>';
  return '';
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
    $('#betMeta').textContent =
      `${(preset.ko || '').slice(5, 16)}　${preset.lg}　${preset.h} vs ${preset.a}`;
    $('#betMsg').textContent = '';
    $('#betAmt').value = localStorage.getItem('bet_amt') || '';
    $('#betWater').value = localStorage.getItem('bet_water') || '';
    /* 攞該場尾盤做基準盤（冇就初盤），生出 ±0.25 三個盤 × 上/下盤 共六選項 */
    try {
      const r = await jget('/api/v3/target?id=' + preset.id);
      const t = r && r.target;
      const c = t && (t.close || t.init);
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
      return `<label><input type="radio" name="betline" value="${v}|${side}"${sel}>` +
             `<span><b>${side === 'up' ? '上盤' : '下盤'}（${esc(t)}）</b><br>` +
             `${esc(fmtLine(v))}</span></label>`;
    };
    $('#betLines').innerHTML =
      `<div class="bet-sec-t">上盤（讓球方；平手＝主隊）</div><div class="bet-lines">` +
      opts.map(v => optHTML(v, 'up')).join('') + `</div>` +
      `<div class="bet-sec-t">下盤（受讓方；平手＝客隊）</div><div class="bet-lines">` +
      opts.map(v => optHTML(v, 'down')).join('') + `</div>`;
    $('#betModal').classList.add('on');
    setTimeout(() => { if (!$('#betAmt').value) $('#betAmt').focus(); }, 80);
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
    const amt = $('#betAmt').value.trim();
    const { v, side } = this.sel();
    const core = `${dm} ${m.lg} ${m.h} vs ${m.a} ${fmtLine(v)} ` +
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
$('#betShareNoW').onclick = () => {
  if (!$('#betAmt').value.trim()) { toast('請先輸入金額'); $('#betAmt').focus(); return; }
  shareText(BetSlip.txt(false));
  BetSlip.close();
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
function _mrow(m, playedSec){
  const pc = window.PhoneCrawl ? PhoneCrawl.lineFor(m.id) : null;
  const pcMark = pc ? (pc.agree === true ? ' <span style="color:var(--up);font-size:10px">✓同本機</span>'
                      : (pc.agree === false ? ' <span style="color:var(--down);font-size:10px" title="手機直爬同本機數據唔一致——分析/推算以本機（雲端）為準">⚠唔同·本機為準</span>' : '')) : '';
  const line = pc
    ? `<div class="ln"><b>${esc(fmtLine(pc.line))}</b><span class="tag" style="font-size:10px;color:var(--gold2)">📱直爬</span>${pcMark}<br>主${pc.ho.toFixed(2)}/客${pc.ao.toFixed(2)}</div>`
    : (m.line ? `<div class="ln"><b>${esc(m.line.line)}</b>${m.line.src === 'crown' ? '<span class="tag dim" style="font-size:10px">Crown</span>' : ''}<br>主${m.line.ho != null ? m.line.ho.toFixed(2) : '—'}/客${m.line.ao != null ? m.line.ao.toFixed(2) : '—'}</div>` : '<div class="ln">無盤</div>');
  const od = playedSec ? '' : ((m.has_odds || (m.line && m.line.src === 'crown'))
    ? `<span class="od has">已有賠率</span>`
    : '<span class="od">未獲取賠率</span>');
  const at = pc
    ? `<span class="od at" title="手機直爬最後更新：${esc(pc.at)}（裝置 ${esc(window.PhoneCrawl.device)}）">📱 ${esc(pc.at.slice(11, 16))}</span>`
    : (m.odds_at ? `<span class="od at" title="賠率/盤口最後更新：${esc(m.odds_at)}">🕒 ${esc(fmtAt(m.odds_at))}</span>` : '');
  const rfb = (playedSec && m.score) ? '' : `<button class="rfb" data-fb="${m.id}" data-ko="${esc(m.kickoff || '')}" title="即時更新呢場最新賠率同盤口">⟳</button>`;
  const sc = m.score ? `<span class="sc">${esc(m.score)}</span>` : '';
  const pr = (!playedSec && lgPred[m.id]) ? _lgPredBadge(m, lgPred[m.id]) : '';
  return `<div class="mrow" data-mid="${m.id}">
    <span class="ko">${esc((m.kickoff || '').slice(5, 16))}</span>
    <span class="lg">${esc(m.league)}</span>
    <span class="tm">${esc(rn(m.home, m.rank_home))} <span class="r">vs</span> ${esc(rn(m.away, m.rank_away))}${pr}</span>
    ${_stateTag(m.state)}${sc}${line}${od}${at}${rfb}${BetSlip.btn(m)}</div>`;
}
/* 聯賽預測：主頁球隊旁展示方向（lgPred = /api/lgpred 結果） */
let lgPred = {};
function _lgPredTeam(m, dir){
  const gv = m.line && m.line.giver;
  if (dir === 'up') return gv === 'away' ? m.away : m.home;
  return gv === 'away' ? m.home : m.away;
}
function _lgPredBadge(m, p){
  const team = _lgPredTeam(m, p.direction);
  const best = (p.rules || [])[0];
  const rate = best ? Math.round(100 * (best.rate || 0)) : null;
  const n = best ? best.n : 0;
  const cls = p.direction === 'up' ? 'r-up' : 'r-down';
  const title = (p.rules || []).map(r =>
    `${r.desc}：${r.direction === 'up' ? '上' : '下'}${Math.round(100 * (r.rate || 0))}%（${r.n}場）`).join('\n');
  return ` <span class="tag lgpred ${cls}" title="${esc(title)}">🎯${p.direction === 'up' ? '上' : '下'}｜${esc(team)}${rate != null ? ` ${rate}%・${n}場` : ''}</span>`;
}
function renderHome(){
  let h = `<div class="day-h">🏟 即將開賽（${homeData.upcoming.length} 場）</div>`;
  if (!homeData.upcoming.length) h += '<div class="note">呢個時段冇即將開賽嘅賽事。</div>';
  let lastDay = null;
  for (const m of homeData.upcoming) {
    const d = (m.kickoff || '').slice(0, 10);
    if (d !== lastDay) { h += `<div class="day-h" style="background:#141a24;color:var(--dim);border-left-color:var(--accent)">📅 ${esc(d)}</div>`; lastDay = d; }
    h += _mrow(m, false);
  }
  h += `<div class="day-h">📼 已開賽（最近 120 小時・${homeData.played.length} 場・按日期分類，撳入去照睇 45 項分析）</div>`;
  lastDay = null;
  for (const m of homeData.played) {
    const d = (m.kickoff || '').slice(0, 10);
    if (d !== lastDay) { h += `<div class="day-h" style="background:#141a24;color:var(--dim);border-left-color:var(--accent)">📅 ${esc(d)}</div>`; lastDay = d; }
    h += _mrow(m, true);
  }
  $('#homeList').innerHTML = h;
  $$('#homeList .mrow').forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
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
    for (const m of list) {
      try {
        const r = await jpost('/api/fetch', {id: m.id});
        if (r.cached) cached++; else r.ok ? ok++ : fail++;
      } catch (e) { fail++; }
      $('#updInfo').textContent = `獲取賠率中 ${ok + fail + cached}/${list.length}…`;
    }
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
$('#btnOddsNow').onclick = async function(){
  if (this.disabled) return;
  if (window.PhoneCrawl && PhoneCrawl.modeOn) { phoneBatchAll(this); return; }
  this.disabled = true;
  try {
    const r = await jpost('/api/fetch-batch', {hours: +($('#hoursSel').value || 0)});
    if (r.error && !r.running) { toast(r.error); this.disabled = false; return; }
    pollFbatch();
  } catch (e) { toast('即時更新失敗：' + e.message); this.disabled = false; }
};
async function phoneBatchAll(btn){
  btn.disabled = true;
  /* 即將開賽全部 ＋ 進行中（已開賽未有賽果）——後者尾盤即最新參考盤 */
  const list = homeData.upcoming.concat(homeData.played.filter(m => !m.score));
  if (!list.length) { toast('冇可更新場次'); btn.disabled = false; return; }
  $('#updInfo').textContent =
    `📱 裝置 ${PhoneCrawl.device} 開始直爬 ${list.length} 場（手機網絡 → titan007）…`;
  try {
    const r = await PhoneCrawl.crawlAll(list, st => {
      $('#updInfo').textContent =
        `📱 手機直爬緊 ${st.done} 場（✓${st.ok} ✗${st.fail} 未開盤${st.noOdds}）…`;
    }, m => m.line || null);
    $('#updInfo').textContent =
      `📱 手機直爬完成：更新 ${r.ok}｜失敗 ${r.fail}｜未開盤 ${r.noOdds}——` +
      `已即時顯示＋同步雲端（裝置 ${PhoneCrawl.device}）`;
    loadHome();
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
    fbPoll = setTimeout(pollFbatch, 3000);
    return;
  }
  $('#btnOddsNow').disabled = false;
  if (st.error) { toast(st.error); return; }
  if (st.last) {
    $('#updInfo').textContent =
      `⚡ 即時更新完成（${st.last}）：成功 ${st.ok}｜失敗 ${st.fail}，已載入最新賠率`;
    loadHome();
  }
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

/* ---------- 場次分析（詳情頁） ---------- */
let curMatch = null;
async function openDetail(mid){
  goto('detail');
  $('#detBody').innerHTML = '<div class="note">載入中…</div>';
  $('#detMsg').textContent = '';
  let res;
  try { res = await jget('/api/v3/target?id=' + mid); }
  catch (e) { $('#detBody').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (res.error) { $('#detBody').innerHTML = '<div class="err">' + esc(res.error) + '</div>'; return; }
  curMatch = res.target;
  const t = res.target;
  setV3UD(t);
  const lb = (title, o) => `<div class="linebox"><div class="lb-t">${title}</div>` +
    (o ? `<div class="lb-v">${esc(o.line)}${o.src === 'crown' ? '<span class="tag dim" style="font-size:10px">Crown</span>' : ''}<br><small>主${o.ho != null ? o.ho.toFixed(2) : '—'}/客${o.ao != null ? o.ao.toFixed(2) : '—'}</small></div>` : '<div class="lb-v" style="color:var(--dim)">無數據</div>') + '</div>';
  const stateTxt = t.state === 'finished' ? '已完場' : t.state === 'live' ? '進行中' : '未開賽';
  let h = `<div class="tcard">
    <h2>${esc(rn(t.home, t.rank_home))} <span style="color:var(--dim)">vs</span> ${esc(rn(t.away, t.rank_away))}</h2>
    <div class="meta">${esc(t.league)}｜${esc(t.category || '')}　${esc(t.kickoff)}　<span class="tag ${t.state === 'live' ? 'live' : 'dim'}">${stateTxt}</span>${t.score ? `　<b class="sc" style="font-size:18px">${esc(t.score)}</b>` : ''}　<span class="od at" title="賠率/盤口最後更新：${esc(t.odds_at || '')}">🕒 賠率更新 ${esc(t.odds_at ? fmtAt(t.odds_at) : '—')}</span></div>
    <div class="lines">
      ${lb('尾盤（檢查基準）', t.close)}${lb('初盤', t.init)}${lb('開賽前4小時', t.h4)}
      ${lb('開賽前30分鐘', t.h30)}${lb('開賽前15分鐘', t.h15)}${lb('開賽前10分鐘', t.h10)}${lb('開賽前5分鐘', t.h5)}
    </div>
    <div id="detLgPred"></div>${BetSlip.btn(t, '🎯 我想投注')}</div>
    ${_pickBar(t.id, 'top')}
    <div class="tcard" id="v3sec">
      <h2 style="color:var(--gold2)">⚽ V3 場次分析（45 項）</h2>
      <div id="v3Sum" class="note">精選W 檢查載入中…</div>
      <div id="v3Items"><div class="note">項目清單載入中…</div></div>
    </div>
    ${_pickBar(t.id, 'bottom')}
    <div class="tcard" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      ${BetSlip.btn(t, '🎯 我想投注')}
      <span class="hint">揀該場盤口±0.25｜入金額｜撳分享→入水位→再撳分享出文字</span>
    </div>`;
  $('#detBody').innerHTML = h;
  wirePickBars(t.id);
  initV3Section(t.id);
  renderDetLgPred(t.id);
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
function _pickBar(mid, pos){
  return `<div class="tcard pk-bar" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <b>我的選擇${pos === 'top' ? '（第1項之上）' : '（最後1項之下）'}：</b>
    <button class="btn pk up" data-pk="up">上盤（${esc(_un())}）</button>
    <button class="btn pk down" data-pk="down">下盤（${esc(_dn())}）</button>
    <span class="hint" id="pkMsg_${pos}"></span>
  </div>`;
}
function wirePickBars(mid){
  $$('#detBody .pk-bar').forEach(bar => {
    bar.querySelectorAll('.pk').forEach(b => b.onclick = () => doPick(mid, b.dataset.pk, bar));
  });
  updatePkButtons(mid);
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
function updatePkButtons(mid){
  const c = picksMap[mid] || '';
  $$('#detBody .pk').forEach(b => b.classList.toggle('accent', b.dataset.pk === c));
}
async function doPick(mid, choice, bar){
  const r = await jpost('/api/pick', {id: mid, choice});
  const msg = bar ? bar.querySelector('.hint') : null;
  if (r.ok) {
    picksMap[mid] = choice;
    if (msg) msg.textContent = `已記低：${choice === 'up' ? '上盤' : '下盤'}（${esc(choice === 'up' ? _un() : _dn())}）（永不刪除）`;
    updatePkButtons(mid);
    refreshPicksMap();
  } else if (msg) {
    msg.textContent = r.error || '記錄失敗';
  }
}
const RES_TXT = {W: '<b class="r-up">✓ 贏</b>', L: '<b class="r-down">✗ 輸</b>', P: '<b>➖ 走</b>', X: '<span style="color:var(--dim)">無法判定</span>'};
async function loadPicksPage(){
  $('#pkList').innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget('/api/picks/full'); }
  catch (e) { $('#pkList').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats;
  $('#pkStats').innerHTML = `<div class="statbar">
    <span>總場數 <b>${s.total}</b></span><span>未開賽 <b>${s.pending}</b></span>
    <span>贏 <b class="r-up">${s.wins}</b></span><span>輸 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>勝出率 <b>${pct(s.win_rate)}</b>（贏÷(贏+輸)，走唔計）</span></div>`;
  const card = p => {
    const res = p.played ? (RES_TXT[p.result] || '<span style="color:var(--dim)">待結算</span>') : '<span style="color:var(--dim)">未開賽</span>';
    const pud = p.line ? (p.line.indexOf('主讓') === 0 ? {up: p.home, down: p.away, push: false}
      : p.line.indexOf('客讓') === 0 ? {up: p.away, down: p.home, push: false}
      : {up: p.home, down: p.away, push: true}) : null;
    return `<div class="fcard" data-mid="${p.id}">
      <div class="f-top">
        <span class="f-time">${esc((p.kickoff || '').slice(5, 16))}　${esc(p.league)}</span>
        <span class="f-teams">${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}</span>
        ${p.score ? `<span class="f-score">${esc(p.score)}</span>` : ''}
        <span class="tag ${p.choice}">${p.choice === 'up' ? '上盤' : '下盤'}＝${pud ? esc(pud[p.choice]) : '—'}</span>
        <span>${res}</span>
        <span class="f-btns">
          <button class="btn pk-repick">⇄ 照揑</button>
        </span>
      </div>
      <div class="gaprow"><span class="lab">本場</span>上盤＝<b>${pud ? esc(pud.up) : '—'}</b>｜下盤＝<b>${pud ? esc(pud.down) : '—'}</b>${pud && pud.push ? '（平手盤：上盤＝平手主隊）' : ''}</div>
      <div class="gaprow"><span class="lab">揀時</span>${esc(p.pick_line || '—')} ${esc(p.pick_odds || '')}</div>
      <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
    </div>`;
  };
  let h = '';
  if (d.pending.length) {
    h += `<div class="day-h">🔜 未開賽（${d.pending.length} 場・順開賽時間・永久保留）</div>` + d.pending.map(card).join('');
  }
  if (d.played.length) {
    h += `<div class="day-h">📼 已開賽（${d.played.length} 場・永久保留，最新排先）</div>` + d.played.map(card).join('');
  }
  if (!h) h = '<div class="note">仲未揀過任何場次。入場次分析頁「我的選擇」撳上/下盤即記低。</div>';
  $('#pkList').innerHTML = h;
  $$('#pkList .fcard').forEach(c => {
    const mid = +c.dataset.mid;
    c.querySelector('.pk-repick').onclick = async ev => {
      ev.stopPropagation();
      const ch = picksMap[mid];
      if (!ch) { toast('搵唔到原先選擇'); return; }
      await jpost('/api/pick', {id: mid, choice: ch});
      toast('已照揑再記一次'); loadPicksPage(); refreshPicksMap();
    };
    c.onclick = () => openDetail(mid);
  });
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
    h += `<details style="margin:4px 0"><summary><span class="sum-t">${esc(c.tri)} × ${esc(c.scope)}</span><span class="sub">${c.n} 場</span></summary><div class="body">${v3Water12Tbl(c, curZ, zones12)}</div></details>`;
  }
  return h;
}
function v3TriTxt(tri) {
  if (!tri) return '';
  return `<table class="ck-table"><thead><tr><th>對比口徑</th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>` +
    [['same', '上賽同主客場（原盤直比）'], ['swap', '上賽主客場對調（互換對比盤）'], ['all', '綜合全部（同主客＋對調）']]
      .map(([k, lab]) => {
        const o = tri[k];
        return `<tr><td>${lab}</td><td>${o ? o.n : 0}</td><td class="${o ? _winCls(o.up_r, o.down_r, 'u') : ''}">${o ? pct(o.up_r) : '—'}</td><td class="${o ? _winCls(o.up_r, o.down_r, 'd') : ''}">${o ? pct(o.down_r) : '—'}</td><td>${o ? pct(o.push_r) : '—'}</td></tr>`;
      }).join('') + '</tbody></table>';
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
      el.innerHTML = '<div class="note" style="font-size:12px">命中率載入中…</div>';
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
      ${p.score ? `<span class="f-score">${esc(p.score)}</span>` : ''}
      <span class="tag ${p.direction}">${dName}</span>
      <span>${res}</span>
      <span class="f-btns">
        <button class="btn ft-refresh">⟳ 重新整理</button>
        <button class="btn ft-share">⇗ 分享</button>${BetSlip.btn(p)}
      </span>
    </div>
    <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
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
    await jpost('/api/v3/featured/scan', {});
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
      <div class="gaprow"><span class="lab">本場</span>上盤＝<b>${esc(ud.up)}</b>｜下盤＝<b>${esc(ud.down)}</b>${ud.push ? '（平手盤：上盤＝平手主隊）' : ''}</div>
      <div class="grid6">${_fwgCellsHTML(p.cells, def.cells)}</div>
      ${_fwgStrict(p.strict)}
      <div class="hrb" data-kind="fw${g}" data-mid="${p.id}"></div>
      <details style="margin-top:6px"><summary><span class="sum-t">⚽ 場次分析 45 項（全資料）</span></summary><div class="body v3-fullitems"><div class="note">載入中…</div></div></details>
    </div></div>`;
}
async function loadFwGrid(g){
  const listEl = $('#fw' + g + 'List');
  listEl.innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget('/api/fwgrid/full?g=' + g); }
  catch (e) { listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
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
  $('#rvList').innerHTML = '<div class="note">載入中…</div>';
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
  $('#flList').innerHTML = '<div class="note">載入中…</div>';
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
function _v1Card(p, z){
  const b = p.brief || {};
  const dName = p.direction === 'up' ? '上盤' : '下盤';
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
      ${p.score ? `<span class="f-score">${esc(p.score)}</span>` : ''}
      <span class="tag ${p.direction}">${dName}</span>
      ${lts.length ? `<span class="tag lt">字頭 ${lts.join(' ')}</span>` : ''}
      <span>${res}</span>
      <span class="f-btns">
        <button class="btn ft-refresh">⟳ 重新整理</button>
        <button class="btn ft-share">⇗ 分享</button>${BetSlip.btn(p)}
      </span>
    </div>
    <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
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
  listEl.innerHTML = '<div class="note">載入中…</div>';
  zlistEl.innerHTML = '<div class="note">載入中…</div>';
  v2El.innerHTML = '<div class="note">載入中…</div>';
  v2zEl.innerHTML = '<div class="note">載入中…</div>';
  let d, dz;
  try {
    [d, dz] = await Promise.all([
      jget('/api/v1/featured/full'), jget('/api/v1/featured/full?z=1')]);
  } catch (e) {
    listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>';
    return;
  }
  const scan = d.scan || {};
  $('#v1ScanInfo').textContent = scan.running
    ? `掃描中 ${scan.done}/${scan.total}…`
    : (scan.last ? `上次掃描：${scan.last}｜新增 ${scan.added || 0}（Z ${scan.added_z || 0}）` : '');
  $('#v1Stats').innerHTML = _v1StatsBar(d.stats, false, 'V1精選') + V1_MAP_NOTE;
  $('#v1zStats').innerHTML = _v1StatsBar(dz.stats, true, 'V1精選Z');
  const render = (dd, el, z, tag, refreshUrl) => {
    let h = '';
    if (dd.pending.length) h += `<div class="day-h">🔜 ${tag}${z ? '精選Z' : '精選'}・未開賽（${dd.pending.length} 場・順開賽時間）</div>` + dd.pending.map(p => _v1Card(p, z)).join('');
    if (dd.played.length) h += `<div class="day-h">📼 ${tag}${z ? '精選Z' : '精選'}・已完場（${dd.played.length} 場・最新排先，永久保留）</div>` + dd.played.map(p => _v1Card(p, z)).join('');
    if (!h) h = `<div class="note">暫無 ${tag}${z ? '精選Z' : '精選'}場次。撳「🔍 重新掃描」即刻篩過。</div>`;
    el.innerHTML = h;
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
      render(d2, v2El, false, 'V2', '/api/featured/refresh');
      render(d2z, v2zEl, true, 'V2', '/api/featured/refresh');
    })
    .catch(e => { v2El.innerHTML = '<div class="err">V2 載入失敗：' + esc(String(e)) + '</div>'; });
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
    await jpost('/api/v1/featured/scan', {});
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
    await jpost('/api/featured/scan', {});
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
  if (s.running) {
    info.textContent = `掃描中 ${s.done}/${s.total}…`;
    _scanTimers[key] = setTimeout(() => pollScanJob(key, url, btnSel, infoSel, onDone, fmtDone), 5000);
    return;
  }
  const b = $(btnSel);
  if (b) b.disabled = false;
  info.textContent = (fmtDone || (x => `完成｜新增 ${x.added || 0}${x.error ? '｜錯誤：' + x.error : ''}`))(s);
  onDone();
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
  if (ok) {
    loadHome();
    refreshPicksMap();
    refreshUpdInfo();
    refreshConnChip();
    $('#btnRepair').onclick = repairConn;
    $('#workLogBox').addEventListener('toggle', () => { refreshWorkLog(); });
    // 每 2 分鐘靜靜哋更新計數
    setInterval(refreshPicksMap, 120000);
    setInterval(() => { refreshUpdInfo(); refreshConnChip(); refreshWorkLog(); }, 30000);
    // 主頁／事後回查／過往紀錄自動刷新（完場狀態、新場次、新結算會自己更新，2026-09-29）
    setInterval(() => {
      if (document.hidden) return;
      if (curPage === 'home' && !_updBusy) loadHome();
      else if (curPage === 'review') loadReview();
      else if (curPage === 'featlog') loadFeatlog();
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
