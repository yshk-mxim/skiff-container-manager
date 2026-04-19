// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * System page — per-page module loaded by index.html.
 *
 * Contains Account section (token rotation + config reset) and the
 * System page itself (docker info, metrics/df links, prune actions,
 * audit log viewer).
 *
 * Uses globals from app.js + ui.js: API, apiFetch, toast, makeBtn,
 * makeActionBtn, guardedAction, esc, UI.
 */
"use strict";

// ── Account (token rotation + config reset) ──
// Renders under the System page. Queries /api/setup-state to know whether the
// server is env-configured (from_env=true → buttons hidden, since both endpoints
// 403 on env-managed configs and a visible button that always errors is worse
// UX than no button). Both buttons require a fresh API call with the existing
// token; on reset-config the server clears state and the next page load is
// redirected to the setup wizard.
async function _renderAccountSection(main) {
  var state;
  try { state = await fetch(API + '/setup-state').then(function(r) { return r.json(); }); }
  catch (e) { return; }  // server unreachable — don't render anything
  if (state && state.from_env) return;  // env-managed; nothing the user can change here

  var h3 = document.createElement('h3');
  h3.textContent = 'Account';
  h3.style.cssText = 'margin-top:28px;margin-bottom:4px;font-size:18px;';
  main.appendChild(h3);
  var desc = document.createElement('p');
  desc.style.cssText = 'font-size:12px;color:var(--muted);margin-bottom:12px;';
  desc.textContent = 'Rotate the API token without restarting, or reset config to re-run the setup wizard.';
  main.appendChild(desc);

  var row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';

  // Rotate token — opens a modal with a generated token + copy + save
  row.appendChild(makeBtn('Rotate API token', function() { _showRotateTokenModal(); }, 'btn'));

  // Reset config — destructive; double-confirm then redirect
  row.appendChild(makeActionBtn('Reset configuration', function() {
    if (!confirm('Reset configuration?\n\nThis clears the API token, Docker host, and registries from server memory. Every logged-in user will be signed out. The next visitor runs the setup wizard.\n\nThis cannot be undone without a server restart.')) {
      throw new Error('Cancelled');
    }
    return apiFetch(API + '/auth/reset-config', { method: 'POST' }).then(function() {
      toast('Configuration reset — redirecting to setup', 'success');
      sessionStorage.clear();
      setTimeout(function() { location.reload(); }, 800);
    });
  }, 'btn danger', 'Resetting\u2026'));

  main.appendChild(row);
}

// Rotate-token modal: generate-or-paste new value, confirm, swap server state,
// then update sessionStorage so the user doesn't get 401'd by their own request.
// Intentionally does NOT use UI.formModal because the submit button starts
// disabled and unlocks only after the user copies the new token — that
// inter-field dependency is beyond the formModal contract.
function _showRotateTokenModal() {
  var m;
  var inp = UI.el('input', {
    type: 'password', readonly: true, id: 'rotate-token',
    style: 'width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:4px;'
         + 'font-size:13px;font-family:monospace;background:var(--card);color:var(--text);box-sizing:border-box',
  });
  var warnCopied = UI.el('p', {
    style: 'color:#fbbf24;font-size:11px;margin-top:6px;display:none',
    text: '\u26a0 Copy the new token before saving — it will not be shown again.',
  });

  var sessionBtn = makeActionBtn('Save rotation', function() {
    var tok = inp.value.trim();
    if (!tok || tok.length < 16) { toast('Token must be at least 16 chars', 'error'); throw new Error('short'); }
    return apiFetch(API + '/auth/rotate-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_token: tok }),
    }).then(function() {
      sessionStorage.setItem('api_token', tok);
      m.close();
      toast('Token rotated — old token is now invalid.', 'success');
    });
  }, 'btn primary', 'Saving\u2026');
  sessionBtn.disabled = true;
  sessionBtn.style.opacity = '0.6';
  sessionBtn.title = 'Copy the new token first to unlock';

  var genBtn = makeBtn('Generate', function() {
    var bytes = crypto.getRandomValues(new Uint8Array(24));
    inp.value = Array.from(bytes).map(function(b) { return b.toString(16).padStart(2, '0'); }).join('');
    warnCopied.style.display = 'block';
    sessionBtn.disabled = true; sessionBtn.style.opacity = '0.6';
  }, 'btn small');
  var copyBtn = makeBtn('Copy', function() {
    if (!inp.value) return;
    navigator.clipboard.writeText(inp.value).then(function() {
      copyBtn.textContent = 'Copied!';
      setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500);
      sessionBtn.disabled = false; sessionBtn.style.opacity = '1';
    });
  }, 'btn small');

  var cancelBtn = UI.el('button', {
    type: 'button', class: 'btn', text: 'Cancel',
    on: {click: function() { m.close(); }},
  });
  var body = UI.el('div', null,
    UI.el('p', {
      style: 'font-size:12px;color:var(--muted);margin-bottom:12px',
      text: 'After rotation, the old token immediately stops working. Copy the new token before saving.',
    }),
    UI.el('label', { text: 'New API token (\u2265 16 chars)' }),
    inp,
    UI.el('div', { style: 'display:flex;gap:8px;margin-top:8px' }, genBtn, copyBtn),
    warnCopied,
  );
  m = UI.modal({
    title: 'Rotate API token',
    body: body,
    actions: [cancelBtn, sessionBtn],
  });
}

