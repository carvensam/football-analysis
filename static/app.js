/* FootballAnalysis V2 前端（2026-09-26 全面重建） */
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
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(url, opt);
      if (r.status >= 500 && i < (tries ?? 2)) { await new Promise(x => setTimeout(x, 1500)); continue; }
      return await r.json();
    } catch (e) {
      if (i >= (tries ?? 2)) throw e;
      await new Promise(x => setTimeout(x, 1500));
    }
  }
}
const jget = url => _jf(url, {}, 2);
async function jpost(url, body){
  return _jf(url, {method:'POST', headers:{'Content-Type':'application/json'},
                   body: JSON.stringify(body || {})}, 1);
}

/* ---------- 狀態 ---------- */
async function pollReady(){
  for (let i = 0; i < 90; i++) {
    try {
      const r = await jget('/api/ready');
      if (r.ready) { setStatus(`✓ 數據池就緒｜v${r.version}`, false); return true; }
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
async function refreshUpdInfo(){
  try {
    const s = await jget('/api/update-status');
    const w = await jget('/api/update-window-status');
    let t = '';
    if (s.running) t += `⟳更新中：${s.phase || ''} `;
    else if (s.last_done) t += `上次更新：${s.last_done_ago != null ? Math.round(s.last_done_ago/60) + '分鐘前' : ''}`;
    if (w.running) t += `｜⚡${w.window_name}更新中 ${w.done}/${w.total}`;
    $('#updInfo').textContent = t;
    $$('#btnUpdate,#btnWin24,#btnWin30,#btnWin10').forEach(b => {
      b.disabled = !!(s.running || w.running);
    });
    return s.running || w.running;
  } catch (e) { return false; }
}

/* ---------- 分頁 ---------- */
const PAGES = ['home','feat','featz','check','picks','results','featlog','v1','settings','detail'];
let curPage = 'home';
function goto(pg){
  curPage = pg;
  $$('.pg').forEach(el => el.classList.remove('on'));
  $('#pg-' + pg).classList.add('on');
  $$('#tabs .tab').forEach(b => b.classList.toggle('on', b.dataset.pg === pg));
  ({feat: () => loadFeatured(false),
    featz: () => loadFeatured(true),
    check: () => loadCheck(),
    picks: () => loadPicksPage(),
    results: () => loadResults(),
    featlog: () => loadFeatlog(),
    v1: () => loadV1(),
    settings: () => loadSettings(),
    home: () => loadHome()}[pg] || (() => {}))();
  window.scrollTo(0, 0);
}
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('.tab');
  if (b) goto(b.dataset.pg);
});

/* ---------- 主頁 ---------- */
let homeData = {upcoming: [], played: []};
async function loadHome(){
  $('#homeList').innerHTML = '<div class="note">載入中…</div>';
  const hours = $('#hoursSel').value;
  try {
    const d = await jget('/api/upcoming?hours=' + hours);
    homeData = d;
    renderHome();
  } catch (e) {
    $('#homeList').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>';
  }
}
function _mrow(m, played){
  const line = m.line ? `<div class="ln"><b>${esc(m.line.line)}</b><br>主${m.line.ho != null ? m.line.ho.toFixed(2) : '—'}/客${m.line.ao != null ? m.line.ao.toFixed(2) : '—'}</div>` : '<div class="ln">無盤</div>';
  const od = played ? '' : (m.has_odds
    ? `<span class="od has">已有賠率</span>`
    : '<span class="od">未獲取賠率</span>');
  const sc = m.score ? `<span class="sc">${esc(m.score)}</span>` : '';
  return `<div class="mrow" data-mid="${m.id}">
    <span class="ko">${esc((m.kickoff || '').slice(5, 16))}</span>
    <span class="lg">${esc(m.league)}</span>
    <span class="tm">${esc(rn(m.home, m.rank_home))} <span class="r">vs</span> ${esc(rn(m.away, m.rank_away))}</span>
    ${sc}${line}${od}</div>`;
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
  h += `<div class="day-h">📼 已開賽（最近 120 小時・${homeData.played.length} 場）</div>`;
  lastDay = null;
  for (const m of homeData.played) {
    const d = (m.kickoff || '').slice(0, 10);
    if (d !== lastDay) { h += `<div class="day-h" style="background:#141a24;color:var(--dim);border-left-color:var(--accent)">📅 ${esc(d)}</div>`; lastDay = d; }
    h += _mrow(m, true);
  }
  $('#homeList').innerHTML = h;
  $$('#homeList .mrow').forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
}
$('#hoursSel').onchange = loadHome;
$('#btnReload').onclick = loadHome;
$('#btnFetchAll').onclick = async function(){
  this.disabled = true;
  const list = homeData.upcoming.filter(m => !m.has_odds);
  let ok = 0, fail = 0;
  $('#updInfo').textContent = `獲取賠率中 0/${list.length}…`;
  for (const m of list) {
    try {
      const r = await jpost('/api/fetch', {id: m.id});
      r.ok ? ok++ : fail++;
    } catch (e) { fail++; }
    $('#updInfo').textContent = `獲取賠率中 ${ok + fail}/${list.length}…`;
  }
  $('#updInfo').textContent = `獲取完成：成功 ${ok}｜失敗 ${fail}`;
  this.disabled = false;
  loadHome();
};
$('#btnUpdate').onclick = async function(){
  this.disabled = true;
  await jpost('/api/update', {});
  pollUpdateLoop();
};
$('#btnWin24').onclick = () => winUpdate('24h');
$('#btnWin30').onclick = () => winUpdate('30m');
$('#btnWin10').onclick = () => winUpdate('10m');
async function winUpdate(win){
  const r = await jpost('/api/update-window', {window: win});
  if (r.error) toast(r.error);
  pollUpdateLoop();
}
let updPoll = null;
async function pollUpdateLoop(){
  clearTimeout(updPoll);
  const busy = await refreshUpdInfo();
  if (busy) { updPoll = setTimeout(pollUpdateLoop, 4000); return; }
  loadHome();
  if (curPage === 'feat') loadFeatured(false);
  if (curPage === 'featz') loadFeatured(true);
  if (curPage === 'v1') loadV1();
}

/* ---------- 詳情頁 ---------- */
let curMatch = null;
async function openDetail(mid){
  goto('detail');
  $('#detBody').innerHTML = '<div class="note">載入中…</div>';
  $('#detMsg').textContent = '';
  let res;
  try { res = await jget('/api/screen?id=' + mid); }
  catch (e) { $('#detBody').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (res.error) { $('#detBody').innerHTML = '<div class="err">' + esc(res.error) + '</div>'; return; }
  curMatch = res.target;
  const t = res.target;
  const lb = (title, o) => `<div class="linebox"><div class="lb-t">${title}</div>` +
    (o ? `<div class="lb-v">${esc(o.line)}<br><small>主${o.ho != null ? o.ho.toFixed(2) : '—'}/客${o.ao != null ? o.ao.toFixed(2) : '—'}</small></div>` : '<div class="lb-v" style="color:var(--dim)">無數據</div>') + '</div>';
  let h = `<div class="tcard">
    <h2>${esc(rn(t.home, t.rank_home))} <span style="color:var(--dim)">vs</span> ${esc(rn(t.away, t.rank_away))}</h2>
    <div class="meta">${esc(t.league)}｜${esc(t.category || '')}　${esc(t.kickoff)}</div>
    <div class="lines">
      ${lb('尾盤（檢查基準）', t.close)}${lb('初盤', t.init)}${lb('開賽前4小時', t.h4)}
      ${lb('開賽前30分鐘', t.h30)}${lb('開賽前15分鐘', t.h15)}${lb('開賽前10分鐘', t.h10)}${lb('開賽前5分鐘', t.h5)}
    </div></div>
    <div class="tcard" id="v2sec">
      <h2 style="color:var(--gold2)">⚽ V2 分析（45 項）</h2>
      <div id="v2Letters" class="note">精選字頭檢查載入中…</div>
      <div id="v2Items"><div class="note">項目清單載入中…</div></div>
    </div>
    <div class="tcard pk-bar" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <b>我的選擇：</b>
      <button class="btn pk up" data-pk="up">上盤</button>
      <button class="btn pk down" data-pk="down">下盤</button>
      <span class="hint" id="pkMsg"></span>
    </div>`;
  $('#detBody').innerHTML = h;
  updatePkButtons(t.id);
  $$('#detBody .pk').forEach(b => b.onclick = () => doPick(t.id, b.dataset.pk));
  initV2Section(t.id);
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
  $$('#detBody .pk').forEach(b => {
    b.classList.toggle('accent', b.dataset.pk === c);
  });
}
async function doPick(mid, choice){
  const r = await jpost('/api/pick', {id: mid, choice});
  if (r.ok) {
    picksMap[mid] = choice;
    $('#pkMsg').textContent = `已記低：${choice === 'up' ? '上盤' : '下盤'}`;
    updatePkButtons(mid);
    refreshPicksMap();
  } else {
    $('#pkMsg').textContent = r.error || '記錄失敗';
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
    return `<div class="fcard" data-mid="${p.id}">
      <div class="f-top">
        <span class="f-time">${esc((p.kickoff || '').slice(5, 16))}　${esc(p.league)}</span>
        <span class="f-teams">${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}</span>
        ${p.score ? `<span class="f-score">${esc(p.score)}</span>` : ''}
        <span class="tag ${p.choice}">${p.choice === 'up' ? '上盤' : '下盤'}</span>
        <span>${res}</span>
        <span class="f-btns">
          <button class="btn pk-repick">⇄ 照揑</button>
          <button class="btn pk-del">🗑</button>
        </span>
      </div>
      <div class="gaprow"><span class="lab">揀時</span>${esc(p.pick_line || '—')} ${esc(p.pick_odds || '')}</div>
      <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
    </div>`;
  };
  let h = '';
  if (d.pending.length) {
    h += `<div class="day-h">🔜 未開賽（${d.pending.length} 場・順開賽時間）</div>` + d.pending.map(card).join('');
  }
  if (d.played.length) {
    h += `<div class="day-h">📼 已開賽（${d.played.length} 場・永久保留，最新排先）</div>` + d.played.map(card).join('');
  }
  if (!h) h = '<div class="note">仲未揀過任何場次。入場次詳情頁底部「我的選擇」撳上/下盤即記低。</div>';
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
    c.querySelector('.pk-del').onclick = async ev => {
      ev.stopPropagation();
      await jpost('/api/pick/delete', {id: mid});
      delete picksMap[mid]; loadPicksPage(); refreshPicksMap();
    };
    c.onclick = () => openDetail(mid);
  });
}


