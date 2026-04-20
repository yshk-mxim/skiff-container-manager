// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Sidebar renderer.
 *
 * Sidebar links are generated from the page registry (UI.registerPage
 * declarations from each pages/*.js file) rather than hardcoded in
 * index.html. Benefits:
 *
 *   1. Adding a page is a single edit in the page's own JS file —
 *      the sidebar updates automatically.
 *   2. Persona filtering is free: UI.getPages(persona) hides
 *      pages whose `personas` list doesn't include the active one.
 *   3. Reordering pages = one `order: NN` change per page, not an
 *      HTML shuffle.
 *
 * Icon inventory lives inline below as inline SVG strings — kept in
 * the codebase (not fetched) so we stay CSP-strict. Adding an icon =
 * add an entry to `ICONS` keyed by page id.
 *
 * Loaded AFTER app.js so that showPage() / currentPage are defined
 * when the click handlers fire. The mount happens on DOMContentLoaded
 * (deferred until page modules have had a chance to register).
 */
"use strict";

(function mountSidebar() {
  // Small icon registry. Each value is the inside of an <svg> — wrapped
  // with width=16, height=16, viewBox=0 0 16 16, fill=currentColor.
  // When adding a new page, add a corresponding icon here with the same id.
  var ICONS = {
    dashboard:  '<path d="M2 2h5v7H2V2zm7 0h5v4H9V2zm0 6h5v7H9V8zm-7 3h5v4H2v-4z"/>',
    containers: '<path d="M2.5 2A1.5 1.5 0 001 3.5v9A1.5 1.5 0 002.5 14h11a1.5 1.5 0 001.5-1.5v-9A1.5 1.5 0 0013.5 2h-11zM2 3.5a.5.5 0 01.5-.5h11a.5.5 0 01.5.5v9a.5.5 0 01-.5.5h-11a.5.5 0 01-.5-.5v-9z"/><path d="M4 6h2v1H4V6zm3 0h2v1H7V6zm3 0h2v1h-2V6zM4 8h2v1H4V8zm3 0h2v1H7V8z"/>',
    images:     '<path d="M1 3.5A1.5 1.5 0 012.5 2h11A1.5 1.5 0 0115 3.5v9a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 011 12.5v-9z"/>',
    templates:  '<path d="M3 2h4v4H3V2zm6 0h4v4H9V2zM3 8h4v4H3V8zm6 2h4a1 1 0 110 2h-2v2h-2v-2H9v-2z"/>',
    volumes:    '<path d="M8 1C4.5 1 2 2.1 2 3.5v9C2 13.9 4.5 15 8 15s6-1.1 6-2.5v-9C14 2.1 11.5 1 8 1z"/>',
    networks:   '<path d="M8 1a2 2 0 012 2 2 2 0 01-1 1.73V6h3a2 2 0 012 2v.27A2 2 0 0115 10a2 2 0 01-2 2 2 2 0 01-1.73-1H10v1.27A2 2 0 0111 14a2 2 0 01-2 2 2 2 0 01-2-2 2 2 0 011-1.73V11H6.73A2 2 0 015 12a2 2 0 01-2-2 2 2 0 011-1.73V8a2 2 0 012-2h3V4.73A2 2 0 018 3a2 2 0 010-2z"/>',
    compose:    '<path d="M3.5 1A1.5 1.5 0 002 2.5v11A1.5 1.5 0 003.5 15h9a1.5 1.5 0 001.5-1.5v-8L9.5 1h-6z"/>',
    system:     '<path d="M8 4.754a3.246 3.246 0 100 6.492 3.246 3.246 0 000-6.492z"/><path d="M9.796 1.343c-.527-1.79-3.065-1.79-3.592 0l-.094.319a.873.873 0 01-1.255.52l-.292-.16c-1.64-.892-3.433.902-2.54 2.541l.159.292a.873.873 0 01-.52 1.255l-.319.094c-1.79.527-1.79 3.065 0 3.592l.319.094a.873.873 0 01.52 1.255l-.16.292c-.892 1.64.902 3.434 2.541 2.54l.292-.159a.873.873 0 011.255.52l.094.319c.527 1.79 3.065 1.79 3.592 0l.094-.319a.873.873 0 011.255-.52l.292.16c1.64.893 3.434-.902 2.54-2.541l-.159-.292a.873.873 0 01.52-1.255l.319-.094c1.79-.527 1.79-3.065 0-3.592l-.319-.094a.873.873 0 01-.52-1.255l.16-.292c.893-1.64-.902-3.433-2.541-2.54l-.292.159a.873.873 0 01-1.255-.52l-.094-.319z"/>',
    settings:   '<path d="M2 3h12v2H2V3zm0 4h12v2H2V7zm0 4h12v2H2v-2z"/><circle cx="4" cy="4" r="1" fill="white"/><circle cx="10" cy="8" r="1" fill="white"/><circle cx="6" cy="12" r="1" fill="white"/>',
  };

  // Persona source: /api/config returns `profile` for authed clients.
  // For the unauthenticated first paint (showLogin branch) render every
  // page; the login form is the only interactive surface anyway. Once the
  // user signs in, app.js calls renderSidebar() again with the profile.
  function iconSvg(id) {
    if (!ICONS[id]) return '';
    return '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">'
         + ICONS[id] + '</svg>';
  }

  function renderSidebar(persona) {
    var mount = document.getElementById('sidebar-mount');
    if (!mount) return;  // no sidebar on this page (e.g. setup wizard)
    // Clear and rebuild — idempotent so persona changes work too.
    while (mount.firstChild) mount.removeChild(mount.firstChild);
    var pages = (window.UI && UI.getPages) ? UI.getPages(persona) : [];
    if (!pages.length) {
      // Registry not yet populated — page modules hadn't parsed yet at
      // our script-load moment. Schedule one retry after microtasks settle.
      setTimeout(function() { renderSidebar(persona); }, 0);
      return;
    }
    pages.forEach(function(p, idx) {
      var a = document.createElement('a');
      a.setAttribute('data-page', p.id);
      if (idx === 0) a.className = 'active';  // default first page highlighted
      // Icon (safe — ICONS is a static allowlist; no user data)
      a.innerHTML = iconSvg(p.id);
      // Label — textContent keeps safety regardless of label source
      var label = document.createTextNode(p.label || p.id);
      a.appendChild(label);
      a.addEventListener('click', function() {
        if (typeof showPage === 'function') showPage(p.id);
      });
      mount.appendChild(a);
    });
  }

  // Expose so app.js can call after login (persona becomes known then).
  window.renderSidebar = renderSidebar;

  // First render runs as soon as the page module scripts have had a chance
  // to call UI.registerPage. That happens synchronously after their script
  // tags parse, which is before DOMContentLoaded completes.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { renderSidebar(null); });
  } else {
    renderSidebar(null);
  }
})();
