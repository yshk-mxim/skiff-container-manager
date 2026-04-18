// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Networks page — per-page module loaded by index.html.
 *
 * Uses globals from app.js: API, currentPage, apiFetch, toast, makeBtn,
 * makeActionBtn, guardedAction, loadNetworks.
 */
"use strict";

async function loadNetworks() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading networks...</div>';
  try {
    var networks = await apiFetch(API + '/networks');
    if (currentPage !== 'networks') return;
    main.innerHTML = '';
    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2'); h2.textContent = 'Networks (' + networks.length + ')';
    var ha = document.createElement('div'); ha.className = 'header-actions';
    ha.append(
      makeBtn('Create network', showCreateNetworkModal, 'btn primary'),
      makeActionBtn('Prune unused', function() {
        if (!confirm('Remove unused custom networks?')) throw new Error('Cancelled');
        return guardedAction('prune-networks', function() {
          return apiFetch(API + '/networks/prune', { method: 'POST' }).then(function(r) {
            var n = (r.deleted || []).length;
            toast(n > 0 ? 'Pruned ' + n + ' network' + (n === 1 ? '' : 's')
                        : 'No unused custom networks to prune',
                  n > 0 ? 'success' : 'info');
            loadNetworks();
          });
        });
      }, 'btn danger', 'Pruning\u2026'),
    );
    header.append(h2, ha); main.appendChild(header);
    var desc = document.createElement('p');
    desc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
    desc.textContent = 'Docker networks are internal to the container environment and used for container-to-container communication. They are not externally accessible.';
    main.appendChild(desc);
    var table = document.createElement('table');
    table.innerHTML = '<thead><tr><th>Name</th><th>ID</th><th>Driver</th><th>Scope</th><th>Containers</th><th>Subnet</th><th>Actions</th></tr></thead>';
    var tbody = document.createElement('tbody');
    if (networks.length === 0) {
      var tr = document.createElement('tr'); var td = document.createElement('td');
      td.colSpan = 7; td.style.cssText = 'text-align:center;color:var(--muted);padding:40px';
      td.textContent = 'No networks found'; tr.appendChild(td); tbody.appendChild(tr);
    } else {
      networks.forEach(function(n) {
        var tr = document.createElement('tr');
        var tdN = document.createElement('td'); tdN.style.fontWeight = '500'; tdN.textContent = n.name;
        if (['bridge', 'host', 'none'].indexOf(n.name) !== -1) {
          var def = document.createElement('span');
          def.textContent = 'built-in';
          def.style.cssText = 'margin-left:6px;font-size:10px;font-weight:500;color:var(--muted);background:#f0f0f0;padding:1px 5px;border-radius:4px';
          tdN.appendChild(def);
        }
        var tdId = document.createElement('td'); tdId.className = 'container-id'; tdId.textContent = n.id;
        var tdD = document.createElement('td'); tdD.textContent = n.driver;
        var tdS = document.createElement('td'); tdS.textContent = n.scope;
        var tdC = document.createElement('td');
        var containerNames = Object.values(n.containers || {});
        tdC.textContent = containerNames.length > 0 ? containerNames.join(', ') : 'none';
        tdC.style.color = containerNames.length > 0 ? 'var(--text)' : 'var(--muted)';
        var tdSubnet = document.createElement('td'); tdSubnet.className = 'container-id';
        tdSubnet.textContent = (n.ipam && n.ipam.length)
          ? n.ipam.map(function(c) { return c.Subnet || ''; }).filter(Boolean).join(', ') : '';
        var tdAct = document.createElement('td');
        var actGrp = document.createElement('div'); actGrp.className = 'btn-group';
        if (['bridge', 'host', 'none'].indexOf(n.name) === -1) {
          actGrp.appendChild(makeActionBtn('Connect...', function() {
            showNetworkConnectModal(n.id, n.name);
          }, 'btn small'));
          Object.entries(n.containers || {}).forEach(function(entry) {
            actGrp.appendChild(makeActionBtn('Disconnect ' + entry[1], function() {
              return apiFetch(API + '/networks/' + n.id + '/disconnect?container_id=' + entry[0],
                              { method: 'POST' }).then(function() {
                toast('Disconnected ' + entry[1], 'info'); loadNetworks();
              });
            }, 'btn danger small'));
          });
          actGrp.appendChild(makeActionBtn('Delete', function() {
            if (!confirm('Delete network "' + n.name + '"?')) throw new Error('Cancelled');
            return guardedAction('del-net-' + n.id, function() {
              return apiFetch(API + '/networks/' + n.id, { method: 'DELETE' }).then(function() {
                toast('Network deleted', 'info'); loadNetworks();
              });
            });
          }, 'btn danger small'));
        }
        tdAct.appendChild(actGrp);
        tr.append(tdN, tdId, tdD, tdS, tdC, tdSubnet, tdAct); tbody.appendChild(tr);
      });
    }
    table.appendChild(tbody); main.appendChild(table);
  } catch (e) {
    main.innerHTML = '';
    var p = document.createElement('p'); p.style.color = 'var(--red)'; p.textContent = 'Failed: ' + e.message;
    main.appendChild(p);
  }
}


function showCreateNetworkModal() {
  UI.formModal({
    title: 'Create network',
    fields: [
      { name: 'name', label: 'Network name', required: true, placeholder: 'my-network' },
      {
        name: 'driver', label: 'Driver', type: 'select',
        options: ['bridge', 'overlay', 'macvlan'].map(function(d) { return { value: d }; }),
      },
    ],
    submitLabel: 'Create',
    onSubmit: function(values) {
      return guardedAction('create-network', function() {
        return apiFetch(API + '/networks/create?name=' + encodeURIComponent(values.name) +
                        '&driver=' + values.driver, { method: 'POST' });
      }).then(function() { toast('Network created', 'success'); loadNetworks(); });
    },
  });
}


function showNetworkConnectModal(networkId, networkName) {
  // Fetch container list FIRST so we can populate the select's options;
  // modals without data are unhelpful and the modal + API call racing
  // caused occasional empty-dropdown flashes in an earlier version.
  apiFetch(API + '/containers').then(function(containers) {
    UI.formModal({
      title: 'Connect container to ' + networkName,
      fields: [
        {
          name: 'container_id', label: 'Select container', type: 'select',
          required: true,
          options: containers.map(function(c) {
            return { value: c.id, label: c.name + ' (' + c.state + ')' };
          }),
        },
      ],
      submitLabel: 'Connect',
      onSubmit: function(values) {
        return apiFetch(
          API + '/networks/' + networkId + '/connect?container_id=' + values.container_id,
          { method: 'POST' },
        ).then(function() { toast('Container connected', 'success'); loadNetworks(); });
      },
    });
  }).catch(function() { toast('Failed to load containers', 'error'); });
}
