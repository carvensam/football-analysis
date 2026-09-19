/* 足球篩查 APP 前端 */
const $ = s => document.querySelector(s);
const ZONES_LABEL = ['≤.69','.70','.75','.80','.85','.90','.95','1.00','1.05','≥1.10'];
const ZONES = ZONES_LABEL.slice();   // 實際用嘅水位分區，render 時會用伺服器版本覆寫內容
let curMatch = null;

function setStatus(t) { $('#status').textContent = t; }

// 球隊名前加「(排名)」；無排名則原樣
function rn(name, rank) { return rank ? `(${rank}) ${name}` : name; }

let poolReady = false;
let poolErr = null;
let listCount = null;
let serverVer = '';
let lastList = [];            // 最近一次載入嘅賽事清單（自動更新賠率用）
let listUpdatedAt = null;     // 最近一次清單刷新時間

function updateStatus() {
  let t = poolErr ? '⚠ 數據池載入失敗（可按重試或重開APP）'
       : poolReady ? '✓ 數據池就緒' + (serverVer ? ' · v' + serverVer : '')
       : '⏳ 歷史數據池載入中…（首次約需 5-10 秒，之後極快）';
  if (listCount != null) t += '　共 ' + listCount + ' 場';
  if (listUpdatedAt) t += '　清單更新於 ' + listUpdatedAt.toTimeString().slice(0, 5);
  $('#status').textContent = t;
}

async function pollReady() {
  for (let i = 0; i < 120; i++) {   // 最多等 2 分鐘
    try {
      const r = await jget('/api/ready');
      serverVer = r.version || '';
      if (r.error) { poolErr = r.error; updateStatus(); return; }
      if (r.ready) { poolReady = true; updateStatus(); return; }
    } catch (e) { /* 伺服器未完全啟動，繼續等 */ }
    updateStatus();
    await new Promise(res => setTimeout(res, 1000));
  }
  poolErr = '等待數據池逾時';
  updateStatus();
}

async function waitPool() {
  while (!poolReady && !poolErr) {
    updateStatus();
    await new Promise(res => setTimeout(res, 800));
  }
  return poolReady;
}

async function jget(url) { const r = await fetch(url); return r.json(); }
async function jpost(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}

function esc(s){ return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// 安全網：任何未捕捉嘅程式錯誤（包括 async）都直接顯示，唔會再停喺「篩查中…」
window.addEventListener('unhandledrejection', e => {
  const R = document.querySelector('#result');
  const msg = String((e.reason && (e.reason.stack || e.reason.message)) || e.reason || '未知錯誤');
  if (R && R.style.display !== 'none') {
    R.innerHTML = `<div class="err">程式錯誤（請截圖俾我）：${esc(msg.slice(0, 600))}</div>
      <div style="margin-top:10px"><button class="btn accent" onclick="location.reload()">重新整理</button></div>`;
  }
  e.preventDefault();
});
function pct(v){ return v==null ? '—' : (v*100).toFixed(1)+'%'; }
function cnt(oc, key){ return oc ? `${oc[key]} (${pct(oc[key+'_r'])})` : '—'; }

async function loadList() {
  setStatus('載入賽事…');
  const hours = $('#hours').value;
  const list = await jget('/api/upcoming?hours='+hours);
  const box = $('#matchList');
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<div class="empty">' + (hours === '0' ? '無即將開賽賽事' : '未來'+hours+'小時無賽事') + '</div>'; listCount = 0; lastList = []; listUpdatedAt = new Date(); updateStatus(); return; }
  lastList = list;
  listUpdatedAt = new Date();
  let lastDate = '';
  for (const m of list) {
    const d = m.kickoff.slice(5, 10);
    if (d !== lastDate) {
      lastDate = d;
      const sep = document.createElement('div');
      sep.className = 'date-sep';
      sep.textContent = m.kickoff.slice(0, 10);
      box.appendChild(sep);
    }
    const row = document.createElement('div');
    row.className = 'mrow' + (curMatch === m.id ? ' active' : '');
    row.dataset.id = m.id;
    const badge = m.has_odds
      ? '<span class="badge ok">已有賠率' + (m.fetched_ago != null ? ' · '+m.fetched_ago+'s前更新' : '') + '</span>'
      : '<span class="badge no">未獲取賠率</span>';
    const line = m.line ? `<span class="line-tag">${esc(m.line.line)} 主${m.line.ho}/客${m.line.ao}</span>` : '';
    row.innerHTML = `
      <div class="top"><span>${esc(m.league)}</span><span>${m.kickoff.slice(11,16)}</span></div>
      <div class="mid">${esc(rn(m.home, m.rank_home))} <span style="color:var(--dim)">vs</span> ${esc(rn(m.away, m.rank_away))}</div>
      <div class="bot">${line} ${badge}</div>`;
    row.onclick = () => openMatch(m.id, row);
    box.appendChild(row);
  }
  listCount = list.length;
  updateStatus();
}

let picksMap = {};   // match_id -> 'up'/'down'

function updatePkButtons(id) {
  const cur = picksMap[id] || '';
  document.querySelectorAll('.tbtns .pk').forEach(b => {
    b.classList.toggle('on', b.dataset.pk === cur);
  });
}

async function togglePick(id, choice) {
  const m = document.getElementById('pkMsg');
  try {
    if (picksMap[id] === choice) {
      await jpost('/api/pick/delete', {id});
      delete picksMap[id];
      if (m) m.textContent = '已取消選擇';
    } else {
      const r = await jpost('/api/pick', {id, choice});
      if (!r.ok) { if (m) m.textContent = '記錄失敗：' + (r.error || ''); return; }
      picksMap[id] = choice;
      if (m) m.textContent = '已記錄：' + (choice === 'up' ? '上盤' : '下盤') + '（再撳一次可取消）';
    }
    updatePkButtons(id);
    loadPicks();
  } catch (e) {
    if (m) m.textContent = '記錄失敗：' + String(e.message || e);
  }
}

const RES_TAG = {W: '<b class="r-up">贏</b>', L: '<b class="r-down">輸</b>',
                 P: '<b class="r-push">走</b>', X: '<span style="color:var(--dim)">無法判定</span>'};

async function loadPicks() {
  let d;
  try { d = await jget('/api/picks'); } catch (e) { return; }
  picksMap = {};
  (d.picks || []).forEach(p => picksMap[p.id] = p.choice);
  const s = d.stats || {};
  const rate = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—';
  const sum = document.getElementById('picksSum');
  if (sum) sum.textContent = `我的選擇｜${s.total || 0} 場（未開賽 ${s.pending || 0}）｜勝 ${s.wins || 0} 輸 ${s.losses || 0} 走 ${s.pushes || 0}｜勝出率 ${rate}`;
  const box = document.getElementById('picksBox');
  if (!box) return;
  if (!(d.picks || []).length) {
    box.innerHTML = '<div class="note">未有任何選擇。喺右邊賽事撳「上盤／下盤」記錄，賽後自動結算統計。<br>勝出率＝勝／(勝＋輸)，走盤不計。</div>';
    return;
  }
  let h = `<table><tr><th class="l">賽事</th><th class="l">尾盤</th><th>我的選擇</th><th>結果</th><th></th></tr>`;
  for (const p of d.picks) {
    const res = p.result == null ? '<span style="color:var(--dim)">未開賽</span>'
      : (RES_TAG[p.result] || p.result);
    const del = p.result == null
      ? `<button class="btn pk-del" data-id="${p.id}">✕</button>` : '';
    h += `<tr><td class="l">${esc(p.kickoff.slice(5, 16))} ${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}`
       + (p.score ? ` <span style="color:var(--dim)">${p.score}</span>` : '') + `</td>`
       + `<td class="l">${esc(p.line || '—')}${p.odds ? ' ' + esc(p.odds) : ''}</td>`
       + `<td>${p.choice === 'up' ? '<b class="r-up">上盤</b>' : '<b class="r-down">下盤</b>'}</td>`
       + `<td>${res}</td><td>${del}</td></tr>`;
  }
  box.innerHTML = h + '</table>';
  box.querySelectorAll('.pk-del').forEach(b => {
    b.onclick = async () => {
      await jpost('/api/pick/delete', {id: parseInt(b.dataset.id, 10)});
      loadPicks();
      if (curMatch) updatePkButtons(curMatch);
    };
  });
}

// ===== 我的選擇 · 獨立大版面 =====
let pkOvTimer = null;

function _ocTxt(o) {   // {n,up_r,down_r,push_r} → '上52.1 下40.4 走7.5｜n=134'
  if (!o) return '無數據';
  return `上${pct(o.up_r)} 下${pct(o.down_r)} 走${pct(o.push_r)}｜n=${o.n}`;
}

function _pairTxt(b) { // 全庫＋同聯賽
  if (!b) return '不適用';
  return `全庫 ${_ocTxt(b.all)}<br><span style="opacity:.75">同聯賽 ${_ocTxt(b.lg)}</span>`;
}

function _gapTxt(g, curLine) {
  // g = {all:{mode,d50}, lg:{...}} —— 展示現時盤 vs 最接近50%盤 差距（全庫為主）
  if (!g) return '<span style="opacity:.7">不適用</span>';
  const parts = [];
  const scope = g.all || g.lg;
  if (!scope) return '<span style="opacity:.7">不適用</span>';
  const d50 = scope.d50, mode = scope.mode;
  const gapText = gd => (gd && (gd.text || gd.gap != null)) ? (gd.text || String(gd.gap)) : null;
  if (d50) {
    parts.push(`現時盤【${esc(curLine || '—')}】 vs 最接近50%盤【${esc(d50.line)}】` +
      `（n=${d50.n}｜上${pct(d50.up_r)}）` +
      (gapText(d50.gap) ? ` → <b>${esc(gapText(d50.gap))}</b>` : ''));
  }
  if (mode) {
    parts.push(`分佈最多盤【${esc(mode.line)}】（n=${mode.n}｜上${pct(mode.up_r)}）` +
      (gapText(mode.gap) ? ` → <b>${esc(gapText(mode.gap))}</b>` : ''));
  }
  return parts.length ? parts.join('<br>') : '<span style="opacity:.7">無數據</span>';
}

function _pkCard(p) {
  const res = p.played ? (p.score ? (RES_TAG[p.result] || esc(p.result))
      : '<span style="color:var(--dim)">待賽果</span>')
    : '<span style="color:var(--dim)">未開賽</span>';
  const b = p.brief || {};
  const i12 = b.i12, i15 = b.i15;
  const i12v = !i12 ? '不適用'
    : `n=${i12.n}` + (i12.top ? `<br>最多【${esc(i12.top.line || '')}】上${pct(i12.top.up_r)} 下${pct(i12.top.down_r)}` : '');
  const i15v = !i15 ? '不適用'
    : `n=${i15.n}` + (i15.zone ? `<br>今場水位區【${esc(i15.zone.zone)}】上${pct(i15.zone.up_r)} 下${pct(i15.zone.down_r)} 走${pct(i15.zone.push_r)}` : '');
  const del = !p.played ? `<button class="btn pk-del" data-id="${p.id}">✕</button>` : '';
  const again = `<button class="btn pk-again" data-id="${p.id}" data-choice="${p.choice}">⇄ 照揀</button>`;
  return `<div class="pk-card">
    <div class="pk-top">
      <span class="pk-time">${esc(p.kickoff.slice(5, 16))}　${esc(p.league)}</span>
      <span class="pk-teams">${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}</span>
      ${p.score ? `<span class="pk-score">${esc(p.score)}</span>` : ''}
      <span class="pk-tag ${p.choice}">${p.choice === 'up' ? '上盤' : '下盤'}</span>
      <span class="pk-res">${res}</span>
      <span style="color:var(--dim);font-size:12px">揀時 ${esc(p.pick_line || '—')}${p.pick_odds ? ' ' + esc(p.pick_odds) : ''}｜尾盤 ${esc(p.line || '—')}${p.odds ? ' ' + esc(p.odds) : ''}</span>
      ${again}${del}
    </div>
    <div class="pk-items">
      <div class="pk-it"><div class="t">① 同初盤及尾盤</div><div class="v">${_pairTxt(b.i1)}</div></div>
      <div class="pk-it"><div class="t">⑤ 對上對賽尾盤</div><div class="v">${_pairTxt(b.i5)}</div></div>
      <div class="pk-it"><div class="t">⑧ 主客入失球±3</div><div class="v">${_pairTxt(b.i8)}</div></div>
      <div class="pk-it"><div class="t">⑫ 主場客場排名±2</div><div class="v">${i12v}</div></div>
      <div class="pk-it"><div class="t">⑮ 排名±2＋同尾盤</div><div class="v">${i15v}</div></div>
    </div>
    <div class="pk-gap"><span class="lab">⑭ 差距</span>${_gapTxt(b.g14, b.cur_line)}</div>
    <div class="pk-gap"><span class="lab">⑰ 差距</span>${_gapTxt(b.g17, b.cur_line)}</div>
  </div>`;
}

async function renderPicksOverlay() {
  const body = document.getElementById('pkOvBody');
  body.innerHTML = '<div class="empty">計算中（每場跑 ①⑤⑧⑫⑮ ＋ ⑭⑰ 差距，約 10-30 秒）…</div>';
  let d;
  try { d = await jget('/api/picks/full'); }
  catch (e) { body.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats || {};
  const rate = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—';
  document.getElementById('pkOvTitle').textContent =
    `我的選擇｜${s.total || 0} 場（未開賽 ${s.pending || 0}）｜已開賽 ${(d.played || []).length} 場（永久保留）｜勝 ${s.wins || 0} 輸 ${s.losses || 0} 走 ${s.pushes || 0}｜勝出率 ${rate}`;
  if (!s.total) {
    body.innerHTML = '<div class="note">未有任何選擇。喺賽事結果頁最底撳「上盤／下盤」記錄。</div>';
    return;
  }
  let h = '';
  if ((d.pending || []).length) {
    h += `<div class="pk-sec-t">未開賽（順開賽時間排）</div>` + d.pending.map(_pkCard).join('');
  }
  if ((d.played || []).length) {
    h += `<div class="pk-sec-t">已開賽（永久保留，按日子分類，最新排先）</div>`;
    let lastDate = '';
    for (const p of d.played) {
      const dd = p.kickoff.slice(0, 10);
      if (dd !== lastDate) {
        h += `<div class="ft-date">${esc(dd)}</div>`;
        lastDate = dd;
      }
      h += _pkCard(p);
    }
  }
  body.innerHTML = h;
  body.querySelectorAll('.pk-del').forEach(btn => {
    btn.onclick = async () => {
      await jpost('/api/pick/delete', {id: parseInt(btn.dataset.id, 10)});
      loadPicks();
      if (curMatch) updatePkButtons(curMatch);
      renderPicksOverlay();
    };
  });
  body.querySelectorAll('.pk-again').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await jpost('/api/pick', {id: parseInt(btn.dataset.id, 10),
                                  choice: btn.dataset.choice});
        btn.textContent = '✓ 已照揀';
        loadPicks();
        if (curMatch) updatePkButtons(curMatch);
      } catch (e) {
        btn.textContent = '✗ 失敗';
      }
      setTimeout(() => { btn.disabled = false; btn.textContent = '⇄ 照揀'; }, 2000);
    };
  });
}

