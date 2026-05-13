// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
//
// Shared status-banner module.
//
// Single entry point for every "something time-bounded is happening"
// surface in SKIFF: docker unreachable, setup window expired, session
// near expiry, WS auth lockout, rate-limited. Renders into the existing
// `#status-banner` div near the top of the page.
//
// Design:
//   - Map<key, entry> — callers identify their state by a stable key so
//     multiple concurrent states coexist (e.g. docker_unreachable +
//     ws_auth_lockout during a tunnel flap). Severity-sorted stack.
//   - Lazy 1-second interval re-renders any entries with `expiresAt` so
//     countdown text ticks without per-caller code. Interval is torn
//     down once no entry has an expiresAt.
//   - DOM-safe: message is assigned via `textContent`, never innerHTML.
//   - Accessible: error banners use role="alert"+aria-live="assertive";
//     warn/info use role="status"+aria-live="polite".
//
// Public API (on `window.statusBanner`):
//   .set(key, {severity, message, action?, expiresInMs?})
//       severity: 'error' | 'warn' | 'info'
//       message:  string — may contain `{seconds}` which will be
//                 substituted from the ticking countdown when
//                 expiresInMs is set.
//       action:   {label, onClick}  — optional inline button
//       expiresInMs: number — when set, entry auto-clears on expiry
//                    and the countdown substitutes into `{seconds}`
//   .clear(key)
//   .clearAll()
//   .get(key)           — for tests; returns the stored entry or null
//   .has(key)
(function (global) {
  var SEVERITY_ORDER = { error: 0, warn: 1, info: 2 };
  var entries = new Map();
  var tickTimer = null;

  function _now() { return Date.now(); }

  function _secondsRemaining(entry) {
    if (!entry.expiresAt) return null;
    return Math.max(0, Math.ceil((entry.expiresAt - _now()) / 1000));
  }

  function _renderMessage(entry) {
    var remaining = _secondsRemaining(entry);
    if (remaining === null) return entry.message;
    return entry.message.replace(/\{seconds\}/g, String(remaining));
  }

  function _render() {
    var container = document.getElementById('status-banner');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);
    if (entries.size === 0) {
      // CSS `.status-banner` defaults to display:none; the severity class
      // added below toggles display:block, so no inline style needed (a
      // strict `style-src 'self'` CSP blocks `.style.display = ...`).
      container.className = 'status-banner';
      return;
    }
    // Stable severity-then-insertion order.
    var sorted = Array.from(entries.entries()).sort(function (a, b) {
      return SEVERITY_ORDER[a[1].severity] - SEVERITY_ORDER[b[1].severity];
    });
    // The outer container takes the highest severity's class so existing
    // tests that inspect `.status-banner.error` keep passing.
    container.className = 'status-banner ' + sorted[0][1].severity;
    sorted.forEach(function (pair) {
      var entry = pair[1];
      var row = document.createElement('div');
      row.className = 'status-banner-item ' + entry.severity;
      row.dataset.key = pair[0];
      row.setAttribute('role', entry.severity === 'error' ? 'alert' : 'status');
      row.setAttribute('aria-live', entry.severity === 'error' ? 'assertive' : 'polite');
      var text = document.createElement('span');
      text.textContent = _renderMessage(entry);
      row.appendChild(text);
      if (entry.action && typeof entry.action.onClick === 'function') {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'status-banner-action';
        btn.textContent = entry.action.label || '';
        btn.addEventListener('click', entry.action.onClick);
        row.appendChild(btn);
      }
      container.appendChild(row);
    });
  }

  function _ensureTick() {
    if (tickTimer !== null) return;
    var anyExpires = false;
    entries.forEach(function (e) { if (e.expiresAt) anyExpires = true; });
    if (!anyExpires) return;
    tickTimer = setInterval(function () {
      var changed = false;
      entries.forEach(function (e, k) {
        if (e.expiresAt && _secondsRemaining(e) <= 0) {
          entries.delete(k);
          changed = true;
        }
      });
      _render();
      // If nothing with an expiresAt remains, tear the ticker down.
      var stillTicking = false;
      entries.forEach(function (e) { if (e.expiresAt) stillTicking = true; });
      if (!stillTicking) {
        clearInterval(tickTimer);
        tickTimer = null;
      }
      // Prevent lint warning about unused `changed`.
      void changed;
    }, 1000);
  }

  function set(key, opts) {
    if (!key || !opts || !opts.severity || !opts.message) return;
    if (!(opts.severity in SEVERITY_ORDER)) return;
    var entry = {
      severity: opts.severity,
      message: String(opts.message),
      action: opts.action || null,
      expiresAt: opts.expiresInMs ? _now() + Number(opts.expiresInMs) : null,
    };
    entries.set(key, entry);
    _render();
    _ensureTick();
  }

  function clear(key) {
    if (entries.delete(key)) _render();
  }

  function clearAll() {
    entries.clear();
    _render();
  }

  global.statusBanner = {
    set: set,
    clear: clear,
    clearAll: clearAll,
    get: function (k) { return entries.get(k) || null; },
    has: function (k) { return entries.has(k); },
    // Exported for tests only; not part of the stable API.
    _render: _render,
  };
})(window);
