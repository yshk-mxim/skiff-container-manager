// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Volumes page — per-page module loaded by index.html.
 *
 * Uses globals from app.js + ui.js: API, currentPage, apiFetch, toast,
 * makeBtn, makeActionBtn, guardedAction, undoableDelete, UI, t.
 *
 * User-facing strings come from `window.SKIFF_STRINGS` via `t(key)`. See
 * `skiff/static/strings.en.js` for the dict and the pre-i18n convention.
 */
"use strict";

async function _showVolumeInspectModal(name) {
  var body = UI.el('div', { text: t('common.loading') });
  var m = UI.modal({
    title: t('volumes.modal.inspect_title', { name: name }),
    body: body,
    actions: [makeBtn(t('common.close'), function() { m.close(); })],
  });
  m.box.style.cssText += 'max-width:540px;';
  try {
    var d = await apiFetch(API + '/volumes/' + encodeURIComponent(name) + '/inspect');
    body.innerHTML = '';
    // Sentinel -1 = driver doesn't report usage data; show a friendly phrase
    // instead of "-1" which looks like a bug.
    function fmt(v) { return v === -1 ? '(not reported by driver)' : v; }
    var noneLower = '(' + t('common.none').toLowerCase() + ')';
    var entries = [
      [t('volumes.columns.name'), d.name],
      [t('volumes.columns.driver'), d.driver],
      [t('volumes.inspect.scope'), d.scope || t('volumes.inspect.local_default')],
      [t('volumes.columns.mountpoint'), d.mountpoint],
      [t('volumes.columns.created'), d.created ? new Date(d.created).toLocaleString() : ''],
      [t('volumes.inspect.usage_bytes'), fmt(d.usage_bytes)],
      [t('volumes.inspect.ref_count'), fmt(d.ref_count)],
      [t('volumes.inspect.labels'), Object.keys(d.labels || {}).length ? d.labels : noneLower],
      [t('volumes.inspect.options'), Object.keys(d.options || {}).length ? d.options : noneLower],
    ];
    if (d.status && Object.keys(d.status).length) {
      entries.push([t('volumes.inspect.status'), d.status]);
    }
    entries.push([
      t('volumes.inspect.used_by'),
      (d.containers && d.containers.length) ? d.containers.join(', ') : noneLower,
    ]);
    entries.forEach(function(e) { body.appendChild(UI.kvRow(e[0], e[1])); });
  } catch (e) {
    body.innerHTML = '';
    body.appendChild(UI.el('p', { style: 'color:var(--red)', text: t('common.error') + ': ' + e.message }));
  }
}


async function loadVolumes() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">' + t('common.loading') + '</div>';
  try {
    var volumes = await apiFetch(API + '/volumes');
    if (currentPage !== 'volumes') return;
    main.innerHTML = '';
    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2');
    h2.textContent = t('volumes.title') + ' (' + volumes.length + ')';
    var ha = document.createElement('div'); ha.className = 'header-actions';
    ha.append(
      makeBtn(t('volumes.actions.create'), showCreateVolumeModal, 'btn primary'),
      makeActionBtn(t('volumes.actions.prune'), function() {
        if (!confirm(t('volumes.confirm.prune'))) throw new Error('Cancelled');
        return guardedAction('prune-volumes', function() {
          return apiFetch(API + '/volumes/prune', { method: 'POST' }).then(function(r) {
            toast(t('volumes.toast.pruned', {
              count: (r.deleted || []).length,
              size: (r.space_reclaimed_mb || 0) + ' MB',
            }), 'success');
            loadVolumes();
          });
        });
      }, 'btn danger', t('common.loading')),
    );
    header.append(h2, ha); main.appendChild(header);
    var volDesc = document.createElement('p');
    volDesc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
    volDesc.textContent = t('volumes.description');
    main.appendChild(volDesc);
    var table = document.createElement('table');
    // The <thead> row is built via createElement so the column-header text
    // flows through `t(...)` rather than an inline English literal.
    var thead = document.createElement('thead');
    var thRow = document.createElement('tr');
    [
      t('volumes.columns.name'),
      t('volumes.columns.driver'),
      t('volumes.in_use'),
      t('volumes.columns.created'),
      t('volumes.columns.actions'),
    ].forEach(function(label) {
      var th = document.createElement('th'); th.textContent = label; thRow.appendChild(th);
    });
    thead.appendChild(thRow); table.appendChild(thead);
    var tbody = document.createElement('tbody');
    if (volumes.length === 0) {
      var tr = document.createElement('tr'); var td = document.createElement('td');
      td.colSpan = 5; td.style.cssText = 'text-align:center;color:var(--muted);padding:40px';
      td.textContent = t('volumes.empty'); tr.appendChild(td); tbody.appendChild(tr);
    } else {
      volumes.forEach(function(v) {
        var tr = document.createElement('tr');
        var tdN = document.createElement('td'); tdN.style.fontWeight = '500'; tdN.textContent = v.name;
        var tdD = document.createElement('td'); tdD.textContent = v.driver;
        var tdU = document.createElement('td');
        if (v.in_use) {
          var badge = document.createElement('span');
          badge.className = 'status running';
          badge.textContent = v.containers.join(', ');
          tdU.appendChild(badge);
        } else {
          tdU.textContent = t('volumes.unused'); tdU.style.color = 'var(--muted)';
        }
        var tdC = document.createElement('td'); tdC.className = 'created-time';
        tdC.textContent = v.created ? new Date(v.created).toLocaleString() : '';
        var tdA = document.createElement('td');
        tdA.style.cssText = 'display:flex;gap:4px;';
        tdA.appendChild(makeBtn(t('volumes.actions.inspect'), (function(name) {
          return function() { _showVolumeInspectModal(name); };
        })(v.name), 'btn small'));
        tdA.appendChild(makeActionBtn(t('common.delete'), function() {
          if (!confirm(t('volumes.confirm.remove', { name: v.name }))) throw new Error('Cancelled');
          return guardedAction('del-vol-' + v.name, function() {
            return undoableDelete(
              API + '/volumes/' + encodeURIComponent(v.name),
              'Volume', loadVolumes,
            );
          });
        }, 'btn danger small'));
        tr.append(tdN, tdD, tdU, tdC, tdA); tbody.appendChild(tr);
      });
    }
    table.appendChild(tbody); main.appendChild(table);
  } catch (e) {
    main.innerHTML = '';
    var p = document.createElement('p');
    p.style.color = 'var(--red)';
    p.textContent = t('common.error') + ': ' + e.message;
    main.appendChild(p);
  }
}


function showCreateVolumeModal() {
  UI.formModal({
    title: t('volumes.modal.create_title'),
    fields: [{
      name: 'name',
      label: t('volumes.columns.name'),
      required: true,
      placeholder: t('volumes.create_placeholder'),
    }],
    submitLabel: t('volumes.actions.create'),
    onSubmit: function(values) {
      return guardedAction('create-volume', function() {
        return apiFetch(
          API + '/volumes/create?name=' + encodeURIComponent(values.name),
          { method: 'POST' },
        );
      }).then(function() { toast(t('volumes.toast.created'), 'success'); loadVolumes(); });
    },
  });
}