/* ---------- 精選 / 精選Z ---------- */
function _ocTxt(o){ return o ? `上${pct(o.up_r)} 下${pct(o.down_r)} 走${pct(o.push_r)}｜${o.n}場` : '—'; }
function _pairTxt(b){ return b ? `全庫 ${_ocTxt(b.all)}${b.league ? `<br><span style="opacity:.75">同聯賽 ${_ocTxt(b.league)}</span>` : ''}` : '不適用'; }
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
async function loadFeatured(z){
  const listEl = $(z ? '#ftzList' : '#ftList');
  listEl.innerHTML = '<div class="note">載入中…</div>';
  let d;
  try { d = await jget('/api/featured/full' + (z ? '?z=1' : '')); }
  catch (e) { listEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats;
  $(z ? '#ftzCnt' : '#ftCnt').textContent = s.pending ? `(${s.pending})` : '';
  $(z ? '#ftzStats' : '#ftStats').innerHTML = `<div class="statbar">
    <span>未開賽 <b>${s.pending}</b></span><span>已完場 <b>${s.played}</b></span>
    <span>贏 <b class="r-up">${s.wins}</b></span><span>輸 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>
    <div class="note">入選規則（V2 十六字頭）：任一字頭 A–P 合格即入選——①項目2（同4h及尾盤）格X有方向 ②項目13（對上對賽尾盤@尾盤）格X同方向（①②兩項贏盤率各自獨立展示）③項目30同盤同方向&gt;49.99% ④項目33同盤同方向&gt;49.99%。${z ? '精選Z＝只有同主隊字頭（I–P）合格。' : ''}卡片「字頭」章＝合格字頭。</div>`;
  const scan = d.scan || {};
  $(z ? '#ftzScanInfo' : '#ftScanInfo').textContent = scan.running
    ? `掃描中 ${scan.done}/${scan.total}…`
    : (scan.last ? `上次掃描：${scan.last}｜新增 ${scan.added || 0}` : '');
  const card = p => {
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
    const i18v = !i18 ? '不適用' : `全庫 ${_ocTxt(i18.all)}<br><span style="opacity:.75">同聯賽 ${_ocTxt(i18.lg)}</span>`;
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
          <button class="btn ft-share">⇗ 分享</button>
        </span>
      </div>
      <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
      <div class="grid6">
        <div class="gi"><div class="t">① 同初盤及尾盤</div><div class="v">${_pairTxt(b.i1)}</div></div>
        <div class="gi"><div class="t">⑤${z ? 'Z' : ''} 對上對賽尾盤${z ? '（同主客）' : ''}</div><div class="v">${_pairTxt(z ? b.i5z : b.i5)}</div></div>
        <div class="gi"><div class="t">⑧ 主客入失球±3</div><div class="v">${_pairTxt(b.i8)}</div></div>
        <div class="gi"><div class="t">⑫ 主場客場排名±2</div><div class="v">${i12v}</div></div>
        <div class="gi"><div class="t">⑮ 排名±2＋同尾盤</div><div class="v">${i15v}</div></div>
        <div class="gi"><div class="t">⑱ 排名差距±1＋同尾盤</div><div class="v">${i18v}</div></div>
      </div>
      <div class="gaprow"><span class="lab">⑭ 差距</span>${_gapTxt(b.g14)}</div>
      <div class="gaprow"><span class="lab">⑰ 差距</span>${_gapTxt(b.g17)}</div>
      <div class="gaprow"><span class="lab">✓ Check</span>${chkTxt}</div>
    </div>`;
  };
  let h = '';
  if (d.pending.length) h += `<div class="day-h">🔜 未開賽（${d.pending.length} 場・順開賽時間）</div>` + d.pending.map(card).join('');
  if (d.played.length) h += `<div class="day-h">📼 已完場（${d.played.length} 場・最新排先，永久保留）</div>` + d.played.map(card).join('');
  if (!h) h = `<div class="note">暫無${z ? '精選Z' : '精選'}場次。撳「🔍 重新掃描」即刻篩過。</div>`;
  listEl.innerHTML = h;
  [...listEl.querySelectorAll('.fcard')].forEach(c => {
    const mid = +c.dataset.mid;
    c.querySelector('.ft-refresh').onclick = async ev => {
      ev.stopPropagation();
      toast('重新整理中…');
      await jpost('/api/featured/refresh', {id: mid});
      toast('完成'); loadFeatured(z);
    };
    c.querySelector('.ft-share').onclick = async ev => {
      ev.stopPropagation();
      const p = [...d.pending, ...d.played].find(x => x.id === mid);
      if (p) shareText(ftShareText(p, z));
    };
    c.onclick = () => openDetail(mid);
  });
}
function ftShareText(p, z){
  const b = p.brief || {};
  const dName = p.direction === 'up' ? '上盤' : '下盤';
  const L = [];
  L.push(`【${z ? '精選Z' : '精選'}】${p.league} ${(p.kickoff || '').slice(5, 16)}`);
  L.push(`${p.home} vs ${p.away}${p.score ? '（' + p.score + '）' : ''}`);
  L.push(`方向：${dName}｜尾盤 ${p.line || '—'} ${p.odds || ''}`);
  if (b.i1 && b.i1.all) L.push(`① 上${pct(b.i1.all.up_r)} 下${pct(b.i1.all.down_r)}（${b.i1.all.n}場）`);
  if (b.g14) L.push(`⑭ 全庫最多【${b.g14.all && b.g14.all.mode ? b.g14.all.mode.line : ''}】${b.g14.all && b.g14.all.mode ? b.g14.all.mode.gap || '' : ''}｜50%【${b.g14.all && b.g14.all.d50 ? b.g14.all.d50.line : ''}】${b.g14.all && b.g14.all.d50 ? b.g14.all.d50.gap || '' : ''}`);
  if (b.g17) L.push(`⑰ 全庫最多【${b.g17.all && b.g17.all.mode ? b.g17.all.mode.line : ''}】${b.g17.all && b.g17.all.mode ? b.g17.all.mode.gap || '' : ''}｜50%【${b.g17.all && b.g17.all.d50 ? b.g17.all.d50.line : ''}】${b.g17.all && b.g17.all.d50 ? b.g17.all.d50.gap || '' : ''}`);
  const lts = (p.letters || {}).letters || [];
  if (lts.length) L.push(`合格字頭：${lts.join(' ')}`);
  if (p.result) L.push(`結果：${p.result === 'W' ? '✅命中' : p.result === 'L' ? '❌未中' : '➖走'}`);
  return L.join('\n');
}
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
$('#btnFtScan').onclick = async function(){
  this.disabled = true;
  await jpost('/api/featured/scan', {});
  pollFtScan(false);
};
$('#btnFtzScan').onclick = async function(){
  this.disabled = true;
  await jpost('/api/featured/scan', {});
  pollFtScan(true);
};
let ftScanTimer = null;
async function pollFtScan(z){
  clearTimeout(ftScanTimer);
  const s = await jget('/api/featured/scan-status');
  const info = $(z ? '#ftzScanInfo' : '#ftScanInfo');
  if (s.running) {
    info.textContent = `掃描中 ${s.done}/${s.total}…`;
    ftScanTimer = setTimeout(() => pollFtScan(z), 5000);
    return;
  }
  $(z ? '#btnFtzScan' : '#btnFtScan').disabled = false;
  info.textContent = `完成｜新增 ${s.added || 0}${s.added_z ? '（Z ' + s.added_z + '）' : ''}${s.error ? '｜錯誤：' + s.error : ''}`;
  loadFeatured(z);
}

/* ---------- 舊版本（V1 版面・V2 十六字頭規則逆向重塑） ---------- */
const V1_MAP_NOTE = `<div class="note">逆向重塑：呢頁嘅精選／精選Z<b>用嘅係 V2 而家嘅十六字頭規則</b>（同「精選」頁完全同一套準則揀場），
只係展示用返 V1 舊項目名。每格標明對應 V2 邊條規則：
① 同初盤及尾盤＝V2 項目1（字頭 gate ①用項目2 四小時版）｜
⑤ 對上對賽尾盤＝V2 項目13・純盤口（字頭 gate ②）；⑤Z 同主客＝字頭 I–P 同主隊範圍｜
⑧ 主客入失球±3＝V2 項目25／26｜
⑫ 主場客場排名±2＝V2 項目27｜
⑮ 排名±2＋同尾盤＝V2 項目27＋同尾盤條件｜
⑱ 排名差距±1＋同尾盤＝V2 項目32｜
⑭ 差距＝V2 項目30 分佈最多盤／項目33 最接近50%盤（字頭 gate ③④）｜
⑰ 差距＝V2 項目30／33・上次比賽樣本版｜
✓ Check＝V2 項目31 今賽比上賽深/淺/不變＋組合統計。</div>`;

function _v1Card(p, z){
  const b = p.brief || {};
  const dName = p.direction === 'up' ? '上盤' : '下盤';
  const res = !p.played ? '<span style="color:var(--dim)">未開賽</span>'
    : p.result === 'W' ? '<b class="r-up">✅ 命中</b>'
    : p.result === 'L' ? '<b class="r-down">❌ 未中</b>'
    : p.result === 'P' ? '<b>➖ 走盤</b>' : '<span style="color:var(--dim)">待結算</span>';
  const i12 = b.i12, i15 = b.i15, i18 = b.i18;
  const i12v = !i12 ? '不適用' : `n=${i12.n}` + (i12.top ? `<br>最多【${esc(i12.top.line || '')}】上${pct(i12.top.up_r)} 下${pct(i12.top.down_r)}` : '');
  const i15v = !i15 ? '不適用' : `n=${i15.n}` + (i15.zone ? `<br>今場水位【${esc(i15.zone.zone || '')}】上${pct(i15.zone.up_r)} 下${pct(i15.zone.down_r)} 走${pct(i15.zone.push_r)}` : '');
  const i18v = !i18 ? '不適用' : `全庫 ${_ocTxt(i18.all)}<br><span style="opacity:.75">同聯賽 ${_ocTxt(i18.lg)}</span>`;
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
      <span>${res}</span>
      <span class="f-btns">
        <button class="btn ft-refresh">⟳ 重新整理</button>
        <button class="btn ft-share">⇗ 分享</button>
      </span>
    </div>
    <div class="gaprow"><span class="lab">尾盤</span>${esc(p.line || '—')} ${esc(p.odds || '')}</div>
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

function _v1StatsBar(s, z){
  return `<div class="statbar">
    <span>${z ? 'V1精選Z' : 'V1精選'}：未開賽 <b>${s.pending}</b></span><span>已完場 <b>${s.played}</b></span>
    <span>贏 <b class="r-up">${s.wins}</b></span><span>輸 <b class="r-down">${s.losses}</b></span>
    <span>走 <b>${s.pushes}</b></span>
    <span>命中率 <b>${pct(s.hit_rate)}</b>（贏÷(贏+輸)）</span></div>`;
}

async function loadV1(){
  const listEl = $('#v1List'), zlistEl = $('#v1zList');
  listEl.innerHTML = '<div class="note">載入中…</div>';
  zlistEl.innerHTML = '<div class="note">載入中…</div>';
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
  $('#v1Stats').innerHTML = _v1StatsBar(d.stats, false) + V1_MAP_NOTE;
  $('#v1zStats').innerHTML = _v1StatsBar(dz.stats, true);

  const render = (dd, el, z) => {
    let h = '';
    if (dd.pending.length) h += `<div class="day-h">🔜 V1${z ? '精選Z' : '精選'}・未開賽（${dd.pending.length} 場・順開賽時間）</div>` + dd.pending.map(p => _v1Card(p, z)).join('');
    if (dd.played.length) h += `<div class="day-h">📼 V1${z ? '精選Z' : '精選'}・已完場（${dd.played.length} 場・最新排先，永久保留）</div>` + dd.played.map(p => _v1Card(p, z)).join('');
    if (!h) h = `<div class="note">暫無 V1${z ? '精選Z' : '精選'}場次。撳「🔍 重新掃描 V1」即刻篩過。</div>`;
    el.innerHTML = h;
    [...el.querySelectorAll('.fcard')].forEach(c => {
      const mid = +c.dataset.mid;
      c.querySelector('.ft-refresh').onclick = async ev => {
        ev.stopPropagation();
        toast('重新整理中…');
        await jpost('/api/v1/featured/refresh', {id: mid});
        toast('完成'); loadV1();
      };
      c.querySelector('.ft-share').onclick = async ev => {
        ev.stopPropagation();
        const p = [...dd.pending, ...dd.played].find(x => x.id === mid);
        if (p) shareText(ftShareText(p, z));
      };
      c.onclick = () => openDetail(mid);
    });
  };
  render(d, listEl, false);
  render(dz, zlistEl, true);
}
$('#btnV1Scan').onclick = async function(){
  this.disabled = true;
  await jpost('/api/v1/featured/scan', {});
  pollV1Scan();
};
let v1ScanTimer = null;
async function pollV1Scan(){
  clearTimeout(v1ScanTimer);
  const s = await jget('/api/v1/featured/scan-status');
  const info = $('#v1ScanInfo');
  if (s.running) {
    info.textContent = `掃描中 ${s.done}/${s.total}…`;
    v1ScanTimer = setTimeout(pollV1Scan, 5000);
    return;
  }
  $('#btnV1Scan').disabled = false;
  info.textContent = `完成｜新增 ${s.added || 0}（Z ${s.added_z || 0}）${s.error ? '｜錯誤：' + s.error : ''}`;
  loadV1();
}

/* ---------- Check 下先（V2 2 行×3×3） ---------- */
let ckTimer = null;
async function loadCheck(){
  clearTimeout(ckTimer);
  $('#ckBody').innerHTML = '<div class="note">全庫回測計算中…（約數秒）</div>';
  let d;
  try { d = await jget('/api/v2/check?id=0'); }
  catch (e) { $('#ckBody').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const at = d.axis_txt;
  const cell = g => g && g.n ? `<td><b>${g.n}</b> 場<br><span class="r-up">上 ${pct(g.up_r)}</span>（${g.up}）<br><span class="r-down">下 ${pct(g.down_r)}</span>（${g.down}）<br><span style="color:var(--dim)">走 ${pct(g.push_r)}</span></td>`
    : '<td style="color:var(--dim)">0 場</td>';
  let h = `<div class="note">${esc(d.row_note.row1)}｜樣本 <b>${d.row1.n}</b> 場。${esc(d.row_note.row2)}｜樣本 <b>${d.row2.n}</b> 場。<br>軸A＝今賽尾盤 對 上賽尾盤（互換對比盤）；軸B＝今賽尾盤 對 上賽尾盤（原盤）；上/下盤率分母已剔除走盤。每 60 秒自動更新。</div>`;
  for (const [row, lab] of [['row1', '行1（同主客場・原盤直比）'], ['row2', '行2（計埋主客互換）']]) {
    h += `<div class="tcard"><h2 style="font-size:16px;color:var(--gold2)">${lab}</h2><div class="checkwrap"><table class="ck-table"><thead><tr><th>軸A＼軸B</th>${d.axes.map(a => `<th>${at[a]}</th>`).join('')}</tr></thead><tbody>`;
    for (const a of d.axes) {
      h += `<tr><th>${at[a]}</th>${d.axes.map(b => cell(d[row].grid[`${a}|${b}`])).join('')}</tr>`;
    }
    h += '</tbody></table></div></div>';
  }
  $('#ckBody').innerHTML = h;
  ckTimer = setTimeout(() => { if (curPage === 'check' && !document.hidden) loadCheck(); }, 60000);
}

/* ---------- 過往賽果 ---------- */
let rsInit = false;
async function loadResults(){
  $('#rsBody').innerHTML = '<div class="note">載入中…</div>';
  const scope = $('#rsScope').value;
  const lg = $('#rsLg').value;
  const lnVal = $('#rsLn').value;
  let url = `/api/results?scope=${scope}&limit=1000`;
  if (lg) url += '&league=' + encodeURIComponent(lg);
  if (lnVal) { const parts = lnVal.split('|'); url += `&hc=${parts[0]}&gv=${parts[1] || 'none'}`; }
  let d;
  try { d = await jget(url); }
  catch (e) { $('#rsBody').innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (!rsInit) {
    $('#rsLg').innerHTML = '<option value="">全部聯賽</option>' + d.leagues.map(l => `<option>${esc(l)}</option>`).join('');
    $('#rsLn').innerHTML = '<option value="">全部盤口</option>' + d.lines.map(l => `<option value="${l.hc}|${l.gv || 'none'}">${esc(l.line)}（${l.n}）</option>`).join('');
    rsInit = true;
  }
  const s = d.stats;
  $('#rsInfo').textContent = `篩後 ${s.total} 場`;
  let h = `<div class="statbar">
    <span>上盤命中率 <b>${pct(s.up_r)}</b>（${s.up_hit}/${s.up_n}）</span>
    <span>下盤命中率 <b>${pct(s.down_r)}</b>（${s.down_hit}/${s.down_n}）</span>
    <span>總命中率 <b>${pct(s.decisive_r)}</b></span>
    <span>精選命中率 <b>${pct(s.feat_r)}</b>（${s.feat_w}贏${s.feat_l}輸）</span>
    <span>我的選擇 <b>${pct(s.pk_r)}</b>（${s.pk_w}贏${s.pk_l}輸）</span></div>`;
  h += '<div class="day-h">📈 逐日走勢（最近 30 日，綠≥50%）</div><div class="trend">';
  for (const t of d.trend) {
    const eff = t.w + t.l;
    const r = eff ? t.w / eff : null;
    const hi = r != null && r >= 0.5;
    h += `<div class="bar ${r == null ? '' : hi ? 'hi' : 'lo'}" style="height:${r == null ? 4 : Math.max(8, r * 64)}px" data-tip="${t.date}｜贏${t.w} 輸${t.l} 走${t.push}${r != null ? '｜' + pct(r) : ''}"></div>`;
  }
  h += '</div>';
  const byLg = {}, byDay = {};
  for (const it of d.items) {
    if (!it.ft_result || it.ft_result === 'P') continue;
    const w = it.ft_result === 'W';
    byLg[it.league] = byLg[it.league] || {w: 0, l: 0};
    byLg[it.league][w ? 'w' : 'l']++;
    const day = (it.kickoff || '').slice(0, 10);
    byDay[day] = byDay[day] || {w: 0, l: 0};
    byDay[day][w ? 'w' : 'l']++;
  }
  const winFilter = $('#rsWin').value;
  const ratioTbl = (obj, lab, keyLab) => {
    let ks = Object.keys(obj).sort((a, b) => (obj[b].w + obj[b].l) - (obj[a].w + obj[a].l));
    if (winFilter === 'win') ks = ks.filter(k => { const e = obj[k].w + obj[k].l; return e > 0 && obj[k].w / e > 0.5; });
    if (winFilter === 'lose') ks = ks.filter(k => { const e = obj[k].w + obj[k].l; return e > 0 && obj[k].l / e > 0.5; });
    ks = ks.slice(0, 30);
    if (!ks.length) return `<h4 style="margin:10px 0 4px">${lab}</h4><div class="note">無符合「${winFilter === 'win' ? '贏超50%' : '輸超50%'}」嘅項目</div>`;
    return `<h4 style="margin:10px 0 4px">${lab}</h4><table class="ck-table"><thead><tr><th>${keyLab}</th><th>贏</th><th>輸</th><th>贏率</th></tr></thead><tbody>` +
      ks.map(k => { const o = obj[k]; const e = o.w + o.l; return `<tr><td>${esc(k)}</td><td class="r-up">${o.w}</td><td class="r-down">${o.l}</td><td><b>${pct(o.w / e)}</b></td></tr>`; }).join('') + '</tbody></table>';
  };
  h += ratioTbl(byLg, '按聯賽贏/輸比例', '聯賽') + ratioTbl(byDay, '按日期贏/輸比例', '日期');
  h += `<div class="day-h">📋 場次列表（${d.items.length} 場・最新排先）</div>`;
  for (const it of d.items) {
    const wtag = it.ft_result === 'W' ? '<span class="tag up">命中</span>' : it.ft_result === 'L' ? '<span class="tag down">未中</span>' : it.ft_result === 'P' ? '<span class="tag dim">走</span>' : '<span class="tag dim">待結算</span>';
    h += `<div class="mrow" data-mid="${it.id}">
      <span class="ko">${esc((it.kickoff || '').slice(5, 16))}</span>
      <span class="lg">${esc(it.league)}</span>
      <span class="tm">${esc(it.home)} <span class="r">vs</span> ${esc(it.away)}</span>
      ${it.score ? `<span class="sc">${esc(it.score)}</span>` : ''}
      <span>${it.direction ? `<span class="tag ${it.direction}">${it.direction === 'up' ? '上' : '下'}</span>` : ''} ${wtag}</span>
      <span class="ln">${esc(it.line || '')}<br>${esc(it.odds || '')}</span></div>`;
  }
  $('#rsBody').innerHTML = h;
  $$('#rsBody .mrow').forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
}
$('#rsScope').onchange = loadResults;
$('#rsLg').onchange = loadResults;
$('#rsLn').onchange = loadResults;
$('#rsWin').onchange = loadResults;

/* ---------- 過往紀錄 ---------- */
async function loadFeatlog(){
  $('#flList').innerHTML = '<div class="note">載入中…</div>';
  const z = $('#flZ').value === '1';
  let d;
  try { d = await jget('/api/featlog' + (z ? '?z=1' : '')); }
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
    return `<div class="mrow" data-mid="${p.id}">
      <span class="ko">${esc((p.kickoff || '').slice(5, 16))}</span>
      <span class="lg">${esc(p.league)}</span>
      <span class="tm">${esc(p.home)} <span class="r">vs</span> ${esc(p.away)}</span>
      ${p.score ? `<span class="sc">${esc(p.score)}</span>` : ''}
      <span class="tag ${p.direction}">${p.direction === 'up' ? '上盤' : '下盤'}</span>
      <span>${res}</span>
      <span class="ln">${esc(p.line || '')}<br>${esc(p.odds || '')}<br><span style="color:var(--dim)">紀錄 ${esc((p.added_at || '').slice(5, 16))}</span></span></div>`;
  };
  let h = '';
  if (d.pending.length) h += `<div class="day-h">🔜 未開賽（${d.pending.length}）</div>` + d.pending.map(card).join('');
  if (d.played.length) h += `<div class="day-h">📼 已完場（${d.played.length}・最新排先）</div>` + d.played.map(card).join('');
  if (!h) h = '<div class="note">暫無紀錄。精選場次一出現會自動紀錄喺度。</div>';
  $('#flList').innerHTML = h;
  $$('#flList .mrow').forEach(r => r.onclick = () => openDetail(+r.dataset.mid));
}
$('#flZ').onchange = loadFeatlog;

/* ---------- 設定 ---------- */
let settingsLoaded = false;
async function loadSettings(){
  if (settingsLoaded) return;
  try {
    const r = await fetch('/static/settings.html');
    $('#setBody').innerHTML = await r.text();
    settingsLoaded = true;
  } catch (e) {
    $('#setBody').innerHTML = '<div class="err">設定頁載入失敗：' + esc(String(e)) + '</div>';
  }
}

/* ================= V2 45 項渲染器 ================= */
const V2_TP = ['初盤', '開賽前4小時', '開賽前30分鐘', '開賽前15分鐘', '開賽前10分鐘', '開賽前5分鐘', '尾盤'];
const V2_TITLES = {};
for (let i = 0; i < 6; i++)
  V2_TITLES[String(i + 1)] = `同${V2_TP[i]}及尾盤的盤口&水位（16格＋12段水位）`;
for (let i = 0; i < 7; i++) {
  V2_TITLES[String(7 + i)] = `對上一次對賽尾盤 對 今場${V2_TP[i]}（純盤口・三合一）`;
  V2_TITLES[String(14 + i)] = `對上一次對賽尾盤 對 今場${V2_TP[i]}（水位版・9格）`;
}
Object.assign(V2_TITLES, {
  '21': '主隊主場勝和負 及 客隊客場（每項±7%）＋同尾盤 → 盤口分佈',
  '22': '主隊主場勝和負 及 客隊客場（每項±7%）＋同尾盤 → 12段水位',
  '23': '主隊總和勝和負 及 客隊總和（每項±7%）＋同尾盤 → 盤口分佈',
  '24': '主隊總和勝和負 及 客隊總和（每項±7%）＋同尾盤 → 12段水位',
  '25': '主隊主場入/失/差 及 客隊客場（各±3）＋同尾盤 → 盤口分佈',
  '26': '主隊主場入/失/差 及 客隊客場（各±3）＋同尾盤 → 12段水位',
  '27': '主隊主場排名 及 客隊客場排名（各±2）＋同尾盤 → 分佈＋水位',
  '28': '主隊總排名 及 客隊總排名（各±2）＋同尾盤 → 分佈＋水位',
  '29': '上次比賽完全相同（角色＋讓/受讓＋讓球數）→ 盤口分佈＋各盤口水位',
  '30': '上次比賽樣本：分佈最多盤口 與今場尾盤淨差距',
  '31': '今賽比上賽 深/淺/不變（Check 下先參照）',
  '32': '主隊主場排名 − 客隊客場排名 差距淨值（±1）＋同尾盤 → 分佈＋水位',
  '33': '上次比賽樣本：上盤勝率最接近50%盤口 與今場尾盤淨差距',
  '34': '主隊主場入/失/差 及 客隊客場（各±3）＋同尾盤 → 分佈＋水位',
  '35': '主隊總和入/失/差 及 客隊總和（各±3）＋同尾盤 → 分佈＋水位',
});
for (let i = 0; i < 6; i++)
  V2_TITLES[String(36 + i)] = `首14分鐘入球%（同${V2_TP[i]}及尾盤・16格）`;
Object.assign(V2_TITLES, {
  '42': '首14分鐘入球%（排名差距淨值 主場−客場 ±1）',
  '43': '首14分鐘入球%（主隊總排名−客隊總排名 差距淨值±1＋同尾盤）',
  '44': '首14分鐘入球%（主隊主場入/失/差 及 客隊客場 各±3＋同尾盤）',
  '45': '首14分鐘入球%（主隊總和入/失/差 及 客隊總和 各±3＋同尾盤）',
});
const V2_COMBO_OPTS = [];
for (let n = 1; n <= 35; n++) {
  if (n === 30 || n === 31 || n === 33) continue;
  V2_COMBO_OPTS.push(String(n));
}

/* 全場通用：上/下盤對應邊隊（setV2UD 喺詳情頁設定；冇資料就用 上盤/下盤） */
let V2UD = null;
function setV2UD(tg){
  if (!tg || !tg.home) { V2UD = null; return; }
  if (tg.giver === 'home')      V2UD = {up: tg.home, down: tg.away, push: false};
  else if (tg.giver === 'away') V2UD = {up: tg.away, down: tg.home, push: false};
  else                          V2UD = {up: tg.home, down: tg.away, push: true};
                              // 平手盤：上盤＝平手主隊
}
function _un(){ return V2UD ? V2UD.up : '上盤'; }   // 上盤隊名
function _dn(){ return V2UD ? V2UD.down : '下盤'; } // 下盤隊名

function _v2oc(o) {
  return o ? `<b class="r-up">上(${esc(_un())}) ${pct(o.up_r)}</b>（${o.up}場）／<b class="r-down">下(${esc(_dn())}) ${pct(o.down_r)}</b>（${o.down}場）／走 ${pct(o.push_r)}（${o.push}場）<span style="color:var(--dim)">｜樣本 ${o.n} 場</span>` : '—';
}
function v2Water12Tbl(w, curZ, zones12) {
  if (!w) return '';
  let h = `<table class="ck-table"><thead><tr><th>上盤水位段<br><small style="color:var(--dim)">上盤＝${esc(_un())}</small></th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>`;
  (w.zones || []).forEach((z, i) => {
    if (!z || !z.n) return;
    const hit = (i === curZ) ? ' class="hl"' : '';
    const zn = z.zone || (zones12 ? zones12[i] : String(i));
    h += `<tr${hit}><td>${esc(zn)}${i === curZ ? ' ◀今場' : ''}</td><td>${z.n}</td>` +
         `<td class="r-up">${pct(z.up_r)}</td><td class="r-down">${pct(z.down_r)}</td><td>${pct(z.push_r)}</td></tr>`;
  });
  h += `</tbody></table><div class="note" style="margin:2px 0 8px">水位表樣本 ${w.n} 場；每段顯示實際水位範圍（例如 0.85-0.89）${curZ >= 0 ? '；<b>黃底＝今場尾盤上盤水位所在段</b>' : ''}</div>`;
  return h;
}
function v2DistTbl(dist, curZ, zones12) {
  if (!dist || !dist.length) return '<div class="note">無符合條件嘅歷史場次</div>';
  let h = `<table class="ck-table"><thead><tr><th>尾盤盤口</th><th>場數</th><th>上(${esc(_un())})場</th><th>下(${esc(_dn())})場</th><th>走</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>`;
  for (const r of dist) {
    const hit = r.is_cur ? ' class="hl"' : '';
    const zones = (r.zones || []).map((z, i) => z ? {zone: zones12 ? zones12[i] : String(i), ...z} : null);
    const sub = v2Water12Tbl({n: r.n, zones}, curZ, zones12);
    h += `<tr${hit}><td>${esc(r.line)}${r.is_cur ? ' ◀今場' : ''}</td><td>${r.n}</td>` +
         `<td>${r.up}</td><td>${r.down}</td><td>${r.push}</td>` +
         `<td class="r-up">${pct(r.up_r)}</td><td class="r-down">${pct(r.down_r)}</td><td>${pct(r.push_r)}</td></tr>`;
    if (zones.some(Boolean)) {
      h += `<tr><td colspan="8" style="padding:0;border:none"><details style="margin:2px 8px"><summary style="padding:4px 8px;font-size:12px;color:var(--dim)">▸ 呢個盤口嘅 12 段水位細分</summary><div class="body">${sub}</div></details></td></tr>`;
    }
  }
  return h + '</tbody></table>';
}
function v2Cells16Tbl(cells) {
  if (!cells || !cells.length) return '';
  let h = `<table class="ck-table"><thead><tr><th>字頭</th><th>範圍</th><th>混合</th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>`;
  for (const c of cells) {
    h += `<tr><td><b>${c.letter}</b></td><td>${esc(c.scope)}</td><td>${esc(c.mix)}</td><td>${c.n}</td>` +
         `<td class="r-up">${pct(c.up_r)}</td><td class="r-down">${pct(c.down_r)}</td><td>${pct(c.push_r)}</td></tr>`;
  }
  return h + '</tbody></table><div class="note">A–H＝混合（全部樣本）；I–P＝同主隊（樣本場次嘅主隊同今場主隊係同一隊）。每格都有上/下/走%＋場數；樣本少嘅格僅供參考。</div>';
}
function v2Cells9HTML(cells, curZ, zones12) {
  if (!cells || !cells.length) return '';
  let h = '';
  for (const c of cells) {
    h += `<details style="margin:4px 0"><summary><span class="sum-t">${esc(c.tri)} × ${esc(c.scope)}</span><span class="sub">${c.n} 場</span></summary><div class="body">${v2Water12Tbl(c, curZ, zones12)}</div></details>`;
  }
  return h;
}
function v2TriTxt(tri) {
  if (!tri) return '';
  return `<table class="ck-table"><thead><tr><th>對比口徑</th><th>場數</th><th>上盤(${esc(_un())})勝率</th><th>下盤(${esc(_dn())})勝率</th><th>走盤率</th></tr></thead><tbody>` +
    [['same', '同主客場（原盤直比）'], ['swap', '主客互換（對調對比盤）'], ['all', '綜合全部']]
      .map(([k, lab]) => {
        const o = tri[k];
        return `<tr><td>${lab}</td><td>${o ? o.n : 0}</td><td class="r-up">${o ? pct(o.up_r) : '—'}</td><td class="r-down">${o ? pct(o.down_r) : '—'}</td><td>${o ? pct(o.push_r) : '—'}</td></tr>`;
      }).join('') + '</tbody></table>';
}
function _gapStr(g){
  if (!g) return '';
  if (typeof g === 'object') return g.text || g.gap_txt || JSON.stringify(g);
  return String(g);
}
function v2GapCard(pack) {
  if (!pack) return '<div class="note">樣本不足</div>';
  return `<div class="linebox"><div class="lb-t">${esc(pack.line)}｜${pack.n} 場</div>
    <div class="lb-v">上(${esc(_un())}) ${pct(pack.up_r)}／下(${esc(_dn())}) ${pct(pack.down_r)}／走 ${pct(pack.push_r)}</div>
    <div class="lb-v" style="color:var(--gold2)">${esc(_gapStr(pack.gap))}</div></div>`;
}
function v2States(it) {
  let h = '';
  for (const [k, lab] of [['conv', '互換對比盤'], ['raw', '同主客原盤']]) {
    const s = it[k];
    if (!s) continue;
    const col = s.state === 'deep' ? 'var(--up)' : s.state === 'shallow' ? 'var(--down)' : 'var(--dim)';
    h += `<div class="linebox"><div class="lb-t">${lab}：上賽 ${esc(s.ref_line || '')}</div>
      <div class="lb-v" style="font-size:18px;color:${col}">${esc(s.state_txt)}</div>
      <div class="lb-v">${esc(s.note || '')}</div></div>`;
  }
  return h || '<div class="note">無對賽記錄</div>';
}
function v2F14Cells(cells) {
  if (!cells || !cells.length) return '';
  let h = '<table class="ck-table"><thead><tr><th>字頭</th><th>範圍</th><th>混合</th><th>有記錄場數</th><th>主隊入%</th><th>客隊入%</th><th>冇入%</th></tr></thead><tbody>';
  for (const c of cells) {
    h += `<tr><td><b>${c.letter}</b></td><td>${esc(c.scope)}</td><td>${esc(c.mix)}</td><td>${c.n}</td>` +
         `<td class="r-up">${pct(c.home_r)}</td><td class="r-down">${pct(c.away_r)}</td><td>${pct(c.none_r)}</td></tr>`;
  }
  return h + '</tbody></table>';
}
function v2F14Stats(st) {
  if (!st) return '<div class="note">無數據</div>';
  if (!st.n) return '<div class="note">暫無符合條件又有首14分鐘記錄嘅場次</div>';
  return `<div class="linebox"><div class="lb-t">樣本 ${st.n} 場</div>
    <div class="lb-v">主隊先入 ${pct(st.home_r)}（${st.home}）｜客隊先入 ${pct(st.away_r)}（${st.away}）｜冇入球 ${pct(st.none_r)}（${st.none}）</div></div>`;
}
function v2ComboHTML() {
  const opts = V2_COMBO_OPTS.map(n =>
    `<label style="display:inline-block;margin:2px 8px 2px 0"><input type="checkbox" class="v2cb" value="${n}"> ${n}. ${esc(V2_TITLES[n] || '')}</label>`).join('');
  return `<h4 style="margin:12px 0 4px">組合篩查（剔 1–35 中多項；30/31/33 係分析項唔可以剔；最多 12 項）</h4>
    <div style="max-height:180px;overflow:auto;border:1px solid var(--line);padding:6px;border-radius:6px">${opts}</div>
    <div style="margin-top:8px"><button class="btn accent v2combo-go">提交組合篩查</button>
    <span class="note v2combo-msg" style="margin-left:8px"></span></div>
    <div class="v2combo-res" style="margin-top:8px"></div>`;
}
function v2WireCombo(mid, bodyEl) {
  const go = bodyEl.querySelector('.v2combo-go');
  if (!go) return;
  const boxes = [...bodyEl.querySelectorAll('.v2cb')];
  boxes.forEach(cb => cb.onchange = () => {
    if (boxes.filter(b => b.checked).length > 12) cb.checked = false;
  });
  go.onclick = async () => {
    const sel = boxes.filter(b => b.checked).map(b => b.value);
    const msg = bodyEl.querySelector('.v2combo-msg');
    const res = bodyEl.querySelector('.v2combo-res');
    if (!sel.length) { msg.textContent = '請先剔選至少一項'; return; }
    go.disabled = true; msg.textContent = '篩查中…';
    let r;
    try { r = await jpost('/api/v2/combo', {id: mid, sel}); }
    catch (e) { res.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; go.disabled = false; return; }
    go.disabled = false;
    if (r.error) { msg.textContent = ''; res.innerHTML = `<div class="err">${esc(r.error)}</div>`; return; }
    msg.textContent = `已剔 ${sel.length} 項：${sel.join('、')}`;
    const scopeRow = (lab, o) => `<tr><td>${lab}</td>` + (o
      ? `<td>${o.n}</td><td>${o.up}</td><td>${o.down}</td><td>${o.push}</td><td class="r-up">${pct(o.up_r)}</td><td class="r-down">${pct(o.down_r)}</td><td>${pct(o.push_r)}</td>`
      : '<td colspan="7">不適用</td>') + '</tr>';
    res.innerHTML = `<table class="ck-table"><thead><tr><th>範圍</th><th>賽事總數(分母)</th><th>上盤</th><th>下盤</th><th>走盤</th><th>上盤率</th><th>下盤率</th><th>走盤率</th></tr></thead><tbody>` +
      scopeRow('全資料庫', r.all) + scopeRow('同一聯賽', r.league) + scopeRow('同類別', r.cat) + '</tbody></table>';
  };
}
async function v2LoadItem(mid, no, bodyEl) {
  bodyEl.innerHTML = '<div class="note">計算中…（首次約 1-3 秒）</div>';
  let it;
  try { it = await jget('/api/v2/item?id=' + mid + '&no=' + no); }
  catch (e) { bodyEl.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  if (it.error) { bodyEl.innerHTML = `<div class="note">參照：${esc(it.ref || '')}</div><div class="err">${esc(it.error)}</div>`; return; }
  const curZ = it.cur_zone12 != null ? it.cur_zone12 : -1;
  const zones12 = it.zones12 || null;
  let h = it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '';
  if (it.data_note) h += `<div class="err" style="background:#4a3a10;border-color:#a08c2a">${esc(it.data_note)}</div>`;
  if (it.oc) h += `<div class="note" style="margin:6px 0">結果（全條件樣本）：${_v2oc(it.oc)}</div>`;
  if (it.tri) h += v2TriTxt(it.tri);
  if (it.cells && it.cells.length && it.cells[0].letter) {
    h += (Number(it.no) >= 36 ? v2F14Cells(it.cells) : v2Cells16Tbl(it.cells));
  }
  if (it.cells && it.cells.length && it.cells[0].tri) h += v2Cells9HTML(it.cells, curZ, zones12);
  if (it.dist && it.dist.length) {
    h += '<h4 style="margin:10px 0 4px">盤口分佈（每個盤口可展開 12 段水位細分；黃底＝同今場尾盤一樣嘅盤口）</h4>';
    h += v2DistTbl(it.dist, curZ, zones12);
  }
  if (it.water) {
    for (const [k, lab] of [['all', '全庫'], ['league', '同聯賽']]) {
      if (!it.water[k]) continue;
      h += `<h4 style="margin:10px 0 4px">12段水位表・${lab}</h4>` + v2Water12Tbl(it.water[k], curZ, zones12);
    }
  }
  if (it.mode || it.d50) {
    h += '<h4 style="margin:10px 0 4px">分佈最多盤口</h4>' + v2GapCard(it.mode);
    h += '<h4 style="margin:10px 0 4px">上盤勝率最接近 50% 嘅盤口（至少5場）</h4>' + v2GapCard(it.d50);
  }
  if (it.conv !== undefined || it.raw !== undefined) h += v2States(it);
  if (it.stats) h += v2F14Stats(it.stats);
  if (no === '20') h += v2ComboHTML();
  bodyEl.innerHTML = h;
  if (no === '20') v2WireCombo(mid, bodyEl);
}
async function initV2Section(mid) {
  const box = document.getElementById('v2Items');
  const lt = document.getElementById('v2Letters');
  if (!box) return;
  let s;
  try { s = await jget('/api/v2/summary?id=' + mid); }
  catch (e) { lt.innerHTML = '<span class="err">V2 載入失敗：' + esc(String(e)) + '</span>'; return; }
  if (s.error) { lt.innerHTML = '<span class="err">' + esc(s.error) + '</span>'; return; }
  setV2UD(s.target);
  const pushNote = V2UD && V2UD.push ? '（今場平手盤：上盤＝平手主隊）' : '';
  const legend = `<div class="note" style="margin:0 0 6px">本場：上盤＝<b>${esc(_un())}</b>｜下盤＝<b>${esc(_dn())}</b>${pushNote}</div>`;
  const L = s.letters || {};
  const passed = L.passed_letters || [];
  const dirTxt = L.direction ? (L.direction === 'up' ? '上盤' : '下盤') : null;
  lt.innerHTML = legend +
    `<div style="font-size:15px;margin-bottom:4px">🏆 精選 16 字頭檢查：${dirTxt ? `<b class="r-up">✅ 合格 → ${dirTxt}(${esc(_un())})</b>（合格字頭 <b>${passed.join(' ')}</b>）` : '<span style="color:var(--dim)">❌ 無字頭合格（唔入選精選）</span>'}</div>` +
    `<table class="ck-table" style="margin-bottom:8px"><thead><tr><th>字頭</th><th>口徑</th><th>方向</th><th>①項目2率</th><th>②項目13率</th><th>③30同盤率</th><th>④33同盤率</th><th>合格</th></tr></thead><tbody>` +
    (L.letters || []).map(c =>
      `<tr${c.pass ? ' class="hl"' : ''}><td><b>${c.letter}</b></td><td>${esc(c.scope)}・${esc(c.mix)}</td>` +
      `<td>${c.dir ? (c.dir === 'up' ? '上' : '下') : '—'}</td>` +
      `<td>${c.r2 != null ? pct(c.r2) + '｜n=' + (c.n2 || 0) : '—'}</td>` +
      `<td>${c.d13_r != null ? pct(c.d13_r) + '｜n=' + (c.n13 || 0) : '—'}</td>` +
      `<td>${c.r30 ? pct(c.r30.r) + '｜n=' + c.r30.n : '—'}</td>` +
      `<td>${c.r33 ? pct(c.r33.r) + '｜n=' + c.r33.n : '—'}</td>` +
      `<td>${c.pass ? '✅' : ''}</td></tr>`).join('') +
    `</tbody></table>` +
    `<div class="note">入選規則：16 個字頭 A–P（A–H＝混合×8 範圍；I–P＝同主隊×8 範圍）。每字頭要 ①項目2（同開賽前4小時及尾盤）格X有方向（上或下盤率&gt;49.99%）→ ②項目13（對上一次對賽尾盤 對 今場尾盤・純盤口）格X同方向 → ③項目30 分佈最多盤口同方向率&gt;49.99% → ④項目33 最接近50%盤同方向率&gt;49.99%。表內「%」＝該方向（上盤＝${_un()}／下盤＝${_dn()}）嘅贏盤率，「n」＝場數。①同②嘅贏盤率<b>各自獨立展示，唔夾埋</b>。四項全過＝字頭合格，<b>任一字頭合格即入選精選</b>；只有 I–P 合格＝精選Z。</div>`;
  let h = '';
  for (let n = 1; n <= 45; n++) {
    const no = String(n);
    const ok = s.applicability ? s.applicability[no] : true;
    h += `<details class="v2-item" data-no="${no}"><summary><span class="sum-t">${no}. ${esc(V2_TITLES[no] || '')}</span>` +
         (ok ? '' : '<span class="sub">不適用</span>') + '</summary><div class="body"></div></details>';
  }
  box.innerHTML = h;
  box.querySelectorAll('details.v2-item').forEach(d => {
    d.addEventListener('toggle', () => {
      if (d.open && !d.dataset.loaded) {
        d.dataset.loaded = '1';
        v2LoadItem(mid, d.dataset.no, d.querySelector('.body'));
      }
    });
  });
}

/* ---------- 啟動 ---------- */
(async function boot(){
  loadSettings();
  const ok = await pollReady();
  if (ok) {
    loadHome();
    refreshPicksMap();
    refreshUpdInfo();
    // 每 2 分鐘靜靜哋更新計數
    setInterval(refreshPicksMap, 120000);
    setInterval(refreshUpdInfo, 30000);
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
