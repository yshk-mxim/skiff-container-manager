// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Notifications: sidebar bell + toast mirror.
 *
 * Design intent (one place so future maintainers can reason about it):
 *
 *   - Every call to the global `toast(message, kind)` is mirrored into
 *     an in-memory ring buffer (last 50) so a user who missed the
 *     ephemeral toast can review what fired.
 *   - Ring buffer is browser-session scoped. No server persistence;
 *     reload clears history. The bell is an aid, not an audit trail.
 *     (The real audit trail is /api/system/audit-log.)
 *   - The bell lives in the sidebar on its own row, above the
 *     "Connected" status line. Click = open panel. Unread count only
 *     clears when the panel is explicitly opened; toast fade does
 *     NOT clear it.
 *   - Panel anchors to the bell's on-screen position (getBoundingClientRect)
 *     so it reads as "notifications for this bell" regardless of
 *     whether the bell is in the sidebar or in fallback (body).
 *   - All critical layout is set as inline styles on the created
 *     elements, not via external CSS classes. That makes the bell
 *     robust against a stale cached styles.css — users who soft-refresh
 *     (F5) between deploys still see a correctly-positioned bell.
 *   - Self-installing: does NOT require index.html to pre-declare a
 *     mount point. Finds `.sidebar` at DOM-ready, inserts the notif
 *     row just above `.sidebar-footer`. If `.sidebar` is absent
 *     (setup wizard, login screen, error page), skips silently.
 */
"use strict";

