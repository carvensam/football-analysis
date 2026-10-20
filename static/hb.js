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
  /* 2026-10-15：統一請求層——20 秒硬逾時＋502/503/504 同斷線自動重試。
   * 冇逾時時網絡凍結會 hang 到天地老（「撳極無反應」元兇）。 */
  function _hfetch(url, tries) {
    tries = tries == null ? 1 : tries;
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 20000);
    return fetch(url, { signal: ctl.signal }).then(function (r) {
      clearTimeout(timer);
      if (!r.ok) throw { status: r.status };
      return r.json();
    }).catch(function (e) {
      clearTimeout(timer);
      var st = e && e.status;
      if (tries > 0 && (st === 502 || st === 503 || st === 504 || st == null)) {
        return new Promise(function (res) { setTimeout(res, 2000); })
          .then(function () { return _hfetch(url, tries - 1); });
      }
      throw e;
    });
  }

  /* 10-17 用戶：好波大小「同聯賽/杯賽命中率」——hb_pick_log 10-17 起記錄，
   * n<10 顯示累積中；detail 用 slot，load 完自動填。 */
  var _lgRatesHb = null;
  function _hbLgRateTxt(lg) {
    var v = _lgRatesHb && _lgRatesHb[lg] && _lgRatesHb[lg].hb;
    if (v && v[1] >= 10) return Math.round(v[0] / v[1] * 100) + '%（n=' + v[1] + '）';
    return '<span style="color:var(--dim)">—（記錄緊，n=' + (v ? v[1] : 0) + '）</span>';
  }
  _hfetch('/api/lg_hit_rates', 1).then(function (d) {
    _lgRatesHb = (d && d.lg) || {};
    /* 2026-10-21：大/細方向細分：幾中幾＋% */
    var sb = (d && d.hb_side) || [];
    if (sb.length) {
      var bar = document.createElement('div');
      bar.style.cssText = 'margin:6px 0;display:flex;gap:6px;flex-wrap:wrap';
      bar.innerHTML = sb.map(function (m) {
        var dec = (m.w || 0) + (m.l || 0), r = dec ? m.w / dec : null;
        return '<span class="ln" style="border:1px solid var(--line);border-radius:12px;padding:2px 10px">' +
          (m.side === '大' ? '大球' : '細球') + ' <b>' + (m.w || 0) + '中' + dec + '</b>' +
          (r == null ? '' : ' ' + (r * 100).toFixed(1) + '%') +
          (m.p ? ' 走' + m.p : '') + '<small style="color:var(--dim)"> n=' + m.n + '</small></span>';
      }).join('');
      var list = document.getElementById('list');
      if (list && list.parentNode) list.parentNode.insertBefore(bar, list);
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-hblgr]'), function (el) {
      el.innerHTML = _hbLgRateTxt(el.getAttribute('data-hblgr'));
    });
  }).catch(function () {});

  function loadList() {
    var hours = $('#hoursSel').value;
    statusEl.textContent = '載入緊賽事…';
    listEl.innerHTML = '<div class="note">載入中…</div>';
    _hfetch('/api/hb/list?hours=' + hours, 2)
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
    return _predictFetch(row, mid, 0);
  }

  /* 2026-10-14：失敗自動重試一次（伺服器好波預測已改背景快取，理論上
   * 唔會再 502；萬一撞正冷計算／維護，重試一次後先放棄，唔使慘情咁「—」） */
  function _predictFetch(row, mid, tried) {
    /* 2026-10-15：經 _hfetch（20 秒硬逾時＋失敗自動重試一次）——
     * 凍網時唔再 hang；外層 tried 重試邏輯照舊。 */
    return _hfetch('/api/hb/predict?id=' + mid, 1)
      .then(function (d) {
        if (d && d.error) throw new Error(d.error);
        row._pred = d;
        fillLean(row, d);
        if (row.classList.contains('open')) fillBody(row, d);
        return d;
      })
      .catch(function () {
        if (tried < 1) {
          return new Promise(function (res) { setTimeout(res, 1500); })
            .then(function () { return _predictFetch(row, mid, tried + 1); });
        }
        var lean = row.querySelector('[data-lean]');
        if (lean) lean.textContent = '—';
        return null;
      });
  }

  function fillLean(row, d) {
    var lean = row.querySelector('[data-lean]');
    if (!lean) return;
    var ou = d.ou || {};
    if (ou.lg_rule) {
      /* 10-17 同聯賽規律（礦工v3 雙重驗證）——規律 verdict 最優先 */
      lean.textContent = '🎯規' + ou.lg_rule.side + ' ' + ou.lg_rule.rate + '%';
      lean.className = 'lean ' + (ou.lg_rule.side === '大' ? 'big' : 'small');
      lean.style.borderColor = 'var(--gold)';
      return;
    }
    if (ou.pick) {
      lean.textContent = '⭐精選' + ou.pick.side + ' ' + ou.pick.best + '%';
      lean.className = 'lean ' + (ou.pick.side === '大' ? 'big' : 'small');
      lean.style.borderColor = 'var(--gold)';
      return;
    }
    if (ou.lean) {
      var br = (ou.best_rates || {})[ou.lean];
      lean.textContent = (ou.lean === '大' ? '大球' : '細球') +
        (br != null ? ' ' + br + '%' : '') +
        (ou.n_voted != null ? '｜' + ou.n_voted + '項' : '');
      lean.className = 'lean ' + (ou.lean === '大' ? 'big' : 'small');
    } else if (ou.strongest && ou.strongest.rate >= 0.52 && ou.line != null) {
      /* 大字/細字擺最前面＋色碼（大=金、細=綠），一眼睇到方向，唔使撳入去
       * （2026-10-11 用戶：「61% 即係大定細？」——舊版「最強訊號細 61%」
       *   個方向字埋喺中間唔夠显眼）
       * 2026-10-11 修正（用戶：「表列無展示大小」）：共識門檻外，有最強單項訊號
       *   （n≥30 已係引擎保證）就照顯示方向，52–58% 加「（弱）」標記，
       *   只有 <52% 或完全冇訊號先顯示「觀望（共識不足）」。 */
      var side = ou.strongest.side;
      var strong = ou.strongest.rate >= 0.58;
      lean.innerHTML = (strong ? '<b>' + side + '</b> ' : side + ' ') +
        Math.round(ou.strongest.rate * 100) + '%' +
        '<small>（最強訊號 n=' + ou.strongest.n + (strong ? '' : '，弱') + '）</small>';
      lean.className = 'lean ' + (side === '大' ? 'big' : 'small');
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
      /* 2026-10-16：同列表徽章完全同一優先次序（精選＞共識＞最強單項≥52%＞觀望）。
       * 舊版淨係顯示共識——85% 場次共識不足時，列表話「細 64%」、詳情話
       * 「觀望」，用戶回報「列表同詳情唔一致」。最強單項做主打時照實標明
       * 來源同「共識不足」，等用戶知邊個為準。 */
      var leanTxt;
      if (ou.lg_rule) {
        /* 10-17 同聯賽規律（礦工v3 雙重驗證 82.5% 閘）——最優先 verdict */
        var _lr = ou.lg_rule;
        leanTxt = '<span style="color:var(--gold2)">🎯同聯賽規律 ' +
                  esc(String(_lr.side)) + ' ' + esc(String(_lr.rate)) + '%</span>' +
                  '<br><small style="color:var(--dim)">' +
                  esc(((_lr.conds || []).join('＋'))) +
                  '｜同聯賽 n=' + esc(String(_lr.n_lg != null ? _lr.n_lg : '?')) +
                  '・全庫 n=' + esc(String(_lr.n_all != null ? _lr.n_all : '?')) + '</small>';
      } else if (ou.pick) {
        leanTxt = '<span style="color:var(--gold2)">⭐精選' + ou.pick.side + '</span>' +
                  '（最高單項 ' + ou.pick.best + '%）' +
                  (ou.lean && ou.lean !== ou.pick.side
                    ? '<br><small style="color:var(--dim)">共識：' + ou.lean +
                      '｜同精選相反，以精選為準</small>' : '');
      } else if (ou.lean === '大' || ou.lean === '細') {
        leanTxt = '<span class="' + (ou.lean === '大' ? 'pct-over' : 'pct-under') +
                  '">' + (ou.lean === '大' ? '大球' : '細球') + '</span>';
        if (ou.strongest && ou.strongest.side !== ou.lean) {
          leanTxt += '<br><small style="color:var(--dim)">最強單項訊號：' +
            ou.strongest.side + ' ' + Math.round(ou.strongest.rate * 100) +
            '%（n=' + ou.strongest.n + '）｜同共識相反，以共識為準</small>';
        }
      } else if (ou.strongest && ou.strongest.rate >= 0.52 && ou.line != null) {
        var s0 = ou.strongest.side;
        var sstrong = ou.strongest.rate >= 0.58;
        leanTxt = '<span class="' + (s0 === '大' ? 'pct-over' : 'pct-under') +
                  '">' + (s0 === '大' ? '大球' : '細球') + ' ' +
                  Math.round(ou.strongest.rate * 100) + '%</span>' +
                  '<br><small style="color:var(--dim)">最強單項訊號 n=' +
                  ou.strongest.n + (sstrong ? '' : '，弱') +
                  '｜共識不足，以單項訊號顯示（同列表一致）</small>';
      } else {
        leanTxt = '<span style="color:var(--dim)">觀望（共識不足）</span>';
        if (ou.strongest) {
          leanTxt += '<br><small style="color:var(--dim)">最強單項訊號：' +
            ou.strongest.side + ' ' + Math.round(ou.strongest.rate * 100) +
            '%（n=' + ou.strongest.n + '）</small>';
        }
      }
      h += gi('預測', leanTxt +
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
        gi('同聯賽命中', '<span data-hblgr="' + esc(mt.league || '') + '">—</span>') +
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
      h += '<details style="margin:8px 0"><summary class="sec-h" style="cursor:pointer">🔬 51 項分析（' + votedN + ' 項表態｜' + cons + '）</summary>' +
        '<table class="t"><thead><tr><th>#</th><th>項目</th><th>n</th><th>大</th><th>細</th><th>走</th><th>表態</th></tr></thead><tbody>' +
        rows + '</tbody></table></details>';
    }

    /* 半全場 9 格：初盤／開賽前4小時／尾盤 ×（同聯賽/杯賽、全庫） */
    var ht = d.htft || {};
    if (ht.points && ht.points.length) {
      h += '<div class="sec-h">⚡ 半全場（9格）｜依據：初盤・開賽前4小時・尾盤</div>';
      ht.points.forEach(function (p) {
        if (p.line == null) return;
        h += '<div class="htft-pt">🕐 ' + p.name + '｜大小線 <b>' + p.line + '</b></div>';
        h += '<div class="htft-pair">';
        h += '<div><div class="htft-cap">同聯賽/杯賽' +
             (p.league.cells ? '（n=' + p.league.n + '）' : '（冇樣本）') + '</div>' +
             htftTable(p.league.cells, p.league_hl) + '</div>';
        h += '<div><div class="htft-cap">全庫' +
             (p.all.cells ? '（n=' + p.all.n + '）' : '（冇樣本）') + '</div>' +
             htftTable(p.all.cells, p.all_hl) + '</div>';
        h += '</div>';
      });
      h += '<div class="note">🟨 主和/主客/客和/客主 之中最高 &gt;6.5%｜🟩 第二高 &gt;6.5%｜' +
           '主主/和和/客客/和主/和客 唔標｜n=樣本場數，細樣本參考時留意</div>';
      if (ht.ht_avg_total != null) {
        h += '<div class="note">半場場均 ' + ht.ht_avg_total + ' 球</div>';
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

  function htftTable(cells, hl) {
    hl = hl || {};
    var h = '<table class="htft"><tr><th>半場＼全場</th><th>主勝</th><th>和</th><th>客勝</th></tr>';
    ['H', 'D', 'A'].forEach(function (htSide) {
      h += '<tr><th>' + (htSide === 'H' ? '主勝' : htSide === 'D' ? '和' : '客勝') + '</th>';
      ['H', 'D', 'A'].forEach(function (ftSide) {
        var k = htSide + ftSide;
        var v = cells ? cells[k] : null;
        if (v == null) { h += '<td>—</td>'; return; }
        var cls = hl.yellow === k ? 'yl' : (hl.green === k ? 'gn' : '');
        h += '<td class="' + cls + '">' + v + '%</td>';
      });
      h += '</tr>';
    });
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
