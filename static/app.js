/* 足球篩查 APP 前端 */
const $ = s => document.querySelector(s);
const ZONES_LABEL = ['≤.69','.70','.75','.80','.85','.90','.95','1.00','1.05','≥1.10'];
const ZONES = ZONES_LABEL.slice();   // 實際用嘅水位分區，render 時會用伺服器版本覆寫內容
let curMatch = null;

function setStatus(t) { $('#status').textContent = t; }

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
  if (!list.length) { box.innerHTML = '<div class="empty">未來'+hours+'小時無賽事</div>'; listCount = 0; lastList = []; listUpdatedAt = new Date(); updateStatus(); return; }
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
      <div class="mid">${esc(m.home)} <span style="color:var(--dim)">vs</span> ${esc(m.away)}</div>
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
    h += `<tr><td class="l">${esc(p.kickoff.slice(5, 16))} ${esc(p.home)} vs ${esc(p.away)}`
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
    <h2>${esc(t.home)} <span style="color:var(--dim)">vs</span> ${esc(t.away)}</h2>
    <div class="meta">${esc(t.league)}　${esc(t.kickoff)}</div>
    <div class="lines">
      ${lineBox('尾盤（檢查基準）', t.close)}
      ${lineBox('初盤', t.init)}
      ${lineBox('開賽前4小時', t.h4)}
    </div>
    <div class="tbtns">
      <button class="btn" id="btnRefreshLine">⟳ 重新整理盤口及水位</button>
      <span class="pk-lab">我的選擇：</span>
      <button class="btn pk ${myPick==='up'?'on':''}" data-pk="up">上盤</button>
      <button class="btn pk ${myPick==='down'?'on':''}" data-pk="down">下盤</button>
      <span id="pkMsg" class="note" style="display:inline-block;margin:0 0 0 8px"></span>
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

// ===== 一鍵更新賽事（賽果＋新場次＋最近三日盤口）=====
async function pollUpdate() {
  const s = await jget('/api/update-status');
  if (s.running) {
    setStatus('⟳ 更新中：' + (s.phase || '…') + '（賽果＋新場次＋盤口）');
    setTimeout(pollUpdate, 3000);
    return;
  }
  if (s.error) {
    setStatus('更新出錯：' + s.error.split('\n')[0]);
    return;
  }
  const r = s.last_result || {};
  setStatus(`✓ 更新完成（賽季檔 ${r.seasons || 0} 個／補盤口 ${r.odds || 0} 場` +
            (r.odds_fail ? `／失敗 ${r.odds_fail}` : '') + '）');
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
  $('#btnFetchAll').disabled = true;
  for (const r of need) {
    setStatus(`獲取賠率中… ${r.querySelector('.mid').textContent}`);
    await fetchOdds(r.dataset.id, true);
    await loadList();
  }
  $('#btnFetchAll').disabled = false;
  setStatus('全部獲取完成');
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
