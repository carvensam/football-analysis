/* 「停止／繼續爬」浮動掣（2026-10-12）：掛喺每個版面右下角。
   狀態經 /api/crawl/state 每 8 秒刷新；
   cmd=stop → 顯示「▶ 繼續爬」（綠），否則「■ 停止」（紅）。
   撳停止 → 引擎完成手頭任務後退出（唔再爬）；撳繼續 → 即重開 6 線。 */
(function () {
  var css = document.createElement('style');
  css.textContent =
    '#crawlStopBtn{position:fixed;right:12px;bottom:66px;z-index:70;' +
    'padding:12px 22px;border-radius:14px;border:2px solid #0009;' +
    'font-size:17px;font-weight:900;cursor:pointer;letter-spacing:.12em;' +
    'box-shadow:0 4px 12px #000c;font-family:inherit}' +
    '#crawlStopBtn.halt{background:#c0392b;color:#fff}' +
    '#crawlStopBtn.go{background:#1e9e52;color:#fff}' +
    '#crawlStopBtn:disabled{opacity:.6;cursor:wait}';
  document.head.appendChild(css);

  var btn = document.createElement('button');
  btn.id = 'crawlStopBtn';
  btn.disabled = true;
  btn.textContent = '爬取…';

  function render(d) {
    btn.disabled = false;
    var via = d && d.via ? '（' + d.via + '）' : '';
    if (d && d.cmd === 'stop') {
      btn.textContent = '▶ 繼續爬';
      btn.className = 'go';
      btn.title = '爬取已停止（' + (d.workers_alive || 0) + ' 線待退出）' + via;
    } else {
      btn.textContent = '■ 停止';
      btn.className = 'halt';
      btn.title = '爬取中（' + (d ? d.workers_alive : '?') + '/' +
        (d ? d.workers_total : '?') + ' 線）——撳即停' + via;
    }
  }

  function poll() {
    /* 2026-10-15：12 秒硬逾時——凍網／雲端死透時唔會一直「爬取…」卡住個掣 */
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 12000);
    fetch('/api/crawl/state', { signal: ctl.signal })
      .then(function (r) { return r.json(); })
      .then(render).catch(function () {
        btn.textContent = '■ 停止'; btn.className = 'halt'; btn.disabled = false;
      })
      .finally(function () { clearTimeout(timer); });
  }

  btn.onclick = function () {
    var stopping = btn.className !== 'go';
    btn.disabled = true;
    btn.textContent = stopping ? '停止中…' : '啟動中…';
    /* 手機直爬一併停：server 爬蟲停唔到手機網絡嗰條線（2026-10-12 用戶：
     * 「停止掣無用」——其實淨係停咗伺服器嗰邊，手機仲繼續逐場試紧） */
    if (stopping && window.PhoneCrawl && window.PhoneCrawl.running) {
      window.PhoneCrawl.stop();
    }
    fetch(stopping ? '/api/crawl/stop' : '/api/crawl/run',
          { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        /* 中繼模式：真正爬蟲喺本機，等佢攞指令＋回報狀態（約 1 分鐘） */
        if (d && d.relay) {
          btn.textContent = '已通知本機…';
          setTimeout(poll, 8000);
          setTimeout(poll, 20000);
        } else {
          setTimeout(poll, 1200);
        }
      })
      .catch(function () { btn.textContent = '錯誤，重試'; btn.disabled = false; });
  };

  function mount() {
    if (document.getElementById('crawlStopBtn')) return;
    document.body.appendChild(btn);
    poll();
    setInterval(poll, 8000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
