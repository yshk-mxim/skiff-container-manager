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


/**
 * Delete a mounted volume by first stopping + removing every container
 * that has it mounted, then deleting the volume. Docker refuses to
 * remove a volume with any attached container (running or stopped) and
 * returns 409; this cascade modal lists the attached containers,
 * confirms the user intends to remove them, runs the cleanup, then
 * soft-deletes the volume with the usual undo window.
 *
 * Flow:
 *   1. Confirm modal listing the N attached containers so the user can
 *      see exactly what will be destroyed.
 *   2. Stop + remove each container via DELETE ?force=true (undo=false
 *      intentionally — we're in a cascade, not a per-container action;
 *      the outer undo covers the whole cascade's visible effect).
 *   3. Delete the volume with ?undo=true so the user still has a
 *      window to reverse the whole operation.
 */
function _deleteVolumeWithAttachedContainers(v) {
  return new Promise(function(resolve, reject) {
    var bg = document.createElement('div');
    bg.className = 'modal-bg';
    bg.onclick = function(ev) { if (ev.target === bg) { bg.remove(); reject(new Error('Cancelled')); } };
    var box = document.createElement('div'); box.className = 'modal';
    box.style.maxWidth = '480px';
    var h3 = document.createElement('h3'); h3.textContent = 'Volume "' + v.name + '" is in use';
    var p1 = document.createElement('p');
    p1.style.cssText = 'font-size:13px;margin:10px 0';
    p1.textContent = 'Deleting this volume requires first stopping + removing every container that mounts it:';
    var list = document.createElement('ul');
    list.style.cssText = 'font-family:monospace;font-size:12px;background:var(--bg-elevated);padding:8px 12px 8px 28px;border-radius:4px;margin:0 0 12px;max-height:140px;overflow:auto';
    v.containers.forEach(function(cname) {
      var li = document.createElement('li'); li.textContent = cname; list.appendChild(li);
    });
    var p2 = document.createElement('p');
    p2.style.cssText = 'font-size:12px;color:var(--muted);margin-bottom:14px';
    p2.textContent = 'Each container will be stopped + force-removed; then the volume is deleted with an undo window (~'
      + (window.UNDO_WINDOW_SECS || 5) + 's).';
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';
    var cancel = document.createElement('button'); cancel.className = 'btn'; cancel.textContent = 'Cancel';
    cancel.onclick = function() { bg.remove(); reject(new Error('Cancelled')); };
    var go = document.createElement('button');
    go.className = 'btn danger';
    go.textContent = 'Remove ' + v.containers.length + ' container(s) + delete volume';
    go.onclick = function() {
      bg.remove();
      resolve(guardedAction('del-vol-cascade-' + v.name, function() {
        // Step 1: cascade-remove each container (force=true, no undo
        // per-item; the volume delete below is what the user undoes).
        var removals = v.containers.map(function(cname) {
          return apiFetch(
            API + '/containers/' + encodeURIComponent(cname) + '?force=true',
            { method: 'DELETE' },
          ).catch(function(e) {
            // One failing container shouldn't abort the whole cascade;
            // log + continue. The final volume delete will 409 again
            // if something's still attached, surfacing the problem.
            return { _failed: cname, err: (e && e.message) || 'remove failed' };
          });
        });
        return Promise.all(removals).then(function(results) {
          var failed = results.filter(function(r) { return r && r._failed; });
          if (failed.length) {
            toast('Failed to remove ' + failed[0]._failed + ': ' + failed[0].err, 'error');
            return;
          }
          // Step 2: delete the volume with undo.
          return undoableDelete(
            API + '/volumes/' + encodeURIComponent(v.name),
            'Volume', loadVolumes,
          );
        });
      }));
    };
    row.append(cancel, go);
    box.append(h3, p1, list, p2, row);
    bg.appendChild(box);
    document.body.appendChild(bg);
    setTimeout(function() { cancel.focus(); }, 0);
  });
}


