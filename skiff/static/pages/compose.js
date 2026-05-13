// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Compose page — per-page module loaded by index.html.
 *
 * Uses globals from app.js + ui.js: API, currentPage, apiFetch, toast,
 * makeBtn, makeActionBtn, showDetail, showCompose, UI.
 */
"use strict";

// Scale modal — pick a service, enter replica count. Fires
// /api/compose/{project}/scale which calls `docker compose up -d --scale`.
function _showComposeScaleModal(stack) {
  var services = (stack.services || []).map(function(s) { return s.name; });
  if (!services.length) {
    toast('No services to scale', 'error');
    return;
  }
  UI.formModal({
    title: 'Scale stack ' + stack.name,
    fields: [
      {
        name: 'service_name',
        label: 'Service',
        type: 'select',
        options: services.map(function(s) { return { value: s }; }),
      },
      {
        name: 'replicas',
        label: 'Replicas',
        type: 'number',
        value: '1',
        help: 'Target number of instances. 0 stops the service without removing it. Max is server-configured (default 10).',
      },
    ],
    submitLabel: 'Scale',
    onSubmit: function(values) {
      return apiFetch(
        API + '/compose/' + encodeURIComponent(stack.name) + '/scale'
        + '?service_name=' + encodeURIComponent(values.service_name)
        + '&replicas=' + encodeURIComponent(values.replicas),
        { method: 'POST' },
      ).then(function() {
        toast('Scaled ' + values.service_name + ' to ' + values.replicas, 'success');
        showCompose();
      });
    },
  });
}


// Stack-aggregated logs modal. Hits /api/compose/{project}/logs,
// renders lines as "service | …" the same way `docker compose logs` does.
// textContent only — server-supplied lines never touch innerHTML.
async function _showComposeAggregateLogs(projectName) {
  var viewer = UI.el('pre', {
    style: 'background:var(--sidebar-bg);color:#e2e8f0;padding:12px;border-radius:6px;'
         + 'font-size:12px;white-space:pre-wrap;max-height:60vh;overflow:auto;font-family:monospace',
    text: 'Loading\u2026',
  });
  var m = UI.modal({
    title: 'Aggregated logs: ' + projectName,
    body: viewer,
    actions: [makeBtn('Close', function() { m.close(); })],
  });
  // `m.box` already has a `.modal` cssText from UI.modal — these two
  // properties augment it. With UI.setStyle (CSSOM-rule backed) we
  // simply set both properties; the rule's cssText is the union of
  // every setStyle call on this element.
  UI.setStyle(m.box, 'max-width', '900px');
  UI.setStyle(m.box, 'width', '90vw');
  try {
    var data = await apiFetch(API + '/compose/' + encodeURIComponent(projectName) + '/logs?tail=500');
    viewer.textContent = (data.lines || []).join('\n') || '(no logs)';
  } catch (e) {
    viewer.textContent = 'Error: ' + (e.message || 'failed to load');
  }
}


