// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Notifications bell — persistent history of toasts.
 *
 * Toasts are ephemeral (fade after 3-5s). Power users miss them when
 * they're on the other tab or looking at a modal. This module intercepts
 * the `toast(...)` global, renders a floating bell with an unread count,
 * and on click opens a panel with the last N notifications.
 *
 * Ring buffer only — no server persistence, cleared on page reload.
 */
"use strict";

(function() {
  if (window._notifRingInstalled) return;
  window._notifRingInstalled = true;

  var CAP = 50;
  var ring = [];
  var unread = 0;
  var bellEl = null;
  var countEl = null;
  var panelEl = null;

  function _install() {
    if (bellEl) return;
    bellEl = document.createElement('div');
    bellEl.className = 'notif-bell';
    bellEl.setAttribute('role', 'button');
    bellEl.setAttribute('aria-label', 'Notifications');
    bellEl.setAttribute('data-testid', 'notif-bell');
    // Belt-and-braces against stale cached CSS: even if the browser
    // has an older styles.css where .notif-bell was `position: fixed;
    // top: 12px; right: 18px;`, these inline styles win via specificity
    // so the bell still renders inline in the sidebar for users who
    // haven't hard-refreshed.
    bellEl.style.position = 'relative';
    bellEl.style.top = 'auto';
    bellEl.style.right = 'auto';
    bellEl.style.marginLeft = 'auto';
    bellEl.textContent = '\ud83d\udd14';
    countEl = document.createElement('span');
    countEl.className = 'notif-count';
    countEl.style.display = 'none';
    bellEl.appendChild(countEl);
    bellEl.onclick = function(ev) { ev.stopPropagation(); _togglePanel(); };
    // Mount into the sidebar-status row so the bell sits next to the
    // "Connected" indicator — same screen region users already look at
    // for app state. Previously pinned top-right of the viewport where
    // it overlapped page-header actions (search bar, Run button).
    // Fall back to body for pre-login routes (setup wizard, login
    // screen) where the sidebar isn't mounted.
    var statusRow = document.getElementById('sidebar-status');
    if (statusRow) {
      statusRow.classList.add('has-bell');
      statusRow.appendChild(bellEl);
    } else {
      // Retry after DOMContentLoaded in case sidebar hasn't mounted yet.
      document.body.appendChild(bellEl);
      setTimeout(function() {
        var sr = document.getElementById('sidebar-status');
        if (sr && bellEl.parentNode === document.body) {
          sr.classList.add('has-bell');
          sr.appendChild(bellEl);
        }
      }, 500);
    }
  }

  function _togglePanel() {
    if (panelEl) { panelEl.remove(); panelEl = null; return; }
    panelEl = document.createElement('div');
    panelEl.className = 'notif-panel';
    panelEl.setAttribute('data-testid', 'notif-panel');
    // Anchor to the bell's current viewport position: panel floats
    // just to the right of the sidebar bell, vertically aligned.
    // Falls back to the old top-right corner if the bell happens to
    // be body-parented (pre-login wizard edge case).
    var rect = bellEl.getBoundingClientRect();
    if (bellEl.parentNode !== document.body) {
      panelEl.style.left = (rect.right + 8) + 'px';
      panelEl.style.top = Math.max(8, rect.top - 4) + 'px';
      panelEl.style.right = 'auto';
    }
    if (!ring.length) {
      var em = document.createElement('div');
      em.className = 'notif-row'; em.style.color = 'var(--muted)';
      em.textContent = 'No notifications yet.';
      panelEl.appendChild(em);
    } else {
      ring.slice().reverse().forEach(function(n) {
        var row = document.createElement('div');
        row.className = 'notif-row';
        var ts = document.createElement('span');
        ts.style.cssText = 'font-size:10px;color:var(--muted);margin-right:8px';
        ts.textContent = new Date(n.time).toLocaleTimeString();
        var body = document.createElement('span');
        body.textContent = n.message;
        if (n.kind === 'error') body.style.color = 'var(--red,#ef4444)';
        else if (n.kind === 'success') body.style.color = 'var(--green,#22c55e)';
        row.append(ts, body);
        panelEl.appendChild(row);
      });
    }
    document.body.appendChild(panelEl);
    // Clicking outside closes.
    setTimeout(function() {
      document.addEventListener('click', function _off(ev) {
        if (!panelEl) return;
        if (panelEl.contains(ev.target) || bellEl.contains(ev.target)) return;
        panelEl.remove(); panelEl = null;
        document.removeEventListener('click', _off);
      });
    }, 0);
    unread = 0;
    _paintCount();
  }

  function _paintCount() {
    if (!countEl) return;
    if (unread > 0) {
      countEl.style.display = '';
      countEl.textContent = unread > 99 ? '99+' : String(unread);
    } else {
      countEl.style.display = 'none';
    }
  }

  function record(message, kind) {
    if (!message) return;
    ring.push({ time: Date.now(), message: String(message), kind: kind || 'info' });
    if (ring.length > CAP) ring.splice(0, ring.length - CAP);
    unread++;
    _paintCount();
  }

  // Patch the global `toast` so every toast is mirrored into the ring.
  function _patchToast() {
    var original = window.toast;
    if (!original) {
      setTimeout(_patchToast, 100);
      return;
    }
    if (original._notifPatched) return;
    window.toast = function(message, kind) {
      try { record(message, kind); } catch (e) {}
      return original.apply(this, arguments);
    };
    window.toast._notifPatched = true;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { _install(); _patchToast(); });
  } else {
    _install(); _patchToast();
  }

  // Expose for tests + for direct callers that want to record without a toast.
  window._notif = { record: record, _ring: ring };
})();