function openPicksOverlay() {
  document.getElementById('pkOverlay').style.display = 'flex';
  renderPicksOverlay();
  clearInterval(pkOvTimer);
  pkOvTimer = setInterval(() => {   // 開住嗰陣每分鐘自動刷新（睇住最新盤）
    if (!document.hidden) renderPicksOverlay();
  }, 60 * 1000);
}

function closePicksOverlay() {
  document.getElementById('pkOverlay').style.display = 'none';
  clearInterval(pkOvTimer);
  pkOvTimer = null;
}

// ===== 精選（七重準則全通過，永久保留＋自動結算）=====
let ftOvTimer = null;
let ftScanTimer = null;

function _ocLine(o) {
  if (!o) return '無數據';
  return `上${pct(o.up_r)} 下${pct(o.down_r)}` + (o.push_r != null ? ` 走${pct(o.push_r)}` : '') + `（n=${o.n}）`;
}

function _ftShareText(p) {
  const b = p.brief || {};
  const dName = p.direction === 'up' ? '上盤' : '下盤';
  const L = [];
  L.push('⚽ FootballAnalysis 精選');
  L.push(p.league);
  L.push(`${rn(p.home, p.rank_home)} vs ${rn(p.away, p.rank_away)}`);
  L.push(`開賽：${p.kickoff}`);
  if (p.line) L.push(`尾盤：${p.line}${p.odds ? ' ' + p.odds : ''}`);
  L.push(`方向：${dName}（①⑤⑧⑫⑮⑱ 同方向全部≥50%）`);
  const i1 = b.i1 || {}, i5 = b.i5 || {}, i8 = b.i8 || {};
  if (i1.all) L.push(`① 同初盤及尾盤：全庫 ${_ocLine(i1.all)}`);
  if (i5.all) L.push(`⑤ 對上對賽尾盤：全庫 ${_ocLine(i5.all)}`);
  if (i8.all) L.push(`⑧ 主客入失球±3：全庫 ${_ocLine(i8.all)}`);
  const i12 = b.i12 || {}, i15 = b.i15 || {};
  if (i12.cur) L.push(`⑫ 主場客場排名±2·同尾盤：全庫 ${_ocLine(i12.cur)}`);
  if (i15.zone) L.push(`⑮ 排名±2＋同尾盤·今場水位區【${i15.zone.zone}】：全庫 ${_ocLine(i15.zone)}`);
  const g14 = _gapPlain(b.g14, b.cur_line), g17 = _gapPlain(b.g17, b.cur_line);
  if (g14) L.push(`⑭ 差距：${g14}`);
  if (g17) L.push(`⑰ 差距：${g17}`);
  if (p.played && p.score) {
    L.push(`賽果：${p.score}（${p.result === 'W' ? '✅ 方向命中' : p.result === 'L' ? '❌ 方向未中' : '➖ 走盤'}）`);
  }
  return L.join('\n');
}