/**
 * "Connect external tool" panel on the System page.
 *
 * Generates copy-paste snippets from live server config (the DOCKER_HOST
 * the server is actually talking to, the audit-log path on disk, etc.)
 * so the user pastes an exact match, not a template. Dropdown picker so
 * we don't overwhelm the page.
 */
async function _renderConnectPanel(main) {
  // Tool list + templates live in skiff/_config/connect_snippets.toml.
  // Server renders `{dockerHost}` / `{metricsUrl}` / `{audit_log_glob}`
  // from live state and returns the ready blocks; the client only picks
  // by id and lays each block out via UI.copyBlock / UI.copyCmd.
  var payload;
  try {
    payload = await apiFetch(API + '/connect-snippets');
  } catch (e) { return; }
  var tools = (payload && payload.tools) || [];
  if (!tools.length) return;

  main.appendChild(UI.el('h3', {
    style: 'margin-top:28px;margin-bottom:4px;font-size:18px;color:var(--text-strong)',
    text: 'Connect external tool',
  }));
  main.appendChild(UI.el('p', {
    style: 'font-size:12px;color:var(--muted);margin-bottom:12px',
    text: 'Copy-paste snippets tailored to this server — no templating required.',
  }));

  var toolsById = {};
  tools.forEach(function(t) { toolsById[t.id] = t; });

  var sel = UI.el('select', {
    'aria-label': 'Connect external tool',
    style: 'padding:6px 10px;font-size:13px;background:var(--card);'
         + 'border:1px solid var(--border);border-radius:6px;color:var(--text)',
  }, tools.map(function(t) {
    return UI.el('option', { value: t.id, text: t.label });
  }));
  var body = UI.el('div');
  var wrap = UI.el('div', {
    style: 'background:var(--card);border:1px solid var(--border);border-radius:8px;padding:18px',
  },
    UI.el('div', { style: 'display:flex;align-items:center;gap:10px;margin-bottom:14px' },
      UI.el('label', {
        style: 'font-size:12px;font-weight:500;color:var(--muted);margin:0',
        text: 'Tool:',
      }),
      sel,
    ),
    body,
  );
  main.appendChild(wrap);

  function renderTool(id) {
    while (body.firstChild) body.removeChild(body.firstChild);
    var tool = toolsById[id];
    if (!tool) return;
    if (tool.hint) {
      body.appendChild(UI.el('p', {
        style: 'font-size:12px;color:var(--muted);margin-bottom:10px',
        text: tool.hint,
      }));
    }
    (tool.blocks || []).forEach(function(b) {
      var widget = b.kind === 'command'
        ? _makeCopyableCommand(b.content, b.filename)
        : _makeCopyableBlock(b.content, b.filename);
      body.appendChild(widget);
    });
    if (tool.note) {
      body.appendChild(UI.el('p', {
        style: 'font-size:12px;color:var(--muted);margin-top:8px',
        text: tool.note,
      }));
    }
  }
  sel.addEventListener('change', function() { renderTool(sel.value); });
  renderTool(tools[0].id);
}

var _makeCopyableBlock = UI.copyBlock;