(function() {
  if (window._notifRingInstalled) return;
  window._notifRingInstalled = true;

  var CAP = 50;
  var ring = [];
  var unread = 0;
  var rowEl = null;       // sidebar row container
  var bellEl = null;      // the 🔔 glyph
  var labelEl = null;     // "Notifications" label
  var countEl = null;     // unread-count chip
  var panelEl = null;     // history popup (created on open)

  function _styleInline(el, css) {
    // Small helper so the ~15 critical layout rules live with the
    // element creation and can't be overridden by stale cached CSS.
    Object.keys(css).forEach(function(k) { el.style[k] = css[k]; });
  }

  function _buildRow() {
    // Real <button>: native Enter/Space activation, :focus-visible
    // works out of the box, screen readers announce it as a button
    // without extra ARIA. (div[role=button] would require manual
    // keydown wiring and custom focus-ring CSS.)
    var row = document.createElement('button');
    row.type = 'button';
    row.className = 'skiff-notif-row';
    row.setAttribute('aria-haspopup', 'dialog');
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('data-testid', 'notif-row');
    _styleInline(row, {
      display: 'flex',
      alignItems: 'center',
      gap: '10px',
      padding: '8px 16px',
      cursor: 'pointer',
      fontSize: '12px',
      userSelect: 'none',
      background: 'transparent',
      border: 'none',
      color: 'inherit',
      textAlign: 'left',
      width: '100%',
      fontFamily: 'inherit',
    });

    var bell = document.createElement('span');
    bell.className = 'skiff-notif-bell';
    bell.setAttribute('data-testid', 'notif-bell');
    // Decorative — the button already has its accessible name via
    // the label child + aria-label below. Hiding the glyph from AT
    // prevents duplicate "bell" announcement.
    bell.setAttribute('aria-hidden', 'true');
    bell.textContent = '\ud83d\udd14';
    _styleInline(bell, { fontSize: '15px', lineHeight: '1' });

    var label = document.createElement('span');
    label.className = 'skiff-notif-label';
    label.textContent = 'Notifications';
    _styleInline(label, { flex: '1' });

    var count = document.createElement('span');
    count.className = 'skiff-notif-count';
    count.setAttribute('data-testid', 'notif-count');
    _styleInline(count, {
      background: '#ef4444',
      color: 'white',
      borderRadius: '10px',
      fontSize: '10px',
      fontWeight: '700',
      minWidth: '18px',
      height: '18px',
      lineHeight: '18px',
      padding: '0 6px',
      textAlign: 'center',
      display: 'none',
    });

    row.appendChild(bell);
    row.appendChild(label);
    row.appendChild(count);

    row.onclick = function(ev) { ev.stopPropagation(); _togglePanel(); };
    // <button> handles Space/Enter natively — no custom keydown needed.

    return { row: row, bell: bell, label: label, count: count };
  }

  function _install() {
    if (rowEl) return;
    var parts = _buildRow();
    rowEl = parts.row;
    bellEl = parts.bell;
    labelEl = parts.label;
    countEl = parts.count;

    var sidebar = document.querySelector('.sidebar');
    var footer = sidebar && sidebar.querySelector('.sidebar-footer');
    if (sidebar && footer) {
      // Insert the notif row as a sibling just above the footer.
      sidebar.insertBefore(rowEl, footer);
      return;
    }
    // No sidebar yet — the page may be the setup wizard or login
    // screen (neither generates notifications users need to act on).
    // Poll briefly; if the sidebar appears, re-mount. Otherwise give
    // up silently after ~5s.
    var attempts = 0;
    var poll = setInterval(function() {
      attempts++;
      var sb = document.querySelector('.sidebar');
      var ft = sb && sb.querySelector('.sidebar-footer');
      if (sb && ft) {
        clearInterval(poll);
        sb.insertBefore(rowEl, ft);
      } else if (attempts > 10) {
        clearInterval(poll);
      }
    }, 500);
  }

  function _togglePanel() {
    if (panelEl) {
      panelEl.remove();
      panelEl = null;
      if (rowEl) {
        rowEl.setAttribute('aria-expanded', 'false');
        rowEl.focus();  // return keyboard focus to the trigger
      }
      return;
    }

    panelEl = document.createElement('div');
    panelEl.className = 'skiff-notif-panel';
    panelEl.setAttribute('role', 'dialog');
    panelEl.setAttribute('aria-label', 'Notifications history');
    panelEl.setAttribute('data-testid', 'notif-panel');
    if (rowEl) rowEl.setAttribute('aria-expanded', 'true');
    _styleInline(panelEl, {
      position: 'fixed',
      zIndex: '901',
      width: '340px',
      maxHeight: '420px',
      overflowY: 'auto',
      background: 'var(--card, #1e293b)',
      border: '1px solid var(--border, #334155)',
      borderRadius: '8px',
      boxShadow: '0 4px 16px rgba(0, 0, 0, 0.25)',
      color: 'var(--text, #e2e8f0)',
      fontSize: '12px',
    });

    // Header row: title + "Clear all"
    var header = document.createElement('div');
    _styleInline(header, {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '10px 14px',
      borderBottom: '1px solid var(--border, #334155)',
      fontWeight: '600',
      fontSize: '13px',
    });
    var hTitle = document.createElement('span'); hTitle.textContent = 'Notifications';
    var hClear = document.createElement('button');
    hClear.type = 'button'; hClear.className = 'skiff-notif-clear';
    hClear.textContent = 'Clear all';
    _styleInline(hClear, {
      background: 'transparent', border: 'none', color: 'var(--muted, #94a3b8)',
      fontSize: '11px', cursor: 'pointer', padding: '0',
    });
    hClear.onclick = function(ev) {
      ev.stopPropagation();
      ring.length = 0;
      unread = 0;
      _paintCount();
      _togglePanel(); // close + will reopen empty on next click
    };
    header.append(hTitle, hClear);
    panelEl.appendChild(header);

    // Body: list of notifications (newest first) or empty state.
    var body = document.createElement('div');
    if (!ring.length) {
      var em = document.createElement('div');
      _styleInline(em, { padding: '16px', color: 'var(--muted, #94a3b8)', textAlign: 'center' });
      em.textContent = 'No notifications yet.';
      body.appendChild(em);
    } else {
      ring.slice().reverse().forEach(function(n, idx) {
        var r = document.createElement('div');
        _styleInline(r, {
          display: 'flex', alignItems: 'flex-start', gap: '8px',
          padding: '10px 14px',
          borderBottom: idx === ring.length - 1 ? 'none' : '1px solid var(--border, #334155)',
        });
        var ts = document.createElement('div');
        _styleInline(ts, { fontSize: '10px', color: 'var(--muted, #94a3b8)', minWidth: '64px' });
        ts.textContent = new Date(n.time).toLocaleTimeString();
        var msg = document.createElement('div');
        _styleInline(msg, { flex: '1', wordBreak: 'break-word' });
        msg.textContent = n.message;
        if (n.kind === 'error')   msg.style.color = '#f87171';
        else if (n.kind === 'success') msg.style.color = '#86efac';
        r.append(ts, msg);
        body.appendChild(r);
      });
    }
    panelEl.appendChild(body);

    // Anchor next to the bell. Default: to the RIGHT of the sidebar
    // (matching the visual "notifications coming out of the bell"
    // expectation). Clamp to viewport so it never goes offscreen.
    var rect = (rowEl || bellEl || document.body).getBoundingClientRect();
    var left = rect.right + 8;
    var top = rect.top;
    if (left + 340 > window.innerWidth) left = window.innerWidth - 348;
    if (top + 420 > window.innerHeight) top = Math.max(8, window.innerHeight - 428);
    panelEl.style.left = left + 'px';
    panelEl.style.top = top + 'px';

    document.body.appendChild(panelEl);
    // Move focus to the first focusable control inside the panel
    // (the Clear-all button). Screen-reader + keyboard users now get
    // a real "panel opened, here's the first action" handoff.
    setTimeout(function() { hClear.focus(); }, 0);

    // Outside-click closes. Delayed attach so THIS click doesn't close.
    setTimeout(function() {
      function _off(ev) {
        if (!panelEl) return;
        if (panelEl.contains(ev.target) || (rowEl && rowEl.contains(ev.target))) return;
        panelEl.remove(); panelEl = null;
        document.removeEventListener('click', _off);
      }
      document.addEventListener('click', _off);
    }, 0);

    // Esc closes.
    function _esc(ev) {
      if (ev.key === 'Escape' && panelEl) {
        panelEl.remove(); panelEl = null;
        document.removeEventListener('keydown', _esc);
      }
    }
    document.addEventListener('keydown', _esc);

    // Opening the panel = user saw the alerts; clear unread. Ring
    // stays intact so they can re-open later.
    unread = 0;
    _paintCount();
  }

  function _paintCount() {
    if (!countEl) return;
    // Self-heal: if the ring is empty, the count chip MUST be zero.
    // Stops the "bell shows 1 but panel is empty" class of bug —
    // any future path that accidentally increments unread without
    // pushing to ring can't leave the user with a phantom badge.
    if (!ring.length) unread = 0;
    if (unread > 0) {
      countEl.style.display = 'inline-block';
      countEl.textContent = unread > 99 ? '99+' : String(unread);
      // Screen readers otherwise announce the chip as just "3" with
      // no context. A proper label + role makes it self-describing.
      countEl.setAttribute('aria-label', unread + ' unread notification' + (unread === 1 ? '' : 's'));
    } else {
      countEl.style.display = 'none';
      countEl.removeAttribute('aria-label');
    }
    // Update the button's accessible name so it reads "Notifications,
    // 3 unread" instead of just "Notifications" after new items arrive.
    if (rowEl) {
      rowEl.setAttribute(
        'aria-label',
        unread > 0 ? 'Notifications, ' + unread + ' unread' : 'Notifications',
      );
    }
  }

  function record(message, kind) {
    if (!message) return;
    ring.push({ time: Date.now(), message: String(message), kind: kind || 'info' });
    if (ring.length > CAP) ring.splice(0, ring.length - CAP);
    unread++;
    _paintCount();
    // Pulse the bell so the user sees the increment without a
    // floating element. Red-tinted pulse for errors so severity
    // reads at a glance — matches the count chip colour so the two
    // layers reinforce each other. Class is toggled off after the
    // animation so a rapid burst re-triggers cleanly (force-reflow
    // trick restarts a running animation).
    if (bellEl) {
      bellEl.classList.remove('pulse', 'pulse-error');
      void bellEl.offsetWidth;
      var pulseCls = kind === 'error' ? 'pulse-error' : 'pulse';
      bellEl.classList.add(pulseCls);
      setTimeout(function() {
        if (bellEl) bellEl.classList.remove('pulse', 'pulse-error');
      }, 1300);
    }
  }

  // Patch the global toast() so every toast is mirrored into the ring
  // WITHOUT interfering with the existing toast fade. toast() lives in
  // app.js; if app.js hasn't parsed yet, retry every 100ms. Once patched,
  // we stop — the `_notifPatched` flag keeps double-wrapping impossible.
  function _patchToast() {
    var original = window.toast;
    if (typeof original !== 'function') {
      setTimeout(_patchToast, 100);
      return;
    }
    if (original._notifPatched) return;
    window.toast = function(message, kind) {
      try { record(message, kind); } catch (e) { /* ignore */ }
      return original.apply(this, arguments);
    };
    window.toast._notifPatched = true;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { _install(); _patchToast(); });
  } else {
    _install(); _patchToast();
  }

  // Public hooks (tests, direct callers that want to record without
  // showing a toast, or a debug console that wants to poke the state).
  window._notif = {
    record: record,
    _ring: ring,
    _rowEl: function() { return rowEl; },
    _unread: function() { return unread; },
  };
})();