async function showCompose() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading...</div>';

  var stacks = [];
  // Surface any fetch failure below the header so the user can tell
  // "zero stacks" from "broken connection" (network blip, daemon down,
  // 503 tunnel drop). An empty array still renders the upload form so
  // the user can deploy a first stack even if the listing is degraded.
  var stacksLoadError = null;
  try { stacks = await apiFetch(API + '/compose/stacks'); }
  catch (e) { stacksLoadError = (e && e.message) || 'failed to load stacks'; }
  if (currentPage !== 'compose') return;

  main.innerHTML = '';
  var header = document.createElement('div'); header.className = 'page-header';
  var h2 = document.createElement('h2'); h2.textContent = 'Compose';
  header.appendChild(h2); main.appendChild(header);
  var compDesc = document.createElement('p');
  UI.setStyle(compDesc, 'color:var(--muted);font-size:12px;margin-bottom:16px');
  compDesc.textContent = 'Compose files are validated before deployment. Privileged mode, host-path mounts, build instructions, and unapproved registries are blocked.';
  main.appendChild(compDesc);
  if (stacksLoadError) {
    var errBanner = document.createElement('div');
    errBanner.className = 'field-error';
    UI.setStyle(errBanner, 'display', 'block');
    errBanner.setAttribute('role', 'alert');
    errBanner.textContent = 'Could not load stacks: ' + stacksLoadError;
    main.appendChild(errBanner);
  }

  // Always render the Running Stacks section (header + search + card
  // wrap) so the page offers a consistent "list-with-search" affordance
  // matching every other list page. The empty case renders a muted
  // "No running stacks yet — upload one below" line in place of cards.
  var stackHeader = document.createElement('h3');
  stackHeader.textContent = 'Running Stacks (' + stacks.length + ')';
  UI.setStyle(stackHeader, 'font-size:16px;margin-bottom:12px');
  main.appendChild(stackHeader);
  var search = document.createElement('input');
  search.type = 'search';
  search.className = 'search-bar';
  search.placeholder = 'Search stacks by project name or service...';
  search.setAttribute('data-testid', 'compose-search');
  if (stacks.length === 0) search.disabled = true;
  main.appendChild(search);
  if (stacks.length > 0) {
    var cardsWrap = document.createElement('div');
    main.appendChild(cardsWrap);
    function renderStackCards(q) {
      cardsWrap.innerHTML = '';
      var needle = (q || '').toLowerCase();
      var filtered = stacks.filter(function(s) {
        if (!needle) return true;
        if ((s.name || '').toLowerCase().indexOf(needle) !== -1) return true;
        return (s.services || []).some(function(svc) {
          return (svc.name || '').toLowerCase().indexOf(needle) !== -1;
        });
      });
      stackHeader.textContent = 'Running Stacks (' + filtered.length +
        (needle && filtered.length !== stacks.length ? '/' + stacks.length : '') + ')';
      if (!filtered.length) {
        var em = document.createElement('p');
        UI.setStyle(em, 'color:var(--muted);font-size:13px;padding:12px 0');
        em.textContent = needle ? 'No matches' : '';
        cardsWrap.appendChild(em);
        return;
      }
      filtered.forEach(function(stack) { cardsWrap.appendChild(_renderStackCard(stack)); });
    }
    // Split card rendering into a helper so the filter can re-render
    // without duplicating the big DOM block. The whole forEach body
    // below moves into _renderStackCard.
    function _renderStackCard(stack) {
      var card = document.createElement('div'); card.className = 'stack-card';
      var h4 = document.createElement('h4');
      var dot = document.createElement('span'); dot.className = 'dot ' + (stack.status === 'running' ? 'ok' : 'down');
      h4.append(dot, document.createTextNode(stack.name));
      card.appendChild(h4);
      var svcList = document.createElement('div'); svcList.className = 'stack-services';
      UI.setStyle(svcList, 'display:flex;flex-direction:column;gap:4px;');
      stack.services.forEach(function(s) {
        var row = document.createElement('div');
        UI.setStyle(row, 'display:flex;align-items:center;gap:8px;font-size:13px;');
        var svcDot = document.createElement('span');
        svcDot.className = 'dot ' + (s.state === 'running' ? 'ok' : 'down');
        var svcText = document.createElement('span');
        svcText.textContent = s.name + ' (' + s.state + ')';
        UI.setStyle(svcText, 'flex:1;');
        var svcLogs = document.createElement('button');
        svcLogs.className = 'btn small'; svcLogs.textContent = 'Logs';
        svcLogs.addEventListener('click', function() { showDetail(s.container_id, s.name, 'logs'); });
        row.append(svcDot, svcText, svcLogs);
        row.appendChild(makeActionBtn('Restart', function() {
          return apiFetch(
            API + '/compose/' + encodeURIComponent(stack.name) +
            '/services/' + encodeURIComponent(s.name) + '/restart',
            { method: 'POST' },
          ).then(function() {
            toast(s.name + ' restarted', 'success');
            showCompose();
          });
        }, 'btn small', 'Restarting\u2026'));
        svcList.appendChild(row);
      });
      card.appendChild(svcList);
      var btnRow = document.createElement('div');
      UI.setStyle(btnRow, 'margin-top:8px;display:flex;gap:6px;flex-wrap:wrap');
      btnRow.appendChild(makeBtn('All service logs', function() {
        _showComposeAggregateLogs(stack.name);
      }, 'btn small'));
      btnRow.appendChild(makeActionBtn('Start', function() {
        return apiFetch(API + '/compose/' + encodeURIComponent(stack.name) + '/start',
                        { method: 'POST' }).then(function() {
          toast(stack.name + ' started', 'success'); showCompose();
        });
      }, 'btn small', 'Starting\u2026'));
      btnRow.appendChild(makeActionBtn('Stop', function() {
        return apiFetch(API + '/compose/' + encodeURIComponent(stack.name) + '/stop',
                        { method: 'POST' }).then(function() {
          toast(stack.name + ' stopped', 'info'); showCompose();
        });
      }, 'btn small', 'Stopping\u2026'));
      btnRow.appendChild(makeActionBtn('Pull updates', function() {
        if (!confirm('Pull latest images for all services in ' + stack.name + '?\nRun a Restart after to apply.'))
          throw new Error('Cancelled');
        // Server's IMAGE_PULL_TIMEOUT is 300s. Default 30s client abort
        // would kill the fetch mid-pull and leave the user guessing if
        // the pull completed server-side.
        return apiFetch(API + '/compose/' + encodeURIComponent(stack.name) + '/pull',
                        { method: 'POST', _timeout: 310000 }).then(function() {
          toast('Images pulled. Run Restart to apply new tags.', 'success');
        });
      }, 'btn small', 'Pulling\u2026'));
      btnRow.appendChild(makeActionBtn('Scale\u2026', function() {
        _showComposeScaleModal(stack);
        throw new Error('ModalOpened');  // don't trigger success toast
      }, 'btn small'));
      btnRow.appendChild(makeActionBtn('Restart all', function() {
        // Restart = down + up in sequence. Use undo=false on down so
        // the restart happens immediately (otherwise the user would
        // see an undo toast from the restart path, which is confusing
        // since they asked for a restart, not a teardown).
        return apiFetch(API + '/compose/down?project_name=' + encodeURIComponent(stack.name) + '&undo=false',
                        { method: 'POST', _timeout: 70000 }).then(function() {
          toast(stack.name + ' stopped, restarting...', 'info');
          var form = new FormData();
          return apiFetch(API + '/compose/up?project_name=' + encodeURIComponent(stack.name),
                          { method: 'POST', body: form, _timeout: 130000 });
        }).then(function() { toast(stack.name + ' restarted', 'success'); showCompose(); });
      }, 'btn small', 'Restarting\u2026'));
      btnRow.appendChild(makeBtn('Download YAML', function() {
        var a = document.createElement('a');
        a.href = API + '/compose/' + encodeURIComponent(stack.name) + '/download';
        a.setAttribute('download', stack.name + '.docker-compose.yml');
        a.click();
      }, 'btn small'));
      btnRow.appendChild(makeActionBtn('Tear down', function() {
        if (!confirm('Tear down stack "' + stack.name + '"? (Undo available for ~' + (window.UNDO_WINDOW_SECS || 5) + 's.)'))
          throw new Error('Cancelled');
        // Server caps down at COMPOSE_DOWN_TIMEOUT = 60s; keep client
        // abort ≥10s above so the server emits its own envelope first.
        // Default undo=true returns {undo_token, expires_in}.
        return apiFetch(API + '/compose/down?project_name=' + encodeURIComponent(stack.name),
                        { method: 'POST', _timeout: 70000 }).then(function(r) {
          if (r && r.undo_token) {
            window.renderUndoToast('Stack "' + stack.name + '" tear-down', r.undo_token, r.expires_in, showCompose);
            return;
          }
          toast(stack.name + ' stopped', 'info'); showCompose();
        });
      }, 'btn danger small', 'Tearing down\u2026'));
      card.appendChild(btnRow);
      return card;
    }
    renderStackCards('');
    search.oninput = function() { renderStackCards(search.value); };
    main.appendChild(document.createElement('hr'));
  }

  var deployHeader = document.createElement('h3');
  deployHeader.textContent = 'Deploy Stack';
  UI.setStyle(deployHeader, 'font-size:16px;margin:16px 0 12px');
  main.appendChild(deployHeader);
  var dz = document.createElement('div'); dz.className = 'drop-zone';
  var fi = document.createElement('input'); fi.type = 'file'; fi.accept = '.yml,.yaml'; UI.setStyle(fi, 'display', 'none');
  fi.onchange = function() { if (fi.files[0]) uploadCompose(fi.files[0]); };
  dz.onclick = function() { fi.click(); };
  var p1 = document.createElement('p');
  UI.setStyle(p1, 'font-weight:500;font-size:14px;color:var(--text)');
  p1.textContent = 'Upload compose file (docker-compose.yml)';
  var p2 = document.createElement('p');
  UI.setStyle(p2, 'font-size:12px;margin-top:8px;color:var(--muted)');
  p2.textContent = 'Click or drag and drop';
  dz.append(fi, p1, p2);
  dz.addEventListener('dragover', function(e) { e.preventDefault(); UI.setStyle(dz, 'borderColor', 'var(--accent)'); });
  dz.addEventListener('dragleave', function() { UI.setStyle(dz, 'borderColor', 'var(--border)'); });
  dz.addEventListener('drop', function(e) {
    e.preventDefault(); UI.setStyle(dz, 'borderColor', 'var(--border)');
    if (e.dataTransfer.files[0]) uploadCompose(e.dataTransfer.files[0]);
  });
  main.appendChild(dz);

  var controls = document.createElement('div'); controls.className = 'mt-16';
  var lbl = document.createElement('label');
  lbl.htmlFor = 'compose-project';
  UI.setStyle(lbl, 'font-size:12px;font-weight:500;color:var(--muted)');
  lbl.textContent = 'Project Name';
  var inp = document.createElement('input'); inp.id = 'compose-project'; inp.value = 'dev';
  UI.setStyle(inp, 'padding:9px 12px;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;width:200px;margin-top:6px');
  controls.append(lbl, document.createElement('br'), inp); main.appendChild(controls);
  var output = document.createElement('div'); output.id = 'compose-output'; output.className = 'mt-16';
  main.appendChild(output);
}