// ── System ──
async function loadSystem() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading system info...</div>';
  try {
    var results = await Promise.all([apiFetch(API+'/system/info'), apiFetch(API+'/system/df')]);
    if (currentPage !== 'system') return;
    var info = results[0]; var df = results[1];
    main.innerHTML = '';
    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2'); h2.textContent = 'System';
    var ha = document.createElement('div'); ha.className = 'header-actions';
    ha.appendChild(makeActionBtn('Prune system', function() { if(!confirm('Remove stopped containers, dangling images, and unused networks?'))throw new Error('Cancelled'); return guardedAction('prune-system', function() { return apiFetch(API+'/system/prune',{method:'POST'}).then(function(r){ var parts = []; if(r.containers_deleted) parts.push(r.containers_deleted+' container'+(r.containers_deleted===1?'':'s')); if(r.images_deleted) parts.push(r.images_deleted+' image'+(r.images_deleted===1?'':'s')); if(r.networks_deleted) parts.push(r.networks_deleted+' network'+(r.networks_deleted===1?'':'s')); var msg = parts.length ? 'Pruned '+parts.join(', ') : 'Nothing to prune'; if(r.space_reclaimed_mb > 0) msg += '. Reclaimed '+r.space_reclaimed_mb+' MB'; toast(msg, parts.length ? 'success' : 'info'); loadSystem();}); }); }, 'btn danger', 'Pruning\u2026'));
    header.append(h2, ha); main.appendChild(header);

    var grid = document.createElement('div'); grid.className = 'info-grid';
    [['Engine',info.docker_version],['API',info.api_version],['OS',info.os],['Kernel',info.kernel],['Arch',info.architecture],['CPUs',info.cpus],['Memory',info.memory_gb+' GB'],['Storage',info.storage_driver],['Containers',info.containers+' ('+info.containers_running+' running, '+info.containers_paused+' paused, '+info.containers_stopped+' stopped)'],['Images',info.images],['Logging',info.logging_driver],['Cgroup',info.cgroup_driver]].forEach(function(item) {
      var card = document.createElement('div'); card.className = 'info-card';
      var l = document.createElement('div'); l.className = 'label'; l.textContent = item[0];
      var v = document.createElement('div'); v.className = 'value'; v.textContent = String(item[1]);
      card.append(l, v); grid.appendChild(card);
    });
    main.appendChild(grid);

    var dfH = document.createElement('h3'); dfH.textContent = 'Disk Usage'; dfH.style.cssText = 'margin-top:28px;margin-bottom:16px;font-size:18px'; main.appendChild(dfH);
    var dfGrid = document.createElement('div'); dfGrid.className = 'info-grid';
    [['Images',df.images_mb+' MB',df.images_count+' images, '+df.images_reclaimable_mb+' MB reclaimable',null],
     ['Containers',df.containers_mb+' MB',df.containers_count+' containers',null],
     ['Volumes',df.volumes_mb+' MB',df.volumes_count+' volumes, '+df.volumes_reclaimable_mb+' MB reclaimable',null],
     ['Build Cache',df.build_cache_mb+' MB',df.build_cache_reclaimable_mb+' MB reclaimable','build_cache'],
     ['Total',df.total_mb+' MB','',null]].forEach(function(item) {
      var card = document.createElement('div'); card.className = 'info-card';
      var l = document.createElement('div'); l.className = 'label'; l.textContent = item[0];
      var v = document.createElement('div'); v.className = 'value'; v.textContent = item[1];
      card.append(l, v);
      if (item[2]) { var sub = document.createElement('div'); sub.className = 'sub'; sub.textContent = item[2]; card.appendChild(sub); }
      if (item[3] === 'build_cache' && df.build_cache_reclaimable_mb > 0) {
        card.appendChild(makeActionBtn('Prune', function() { if(!confirm('Prune build cache?'))throw new Error('Cancelled'); return guardedAction('prune-build-cache', function() { return apiFetch(API+'/system/prune-build-cache',{method:'POST'}).then(function(r){toast('Reclaimed '+r.space_reclaimed_mb+' MB','success');loadSystem();}); }); }, 'btn danger small', 'Pruning\u2026'));
      }
      dfGrid.appendChild(card);
    });
    main.appendChild(dfGrid);

    // Account section — token rotation + config reset. Shown only
    // when the server isn't env-configured (otherwise the endpoints 403 anyway).
    // /api/setup-state tells us whether the server is in from_env mode.
    _renderAccountSection(main);

    // Connect external tool — copy-paste integration strings for VSCode,
    // JetBrains, docker CLI, Prometheus, SIEMs. All strings generated from
    // live server config so there's nothing to edit after pasting.
    _renderConnectPanel(main);

    // Security options
    if (info.security_options && info.security_options.length) {
      var secH = document.createElement('h3'); secH.textContent = 'Security'; secH.style.cssText = 'margin-top:28px;margin-bottom:12px;font-size:18px'; main.appendChild(secH);
      var secList = document.createElement('div'); secList.style.cssText = 'background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px';
      info.security_options.forEach(function(opt) {
        var p = document.createElement('div'); p.className = 'mono'; p.style.cssText = 'font-size:12px;padding:2px 0'; p.textContent = opt; secList.appendChild(p);
      });
      main.appendChild(secList);
    }

    // Docker Events — live window into daemon-emitted events (container
    // lifecycle, image pulls, network attachments). Matches `docker events`
    // on the CLI. Polls every 5s while the page is visible.
    var eventsH = document.createElement('h3');
    eventsH.textContent = 'Docker Events';
    eventsH.style.cssText = 'margin-top:28px;margin-bottom:4px;font-size:18px';
    main.appendChild(eventsH);
    var eventsDesc = document.createElement('p');
    eventsDesc.style.cssText = 'font-size:12px;color:var(--muted);margin-bottom:12px';
    eventsDesc.textContent = 'Live daemon events for the last minute. Refreshes every 5 seconds while this page is open.';
    main.appendChild(eventsDesc);
    var eventsBox = document.createElement('pre');
    eventsBox.setAttribute('data-testid', 'events-viewer');
    eventsBox.style.cssText = 'background:var(--sidebar-bg);color:#e2e8f0;padding:12px;border-radius:6px;font-size:12px;max-height:240px;overflow:auto;font-family:monospace;margin-bottom:16px';
    eventsBox.textContent = 'Loading\u2026';
    main.appendChild(eventsBox);
    function _refreshEvents() {
      apiFetch(API + '/system/events?since_secs=60&limit=200').then(function(d) {
        if (currentPage !== 'system') return;
        var lines = (d.events || []).map(function(e) {
          var ts = e.time ? new Date(e.time * 1000).toLocaleTimeString() : '';
          var attrs = Object.keys(e.actor_attributes || {}).map(function(k) {
            return k + '=' + e.actor_attributes[k];
          }).join(' ');
          return ts + ' ' + (e.type || '?') + '.' + (e.action || '?')
               + (e.actor_id ? ' [' + e.actor_id + ']' : '')
               + (attrs ? ' ' + attrs : '');
        });
        eventsBox.textContent = lines.length
          ? lines.reverse().join('\n')
          : '(no events in the last ' + (d.since_secs || 60) + 's)';
      }).catch(function() { /* keep last-good */ });
    }
    _refreshEvents();
    managedInterval(_refreshEvents, 5000);

    // Audit log
    var auditH = document.createElement('h3'); auditH.textContent = 'Audit Log'; auditH.style.cssText = 'margin-top:28px;margin-bottom:4px;font-size:18px'; main.appendChild(auditH);
    var auditDesc = document.createElement('p'); auditDesc.style.cssText = 'font-size:12px;color:var(--muted);margin-bottom:12px'; auditDesc.textContent = 'Recent API requests made to this app.'; main.appendChild(auditDesc);
    var auditToolbar = document.createElement('div'); auditToolbar.style.cssText = 'display:flex;gap:8px;margin-bottom:8px;align-items:center;flex-wrap:wrap';
    // Tail-size selector — backend caps at MAX_AUDIT_LINES (2000 by default).
    // Showing only 200 with no affordance to see more was silent truncation.
    var tailSelect = document.createElement('select');
    tailSelect.setAttribute('data-testid', 'audit-tail-select');
    tailSelect.style.cssText = 'padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);font-size:13px';
    [200, 500, 1000, 2000].forEach(function(n) {
      var opt = document.createElement('option'); opt.value = String(n); opt.textContent = 'Last ' + n;
      tailSelect.appendChild(opt);
    });
    var auditFilter = document.createElement('input');
    auditFilter.type = 'search';
    auditFilter.className = 'search-bar';
    auditFilter.placeholder = 'Filter by event, path, method, or status...';
    auditFilter.setAttribute('data-testid', 'audit-filter');
    auditFilter.style.cssText = 'flex:1;min-width:220px;margin:0';
    var auditRefreshBtn = makeBtn('Refresh', function() { loadAuditLog(auditBody, tailSelect, auditFilter, auditH); }, 'btn small');
    var auditDlBtn = makeBtn('Download .jsonl', function() {
      var a = document.createElement('a'); a.href = API+'/system/audit-log/download';
      a.setAttribute('download','audit.jsonl'); a.click();
    }, 'btn small');
    auditToolbar.append(tailSelect, auditFilter, auditRefreshBtn, auditDlBtn);
    main.appendChild(auditToolbar);
    var auditTable = document.createElement('table');
    auditTable.innerHTML = '<thead><tr><th>Time</th><th>Event</th><th>Resource</th><th>Method</th><th>Path</th><th>Status</th><th>Remote</th></tr></thead>';
    var auditBody = document.createElement('tbody');
    auditTable.appendChild(auditBody);
    main.appendChild(auditTable);
    tailSelect.addEventListener('change', function() { loadAuditLog(auditBody, tailSelect, auditFilter, auditH); });
    auditFilter.addEventListener('input', function() { loadAuditLog(auditBody, tailSelect, auditFilter, auditH, true); });
    loadAuditLog(auditBody, tailSelect, auditFilter, auditH);

  } catch (e) { main.innerHTML=''; var p=document.createElement('p'); p.style.color='var(--red)'; p.textContent='Failed: '+e.message; main.appendChild(p); }
}

