// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Command palette (⌘K / Ctrl-K).
 *
 * Global keyboard-driven action launcher. Modeled on VSCode / Figma
 * command palette: press ⌘K (macOS) or Ctrl-K (Linux/Windows), type any
 * fragment, arrow-key through results, Enter to run.
 *
 * Sources of commands:
 *   1. Static navigation — go to each sidebar page
 *   2. Running containers — "Stop <name>", "Restart <name>", "Logs <name>",
 *      "Inspect <name>", "Terminal in <name>"
 *   3. Actions — "Run new container"
 *
 * Container names are fetched on palette-open (not constantly) to avoid
 * holding stale state.
 *
 * Security: every action invokes the same apiFetch paths the sidebar
 * buttons use — no new surface. Values rendered via textContent; no HTML
 * injection even if a container name contains special characters.
 *
 * Dependencies: global functions `apiFetch`, `showPage`, `showRunModal`,
 * `showDetail`, `loadContainers`, `toast`, and the `UI` namespace. These
 * live in `app.js` today; future refactor moves them to `window.SKIFF.*`.
 */
"use strict";

(function wireCommandPalette() {
  var modalEl = null, inputEl = null, resultsEl = null, activeIdx = 0, currentItems = [];
  var API = '/api';

  function isMacLike() {
    return navigator.platform && /Mac|iPhone|iPod|iPad/i.test(navigator.platform);
  }

  function formatShortcut() {
    return isMacLike() ? '\u2318K' : 'Ctrl+K';
  }

  // Localise the sidebar hint to the user's platform so the visible
  // shortcut matches what they should actually press. Runs after DOM is
  // ready — the hint is rendered server-side with the Ctrl+K fallback.
  function _paintSidebarHint() {
    var hintKbd = document.querySelector('#sidebar-palette-hint kbd');
    if (hintKbd) hintKbd.textContent = formatShortcut();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _paintSidebarHint);
  } else {
    _paintSidebarHint();
  }

  async function buildItems() {
    var items = [];
    [
      { label: 'Go to Containers', page: 'containers' },
      { label: 'Go to Images',     page: 'images' },
      { label: 'Go to Volumes',    page: 'volumes' },
      { label: 'Go to Networks',   page: 'networks' },
      { label: 'Go to Compose',    page: 'compose' },
      { label: 'Go to System',     page: 'system' },
    ].forEach(function(nav) {
      items.push({
        label: nav.label, hint: 'Navigation',
        run: function() { showPage(nav.page); },
      });
    });
    items.push({
      label: 'Run new container', hint: 'Action',
      run: function() { showPage('containers'); setTimeout(function() { showRunModal(); }, 80); },
    });
    try {
      var containers = await apiFetch(API + '/containers');
      (containers || []).forEach(function(c) {
        var name = c.name, id = c.id, state = (c.state || c.status || '').toLowerCase();
        items.push({ label: 'Inspect ' + name, hint: 'Container \u00b7 ' + state,
          run: function() { showDetail(id, name, 'inspect'); } });
        items.push({ label: 'Logs ' + name, hint: 'Container \u00b7 ' + state,
          run: function() { showDetail(id, name, 'logs'); } });
        if (state === 'running') {
          items.push({ label: 'Stop ' + name, hint: 'Container \u00b7 running',
            run: function() {
              apiFetch(API + '/containers/' + id + '/stop', { method: 'POST' })
                .then(function() { toast(name + ' stopped', 'info'); loadContainers(); });
            } });
          items.push({ label: 'Restart ' + name, hint: 'Container \u00b7 running',
            run: function() {
              apiFetch(API + '/containers/' + id + '/restart', { method: 'POST' })
                .then(function() { toast(name + ' restarted', 'success'); loadContainers(); });
            } });
          items.push({ label: 'Terminal in ' + name, hint: 'Container \u00b7 running',
            run: function() { showDetail(id, name, 'terminal'); } });
        } else {
          items.push({ label: 'Start ' + name, hint: 'Container \u00b7 ' + state,
            run: function() {
              apiFetch(API + '/containers/' + id + '/start', { method: 'POST' })
                .then(function() { toast(name + ' started', 'success'); loadContainers(); });
            } });
        }
      });
    } catch (e) { /* unauthenticated or Docker down — just navigation */ }
    return items;
  }

  function matches(label, query) {
    if (!query) return true;
    var i = 0, q = query.toLowerCase(), l = label.toLowerCase();
    for (var c = 0; c < l.length && i < q.length; c++) {
      if (l[c] === q[i]) i++;
    }
    return i === q.length;
  }

  function render(items, query) {
    while (resultsEl.firstChild) resultsEl.removeChild(resultsEl.firstChild);
    currentItems = items.filter(function(it) { return matches(it.label, query); }).slice(0, 30);
    if (!currentItems.length) {
      resultsEl.appendChild(UI.el('div', {
        style: 'padding:12px;color:var(--muted);font-size:13px',
        text: 'No results for "' + query + '"',
      }));
      return;
    }
    currentItems.forEach(function(it, idx) {
      var row = UI.el('div', {
        class: idx === activeIdx ? 'cmdp-row active' : 'cmdp-row',
        style: 'padding:8px 14px;cursor:pointer;border-left:3px solid '
             + (idx === activeIdx ? 'var(--accent)' : 'transparent')
             + ';background:' + (idx === activeIdx ? 'var(--card-hover)' : 'transparent')
             + ';display:flex;justify-content:space-between;align-items:center;gap:12px',
        on: {
          mouseenter: function() { activeIdx = idx; render(items, query); },
          click: function() { activate(); },
        },
      },
        UI.el('span', { style: 'color:var(--text)', text: it.label }),
        UI.el('span', { style: 'font-size:11px;color:var(--muted);white-space:nowrap', text: it.hint }),
      );
      resultsEl.appendChild(row);
    });
  }

  function activate() {
    var it = currentItems[activeIdx];
    close();
    if (it) try { it.run(); } catch (e) { console.error(e); }
  }

  function close() {
    if (modalEl && modalEl.parentNode) modalEl.parentNode.removeChild(modalEl);
    modalEl = inputEl = resultsEl = null;
    currentItems = [];
    activeIdx = 0;
  }

  async function open() {
    if (modalEl) return;
    var items = await buildItems();
    activeIdx = 0;
    inputEl = UI.el('input', {
      type: 'text',
      placeholder: 'Type to filter — arrow keys to move, Enter to run (' + formatShortcut() + ' to open)',
      style: 'width:100%;padding:14px 18px;font-size:15px;border:0;background:transparent;'
           + 'color:var(--text);outline:none;font-family:inherit',
      on: {
        input: function() { activeIdx = 0; render(items, inputEl.value); },
        keydown: function(e) {
          if (e.key === 'ArrowDown') { activeIdx = Math.min(activeIdx + 1, currentItems.length - 1); render(items, inputEl.value); e.preventDefault(); }
          else if (e.key === 'ArrowUp') { activeIdx = Math.max(activeIdx - 1, 0); render(items, inputEl.value); e.preventDefault(); }
          else if (e.key === 'Enter') { activate(); e.preventDefault(); }
          else if (e.key === 'Escape') { close(); e.preventDefault(); }
        },
      },
    });
    resultsEl = UI.el('div', {
      style: 'max-height:50vh;overflow-y:auto;border-top:1px solid var(--border-subtle)',
    });
    var box = UI.el('div', {
      class: 'modal',
      style: 'width:560px;max-height:70vh;padding:0;overflow:hidden;display:flex;flex-direction:column',
    }, inputEl, resultsEl);
    modalEl = UI.el('div', {
      class: 'modal-bg', style: 'align-items:flex-start;padding-top:10vh',
      on: { click: function(e) { if (e.target === modalEl) close(); } },
    }, box);
    document.body.appendChild(modalEl);
    render(items, '');
    setTimeout(function() { if (inputEl) inputEl.focus(); }, 10);
  }

  window.addEventListener('keydown', function(e) {
    var isMeta = isMacLike() ? e.metaKey : e.ctrlKey;
    if (isMeta && !e.shiftKey && !e.altKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (modalEl) close(); else open();
    }
  });
})();
