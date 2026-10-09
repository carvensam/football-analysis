/* 好波 hb.js — 大小球＋半全場預測 UI（2026-10-05） */
(function () {
  'use strict';
  var $ = function (s) { return document.querySelector(s); };
  var listEl = $('#list'), statusEl = $('#status');

  function toast(msg) {
    var t = $('#toast');
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.style.display = 'none'; }, 2200);
  }

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pct(v, cls) {
    if (v == null) return '—';
    return '<span class="big-pct ' + (cls || '') + '">' + v + '%</span>';
  }

  /* ---------- 賽事清單 ---------- */
  function loadList() {
    var hours = $('#hoursSel').value;
    statusEl.textContent = '載入緊賽事…';
    listEl.innerHTML = '<div class="note">載入中…</div>';
    fetch('/api/hb/list?hours=' + hours)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.matches || !d.matches.length) {
          listEl.innerHTML = '<div class="note">呢個時段冇即將開賽嘅賽事。</div>';
          statusEl.textContent = '完成';
          return;
        }
        statusEl.textContent = '共 ' + d.matches.length + ' 場｜' + (d.now || '');
        renderList(d.matches);
      })
      .catch(function (e) {
        listEl.innerHTML = '<div class="err">載入失敗：' + esc(e) + '</div>';
        statusEl.textContent = '載入失敗';
      });
  }

  function renderList(matches) {
    listEl.innerHTML = '';
    matches.forEach(function (m) {
      var row = document.createElement('div');
      row.className = 'mrow';
      row.dataset.mid = m.id;
      var ouTxt = m.ou_line != null
        ? '大小 <b>' + m.ou_line + '</b>（大' + (m.ou_over != null ? m.ou_over : '—') +
          '/細' + (m.ou_under != null ? m.ou_under : '—') + '）'
        : '大小 <b>未開盤</b>';
      if (m.init_line != null && m.ou_line != null && m.init_line !== m.ou_line) {
        ouTxt += '｜初盤 ' + m.init_line;
      }
      var ahTxt = m.ah_line ? '｜亞盤 ' + esc(m.ah_line) : '';
      row.innerHTML =
        '<div class="m-top">' +
          '<span class="ko">' + esc(m.kickoff.slice(5, 16)) + '</span>' +
          '<span class="lg">' + esc(m.league) + '</span>' +
          '<span class="tm">' + esc(m.home) + ' <span style="color:var(--dim)">vs</span> ' + esc(m.away) + '</span>' +
          '<span class="lean" data-lean>分析中…</span>' +
          '<span class="ln">' + ouTxt + ahTxt + '</span>' +
        '</div>' +
        '<div class="body" data-body><div class="spin">分析緊…</div></div>';
      row.addEventListener('click', function () { toggle(row, m); });
      listEl.appendChild(row);
    });
    // 背景順序預測（4 線並行），填返 lean 徽章
    queuePredicts(matches, 0);
  }

  /* 簡易並行隊列：全部場次自動預測，摺疊打開時即刻有內容 */
  var PRED_CONC = 4;
  function queuePredicts(matches, i) {
    if (i >= matches.length) return;
    var batch = matches.slice(i, i + PRED_CONC);
    batch.forEach(function (m) { predictFor(m.id, false); });
    setTimeout(function () { queuePredicts(matches, i + PRED_CONC); }, 60);
  }

  function predictFor(mid, force) {
    var row = listEl.querySelector('.mrow[data-mid="' + mid + '"]');
    if (!row) return Promise.resolve(null);
    if (row._pred && !force) return Promise.resolve(row._pred);
    return fetch('/api/hb/predict?id=' + mid)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) throw new Error(d.error);
        row._pred = d;
        fillLean(row, d);
        if (row.classList.contains('open')) fillBody(row, d);
        return d;
      })
      .catch(function () {
        var lean = row.querySelector('[data-lean]');
        if (lean) lean.textContent = '—';
        return null;
      });
  }

  function fillLean(row, d) {
    var lean = row.querySelector('[data-lean]');
    if (!lean) return;
    var ou = d.ou || {};
    if (ou.pick) {
      lean.textContent = '⭐精選' + ou.pick.side + ' ' + ou.pick.best + '%';
      lean.className = 'lean big';
      lean.style.borderColor = 'var(--gold)';
      lean.style.color = 'var(--gold2)';
      return;
    }
    if (ou.lean) {
      var br = (ou.best_rates || {})[ou.lean];
      lean.textContent = (ou.lean === '大' ? '大球' : '細球') +
        (br != null ? ' ' + br + '%' : '') +
        (ou.n_voted != null ? '｜' + ou.n_voted + '項' : '');
      lean.className = 'lean ' + (ou.lean === '大' ? 'big' : 'small');
    } else if (ou.strongest && ou.strongest.rate >= 0.58 && ou.line != null) {
      lean.textContent = '最強訊號' + ou.strongest.side + ' ' +
        Math.round(ou.strongest.rate * 100) + '%（n=' + ou.strongest.n + '）';
      lean.className = 'lean';
      lean.style.color = 'var(--gold2)';
    } else if (ou.line != null) {
      lean.textContent = '觀望（共識不足）';
      lean.className = 'lean';
    } else {
      lean.textContent = '未開盤';
      lean.className = 'lean';
    }
  }

  function toggle(row, m) {
    var willOpen = !row.classList.contains('open');
    row.classList.toggle('open', willOpen);
    if (willOpen) {
      var body = row.querySelector('[data-body]');
      if (row._pred) {
        fillBody(row, row._pred);
      } else {
        body.innerHTML = '<div class="spin">分析緊…</div>';
        predictFor(m.id, true);
      }
    }
  }

  /* ---------- 詳情內容 ---------- */
  function fillBody(row, d) {
    var body = row.querySelector('[data-body]');
    body.innerHTML = buildDetail(d);
  }

  function buildDetail(d) {
    var h = '';
    var ou = d.ou || {};
    var mt = d.match || {};

    /* 大小球 */
    h += '<div class="sec-h">⚽ 大小球</div><div class="grid">';
    if (ou.line != null) {
      h += gi('尾盤線', ou.line + ' 球');
      h += gi('水位', '大 ' + (ou.over_odds != null ? ou.over_odds : '—') +
                  '｜細 ' + (ou.under_odds != null ? ou.under_odds : '—'));
      h += gi('預測', (ou.lean === '大' ? '<span class="pct-over">大球</span>' : '<span class="pct-under">細球</span>') +
                  (ou.confidence ? '（信心' + ou.confidence + '）' : ''));
      h += gi('場均入球 λ', ou.lambda + '<br><small>主 ' + ou.lambda_home + '｜客 ' + ou.lambda_away + '</small>');
      h += gi('預期比分', ou.expected_score ? ou.expected_score[0] + ' - ' + ou.expected_score[1] : '—');
      if (ou.top_scores && ou.top_scores.length) {
        var sc = ou.top_scores.slice(0, 3).map(function (s) {
          return s[0] + '（' + s[1] + '%）';
        }).join('｜');
        h += gi('最可能波膽', sc);
      }
    } else {
      h += gi('尾盤線', '未開大小球盤');
      h += gi('場均入球 λ', ou.lambda != null ? ou.lambda : '—');
    }
    h += '</div>';

    if (ou.line != null && ou.over_r != null) {
      h += '<div class="grid">' +
        gi('大球', pct(ou.over_r, 'pct-over')) +
        gi('細球', pct(ou.under_r, 'pct-under')) +
        gi('走盤', pct(ou.push_r)) +
        gi('聯賽實證大球', (ou.empirical_over != null ? ou.empirical_over + '%' : '—') +
           '<br><small>n=' + (ou.empirical_n || 0) + '</small>') +
      '</div>';
    }
    if (ou.layers) {
      h += '<div class="grid">' +
        gi('λ 分層・球隊', ou.layers.team) +
        gi('λ 分層・聯賽階段', ou.layers.league_stage) +
        gi('λ 分層・莊家', ou.layers.bookmaker != null ? ou.layers.bookmaker : '—') +
      '</div>';
    }
    if (ou.line_move != null) {
      var mv = ou.line_move;
      var mvTxt = mv > 0 ? '升盤（+' + mv + '）' : mv < 0 ? '降盤（' + mv + '）' : '不變';
      if (ou.over_odds_move != null) {
        mvTxt += '｜大球水位 ' + (ou.over_odds_move > 0 ? '+' : '') + ou.over_odds_move;
      }
      h += '<div class="note">初盤 ' + (ou.init_line != null ? ou.init_line + ' 球' : '—') +
           ' → 尾盤 ' + ou.line + ' 球：' + mvTxt + '</div>';
    }

    /* 49 項分析（2026-10-06 重做：FootballAnalysis 概念做大細） */
    if (ou.items && ou.items.length) {
      var votedN = ou.n_voted || 0;
      var cons = (ou.lean ? ('共識：<b class="' + (ou.lean === '大' ? 'pct-over' : 'pct-under') + '">' + ou.lean + '</b>｜票數 大' + (ou.votes && ou.votes['大'] || 0) + '：細' + (ou.votes && ou.votes['細'] || 0)) : '共識：觀望（唔夠強唔硬估）');
      if (ou.pick) cons += '｜<b style="color:var(--gold2)">⭐精選' + ou.pick.side + '（最高單項 ' + ou.pick.best + '%）</b>';
      var rows = ou.items.map(function (it) {
        var r = it.rates;
        var cls = it.side === '大' ? 'pct-over' : (it.side === '細' ? 'pct-under' : '');
        return '<tr><td>' + it.no + '</td><td style="text-align:left">' + esc(it.title) + '</td>' +
          '<td>' + (r ? r.n : '—') + '</td>' +
          '<td>' + (r ? (r.over_r * 100).toFixed(1) + '%' : '—') + '</td>' +
          '<td>' + (r ? (r.under_r * 100).toFixed(1) + '%' : '—') + '</td>' +
          '<td>' + (r ? (r.push_r * 100).toFixed(1) + '%' : '—') + '</td>' +
          '<td class="' + cls + '">' + (it.side || '') + '</td></tr>';
      }).join('');
      h += '<details style="margin:8px 0"><summary class="sec-h" style="cursor:pointer">🔬 49 項分析（' + votedN + ' 項表態｜' + cons + '）</summary>' +
        '<table class="t"><thead><tr><th>#</th><th>項目</th><th>n</th><th>大</th><th>細</th><th>走</th><th>表態</th></tr></thead><tbody>' +
        rows + '</tbody></table></details>';
    }

    /* 半全場 9 格 */
    var ht = d.htft || {};
    if (ht.cells) {
      h += '<div class="sec-h">⚡ 半全場（9格）</div>';
      h += htftTable(ht.cells, ht.top);
      if (ht.top) {
        h += '<div class="note">最可能：' + cellName(ht.top.cell) + ' ' + ht.top.pct + '%' +
             (ht.ht_avg_total != null ? '｜半場場均 ' + ht.ht_avg_total + ' 球' : '') + '</div>';
      }
    }

    /* 上下盤 × 大小球 */
    var rel = d.relation || {};
    if (rel.league || rel.all) {
      h += '<div class="sec-h">🔀 上下盤 × 大小球關係（贏邊 → 大/細 傾向）</div>';
      if (rel.league) h += relTable(rel.league, '同聯賽');
      if (rel.all) h += relTable(rel.all, '全資料庫');
    }

    /* 因子明細 */
    var f = d.factors || {};
    if (f.league) {
      h += '<div class="sec-h">🧮 因子明細</div><div class="grid">' +
        gi('聯賽・場均', f.league.avg_total + ' 球<br><small>主 ' + f.league.avg_home +
           '｜客 ' + f.league.avg_away + '</small><br><small>n=' + f.league.n + '</small>') +
        gi('階段因子', (f.stage && f.stage.factor != null ? '×' + f.stage.factor : '—') +
           (f.stage && f.stage.n ? '<br><small>' + esc(f.stage.round || '') + ' n=' + f.stage.n + '</small>' : ''));
      var ht2 = f.home_team || {}, at2 = f.away_team || {};
      h += gi('主隊（主場）', teamFactorTxt(ht2, 'h'));
      h += gi('客隊（客場）', teamFactorTxt(at2, 'a'));
      h += '</div>';
    }
    if (mt.ah_line) h += '<div class="note">亞盤：' + esc(mt.ah_line) + '</div>';
    return h;
  }

  function gi(t, v) {
    return '<div class="gi"><div class="t">' + t + '</div><div class="v">' + v + '</div></div>';
  }

  function teamFactorTxt(T, side) {
    if (!T || !T.n_h && !T.n_a) return '冇數據';
    var n = side === 'h' ? (T.n_h || 0) : (T.n_a || 0);
    var gf = side === 'h' ? T.gf_h : T.gf_a;
    var ga = side === 'h' ? T.ga_h : T.ga_a;
    return '入 ' + (gf != null ? gf : '—') + '｜失 ' + (ga != null ? ga : '—') +
           '<br><small>n=' + n + '</small>';
  }

  function cellName(c) {
    var m = { H: '主', A: '客', D: '和' };
    return '半場' + m[c.charAt(0)] + ' → 全場' + m[c.charAt(1)];
  }

  function htftTable(cells, top) {
    var order = ['HH', 'HD', 'HA', 'DH', 'DD', 'DA', 'AH', 'AD', 'AA'];
    var h = '<table class="htft"><tr><th>半場＼全場</th><th>主勝</th><th>和</th><th>客勝</th></tr>';
    ['H', 'D', 'A'].forEach(function (htSide) {
      h += '<tr><th>' + (htSide === 'H' ? '主勝' : htSide === 'D' ? '和' : '客勝') + '</th>';
      ['H', 'D', 'A'].forEach(function (ftSide) {
        var k = htSide + ftSide;
        var v = cells[k];
        if (v == null) { h += '<td>—</td>'; return; }
        var isTop = top && top.cell === k;
        h += '<td class="' + (isTop ? 'top' : '') + '">' + v + '%</td>';
      });
      h += '</tr>';
    });
    void order;
    h += '</table>';
    return h;
  }

  function relTable(r, title) {
    var h = '<table class="rel"><tr><th colspan="3">' + esc(title) + '（n=' + r.n + '）</th></tr>';
    h += '<tr><th></th><th>→ 大球</th><th>→ 細球</th></tr>';
    h += '<tr><th>上盤贏</th><td>' + (r.up_over_r != null ? pct(r.up_over_r, 'pct-over') + '<br><small>' + r.up_over + '場</small>' : '—') + '</td>' +
         '<td>' + (r.up_under_r != null ? pct(r.up_under_r, 'pct-under') + '<br><small>' + r.up_under + '場</small>' : '—') + '</td></tr>';
    h += '<tr><th>下盤贏</th><td>' + (r.down_over_r != null ? pct(r.down_over_r, 'pct-over') + '<br><small>' + r.down_over + '場</small>' : '—') + '</td>' +
         '<td>' + (r.down_under_r != null ? pct(r.down_under_r, 'pct-under') + '<br><small>' + r.down_under + '場</small>' : '—') + '</td></tr>';
    h += '</table>';
    return h;
  }

  /* ---------- 事件 ---------- */
  $('#btnReload').addEventListener('click', function () {
    toast('重新整理緊…');
    loadList();
  });
  $('#hoursSel').addEventListener('change', loadList);

  loadList();
})();