// Cache of the most recently fetched rows so an input filter doesn't re-hit
// the network on every keystroke. `reuseCache=true` (passed by the filter
// keystroke handler) re-renders without an /api/... round trip.
var _auditRowCache = [];
function loadAuditLog(tbody, tailSelect, auditFilter, auditH, reuseCache) {
  var tail = tailSelect && tailSelect.value ? Number(tailSelect.value) : 200;
  var needle = auditFilter && auditFilter.value ? auditFilter.value.toLowerCase() : '';
  function _rowMatches(row) {
    if (!needle) return true;
    var hay = [
      row.event, row.raw, row.path, row.method, row.remote,
      row.resource_type, row.resource_id,
      typeof row.status === 'number' ? String(row.status) : '',
    ].filter(Boolean).join(' ').toLowerCase();
    return hay.indexOf(needle) !== -1;
  }
  function _paint(rows) {
    tbody.innerHTML = '';
    var matching = rows.filter(_rowMatches);
    if (auditH) {
      auditH.textContent = 'Audit Log (' + matching.length +
        (needle && matching.length !== rows.length ? '/' + rows.length : '') + ')';
    }
    if (!matching.length) {
      var tr0 = document.createElement('tr'); var td0 = document.createElement('td');
      td0.colSpan = 7; td0.style.cssText = 'text-align:center;color:var(--muted);padding:20px';
      td0.textContent = needle ? 'No audit entries match "' + needle + '".' : 'No audit entries yet.';
      tr0.appendChild(td0); tbody.appendChild(tr0); return;
    }
    matching.slice().reverse().forEach(function(row) {
      _paintAuditRow(row, tbody);
    });
  }
  if (reuseCache && _auditRowCache.length) { _paint(_auditRowCache); return; }
  tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px">Loading…</td></tr>';
  apiFetch(API+'/system/audit-log?tail=' + tail).then(function(rows) {
    _auditRowCache = rows || [];
    _paint(_auditRowCache);
  }).catch(function(e) {
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 7;
    td.style.cssText = 'color:var(--red);padding:12px';
    td.textContent = 'Failed: ' + e.message;
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
}

function _paintAuditRow(row, tbody) {
      var tr = document.createElement('tr');
      var ts = row.timestamp ? new Date(row.timestamp).toLocaleTimeString() : '';
      var evt = row.event || row.raw || '';
      var statusCode = typeof row.status === 'number' ? row.status : 0;
      var statusColor = statusCode >= 400 ? 'var(--red)' : statusCode >= 200 ? 'var(--green,#22c55e)' : '';
      function _auditCell(text, extraStyle) {
        var td = document.createElement('td');
        td.style.cssText = 'font-size:12px;' + (extraStyle || '');
        td.textContent = text;
        return td;
      }
      var td0 = document.createElement('td');
      td0.style.cssText = 'font-size:11px;color:var(--muted);white-space:nowrap';
      td0.textContent = ts;
      var td4 = _auditCell(String(row.status || ''));
      if (statusColor) td4.style.color = statusColor;
      var td3 = document.createElement('td');
      td3.style.cssText = 'font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      td3.title = row.path || '';
      td3.textContent = row.path || '';
      // Resource column: server-classified resource_type + resource_id
      // (e.g. "container abc123", "volume my-vol"). Falls back to
      // dashes when the line is a non-resource-scoped event like
      // app.started / security.* warnings.
      var resourceText = row.resource_type && row.resource_id
        ? (row.resource_type + ' ' + row.resource_id)
        : (row.resource_type || row.resource_id || '—');
      tr.appendChild(td0);
      tr.appendChild(_auditCell(evt));
      tr.appendChild(_auditCell(resourceText, 'font-family:var(--mono,monospace);color:var(--muted)'));
      tr.appendChild(_auditCell(row.method || ''));
      tr.appendChild(td3);
      tr.appendChild(td4);
      tr.appendChild(_auditCell(row.remote || '', 'color:var(--muted)'));
      tbody.appendChild(tr);
}

// ── Keyboard shortcuts ──
document.addEventListener('keydown', function(e) {
  // Don't fire when typing in an input/textarea/select or inside a modal
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
  if (!getToken()) return;

  // Esc — close any open modal
  if (e.key === 'Escape') {
    var modal = document.querySelector('.modal-overlay');
    if (modal) { modal.remove(); return; }
  }

  // Don't fire if a modal is open (other keys)
  if (document.querySelector('.modal-overlay')) return;

  var key = e.key;
  // 1-6 — sidebar navigation
  var navMap = {'1':'containers','2':'images','3':'volumes','4':'networks','5':'compose','6':'system'};
  if (navMap[key]) { showPage(navMap[key]); return; }

  // r — Run new container
  if (key === 'r' && currentPage === 'containers') { showRunModal(); return; }

  // / — focus search bar
  if (key === '/') {
    e.preventDefault();
    var searchInput = document.querySelector('#container-search, #image-search, input[placeholder*="Search"]');
    if (searchInput) searchInput.focus();
    return;
  }

  // ? — show shortcut help. Built via UI.el (no innerHTML, no inline
  // onclick) so the strict CSP `script-src 'self'` actually lets the
  // Close button work.
  if (key === '?') {
    var SHORTCUTS = [
      ['1–6',  'Navigate sections'],
      ['r',    'Run new container'],
      ['/',    'Focus search'],
      ['Esc',  'Close modal'],
      ['?',    'Show this help'],
    ];
    var kbdStyle = 'background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:2px 6px';
    var rows = SHORTCUTS.map(function(row) {
      return UI.el('tr', null,
        UI.el('td', { style: 'padding:5px 0' }, UI.el('kbd', { style: kbdStyle, text: row[0] })),
        UI.el('td', { style: 'padding:5px 0 5px 12px', text: row[1] }),
      );
    });
    var closeBtn = UI.el('button', { class: 'btn', style: 'margin-top:20px;width:100%', text: 'Close' });
    var box = UI.el('div', {
      style: 'background:var(--card);border-radius:12px;padding:28px 32px;min-width:320px;max-width:480px',
    },
      UI.el('h3', { style: 'margin-bottom:16px;font-size:16px', text: 'Keyboard shortcuts' }),
      UI.el('table', { style: 'width:100%;border-collapse:collapse;font-size:13px' }, rows),
      closeBtn,
    );
    var overlay = UI.el('div', {
      class: 'modal-overlay',
      style: 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1000',
    }, box);
    function dismiss() { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }
    overlay.addEventListener('click', function(ev) { if (ev.target === overlay) dismiss(); });
    closeBtn.addEventListener('click', dismiss);
    document.body.appendChild(overlay);
    return;
  }
});
