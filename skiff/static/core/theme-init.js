// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
//
// Runs before first paint to avoid flash-of-wrong-theme. Kept same-origin
// (not inline in index.html) so the app's strict CSP `script-src 'self'`
// passes without an `unsafe-inline` escape or a SHA-hash allowlist.
//
// Cycle handled by core/theme.js at click time: system (no attr) → light
// → dark → back to system. This init file only applies the persisted
// choice, if any.
(function () {
  try {
    var pref = localStorage.getItem('skiff_theme');
    if (pref === 'light' || pref === 'dark') {
      document.documentElement.setAttribute('data-theme', pref);
    }
    // else: leave attribute absent so the prefers-color-scheme rule kicks in
  } catch (e) {
    // localStorage can be blocked (file://, private mode) — ignore
  }
})();