function _gapPlain(g, curLine) {   // 純文字版（分享用）
  if (!g) return null;
  const scope = g.all || g.lg;
  if (!scope) return null;
  const parts = [];
  const gt = gd => (gd && (gd.text || gd.gap != null)) ? (gd.text || String(gd.gap)) : null;
  if (scope.d50) {
    parts.push(`現時盤【${curLine || '—'}】 vs 最接近50%盤【${scope.d50.line}】（上${pct(scope.d50.up_r)}）` +
      (gt(scope.d50.gap) ? ` → ${gt(scope.d50.gap)}` : ''));
  }
  if (scope.mode) {
    parts.push(`分佈最多盤【${scope.mode.line}】（n=${scope.mode.n}｜上${pct(scope.mode.up_r)}）` +
      (gt(scope.mode.gap) ? ` → ${gt(scope.mode.gap)}` : ''));
  }
  return parts.join('；') || null;
}

async function _ftShare(p, btn) {
  const text = _ftShareText(p);
  const menu = btn.parentElement.querySelector('.ft-menu');
  if (menu) { menu.remove(); return; }
  const m = document.createElement('div');
  m.className = 'ft-menu';
  const wa = 'https://wa.me/?text=' + encodeURIComponent(text);
  const line = 'https://line.me/R/share/text?text=' + encodeURIComponent(text);
  m.innerHTML = `<a class="btn" href="${wa}" target="_blank" rel="noopener">WhatsApp</a>
    <a class="btn" href="${line}" target="_blank" rel="noopener">LINE</a>
    <button class="btn" data-act="copy">複製文字</button>`;
  btn.parentElement.appendChild(m);
  m.querySelector('[data-act="copy"]').onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      m.querySelector('[data-act="copy"]').textContent = '✓ 已複製';
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      m.querySelector('[data-act="copy"]').textContent = '✓ 已複製';
    }
  };
}

function _ftCard(p) {
  const b = p.brief || {};
  const dName = p.direction === 'up' ? '上盤' : '下盤';
  const res = !p.played ? '<span style="color:var(--dim)">未開賽</span>'
    : (p.result === 'W' ? '<b class="r-up">✅ 命中</b>'
      : p.result === 'L' ? '<b class="r-down">❌ 未中</b>'
      : p.result === 'P' ? '<b>➖ 走盤</b>' : '<span style="color:var(--dim)">待結算</span>');
  const i12 = b.i12, i15 = b.i15, i18 = b.i18;
  const i12v = !i12 ? '不適用'
    : `n=${i12.n}` + (i12.top ? `<br>最多【${esc(i12.top.line || '')}】上${pct(i12.top.up_r)} 下${pct(i12.top.down_r)}` : '');
  const i15v = !i15 ? '不適用'
    : `n=${i15.n}` + (i15.zone ? `<br>今場水位區【${esc(i15.zone.zone)}】上${pct(i15.zone.up_r)} 下${pct(i15.zone.down_r)} 走${pct(i15.zone.push_r)}` : '');
  const i18v = !i18 ? '不適用' : `全庫 ${_ocTxt(i18.all)}<br><span style="opacity:.75">同聯賽 ${_ocTxt(i18.lg)}</span>`;
  return `<div class="pk-card ft-card">
    <div class="pk-top">
      <span class="pk-time">${esc(p.kickoff.slice(5, 16))}　${esc(p.league)}</span>
      <span class="pk-teams">${esc(rn(p.home, p.rank_home))} vs ${esc(rn(p.away, p.rank_away))}</span>
      ${p.score ? `<span class="pk-score">${esc(p.score)}</span>` : ''}
      <span class="pk-tag ${p.direction}">${dName}</span>
      <span class="pk-res">${res}</span>
      <span style="color:var(--dim);font-size:12px">尾盤 ${esc(p.line || '—')}${p.odds ? ' ' + esc(p.odds) : ''}</span>
      <button class="btn ft-refresh" data-mid="${p.id}">⟳ 重新整理</button>
      <button class="btn ft-share">⇗ 分享</button>
    </div>
    <div class="pk-items">
      <div class="pk-it"><div class="t">① 同初盤及尾盤</div><div class="v">${_pairTxt(b.i1)}</div></div>
      <div class="pk-it"><div class="t">⑤ 對上對賽尾盤</div><div class="v">${_pairTxt(b.i5)}</div></div>
      <div class="pk-it"><div class="t">⑧ 主客入失球±3</div><div class="v">${_pairTxt(b.i8)}</div></div>
      <div class="pk-it"><div class="t">⑫ 主場客場排名±2</div><div class="v">${i12v}</div></div>
      <div class="pk-it"><div class="t">⑮ 排名±2＋同尾盤</div><div class="v">${i15v}</div></div>
      <div class="pk-it"><div class="t">⑱ 排名差距±1＋同尾盤</div><div class="v">${i18v}</div></div>
    </div>
    <div class="pk-gap"><span class="lab">⑭ 差距</span>${_gapTxt(b.g14, b.cur_line)}</div>
    <div class="pk-gap"><span class="lab">⑰ 差距</span>${_gapTxt(b.g17, b.cur_line)}</div>
    <div class="pk-gap"><span class="lab">✓ Check</span>${_ftCheckTxt(p)}</div>
  </div>`;
}

function _ftCheckTxt(p) {
  // 精選場係咪中咗 Check 下先 8 組合嘅任何一條；中咗 → 講明邊條＋歷史開出上/下盤率＋統計基數
  const chk = p.check;
  if (!chk) {
    return '<span style="color:var(--dim)">呢場唔中任何一條組合（⑭／⑰ 無深淺差距或樣本不足）</span>';
  }
  // 後端 g14/g17 係「上盤」角度；方向=下時換算做「下盤」角度（deep<->shallow 對調）
  const sw = v => p.direction === 'down' ? (v === 'deep' ? 'shallow' : 'deep') : v;
  const cb = CK_COMBOS.find(c => c.dir === p.direction &&
    c.g14s === sw(chk.g14) && c.g17s === sw(chk.g17));
  return `<b style="color:#ffd766">中咗呢條組合</b>：${esc(cb ? cb.label : '')}` +
    ` → 歷史開出 <b class="r-up">上盤 ${pct(chk.up_r)}</b> ／ <b class="r-down">下盤 ${pct(chk.down_r)}</b>` +
    `<span style="color:var(--dim)">（統計基數 ${chk.n} 場：上 ${chk.up}｜下 ${chk.down}｜走 ${chk.push}，走盤唔計分母）</span>`;
}

async function renderFeaturedOverlay() {
  const body = document.getElementById('ftOvBody');
  let d;
  try { d = await jget('/api/featured/full'); }
  catch (e) { body.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats || {};
  const rate = s.hit_rate != null ? (s.hit_rate * 100).toFixed(1) + '%' : '—';
  document.getElementById('ftOvTitle').textContent =
    `★ 精選｜${s.total || 0} 場（未開賽 ${s.pending || 0}｜已完場 ${s.played || 0}）｜命中 ${s.wins || 0} 場｜命中率 ${rate}`;
  const scan = d.scan || {};
  document.getElementById('ftScanInfo').textContent =
    scan.running ? `掃描中 ${scan.done}/${scan.total}…` :
    (scan.last ? `上次掃描：${scan.last}（+${scan.added} 場）` : '從未掃描');
  let h = '';
  if ((d.pending || []).length) {
    h += `<div class="pk-sec-t">未開賽（順開賽時間排）</div>` + d.pending.map(_ftCard).join('');
  }
  const played = d.played || [];
  if (played.length) {
    h += `<div class="pk-sec-t">已開賽／已完場（${played.length} 場，按日子分類，最新排先）</div>`;
    let lastDate = '';
    for (const p of played) {
      const dd = p.kickoff.slice(0, 10);
      if (dd !== lastDate) {
        h += `<div class="ft-date">${esc(dd)}</div>`;
        lastDate = dd;
      }
      h += _ftCard(p);
    }
  }
  if (!h) h = '<div class="note">暫無精選場次。撳「🔍 掃描精選」喺全部即將開賽嘅場次入面搵（約 1-3 分鐘）。</div>';
  body.innerHTML = h;
  body.querySelectorAll('.ft-share').forEach((btn, i) => {
    const all = [...(d.pending || []), ...played];
    btn.onclick = () => _ftShare(all[i], btn);
  });
  body.querySelectorAll('.ft-refresh').forEach(btn => {
    btn.onclick = async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '⟳ 更新中…';
      try {
        const r = await jpost('/api/featured/refresh', {id: btn.dataset.mid});
        btn.textContent = !r.ok ? ('✕ ' + (r.error || '失敗'))
          : r.removed ? '✕ 已移出精選'
          : '✓ 已更新';
      } catch (e) {
        btn.textContent = '✕ 失敗';
      }
      setTimeout(() => renderFeaturedOverlay(), 1500);
    };
  });
}

