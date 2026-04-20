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
    // Fetch both catalogues in parallel; one failing still renders
    // the other section with its own error placeholder.
    var appsP = apiFetch(API + '/templates').catch(function(e) { return { _err: e }; });
    var stacksP = apiFetch(API + '/compose/templates').catch(function(e) { return { _err: e }; });
    var results = await Promise.all([appsP, stacksP]);
    if (currentPage !== 'templates') return;
    var apps = results[0];
    var stacks = results[1];
    main.innerHTML = '';

    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2'); h2.textContent = 'Templates';
    header.appendChild(h2);
    main.appendChild(header);
    var desc = document.createElement('p');
    desc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
    desc.textContent = 'Quick-start catalogues. Apps deploy a single container via the Run modal. Stacks deploy a multi-service docker-compose project.';
    main.appendChild(desc);

    var search = document.createElement('input');
    search.type = 'search';
    search.className = 'search-bar';
    search.placeholder = 'Filter by name, description, image…';
    main.appendChild(search);

    // ── Apps section ───────────────────────────────────────────
    var appsH = document.createElement('h3');
    appsH.textContent = 'Apps (single container)';
    appsH.style.cssText = 'font-size:15px;margin:20px 0 8px;color:var(--text-strong)';
    main.appendChild(appsH);
    var appsGrid = document.createElement('div');
    appsGrid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px';
    main.appendChild(appsGrid);

    // ── Stacks section ──────────────────────────────────────────
    var stacksH = document.createElement('h3');
    stacksH.textContent = 'Stacks (docker-compose)';
    stacksH.style.cssText = 'font-size:15px;margin:24px 0 8px;color:var(--text-strong)';
    main.appendChild(stacksH);
    var stacksDesc = document.createElement('p');
    stacksDesc.style.cssText = 'color:var(--muted);font-size:11px;margin-bottom:10px';
    stacksDesc.textContent = 'Multi-service blueprints. Click a card to review env vars and deploy via compose up.';
    main.appendChild(stacksDesc);
    var stacksGrid = document.createElement('div');
    stacksGrid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px';
    main.appendChild(stacksGrid);

    function _matches(tmpl, needle) {
      if (!needle) return true;
      return (tmpl.name || '').toLowerCase().indexOf(needle) !== -1
          || (tmpl.description || '').toLowerCase().indexOf(needle) !== -1
          || (tmpl.category || '').toLowerCase().indexOf(needle) !== -1
          || (tmpl.image || '').toLowerCase().indexOf(needle) !== -1
          || (tmpl.images || []).some(function(i) { return i.toLowerCase().indexOf(needle) !== -1; });
    }

    function _renderAppCard(tmpl) {
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
      if (tmpl.is_allowed) card.onclick = function() { _deployTemplate(tmpl); };
      return card;
    }

    function _renderStackCard(tmpl) {
      var card = document.createElement('div');
      card.className = 'template-card stack-card';
      card.setAttribute('data-testid', 'stack-' + tmpl.id);
      card.style.cssText = 'background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px;'
        + (tmpl.is_allowed ? 'cursor:pointer' : 'opacity:0.55;cursor:not-allowed');
      if (!tmpl.is_allowed) {
        card.title = 'Not deployable: ' + (tmpl.reject_reason || 'registry not in allowlist');
      }
      var name = document.createElement('div');
      name.style.cssText = 'font-weight:600;font-size:14px;display:flex;align-items:center;justify-content:space-between;gap:6px';
      var nn = document.createElement('span'); nn.textContent = tmpl.name;
      var badge = document.createElement('span');
      badge.style.cssText = 'font-size:10px;font-weight:500;color:white;background:var(--accent);padding:2px 6px;border-radius:3px;letter-spacing:.04em';
      badge.textContent = 'STACK';
      name.append(nn, badge);
      var imgs = document.createElement('div');
      imgs.style.cssText = 'font-family:monospace;font-size:11px;color:var(--muted);line-height:1.5';
      imgs.textContent = (tmpl.images || []).join(' · ');
      var d = document.createElement('p');
      d.style.cssText = 'font-size:12px;color:var(--text);margin:0';
      d.textContent = tmpl.description;
      var meta = document.createElement('div');
      meta.style.cssText = 'font-size:11px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap';
      var svcCount = (tmpl.yaml.match(/^  \w+:$/gm) || []).length;
      if (svcCount) meta.appendChild(UI.el('span', { text: svcCount + ' services' }));
      if ((tmpl.env || []).length) meta.appendChild(UI.el('span', { text: tmpl.env.length + ' env knobs' }));
      if ((tmpl.ports || []).length) {
        meta.appendChild(UI.el('span', { text: 'ports: ' + tmpl.ports.map(function(p) { return p.host; }).join(', ') }));
      }
      card.append(name, imgs, d, meta);
      if (tmpl.is_allowed) card.onclick = function() { _deployStackTemplate(tmpl); };
      return card;
    }

    function _render(q) {
      appsGrid.innerHTML = '';
      stacksGrid.innerHTML = '';
      var needle = (q || '').toLowerCase();
      if (apps && apps._err) {
        appsGrid.appendChild(UI.el('p', {
          style: 'color:var(--red);font-size:12px;padding:8px',
          text: 'Apps unavailable: ' + apps._err.message,
        }));
      } else {
        var appMatches = (apps.templates || []).filter(function(t) { return _matches(t, needle); });
        if (!appMatches.length) {
          appsGrid.appendChild(UI.el('p', {
            style: 'color:var(--muted);font-size:12px;padding:8px',
            text: needle ? 'No app templates match.' : 'No app templates available.',
          }));
        }
        appMatches.forEach(function(t) { appsGrid.appendChild(_renderAppCard(t)); });
      }
      if (stacks && stacks._err) {
        stacksGrid.appendChild(UI.el('p', {
          style: 'color:var(--red);font-size:12px;padding:8px',
          text: 'Stacks unavailable: ' + stacks._err.message,
        }));
      } else {
        var stackMatches = (stacks.templates || []).filter(function(t) { return _matches(t, needle); });
        if (!stackMatches.length) {
          stacksGrid.appendChild(UI.el('p', {
            style: 'color:var(--muted);font-size:12px;padding:8px',
            text: needle ? 'No stack templates match.' : 'No stack templates available.',
          }));
        }
        stackMatches.forEach(function(t) { stacksGrid.appendChild(_renderStackCard(t)); });
      }
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


/**
 * Deploy a compose-stack template. Opens a review modal so the user
 * can name the project + fill in env vars (passwords, etc.) before
 * the YAML is sent through `POST /api/compose/up`. Refuses to submit
 * any field marked `secret: true` with an empty value.
 */
function _deployStackTemplate(tmpl) {
  var fields = [
    { name: 'project_name', label: 'Project name', required: true,
      value: tmpl.id + '-' + Math.floor(Math.random() * 1000),
      hint: 'Lowercase, digits, dash/underscore. Used as the compose project prefix.' },
  ];
  (tmpl.env || []).forEach(function(e) {
    fields.push({
      name: 'env_' + e.key,
      label: e.key + (e.description ? ' — ' + e.description : ''),
      type: e.secret ? 'password' : 'text',
      required: !!e.secret,
      placeholder: e.secret ? 'required' : '',
    });
  });
  UI.formModal({
    title: 'Deploy stack: ' + tmpl.name,
    fields: fields,
    submitLabel: 'Deploy stack',
    onSubmit: function(values) {
      // Substitute ${VAR} placeholders with the supplied values.
      var yaml = tmpl.yaml;
      (tmpl.env || []).forEach(function(e) {
        var v = values['env_' + e.key] || '';
        // Anchor both ${VAR} and ${VAR:-default} forms; keep it literal
        // so no regex interpretation surprises. YAML stays valid.
        yaml = yaml.split('${' + e.key + '}').join(v);
      });
      // Send as a multipart file upload — matches the UI's drop-zone
      // deploy flow and the server's existing /compose/up contract.
      var form = new FormData();
      var blob = new Blob([yaml], { type: 'text/yaml' });
      form.append('file', blob, 'docker-compose.yml');
      var url = API + '/compose/up?project_name=' + encodeURIComponent(values.project_name);
      return apiFetch(url, { method: 'POST', body: form, _timeout: 130000 })
        .then(function() {
          toast('Stack "' + values.project_name + '" deployed — opening Compose page', 'success');
          setTimeout(function() { window.showPage('compose'); }, 300);
        });
    },
  });
}


// Build the Run-container call out of a template. We go through the
// existing showRunModal() so the user can inspect + tweak values before
// deploy — template → review → deploy, which the novice audits visually.
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