/**
 * Open a file browser for a volume. Docker has no native volume-fs API,
 * so we POST /volumes/<name>/browse which either returns an existing
 * container that has the volume mounted, or spawns an alpine helper.
 * Either way we navigate to that container's Files tab with the mount
 * path pre-selected — and the ls/files/delete surface the user already
 * knows from the container browser does the actual read/write.
 *
 * If a helper was created, we record its id in sessionStorage so a
 * later "Close browse session" action can DELETE /browse and remove
 * the helper. Helpers without explicit cleanup are also fine — they
 * sit idle until the operator prunes containers.
 */
async function _openVolumeBrowse(name) {
  try {
    var info = await apiFetch(
      API + '/volumes/' + encodeURIComponent(name) + '/browse',
      { method: 'POST' },
    );
    // Remember the helper → used by the detail page to offer a Close
    // button that removes it cleanly.
    if (info.helper) {
      sessionStorage.setItem('skiff.volbrowse.' + info.container_id,
                             JSON.stringify({ volume: name, helper: info.helper }));
    }
    if (typeof window.showDetail === 'function') {
      window.showDetail(info.container_id, name, 'files');
    } else {
      // Fallback: navigate to Containers and hope the user finds the row.
      window.showPage('containers');
    }
    // Store the mount path so the Files tab's initial path can be the
    // volume's mount, not `/` which would just show the container's rootfs.
    if (!window._filesPath) window._filesPath = {};
    window._filesPath[info.container_id] = info.mount_path || '/mnt';
    toast(
      info.helper
        ? 'Spawned helper alpine container to browse volume "' + name + '"'
        : 'Browsing volume "' + name + '" via attached container',
      'info',
    );
  } catch (e) {
    toast(e.message || 'Browse failed', 'error');
  }
}


