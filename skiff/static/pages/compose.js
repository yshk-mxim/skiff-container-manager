// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Compose page — per-page module loaded by index.html.
 *
 * Uses globals from app.js + ui.js: API, currentPage, apiFetch, toast,
 * makeBtn, makeActionBtn, showDetail, showCompose, UI.
 */
"use strict";

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
  m.box.style.cssText += 'max-width:900px;width:90vw;';
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
  try { stacks = await apiFetch(API + '/compose/stacks'); } catch (e) {}
  if (currentPage !== 'compose') return;

  main.innerHTML = '';
  var header = document.createElement('div'); header.className = 'page-header';
  var h2 = document.createElement('h2'); h2.textContent = 'Compose';
  header.appendChild(h2); main.appendChild(header);
  var compDesc = document.createElement('p');
  compDesc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
  compDesc.textContent = 'Compose files are validated before deployment. Privileged mode, host-path mounts, build instructions, and unapproved registries are blocked.';
  main.appendChild(compDesc);

  if (stacks.length > 0) {
    var stackHeader = document.createElement('h3');
    stackHeader.textContent = 'Running Stacks';
    stackHeader.style.cssText = 'font-size:16px;margin-bottom:12px';
    main.appendChild(stackHeader);
    stacks.forEach(function(stack) {
      var card = document.createElement('div'); card.className = 'stack-card';
      var h4 = document.createElement('h4');
      var dot = document.createElement('span'); dot.className = 'dot ' + (stack.status === 'running' ? 'ok' : 'down');
      h4.append(dot, document.createTextNode(stack.name));
      card.appendChild(h4);
      var svcList = document.createElement('div'); svcList.className = 'stack-services';
      svcList.style.cssText = 'display:flex;flex-direction:column;gap:4px;';
      stack.services.forEach(function(s) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:13px;';
        var svcDot = document.createElement('span');
        svcDot.className = 'dot ' + (s.state === 'running' ? 'ok' : 'down');
        var svcText = document.createElement('span');
        svcText.textContent = s.name + ' (' + s.state + ')';
        svcText.style.cssText = 'flex:1;';
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
      btnRow.style.cssText = 'margin-top:8px;display:flex;gap:6px;flex-wrap:wrap';
      btnRow.appendChild(makeBtn('All service logs', function() {
        _showComposeAggregateLogs(stack.name);
      }, 'btn small'));
      btnRow.appendChild(makeActionBtn('Restart all', function() {
        return apiFetch(API + '/compose/down?project_name=' + encodeURIComponent(stack.name),
                        { method: 'POST' }).then(function() {
          toast(stack.name + ' stopped, restarting...', 'info');
          var form = new FormData();
          return apiFetch(API + '/compose/up?project_name=' + encodeURIComponent(stack.name),
                          { method: 'POST', body: form });
        }).then(function() { toast(stack.name + ' restarted', 'success'); showCompose(); });
      }, 'btn small', 'Restarting\u2026'));
      btnRow.appendChild(makeActionBtn('Tear down', function() {
        return apiFetch(API + '/compose/down?project_name=' + encodeURIComponent(stack.name),
                        { method: 'POST' }).then(function() {
          toast(stack.name + ' stopped', 'info'); showCompose();
        });
      }, 'btn danger small', 'Tearing down\u2026'));
      card.appendChild(btnRow);
      main.appendChild(card);
    });
    main.appendChild(document.createElement('hr'));
  }

  var deployHeader = document.createElement('h3');
  deployHeader.textContent = 'Deploy Stack';
  deployHeader.style.cssText = 'font-size:16px;margin:16px 0 12px';
  main.appendChild(deployHeader);
  var dz = document.createElement('div'); dz.className = 'drop-zone';
  var fi = document.createElement('input'); fi.type = 'file'; fi.accept = '.yml,.yaml'; fi.style.display = 'none';
  fi.onchange = function() { if (fi.files[0]) uploadCompose(fi.files[0]); };
  dz.onclick = function() { fi.click(); };
  var p1 = document.createElement('p');
  p1.style.cssText = 'font-weight:500;font-size:14px;color:var(--text)';
  p1.textContent = 'Upload compose file (docker-compose.yml)';
  var p2 = document.createElement('p');
  p2.style.cssText = 'font-size:12px;margin-top:8px;color:var(--muted)';
  p2.textContent = 'Click or drag and drop';
  dz.append(fi, p1, p2);
  dz.addEventListener('dragover', function(e) { e.preventDefault(); dz.style.borderColor = 'var(--accent)'; });
  dz.addEventListener('dragleave', function() { dz.style.borderColor = 'var(--border)'; });
  dz.addEventListener('drop', function(e) {
    e.preventDefault(); dz.style.borderColor = 'var(--border)';
    if (e.dataTransfer.files[0]) uploadCompose(e.dataTransfer.files[0]);
  });
  main.appendChild(dz);

  var controls = document.createElement('div'); controls.className = 'mt-16';
  var lbl = document.createElement('label');
  lbl.style.cssText = 'font-size:12px;font-weight:500;color:var(--muted)';
  lbl.textContent = 'Project Name';
  var inp = document.createElement('input'); inp.id = 'compose-project'; inp.value = 'dev';
  inp.style.cssText = 'padding:9px 12px;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;width:200px;margin-top:6px';
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
    var data = await apiFetch(API + '/compose/up?project_name=' + encodeURIComponent(project),
                              { method: 'POST', body: form });
    var lv = document.createElement('div'); lv.className = 'log-viewer'; lv.style.color = '#3fb950';
    lv.textContent = data.output || 'Stack deployed successfully.';
    out.innerHTML = ''; out.appendChild(lv);
    toast('Stack "' + project + '" deployed', 'success');
    setTimeout(function() { if (currentPage === 'compose') showCompose(); }, 500);
  } catch (e) {
    var lv = document.createElement('div'); lv.className = 'log-viewer'; lv.style.color = '#f85149';
    lv.textContent = e.message; out.innerHTML = ''; out.appendChild(lv);
  }
}