async function pollFtScan() {
  const s = await jget('/api/featured/scan-status');
  if (s.running) {
    document.getElementById('ftScanInfo').textContent = `掃描中 ${s.done}/${s.total}…`;
    ftScanTimer = setTimeout(pollFtScan, 3000);
    return;
  }
  document.getElementById('ftScanInfo').textContent =
    (s.last ? `上次掃描：${s.last}（+${s.added} 場）` : '從未掃描') +
    (s.error ? '｜出錯：' + s.error : '');
  document.getElementById('btnFtScan').disabled = false;
  renderFeaturedOverlay();
}

async function startFtScan() {
  const btn = document.getElementById('btnFtScan');
  btn.disabled = true;
  await jpost('/api/featured/scan', {});
  pollFtScan();
}

function openFeaturedOverlay() {
  document.getElementById('ftOverlay').style.display = 'flex';
  renderFeaturedOverlay();
  // 超過 30 分鐘無掃描過 → 自動掃描
  jget('/api/featured/scan-status').then(s => {
    if (!s.running && (!s.last || (Date.now() - new Date(s.last.replace(' ', 'T')).getTime()) > 30 * 60 * 1000)) {
      startFtScan();
    }
  }).catch(() => {});
  clearInterval(ftOvTimer);
  ftOvTimer = setInterval(() => {
    if (!document.hidden) renderFeaturedOverlay();
  }, 60 * 1000);
}

function closeFeaturedOverlay() {
  document.getElementById('ftOverlay').style.display = 'none';
  clearInterval(ftOvTimer);
  clearTimeout(ftScanTimer);
  ftOvTimer = null;
  ftScanTimer = null;
}

// ===== Check 下先（8 組合回測）=====
let ckOvTimer = null;
let ckScanTimer = null;

// 8 組合顯示定義：dir=入選方向；g14s/g17s 以「該方向角度」嘅深/淺顯示
const CK_COMBOS = [
  {dir: 'up',   g14s: 'deep',    g17s: 'deep',    label: '①⑤⑧⑫⑮⑱：上 ＋ ⑭上盤深咗 ＋ ⑰上盤深咗'},
  {dir: 'up',   g14s: 'shallow', g17s: 'deep',    label: '①⑤⑧⑫⑮⑱：上 ＋ ⑭上盤淺咗 ＋ ⑰上盤深咗'},
  {dir: 'up',   g14s: 'deep',    g17s: 'shallow', label: '①⑤⑧⑫⑮⑱：上 ＋ ⑭上盤深咗 ＋ ⑰上盤淺咗'},
  {dir: 'up',   g14s: 'shallow', g17s: 'shallow', label: '①⑤⑧⑫⑮⑱：上 ＋ ⑭上盤淺咗 ＋ ⑰上盤淺咗'},
  {dir: 'down', g14s: 'shallow', g17s: 'shallow', label: '①⑤⑧⑫⑮⑱：下 ＋ ⑭下盤深咗 ＋ ⑰下盤深咗'},
  {dir: 'down', g14s: 'deep',    g17s: 'shallow', label: '①⑤⑧⑫⑮⑱：下 ＋ ⑭下盤淺咗 ＋ ⑰下盤深咗'},
  {dir: 'down', g14s: 'shallow', g17s: 'deep',    label: '①⑤⑧⑫⑮⑱：下 ＋ ⑭下盤深咗 ＋ ⑰下盤淺咗'},
  {dir: 'down', g14s: 'deep',    g17s: 'deep',    label: '①⑤⑧⑫⑮⑱：下 ＋ ⑭下盤淺咗 ＋ ⑰下盤淺咗'},
];
// 換算：後端 g14/g17 以「上盤」角度編碼；方向=下時，下盤深咗=上盤淺咗（deep<->shallow 對調）

