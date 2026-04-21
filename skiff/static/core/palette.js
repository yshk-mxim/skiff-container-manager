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
      { label: 'Go to Dashboard',  page: 'dashboard' },
      { label: 'Go to Containers', page: 'containers' },
      { label: 'Go to Images',     page: 'images' },
      { label: 'Go to Templates',  page: 'templates' },
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
        // _apiAction: palette commands run OUTSIDE a button's click handler,
        // so there's no makeActionBtn wrapper to translate thrown envelope
        // messages into toasts. Wire the rejection path here so the user
        // sees WHY a stop/start/restart refused (e.g. "container already
        // stopped", "would leave rootless uid mapping invalid").
        var _apiAction = function(url, verb, tone) {
          apiFetch(url, { method: 'POST' })
            .then(function() { toast(name + ' ' + verb, tone || 'info'); loadContainers(); })
            .catch(function(e) { toast((e && e.message) ? e.message : (verb + ' failed'), 'error'); });
        };
        if (state === 'running') {
          items.push({ label: 'Stop ' + name, hint: 'Container \u00b7 running',
            run: function() { _apiAction(API + '/containers/' + id + '/stop', 'stopped', 'info'); } });
          items.push({ label: 'Restart ' + name, hint: 'Container \u00b7 running',
            run: function() { _apiAction(API + '/containers/' + id + '/restart', 'restarted', 'success'); } });
          items.push({ label: 'Terminal in ' + name, hint: 'Container \u00b7 running',
            run: function() { showDetail(id, name, 'terminal'); } });
        } else {
          items.push({ label: 'Start ' + name, hint: 'Container \u00b7 ' + state,
            run: function() { _apiAction(API + '/containers/' + id + '/start', 'started', 'success'); } });
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

  // Number-key navigation: 1-9 jumps to the corresponding sidebar
  // section. `0` is the 10th slot and opens API docs in a new tab
  // (external link — see the api-docs page registration in app.js).
  // Ordered to match the visible sidebar order. Adding a new page?
  // Register it in the page module with a sidebar `order` value, then
  // add it here so the number key works — the two lists are
  // intentionally decoupled so a persona-filtered page (System is
  // hidden under the homelab persona) still keeps a stable number.
  var NUMBER_NAV = [
    null, 'dashboard', 'containers', 'images', 'templates',
    'volumes', 'networks', 'compose', 'system', 'settings',
  ];
  var NUMBER_ZERO_URL = '/api/docs';  // keyboard '0' opens this in a new tab

  window.addEventListener('keydown', function(e) {
    var isMeta = isMacLike() ? e.metaKey : e.ctrlKey;
    if (isMeta && !e.shiftKey && !e.altKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (modalEl) close(); else open();
      return;
    }
    var t = e.target;
    var editable = t && (
      t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
      t.isContentEditable
    );
    // `?` → shortcuts help. Ignored inside editable fields so it doesn't
    // hijack the character when the user is typing in a form.
    if (e.key === '?' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (!editable) {
        e.preventDefault();
        _openShortcutsHelp();
      }
      return;
    }
    // `1`-`9` → jump to sidebar section. `0` opens API docs.
    // Same editable-field guard so typing a number in a form stays a number.
    if (!editable && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      if (e.key === '0') {
        e.preventDefault();
        window.open(NUMBER_ZERO_URL, '_blank', 'noopener');
        return;
      }
      var idx = parseInt(e.key, 10);
      if (idx >= 1 && idx <= 9 && NUMBER_NAV[idx]) {
        if (typeof window.showPage === 'function') {
          e.preventDefault();
          window.showPage(NUMBER_NAV[idx]);
          return;
        }
      }
    }
    // `r` on the Containers page → open Run-container modal.
    if (!editable && e.key === 'r' && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      if (window.currentPage === 'containers' && typeof window.showRunModal === 'function') {
        e.preventDefault();
        window.showRunModal();
        return;
      }
    }
    // `/` → focus the nearest search input on the current page.
    if (!editable && e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      var search = document.querySelector(
        'input[type="search"], input[placeholder*="earch" i], input[placeholder*="ilter" i]',
      );
      if (search) {
        e.preventDefault();
        search.focus();
      }
    }
  });

  function _openShortcutsHelp() {
    if (document.querySelector('.shortcuts-help')) return;
    var bg = document.createElement('div');
    bg.className = 'modal-bg shortcuts-help';
    bg.onclick = function(ev) { if (ev.target === bg) bg.remove(); };
    var box = document.createElement('div'); box.className = 'modal';
    box.style.maxWidth = '480px';
    var h3 = document.createElement('h3'); h3.textContent = 'Keyboard shortcuts';
    box.appendChild(h3);
    var dl = document.createElement('dl');
    dl.style.cssText = 'display:grid;grid-template-columns:auto 1fr;gap:8px 16px;margin:12px 0 8px';
    [
      [formatShortcut(), 'Command palette — jump to any page or container'],
      ['?', 'This shortcut help'],
      ['1 – 9', 'Jump to sidebar section (Dashboard, Containers, Images, Templates, Volumes, Networks, Compose, System, Settings)'],
      ['0', 'Open API docs (Swagger UI) in a new tab'],
      ['r', 'Open Run-container modal (on the Containers page)'],
      ['/', 'Focus the nearest search / filter input'],
      ['Esc', 'Close modal / palette / overlay'],
      ['Tab', 'Next interactive element (visible focus ring)'],
      ['\u2191 / \u2193', 'Move within a palette or list'],
      ['Enter', 'Submit modal form or run selected palette item'],
    ].forEach(function(row) {
      var dt = document.createElement('dt');
      dt.style.cssText = 'font-family:monospace;background:var(--card);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-size:12px;';
      dt.textContent = row[0];
      var dd = document.createElement('dd');
      dd.style.cssText = 'font-size:13px;color:var(--text);margin:0';
      dd.textContent = row[1];
      dl.append(dt, dd);
    });
    box.appendChild(dl);
    var note = document.createElement('p');
    note.style.cssText = 'font-size:11px;color:var(--muted);margin-top:8px';
    note.textContent = 'The Terminal tab also honours xterm.js shortcuts (Ctrl-C, arrow-key history, Tab completion) — those run inside the PTY, not the browser.';
    box.appendChild(note);
    // Replay first-run tour. Exposed here (not in the sidebar) because
    // the tour is a one-time walkthrough: a permanent sidebar entry
    // would invite accidental re-runs, while the ? modal is a
    // deliberate "help me" surface where a restart button reads as
    // discoverable help.
    var tourRow = document.createElement('div');
    tourRow.style.cssText = 'margin-top:14px;padding-top:12px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:10px';
    var tourLabel = document.createElement('span');
    tourLabel.style.cssText = 'font-size:12px;color:var(--muted)';
    tourLabel.textContent = 'Want to see the 4-step guided tour again?';
    var tourBtn = document.createElement('button');
    tourBtn.className = 'btn small';
    tourBtn.textContent = 'Replay first-run tour';
    tourBtn.onclick = function() {
      bg.remove();
      if (window._tour && typeof window._tour.start === 'function') window._tour.start();
    };
    tourRow.append(tourLabel, tourBtn);
    box.appendChild(tourRow);
    bg.appendChild(box);
    document.body.appendChild(bg);
    // Escape closes the help modal. Listener is removed when the modal
    // is dismissed so a second help modal's handler doesn't fire on the
    // first's Escape. Matches the "Esc closes modals" shortcut the help
    // itself documents.
    function _handleEsc(ev) {
      if (ev.key === 'Escape') {
        document.removeEventListener('keydown', _handleEsc, true);
        if (bg.parentNode) bg.remove();
      }
    }
    document.addEventListener('keydown', _handleEsc, true);
  }
})();