async function uploadCompose(file) {
  var project = document.getElementById('compose-project').value;
  var form = new FormData(); form.append('file', file);
  var out = document.getElementById('compose-output');
  out.innerHTML = '<div class="log-viewer">Deploying stack...</div>';
  try {
    // Server caps compose up at COMPOSE_UP_TIMEOUT = 120s; default 30s
    // client abort would leave the user staring at a fetch-level timeout
    // while the pull+up keeps running server-side. +10s margin so the
    // server's own timeout fires first with its structured envelope.
    var data = await apiFetch(API + '/compose/up?project_name=' + encodeURIComponent(project),
                              { method: 'POST', body: form, _timeout: 130000 });
    var lv = document.createElement('div'); lv.className = 'log-viewer'; UI.setStyle(lv, 'color', '#3fb950');
    lv.textContent = data.output || 'Stack deployed successfully.';
    out.innerHTML = ''; out.appendChild(lv);
    toast('Stack "' + project + '" deployed', 'success');
    setTimeout(function() { if (currentPage === 'compose') showCompose(); }, 500);
  } catch (e) {
    var lv = document.createElement('div'); lv.className = 'log-viewer'; UI.setStyle(lv, 'color', '#f85149');
    lv.textContent = e.message; out.innerHTML = ''; out.appendChild(lv);
  }
}
