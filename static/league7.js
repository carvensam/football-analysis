(function(){
  var me = document.currentScript || document.querySelector('script[data-tool]');
  var tool = (me && me.dataset) ? me.dataset.tool : '';
  var box = document.createElement('section');
  box.setAttribute('aria-label', '分聯賽及杯賽實測');
  box.style.cssText = 'margin:10px auto;max-width:1000px;padding:0 10px';
  var main = document.getElementById('main');
  if (main && main.parentNode) main.parentNode.insertBefore(box, main);
  else if (me && me.parentNode) me.parentNode.insertBefore(box, me);
  else document.body.insertBefore(box, document.body.firstChild);
  var loading = false;
  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"]/g,
      function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; });
  }
  function message(text, error){
    box.innerHTML = '<div style="padding:12px;color:' + (error ? '#ff9d9d' : 'var(--dim,#8b94a3)') + '">' + esc(text) + '</div>';
  }
  function render(rows){
    var cell = 'white-space:nowrap;padding:5px 10px;border-bottom:1px solid var(--line,#2b3648);font-size:13px';
    var h = '<div style="background:var(--card,#1a2230);border:1px solid var(--line,#2b3648);border-radius:10px;padding:10px 12px">' +
      '<b>分聯賽／杯賽實測</b> <span style="color:var(--dim,#8b94a3);font-size:12px">2026 總成績｜最近七日（按賽事日期；每60秒更新；表格可橫掃）</span>' +
      '<div style="overflow-x:auto;max-width:100%;-webkit-overflow-scrolling:touch">' +
      '<table class="league7-table" style="border-collapse:collapse;width:100%;min-width:680px;max-width:none"><thead><tr>' +
      ['聯賽／杯賽', '2026 總成績', '勝率', '最近七日', '勝率'].map(function(x, i){
        return '<th style="' + cell + ';text-align:' + (i === 2 || i === 4 ? 'right' : 'left') + ';color:var(--gold2,#f5d76e)">' + x + '</th>';
      }).join('') + '</tr></thead><tbody>';
    rows.forEach(function(r){
      function hit(v){ return v == null ? '—' : Number(v).toFixed(1) + '%'; }
      function color(v){ return v == null ? 'var(--dim,#8b94a3)' : (v >= 50 ? '#3fb950' : '#f85149'); }
      h += '<tr' + (r.warn ? ' style="background:rgba(139,0,0,.16)"' : '') + '>' +
        '<td style="' + cell + '">' + esc(r.league) + '</td>' +
        '<td style="' + cell + '">' + r.n + '場｜贏' + r.w + '｜輸' + r.l + (r.p ? '｜走' + r.p : '') + '</td>' +
        '<td style="' + cell + ';text-align:right;color:' + color(r.hit) + '"><b>' + hit(r.hit) + '</b></td>' +
        '<td style="' + cell + '">' + (r.n7 == null ? '—' : r.n7 + '場｜贏' + (r.w7 || 0) + '｜輸' + (r.l7 || 0) + (r.p7 ? '｜走' + r.p7 : '')) + '</td>' +
        '<td style="' + cell + ';text-align:right;color:' + color(r.hit7) + '"><b>' + hit(r.hit7) + '</b></td></tr>';
    });
    box.innerHTML = h + '</tbody></table></div></div>';
  }
  function loadTable(){
    if (loading) return;
    loading = true;
    var ctl = new AbortController();
    var timeout = setTimeout(function(){ ctl.abort(); }, 12000);
    fetch('/api/league_report?tool=' + encodeURIComponent(tool), {signal: ctl.signal, cache: 'no-store'})
      .then(function(r){
        return r.json().then(function(d){
          if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
          return d;
        });
      })
      .then(function(d){
        if (d.rows && d.rows.length) render(d.rows);
        else message('分聯賽／杯賽實測：暫無 2026 已結算紀錄。');
        if (d.errors) box.insertAdjacentHTML('beforeend',
          '<div style="color:#ff9d9d;padding:8px">有 ' + Number(d.errors) + ' 場計算失敗，呢個報表未完整。</div>');
      })
      .catch(function(e){
        message('分聯賽／杯賽實測：載入失敗，會自動重試（' + e.message + '）。', true);
      })
      .finally(function(){ clearTimeout(timeout); loading = false; });
  }
  message('分聯賽／杯賽實測：載入中…');
  loadTable();
  setInterval(function(){ if (!document.hidden) loadTable(); }, 60000);
  setInterval(function(){ if (!document.hidden && !box.querySelector('table')) loadTable(); }, 20000);
  document.addEventListener('visibilitychange', function(){ if (!document.hidden) loadTable(); });
})();