async function _showVolumeInspectModal(name) {
  var body = UI.el('div', { text: t('common.loading') });
  var _raw = null;
  var m = UI.modal({
    title: t('volumes.modal.inspect_title', { name: name }),
    body: body,
    actions: [
      makeBtn('Browse files', function() { m.close(); _openVolumeBrowse(name); }, 'btn primary small'),
      makeBtn('Export JSON', function() {
        if (_raw) UI.downloadJson(_raw, 'volume-' + name + '.json');
      }, 'btn small'),
      makeBtn(t('common.close'), function() { m.close(); }),
    ],
  });
  m.box.style.cssText += 'max-width:540px;';
  try {
    var d = await apiFetch(API + '/volumes/' + encodeURIComponent(name) + '/inspect');
    _raw = d;
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
  main.innerHTML = '';
  main.appendChild(UI.el('div', { class: 'refreshing', text: t('common.loading') }));
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
        if (!confirm(t('volumes.confirm.prune') + ' (Undo available for ~' + (window.UNDO_WINDOW_SECS || 5) + 's.)'))
          throw new Error('Cancelled');
        return guardedAction('prune-volumes', function() {
          return apiFetch(API + '/volumes/prune', { method: 'POST' }).then(function(r) {
            if (r && r.undo_token) {
              window.renderUndoToast('Volume prune', r.undo_token, r.expires_in, loadVolumes);
              return;
            }
            // Queue full — server ran synchronously.
            toast(t('volumes.toast.pruned', {
              count: (r.deleted || []).length,
              size: (r.space_reclaimed_mb || 0) + ' MB',
            }), 'success');
            loadVolumes();
          });
        });
      }, 'btn danger', 'Pruning\u2026'),
    );
    header.append(h2, ha); main.appendChild(header);
    var volDesc = document.createElement('p');
    volDesc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
    volDesc.textContent = t('volumes.description');
    main.appendChild(volDesc);
    // Search bar (matches the pattern used by containers + images pages).
    // Client-side substring filter on name + driver so large volume lists
    // stay navigable; the 0-vs-N count updates as the filter changes.
    var search = document.createElement('input');
    search.type = 'search';
    search.className = 'search-bar';
    search.placeholder = t('volumes.search_placeholder');
    search.setAttribute('data-testid', 'volumes-search');
    main.appendChild(search);
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
    function renderVolumeRows(q) {
      tbody.innerHTML = '';
      var needle = (q || '').toLowerCase();
      var filtered = volumes.filter(function(v) {
        if (!needle) return true;
        return (v.name || '').toLowerCase().indexOf(needle) !== -1 ||
               (v.driver || '').toLowerCase().indexOf(needle) !== -1;
      });
      h2.textContent = t('volumes.title') + ' (' + filtered.length +
        (needle && filtered.length !== volumes.length ? '/' + volumes.length : '') + ')';
      if (filtered.length === 0) {
        var tr = document.createElement('tr'); var td = document.createElement('td');
        td.colSpan = 5; td.style.cssText = 'text-align:center;color:var(--muted);padding:40px';
        td.textContent = needle ? t('common.no_matches') : t('volumes.empty');
        tr.appendChild(td); tbody.appendChild(tr);
        return;
      }
      filtered.forEach(function(v) {
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
        tdA.appendChild(makeBtn('Browse', (function(name) {
          return function() { _openVolumeBrowse(name); };
        })(v.name), 'btn small'));
        tdA.appendChild(makeActionBtn(t('common.delete'), function() {
          // Mounted volumes can't be removed by Docker without first
          // detaching every container that uses them. Surface that up
          // front — give the user an explicit "Stop & remove those
          // containers, then delete the volume" choice instead of
          // showing a confusing 409 envelope after the click.
          if (v.in_use && v.containers && v.containers.length) {
            return _deleteVolumeWithAttachedContainers(v);
          }
          if (!confirm(t('volumes.confirm.remove', { name: v.name }))) throw new Error('Cancelled');
          return guardedAction('del-vol-' + v.name, function() {
            return undoableDelete(
              API + '/volumes/' + encodeURIComponent(v.name),
              'Volume', loadVolumes,
            );
          });
        }, 'btn danger small', 'Deleting\u2026'));
        tr.append(tdN, tdD, tdU, tdC, tdA); tbody.appendChild(tr);
      });
    }
    renderVolumeRows('');
    search.oninput = function() { renderVolumeRows(search.value); };
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
  // Volumes are immutable after creation (docker doesn't let you change
  // labels, driver, or opts later). So the create modal needs to expose
  // every knob up-front; this form mirrors `docker volume create`.
  UI.formModal({
    title: t('volumes.modal.create_title'),
    fields: [
      {
        name: 'name',
        label: t('volumes.columns.name'),
        required: true,
        placeholder: t('volumes.create_placeholder'),
      },
      {
        name: 'driver',
        label: 'Driver',
        type: 'select',
        options: ['local', 'nfs', 'tmpfs'].map(function(d) { return { value: d }; }),
        help: 'local is the default. nfs/tmpfs require the driver to be available on the host.',
      },
      {
        name: 'labels',
        label: 'Labels (optional)',
        type: 'textarea',
        placeholder: 'env=prod\nteam=platform',
        help: 'key=value pairs, one per line. Labels cannot be changed after creation.',
      },
      {
        name: 'driver_opts',
        label: 'Driver options (optional)',
        type: 'textarea',
        placeholder: 'type=nfs\ndevice=:/path\no=addr=10.0.0.1,rw',
        help: 'Driver-specific options. For local+nfs: type/device/o. Cannot be changed later.',
      },
    ],
    submitLabel: t('volumes.actions.create'),
    onSubmit: function(values) {
      var params = new URLSearchParams({ name: values.name });
      if (values.driver) params.set('driver', values.driver);
      if (values.labels) params.set('labels', values.labels);
      if (values.driver_opts) params.set('driver_opts', values.driver_opts);
      return guardedAction('create-volume', function() {
        return apiFetch(
          API + '/volumes/create?' + params.toString(),
          { method: 'POST' },
        );
      }).then(function() { toast(t('volumes.toast.created'), 'success'); loadVolumes(); });
    },
  });
}
