// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Dashboard page — landing summary. Shows container/image/volume/network
 * totals, per-state container counts, recent daemon events, and quick-
 * action buttons for the common first-move flows (run a container, deploy
 * a template, open the wizard again).
 *
 * Backed by /api/system/overview which returns everything the dashboard
 * needs in one round-trip. Polls every 8 seconds while the page is
 * visible so counts stay live without hammering the daemon.
 */
"use strict";

async function showDashboard() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading dashboard...</div>';
  if (currentPage !== 'dashboard') return;

  function _stat(label, value, subtitle) {
    var card = document.createElement('div');
    card.className = 'stat-card';
    var v = document.createElement('div'); v.className = 'stat-value'; v.textContent = value;
    var l = document.createElement('div'); l.className = 'stat-label'; l.textContent = label;
    card.append(v, l);
    if (subtitle) {
      var s = document.createElement('div'); s.className = 'stat-subtitle'; s.textContent = subtitle;
      card.appendChild(s);
    }
    return card;
  }

  function _render(data) {
    if (currentPage !== 'dashboard') return;
    main.innerHTML = '';
    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2'); h2.textContent = 'Overview';
    header.appendChild(h2);
    main.appendChild(header);

    // Stat grid — totals for each resource plus per-state container breakdown.
    var grid = document.createElement('div');
    UI.setStyle(grid, 'display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px');
    var c = data.containers || {};
    grid.append(
      _stat('Containers', c.total || 0, (c.running || 0) + ' running, ' + (c.exited || 0) + ' exited'),
      _stat('Images', (data.images || {}).total || 0, ((data.images || {}).disk_mb || 0) + ' MB on disk'),
      _stat('Volumes', (data.volumes || {}).total || 0),
      _stat('Networks', (data.networks || {}).total || 0),
    );
    main.appendChild(grid);

    // Quick actions — the flows a new user most wants in the first 30s.
    var actH = document.createElement('h3');
    UI.setStyle(actH, 'font-size:16px;margin-bottom:12px');
    actH.textContent = 'Quick actions';
    main.appendChild(actH);
    var actBar = document.createElement('div');
    UI.setStyle(actBar, 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px');
    actBar.append(
      makeBtn('Run a container', function() { showPage('containers'); showRunModal(); }, 'btn primary'),
      makeBtn('Quick-start from template', function() { showPage('templates'); }, 'btn'),
      makeBtn('Deploy a compose stack', function() { showPage('compose'); }, 'btn'),
      makeBtn('Pull an image', function() { showPage('images'); }, 'btn'),
    );
    main.appendChild(actBar);

    // Recent events — last 5 minutes.
    var evH = document.createElement('h3');
    UI.setStyle(evH, 'font-size:16px;margin-bottom:8px');
    evH.textContent = 'Recent activity (last 5 min)';
    main.appendChild(evH);
    var evDesc = document.createElement('p');
    UI.setStyle(evDesc, 'color:var(--muted);font-size:12px;margin-bottom:8px');
    evDesc.textContent = 'Live stream of Docker daemon events. Full log on the System page.';
    main.appendChild(evDesc);
    var evBox = document.createElement('pre');
    UI.setStyle(evBox, 'background:var(--sidebar-bg);color:#e2e8f0;padding:12px;border-radius:6px;font-size:12px;max-height:240px;overflow:auto;font-family:monospace');
    var events = data.recent_events || [];
    if (!events.length) {
      evBox.textContent = '(no events in the last 5 minutes — the daemon is idle)';
    } else {
      evBox.textContent = events.slice().reverse().map(function(e) {
        var ts = e.time ? new Date(e.time * 1000).toLocaleTimeString() : '';
        return ts + ' ' + (e.type || '?') + '.' + (e.action || '?')
             + (e.actor_name ? ' [' + e.actor_name + ']' : '');
      }).join('\n');
    }
    main.appendChild(evBox);
  }

  try {
    var data = await apiFetch(API + '/system/overview');
    _render(data);
    // Refresh counts + events every 8s. Uses managedInterval so
    // showPage() tears it down when the user navigates away. Consecutive
    // failure counter: a single transient blip is silent (common for 8s
    // polling over a tunnel), but three in a row posts a sticky banner so
    // the user knows data is stale rather than fresh-but-empty.
    var _overviewFailStreak = 0;
    managedInterval(function() {
      apiFetch(API + '/system/overview').then(function(d) {
        _overviewFailStreak = 0;
        _render(d);
      }).catch(function(e) {
        _overviewFailStreak += 1;
        if (_overviewFailStreak === 3) {
          toast('Dashboard refresh failing: ' + (e && e.message ? e.message : 'network error'), 'error');
        }
      });
    }, DASHBOARD_POLL_MS);
  } catch (e) {
    main.innerHTML = '';
    var p = document.createElement('p'); UI.setStyle(p, 'color', 'var(--red)');
    p.textContent = 'Failed to load overview: ' + e.message;
    main.appendChild(p);
  }
}
