// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * First-run tour. Fires once after wizard completion (detected via
 * localStorage `skiff.tour.done`). 4 cards walk through: sidebar,
 * command palette, dashboard stats, and exit.
 *
 * Skippable with Esc or the Skip button. Dismissing marks it done so
 * subsequent sessions don't see it.
 */
"use strict";

(function() {
  if (window._tourInstalled) return;
  window._tourInstalled = true;

  var STORAGE_KEY = 'skiff.tour.done';
  var steps = [
    {
      title: 'Welcome to SKIFF',
      body: 'This is a quick 30-second tour. Press Esc to skip at any time.',
    },
    {
      title: 'Sidebar navigation',
      body: 'All resources live in the sidebar: Dashboard (this page), Containers, Images, Templates, Volumes, Networks, Compose, System. Each page has its own search bar at the top.',
    },
    {
      title: 'Command palette',
      body: 'Press ⌘K (macOS) or Ctrl-K (everywhere else) to jump to any page or container by name. It works from anywhere in the app.',
    },
    {
      title: 'You are set',
      body: 'Click "Quick-start from template" below to deploy nginx, postgres, or a dev shell in one click. Welcome aboard.',
    },
  ];

  function _maybeStart() {
    // Don't fire inside the wizard itself (wizard sets document.body class or
    // the UI shows the setup form before the sidebar renders). Wait until the
    // sidebar appears, meaning the user is signed in.
    if (!document.querySelector('.sidebar')) return;
    try {
      if (localStorage.getItem(STORAGE_KEY)) return;
    } catch (e) { return; }
    _render(0);
  }

  function _render(idx) {
    var existing = document.querySelector('.tour-overlay');
    if (existing) existing.remove();
    if (idx >= steps.length) {
      _markDone();
      return;
    }
    var ov = document.createElement('div');
    ov.className = 'tour-overlay';
    ov.setAttribute('data-testid', 'tour-overlay');
    var card = document.createElement('div'); card.className = 'tour-card';
    var h = document.createElement('h3'); h.style.cssText = 'font-size:18px;margin:0 0 8px'; h.textContent = steps[idx].title;
    var p = document.createElement('p'); p.style.cssText = 'color:var(--muted);font-size:13px;margin-bottom:18px'; p.textContent = steps[idx].body;
    var bar = document.createElement('div'); bar.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;align-items:center';
    var progress = document.createElement('span');
    progress.style.cssText = 'font-size:11px;color:var(--muted);margin-right:auto';
    progress.textContent = (idx + 1) + ' / ' + steps.length;
    var skip = document.createElement('button'); skip.className = 'btn'; skip.textContent = 'Skip';
    skip.onclick = function() { _markDone(); ov.remove(); };
    var next = document.createElement('button'); next.className = 'btn primary';
    next.textContent = idx === steps.length - 1 ? 'Done' : 'Next';
    next.onclick = function() { _render(idx + 1); };
    bar.append(progress, skip, next);
    card.append(h, p, bar);
    ov.appendChild(card);
    document.body.appendChild(ov);
    // Esc → skip.
    var _esc = function(e) {
      if (e.key === 'Escape') {
        _markDone();
        if (ov.parentNode) ov.remove();
        document.removeEventListener('keydown', _esc);
      }
    };
    document.addEventListener('keydown', _esc);
    next.focus();
  }

  function _markDone() {
    try { localStorage.setItem(STORAGE_KEY, '1'); } catch (e) {}
  }

  // Wait for the sidebar to render. Poll every 500ms for up to 10s.
  var attempts = 0;
  var pollId = setInterval(function() {
    attempts++;
    if (document.querySelector('.sidebar')) {
      clearInterval(pollId);
      setTimeout(_maybeStart, 400);  // let first-page render complete
    } else if (attempts > 20) {
      clearInterval(pollId);
    }
  }, 500);

  // Exposed for the "?" help cheatsheet in case we add a "restart tour" action.
  window._tour = { start: function() { try { localStorage.removeItem(STORAGE_KEY); } catch (e) {} _render(0); } };
})();
