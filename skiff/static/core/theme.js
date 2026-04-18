// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Theme toggle wiring.
 *
 * The pre-paint <script> in index.html already set data-theme (or left
 * it absent for "system"). This function syncs the three segmented-button
 * states and hooks up click handlers. Called once on DOMContentLoaded.
 *
 * Storage key: 'skiff_theme' — values "light" | "dark" | absent (system).
 * Writing to localStorage is best-effort; private-mode browsers that
 * block storage just lose the preference across sessions, which is
 * acceptable. NOTE: this is the ONLY legitimate use of localStorage in
 * SKIFF — everything else is sessionStorage.
 */
"use strict";

(function wireThemeToggle() {
  function apply(theme) {
    if (theme === 'light' || theme === 'dark') {
      document.documentElement.setAttribute('data-theme', theme);
      try { localStorage.setItem('skiff_theme', theme); } catch (e) { /* ignore */ }
    } else {
      document.documentElement.removeAttribute('data-theme');
      try { localStorage.removeItem('skiff_theme'); } catch (e) { /* ignore */ }
    }
    syncPressed();
  }
  function currentPref() {
    try { return localStorage.getItem('skiff_theme') || ''; } catch (e) { return ''; }
  }
  function syncPressed() {
    var pref = currentPref();
    document.querySelectorAll('.theme-toggle button[data-theme-value]').forEach(function(b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-theme-value') === pref ? 'true' : 'false');
    });
  }
  function init() {
    document.querySelectorAll('.theme-toggle button[data-theme-value]').forEach(function(b) {
      b.addEventListener('click', function() { apply(b.getAttribute('data-theme-value')); });
    });
    syncPressed();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
