/* 精選8s 前端（2026-10-14）：專搵上盤。
   資料來源：/api/fw8s/full（即將開賽＋觀望）、/api/fw8s/rules（每聯賽公式＋逐年分段）、
   /api/fw8s/history（過往入選結算）。10 分鐘伺服器快取，force=1 跳過。 */
(function () {
  var $ = function (s) { return document.querySelector(s); };
  var body = $('#body'), status = $('#status');
  var hoursSel = $('#hoursSel');
  var curTab = 'up';
  var fullCache = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pct(x, d) {
    if (x == null || isNaN(x)) return '—';
    return (x * 100).toFixed(d == null ? 1 : d) + '%';
  }
  function toast(msg) {
    var t = $('#toast');
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(toast._h);
    toast._h = setTimeout(function () { t.style.display = 'none'; }, 2600);
  }
  function jfetch(url, tries) {
    /* 2026-10-15：每個嘗試 20 秒硬逾時（AbortController）——冇逾時時網絡凍結
       會 hang 到天地老（「撳極無反應」元兇）；逾時同 502/503/504／斷線
       一律退避重試，用盡先抛人話錯誤。 */
    tries = tries == null ? 2 : tries;
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 20000);
    return fetch(url, { signal: ctl.signal }).then(function (r) {
      clearTimeout(timer);
      if (!r.ok) throw { status: r.status, msg: 'HTTP ' + r.status };
      return r.json();
    }).catch(function (e) {
      clearTimeout(timer);
      var st = e && e.status;
      var timedOut = e && e.name === 'AbortError';
      if (tries > 0 && (st === 502 || st === 503 || st === 504 || st == null)) {
        /* 502/503/504（Render 閘道／部屬緊／無回應）、網絡斷線同逾時：自動重試，唔好一撳就話失敗 */
        return new Promise(function (res) { setTimeout(res, 2000); })
          .then(function () { return jfetch(url, tries - 1); });
      }
      throw new Error(timedOut ? '回應逾時，請稍候再試'
                               : (e && e.msg ? e.msg : String(e)));
    });
  }

  /* ---------- 統計條 ---------- */
  function renderSum(d) {
    var s = d.summary || {};
    var html =
      si('未來' + (d.summary && d.summary.hours || hoursSel.value) + '小時', s.picks || 0, 'gold') +
      si('觀望', s.watched || 0, '') +
      si('直選聯賽', s.direct_leagues || 0, '') +
      si('精煉聯賽', s.refine_leagues || 0, '') +
      si('觀望聯賽', s.watch_leagues || 0, '');
    $('#sumbar').innerHTML = html;
    $('#upCnt').textContent = s.picks ? '(' + s.picks + ')' : '';
  }
  function si(t, v, cls) {
    return '<div class="si"><div class="t">' + esc(t) + '</div>' +
      '<div class="v ' + (cls || '') + '">' + esc(v) + '</div></div>';
  }

  /* ---------- Tab 1：即將開賽 ---------- */
  function modeChip(m, miss) {
    if (m === 'direct') return '<span class="badge direct">直選</span>';
    if (m === 'refine') return '<span class="badge refine">精煉</span>';
    if (m === 'refine_miss')
      return '<span class="badge refine">精煉</span><span class="badge watchb">條件未過</span>';
    return '<span class="badge watchb">觀望</span>';
  }
  function pickRow(r, isPick) {
    var h = '<div class="mrow ' + (isPick ? 'pick' : 'watch') + '"><div class="m-top">' +
      '<span class="ko">' + esc((r.kickoff || '').slice(5, 16)) + '</span>' +
      '<span class="lg">' + esc(r.league) + '</span>' +
      '<span class="tm">' + esc(r.home) + ' vs ' + esc(r.away) + '</span>' +
      (isPick ? '<span class="badge up">⬆️ 上盤</span>' : '') +
      modeChip(r.mode) +
      '</div>' +
      '<div class="m-top" style="margin-top:4px">' +
      '<span class="ln">' + esc(r.line || '') + (r.odds ? '　' + esc(r.odds) : '') + '</span>' +
      '<span class="ln" style="flex:1"></span>' +
      '<span class="ln">聯賽歷史 <b>' + pct(r.hist_up_r) + '</b>（n=' +
        (r.hist_n || 0) + '）</span>' +
      '</div>';
    if (r.mode === 'refine' && r.conds && r.conds.length)
      h += '<div class="sub">精煉條件：' + esc(r.conds.join('、')) + '</div>';
    if (r.mode === 'refine_miss' && r.miss_conds && r.miss_conds.length)
      h += '<div class="sub">未過條件：' + esc(r.miss_conds.join('、')) + '</div>';
    if (!isPick)
      h += '<div class="sub">共識方向上盤但' +
        (r.mode === 'watch' ? '聯賽歷史未過 65%，先觀望' : '精煉條件未過，先觀望') + '</div>';
    return h + '</div>';
  }
  function renderUp(d) {
    var h = '';
    if (d.picks && d.picks.length) {
      h += '<div class="sec-h">🎯 入選（' + d.picks.length + '）—— 格共識方向上盤＋聯賽公式通過</div>';
      d.picks.forEach(function (r) { h += pickRow(r, true); });
    } else {
      h += '<div class="sec-h">🎯 入選（0）</div><div class="empty">未來時段暫無入選場次</div>';
    }
    if (d.watched && d.watched.length) {
      h += '<div class="sec-h">👁 觀望（' + d.watched.length + '）—— 方向上盤但公式未通過，唔會計入精選8s</div>';
      d.watched.forEach(function (r) { h += pickRow(r, false); });
    }
    if (d.settled_now)
      h += '<div class="note">剛結算 ' + d.settled_now + ' 場（去「過往紀錄」睇結果）</div>';
    body.innerHTML = h;
  }

  /* ---------- Tab 2：每聯賽公式 ---------- */
  function renderRules(d) {
    var rows = d.rows || [];
    var h = '<div class="note">每聯賽／杯賽獨立公式：方向=「格共識上」嘅場次命中率（check_rows 全庫回測口徑）。' +
      '<b style="color:#7ee2a0">直選</b>＝歷史 ≥65% 直接用；' +
      '<b style="color:#9ecbff">精煉</b>＝加咗過濾條件先過 65%；' +
      '<b>觀望</b>＝未過 65%，暫時唔會入選。</div>';
    h += '<div class="sumbar">' +
      si('全庫上盤場次', (d.total && d.total.n) || 0, '') +
      si('全庫命中率', pct(d.total && d.total.up_r), 'up') +
      si('聯賽數', rows.length, 'gold') +
      '</div>';
    h += '<table class="t"><thead><tr><th>聯賽／杯賽</th><th>模式</th><th>n</th>' +
      '<th>上盤率</th><th>逐年（n＋%）</th><th>精煉條件</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      var yrs = Object.keys(r.years || {}).map(function (y) {
        var v = r.years[y];
        return y + '：' + (v.up_r == null ? '—' : pct(v.up_r, 0)) + '(n=' + v.n + ')';
      }).join('　');
      var modeTxt = { direct: '直選', refine: '精煉', watch: '觀望' }[r.mode] || r.mode;
      var pcCls = (r.up_r != null && r.up_r >= 0.65) ? 'pc-up' : 'pc-dn';
      h += '<tr class="mode-' + esc(r.mode) + '"><td class="l">' + esc(r.league) + '</td>' +
        '<td>' + modeTxt + '</td><td>' + r.n + '</td>' +
        '<td class="' + pcCls + '">' + pct(r.up_r) + '</td>' +
        '<td class="l yrs">' + esc(yrs) + '</td>' +
        '<td class="l">' + esc((r.conds || []).join('、') || '') + '</td></tr>';
    });
    h += '</tbody></table>';
    body.innerHTML = h;
  }

  /* ---------- Tab 2b：全庫實跑（8 格共識真跑全歷史） ---------- */
  function renderBt(d) {
    var t = d.total || {};
    var h = '<div class="note">用「精選8」同一個 8 格共識引擎<b>真跑晒成個資料庫</b>（2021–2026 全部聯賽/杯賽，' +
      (d.total_eval || 0).toLocaleString() + ' 場有賽果場次逐場評估）。' +
      '結論：<b style="color:#7ee2a0">8 格完全一致指上盤嘅場次，全庫 ' +
      (d.up_consensus_n || 0) + ' 次</b>——上盤訊號要落到 4 格公式先存在' +
      '（見「每聯賽公式」分頁）。下表係各聯賽/杯賽共識指下盤嘅實跑結果。</div>';
    h += '<div class="sumbar">' +
      si('評估場次', (d.total_eval || 0).toLocaleString(), '') +
      si('共識場次', t.n || 0, 'gold') +
      si('下盤開出', t.b || 0, 'up') +
      si('下盤輸', t.a || 0, '') +
      si('走盤', t.p || 0, '') +
      si('下盤開出率', pct(t.down_r), 'up') +
      '</div>';
    h += '<table class="t"><thead><tr><th>聯賽／杯賽</th><th>共識場次</th>' +
      '<th>下盤開出</th><th>下盤輸</th><th>走盤</th><th>下盤開出率</th></tr></thead><tbody>';
    (d.rows || []).forEach(function (r) {
      var dn = r.down || { n: 0, a: 0, b: 0, p: 0, hit_r: null };
      var pcCls = (dn.hit_r != null && dn.hit_r >= 0.6) ? 'pc-up' : 'pc-dn';
      h += '<tr><td class="l">' + esc(r.league) + '</td>' +
        '<td>' + dn.n + '</td><td>' + dn.b + '</td><td>' + dn.a + '</td>' +
        '<td>' + dn.p + '</td>' +
        '<td class="' + pcCls + '">' + pct(dn.hit_r) + '</td></tr>';
    });
    h += '</tbody></table>';
    body.innerHTML = h;
  }

  /* ---------- Tab 3：過往紀錄 ---------- */
  function renderHist(d) {
    var st = d.stats || {};
    var h = '<div class="sumbar">' +
      si('總入選', st.n || 0, 'gold') +
      si('命中(上盤贏)', st.w || 0, 'up') +
      si('錯', st.l || 0, '') +
      si('走盤', st.p || 0, '') +
      si('命中率', st.hit_r == null ? '—' : st.hit_r + '%', 'up') +
      '</div>';
    if (!(d.rows && d.rows.length)) {
      h += '<div class="empty">暫無入選紀錄（每次掃描自動記低入選場次，開賽後自動結算）</div>';
      body.innerHTML = h; return;
    }
    h += '<table class="t"><thead><tr><th>開賽時間</th><th>聯賽</th><th>對賽</th>' +
      '<th>盤口</th><th>模式</th><th>比分</th><th>結果</th></tr></thead><tbody>';
    d.rows.forEach(function (r) {
      var res = r.result
        ? '<span class="res ' + esc(r.result) + '">' +
          ({ W: '✔ 贏', L: '✘ 輸', P: '— 走' }[r.result] || r.result) + '</span>'
        : '<span class="hint">未結算</span>';
      var modeTxt = { direct: '直選', refine: '精煉' }[r.mode] || r.mode;
      h += '<tr><td>' + esc((r.kickoff || '').slice(5, 16)) + '</td>' +
        '<td class="l">' + esc(r.league) + '</td>' +
        '<td class="l">' + esc(r.home) + ' vs ' + esc(r.away) + '</td>' +
        '<td>' + esc(r.line || '') + '</td><td>' + modeTxt + '</td>' +
        '<td>' + esc(r.score || '—') + '</td><td>' + res + '</td></tr>';
    });
    h += '</tbody></table>';
    body.innerHTML = h;
  }

  /* ---------- 載入 ---------- */
  function refreshNote(d) {
    var r = d.refreshed;
    if (!r) return '';
    if (r.busy) return '｜⚠️ 另一個更新進行緊，今次用現有數據';
    if (r.disabled) return '｜⚠️ 雲端唔支援即時重抓';
    if (r.err) return '｜⚠️ 重抓失敗：' + String(r.err).slice(0, 60);
    return '｜✅ 已重抓 ' + (r.total || 0) + ' 場盤口（成 ' +
      (r.ok != null ? r.ok : '?') + '／敗 ' + (r.fail != null ? r.fail : '?') + '）' +
      (r.done ? '' : '，其餘繼續緊');
  }
  function loadFull(force) {
    status.textContent = force ? '重抓最新盤口及賠率中…（約 30-70 秒）' : '載入中…';
    var h = hoursSel.value;
    return jfetch('/api/fw8s/full?hours=' + h + (force ? '&force=1' : ''))
      .then(function (d) {
        fullCache = d;
        status.textContent = '更新於 ' + (d.updated || '') + refreshNote(d) +
          '｜伺服器每 10 分鐘自動快取';
        renderSum(d);
        if (curTab === 'up') renderUp(d);
      })
      .catch(function (e) {
        status.textContent = '載入失敗';
        body.innerHTML = '<div class="err">載入失敗：' + esc(e.message) +
          '（伺服器第一次掃描可能要 1-2 分鐘，請稍後再撳重新整理）</div>';
      });
  }
  function loadTab() {
    if (curTab === 'up') {
      if (fullCache) { renderSum(fullCache); renderUp(fullCache); }
      else loadFull(false);
    } else if (curTab === 'rules') {
      status.textContent = '載入公式…';
      jfetch('/api/fw8s/rules').then(function (d) {
        status.textContent = '更新於 ' + (d.updated || '');
        renderRules(d);
      }).catch(function (e) {
        body.innerHTML = '<div class="err">載入失敗：' + esc(e.message) + '</div>';
      });
    } else if (curTab === 'bt') {
      status.textContent = '載入全庫實跑…';
      jfetch('/api/fw8s/bt').then(function (d) {
        status.textContent = '更新於 ' + (d.updated || '');
        if (!d.ok) {
          body.innerHTML = '<div class="err">' + esc(d.error || '載入失敗') + '</div>';
          return;
        }
        renderBt(d);
      }).catch(function (e) {
        body.innerHTML = '<div class="err">載入失敗：' + esc(e.message) + '</div>';
      });
    } else if (curTab === 'hist') {
      status.textContent = '載入紀錄…';
      jfetch('/api/fw8s/history').then(renderHist).catch(function (e) {
        body.innerHTML = '<div class="err">載入失敗：' + esc(e.message) + '</div>';
      });
    }
  }
  $('#tabs').addEventListener('click', function (e) {
    var b = e.target.closest('.tab');
    if (!b) return;
    curTab = b.dataset.t;
    document.querySelectorAll('#tabs .tab').forEach(function (x) {
      x.classList.toggle('on', x === b);
    });
    loadTab();
  });
  hoursSel.addEventListener('change', function () { fullCache = null; loadFull(false); });
  $('#btnReload').addEventListener('click', function () {
    toast('重新掃描中…先重抓盤口賠率，再重新篩選（約 30-70 秒）');
    fullCache = null;
    loadFull(true).then(function () { toast('已更新'); });
  });
  loadFull(false);
})();