async function renderCheckOverlay() {
  const body = document.getElementById('ckOvBody');
  let d;
  try { d = await jget('/api/check/full'); }
  catch (e) { body.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const scan = d.scan || {};
  const totalN = (d.combos || []).reduce((s, c) => s + c.n, 0);
  // 資料齊全但今次進程未跑過掃描（重開伺服器／雲端種入 CSV）：唔好顯示「0/0」
  const seeded = !scan.running && !scan.last && !(scan.done > 0) && totalN > 0;
  document.getElementById('ckOvTitle').textContent = seeded
    ? `✓ Check 下先｜回測資料齊全（全庫）｜入組合 ${totalN} 場`
    : `✓ Check 下先｜已回測 ${scan.done || 0}/${scan.total || 0} 場｜入組合 ${totalN} 場`;
  document.getElementById('ckScanInfo').textContent =
    scan.running ? `回測中 ${scan.done}/${scan.total}…` :
    seeded ? '已載入完整回測結果；按「回測掃描」補上新增賽事' :
    (scan.last ? `上次回測：${scan.last}` : '從未回測（約 30-90 分鐘，可中斷續跑）');
  const map = {};
  for (const c of (d.combos || [])) {
    // 上盤角度 → 該組合顯示角度：方向 up 照用；方向 down 時 deep/shallow 對調
    map[c.direction + '|' + c.g14 + '|' + c.g17] = c;
  }
  let h = '<table class="ck-table"><thead><tr>' +
    '<th>組合</th><th>場數</th><th>開出上盤</th><th>開出下盤</th><th>走盤</th>' +
    '<th>上盤率</th><th>下盤率</th></tr></thead><tbody>';
  for (const cb of CK_COMBOS) {
    let key = cb.dir + '|' + cb.g14s + '|' + cb.g17s;
    if (cb.dir === 'down') {
      const sw = s => s === 'deep' ? 'shallow' : 'deep';
      key = 'down|' + sw(cb.g14s) + '|' + sw(cb.g17s);
    }
    const c = map[key];
    h += '<tr' + (c && c.n >= 20 ? '' : ' class="dim"') + '>' +
      `<td>${esc(cb.label)}</td>` +
      (c ? `<td>${c.n}</td><td>${c.up}</td><td>${c.down}</td><td>${c.push}</td>` +
           `<td>${pct(c.up_r)}</td><td>${pct(c.down_r)}</td>`
         : '<td colspan="6">無數據</td>') +
      '</tr>';
  }
  h += '</tbody></table>';
  h += '<div class="note" style="margin-top:10px">口径：只計①⑤⑧⑫⑮⑱全部通過嘅歷史場次；' +
       '⑭／⑰ 用「上盤勝率最接近 50% 嘅盤口」（不足5場用分佈最多盤）；' +
       '上盤率／下盤率分母已剔除走盤；回測可隨時停止，下次會由停低位繼續。</div>';
  body.innerHTML = h;
}

async function pollCkScan() {
  const s = await jget('/api/check/scan-status');
  if (s.running) {
    document.getElementById('ckScanInfo').textContent = `回測中 ${s.done}/${s.total}…`;
    ckScanTimer = setTimeout(pollCkScan, 5000);
    return;
  }
  document.getElementById('ckScanInfo').textContent =
    (s.last ? `上次回測：${s.last}` : '從未回測') + (s.error ? '｜出錯：' + s.error : '');
  document.getElementById('btnCkScan').disabled = false;
  renderCheckOverlay();
}

async function startCkScan() {
  const btn = document.getElementById('btnCkScan');
  btn.disabled = true;
  await jpost('/api/check/scan', {});
  pollCkScan();
}

function openCheckOverlay() {
  document.getElementById('ckOverlay').style.display = 'flex';
  renderCheckOverlay();
  clearInterval(ckOvTimer);
  ckOvTimer = setInterval(() => {
    if (!document.hidden) renderCheckOverlay();
  }, 60 * 1000);
}

function closeCheckOverlay() {
  document.getElementById('ckOverlay').style.display = 'none';
  clearInterval(ckOvTimer);
  clearTimeout(ckScanTimer);
  ckOvTimer = null;
  ckScanTimer = null;
}

async function fetchOdds(id, quiet) {
  if (!quiet) setStatus('獲取賠率中…（球探網，請稍候）');
  const r = await jpost('/api/fetch', {id});
  if (!quiet) setStatus(r.ok ? '賠率已獲取' : '獲取失敗：' + (r.error||'未知'));
  return r;
}

async function openMatch(id, row) {
  curMatch = id;
  document.querySelectorAll('.mrow').forEach(e => e.classList.remove('active'));
  if (row) row.classList.add('active');
  $('#placeholder').style.display = 'none';
  const R = $('#result');
  R.style.display = 'block';
  // 數據池未就緒就等佢（通常啟動後數秒內完成）
  const ok = await waitPool();
  if (curMatch !== id) return;
  if (!ok) {
    R.innerHTML = `<div class="err">歷史數據池未能載入：${esc(poolErr || '未知原因')}</div>
      <div style="margin-top:10px"><button class="btn accent" id="retryBtn">重試</button></div>`;
    $('#retryBtn').onclick = () => openMatch(id, row);
    return;
  }
  R.innerHTML = '<div class="empty">篩查中…（一般 1-3 秒）</div>';
  let res;
  try {
    const r = await fetch('/api/screen?id=' + id);
    if (!r.ok) throw new Error('伺服器回應 ' + r.status);
    res = await r.json();
  } catch (e) {
    if (curMatch !== id) return;
    R.innerHTML = `<div class="err">載入失敗：${esc(String((e && e.message) || e))}</div>
      <div class="note">請確認 APP 伺服器仍在運行（雙擊桌面「啟動篩查APP」圖示），或按下面重試。</div>
      <div style="margin-top:10px"><button class="btn accent" id="retryBtn">重試</button></div>`;
    $('#retryBtn').onclick = () => openMatch(id, row);
    return;
  }
  if (curMatch !== id) return;   // 用戶已撳咗另一場，捨棄過期結果
  renderResult(res);
}

function ocRow(label, oc, pool) {
  const poolTxt = pool != null ? pool.toLocaleString() : '—';
  if (!oc) return `<tr><td class="l">${label}</td><td class="num">${poolTxt}</td><td colspan="4" class="err">無樣本</td></tr>`;
  return `<tr><td class="l">${label}</td><td class="num">${poolTxt}</td><td class="num">${oc.n}</td>
    <td class="r-up num">${cnt(oc,'up')}</td>
    <td class="r-down num">${cnt(oc,'down')}</td>
    <td class="r-push num">${cnt(oc,'push')}</td></tr>`;
}

let curLeague = '';
let curCategory = '';

function outcomeTable(item) {
  const pool = item.pool || {};
  return `<table><tr><th class="l">範圍</th><th>賽事總數（分母）</th><th>命中</th><th>上盤勝</th><th>下盤勝</th><th>走盤</th></tr>
    ${ocRow('全資料庫', item.all, pool.all)}${ocRow('同聯賽（' + esc(curLeague) + '）', item.league, pool.league)}</table>
    <div class="note">讓球盤：上盤＝讓球方；<b>平手盤：上盤＝平手主隊（主勝）、下盤＝平手客隊（客勝）、走盤＝賽和</b>。勝率分母已剔除走盤。「賽事總數」＝該範圍內有尾盤數據的完場賽事總數。</div>`;
}

function distRender(d, zones) {
  if (!d || !d.n) return `<div class="err">無樣本（分母 ${d && d.pool != null ? d.pool.toLocaleString() : '—'} 場）</div>`;
  const maxN = Math.max(...d.dist.map(r => r.n), 1);
  let h = `<div class="note">樣本 ${d.n} 場／分母 ${d.pool != null ? d.pool.toLocaleString() : '—'} 場　<span style="opacity:.7">水位＝上盤（讓球方）尾盤水位；<b>平手盤：上盤＝平手主隊、下盤＝平手客隊</b>，取低水方。水位格內細字＝上·下·走 率（鼠標停在格上睇詳細）</span></div>`;
  h += `<table><tr><th class="l">盤口</th><th>場次</th><th>上盤勝</th><th>下盤勝</th><th>走盤</th>` +
       zones.map((z,i)=>`<th class="zhead">${ZONES_LABEL[i]}</th>`).join('') + `</tr>`;
  for (const r of d.dist) {
    const w = Math.round(r.n / maxN * 90);
    h += `<tr><td class="l">${esc(r.line)}</td>
      <td><div class="bar-wrap"><div class="bar" style="width:${w}px"></div><span class="num">${r.n}</span></div></td>
      <td class="r-up num">${pct(r.up_r)}</td><td class="r-down num">${pct(r.down_r)}</td>
      <td class="r-push num">${r.push}${r.push_r != null ? ' (' + pct(r.push_r) + ')' : ''}</td>`;
    const zm = Math.max(r.max_zone_n, 1);
    h += r.zones.map(z => {
      if (!z) return '<td class="zone-row"></td>';
      const bg = `background:rgba(63,167,255,${(0.25 + 0.75*z.n/zm).toFixed(2)})`;
      const rates = z.up_r != null
        ? `<span class="zrate">${(z.up_r*100).toFixed(0)}·${(z.down_r*100).toFixed(0)}·${(z.push_r*100).toFixed(0)}</span>`
        : '';
      const tip = `${z.n}場｜上盤(贏1) ${z.up}場${z.up_r!=null?' '+pct(z.up_r):''}｜下盤(輸1) ${z.down}場${z.down_r!=null?' '+pct(z.down_r):''}｜走盤 ${z.push}場${z.push_r!=null?' '+pct(z.push_r):''}`;
      return `<td class="zone-row"><span class="zcell" style="${bg}" title="${tip}"><b>${z.n}</b>${rates}</span></td>`;
    }).join('') + '</tr>';
  }
  h += '</table>';
  return h;
}

function distSection(item) {
  const key = 'd' + Math.random().toString(36).slice(2,7);
  return `<div class="tabs">
    <span class="tab on" data-k="all" data-t="${key}">全庫</span>
    <span class="tab" data-k="league" data-t="${key}">同聯賽</span></div>
    <div id="${key}-all">${distRender(item.all, ZONES)}</div>
    <div id="${key}-league" style="display:none">${distRender(item.league, ZONES)}</div>`;
}

function zoneRates(d) {
  if (!d || !d.n) return `<div class="err">無樣本（分母 ${d && d.pool != null ? d.pool.toLocaleString() : '—'} 場）</div>`;
  let h = `<div class="note">樣本 ${d.n} 場／分母 ${d.pool != null ? d.pool.toLocaleString() : '—'} 場　<span style="opacity:.7">水位＝上盤（讓球方）尾盤水位；<b>平手盤：上盤＝平手主隊、下盤＝平手客隊</b>，取低水方。結果只計 贏1／走盤／輸1</span></div>`;
  h += `<table><tr><th class="l">上盤水位</th><th>場次</th><th>上盤率（贏1）</th><th>下盤率（輸1）</th><th>走盤率</th></tr>`;
  for (const z of d.zones) {
    h += `<tr><td class="l">${esc(z.zone)}</td><td class="num">${z.n}</td>
      <td class="r-up num">${pct(z.up_r)}</td><td class="r-down num">${pct(z.down_r)}</td>
      <td class="r-push num">${pct(z.push_r)}</td></tr>`;
  }
  return h + '</table>';
}

function zoneSection(item) {
  const key = 'z' + Math.random().toString(36).slice(2,7);
  return `<div class="tabs">
    <span class="tab on" data-k="all" data-t="${key}">全庫</span>
    <span class="tab" data-k="league" data-t="${key}">同聯賽</span></div>
    <div id="${key}-all">${zoneRates(item.all)}</div>
    <div id="${key}-league" style="display:none">${zoneRates(item.league)}</div>`;
}

function gapCard(t, g) {
  if (!g) return '<div class="err">無樣本</div>';
  const rates = (g.up_r != null)
    ? `<div>上盤率 <b class="r-up">${pct(g.up_r)}</b>　下盤率 <b class="r-down">${pct(g.down_r)}</b>　走盤率 <b class="r-push">${g.push_r != null ? pct(g.push_r) : '—'}</b>${g.push != null ? '（走 ' + g.push + ' 場）' : ''}</div>`
    : '';
  const ping = g.ref_line === '平手' ? '<div class="note">平手盤：上盤＝平手主隊、下盤＝平手客隊、走盤＝賽和</div>' : '';
  return `<div class="gapcard"><div class="g-t">${t}</div>
    <div class="g-v">${esc(g.line)}（${g.n} 場）</div>${ping}${rates}
    <div>與今場尾盤淨差距：<b>${esc(g.gap.text)}</b>${g.gap.note ? '　<span class="note">'+esc(g.gap.note)+'</span>' : ''}</div></div>`;
}

function i15ResultTable(it) {
  if (it.error) return `<div class="err">${esc(it.error)}</div>`;
  const pool = it.pool || {};
  return `<div class="note">${esc(it.ref || '')}</div>
    <table><tr><th class="l">範圍</th><th>賽事總數（分母）</th><th>命中</th><th>上盤勝</th><th>下盤勝</th><th>走盤</th></tr>
    ${ocRow('全資料庫', it.all, pool.all)}
    ${ocRow('同聯賽（' + esc(curLeague) + '）', it.league, pool.league)}
    ${ocRow('同賽事類別（' + esc(curCategory) + '）', it.cat, pool.cat)}
    </table>
    <div class="note">同時符合全部剔選條件（AND）。勝率分母已剔除走盤。讓球盤：上盤＝讓球方；<b>平手盤：上盤＝平手主隊、下盤＝平手客隊</b>。</div>`;
}

function item20HTML(it, t, titles) {
  let boxes = '';
  for (let k = 1; k <= 19; k++) {
    if (k === 14 || k === 17) continue;   // 分析項，不能剔
    boxes += `<div class="i16-row"><label title="${esc(titles[k] || '')}">
      <input type="checkbox" class="i16-cb" value="${k}" ${it.sel && it.sel.includes(k) ? 'checked' : ''}>
      <b>${k}.</b> ${esc(titles[k] || '')}</label></div>`;
  }
  return `<div class="note">${esc(it.ref || '')}</div>
    <div class="i15-list">${boxes}</div>
    <div style="margin-top:10px"><button class="btn accent" id="i20-submit">提交組合篩查</button></div>
    <div id="i20-result" style="margin-top:10px">${it.sel ? i15ResultTable(it) : '<div class="note">尚未提交</div>'}</div>`;
}

function itemPreview(i, it) {
  if (it.error) return `<span class="pv err">｜${esc(it.error)}</span>`;
  const fmtOC = oc => (oc && oc.n != null)
    ? `${oc.up}／${oc.n}（上 ${pct(oc.up_r)}｜下 ${pct(oc.down_r)}）` : '—';
  if (i <= 9)
    return `<span class="pv">｜全庫 ${fmtOC(it.all)}　同聯賽 ${fmtOC(it.league)}</span>`;
  if (i <= 13 || i == 15 || i == 16 || i == 18 || i == 19)
    return `<span class="pv">｜全庫樣本 ${(it.all && it.all.n) || 0} 場　同聯賽 ${(it.league && it.league.n) || 0} 場</span>`;
  if (i == 14 || i == 17) {
    const m = it.all && it.all.mode, d = it.all && it.all.d50;
    return `<span class="pv">｜全庫：分佈最多 ${m ? esc(m.line) + ' ' + m.n + '場' : '—'}　近50% ${d ? esc(d.line) + ' ' + d.n + '場' : '—'}</span>`;
  }
  if (i == 20)
    return `<span class="pv">｜${it.sel ? '已剔選：' + it.sel.map(s => '第' + s + '項').join('、') : '剔選第1-19項（第14、17項除外）後提交'}</span>`;
  return '';
}

function renderResult(res) {
  if (res.error) { $('#result').innerHTML = '<div class="err">'+esc(res.error)+'</div>'; return; }
  const t = res.target;
  curLeague = t.league;
  curCategory = t.category || '';
  ZONES.length = 0; ZONES.push(...res.zones);
  const myPick = picksMap[t.id] || '';
  let h = `<div class="tcard">
    <h2>${esc(rn(t.home, t.rank_home))} <span style="color:var(--dim)">vs</span> ${esc(rn(t.away, t.rank_away))}</h2>
    <div class="meta">${esc(t.league)}　${esc(t.kickoff)}</div>
    <div class="lines">
      ${lineBox('尾盤（檢查基準）', t.close)}
      ${lineBox('初盤', t.init)}
      ${lineBox('開賽前4小時', t.h4)}
    </div>
    <div class="tbtns">
      <button class="btn" id="btnRefreshLine">⟳ 重新整理盤口及水位</button>
    </div></div>`;

  const titles = {};
  for (const [k, v] of Object.entries(res.items)) titles[k] = v.title;

  for (let i = 1; i <= 20; i++) {
    const it = res.items[String(i)];
    if (!it) continue;
    let body = '';
    if (i == 20) {
      body = it.error ? `<div class="err">${esc(it.error)}</div>` : item20HTML(it, t, titles);
    } else if (it.error) {
      body = `<div class="err">${i >= 10 && it.ref ? '' : esc(it.ref || '')} ${esc(it.error)}</div>`;
    } else if (i <= 9) {
      body = (it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '') + outcomeTable(it);
    } else if (i <= 13) {
      body = (it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '') + distSection(it);
    } else if (i == 15) {
      body = (it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '') + zoneSection(it);
    } else if (i == 18 || i == 19) {
      const ocLine = d => {
        if (!d || !d.oc) return '';
        const oc = d.oc;
        return `<div class="note">結果：上盤 ${oc.up} 場（${pct(oc.up_r)}）｜下盤 ${oc.down} 場（${pct(oc.down_r)}）｜走盤 ${oc.push} 場（${pct(oc.push_r)}）　<b>樣本 ${oc.n} 場</b></div>`;
      };
      body = (it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '')
        + ocLine(it.all) + ocLine(it.league)
        + '<h4 style="margin:12px 0 4px">各水位 上盤率／下盤率／走盤率</h4>'
        + zoneSection({all: it.all, league: it.league});
    } else if (i == 16) {
      body = (it.ref ? `<div class="note">參照：${esc(it.ref)}</div>` : '')
        + distSection({all: {n: it.all.n, pool: it.all.pool, dist: it.all.dist},
                       league: {n: it.league.n, pool: it.league.pool, dist: it.league.dist}})
        + '<h4 style="margin:12px 0 4px">各水位 上盤率／下盤率／走盤率</h4>'
        + zoneSection({all: it.all, league: it.league});
    } else {
      body = `<div class="note">參照：${esc(it.ref || '')}</div>
        <h4 style="margin:8px 0 4px">全庫</h4>${gapCard('分佈最多的盤口', it.all && it.all.mode)}${gapCard('上盤勝率最接近 50% 的盤口（至少5場）', it.all && it.all.d50)}
        <h4 style="margin:12px 0 4px">同聯賽（${esc(t.league)}）</h4>${gapCard('分佈最多的盤口', it.league && it.league.mode)}${gapCard('上盤勝率最接近 50% 的盤口（至少5場）', it.league && it.league.d50)}`;
    }
    h += `<details><summary><span class="sum-t">${i}. ${esc(it.title)}</span>${it.ref && i<=9 ? `<span class="sub">${esc(it.ref)}</span>` : ''}${itemPreview(i, it)}</summary><div class="body">${body}</div></details>`;
  }
  // 我的選擇（上/下盤）——放喺頁最底
  h += `<div class="tbtns pk-bar">
      <span class="pk-lab">我的選擇：</span>
      <button class="btn pk ${myPick==='up'?'on':''}" data-pk="up">上盤</button>
      <button class="btn pk ${myPick==='down'?'on':''}" data-pk="down">下盤</button>
      <span id="pkMsg" class="note" style="display:inline-block;margin:0 0 0 8px"></span>
    </div>`;
  $('#result').innerHTML = h;

  // 重新整理盤口及水位：強制重新抓取，然後重新篩查
  const rf = document.getElementById('btnRefreshLine');
  if (rf) rf.onclick = async () => {
    rf.disabled = true;
    rf.textContent = '⟳ 更新中…';
    try {
      const r = await jpost('/api/fetch', {id: t.id, force: true});
      const m = document.getElementById('pkMsg');
      if (m) m.textContent = r.ok ? '已更新，重新篩查中…' : '更新失敗：' + (r.error || '未知');
      loadList();
      if (r.ok) openMatch(t.id);
    } finally {
      rf.disabled = false;
      rf.textContent = '⟳ 重新整理盤口及水位';
    }
  };
  document.querySelectorAll('.tbtns .pk').forEach(b => {
    b.onclick = () => togglePick(t.id, b.dataset.pk);
  });

  const i20btn = document.getElementById('i20-submit');
  if (i20btn) i20btn.onclick = async () => {
    const sel = [...document.querySelectorAll('.i16-cb:checked')].map(cb => cb.value);
    if (!sel.length) {
      document.getElementById('i20-result').innerHTML = '<div class="err">請先剔選至少一項</div>';
      return;
    }
    i20btn.disabled = true;
    document.getElementById('i20-result').innerHTML = '<div class="note">組合篩查中…</div>';
    let r18;
    try {
      const res2 = await jget('/api/screen?id=' + t.id + '&sel=' + sel.join(','));
      r18 = (res2.items && res2.items['20']) || {error: '結果格式錯誤'};
    } catch (e) {
      r18 = {error: '載入失敗：' + String((e && e.message) || e)};
    }
    document.getElementById('i20-result').innerHTML = i15ResultTable(r18);
    i20btn.disabled = false;
  };

  document.querySelectorAll('.tab').forEach(tab => {
    tab.onclick = e => {
      e.stopPropagation();
      const k = tab.dataset.k, id = tab.dataset.t;
      tab.parentElement.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
      tab.classList.add('on');
      ['all','league'].forEach(x => {
        const el = document.getElementById(id + '-' + x);
        if (el) el.style.display = (x === k) ? 'block' : 'none';
      });
    };
  });
  $('#resultPane').scrollTop = 0;
}

function lineBox(title, o) {
  if (!o) return `<div class="linebox"><div class="lb-t">${title}</div><div class="lb-v">無數據</div></div>`;
  return `<div class="linebox"><div class="lb-t">${title}</div>
    <div class="lb-v">${esc(o.line)}</div>
    <div class="lb-w">主 ${o.ho}／客 ${o.ao}</div></div>`;
}

$('#btnRefresh').onclick = loadList;
$('#hours').onchange = loadList;

// ===== 我的選擇 · 大版面 =====
$('#btnPicksBig').onclick = openPicksOverlay;
$('#pkOvClose').onclick = closePicksOverlay;

// ===== 精選 · 大版面 =====
$('#btnFeatured').onclick = openFeaturedOverlay;
$('#ftOvClose').onclick = closeFeaturedOverlay;
$('#btnFtScan').onclick = startFtScan;

// ===== Check 下先 =====
$('#btnCheck').onclick = openCheckOverlay;
$('#ckOvClose').onclick = closeCheckOverlay;
$('#btnCkScan').onclick = startCkScan;

// ===== 過往賽果 =====
let rsFilter = {league: '', hc: '', gv: ''};   // '' = 全部

function _pct_(v) { return v == null ? '—' : (v * 100).toFixed(1) + '%'; }

function _rsCard(m) {
  const tag = m.outcome === 'up' ? '<b class="r-up">上盤</b>'
    : m.outcome === 'down' ? '<b class="r-down">下盤</b>'
    : m.outcome === 'push' ? '<b>➖ 走盤</b>'
    : '<span style="color:var(--dim)">無尾盤</span>';
  return `<div class="rs-card">
    <span class="pk-time">${esc(m.kickoff.slice(5, 16))}　${esc(m.league)}</span>
    <span class="pk-teams">${esc(m.home)} <b>${esc(m.score)}</b> ${esc(m.away)}</span>
    <span style="color:var(--dim);font-size:12px">${esc(m.line || '—')}${m.odds ? ' ' + esc(m.odds) : ''}</span>
    <span class="pk-res">${tag}</span>
  </div>`;
}

function _rsDrawTrend(canvas, trend) {
  // 上盤 vs 下盤逐日走勢：藍柱＝上盤率，紅柱＝下盤率（分母剔除走盤），50% 參考線
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth * (window.devicePixelRatio || 1);
  const H = canvas.height = canvas.clientHeight * (window.devicePixelRatio || 1);
  ctx.clearRect(0, 0, W, H);
  if (!trend || !trend.length) {
    ctx.fillStyle = '#8a93a6';
    ctx.font = `${14 * (window.devicePixelRatio || 1)}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.fillText('暫無走勢數據', W / 2, H / 2);
    return;
  }
  const dpr = window.devicePixelRatio || 1;
  const padL = 44 * dpr, padR = 10 * dpr, padT = 22 * dpr, padB = 30 * dpr;
  const cw = (W - padL - padR) / trend.length;
  const yOf = v => padT + (1 - v) * (H - padT - padB);
  const css = getComputedStyle(document.body);
  const cUp = css.getPropertyValue('--up').trim() || '#3fa7ff';
  const cDown = css.getPropertyValue('--down').trim() || '#ff6b6b';
  const cDim = '#8a93a6';
  // 格線 0/25/50/75/100%
  ctx.font = `${10 * dpr}px sans-serif`;
  ctx.textAlign = 'right';
  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    const y = yOf(v);
    ctx.strokeStyle = v === 0.5 ? 'rgba(255,215,102,.55)' : 'rgba(138,147,166,.18)';
    ctx.lineWidth = (v === 0.5 ? 1.5 : 1) * dpr;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(W - padR, y);
    ctx.stroke();
    ctx.fillStyle = cDim;
    ctx.fillText(Math.round(v * 100) + '%', padL - 5 * dpr, y + 3 * dpr);
  }
  trend.forEach((t, i) => {
    const eff = t.up + t.down;
    const upr = eff ? t.up / eff : 0;
    const downr = eff ? t.down / eff : 0;
    const bw = Math.max(2 * dpr, cw * 0.32);
    const x = padL + i * cw + cw / 2;
    // 上盤柱（向上係基於 yOf：值大＝柱頂高）
    ctx.fillStyle = cUp;
    ctx.fillRect(x - bw - 1 * dpr, yOf(upr), bw, yOf(0) - yOf(upr));
    ctx.fillStyle = cDown;
    ctx.fillRect(x + 1 * dpr, yOf(downr), bw, yOf(0) - yOf(downr));
  });
  // 日期標籤（頭/尾/中間，最多 6 個）
  ctx.fillStyle = cDim;
  ctx.textAlign = 'center';
  const step = Math.max(1, Math.ceil(trend.length / 6));
  trend.forEach((t, i) => {
    if (i % step === 0 || i === trend.length - 1) {
      ctx.fillText(t.date.slice(5), padL + i * cw + cw / 2, H - 10 * dpr);
    }
  });
  // 圖例
  ctx.textAlign = 'left';
  ctx.fillStyle = cUp;
  ctx.fillText('■ 上盤率', padL, 14 * dpr);
  ctx.fillStyle = cDown;
  ctx.fillText('■ 下盤率', padL + 70 * dpr, 14 * dpr);
  ctx.fillStyle = '#ffd766';
  ctx.fillText('― 50%', padL + 140 * dpr, 14 * dpr);
}

function _rsFilterBar(d) {
  const lgOpts = ['<option value="">全部聯賽</option>']
    .concat((d.leagues || []).map(l =>
      `<option value="${esc(l)}" ${rsFilter.league === l ? 'selected' : ''}>${esc(l)}</option>`));
  const lnOpts = ['<option value="">全部盤口</option>']
    .concat((d.lines || []).map(o => {
      const v = `${o.hc}|${o.gv}`;
      return `<option value="${v}" ${rsFilter.hc !== '' && String(rsFilter.hc) === String(o.hc) && rsFilter.gv === o.gv ? 'selected' : ''}>${esc(o.line)}（${o.n} 場）</option>`;
    }));
  return `<div class="rs-filter">
    <label>聯賽 <select id="rsLg">${lgOpts.join('')}</select></label>
    <label>盤口 <select id="rsLn">${lnOpts.join('')}</select></label>
    <span style="color:var(--dim);font-size:12px">篩選後統計、走勢、列表全部跟住變</span>
  </div>`;
}

async function renderResultsOverlay() {
  const body = document.getElementById('rsOvBody');
  body.innerHTML = '<div class="empty">載入中…</div>';
  const q = new URLSearchParams({limit: '1000'});
  if (rsFilter.league) q.set('league', rsFilter.league);
  if (rsFilter.hc !== '') { q.set('hc', rsFilter.hc); q.set('gv', rsFilter.gv || 'none'); }
  let d;
  try { d = await jget('/api/results?' + q.toString()); }
  catch (e) { body.innerHTML = '<div class="err">載入失敗：' + esc(String(e)) + '</div>'; return; }
  const s = d.stats || {};
  const f = d.filter || {};
  const fLine = f.hc != null
    ? ((d.lines.find(o => String(o.hc) === String(f.hc) && o.gv === (f.gv || 'none')) || {}).line)
    : null;
  const fTxt = (f.league ? '｜' + f.league : '') + (fLine ? '｜' + fLine : '');
  document.getElementById('rsOvTitle').textContent =
    `📋 過往賽果${fTxt}｜統計 ${s.total || 0} 場（顯示最近 ${(d.items || []).length} 場，最新排先）`;
  const cell = (lab, val, sub) =>
    `<div class="rs-stat"><div class="rs-lab">${lab}</div><div class="rs-val">${val}</div>` +
    (sub ? `<div class="rs-sub">${sub}</div>` : '') + '</div>';
  let h = _rsFilterBar(d);
  h += '<div class="rs-stats">';
  h += cell('上盤命中率', _pct_(s.up_r), `上${s.up || 0} 下${s.down || 0} 走${s.push || 0}（走盤唔計分母）`);
  h += cell('下盤命中率', _pct_(s.down_r), '同上，分母剔除走盤');
  h += cell('總命中率', _pct_(s.decisive_r), '開出上/下盤結果嘅比例（＝1－走盤率）');
  h += cell('精選命中率', _pct_(s.feat_r),
    s.feat_w != null ? `中${s.feat_w} 錯${s.feat_l} 走${s.feat_p || 0}` : '');
  h += cell('我的選擇命中率', _pct_(s.pk_r),
    s.pk_w != null ? `中${s.pk_w} 錯${s.pk_l} 走${s.pk_p || 0}` : '');
  h += '</div>';
  h += `<div class="rs-trend-w"><div class="rs-trend-t">上盤 vs 下盤逐日走勢（最近 ${(d.trend || []).length} 日，跟篩選）</div>
    <canvas id="rsTrend" class="rs-trend"></canvas></div>`;
  let lastDate = '';
  for (const m of (d.items || [])) {
    const dd = m.kickoff.slice(0, 10);
    if (dd !== lastDate) {
      h += `<div class="ft-date">${esc(dd)}</div>`;
      lastDate = dd;
    }
    h += _rsCard(m);
  }
  if (!(d.items || []).length) h += '<div class="note">暫無已完場賽事。</div>';
  body.innerHTML = h;
  // 篩選聯動
  document.getElementById('rsLg').onchange = ev => {
    rsFilter.league = ev.target.value;
    renderResultsOverlay();
  };
  document.getElementById('rsLn').onchange = ev => {
    const v = ev.target.value;
    if (!v) { rsFilter.hc = ''; rsFilter.gv = ''; }
    else { const [hc, gv] = v.split('|'); rsFilter.hc = hc; rsFilter.gv = gv; }
    renderResultsOverlay();
  };
  _rsDrawTrend(document.getElementById('rsTrend'), d.trend || []);
}

function openResultsOverlay() {
  document.getElementById('rsOverlay').style.display = 'flex';
  renderResultsOverlay();
}
function closeResultsOverlay() {
  document.getElementById('rsOverlay').style.display = 'none';
}
$('#btnResults').onclick = openResultsOverlay;
$('#rsOvClose').onclick = closeResultsOverlay;
// 測試用：?autocheck=1 自動打開 Check 下先；?autopk=1 自動打開我的選擇大版面；?autofeat=1 精選
if (location.search.indexOf('autocheck=1') >= 0) {
  setTimeout(openCheckOverlay, 800);
}
if (location.search.indexOf('autopk=1') >= 0) {
  setTimeout(openPicksOverlay, 800);
}
if (location.search.indexOf('autofeat=1') >= 0) {
  setTimeout(openFeaturedOverlay, 800);
}
if (location.search.indexOf('autors=1') >= 0) {
  setTimeout(openResultsOverlay, 800);
}

// ===== 各頁「⟳ 刷新盤口」：更新即時盤口＋賠率，重算精選 =====
let refreshing = false;
function setOvStatus(msg) {
  ['pkOvStatus', 'ftScanInfo', 'ckScanInfo'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = msg;
  });
}
async function pollRefresh() {
  for (;;) {
    const s = await jget('/api/update-status');
    if (!s.running) {
      if (s.error) { setOvStatus('更新出錯：' + String(s.error).split('\n')[0]); return false; }
      const r = s.last_result || {};
      setOvStatus(`✓ 已更新（補盤口 ${r.odds || 0} 場／刷新已存在場次 ${r.odds_refresh || '—'}）｜精選重算中…`);
      break;
    }
    setOvStatus('⟳ ' + (s.phase || '更新緊最新盤口及賠率') + '…');
    await new Promise(rs => setTimeout(rs, 3000));
  }
  for (;;) {   // 等精選重算完成
    const f = await jget('/api/featured/scan-status');
    if (!f.running) break;
    setOvStatus(`⟳ 精選重算中 ${f.done}/${f.total}…`);
    await new Promise(rs => setTimeout(rs, 3000));
  }
  return true;
}
async function refreshOddsAll() {
  if (refreshing) { setOvStatus('⟳ 更新緊，唔好重複撳…'); return; }
  refreshing = true;
  document.querySelectorAll('.ov-refresh').forEach(b => b.disabled = true);
  setOvStatus('⟳ 更新緊最新盤口及賠率…');
  try {
    const r = await jpost('/api/update', {});
    if (r.error) { setOvStatus('更新：' + r.error); return; }
    if (!(await pollRefresh())) return;
    setOvStatus('✓ 完成：盤口＋賠率已更新，精選已重算');
    await loadList();
    if (curMatch) openMatch(curMatch);
    if (document.getElementById('pkOverlay').style.display !== 'none') renderPicksOverlay();
    if (document.getElementById('ftOverlay').style.display !== 'none') renderFeaturedOverlay();
    if (document.getElementById('ckOverlay').style.display !== 'none') renderCheckOverlay();
  } catch (e) {
    setOvStatus('更新失敗：' + e);
  } finally {
    refreshing = false;
    document.querySelectorAll('.ov-refresh').forEach(b => b.disabled = false);
  }
}
document.querySelectorAll('.ov-refresh').forEach(b => b.onclick = refreshOddsAll);

// ===== 一鍵更新賽事（賽果＋新場次＋已存在場次嘅盤口及賠率全部刷新）=====
async function pollUpdate() {
  const s = await jget('/api/update-status');
  if (s.running) {
    setStatus('⟳ 更新中：' + (s.phase || '…') + '（賽果＋新場次＋盤口賠率）');
    setTimeout(pollUpdate, 3000);
    return;
  }
  if (s.error) {
    setStatus('更新出錯：' + s.error.split('\n')[0]);
    return;
  }
  const r = s.last_result || {};
  setStatus(`✓ 更新完成（賽季檔 ${r.seasons || 0} 個／補盤口 ${r.odds || 0} 場` +
            (r.odds_fail ? `／失敗 ${r.odds_fail}` : '') +
            (r.odds_refresh ? `／已存在場次盤口賠率刷新 ${r.odds_refresh}` : '') + '）');
  await loadList();
  if (curMatch) openMatch(curMatch);   // 用新數據重新篩查已選場次
}

async function startUpdate(auto) {
  try {
    const r = await jpost('/api/update', {auto: !!auto});
    if (r.skipped) return;                       // 30 分鐘內已自動更新過
    if (r.running || r.started) { pollUpdate(); return; }
    if (r.error) setStatus('更新：' + r.error);
  } catch (e) { /* 伺服器舊版無此端點時靜默 */ }
}

$('#btnUpdate').onclick = async () => {
  $('#btnUpdate').disabled = true;
  try { await startUpdate(false); }
  finally { setTimeout(() => { $('#btnUpdate').disabled = false; }, 1200); }
};

// 每次開 APP 自動更新一次（伺服器會節流：30 分鐘內只跑一次）
setTimeout(() => startUpdate(true), 2500);

$('#btnFetchAll').onclick = async () => {
  const rows = [...document.querySelectorAll('.mrow')];
  const need = rows.filter(r => !r.querySelector('.badge.ok'));
  if (!need.length) { setStatus('全部已有賠率'); return; }
  const CAP = 300;   // 防止一次過幾千場跑幾個鐘
  const todo = need.slice(0, CAP);
  $('#btnFetchAll').disabled = true;
  for (const r of todo) {
    setStatus(`獲取賠率中… ${r.querySelector('.mid').textContent}` +
              (need.length > CAP ? `（${todo.indexOf(r) + 1}/${todo.length}，其餘 ${need.length - CAP} 場未處理）` : ''));
    await fetchOdds(r.dataset.id, true);
    await loadList();
  }
  $('#btnFetchAll').disabled = false;
  setStatus(need.length > CAP ? `已獲取前 ${CAP} 場（其餘 ${need.length - CAP} 場請篩時段後再撳）` : '全部獲取完成');
};

loadList();
loadPicks();
pollReady();

// ===== 自動更新（唔使人手撳 Refresh）=====
// 每 5 分鐘自動刷新賽事清單（分頁唔活躍時暫停；已選場次狀態保留）
setInterval(() => {
  if (!document.hidden) loadList().catch(() => {});
}, 5 * 60 * 1000);

// 每 15 分鐘自動更新「2 小時內開賽」場次嘅賠率（只更新超過 10 分鐘未刷過嘅，逐場順序避免限流）
let autoOddsBusy = false;
setInterval(async () => {
  if (document.hidden || autoOddsBusy || !lastList.length) return;
  const now = Date.now();
  const soon = lastList.filter(m => {
    const t = new Date(m.kickoff.replace(' ', 'T')).getTime();
    const stale = m.fetched_ago == null || m.fetched_ago > 600;
    return t > now && t - now < 2 * 3600 * 1000 && stale;
  });
  if (!soon.length) return;
  autoOddsBusy = true;
  try {
    for (const m of soon) {
      await fetchOdds(m.id, true);
      await new Promise(r => setTimeout(r, 800));
    }
    await loadList();   // 刷新「已有賠率 · N秒前更新」標記
    if (curMatch) openMatch(curMatch);  // 已選場次一齊重篩，睇到最新盤
  } catch (e) { /* 靜默，下次再試 */ }
  autoOddsBusy = false;
}, 15 * 60 * 1000);
