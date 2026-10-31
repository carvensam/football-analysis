(() => {
  const MONTH = '2026-10';
  const PAGE_TOOLS = {
    featw: ['W'], v1: ['V1', 'V1Z', 'V2', 'V2Z'],
    fw7: ['F7'], fw8: ['F8'], fw12: ['F12'], fw12lib: ['F12'],
    fw8s: ['F8S', 'S8'], x8: ['X8'], w88: ['W88', 'K3'],
    newjx: ['JX'], haobao: ['HB'], ou: ['OU'], htft: ['HTFT'], htftm: ['HTFT'],
  };
  const RESULT_TEXT = {W: '命中', L: '不命中', P: '走盤'};

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function host() {
    let node = document.getElementById('toolResults');
    if (!node) {
      node = el('section', 'tool-results');
      node.id = 'toolResults';
      node.setAttribute('aria-label', '2026年10月工具結果');
      const main = document.querySelector('#main') || document.querySelector('main');
      (main || document.body).appendChild(node);
    }
    return node;
  }

  function style() {
    if (document.getElementById('toolResultsStyle')) return;
    const sheet = el('style');
    sheet.id = 'toolResultsStyle';
    sheet.textContent = `
      .tool-results{margin:12px 0;clear:both}
      .tool-result{margin:8px 0;border:1px solid var(--line,#2b3648);border-radius:9px;background:var(--card,#1a2230)}
      .tool-result summary{cursor:pointer;padding:9px 10px;font-weight:700;line-height:1.5}
      .tr-stats{padding:4px 10px;color:var(--dim,#8b94a3);font-size:12px;line-height:1.6}
      .tr-table-wrap{overflow-x:auto;max-width:100%;padding:0 7px 7px}
      .tr-table{width:100%;border-collapse:collapse;font-size:13px;text-align:left}
      .tr-table th,.tr-table td{padding:6px;border-bottom:1px solid var(--line,#2b3648);vertical-align:top}
      .tr-table th{color:var(--gold2,#f5d76e);white-space:nowrap;background:var(--bg2,#161d27)}
      .tr-W{color:var(--up,#3fb96c);font-weight:700}
      .tr-L{color:var(--down,#e05c5c);font-weight:700}
      .tr-P,.tr-pending{color:var(--dim,#8b94a3)}
      .tr-more{margin:8px}
      .tr-error{padding:8px;color:var(--down,#e05c5c)}
      @media(max-width:600px){.tr-table{min-width:650px;font-size:12px}
        .tool-result summary{font-size:13px}.tr-stats{font-size:11px}}
    `;
    document.head.appendChild(sheet);
  }

  async function get(tool, offset, limit, month = MONTH) {
    const query = new URLSearchParams({tool, month, offset, limit});
    const response = await fetch('/api/tool_results?' + query, {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }

  function appendRow(body, row) {
    const tr = el('tr');
    const ko = String(row.kickoff || '');
    const values = [ko.slice(0, 10), ko.slice(11, 16), row.league,
                    row.home, row.away,
                    [row.direction, row.filter].filter(x => x !== null && x !== undefined && x !== '').join('｜') || '—',
                    RESULT_TEXT[row.result] || '未結算', row.score || '—'];
    values.forEach((value, index) => {
      const td = el('td', index === 5 ? 'tr-' + (row.result || 'pending') : '', value || '—');
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }

  function render(details, data, tool, allStats) {
    const stats = data.stats || {};
    const summary = `總篩選 ${stats.selected || 0}（${stats.match_count || 0}場）` +
      `｜命中 ${stats.hit || 0}｜不命中 ${stats.miss || 0}` +
      `｜走盤 ${stats.push || 0}｜未結算 ${stats.pending || 0}` +
      `｜命中率 ${stats.hit_rate == null ? '—' : stats.hit_rate + '%'}`;
    const all = allStats || {};
    const allSummary = `全庫總篩選 ${all.selected || 0}（${all.match_count || 0}場）` +
      `｜命中 ${all.hit || 0}｜不命中 ${all.miss || 0}` +
      `｜走盤 ${all.push || 0}｜未結算 ${all.pending || 0}` +
      `｜命中率 ${all.hit_rate == null ? '—' : all.hit_rate + '%'}`;
    details.querySelector('summary').textContent = `${data.name || tool}｜${allSummary}`;
    const body = details.querySelector('.tr-body');
    body.replaceChildren();
    body.appendChild(el('div', 'tr-stats', allSummary));
    body.appendChild(el('div', 'tr-stats', `${MONTH}｜${summary}`));
    if (data.missing_source) {
      body.appendChild(el('div', 'tr-stats', '未有記錄來源：' + data.missing_source));
    }
    if (stats.errors) {
      body.appendChild(el('div', 'tr-error', `有 ${stats.errors} 場計算失敗，未當完成。`));
    }
    const wrap = el('div', 'tr-table-wrap');
    const table = el('table', 'tr-table');
    const head = el('thead');
    const hr = el('tr');
    ['日期', '時間', '賽事', '主場', '客場', '方向／條件', '結果', '比分'].forEach(text => {
      hr.appendChild(el('th', '', text));
    });
    head.appendChild(hr);
    table.appendChild(head);
    const rows = el('tbody');
    (data.rows || []).forEach(row => appendRow(rows, row));
    table.appendChild(rows);
    wrap.appendChild(table);
    body.appendChild(wrap);
    if (!(data.rows || []).length && !data.missing_source) {
      body.appendChild(el('div', 'tr-stats', '呢個月暫無已記錄選擇。'));
    }
    if (data.page && data.page.has_more) {
      const more = el('button', 'btn tr-more', '顯示更多');
      more.type = 'button';
      more.onclick = async () => {
        more.disabled = true;
        more.textContent = '載入中…';
        try {
          const next = await get(tool, data.page.next_offset, data.page.limit);
          next.rows.forEach(row => appendRow(rows, row));
          data.page = next.page;
          more.textContent = '顯示更多';
          more.disabled = false;
          if (!next.page.has_more) more.remove();
        } catch (error) {
          more.textContent = '載入失敗，請重試';
          more.disabled = false;
        }
      };
      body.appendChild(more);
    }
  }

  async function load(page) {
    const tools = PAGE_TOOLS[page] || [];
    const root = host();
    root.hidden = !tools.length;
    if (!tools.length) return;
    style();
    root.replaceChildren();
    await Promise.all(tools.map(async tool => {
      const details = el('details', 'tool-result');
      details.open = true;
      details.dataset.tool = tool;
      details.appendChild(el('summary', '', `${tool}｜${MONTH}｜載入中…`));
      details.appendChild(el('div', 'tr-body'));
      root.appendChild(details);
      try {
        const [data, allData] = await Promise.all([
          get(tool, 0, 50), get(tool, 0, 1, 'all')
        ]);
        render(details, data, tool, allData.stats);
      } catch (error) {
        details.querySelector('summary').textContent = `${tool}｜${MONTH}｜載入失敗`;
        details.querySelector('.tr-body').textContent = String(error.message || error);
      }
    }));
  }

  window.ToolResults = {load};
  document.addEventListener('DOMContentLoaded', () => {
    if (window.self !== window.top) return;
    const main = document.querySelector('main');
    if (main && main.dataset.tools) {
      PAGE_TOOLS.__standalone = main.dataset.tools.split(',').map(tool => tool.trim());
      load('__standalone');
    }
  });
})();
