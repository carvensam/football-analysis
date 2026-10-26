/* 2026-10-25 分聯賽/杯賽實測報表（獨立工具頁共用）。
 * 用法（每頁一行）：<script src="league7.js" data-tool="88w"></script>
 * 放喺頁面內容頂部位置——script 會喺自己位置前插入報表卡。
 * 後端 /api/league_report 即時 SQL：賽事一結算，下次載入即反映。 */
(function(){
  var me = document.currentScript;
  var tool = (me && me.dataset) ? me.dataset.tool : '';
  var UP = 'var(--up, #3fb950)', DOWN = 'var(--down, #f85149)', DIM = 'var(--dim, #8b94a3)';
  var box = document.createElement('div');
  box.style.cssText = 'margin:10px auto;max-width:1000px;padding:0 10px';
  if (me && me.parentNode) me.parentNode.insertBefore(box, me);
  else return;
  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"]/g,
      function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; });
  }
  fetch('/api/league_report?tool=' + encodeURIComponent(tool))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d.rows || !d.rows.length){
        box.innerHTML = '<div style="color:' + DIM + ';font-size:13px">🏆 分聯賽/杯賽實測：暫無 2026 已結算紀錄。</div>';
        return;
      }
      /* 2026-10-26 手機走哂位根因：5 欄 nowrap flex 闊過熒幕時右欄被裁、聯賽名被
       * ellipsis 食晒（文字缺失）。改 display:table（欄位對齊、全字可見）＋外層橫掃。 */
      var CELL = 'display:table-cell;white-space:nowrap;padding:4px 12px 4px 0;font-size:13px';
      var h = '<div style="background:var(--card, #1a2230);border:1px solid var(--line, #2b3648);border-radius:10px;padding:10px 12px">' +
        '<b>🏆 分聯賽/杯賽實測</b> <span style="color:' + DIM + ';font-size:12px">2026 總成績｜最近七日（結算即更新；近7日勝率排先；雙低於60%<span style="background:rgba(139,0,0,.16)">淺暗紅</span>；表格可橫掃）</span>' +
        '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><div style="display:table;border-collapse:collapse">' +
        '<div style="display:table-row">' +
        '<span style="' + CELL + ';font-size:11px;color:' + DIM + '"></span>' +
        '<span style="' + CELL + ';font-size:11px;color:' + DIM + '">2026 總成績</span>' +
        '<span style="' + CELL + ';font-size:11px;color:' + DIM + ';padding:4px 0 4px 12px;text-align:right">勝率</span>' +
        '<span style="' + CELL + ';font-size:11px;color:' + DIM + '">最近七日</span>' +
        '<span style="' + CELL + ';font-size:11px;color:' + DIM + ';padding:4px 0 4px 12px;text-align:right">勝率</span></div>';
      for (var i = 0; i < d.rows.length; i++){
        var r = d.rows[i];
        var hit = r.hit != null ? r.hit.toFixed(1) + '%' : '—';
        var hit7 = r.hit7 != null ? r.hit7.toFixed(1) + '%' : '—';
        h += '<div style="display:table-row;' + (r.warn ? 'background:rgba(139,0,0,.16);' : '') + '">' +
          '<span style="' + CELL + '">' + esc(r.league) + '</span>' +
          '<span style="' + CELL + ';color:' + DIM + '">' + r.n + '場｜<b style="color:' + UP + '">贏' + r.w + '</b>｜<b style="color:' + DOWN + '">輸' + r.l + '</b>' + (r.p ? '｜<span style="color:' + DIM + '">走' + r.p + '</span>' : '') + '</span>' +
          '<b style="' + CELL + ';padding:4px 0 4px 12px;text-align:right;color:' + (r.hit != null && r.hit >= 50 ? UP : DOWN) + '">' + hit + '</b>' +
          '<span style="' + CELL + ';color:' + DIM + '">' + (r.n7 != null ? r.n7 + '場｜<b style="color:' + UP + '">贏' + (r.w7 || 0) + '</b>｜<b style="color:' + DOWN + '">輸' + (r.l7 || 0) + '</b>' + (r.p7 ? '｜走' + r.p7 : '') : '—') + '</span>' +
          '<b style="' + CELL + ';padding:4px 0 4px 12px;text-align:right;color:' + (r.hit7 != null ? (r.hit7 >= 50 ? UP : DOWN) : DIM) + '">' + hit7 + '</b></div>';
      }
      box.innerHTML = h + '</div></div></div>';
    })
    .catch(function(){
      box.innerHTML = '<div style="color:' + DIM + ';font-size:13px">🏆 分聯賽/杯賽實測：載入失敗。</div>';
    });
})();
