// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * App templates page — one-click deployables. Backed by /api/templates,
 * which returns the curated catalogue from config._APP_TEMPLATES.
 *
 * Clicking a template card opens a prefilled "Run container" modal so
 * the user can confirm + tweak env vars and ports before deploy.
 * Templates whose image's registry isn't in the allowlist render greyed
 * with the reject reason on hover.
 */
"use strict";

async function showTemplates() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading templates...</div>';
  if (currentPage !== 'templates') return;
  try {
    var data = await apiFetch(API + '/templates');
    if (currentPage !== 'templates') return;
    main.innerHTML = '';

    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2'); h2.textContent = 'App templates';
    header.appendChild(h2);
    main.appendChild(header);
    var desc = document.createElement('p');
    desc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
    desc.textContent = 'Quick-start catalogue of common app images. One click prefills the Run modal with sensible ports, env vars, and named volumes.';
    main.appendChild(desc);

    var search = document.createElement('input');
    search.type = 'search';
    search.className = 'search-bar';
    search.placeholder = 'Filter templates by name or description...';
    main.appendChild(search);

    var grid = document.createElement('div');
    grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px';
    main.appendChild(grid);

    function _render(q) {
      grid.innerHTML = '';
      var needle = (q || '').toLowerCase();
      var matches = (data.templates || []).filter(function(t) {
        if (!needle) return true;
        return (t.name || '').toLowerCase().indexOf(needle) !== -1
            || (t.description || '').toLowerCase().indexOf(needle) !== -1
            || (t.category || '').toLowerCase().indexOf(needle) !== -1
            || (t.image || '').toLowerCase().indexOf(needle) !== -1;
      });
      if (!matches.length) {
        var em = document.createElement('p');
        em.style.cssText = 'color:var(--muted);font-size:13px;padding:12px';
        em.textContent = needle ? 'No templates match.' : 'No templates available.';
        grid.appendChild(em);
        return;
      }
      matches.forEach(function(tmpl) {
        var card = document.createElement('div');
        card.className = 'template-card';
        card.setAttribute('data-testid', 'template-' + tmpl.id);
        card.style.cssText = 'background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px;'
          + (tmpl.is_allowed ? 'cursor:pointer' : 'opacity:0.55;cursor:not-allowed');
        if (!tmpl.is_allowed) {
          card.title = 'Not deployable: ' + (tmpl.reject_reason || 'registry not in allowlist');
        }
        var name = document.createElement('div');
        name.style.cssText = 'font-weight:600;font-size:14px;display:flex;align-items:center;justify-content:space-between;gap:6px';
        var nn = document.createElement('span'); nn.textContent = tmpl.name;
        var cat = document.createElement('span');
        cat.style.cssText = 'font-size:10px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.04em';
        cat.textContent = tmpl.category || '';
        name.append(nn, cat);
        var img = document.createElement('div');
        img.style.cssText = 'font-family:monospace;font-size:11px;color:var(--muted)';
        img.textContent = tmpl.image;
        var d = document.createElement('p');
        d.style.cssText = 'font-size:12px;color:var(--text);margin:0';
        d.textContent = tmpl.description;
        card.append(name, img, d);
        if (tmpl.is_allowed) {
          card.onclick = function() { _deployTemplate(tmpl); };
        }
        grid.appendChild(card);
      });
    }
    search.oninput = function() { _render(search.value); };
    _render('');
  } catch (e) {
    main.innerHTML = '';
    var p = document.createElement('p'); p.style.color = 'var(--red)';
    p.textContent = 'Failed to load templates: ' + e.message;
    main.appendChild(p);
  }
}


// Build the Run-container call out of a template. We go through the
// existing showRunModal() so the user can inspect + tweak values before
// deploy — matches Portainer's "template → review → deploy" flow.
function _deployTemplate(tmpl) {
  showPage('containers');
  // Wait a tick so showRunModal can find the containers page.
  setTimeout(function() {
    showRunModal({
      image: tmpl.image,
      name: tmpl.id + '-' + Math.floor(Math.random() * 10000),
      command: tmpl.command || '',
      ports: tmpl.ports || [],
      env: tmpl.env || [],
      volumes: tmpl.volumes || [],
    });
  }, 60);
}
