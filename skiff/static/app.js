// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
"use strict";
const API = '/api';
let currentPage = 'dashboard';
// Expose on window so core/palette.js and other modules can read the
// active page without the app.js module boundary. Kept in sync by the
// only writer (showPage below).
window.currentPage = currentPage;
let refreshTimer = null;
let dockerOk = false;
let _lastContainers = null;
let _refreshInFlight = false;
let _dockerVmHost = '';
let _appDockerHost = '';
const MAX_LOG_LINES = 10000;
// Mirrors VALID_RESTART_POLICIES in skiff/config.py — kept in sync manually.
// If this ever drifts, the Clone modal's restart select silently falls back to
// "no" which is safe but may confuse users; covered by e2e.
const VALID_RESTART_POLICIES_CLIENT = ['no', 'on-failure', 'unless-stopped', 'always'];

/**
 * Run a DELETE with the undo window, then show a toast whose "Undo" link
 * cancels the pending operation. Falls back to a plain "X deleted" toast
 * when the server didn't return an undo_token (queue full / undo disabled).
 *
 * Usage:
 *   undoableDelete('/api/containers/abc', 'Container', loadContainers);
 */
async function undoableDelete(url, kindLabel, refresh) {
  var sep = url.indexOf('?') >= 0 ? '&' : '?';
  var resp = await apiFetch(url + sep + 'undo=1', { method: 'DELETE' });
  if (resp && resp.undo_token) {
    // Undo bar appended to #undo-bar-container (bottom-center) so
    // every undo surface in the app reads as one affordance — delete-
    // undo, compose-down-undo, and prune-undo all share a visual lane.
    // Countdown text + draining progress bar show the remaining
    // window at a glance.
    var windowSecs = Math.max(1, resp.expires_in || 5);
    var container = document.getElementById('undo-bar-container');
    if (!container) {
      container = UI.el('div', {
        id: 'undo-bar-container',
        class: 'undo-bar-container',
        role: 'alert',
        'aria-live': 'assertive',
        'aria-atomic': 'true',
      });
      document.body.appendChild(container);
    }
    // Also record the pending delete to the bell so the event is
    // visible in the notifications panel, not just in the undo lane.
    // If the user clicks Undo, we record that too (below).
    if (typeof window.toast === 'function') {
      window.toast(kindLabel + ' delete queued — undo available for ' + windowSecs + 's', 'info');
    }
    var undone = false;
    var labelSpan = UI.el('span', {
      class: 'toast-label',
      text: t('undo.pending_label', { kind: kindLabel, seconds: windowSecs }),
    });
    var undoLink = UI.el('span', {
      class: 'undo-link', text: t('undo.button'),
      on: {
        click: function() {
          if (undone) return;
          undone = true;
          apiFetch(API + '/undo/' + encodeURIComponent(resp.undo_token),
                   { method: 'POST' })
            .then(function() {
              if (toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
              UI.toast(t('undo.toast'), 'success');
              if (refresh) refresh();
            })
            .catch(function() { UI.toast(t('undo.window_passed'), 'error'); });
        },
      },
    });
    var bar = UI.el('div', { class: 'toast-progress-bar' });
    var progress = UI.el('div', { class: 'toast-progress' }, bar);
    var toastEl = UI.el('div', { class: 'toast-undo' },
      UI.el('div', { class: 'toast-row' }, labelSpan, undoLink),
      progress,
    );
    container.appendChild(toastEl);
    // Kick the progress-bar transition on the next frame so the browser
    // renders the 100% width before animating to 0% over the window.
    requestAnimationFrame(function() {
      UI.setStyle(bar, 'transition', 'width ' + windowSecs + 's linear');
      UI.setStyle(bar, 'width', '0%');
    });
    // Tick the countdown text every 500ms.
    var startedAt = Date.now();
    var tick = setInterval(function() {
      if (undone) { clearInterval(tick); return; }
      var remainingMs = windowSecs * 1000 - (Date.now() - startedAt);
      var remainingS = Math.max(0, Math.ceil(remainingMs / 1000));
      if (remainingS <= 0) {
        labelSpan.textContent = t('undo.pending_finalizing', { kind: kindLabel });
        UI.setStyle(undoLink, 'display', 'none');
        clearInterval(tick);
      } else {
        labelSpan.textContent = t('undo.pending_label', { kind: kindLabel, seconds: remainingS });
      }
    }, 500);
    // Fixed 1500 ms buffer after the window — absorbs server-side
    // Timer jitter + Docker SDK RTT. Not proportional because the
    // jitter is bounded, not proportional to window length.
    var reloadMs = windowSecs * 1000 + 1500;
    setTimeout(function() {
      if (toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
      clearInterval(tick);
      if (!undone && refresh) refresh();
    }, reloadMs);
  } else {
    toast(kindLabel + t('undo.deleted_suffix'), 'info');
  }
  // Refresh immediately so the row vanishes from the list while the toast
  // tells the user they have time to click Undo. The final re-fetch above
  // keeps the view honest once the window actually expires.
  if (refresh) refresh();
}


/**
 * Shared undo-toast renderer for queued destructive ops. Every page
 * that triggers a prune/down/delete that returns {undo_token,
 * expires_in} from the server routes through this helper so the
 * countdown, progress bar, Undo-link, and expiry-refresh are identical
 * everywhere. Call sites:
 *   - System prune (undoableSystemPrune wraps it below)
 *   - Images prune (dangling + all-unused)
 *   - Volumes prune
 *   - Networks prune
 *   - Build cache prune
 *   - Compose down
 *
 * `label` names the op ("System prune", "Volume prune", …).
 * `undoToken` / `windowSecs` come from the server envelope.
 * `refreshFn` is called once the window expires so the listing
 * refreshes after the op actually fires.
 */
function renderUndoToast(label, undoToken, windowSecs, refreshFn) {
  // Zero / missing window means undo is disabled (UNDO_DELAY_SECS=0).
  // Skip the toast entirely — the op fired inline — and just refresh.
  // Proportional timing collapses to 0 here and this guard is the
  // reason the refresh buffer never needs to handle the zero case.
  if (!windowSecs || windowSecs <= 0) {
    if (refreshFn) refreshFn();
    return;
  }
  windowSecs = Math.max(1, windowSecs);
  // Undo bars live in #undo-bar-container (pre-declared in index.html
  // with role="alert" aria-live="assertive" aria-atomic="true" so
  // screen readers interrupt-announce the available undo action).
  // Fall back to creating the container only if the HTML shell is
  // older than this code (e.g. cached served bytes from pre-a11y
  // rollout).
  var container = document.getElementById('undo-bar-container');
  if (!container) {
    container = UI.el('div', {
      id: 'undo-bar-container',
      class: 'undo-bar-container',
      role: 'alert',
      'aria-live': 'assertive',
      'aria-atomic': 'true',
    });
    document.body.appendChild(container);
  }
  // Record the queued operation to the bell. The undo bar is the live
  // affordance; the bell entry is the history line. Together: user can
  // act now (undo bar) OR review later (bell). Every page that calls
  // renderUndoToast — compose-down, volume-prune, network-prune,
  // image-prune, system-prune, build-cache-prune — now gets a bell
  // entry without each page needing to duplicate the toast call.
  if (typeof window.toast === 'function') {
    window.toast(label + ' queued — undo available for ' + windowSecs + 's', 'info');
  }
  var undone = false;
  var labelSpan = UI.el('span', {
    class: 'toast-label',
    text: label + ' in ' + windowSecs + 's — click Undo to cancel',
  });
  var undoLink = UI.el('span', {
    class: 'undo-link', text: t('undo.button'),
    on: {
      click: function() {
        if (undone) return;
        undone = true;
        apiFetch(API + '/undo/' + encodeURIComponent(undoToken),
                 { method: 'POST' })
          .then(function() {
            if (toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
            UI.toast(t('undo.toast'), 'success');
          })
          .catch(function() { UI.toast(t('undo.window_passed'), 'error'); });
      },
    },
  });
  var bar = UI.el('div', { class: 'toast-progress-bar' });
  var progress = UI.el('div', { class: 'toast-progress' }, bar);
  var toastEl = UI.el('div', { class: 'toast-undo' },
    UI.el('div', { class: 'toast-row' }, labelSpan, undoLink),
    progress,
  );
  container.appendChild(toastEl);
  requestAnimationFrame(function() {
    UI.setStyle(bar, 'transition', 'width ' + windowSecs + 's linear');
    UI.setStyle(bar, 'width', '0%');
  });
  var startedAt = Date.now();
  var tick = setInterval(function() {
    if (undone) { clearInterval(tick); return; }
    var remainingMs = windowSecs * 1000 - (Date.now() - startedAt);
    var remainingS = Math.max(0, Math.ceil(remainingMs / 1000));
    if (remainingS <= 0) {
      labelSpan.textContent = label + ' firing…';
      UI.setStyle(undoLink, 'display', 'none');
      clearInterval(tick);
    } else {
      labelSpan.textContent = label + ' in ' + remainingS + 's — click Undo to cancel';
    }
  }, 500);
  // Post-window refresh buffer: the server's `threading.Timer` fires
  // a fixed amount late (Python scheduling + Docker SDK RTT), not
  // proportional to the window. 1500 ms covers jitter at every window
  // length — the user's short-window case (UNDO_DELAY_SECS=1) stopped
  // refreshing because a 500 ms buffer raced the rm; long windows
  // don't need more buffer than short ones.
  var postBufferMs = 1500;
  setTimeout(function() {
    if (toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
    clearInterval(tick);
    if (!undone && refreshFn) refreshFn();
  }, windowSecs * 1000 + postBufferMs);
}

window.renderUndoToast = renderUndoToast;


/**
 * System-prune variant. Server returns {undo_token, expires_in} by
 * default; if the queue is full it returns the legacy counts shape —
 * render both cleanly.
 */
async function undoableSystemPrune() {
  var resp = await apiFetch(API + '/system/prune', { method: 'POST' });
  if (resp && resp.undo_token) {
    renderUndoToast('System prune', resp.undo_token, resp.expires_in, loadSystem);
    return;
  }
  // Queue was full — server ran it synchronously. Render the legacy
  // counts summary so the user still sees what happened.
  var parts = [];
  if (resp && resp.containers_deleted) parts.push(resp.containers_deleted + ' container(s)');
  if (resp && resp.images_deleted) parts.push(resp.images_deleted + ' image(s)');
  if (resp && resp.networks_deleted) parts.push(resp.networks_deleted + ' network(s)');
  var m = parts.length ? 'Pruned ' + parts.join(', ') : 'Nothing to prune';
  if (resp && resp.space_reclaimed_mb > 0) m += '. Reclaimed ' + resp.space_reclaimed_mb + ' MB';
  toast(m, parts.length ? 'success' : 'info');
  loadSystem();
}


// Core widgets (command palette, theme toggle) live in
// skiff/static/core/{palette,theme}.js and load as separate <script>
// tags in index.html.

function esc(s) {
  var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML;
}

var _inFlight = new Set();
function guardedAction(key, fn) {
  if (_inFlight.has(key)) { toast(t('undo.action_in_progress'), 'info'); return Promise.resolve(); }
  _inFlight.add(key);
  return Promise.resolve().then(fn).finally(function() { _inFlight.delete(key); });
}

var _activeIntervals = [];
function managedInterval(fn, ms) {
  var id = setInterval(fn, ms);
  _activeIntervals.push(id);
  return id;
}
function clearAllIntervals() {
  _activeIntervals.forEach(function(id) { clearInterval(id); });
  _activeIntervals = [];
  clearInterval(refreshTimer);
  refreshTimer = null;
}

// Pause auto-refresh when tab is hidden, resume when visible
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'hidden') {
    clearAllIntervals();
  } else if (getToken() && currentPage) {
    var pages = { containers: loadContainers, images: loadImages, volumes: loadVolumes, networks: loadNetworks, compose: showCompose, system: loadSystem };
    if (pages[currentPage]) pages[currentPage]();
  }
});

function closeDetailWS() {
  var main = document.getElementById('main');
  if (main && main._ws) {
    try { main._ws.close(1000, 'navigating away'); } catch(e) {}
    main._ws = null;
  }
}

// ── Auth ──
// Session timeout defaults: 15-min idle + 8-hour absolute. Both are
// overridden from /api/config at boot if the operator set the
// SESSION_IDLE_SECS / SESSION_ABS_TIMEOUT env vars on the server — so a
// deployment tightening the window doesn't need an app.js edit.
// The defaults here are the fallback if /api/config hasn't resolved yet
// (e.g. for requests fired before the initial config fetch).
var SESSION_IDLE_MS = 15 * 60 * 1000;
var SESSION_ABSOLUTE_MS = 8 * 60 * 60 * 1000;
// UNDO_WINDOW_SECS mirrors config.UNDO_DELAY_SECS. The fallback of 5 matches
// defaults.toml so a first-paint confirm() prompt doesn't tell the user
// "undo for ~10s" while the server actually applies 5s — that was the bug
// before UNDO_DELAY_SECS was exposed. _applySessionTimeoutsFromConfig()
// refreshes this from /api/config once it resolves.
var UNDO_WINDOW_SECS = 5;
// Browser-side polling + fetch-timeout knobs. Each shadows a server-side
// value of the same name; _applySessionTimeoutsFromConfig() updates them
// once /api/config resolves. Fallbacks match defaults.toml so first-paint
// behaviour matches steady-state before the config round-trip completes.
var CONTAINERS_POLL_MS = 5000;
var DASHBOARD_POLL_MS = 8000;
var EVENTS_POLL_MS = 5000;
var FETCH_TIMEOUT_MS = 30000;
var _idleTimer = null;

function _applySessionTimeoutsFromConfig(appCfg) {
  // Server-side knobs: SESSION_IDLE_SECS (seconds) and SESSION_ABS_TIMEOUT
  // (seconds). Both are `expose=True`, so /api/config serves them.
  // Cap the values at sane bounds so a bad value can't freeze the UI
  // (idle < 60s would lock out the user before any page finishes loading).
  if (!appCfg) return;
  var changed = false;
  var idle = Number(appCfg.session_idle_secs);
  if (isFinite(idle) && idle >= 60 && idle <= 24 * 60 * 60 && SESSION_IDLE_MS !== idle * 1000) {
    SESSION_IDLE_MS = idle * 1000;
    changed = true;
  }
  var abs_ = Number(appCfg.session_abs_timeout);
  if (isFinite(abs_) && abs_ >= 300 && abs_ <= 7 * 24 * 60 * 60 && SESSION_ABSOLUTE_MS !== abs_ * 1000) {
    SESSION_ABSOLUTE_MS = abs_ * 1000;
    changed = true;
  }
  // Pull the authoritative undo window so confirm() prompts and bulk-
  // action labels match what the server will actually enforce.
  var undo = Number(appCfg.undo_delay_secs);
  if (isFinite(undo) && undo >= 0 && undo <= 3600) {
    UNDO_WINDOW_SECS = undo;
  }
  // Browser-side poll intervals + fetch timeout. All reads are
  // bounds-clamped so a misconfigured /api/config can't jam the UI
  // (e.g. 0 would make setInterval fire as fast as the event loop
  // allows; 10^9 would feel like no polling at all).
  function _applyMs(field, current, lo, hi) {
    var n = Number(appCfg[field]);
    return (isFinite(n) && n >= lo && n <= hi) ? n : current;
  }
  CONTAINERS_POLL_MS = _applyMs("containers_poll_ms", CONTAINERS_POLL_MS, 500, 600000);
  DASHBOARD_POLL_MS  = _applyMs("dashboard_poll_ms",  DASHBOARD_POLL_MS,  500, 600000);
  EVENTS_POLL_MS     = _applyMs("events_poll_ms",     EVENTS_POLL_MS,     500, 600000);
  FETCH_TIMEOUT_MS   = _applyMs("fetch_timeout_ms",   FETCH_TIMEOUT_MS,   1000, 3600000);
  // Re-arm the idle timer if the value changed after setToken() already
  // scheduled one with the hardcoded default. Without this, an operator
  // who sets SESSION_IDLE_SECS=60 stays signed-in for 900 s because the
  // timer was scheduled before /api/config resolved.
  if (changed && _idleTimer != null && typeof getToken === 'function' && getToken()) {
    resetIdleTimer();
  }
}

function getToken() { return sessionStorage.getItem('api_token') || ''; }
function setToken(t) {
  sessionStorage.setItem('api_token', t);
  sessionStorage.setItem('session_start', String(Date.now()));
  resetIdleTimer();
}

// Track all active WebSockets so they can all be closed on session end / tunnel drop
var _activeWS = new Set();
function registerWS(ws) {
  _activeWS.add(ws);
  ws.addEventListener('close', function() { _activeWS.delete(ws); });
  return ws;
}
function closeAllWS() {
  _activeWS.forEach(function(ws) { try { ws.close(1000, 'session ended'); } catch(e) {} });
  _activeWS.clear();
}

function sessionCleanup() {
  // Close all open WebSockets, clear refresh timers, remove modals
  clearAllIntervals();
  closeAllWS();
  var main = document.getElementById('main');
  if (main) main._ws = null;
  document.querySelectorAll('.modal-bg').forEach(function(m) { m.remove(); });
  _refreshInFlight = false;
  // Tear down every cached terminal iframe. closeAllWS doesn't touch
  // these — the WebSockets live inside each iframe's contentWindow,
  // not in the main document's WS registry. Without this, signing
  // out (or hitting the 8-hour absolute timeout) leaves N background
  // WS connections + xterm runtimes alive until the page is closed.
  if (window._termCache) {
    Object.keys(window._termCache).forEach(function (id) {
      try { _termCacheClose(id); } catch (e) { /* ignore */ }
    });
  }
  if (typeof _hideTermHost === 'function') _hideTermHost();
}

function checkSessionExpiry() {
  var start = parseInt(sessionStorage.getItem('session_start') || '0', 10);
  if (start && (Date.now() - start) > SESSION_ABSOLUTE_MS) {
    sessionStorage.clear();
    sessionCleanup();
    toast('Session expired (8-hour limit)', 'error');
    showLogin();
    return true;
  }
  return false;
}

// Lead-time on the idle-session warning banner. The user sees
// "Signing you out in Ns" 60s before the session is actually cleared,
// giving them time to click Stay signed in (which triggers a no-op
// apiFetch that resets both timers).
var _SESSION_IDLE_WARN_MS = 60 * 1000;
var _idleWarnTimer = null;
var _absoluteWarnTimer = null;
var _SESSION_ABS_WARN_MS = 2 * 60 * 1000;  // 2 minutes before hard cutoff

function resetIdleTimer() {
  clearTimeout(_idleTimer);
  clearTimeout(_idleWarnTimer);
  if (window.statusBanner) {
    window.statusBanner.clear('session_near_expiry');
  }
  _idleWarnTimer = setTimeout(function() {
    if (!getToken() || !window.statusBanner) return;
    // Paint the banner with the remaining countdown — {seconds}
    // substitutes from expiresInMs as the ticker runs.
    window.statusBanner.set('session_near_expiry', {
      severity: 'warn',
      message: (typeof t === 'function') ? t('banner.session_near_expiry') : 'Signing you out in {seconds}s for inactivity.',
      expiresInMs: _SESSION_IDLE_WARN_MS,
      action: {
        label: (typeof t === 'function') ? t('banner.stay_signed_in') : 'Stay signed in',
        onClick: function() {
          // A cheap authenticated call resets both timers via the
          // apiFetch path and, on 200, triggers the clear below.
          apiFetch(API + '/config').catch(function() { /* offline ok */ });
          resetIdleTimer();
        },
      },
    });
  }, Math.max(0, SESSION_IDLE_MS - _SESSION_IDLE_WARN_MS));
  _idleTimer = setTimeout(function() {
    if (getToken()) {
      sessionStorage.clear();
      sessionCleanup();
      if (window.statusBanner) window.statusBanner.clear('session_near_expiry');
      toast('Session expired (idle timeout)', 'error');
      showLogin();
    }
  }, SESSION_IDLE_MS);
}

// Absolute-session warning: fires once, ~2 minutes before the hard
// cutoff. Absolute can't be extended (that's the point), so no action
// button — just tell the user to save their work.
function armAbsoluteWarning() {
  if (_absoluteWarnTimer) clearTimeout(_absoluteWarnTimer);
  var start = parseInt(sessionStorage.getItem('session_start') || '0', 10);
  if (!start) return;
  var elapsed = Date.now() - start;
  var msUntilWarning = SESSION_ABSOLUTE_MS - _SESSION_ABS_WARN_MS - elapsed;
  if (msUntilWarning <= 0) return;  // already past — absolute check fires on next apiFetch
  _absoluteWarnTimer = setTimeout(function() {
    if (!getToken() || !window.statusBanner) return;
    window.statusBanner.set('session_absolute_near_expiry', {
      severity: 'warn',
      message: (typeof t === 'function') ? t('banner.session_absolute_near_expiry') : 'Session ends in {seconds}s. Save your work.',
      expiresInMs: _SESSION_ABS_WARN_MS,
    });
  }, msUntilWarning);
}

// Reset idle timer on user activity
['click','keydown','mousemove','scroll','touchstart'].forEach(function(evt) {
  document.addEventListener(evt, function() { if (getToken()) resetIdleTimer(); }, { passive: true });
});

function wsUrl(path) {
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return proto + '//' + location.host + path;
}

function wsAuthOnOpen(ws) {
  // Send auth token as first message instead of URL query param (avoids token in proxy logs)
  var t = getToken();
  if (t) ws.send('AUTH ' + t);
}

// ── Toast notifications ──
function toast(msg, type) {
  // The bell is the ONLY notification surface. `notifications.js`
  // patches this function before call, records the event into the
  // ring, increments the unread counter, and pulses the bell glyph
  // (red for errors, teal for info/success — see bell animation).
  //
  // Errors still demand attention — the bell red-pulse + red count
  // chip give severity two layers of visual weight in the sidebar,
  // and the bell panel surfaces the full message. Truly critical
  // failures should also render inline on the affected form / row
  // (e.g. Run modal's own error banner), not rely on a global toast.
  //
  // Undo bars are an ACTION AFFORDANCE and live in #undo-bar-container,
  // unchanged by this rule.
  if (type) { /* kind is recorded via the notifications.js patch */ }
  // No floating render. Return so `toast(...)` stays usable as a
  // side-effect-only call site without every caller having to swap
  // to `window._notif.record`.
}

// ── Login ──
function showLogin() {
  var main = document.getElementById('main');
  main.innerHTML = '';
  var wrap = document.createElement('div'); UI.setStyle(wrap, 'display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:70vh;');
  // Brand
  var brand = document.createElement('div'); UI.setStyle(brand, 'display:flex;align-items:center;gap:12px;margin-bottom:32px');
  brand.innerHTML = '<svg width="40" height="40" viewBox="0 0 28 28" fill="none"><rect width="28" height="28" rx="6" fill="#0d9488"/><rect x="6" y="6" width="16" height="16" rx="2" stroke="white" stroke-width="1.5" fill="none"/><line x1="6" y1="11" x2="22" y2="11" stroke="white" stroke-width="1.5"/><line x1="6" y1="16" x2="22" y2="16" stroke="white" stroke-width="1.5"/><circle cx="9" cy="8.5" r="1" fill="white"/><circle cx="9" cy="13.5" r="1" fill="white"/><circle cx="9" cy="18.5" r="1" fill="white"/></svg>';
  var brandName = document.createElement('span'); brandName.textContent = 'SKIFF Container Manager'; UI.setStyle(brandName, 'font-size:22px;font-weight:700;color:var(--text)');
  brand.appendChild(brandName);
  wrap.appendChild(brand);
  var box = document.createElement('div'); UI.setStyle(box, 'width:340px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:32px;box-shadow:0 4px 24px rgba(0,0,0,0.07)');
  var h3 = document.createElement('h3'); h3.textContent = 'Sign in'; UI.setStyle(h3, 'margin-bottom:4px;font-size:18px'); box.appendChild(h3);
  var sub = document.createElement('p'); sub.innerHTML = 'Enter the API token you saved during setup.<br><span style="font-size:11px">If you chose &ldquo;Save .env&rdquo;, the token is in that file as <code>API_TOKEN=</code>.</span>'; UI.setStyle(sub, 'font-size:12px;color:var(--muted);margin-bottom:20px'); box.appendChild(sub);
  // WCAG 2.1 AA (Principle 1.3.1 F68, Principle 4.1.2 H91): the
  // password input needs a programmatically-associated label.
  // Setting `htmlFor` on the label + matching `id` on the input
  // ties them for assistive tech; `aria-label` on the input itself
  // is a belt-and-braces backup that survives any future refactor
  // that drops the DOM sibling order.
  var lbl = document.createElement('label'); lbl.textContent = 'API Token'; lbl.htmlFor = 'login-token'; UI.setStyle(lbl, 'font-size:12px;font-weight:500;color:var(--muted);display:block;margin-bottom:6px'); box.appendChild(lbl);
  var inp = document.createElement('input'); inp.type = 'password'; inp.placeholder = 'Paste your API token';
  inp.id = 'login-token';
  inp.setAttribute('aria-label', 'API Token');
  inp.setAttribute('autocomplete', 'current-password');
  UI.setStyle(inp, 'width:100%;padding:9px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--card);color:var(--text)');
  box.appendChild(inp);
  var btn = document.createElement('button'); btn.className = 'btn primary'; btn.textContent = 'Sign in'; UI.setStyle(btn, 'margin-top:16px;width:100%;padding:10px;font-size:14px');
  btn.onclick = function() {
    if (!inp.value.trim()) { UI.setStyle(inp, 'borderColor', 'var(--red)'); return; }
    setToken(inp.value.trim());
    // The init IIFE exits early when there's no token at page-load time, so
    // /api/config (which populates _appDockerHost for the unreachable-Docker
    // tunnel detection) never runs on a post-startup login. Fetch it here so
    // the Reconnect-button branch works without a full page reload. Swallow
    // failures — the app is still usable with _appDockerHost empty.
    apiFetch(API + '/config').then(function(appCfg) {
      if (appCfg.docker_vm_host) _dockerVmHost = appCfg.docker_vm_host;
      if (appCfg.docker_host) _appDockerHost = appCfg.docker_host;
      _applySessionTimeoutsFromConfig(appCfg);
      // If a prior tab tripped the WS auth-lockout on this IP, paint the
      // banner immediately instead of waiting for the next WS handshake
      // to fail. Server returns 0 when not locked; we only surface a
      // banner when there's a real remaining countdown.
      var wsLock = appCfg.ws_auth_locked_remaining_secs;
      if (window.statusBanner && typeof wsLock === 'number' && wsLock > 0) {
        window.statusBanner.set('ws_auth_lockout', {
          severity: 'error',
          message: (typeof t === 'function') ? t('banner.ws_auth_lockout') : 'WebSocket locked out — try again in {seconds}s.',
          expiresInMs: wsLock * 1000,
        });
      }
      if (typeof armAbsoluteWarning === 'function') armAbsoluteWarning();
    }).catch(function() {}).finally(function() { showPage('containers'); });
  };
  inp.addEventListener('keydown', function(e) { if (e.key === 'Enter') btn.click(); });
  inp.addEventListener('input', function() { UI.setStyle(inp, 'borderColor', ''); });
  box.appendChild(btn);
  wrap.appendChild(box);
  main.appendChild(wrap);
  inp.focus();
}

// ── Fetch wrapper ──
// FETCH_TIMEOUT_MS is declared + refreshed from /api/config up near the
// other session-timing knobs. No duplicate `var` here.
/**
 * Authenticated fetch wrapper. Injects the API token and CSRF header, enforces session
 * expiry, and surfaces HTTP errors as thrown Error objects with the server's detail message.
 * @param {string} url - API path (e.g. '/api/containers')
 * @param {RequestInit} [opts] - Fetch options (method, body, headers, etc.)
 * @returns {Promise<any>} Parsed JSON response body
 */
async function apiFetch(url, opts) {
  if (checkSessionExpiry()) throw new Error('Session expired');
  opts = opts || {};
  const headers = { 'X-Requested-With': 'ContainerManager' };
  var t = getToken();
  if (t) headers['Authorization'] = 'Bearer ' + t;
  if (opts.headers) Object.assign(headers, opts.headers);
  // Abort after FETCH_TIMEOUT_MS to prevent hanging requests
  var controller = new AbortController();
  var timeoutId = setTimeout(function() { controller.abort(); }, opts._timeout || FETCH_TIMEOUT_MS);
  var fetchOpts = Object.assign({}, opts, { headers: headers, signal: controller.signal });
  delete fetchOpts._timeout;
  var res;
  try {
    res = await fetch(url, fetchOpts);
  } catch(e) {
    clearTimeout(timeoutId);
    if (e.name === 'AbortError') throw new Error('Request timed out — check your connection');
    throw new Error('Network error — server may be unreachable');
  }
  clearTimeout(timeoutId);
  if (res.status === 503) {
    setDockerStatus(false, 'Container engine unreachable');
    closeAllWS();  // close open terminals/log streams — tunnel may have dropped
    throw new Error('Container engine unreachable');
  }
  if (res.status === 401) { sessionStorage.removeItem('api_token'); toast('Authentication failed — check your API token', 'error'); showLogin(); throw new Error('Authentication required'); }
  if (res.status === 429) {
    // Parse Retry-After so the user sees an exact countdown instead of
    // guessing when to retry. slowapi emits seconds as an integer. The
    // banner auto-clears on expiry, matching the actual server-side
    // lockout window.
    var retryAfterRaw = res.headers.get('Retry-After');
    var retryAfter = retryAfterRaw ? parseInt(retryAfterRaw, 10) : NaN;
    if (window.statusBanner && isFinite(retryAfter) && retryAfter > 0) {
      window.statusBanner.set('rate_limited', {
        severity: 'warn',
        message: (typeof t === 'function') ? t('banner.rate_limited') : 'Rate limited — retry in {seconds}s.',
        expiresInMs: retryAfter * 1000,
      });
    }
    throw new Error('Rate limited — please wait a moment and try again');
  }
  if (!res.ok) {
    const err = await res.json().catch(function() { return { detail: res.statusText }; });
    // Preserve structured details (e.g. {detail: {message, code, help}}) so callers
    // can render classified errors. Error.message is always a string for display.
    var detail = err.detail;
    var msg = (detail && typeof detail === 'object') ? (detail.message || 'Request failed') : (detail || 'Request failed');
    var e = new Error(msg);
    if (detail && typeof detail === 'object') e.detail = detail;
    throw e;
  }
  setDockerStatus(true);
  return res.json();
}

// ── Docker status ──
//
// Connected state: the sidebar dot flips green and the `docker_unreachable`
// banner is cleared. Disconnected state: sidebar dot turns red and the
// banner module paints a per-transport recovery hint. Routed through
// statusBanner.set/.clear so other states (setup_window_expired,
// session_near_expiry, ws_auth_lockout, rate_limited) can coexist —
// previous implementation owned #status-banner directly and would wipe
// concurrent states.
function setDockerStatus(ok, msg) {
  dockerOk = ok;
  var el = document.getElementById('sidebar-status');
  if (ok) {
    el.innerHTML = '<span class="dot ok"></span> <span>Connected</span>';
    if (window.statusBanner) window.statusBanner.clear('docker_unreachable');
    return;
  }
  el.innerHTML = '<span class="dot down"></span> <span>Disconnected</span>';
  // Heuristic: tunnel-like sockets (/tmp/skiff-* or path containing
  // "tunnel") are managed by SKIFF — direct the user to Containers page
  // where the Reconnect button lives. Other unix:// sockets are local
  // runtimes. tcp:// is remote Docker.
  var isTunnelSocket = _appDockerHost && /^unix:\/\/.*(skiff|tunnel)/i.test(_appDockerHost);
  var isTcp = _appDockerHost && /^tcp:\/\//i.test(_appDockerHost);
  var detail;
  if (isTunnelSocket) {
    detail = 'SSH tunnel is down \u2014 open the Containers page to reconnect.';
  } else if (isTcp) {
    detail = 'Check that the remote Docker daemon is reachable at ' + _appDockerHost + ' and accepting TLS.';
  } else {
    detail = 'Make sure your Docker runtime (Docker Desktop, Colima, OrbStack, dockerd, etc.) is running, then reload.';
  }
  // "Container engine unreachable." prefix retained — tests
  // (test_e2e_resilience.py::test_r4_503_flips_banner_on_mid_session_failure)
  // match substrings "unreachable" / "engine" on the banner text.
  var message = 'Container engine unreachable. ' + detail;
  if (window.statusBanner) {
    window.statusBanner.set('docker_unreachable', { severity: 'error', message: message });
  }
}

// ── Navigation ──
function showPage(page) {
  // External entries (e.g. api-docs) short-circuit: open their href
  // in a new tab and leave the current SPA page alone. This is the
  // self-heal path for users whose browser cached an older sidebar.js
  // that attached a generic click handler to every .sidebar a — the
  // click still calls showPage('api-docs'), but this branch now
  // recognises the entry via the UI page registry and opens the URL
  // instead of falling through to loadContainers.
  var registryEntry = (window.UI && UI.getPages)
    ? UI.getPages(null).find(function(p) { return p.id === page; })
    : null;
  if (registryEntry && registryEntry.external && registryEntry.href) {
    // Deliberately do NOT persist this as the last-viewed page —
    // external actions aren't landing pages. The sidebar's "active"
    // styling on whatever page the user was on stays put.
    window.open(registryEntry.href, '_blank', 'noopener');
    return;
  }
  clearAllIntervals();
  closeDetailWS();
  // Hide the body-level terminal overlay so it doesn't paint over the
  // new page. The iframe stays mounted in `#_term-host` (separate from
  // #main), so its contentWindow + WS + scrollback survive the
  // navigation. Returning to a container's Terminal tab will
  // reattach the same session.
  _hideTermHost();
  currentPage = page;
  window.currentPage = page;
  // Remember the last-viewed page so a reload keeps the user where
  // they were. Silently ignore storage failures (private browsing
  // on some platforms throws). Only persist for pages in the sidebar
  // registry — detail routes are page='containers' under the hood.
  try { localStorage.setItem('skiff.last_page', page); } catch (e) { /* ignore */ }
  var main = document.getElementById('main');
  document.querySelectorAll('.sidebar a').forEach(function(a) { a.classList.remove('active'); });
  document.querySelectorAll('.sidebar a').forEach(function(a) {
    if (a.textContent.trim().toLowerCase() === page) a.classList.add('active');
  });
  _refreshInFlight = false;
  _lastContainers = null;
  var pages = {
    dashboard: showDashboard,
    containers: loadContainers,
    images: loadImages,
    templates: showTemplates,
    volumes: loadVolumes,
    networks: loadNetworks,
    compose: showCompose,
    system: loadSystem,
    settings: showSettings,
  };
  (pages[page] || loadContainers)();
}

// Page-navigation factory. Sidebar/palette still read from the
// hardcoded dispatch above because hot reloading the dispatch under test
// pressure (live_server fixture) exposes an asyncio-loop subtlety; the
// registry is populated for future callers (UI.getPages for persona
// filtering, wizard "what's here" modal, help system). Keep in sync when
// a new page is added.
(function registerPages() {
  [
    { id: 'dashboard',  label: 'Dashboard',  order: 5,  keywords: ['home', 'overview'] },
    { id: 'containers', label: 'Containers', order: 10, keywords: ['ps', 'ls'] },
    { id: 'images',     label: 'Images',     order: 20, keywords: ['pull', 'image'] },
    { id: 'templates',  label: 'Templates',  order: 25, keywords: ['quick-start', 'app', 'deploy'] },
    { id: 'volumes',    label: 'Volumes',    order: 30, keywords: ['mount'] },
    { id: 'networks',   label: 'Networks',   order: 40, keywords: ['net'] },
    { id: 'compose',    label: 'Compose',    order: 50, keywords: ['stack'] },
    { id: 'system',     label: 'System',     order: 60,
      personas: ['dev', 'sre', 'reviewer', 'ci'],
      keywords: ['prune', 'metrics', 'audit'] },
    { id: 'settings',   label: 'Settings',   order: 70,
      keywords: ['config', 'configuration', 'knob', 'env', 'toml', 'rate limit', 'session'] },
    // 10th nav slot: API docs. External link (OpenAPI / Swagger UI)
    // opens in a new tab so the operator can have their SKIFF session
    // + reference docs side-by-side. `external: true` tells
    // core/sidebar.js to emit an <a href target=_blank> instead of
    // wiring a showPage() click handler.
    { id: 'api-docs',   label: 'API docs',   order: 80,
      external: true, href: '/api/docs',
      keywords: ['api', 'openapi', 'swagger', 'reference'] },
  ].forEach(function(d) { UI.registerPage(d); });
})();

function makeBtn(label, onclick, cls) {
  var btn = document.createElement('button');
  btn.className = cls || 'btn';
  btn.textContent = label;
  btn.onclick = onclick;
  return btn;
}

function makeActionBtn(label, action, cls, pendingLabel) {
  var btn = makeBtn(label, async function() {
    btn.disabled = true; btn.classList.add('loading');
    if (pendingLabel) btn.textContent = pendingLabel;
    try { await action(); } catch(e) { toast(e.message, 'error'); }
    btn.disabled = false; btn.classList.remove('loading');
    btn.textContent = label;
  }, cls);
  return btn;
}

function formatPorts(ports) {
  if (!ports || Object.keys(ports).length === 0) return null;
  return Object.entries(ports).map(function(entry) { return entry[1] ? entry[1][0].HostPort + ':' + entry[0] : entry[0]; });
}

function relTime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  var s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

// Thin aliases to the UI widget library (skiff/static/ui.js). These keep the
// call sites in app.js short while we finish migrating everything to `UI.*`.
var copyToClipboard = UI.copy;
var _makeCopyableCommand = UI.copyCmd;

/**
 * "How do I start Docker?" card for the unreachable-Docker empty state.
 * Shows copy-paste runtime-start commands grouped by OS. All recommended
 * commands are non-root / user-space (Colima, OrbStack, Podman machine).
 * sudo-required commands (systemctl) are flagged as such so junior users
 * don't paste them blindly.
 */
function _renderStartDockerHelper(parent) {
  var isMac = navigator.platform && /Mac|iPhone|iPod|iPad/i.test(navigator.platform);
  // On Linux (and anything not explicitly Mac), also show Linux runtimes.
  var showMac = isMac || !navigator.platform;
  var showLinux = !isMac || !navigator.platform;

  var card = document.createElement('div');
  UI.setStyle(card, 'margin-top:20px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:8px;padding:20px;max-width:640px;text-align:left');

  var h4 = document.createElement('p');
  UI.setStyle(h4, 'font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em');
  h4.textContent = 'First time? Start your container runtime';
  card.appendChild(h4);

  var intro = document.createElement('p');
  UI.setStyle(intro, 'font-size:13px;color:var(--text);margin-bottom:14px;line-height:1.5');
  intro.textContent = 'Pick the runtime you have installed (or install one), then click Copy and paste the command in a terminal. No admin rights needed for the recommended options.';
  card.appendChild(intro);

  function addRuntime(name, descr, cmd, tag) {
    var row = document.createElement('div');
    UI.setStyle(row, 'margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--border-subtle)');
    var head = document.createElement('div');
    UI.setStyle(head, 'display:flex;align-items:center;gap:8px;margin-bottom:4px;');
    var nameEl = document.createElement('strong');
    UI.setStyle(nameEl, 'font-size:13px;color:var(--text-strong)');
    nameEl.textContent = name;
    head.appendChild(nameEl);
    if (tag) {
      var tagEl = document.createElement('span');
      UI.setStyle(tagEl, 'font-size:10px;padding:1px 8px;border-radius:10px;background:' +
        (tag === 'recommended' ? 'var(--badge-running-bg);color:var(--badge-running-fg)' :
         tag === 'sudo' ? 'var(--badge-stopped-bg);color:var(--badge-stopped-fg)' :
         'var(--border);color:var(--muted)'));
      tagEl.textContent = tag;
      head.appendChild(tagEl);
    }
    row.appendChild(head);
    var descrEl = document.createElement('div');
    UI.setStyle(descrEl, 'font-size:12px;color:var(--muted);margin-bottom:6px');
    descrEl.textContent = descr;
    row.appendChild(descrEl);
    row.appendChild(_makeCopyableCommand(cmd));
    card.appendChild(row);
  }

  if (showMac) {
    addRuntime(
      'Colima',
      'Free, user-space container runtime. If not installed: brew install colima docker docker-compose',
      'colima start',
      'recommended',
    );
    addRuntime(
      'OrbStack',
      'Commercial app with a free tier. Just open the app and it starts a user-space runtime.',
      'open -a OrbStack',
      '',
    );
    addRuntime(
      'Rancher Desktop',
      'Open the Rancher Desktop app from Applications and wait for the green checkmark.',
      'open -a "Rancher Desktop"',
      '',
    );
  }
  if (showLinux) {
    addRuntime(
      'Podman (rootless)',
      'User-space container runtime. Socket usually at /run/user/$UID/podman/podman.sock.',
      'systemctl --user start podman.socket',
      'recommended',
    );
    addRuntime(
      'Docker Engine',
      'System-wide Docker daemon. Requires admin privileges.',
      'sudo systemctl start docker',
      'sudo',
    );
  }
  addRuntime(
    'Nothing installed yet?',
    (showMac
      ? 'Install Colima via Homebrew: non-root, no GUI, works on Apple Silicon and Intel.'
      : 'Install Podman or Docker via your distro package manager.'),
    (showMac ? 'brew install colima docker docker-compose' : 'sudo apt install podman'),
    (showMac ? '' : 'sudo'),
  );

  var note = document.createElement('p');
  UI.setStyle(note, 'font-size:11px;color:var(--muted);margin-top:8px;line-height:1.5');
  note.textContent = 'After starting the runtime, reload this page. If you already used one of these and it\'s running, check that DOCKER_HOST points at its socket (see the "Common values" hint in the setup wizard).';
  card.appendChild(note);

  parent.appendChild(card);
}

// ── Unreachable-Docker empty state ──
// Renders the "cannot reach Docker" panel tailored to the configured docker_host.
// For server-managed SSH tunnels: asynchronously fetches /api/tunnel/status and
// offers a one-click Reconnect that re-opens the tunnel using the server-stored
// ssh_target (zero-trust: target never leaves the server). For user-managed
// tunnels or local runtimes: shows a concise hint, no form.
function _renderUnreachableDocker(main) {
  main.innerHTML = '';
  var errDiv = document.createElement('div');
  errDiv.className = 'empty-state';
  var h3 = document.createElement('h3');
  h3.textContent = 'Cannot reach Docker engine';
  errDiv.appendChild(h3);
  var p = document.createElement('p');
  UI.setStyle(p, 'margin-top:8px;max-width:480px');
  var isTunnelSocket = _appDockerHost && /^unix:\/\/.*(skiff|tunnel)/i.test(_appDockerHost);
  if (!isTunnelSocket) {
    p.textContent = 'Your container runtime isn\'t responding at ' + (_appDockerHost || 'the configured socket') + '. Start it and reload this page.';
    errDiv.appendChild(p);
    main.appendChild(errDiv);
    // Helpful panel for juniors: copy-paste commands to start a runtime.
    _renderStartDockerHelper(main);
    return;
  }
  // Tunnel-shaped socket: ask the server whether it's managed before deciding the UX.
  p.textContent = 'Checking tunnel status\u2026';
  errDiv.appendChild(p);
  main.appendChild(errDiv);
  apiFetch(API + '/tunnel/status').then(function(st) {
    while (errDiv.firstChild) errDiv.removeChild(errDiv.firstChild);
    var h = document.createElement('h3');
    h.textContent = 'Cannot reach Docker engine';
    errDiv.appendChild(h);
    var desc = document.createElement('p');
    UI.setStyle(desc, 'margin-top:8px;max-width:480px');
    if (st.managed) {
      desc.textContent = 'The managed SSH tunnel to your Docker host dropped. Reconnect to restore the link — SKIFF will reuse the SSH target it stored during setup.';
      errDiv.appendChild(desc);
      var btnWrap = document.createElement('div');
      UI.setStyle(btnWrap, 'margin-top:16px;display:flex;gap:10px;align-items:center;');
      var btn = document.createElement('button');
      btn.className = 'btn primary';
      btn.textContent = 'Reconnect tunnel';
      var status = document.createElement('span');
      UI.setStyle(status, 'font-size:12px;color:var(--muted);');
      btn.addEventListener('click', function() {
        btn.disabled = true;
        var original = btn.textContent;
        btn.textContent = 'Reconnecting\u2026';
        status.textContent = '';
        apiFetch(API + '/tunnel/reconnect', { method: 'POST' }).then(function() {
          UI.setStyle(status, 'color', 'var(--green, #22c55e)');
          status.textContent = '\u2713 Tunnel re-opened';
          setTimeout(function() { loadContainers(); }, 500);
        }).catch(function(err) {
          btn.disabled = false;
          btn.textContent = original;
          UI.setStyle(status, 'color', 'var(--red, #f87171)');
          // err.message is already the server's classified message; err.detail may carry help
          var help = (err && err.detail && err.detail.help) ? ' \u2014 ' + err.detail.help : '';
          status.textContent = '\u2717 ' + (err.message || 'Reconnect failed') + help;
        });
      });
      btnWrap.appendChild(btn);
      btnWrap.appendChild(status);
      errDiv.appendChild(btnWrap);
    } else {
      // Socket looks tunnel-like but server has no stored target (e.g. env-configured
      // DOCKER_HOST pointing at a user-managed socket). Minimal guidance, no form.
      desc.textContent = 'SKIFF is configured to use a tunnel socket at ' + (_appDockerHost || '') + '. Open your SSH tunnel, then reload this page.';
      errDiv.appendChild(desc);
    }
  }).catch(function() {
    // Status fetch itself failed (e.g. session expired) — fall back to generic hint.
    while (errDiv.firstChild) errDiv.removeChild(errDiv.firstChild);
    var h = document.createElement('h3');
    h.textContent = 'Cannot reach Docker engine';
    errDiv.appendChild(h);
    var d = document.createElement('p');
    UI.setStyle(d, 'margin-top:8px;max-width:480px');
    d.textContent = 'Could not query tunnel status. Try reloading the page.';
    errDiv.appendChild(d);
  });
}


// ── Containers ──
/**
 * Load the container list from the API, render it into the containers page, and wire up
 * search/sort controls and the auto-refresh interval. Guards against concurrent refreshes.
 */
async function loadContainers() {
  if (_refreshInFlight) return;
  _refreshInFlight = true;
  var main = document.getElementById('main');
  if (!_lastContainers) main.innerHTML = '<div class="refreshing">Loading containers...</div>';
  try {
    var containers = await apiFetch(API + '/containers');
    _lastContainers = containers;
    _refreshInFlight = false;
    if (currentPage !== 'containers') return;
    // Guard against the 5s refresh-timer race: a loadContainers() in
    // flight when the user clicks Logs/Terminal/Inspect will resolve
    // AFTER showDetail() cleared the timer, and without this check
    // would stomp on the detail view (main.innerHTML wiped and
    // replaced with the list). showDetail mounts #detail-content;
    // its presence means the user has navigated away from the list.
    if (document.getElementById('detail-content')) return;
    renderContainers(containers);
    clearInterval(refreshTimer);
    refreshTimer = managedInterval(loadContainers, CONTAINERS_POLL_MS);
  } catch (e) {
    _refreshInFlight = false;
    // If apiFetch already redirected to login (401), showLogin() has written
    // the login form into #main. Don't stomp on it with the docker-down UI.
    // Also don't schedule a refresh timer — the user needs to sign in first.
    if (e && e.message === 'Authentication required') {
      clearInterval(refreshTimer);
      return;
    }
    // Flip the sidebar status on any refresh failure — not just 503 (which
    // apiFetch handles directly). Before this, mid-session 500s / network
    // timeouts left cached data on-screen with no user-visible error, so
    // users didn't know the list was stale. setDockerStatus will reset to ok
    // on the next successful refresh.
    setDockerStatus(false, e.message || 'Container list refresh failed');
    if (!_lastContainers) {
      _renderUnreachableDocker(main);
    }
    clearInterval(refreshTimer);
    refreshTimer = managedInterval(loadContainers, CONTAINERS_POLL_MS);
  }
}

var _containerSearch = '';

// Open a modal to commit a running/exited container to a new image.
// Backed by POST /api/containers/{id}/commit.
function _showCommitModal(c) {
  UI.formModal({
    title: 'Commit "' + c.name + '" to image',
    fields: [
      {
        name: 'repository', label: 'Image repository',
        required: true,
        value: 'local/' + (c.name || 'commit'),
        help: 'Lowercase, with optional `user/` or `host/` prefix. The resulting image is LOCAL only — push it separately via the Images page.',
      },
      {
        name: 'tag', label: 'Tag', value: 'latest',
        help: 'Docker tag grammar: letters, digits, `.`, `-`, `_`.',
      },
      {
        name: 'message', label: 'Commit message (optional)',
        placeholder: 'installed vim',
        help: 'Appears in the image history.',
      },
      {
        name: 'author', label: 'Author (optional)',
        placeholder: 'jane@example.com',
      },
    ],
    submitLabel: 'Commit',
    onSubmit: function(values) {
      var params = new URLSearchParams({
        repository: values.repository,
        tag: values.tag || 'latest',
      });
      if (values.message) params.set('message', values.message);
      if (values.author) params.set('author', values.author);
      return apiFetch(API + '/containers/' + c.id + '/commit?' + params.toString(),
                      { method: 'POST' }).then(function(r) {
        toast('Committed ' + c.name + ' → ' + r.repository + ':' + r.tag, 'success');
      });
    },
  });
}
// Bulk-selection state for the containers page. Cleared on page switch
// via showPage(), replaced when the user clicks the "select all" header.
var _bulkSelected = new Set();
var _containerSort = 'name';
var _containerSortDir = 1;
function sortContainers(arr, key, dir) {
  return arr.slice().sort(function(a, b) {
    var va = key === 'created' ? new Date(a.created).getTime() : (a[key] || '').toString().toLowerCase();
    var vb = key === 'created' ? new Date(b.created).getTime() : (b[key] || '').toString().toLowerCase();
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
}
/**
 * Build and insert the containers table into the DOM from an API response array.
 * Applies the current search filter and sort order, wires action buttons, and
 * sets up the detail-panel click handlers.
 * @param {Array<Object>} containers - Container objects from GET /api/containers
 */
function renderContainers(containers) {
  var main = document.getElementById('main');
  main.innerHTML = '';

  var header = document.createElement('div');
  header.className = 'page-header';
  var h2 = document.createElement('h2');
  h2.textContent = 'Containers (' + containers.length + ')';
  var actions = document.createElement('div');
  actions.className = 'header-actions';
  var search = document.createElement('input');
  search.className = 'search-bar';
  search.placeholder = 'Search containers...';
  search.value = _containerSearch;
  search.oninput = function() { _containerSearch = search.value; renderContainers(_lastContainers); };
  actions.append(search, makeBtn('Run new container', function() { showRunModal(); }, 'btn primary'));
  header.append(h2, actions);
  main.appendChild(header);

  var filtered = containers;
  if (_containerSearch) {
    var q = _containerSearch.toLowerCase();
    filtered = containers.filter(function(c) { return c.name.toLowerCase().includes(q) || c.image.toLowerCase().includes(q) || c.id.includes(q); });
  }

  if (filtered.length === 0) {
    var empty = document.createElement('div'); empty.className = 'empty-state';
    empty.innerHTML = containers.length === 0 ? '<h3>No containers</h3><p style="margin-top:8px">No containers found on the connected Docker engine.</p>' : '<h3>No matches</h3><p>No containers match your search.</p>';
    if (containers.length === 0) { var runBtn = makeBtn('Run new container', function() { showRunModal(); }, 'btn primary'); UI.setStyle(runBtn, 'marginTop', '16px'); empty.appendChild(runBtn); }
    main.appendChild(empty); return;
  }

  filtered = sortContainers(filtered, _containerSort, _containerSortDir);

  // Bulk action bar — renders above the table when any row is selected.
  // Lets the operator stop/start/restart/delete N containers in one action.
  function _renderBulkBar(visibleIds) {
    // Drop any selection that's no longer visible (filtered out) so the
    // displayed count matches reality.
    _bulkSelected = new Set([..._bulkSelected].filter(function(id) {
      return visibleIds.has(id);
    }));
    var existing = main.querySelector('.bulk-bar');
    if (existing) existing.remove();
    if (!_bulkSelected.size) return;
    var bar = document.createElement('div');
    bar.className = 'bulk-bar';
    UI.setStyle(bar, 'background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap');
    var lbl = document.createElement('span'); UI.setStyle(lbl, 'font-size:13px;font-weight:500');
    lbl.textContent = _bulkSelected.size + ' selected';
    bar.appendChild(lbl);
    // Capture each per-item rejection with its server envelope message, then
    // summarise: N succeeded + list of failed names + the first server-side
    // reason. The user reads WHY one container refused to stop (e.g.
    // "container already stopped") and can act per-item.
    function _doBulk(label, path, successVerb) {
      bar.appendChild(makeActionBtn(label, function() {
        var ids = [..._bulkSelected];
        if (!confirm(label + ' ' + ids.length + ' container(s)?')) throw new Error('Cancelled');
        return Promise.all(ids.map(function(id) {
          return apiFetch(API + '/containers/' + id + '/' + path, { method: 'POST' })
            .then(function() { return { id: id, ok: true }; })
            .catch(function(e) { return { id: id, ok: false, err: (e && e.message) || 'failed' }; });
        })).then(function(results) {
          var failed = results.filter(function(r) { return !r.ok; });
          var okCount = results.length - failed.length;
          if (failed.length === 0) {
            toast(successVerb + ' ' + okCount + ' container(s)', 'success');
          } else if (okCount === 0) {
            toast(label + ' failed for all ' + failed.length + ' container(s): ' + failed[0].err, 'error');
          } else {
            toast(successVerb + ' ' + okCount + ', ' + failed.length + ' failed: ' + failed[0].err, 'error');
          }
          _bulkSelected = new Set();
          loadContainers();
        });
      }, 'btn small'));
    }
    _doBulk('Start', 'start', 'Started');
    _doBulk('Stop', 'stop', 'Stopped');
    _doBulk('Restart', 'restart', 'Restarted');
    bar.appendChild(makeActionBtn('Delete', function() {
      var ids = [..._bulkSelected];
      if (!confirm('Delete ' + ids.length + ' container(s)? Running ones will be force-killed and CANNOT be undone.'))
        throw new Error('Cancelled');
      return Promise.all(ids.map(function(id) {
        return apiFetch(API + '/containers/' + id + '?force=true', { method: 'DELETE' })
          .then(function() { return { id: id, ok: true }; })
          .catch(function(e) { return { id: id, ok: false, err: (e && e.message) || 'failed' }; });
      })).then(function(results) {
        var failed = results.filter(function(r) { return !r.ok; });
        var okCount = results.length - failed.length;
        if (failed.length === 0) {
          toast('Deleted ' + okCount + ' container(s)', 'success');
        } else if (okCount === 0) {
          toast('Delete failed for all ' + failed.length + ': ' + failed[0].err, 'error');
        } else {
          toast('Deleted ' + okCount + ', ' + failed.length + ' failed: ' + failed[0].err, 'error');
        }
        _bulkSelected = new Set();
        loadContainers();
      });
    }, 'btn danger small'));
    var clearBtn = makeBtn('Clear', function() { _bulkSelected = new Set(); renderContainers(_lastContainers); }, 'btn small');
    bar.appendChild(clearBtn);
    main.insertBefore(bar, main.querySelector('table'));
  }

  var table = document.createElement('table');
  var thead = document.createElement('thead');
  var headerRow = document.createElement('tr');
  // Header checkbox — toggles select-all among visible rows.
  var thChk = document.createElement('th');
  UI.setStyle(thChk, 'width:24px');
  var headerChk = document.createElement('input');
  headerChk.type = 'checkbox';
  headerChk.setAttribute('aria-label', 'Select all containers');
  headerChk.title = 'Select all visible';
  thChk.appendChild(headerChk);
  headerRow.appendChild(thChk);
  [['Name','name'],['Image','image'],['Status','state'],['Ports',null],['Created','created'],['Actions',null]].forEach(function(col) {
    var th = document.createElement('th');
    th.textContent = col[0];
    if (col[1]) {
      UI.setStyle(th, 'cursor', 'pointer');
      if (_containerSort === col[1]) th.textContent += _containerSortDir === 1 ? ' \u25B2' : ' \u25BC';
      th.onclick = function() {
        if (_containerSort === col[1]) _containerSortDir *= -1;
        else { _containerSort = col[1]; _containerSortDir = 1; }
        renderContainers(_lastContainers);
      };
    }
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  table.appendChild(thead);
  var tbody = document.createElement('tbody');
  var visibleIds = new Set(filtered.map(function(c) { return c.id; }));
  // Context menu — right-click a row to mirror the action buttons. Same
  // verbs as the btn-group, but reachable without scrolling the row into
  // view. Closed on outside click or Esc.
  function _showCtxMenu(ev, c) {
    ev.preventDefault();
    var existing = document.querySelector('.ctx-menu');
    if (existing) existing.remove();
    var menu = document.createElement('div');
    menu.className = 'ctx-menu';
    menu.setAttribute('data-testid', 'container-ctx-menu');
    function _item(label, fn, cls) {
      var b = document.createElement('button');
      b.textContent = label;
      if (cls) b.className = cls;
      b.onclick = function() { menu.remove(); fn(); };
      menu.appendChild(b);
    }
    _item('Inspect', function() { showDetail(c.id, c.name, 'inspect'); });
    _item('Logs', function() { showDetail(c.id, c.name, 'logs'); });
    _item('Terminal', function() { showDetail(c.id, c.name, 'terminal'); });
    _item('Stats', function() { showDetail(c.id, c.name, 'stats'); });
    if (c.state === 'running') {
      _item('Stop', function() {
        apiFetch(API + '/containers/' + c.id + '/stop', { method: 'POST' })
          .then(function() { toast(c.name + ' stopped', 'info'); loadContainers(); });
      });
      _item('Restart', function() {
        apiFetch(API + '/containers/' + c.id + '/restart', { method: 'POST' })
          .then(function() { toast(c.name + ' restarted', 'success'); loadContainers(); });
      });
      _item('Pause', function() {
        apiFetch(API + '/containers/' + c.id + '/pause', { method: 'POST' })
          .then(function() { loadContainers(); });
      });
    } else {
      _item('Start', function() {
        apiFetch(API + '/containers/' + c.id + '/start', { method: 'POST' })
          .then(function() { toast(c.name + ' started', 'success'); loadContainers(); });
      });
    }
    _item('Commit to image\u2026', function() { _showCommitModal(c); });
    _item('Delete', function() {
      var needsStop = c.state === 'running' || c.state === 'paused';
      if (!confirm('Delete "' + c.name + '"?')) return;
      // For running/paused containers, stop first so soft-delete (no
      // force) can take the undoable path. Undo restores the container
      // in its stopped state; user can start it back with one click.
      var stopStep = needsStop
        ? apiFetch(API + '/containers/' + c.id + '/stop', { method: 'POST' })
        : Promise.resolve();
      stopStep
        .catch(function() { /* tolerate already-stopping races */ })
        .then(function() {
          undoableDelete(API + '/containers/' + c.id, c.name, loadContainers);
        });
    }, 'danger');
    UI.setStyle(menu, 'left', Math.min(ev.clientX, window.innerWidth - 180) + 'px');
    UI.setStyle(menu, 'top', Math.min(ev.clientY, window.innerHeight - 200) + 'px');
    document.body.appendChild(menu);
    function _off(e) {
      if (menu.contains(e.target)) return;
      menu.remove(); document.removeEventListener('click', _off); document.removeEventListener('contextmenu', _off);
    }
    setTimeout(function() {
      document.addEventListener('click', _off);
      document.addEventListener('contextmenu', _off);
    }, 0);
  }

  headerChk.checked = filtered.length > 0 && filtered.every(function(c) { return _bulkSelected.has(c.id); });
  headerChk.onchange = function() {
    if (headerChk.checked) {
      filtered.forEach(function(c) { _bulkSelected.add(c.id); });
    } else {
      filtered.forEach(function(c) { _bulkSelected.delete(c.id); });
    }
    renderContainers(_lastContainers);
  };
  filtered.forEach(function(c) {
    var tr = document.createElement('tr');
    tr.oncontextmenu = function(ev) { _showCtxMenu(ev, c); };
    var tdChk = document.createElement('td'); UI.setStyle(tdChk, 'width:24px');
    var rowChk = document.createElement('input');
    rowChk.type = 'checkbox';
    rowChk.setAttribute('aria-label', 'Select ' + c.name);
    rowChk.checked = _bulkSelected.has(c.id);
    rowChk.onchange = function() {
      if (rowChk.checked) _bulkSelected.add(c.id);
      else _bulkSelected.delete(c.id);
      _renderBulkBar(visibleIds);
    };
    tdChk.appendChild(rowChk);
    tr.appendChild(tdChk);
    var tdName = document.createElement('td');
    var nd = document.createElement('div'); nd.className = 'container-name'; nd.textContent = c.name;
    var id = document.createElement('div'); id.className = 'container-id'; id.textContent = c.id;
    tdName.append(nd, id);
    var tdImage = document.createElement('td'); UI.setStyle(tdImage, 'font-size:12px;color:var(--muted)'); tdImage.textContent = c.image;
    var tdStatus = document.createElement('td');
    var ss = document.createElement('span'); ss.className = 'status ' + c.state; ss.textContent = c.status;
    tdStatus.appendChild(ss);
    if (c.health && c.health !== 'none') { var hb = document.createElement('span'); hb.className = 'health-badge ' + c.health; hb.textContent = c.health; tdStatus.appendChild(hb); }
    var tdPorts = document.createElement('td');
    var portList = formatPorts(c.ports);
    if (portList) {
      portList.forEach(function(ps, i) {
        if (i > 0) tdPorts.appendChild(document.createTextNode(', '));
        var parts = ps.split(':');
        if (parts.length === 2 && parts[0] !== '0') {
          var a = document.createElement('a');
          a.className = 'port-link';
          a.textContent = ps;
          a.href = 'http://' + (_dockerVmHost || location.hostname) + ':' + parts[0];
          a.target = '_blank';
          a.rel = 'noopener';
          tdPorts.appendChild(a);
        } else {
          var s = document.createElement('span'); UI.setStyle(s, 'font-size:12px;color:var(--muted)'); s.textContent = ps; tdPorts.appendChild(s);
        }
      });
    } else { tdPorts.textContent = '\u2014'; UI.setStyle(tdPorts, 'color', 'var(--muted)'); }
    var tdCreated = document.createElement('td'); tdCreated.className = 'created-time'; tdCreated.textContent = relTime(c.created);
    var tdActions = document.createElement('td');
    var bg = document.createElement('div'); bg.className = 'btn-group';
    if (c.state === 'running') {
      bg.append(
        makeActionBtn('Stop', function() { return guardedAction('stop-c-'+c.id, function() { return apiFetch(API+'/containers/'+c.id+'/stop',{method:'POST'}).then(function(){toast(c.name+' stopped','info');loadContainers();}); }); }, undefined, 'Stopping\u2026'),
        makeActionBtn('Restart', function() { return guardedAction('restart-c-'+c.id, function() { return apiFetch(API+'/containers/'+c.id+'/restart',{method:'POST'}).then(function(){loadContainers();}); }); }, undefined, 'Restarting\u2026'),
        makeActionBtn('Pause', function() { return guardedAction('pause-c-'+c.id, function() { return apiFetch(API+'/containers/'+c.id+'/pause',{method:'POST'}).then(function(){toast(c.name+' paused','info');loadContainers();}); }); }, undefined, 'Pausing\u2026'),
        makeBtn('Logs', function() { showDetail(c.id, c.name, 'logs'); }),
        makeBtn('Terminal', function() { showDetail(c.id, c.name, 'terminal'); }),
        makeBtn('Inspect', function() { showDetail(c.id, c.name, 'inspect'); }),
        makeBtn('Stats', function() { showDetail(c.id, c.name, 'stats'); }),
        makeActionBtn('Kill', function() { if(!confirm('Force kill "'+c.name+'"?'))throw new Error('Cancelled'); return guardedAction('kill-c-' + c.id, function() { return apiFetch(API+'/containers/'+c.id+'/kill',{method:'POST'}).then(function(){toast(c.name+' killed','info');loadContainers();}); }); }, 'btn danger small', 'Killing\u2026'),
      );
    } else if (c.state === 'paused') {
      bg.append(
        makeActionBtn('Unpause', function() { return guardedAction('unpause-c-'+c.id, function() { return apiFetch(API+'/containers/'+c.id+'/unpause',{method:'POST'}).then(function(){loadContainers();}); }); }, 'btn primary', 'Unpausing\u2026'),
        makeBtn('Logs', function() { showDetail(c.id, c.name, 'logs'); }),
        makeBtn('Inspect', function() { showDetail(c.id, c.name, 'inspect'); }),
      );
    } else {
      bg.append(
        makeActionBtn('Start', function() {
          return guardedAction('start-c-'+c.id, function() {
            return apiFetch(API+'/containers/'+c.id+'/start',{method:'POST'}).then(function(){
              return new Promise(function(resolve) { setTimeout(resolve, 600); });
            }).then(function(){
              return apiFetch(API+'/containers/'+c.id+'/inspect');
            }).then(function(data){
              var s = data.state || {};
              if (s.Status === 'exited' || s.Status === 'dead') {
                toast(c.name + ' exited immediately (code ' + (s.ExitCode !== undefined ? s.ExitCode : '?') + ')', 'error');
              } else {
                toast(c.name + ' started', 'success');
              }
              loadContainers();
            });
          });
        }, 'btn primary', 'Starting\u2026'),
        makeBtn('Logs', function() { showDetail(c.id, c.name, 'logs'); }),
        makeBtn('Inspect', function() { showDetail(c.id, c.name, 'inspect'); }),
      );
    }
    bg.appendChild(makeActionBtn('Delete', function() {
      // Backend short-circuits the undo queue when `force=true` is set
      // (see containers.py::delete_container). Running/paused containers
      // are stopped first so the subsequent soft-delete takes the
      // undoable path; undo restores the container in stopped state.
      var needsStop = c.state === 'running' || c.state === 'paused';
      var prompt = needsStop
        ? 'Container "' + c.name + '" is ' + c.state + '. Stop and delete? (Undo available for ~' + UNDO_WINDOW_SECS + 's.)'
        : 'Delete container "' + c.name + '"?';
      if (!confirm(prompt)) throw new Error('Cancelled');
      return guardedAction('del-c-' + c.id, function() {
        var stopStep = needsStop
          ? apiFetch(API + '/containers/' + c.id + '/stop', { method: 'POST' })
              .catch(function() { /* tolerate already-stopping races */ })
          : Promise.resolve();
        return stopStep.then(function() {
          return undoableDelete(API + '/containers/' + c.id, c.name, loadContainers);
        });
      });
    }, 'btn danger'));
    tdActions.appendChild(bg);
    // Leading checkbox column was pushed into the row earlier; name →
    // actions come next in the stable column order.
    tr.append(tdName, tdImage, tdStatus, tdPorts, tdCreated, tdActions);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  main.appendChild(table);
  _renderBulkBar(visibleIds);
}

// ── Detail view ──
function showDetail(id, name, tab) {
  clearInterval(refreshTimer); _refreshInFlight = false;
  // Kill every managed refresh interval from the previous tab —
  // showStatsContent / showProcessesContent / etc. arm their own 3s
  // poll via managedInterval() and, without this line, the old
  // tab's refresh keeps firing against #detail-content AFTER the
  // new tab has rendered into it, stomping on the display (e.g.
  // switching Stats → Terminal overwrote the terminal with the
  // stats grid every 3 seconds).
  clearAllIntervals();
  var main = document.getElementById('main');
  // `main._ws` holds the CURRENT tab's WS. For logs we always close
  // on tab-switch (a fresh tail starts on re-entry). The terminal WS
  // now lives inside the iframe's window — stashing the iframe in
  // body (below) keeps it attached so its WS doesn't die.
  if (main._ws) {
    try { main._ws.close(); } catch(e) {}
    main._ws = null;
  }
  // Hide the body-level terminal overlay before wiping #main so it
  // doesn't paint over the new tab's content. The iframe itself lives
  // in `#_term-host` (not in main), so the wipe doesn't touch it —
  // its contentWindow, WebSocket, and xterm scrollback survive.
  // If the new tab is Terminal again, showShellContent re-shows the
  // overlay and reattaches to the new slot.
  _hideTermHost();
  main.innerHTML = '';
  var header = document.createElement('div'); header.className = 'page-header';
  var h2 = document.createElement('h2'); h2.textContent = name;
  header.append(h2, makeBtn('Back to list', function() { showPage('containers'); }));
  main.appendChild(header);
  var tabs = document.createElement('div'); tabs.className = 'detail-tabs';
  ['logs','terminal','inspect','stats','processes','files'].forEach(function(t) {
    var d = document.createElement('div'); d.className = 'detail-tab' + (t === tab ? ' active' : '');
    d.textContent = t.charAt(0).toUpperCase() + t.slice(1);
    d.onclick = function() { showDetail(id, name, t); };
    tabs.appendChild(d);
  });
  main.appendChild(tabs);
  var content = document.createElement('div'); content.id = 'detail-content'; main.appendChild(content);
  if (tab === 'logs') showLogsContent(id, name);
  else if (tab === 'terminal') showShellContent(id);
  else if (tab === 'inspect') showInspectContent(id);
  else if (tab === 'stats') showStatsContent(id);
  else if (tab === 'processes') showProcessesContent(id);
  else if (tab === 'files') showFilesContent(id);
}

// ── Logs with search and download ──
/**
 * Render the log viewer tab for a container: search/filter input, WebSocket stream,
 * and plain-text + JSONL download buttons.
 * @param {string} id - Container short ID
 * @param {string} name - Container name (used in download filename)
 */
function showLogsContent(id, name) {
  var el = document.getElementById('detail-content');
  el.innerHTML = '';
  var toolbar = document.createElement('div'); toolbar.className = 'log-toolbar';
  var searchInp = document.createElement('input'); searchInp.className = 'log-search'; searchInp.placeholder = 'Search logs (regex)...';
  function downloadLogs(fmt) {
    var headers = { 'X-Requested-With': 'ContainerManager' };
    var t = getToken();
    if (t) headers['Authorization'] = 'Bearer ' + t;
    var suffix = fmt === 'jsonl' ? '/logs/download.jsonl' : '/logs/download';
    fetch(API+'/containers/'+id+suffix+'?tail=5000', { headers: headers })
      .then(function(resp) { if (!resp.ok) throw new Error('HTTP '+resp.status); return resp.blob(); })
      .then(function(blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = id+(fmt==='jsonl'?'-logs.jsonl':'-logs.txt'); a.click();
        URL.revokeObjectURL(url);
      }).catch(function(e) { toast('Download failed: '+e.message, 'error'); });
  }
  var dlBtn = makeBtn('Download .txt', function() { downloadLogs('txt'); }, 'btn small');
  var dlJsonlBtn = makeBtn('Download .jsonl', function() { downloadLogs('jsonl'); }, 'btn small');
  toolbar.append(searchInp, dlBtn, dlJsonlBtn);
  el.appendChild(toolbar);
  var viewer = document.createElement('div'); viewer.className = 'log-viewer'; viewer.id = 'log-output';
  viewer.textContent = 'Connecting...';
  el.appendChild(viewer);

  var allLines = [];
  searchInp.oninput = function() {
    var q = searchInp.value;
    if (!q) { viewer.textContent = allLines.join(''); return; }
    try {
      var escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      var re = new RegExp('(' + escaped + ')', 'gi');
      viewer.innerHTML = '';
      allLines.forEach(function(line) {
        re.lastIndex = 0;
        if (re.test(line)) {
          re.lastIndex = 0;
          var parts = line.split(re);
          parts.forEach(function(p) {
            var s = document.createElement('span');
            var testRe = new RegExp('^' + escaped + '$', 'i');
            if (testRe.test(p)) s.className = 'log-match';
            s.textContent = p;
            viewer.appendChild(s);
          });
        }
      });
    } catch(e) { /* invalid regex, ignore */ }
  };

  connectLogsWS(id, 0, allLines, viewer);
}

var MAX_LOG_RECONNECTS = 10;
/**
 * Open a WebSocket to stream container logs. Reconnects automatically with
 * exponential backoff on unexpected close. Appends lines to `allLines` and
 * re-renders `viewer` respecting the current search filter.
 * @param {string} id - Container short ID
 * @param {number} attempt - Current reconnect attempt count (0 = first connect)
 * @param {string[]} allLines - Accumulated log lines (mutated in place)
 * @param {HTMLElement} viewer - DOM element to render lines into
 */
function connectLogsWS(id, attempt, allLines, viewer) {
  if (!document.getElementById('log-output')) return;
  if (attempt >= MAX_LOG_RECONNECTS) {
    allLines.push('\n[Max reconnect attempts reached — container may be removed]\n');
    viewer.textContent = allLines.join('');
    return;
  }
  var delay = Math.min(1000 * Math.pow(2, attempt), 16000);
  // Close any previous WS stored on the log viewer to avoid duplicates
  if (viewer._ws) { try { viewer._ws.close(1000, 'reconnecting'); } catch(e) {} }
  var ws = registerWS(new WebSocket(wsUrl('/ws/logs/' + id)));
  viewer._ws = ws;
  ws.onopen = function() {
    wsAuthOnOpen(ws);
    // First connect: replace the "Connecting..." placeholder with the
    // current (possibly empty) buffer. Without this, a quiet container
    // (no stdout yet) leaves "Connecting..." on screen forever even
    // though the WS is open and waiting for data.
    if (attempt === 0 && viewer.textContent === 'Connecting...') {
      viewer.textContent = allLines.join('');
    }
    if (attempt > 0) { allLines.push('\n[Reconnected]\n'); viewer.textContent = allLines.join(''); }
  };
  ws.onmessage = function(e) {
    if (ws !== viewer._ws) return;  // discard messages from stale sockets
    allLines.push(e.data);
    if (allLines.length > MAX_LOG_LINES) { allLines.splice(0, allLines.length - MAX_LOG_LINES); viewer.textContent = allLines.join(''); }
    else { viewer.textContent += e.data; }
    viewer.scrollTop = viewer.scrollHeight;
  };
  ws.onerror = function() { allLines.push('\n[Connection error]'); viewer.textContent += '\n[Connection error]'; };
  ws.onclose = function(evt) {
    if (ws !== viewer._ws) return;  // stale socket closed, ignore
    if (evt.code === 1000) return;  // clean close (navigation away or disconnect)
    if (evt.code === 4003) {        // session expired — do not reconnect
      // Lockout branch: server carries `ws_auth_lockout:<N>` in
      // evt.reason so the UI paints a banner with the remaining
      // seconds. Non-lockout 4003s fall through to the regular
      // session-expired toast.
      _surfaceWsLockout(evt);
      allLines.push('\n[Session expired — please log in again]\n');
      viewer.textContent = allLines.join('');
      toast('Session expired — please log in again', 'error');
      return;
    }
    if (document.getElementById('log-output')) {
      allLines.push('\n[Reconnecting in '+(delay/1000)+'s...]\n');
      viewer.textContent += '\n[Reconnecting in '+(delay/1000)+'s...]\n';
      setTimeout(function() { connectLogsWS(id, attempt + 1, allLines, viewer); }, delay);
    }
  };
  document.getElementById('main')._ws = ws;
}

// Shared WS 4003 reason parser. The server uses
// `reason = "ws_auth_lockout:<secs>"` only on the auth-lockout branch;
// other 4003s (session expired, origin denied) keep reason empty.
function _surfaceWsLockout(evt) {
  if (!window.statusBanner) return;
  var reason = (evt && evt.reason) ? String(evt.reason) : '';
  if (!reason.startsWith('ws_auth_lockout:')) return;
  var secs = parseInt(reason.split(':', 2)[1], 10);
  if (!isFinite(secs) || secs <= 0) return;
  window.statusBanner.set('ws_auth_lockout', {
    severity: 'error',
    message: (typeof t === 'function') ? t('banner.ws_auth_lockout') : 'WebSocket locked out — try again in {seconds}s.',
    expiresInMs: secs * 1000,
  });
}

// ── Terminal (iframe-sandboxed xterm.js) ──
//
// xterm.js writes inline element.style assignments during render — a
// pattern the SPA's strict `style-src 'self'` (no 'unsafe-inline')
// CSP would otherwise block. To keep the rest of the SPA under the
// strict policy without losing the terminal, xterm is confined to a
// same-origin iframe served by `GET /api/terminal-frame/{id}`. That
// route ships its own route-scoped CSP that DOES allow 'unsafe-inline'
// for style-src; the relaxation never bleeds into the parent document.
//
// All terminal behaviour (xterm.Terminal, FitAddon, WS reconnect,
// resize, keystroke wiring) lives in `/static/terminal-frame.js` and
// runs inside the iframe's window. The parent just embeds the iframe
// and exchanges intent via postMessage:
//
//   parent ← iframe : { type: 'terminal-ready' }
//                     { type: 'terminal-disconnected', code, reason }
//                     { type: 'terminal-session-expired' }
//                     { type: 'terminal-error', message }
//   iframe ← parent : { type: 'focus' }
//                     { type: 'disconnect' }
//
// Origin checks: the iframe only honours messages from window.parent
// at window.location.origin; the parent only handles messages whose
// `event.origin` equals `window.location.origin`. The route also pins
// `frame-ancestors 'self'` so the page cannot be embedded cross-site.
//
// Per-container session cache: the iframe element is held in
// `_termCache[id]` so Terminal → Logs → Terminal preserves the live
// session. The iframe is detached/re-attached rather than re-created;
// in browsers that re-load on re-attach, terminal-frame.js's
// reconnect-on-close path picks the session back up.
if (!window._termCache) window._termCache = {};

// Stable parent for terminal iframes. Lives at <body> level, so tab
// switches that wipe `#main` never touch it. Moving an <iframe>
// element between parents via appendChild destroys its contentWindow
// (and therefore its WebSocket + xterm scrollback); `Node.moveBefore`
// would preserve it, but as of 2026 Safari doesn't ship moveBefore.
// We sidestep the move entirely: the iframe lives in `#_term-host`
// for its whole lifetime, and we use CSS positioning to overlay it
// onto the `.terminal-slot` placeholder that showShellContent paints
// inside the detail content area.
function _ensureTermHost() {
  var host = document.getElementById('_term-host');
  if (host) return host;
  host = document.createElement('div');
  host.id = '_term-host';
  // Positioned out of flow at body level. Coordinates are recomputed
  // from the active slot's getBoundingClientRect; scroll listeners
  // keep the overlay glued to the slot as the page moves.
  UI.setStyle(host, 'position:absolute;left:0;top:0;width:0;height:0;pointer-events:auto;');
  // Hidden until showShellContent activates a specific container.
  UI.setStyle(host, 'display', 'none');
  document.body.appendChild(host);
  return host;
}

// Reposition `#_term-host` to overlay `slot` exactly. Doc-relative
// coords (left + window.scrollX, top + window.scrollY) so the host
// scrolls with the page even though it lives at body level. Called
// on initial mount and on every resize / scroll event while the
// terminal is active.
function _positionTermHost(slot) {
  var host = _ensureTermHost();
  if (!slot || !slot.isConnected) return;
  var r = slot.getBoundingClientRect();
  UI.setStyle(host, 'top', (r.top + window.scrollY) + 'px');
  UI.setStyle(host, 'left', (r.left + window.scrollX) + 'px');
  UI.setStyle(host, 'width', r.width + 'px');
  UI.setStyle(host, 'height', r.height + 'px');
}

// Tracker for the current slot + observers, so we can swap targets
// when the user moves between containers' Terminal tabs.
window._termActive = window._termActive || { slot: null, ro: null, scrollHandler: null, resizeHandler: null };

function _attachTermToSlot(slot, id) {
  var host = _ensureTermHost();
  UI.setStyle(host, 'display', 'block');
  // Show the requested container's iframe; hide all others.
  if (window._termCache) {
    Object.keys(window._termCache).forEach(function (otherId) {
      var entry = window._termCache[otherId];
      if (!entry || !entry.iframe) return;
      UI.setStyle(entry.iframe, 'display', otherId === id ? 'block' : 'none');
      // Sized to fill the host. Set on every attach because the host
      // dimensions are recomputed from the slot.
      if (otherId === id) {
        UI.setStyle(entry.iframe, 'width:100%;height:100%;border:0;display:block;');
      }
    });
  }
  // Position now, and re-position on the events that can move the slot.
  _positionTermHost(slot);
  // Tear down any prior listeners — we only track ONE slot at a time.
  _detachTermSlotListeners();
  function reposition() { _positionTermHost(slot); }
  window._termActive.slot = slot;
  window._termActive.resizeHandler = reposition;
  window._termActive.scrollHandler = reposition;
  window.addEventListener('resize', reposition);
  window.addEventListener('scroll', reposition, true);  // capture: catch
                                                        // scroll on inner
                                                        // scrollable
                                                        // ancestors too
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(reposition);
    ro.observe(slot);
    ro.observe(document.documentElement);
    window._termActive.ro = ro;
  }
}

function _detachTermSlotListeners() {
  var a = window._termActive;
  if (!a) return;
  if (a.resizeHandler) window.removeEventListener('resize', a.resizeHandler);
  if (a.scrollHandler) window.removeEventListener('scroll', a.scrollHandler, true);
  if (a.ro) { try { a.ro.disconnect(); } catch (e) { /* ignore */ } }
  a.slot = null;
  a.ro = null;
  a.resizeHandler = null;
  a.scrollHandler = null;
}

// Called from any flow that leaves the Terminal-active state (showDetail
// to a non-terminal tab, showPage navigating away, sign-out). Hides
// the host overlay but DOES NOT touch the cached iframes — they keep
// their contentWindow (and WS + scrollback) alive so a return visit
// reattaches the same session.
function _hideTermHost() {
  var host = document.getElementById('_term-host');
  if (host) UI.setStyle(host, 'display', 'none');
  _detachTermSlotListeners();
}

function _termCacheClose(id) {
  // Hard-close: drop the cached iframe entirely (called when user
  // clicks Disconnect or navigates back to the container list).
  var c = window._termCache[id];
  if (!c) return;
  if (c.iframe) {
    // Politely tell the iframe to close its WS before we detach.
    try {
      c.iframe.contentWindow.postMessage(
        { type: 'disconnect' },
        window.location.origin,
      );
    } catch (e) { /* iframe may have already navigated away */ }
    if (c.iframe.parentNode) {
      try { c.iframe.parentNode.removeChild(c.iframe); } catch (e) { /* ignore */ }
    }
  }
  delete window._termCache[id];
}

function showShellContent(id) {
  var el = document.getElementById('detail-content');
  el.innerHTML = '';
  UI.setStyle(el, 'position', 'relative');

  // Plant the slot — a 380px-tall placeholder div that reserves layout
  // space for the terminal overlay. The slot lives inside detail-content
  // (and gets wiped along with everything else on tab switch); the
  // iframe overlay itself lives in `#_term-host` at body level so it
  // survives tab switches without DOM moves (Safari has no
  // Node.moveBefore yet, and plain appendChild destroys the iframe's
  // contentWindow). Slot id="term-output" is preserved so existing
  // selectors keep working.
  var slot = document.createElement('div');
  slot.id = 'term-output';
  slot.className = 'terminal terminal-slot';
  el.appendChild(slot);

  // First-time mount: create the iframe inside the stable host so it
  // can be reused across every subsequent showShellContent call for
  // this container.
  var cached = window._termCache[id];
  if (!cached || !cached.iframe) {
    var host = _ensureTermHost();
    var iframe = document.createElement('iframe');
    iframe.src = '/api/terminal-frame/' + encodeURIComponent(id);
    iframe.title = 'Container shell';
    // No class — sizing is set on the host via _attachTermToSlot. The
    // old `.terminal-iframe` rule pins a 380px height which now lives
    // on the slot (the layout-space owner) instead.
    // Restrictive sandbox: allow-scripts for xterm.js, allow-same-origin
    // for sessionStorage AUTH + same-origin WS to /ws/exec/. Nothing
    // else — no popups, no top-nav, no form submission.
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
    host.appendChild(iframe);
    cached = { iframe: iframe };
    window._termCache[id] = cached;
  }

  // Overlay the host on the slot and start tracking layout changes.
  _attachTermToSlot(slot, id);

  // Disconnect button lives INSIDE `#_term-host`, on top of the iframe.
  // Putting it in detail-content would stack it under the body-level
  // overlay (different stacking contexts; document-order siblings stack
  // later siblings on top), so the button would be invisible.
  // Inside the host, both share the same context and z-index:3 keeps it
  // above the iframe. Recreated per `showShellContent` call so the
  // closure captures the right container id; the host is cleared of
  // any previous detached button below before re-mount.
  var host = _ensureTermHost();
  // Remove any previous Disconnect button (one per active container);
  // keep only the iframe(s) themselves.
  Array.prototype.slice.call(host.querySelectorAll('button.btn')).forEach(function (b) {
    if (b.parentNode === host) host.removeChild(b);
  });
  var disconnectBtn = makeBtn('Disconnect', function() {
    _termCacheClose(id);
  }, 'btn small danger');
  UI.setStyle(disconnectBtn, 'position:absolute;top:8px;right:8px;z-index:3');
  host.appendChild(disconnectBtn);

  // Single message listener per parent. Tracked on window so re-entry
  // doesn't stack duplicates that double-handle every event.
  if (!window._termFrameListenerInstalled) {
    window._termFrameListenerInstalled = true;
    window.addEventListener('message', _onTerminalFrameMessage);
  }

  // Defer focus so the iframe has time to attach its own message
  // listener before we send the focus command.
  setTimeout(function () {
    try {
      cached.iframe.contentWindow.postMessage(
        { type: 'focus' },
        window.location.origin,
      );
    } catch (e) { /* iframe still loading — terminal-frame.js focuses
                     itself on init anyway, so this is best-effort */ }
  }, 200);
}

function _onTerminalFrameMessage(e) {
  // Origin pin: only honour messages from the same origin (the iframe).
  // A cross-origin embedder would post an `origin` of "null" or its own
  // host; rejecting those is the parent half of the protocol.
  if (e.origin !== window.location.origin) return;
  var data = e.data || {};
  if (data.type === 'terminal-session-expired') {
    // Reuse the existing session-expiry surface so the user sees the
    // same banner + toast they'd see for an API call that 401s.
    toast('Session expired — please log in again', 'error');
    if (typeof _surfaceWsLockout === 'function') {
      _surfaceWsLockout({ code: 4003, reason: 'session-expired' });
    }
  } else if (data.type === 'terminal-error') {
    // Most terminal-error events are transient WS hiccups that the
    // iframe's own reconnect handles. Log to console for debug; only
    // surface a toast when the iframe declared a fatal failure.
    try { console.warn('terminal-frame:', data.message); } catch (err) { /* ignore */ }
  }
  // 'terminal-ready' and 'terminal-disconnected' are informational —
  // the iframe shows its own status banner inside the frame, so the
  // parent doesn't need to surface them.
}

// ── Inspect ──
// Convert bytes to a compact GCP/Kubernetes-style quantity ("256Mi", "1Gi").
// Uses IEC binary units since that's what Docker reports internally.
function _fmtBytes(n) {
  n = Number(n) || 0;
  if (n === 0) return '0';
  var units = ['', 'Ki', 'Mi', 'Gi', 'Ti'];
  var i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  // 1 decimal unless it would end in .0
  var s = (n % 1 === 0) ? n.toFixed(0) : n.toFixed(1);
  return s + units[i];
}
// CpuQuota/CpuPeriod → fractional CPUs. Period=0 means no quota set.
function _fmtCpus(quota, period) {
  if (!quota || !period) return '';
  return (quota / period).toFixed(2).replace(/\.00$/, '');
}

async function showInspectContent(id) {
  var el = document.getElementById('detail-content');
  el.innerHTML = '<div class="refreshing">Loading...</div>';
  try {
    var d = await apiFetch(API+'/containers/'+id+'/inspect');
    el.innerHTML = '';
    var panel = document.createElement('div'); panel.className = 'inspect-panel';
    // Thin wrapper around UI.kvSection so the existing call sites keep their
    // "addSection(title, entries)" shape — entries are [label, value] pairs
    // per the legacy convention.
    function addSection(title, entries) {
      panel.appendChild(UI.kvSection(title, entries));
    }
    // Rename + Clone row
    var actionRow = document.createElement('div'); UI.setStyle(actionRow, 'margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap');
    var renameInp = document.createElement('input'); renameInp.value = d.name; UI.setStyle(renameInp, 'padding:5px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px;width:200px');
    actionRow.append(renameInp, makeActionBtn('Rename', function() {
      var newName = renameInp.value;
      if (newName === d.name) throw new Error('Name unchanged');
      return guardedAction('rename-c-'+id, function() {
        return apiFetch(API+'/containers/'+id+'/rename?name='+encodeURIComponent(newName),{method:'POST'}).then(function(){toast('Renamed to '+newName,'success');showDetail(id, newName, 'inspect');});
      });
    }, 'btn small'));
    // Clone with changes — opens the Run modal pre-filled from this container.
    // Immutable params (ports/volumes/env/network/command/read-only/tmpfs) become
    // editable there; the server uses inherit_from for env preservation.
    var cloneBtn = document.createElement('button');
    cloneBtn.className = 'btn small';
    cloneBtn.textContent = 'Clone with changes';
    cloneBtn.title = 'Open Run modal pre-filled with this container\'s config — edit any field and launch a copy, optionally replacing this one.';
    cloneBtn.addEventListener('click', function() { showRunModal(null, d); });
    actionRow.append(cloneBtn);
    panel.appendChild(actionRow);
    addSection('General', [['ID',d.id],['Name',d.name],['Image',d.image],['Created',d.created],['Status',d.state.Status],['PID',d.state.Pid],['Restarts',d.restart_count],['Platform',d.platform]]);
    addSection('Config', [['Command',(d.config.cmd||[]).join(' ')],['Entrypoint',(d.config.entrypoint||[]).join(' ')],['Working Dir',d.config.working_dir],['User',d.config.user||'(default)'],['Hostname',d.config.hostname]]);
    // Editable Resources section — uses POST /api/containers/{id}/update for the
    // live-mutable fields. Values round-trip through GCP-style units (Mi/Gi, 0.5)
    // to keep the UX familiar for Cloud Run / GKE users.
    _renderEditableResources(panel, d, id);
    if (d.config.env && d.config.env.length) { addSection('Environment', d.config.env.map(function(e) { var p = e.split('='); return [p[0], p.slice(1).join('=')]; })); }
    if (d.mounts && d.mounts.length) { addSection('Mounts', d.mounts.map(function(m) { return [m.destination, m.source+' ('+m.type+(m.rw?',rw':',ro')+')']; })); }
    if (d.health_check && d.health_check.status !== 'none') {
      var hcEntries = [['Status', d.health_check.status],['Failing Streak', d.health_check.failing_streak]];
      if (d.health_check.test) hcEntries.unshift(['Test', d.health_check.test.join(' ')]);
      if (d.health_check.log && d.health_check.log.length) {
        d.health_check.log.forEach(function(l,i) { hcEntries.push(['Probe '+(i+1), 'Exit: '+l.ExitCode+' — '+(l.Output||'').substring(0,200)]); });
      }
      addSection('Health Check', hcEntries);
    }
    if (Object.keys(d.network).length) { addSection('Networks', Object.entries(d.network).map(function(e) { return [e[0], 'IP: '+e[1].ip_address+' GW: '+e[1].gateway]; })); }
    el.appendChild(panel);
  } catch (e) { el.textContent = 'Error: ' + e.message; }
}

// Render the Resources section with inline-editable fields for live-updatable
// constraints (memory, cpus, pids_limit, restart_policy). Immutable fields
// (read-only FS, security_opt, tmpfs) appear below as read-only. Editing any
// field enables Save/Cancel; Save issues a single PATCH-like POST with only the
// changed fields in the body. Zero-trust: token carried by apiFetch, CSRF set.
function _renderEditableResources(panel, d, containerId) {
  var hc = d.host_config || {};
  var sec = document.createElement('div'); sec.className = 'inspect-section';
  var hdr = document.createElement('div');
  UI.setStyle(hdr, 'display:flex;align-items:center;gap:10px;margin-bottom:6px;');
  var h4 = document.createElement('h4'); h4.textContent = 'Resources';
  UI.setStyle(h4, 'margin:0;');
  var badge = document.createElement('span');
  badge.textContent = 'Live-updatable';
  UI.setStyle(badge, 'font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;padding:2px 8px;border-radius:12px;background:#1e3a5f;color:#93c5fd;');
  badge.title = 'These fields can be changed without recreating the container.';
  hdr.append(h4, badge);
  sec.appendChild(hdr);

  // Current values (GCP-style display)
  var curMem = _fmtBytes(hc.memory_bytes);
  var curMemRes = _fmtBytes(hc.memory_reservation_bytes);
  var curCpus = _fmtCpus(hc.cpu_quota, hc.cpu_period || 100000);
  var curCpuShares = hc.cpu_shares || '';
  var curPids = hc.pids_limit || '';
  var curRp = (hc.restart_policy && hc.restart_policy.Name) || 'no';
  var curRetry = (hc.restart_policy && hc.restart_policy.MaximumRetryCount) || '';

  // Helpers to build an editable row. The label cell gets a UI.helpIcon with
  // the per-field "why this matters" explanation, rendered as a native
  // `title` tooltip — reduces the need to read docs for each setting.
  var inputs = {};
  function editRow(label, key, value, placeholder, hint, helpText) {
    var k = UI.el('div', { class: 'k' },
      UI.el('span', { text: label }),
      helpText ? UI.helpIcon(helpText) : null,
    );
    var inp = UI.el('input', {
      type: 'text', value: value, placeholder: placeholder || '',
      style: 'padding:4px 8px;border:1px solid var(--border);border-radius:4px;'
           + 'font-size:12px;font-family:monospace;width:160px;'
           + 'background:var(--card);color:var(--text)',
      on: { input: function() { updateButtons(); } },
    });
    inp.dataset.originalValue = value;
    inputs[key] = inp;
    var v = UI.el('div', { class: 'v', style: 'display:flex;flex-direction:column;gap:3px' }, inp);
    if (hint) {
      v.appendChild(UI.el('div', { style: 'font-size:10px;color:var(--muted)', text: hint }));
    }
    sec.appendChild(UI.el('div', { class: 'inspect-kv' }, k, v));
  }

  editRow('Memory limit', 'memory', curMem, '256Mi, 1Gi, 512M',
    'IEC (Mi/Gi) or decimal (M/G). Empty = no limit.',
    'Hard ceiling on container memory. Container is killed if it exceeds this. Accepts 256Mi, 1Gi, or decimal units like 512M. Cannot exceed SKIFF\'s global cap (MAX_CONTAINER_MEM).');
  editRow('Memory reservation', 'memory_reservation', curMemRes, '128Mi',
    'Soft limit; container starts being killed above this under pressure.',
    'Soft memory floor. The engine tries to keep at least this much available to the container even when the host is under memory pressure. Lower than Memory limit.');
  editRow('CPUs', 'cpus', curCpus, '0.5, 1, 500m',
    'Fractional cores. "500m" = 0.5 CPU (Kubernetes style).',
    'Fractional CPUs. Under the hood this sets cpu_quota / cpu_period. 1 = one full core. 0.5 or 500m = half a core. Cannot exceed SKIFF\'s global cap (MAX_CONTAINER_CPU).');
  editRow('CPU shares', 'cpu_shares', curCpuShares, '1024',
    'Relative weight 2-1024 (advanced; leave blank for default).',
    'Relative CPU weight when multiple containers contend for CPU. 1024 is the default. Value only matters at saturation. Leave blank unless you\'re tuning multi-container workloads.');
  editRow('PIDs limit', 'pids_limit', curPids, '100',
    'Max processes inside container.',
    'Cap on the number of processes/threads the container can spawn. Defends against fork-bombs. Typical web services need 50-200.');

  // Restart policy — select + optional retry
  var rpRow = document.createElement('div'); rpRow.className = 'inspect-kv';
  var rpK = document.createElement('div'); rpK.className = 'k'; rpK.textContent = 'Restart policy';
  var rpV = document.createElement('div'); rpV.className = 'v';
  UI.setStyle(rpV, 'display:flex;gap:6px;align-items:center;');
  var rpSel = document.createElement('select');
  UI.setStyle(rpSel, 'padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;background:var(--card);color:var(--text);');
  ['no', 'on-failure', 'unless-stopped', 'always'].forEach(function(n) {
    var o = document.createElement('option'); o.value = n; o.textContent = n;
    if (n === curRp) o.selected = true;
    rpSel.appendChild(o);
  });
  rpSel.dataset.originalValue = curRp;
  var rpRetry = document.createElement('input');
  rpRetry.type = 'number'; rpRetry.min = '0'; rpRetry.max = '5';
  rpRetry.value = curRetry; rpRetry.placeholder = 'retries';
  UI.setStyle(rpRetry, 'padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:12px;width:80px;background:var(--card);color:var(--text);');
  rpRetry.dataset.originalValue = String(curRetry);
  UI.setStyle(rpRetry, 'display', (curRp === 'on-failure') ? 'inline-block' : 'none');
  rpSel.addEventListener('change', function() {
    UI.setStyle(rpRetry, 'display', (rpSel.value === 'on-failure') ? 'inline-block' : 'none');
    updateButtons();
  });
  rpRetry.addEventListener('input', updateButtons);
  inputs.restart_policy_name = rpSel;
  inputs.restart_policy_retry = rpRetry;
  rpV.append(rpSel, rpRetry);
  rpRow.append(rpK, rpV); sec.appendChild(rpRow);

  // Save / Cancel buttons — hidden until a change is made
  var btnRow = document.createElement('div');
  UI.setStyle(btnRow, 'margin-top:10px;display:flex;gap:8px;align-items:center;');
  var saveBtn = document.createElement('button');
  saveBtn.className = 'btn primary small';
  saveBtn.textContent = 'Save changes';
  saveBtn.disabled = true;
  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn small';
  cancelBtn.textContent = 'Revert';
  cancelBtn.disabled = true;
  var status = document.createElement('span');
  UI.setStyle(status, 'font-size:12px;color:var(--muted);');
  btnRow.append(saveBtn, cancelBtn, status);
  sec.appendChild(btnRow);

  function hasChanges() {
    for (var key in inputs) {
      if (inputs[key].value !== inputs[key].dataset.originalValue) return true;
    }
    return false;
  }
  function updateButtons() {
    var changed = hasChanges();
    saveBtn.disabled = !changed;
    cancelBtn.disabled = !changed;
  }
  cancelBtn.addEventListener('click', function() {
    for (var key in inputs) { inputs[key].value = inputs[key].dataset.originalValue; }
    UI.setStyle(rpRetry, 'display', (rpSel.value === 'on-failure') ? 'inline-block' : 'none');
    updateButtons();
    status.textContent = '';
  });
  saveBtn.addEventListener('click', function() {
    var body = {};
    // Only send fields that actually changed — minimizes surface and audit noise
    ['memory', 'memory_reservation', 'cpus'].forEach(function(k) {
      var inp = inputs[k];
      if (inp.value !== inp.dataset.originalValue) {
        body[k] = inp.value.trim() || null;
      }
    });
    ['cpu_shares', 'pids_limit'].forEach(function(k) {
      var inp = inputs[k];
      if (inp.value !== inp.dataset.originalValue) {
        var num = inp.value.trim() === '' ? null : parseInt(inp.value, 10);
        if (num !== null && isNaN(num)) {
          UI.setStyle(status, 'color', 'var(--red, #f87171)');
          status.textContent = k + ' must be a whole number';
          throw new Error('invalid ' + k);
        }
        body[k] = num;
      }
    });
    if (rpSel.value !== rpSel.dataset.originalValue ||
        String(rpRetry.value) !== rpRetry.dataset.originalValue) {
      var rp = { Name: rpSel.value };
      if (rpSel.value === 'on-failure') {
        var r = parseInt(rpRetry.value, 10);
        if (!isNaN(r)) rp.MaximumRetryCount = r;
      }
      body.restart_policy = rp;
    }
    if (Object.keys(body).length === 0) { return; }
    saveBtn.disabled = true; cancelBtn.disabled = true;
    UI.setStyle(status, 'color', 'var(--muted)'); status.textContent = 'Saving\u2026';
    apiFetch(API + '/containers/' + containerId + '/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function() {
      UI.setStyle(status, 'color', 'var(--green, #22c55e)');
      status.textContent = '\u2713 Saved';
      // Re-render Inspect to pick up canonicalised values from the server
      setTimeout(function() { showDetail(containerId, d.name, 'inspect'); }, 400);
    }).catch(function(err) {
      saveBtn.disabled = false; cancelBtn.disabled = false;
      UI.setStyle(status, 'color', 'var(--red, #f87171)');
      status.textContent = '\u2717 ' + (err.message || 'Update failed');
    });
  });

  // Read-only (recreate-required) fields rendered below. These are part of the
  // container's create-time config and cannot be live-updated; a future "Clone
  // with changes" button (future) will expose the recreate path.
  var roRow = document.createElement('div'); roRow.className = 'inspect-kv';
  UI.setStyle(roRow, 'margin-top:8px;border-top:1px dashed var(--border);padding-top:8px;');
  var roK = document.createElement('div'); roK.className = 'k'; roK.textContent = 'Read-only rootfs';
  var roV = document.createElement('div'); roV.className = 'v mono';
  roV.textContent = hc.readonly_rootfs ? 'yes' : 'no';
  roRow.append(roK, roV); sec.appendChild(roRow);
  if (hc.security_opt && hc.security_opt.length) {
    var soRow = document.createElement('div'); soRow.className = 'inspect-kv';
    var soK = document.createElement('div'); soK.className = 'k'; soK.textContent = 'Security';
    var soV = document.createElement('div'); soV.className = 'v mono';
    soV.textContent = hc.security_opt.join(', ');
    soRow.append(soK, soV); sec.appendChild(soRow);
  }
  if (hc.tmpfs && Object.keys(hc.tmpfs).length) {
    Object.entries(hc.tmpfs).forEach(function(e) {
      var tmpRow = document.createElement('div'); tmpRow.className = 'inspect-kv';
      var tmpK = document.createElement('div'); tmpK.className = 'k'; tmpK.textContent = 'tmpfs ' + e[0];
      var tmpV = document.createElement('div'); tmpV.className = 'v mono';
      tmpV.textContent = e[1] || '(default opts)';
      tmpRow.append(tmpK, tmpV); sec.appendChild(tmpRow);
    });
  }
  panel.appendChild(sec);
}

// ── Stats ──
async function showStatsContent(id) {
  var el = document.getElementById('detail-content');
  el.innerHTML = '<div class="stats-grid" id="stats-grid"><div class="refreshing">Loading stats...</div></div>';
  var _statsInFlight = false;
  async function refresh() {
    if (_statsInFlight) return;
    _statsInFlight = true;
    try {
      var s = await apiFetch(API+'/containers/'+id+'/stats');
      var grid = document.getElementById('stats-grid');
      if (!grid) return;
      grid.innerHTML = '';
      [['CPU',s.cpu_percent+'%'],['Memory',s.mem_usage_mb+' MB'],['Mem Limit',s.mem_limit_mb+' MB'],['Mem %',s.mem_percent+'%'],['Net RX',s.net_rx_mb+' MB'],['Net TX',s.net_tx_mb+' MB'],['Disk Read',s.blk_read_mb+' MB'],['Disk Write',s.blk_write_mb+' MB']].forEach(function(item) {
        var card = document.createElement('div'); card.className = 'stat';
        var l = document.createElement('div'); l.className = 'label'; l.textContent = item[0];
        var v = document.createElement('div'); v.className = 'value'; v.textContent = item[1];
        card.append(l, v); grid.appendChild(card);
      });
    } catch (e) {
      var grid = document.getElementById('stats-grid');
      if (grid) { grid.innerHTML = ''; var p = document.createElement('p'); UI.setStyle(p, 'color', 'var(--red)'); p.textContent='Error: '+e.message; grid.appendChild(p); }
      if (e.message.indexOf('not running') !== -1 || e.message.indexOf('not found') !== -1 || e.message.indexOf('unreachable') !== -1) clearInterval(refreshTimer);
    } finally { _statsInFlight = false; }
  }
  refresh();
  var statId = managedInterval(function() {
    if (!document.getElementById('detail-content')) { clearInterval(statId); return; }
    refresh();
  }, 3000);
}

// ── Processes (docker top) ──
async function showProcessesContent(id) {
  var el = document.getElementById('detail-content');
  el.innerHTML = '<div class="refreshing">Loading processes...</div>';
  var _topInFlight = false;
  async function refresh() {
    if (_topInFlight) return;
    _topInFlight = true;
    try {
      var data = await apiFetch(API+'/containers/'+id+'/top');
      var el2 = document.getElementById('detail-content');
      if (!el2) return;
      el2.innerHTML = '';
      if (!data.processes || data.processes.length === 0) {
        el2.innerHTML = '<div class="empty-state"><p>No processes running (container may be stopped)</p></div>';
        return;
      }
      var table = document.createElement('table');
      var thead = document.createElement('thead');
      var headerRow = document.createElement('tr');
      (data.titles || []).forEach(function(t) { var th = document.createElement('th'); th.textContent = t; headerRow.appendChild(th); });
      thead.appendChild(headerRow); table.appendChild(thead);
      var tbody = document.createElement('tbody');
      data.processes.forEach(function(proc) {
        var tr = document.createElement('tr');
        proc.forEach(function(val) { var td = document.createElement('td'); td.className = 'mono'; UI.setStyle(td, 'fontSize', '12px'); td.textContent = val; tr.appendChild(td); });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody); el2.appendChild(table);
    } catch (e) {
      var el2 = document.getElementById('detail-content');
      if (el2) { el2.innerHTML = ''; var p = document.createElement('p'); UI.setStyle(p, 'color', 'var(--red)'); p.textContent = e.message; el2.appendChild(p); }
      if (e.message.indexOf('not running') !== -1 || e.message.indexOf('not found') !== -1 || e.message.indexOf('unreachable') !== -1 || e.message.indexOf('conflict') !== -1) clearInterval(refreshTimer);
    } finally { _topInFlight = false; }
  }
  refresh();
  var procId = managedInterval(function() {
    if (!document.getElementById('detail-content')) { clearInterval(procId); return; }
    refresh();
  }, 3000);
}

// ── Files (docker cp / docker diff) ──
//
// The Files tab now has two sub-views:
//   1. **Browser** — navigate the container's live filesystem. Backed
//      by /api/containers/{id}/ls. Click a dir to enter, click a file
//      to download it, drag-drop to upload into the current dir.
//   2. **Changes** — the original `docker diff` output (paths added,
//      modified, or deleted since the image).
// A pair of tabs at the top switches between them.

// Persist the current browsed path per container so re-entering the
// Files tab lands back where the user was (common flow: edit file
// locally, re-upload to same path).
var _filesPath = {};

async function showFilesContent(id) {
  var el = document.getElementById('detail-content');
  el.innerHTML = '';
  var tabBar = document.createElement('div');
  tabBar.className = 'detail-subtabs';
  UI.setStyle(tabBar, 'display:flex;gap:6px;margin-bottom:12px');
  var browserPanel = document.createElement('div');
  var changesPanel = document.createElement('div');
  UI.setStyle(changesPanel, 'display', 'none');
  function _mkSubTab(label, panel, onActivate) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn small';
    b.textContent = label;
    b.onclick = function() {
      [browserPanel, changesPanel].forEach(function(p) { UI.setStyle(p, 'display', 'none'); });
      tabBar.querySelectorAll('button').forEach(function(x) { x.classList.remove('primary'); });
      b.classList.add('primary');
      UI.setStyle(panel, 'display', '');
      if (onActivate) onActivate();
    };
    return b;
  }
  var browseBtn = _mkSubTab('Browse', browserPanel, function() { _renderFileBrowser(id, browserPanel); });
  var diffBtn = _mkSubTab('Changes (docker diff)', changesPanel, function() { _renderDiff(id, changesPanel); });
  tabBar.append(browseBtn, diffBtn);
  el.append(tabBar, browserPanel, changesPanel);
  browseBtn.click();  // default view
}


async function _renderFileBrowser(id, panel) {
  panel.innerHTML = '<div class="refreshing">Loading\u2026</div>';
  var path = _filesPath[id] || '/';

  // Header: breadcrumb, Refresh, Upload, New folder is not supported
  // (docker has no mkdir primitive over cp — require the user to exec).
  var header = document.createElement('div');
  UI.setStyle(header, 'display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap');
  var breadcrumb = document.createElement('div');
  UI.setStyle(breadcrumb, 'flex:1;font-size:13px;font-family:monospace');
  function _paintBreadcrumb(p) {
    breadcrumb.innerHTML = '';
    var parts = p.split('/').filter(Boolean);
    function _crumbLink(label, navPath) {
      var a = document.createElement('a');
      a.href = '#'; a.textContent = label;
      UI.setStyle(a, 'color:var(--accent,#0d9488);text-decoration:none;cursor:pointer');
      a.onclick = function(ev) { ev.preventDefault(); _filesPath[id] = navPath; _renderFileBrowser(id, panel); };
      return a;
    }
    breadcrumb.appendChild(_crumbLink('/', '/'));
    var acc = '';
    parts.forEach(function(p2, i) {
      acc += '/' + p2;
      breadcrumb.appendChild(document.createTextNode(i === 0 ? '' : ' / '));
      breadcrumb.appendChild(_crumbLink(p2, acc));
    });
  }
  _paintBreadcrumb(path);
  var refreshBtn = makeBtn('Refresh', function() { _renderFileBrowser(id, panel); }, 'btn small');
  var uploadBtn = makeBtn('Upload file', function() { _uploadPrompt(id, path, panel); }, 'btn small primary');
  header.append(breadcrumb, refreshBtn, uploadBtn);

  var table = document.createElement('table');
  table.innerHTML = '<thead><tr><th style="width:24px"></th><th>Name</th><th>Size</th><th>Mode</th><th>Actions</th></tr></thead>';
  var tbody = document.createElement('tbody');
  table.appendChild(tbody);

  try {
    var data = await apiFetch(API + '/containers/' + id + '/ls?path=' + encodeURIComponent(path));
    panel.innerHTML = '';
    panel.appendChild(header);

    // Parent-directory hop (unless we're at /).
    if (path !== '/') {
      var parent = path.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/';
      var upTr = document.createElement('tr');
      UI.setStyle(upTr, 'cursor', 'pointer');
      upTr.onclick = function() { _filesPath[id] = parent; _renderFileBrowser(id, panel); };
      upTr.innerHTML = '<td>\u21B0</td><td colspan="4" style="color:var(--muted)">.. (up to ' + esc(parent) + ')</td>';
      tbody.appendChild(upTr);
    }

    var entries = data.entries || [];
    if (!entries.length) {
      var em = document.createElement('tr');
      em.innerHTML = '<td></td><td colspan="4" style="color:var(--muted);padding:12px 0">(empty directory)</td>';
      tbody.appendChild(em);
    } else {
      entries.forEach(function(e) {
        var tr = document.createElement('tr');
        var tdIcon = document.createElement('td');
        tdIcon.textContent = e.type === 'dir' ? '\ud83d\udcc1'
                            : e.type === 'link' ? '\ud83d\udd17'
                            : '\ud83d\udcc4';
        var tdName = document.createElement('td');
        var nameSpan = document.createElement('span');
        nameSpan.textContent = e.name + (e.type === 'link' && e.target ? ' \u2192 ' + e.target : '');
        if (e.type === 'dir' || e.type === 'link') {
          UI.setStyle(nameSpan, 'color:var(--accent,#0d9488);cursor:pointer');
          nameSpan.onclick = function() {
            var next = path.replace(/\/+$/, '') + '/' + e.name;
            _filesPath[id] = next;
            _renderFileBrowser(id, panel);
          };
        }
        tdName.appendChild(nameSpan);
        var tdSize = document.createElement('td');
        tdSize.textContent = e.type === 'file' ? _formatBytes(e.size) : '';
        UI.setStyle(tdSize, 'font-family:monospace;color:var(--muted);font-size:12px');
        var tdMode = document.createElement('td');
        UI.setStyle(tdMode, 'font-family:monospace;font-size:11px;color:var(--muted)');
        tdMode.textContent = e.mode || '';
        var tdAct = document.createElement('td');
        UI.setStyle(tdAct, 'display:flex;gap:4px');
        if (e.type === 'file' || e.type === 'link' || e.type === 'dir') {
          if (e.type !== 'dir') {
            var dlBtn = makeBtn('Download', (function(n) {
              return function() {
                var fullPath = path.replace(/\/+$/, '') + '/' + n;
                // apiFetch sends Authorization; we need raw fetch + download.
                fetch(API + '/containers/' + id + '/files?path=' + encodeURIComponent(fullPath),
                      { headers: { 'Authorization': 'Bearer ' + getToken(),
                                   'X-Requested-With': 'ContainerManager' } })
                  .then(function(r) {
                    if (!r.ok) throw new Error('Download failed: ' + r.status);
                    return r.blob();
                  })
                  .then(function(blob) {
                    var url = URL.createObjectURL(blob);
                    var a = document.createElement('a');
                    a.href = url;
                    a.download = n + '.tar';
                    document.body.appendChild(a); a.click(); document.body.removeChild(a);
                    setTimeout(function() { URL.revokeObjectURL(url); }, 500);
                    toast('Downloaded ' + n, 'success');
                  })
                  .catch(function(err) { toast(err.message, 'error'); });
              };
            })(e.name), 'btn small');
            tdAct.appendChild(dlBtn);
          }
          // Delete (soft by default — server preserves the bytes for the
          // undo window and rm-rfs on expiry). Directories also deletable,
          // same cap applies. Uses the shared renderUndoToast so the UX
          // matches prune / compose down / container delete.
          tdAct.appendChild(makeActionBtn('Delete', (function(n, etype) {
            return function() {
              var fullPath = path.replace(/\/+$/, '') + '/' + n;
              var label = etype === 'dir' ? 'directory' : 'file';
              if (!confirm('Delete ' + label + ' "' + n + '"? (Undo available for ~' + (window.UNDO_WINDOW_SECS || 5) + 's.)'))
                throw new Error('Cancelled');
              return guardedAction('del-file-' + id + '-' + fullPath, function() {
                return apiFetch(
                  API + '/containers/' + id + '/files?path=' + encodeURIComponent(fullPath),
                  { method: 'DELETE' },
                ).then(function(r) {
                  if (r && r.undo_token) {
                    window.renderUndoToast(
                      'Delete "' + n + '"', r.undo_token, r.expires_in,
                      function() { _renderFileBrowser(id, panel); },
                    );
                    return;
                  }
                  toast('Deleted ' + n, 'info');
                  _renderFileBrowser(id, panel);
                });
              });
            };
          })(e.name, e.type), 'btn danger small'));
        }
        tr.append(tdIcon, tdName, tdSize, tdMode, tdAct);
        tbody.appendChild(tr);
      });
    }
    if (data.truncated) {
      var note = document.createElement('p');
      UI.setStyle(note, 'font-size:11px;color:var(--amber,#f59e0b);margin-top:8px');
      note.textContent = 'Directory truncated at ' + entries.length + ' entries. Raise CONTAINER_LS_MAX_ENTRIES on the server to see more.';
      panel.appendChild(note);
    }
    panel.appendChild(table);

    // Drag-and-drop upload target.
    panel.ondragover = function(ev) { ev.preventDefault(); UI.setStyle(panel, 'outline', '2px dashed var(--accent,#0d9488)'); };
    panel.ondragleave = function() { UI.setStyle(panel, 'outline', ''); };
    panel.ondrop = function(ev) {
      ev.preventDefault();
      UI.setStyle(panel, 'outline', '');
      var f = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if (f) _doUpload(id, path, f, panel);
    };
  } catch (e) {
    panel.innerHTML = '';
    panel.appendChild(header);
    var errP = document.createElement('p');
    UI.setStyle(errP, 'color', 'var(--red)');
    errP.textContent = 'Cannot list ' + path + ': ' + e.message;
    panel.appendChild(errP);
  }
}


function _uploadPrompt(id, path, panel) {
  var inp = document.createElement('input');
  inp.type = 'file';
  inp.onchange = function() {
    var f = inp.files && inp.files[0];
    if (f) _doUpload(id, path, f, panel);
  };
  inp.click();
}


function _doUpload(id, path, file, panel) {
  var fd = new FormData();
  fd.append('file', file);
  toast('Uploading ' + file.name + '\u2026', 'info');
  fetch(API + '/containers/' + id + '/upload?path=' + encodeURIComponent(path), {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + getToken(),
      'X-Requested-With': 'ContainerManager',
    },
    body: fd,
  })
    .then(function(r) {
      if (!r.ok) return r.json().then(function(body) {
        var msg = (body && body.detail && body.detail.message) || ('HTTP ' + r.status);
        throw new Error(msg);
      });
      return r.json();
    })
    .then(function() {
      toast('Uploaded ' + file.name, 'success');
      _renderFileBrowser(id, panel);
    })
    .catch(function(err) { toast('Upload failed: ' + err.message, 'error'); });
}


function _formatBytes(n) {
  if (!n || n < 0) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB'];
  var i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? String(n) : n.toFixed(1)) + ' ' + units[i];
}


async function _renderDiff(id, panel) {
  panel.innerHTML = '<div class="refreshing">Loading filesystem changes\u2026</div>';
  try {
    var data = await apiFetch(API + '/containers/' + id + '/diff');
    panel.innerHTML = '';
    if (!data || data.length === 0) {
      var empty = document.createElement('div'); empty.className = 'empty-state';
      var line1 = document.createElement('p'); UI.setStyle(line1, 'margin-bottom:6px;font-weight:500');
      line1.textContent = 'No files have changed since this container started.';
      var line2 = document.createElement('p'); UI.setStyle(line2, 'font-size:12px;color:var(--muted);margin:0 auto;max-width:520px');
      line2.textContent = 'This view shows the output of `docker diff` — paths the container has added, modified, or deleted versus its base image. An empty list means the container has only read from its image layers (typical for a read-only service, or a container that has just started).';
      empty.append(line1, line2);
      panel.appendChild(empty);
      return;
    }
    var table = document.createElement('table');
    table.innerHTML = '<thead><tr><th>Change</th><th>Path</th></tr></thead>';
    var tbody = document.createElement('tbody');
    data.forEach(function(d) {
      var tr = document.createElement('tr');
      var tdKind = document.createElement('td');
      var badge = document.createElement('span');
      badge.className = 'status ' + (d.kind === 'Added' ? 'running' : d.kind === 'Deleted' ? 'exited' : 'created');
      badge.textContent = d.kind;
      tdKind.appendChild(badge);
      var tdPath = document.createElement('td'); tdPath.className = 'mono'; UI.setStyle(tdPath, 'fontSize', '12px'); tdPath.textContent = d.path;
      tr.append(tdKind, tdPath); tbody.appendChild(tr);
    });
    table.appendChild(tbody); panel.appendChild(table);
    var note = document.createElement('p'); UI.setStyle(note, 'font-size:11px;color:var(--muted);margin-top:8px');
    note.textContent = data.length + ' change(s) from base image';
    panel.appendChild(note);
  } catch (e) {
    panel.innerHTML = '';
    var p = document.createElement('p'); UI.setStyle(p, 'color', 'var(--red)'); p.textContent = e.message;
    panel.appendChild(p);
  }
}

// ── Shared Hub Search builder ──
var POPULAR_IMAGES = [
  {name:'nginx',    desc:'Web server'},
  {name:'postgres', desc:'SQL database'},
  {name:'redis',    desc:'In-memory cache'},
  {name:'alpine',   desc:'Minimal Linux'},
  {name:'ubuntu',   desc:'Ubuntu Linux'},
  {name:'mysql',    desc:'MySQL database'},
  {name:'node',     desc:'Node.js runtime'},
  {name:'python',   desc:'Python runtime'},
];

/**
 * Build and return the Docker Hub registry search UI (input + auto-suggest dropdown).
 * Calls `onSelect(imageName)` when the user picks a result or presses Enter.
 * @param {Function} onSelect - Callback receiving the selected image name string
 * @returns {HTMLElement} The search container element to insert into the modal
 */
function buildHubSearch(onSelect) {
  var section = document.createElement('div');
  var label = document.createElement('p'); UI.setStyle(label, 'font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em'); label.textContent = 'Search Docker Hub';
  section.appendChild(label);

  // Popular starter chips
  var popular = document.createElement('div'); UI.setStyle(popular, 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px');
  POPULAR_IMAGES.forEach(function(img) {
    var chip = document.createElement('button'); chip.type = 'button';
    UI.setStyle(chip, 'font-size:11px;padding:3px 10px;border:1px solid var(--border);border-radius:20px;background:var(--bg);color:var(--text);cursor:pointer;display:flex;align-items:center;gap:4px');
    chip.innerHTML = '<strong>' + esc(img.name) + '</strong><span style="color:var(--muted)"> · ' + esc(img.desc) + '</span>';
    chip.onclick = function() { showTags(img.name); };
    popular.appendChild(chip);
  });
  section.appendChild(popular);

  var row = document.createElement('div'); UI.setStyle(row, 'display:flex;gap:8px;margin-bottom:8px');
  var hubInp = document.createElement('input'); hubInp.placeholder = 'Search by image name, e.g. postgres'; UI.setStyle(hubInp, 'flex', '1');
  var results = document.createElement('div'); UI.setStyle(results, 'max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:4px'); results.setAttribute('data-testid','hub-results');

  function showTags(imageName) {
    results.innerHTML = '<span style="font-size:12px;color:var(--muted)">Loading tags for ' + esc(imageName) + '…</span>';
    apiFetch(API+'/registry/tags?image=' + encodeURIComponent(imageName))
      .then(function(data) {
        results.innerHTML = '';
        var back = document.createElement('div');
        UI.setStyle(back, 'font-size:11px;color:var(--accent,#0d9488);cursor:pointer;margin-bottom:6px;display:flex;align-items:center;gap:4px');
        back.innerHTML = '&#8592; Back to search';
        back.onclick = function() { results.innerHTML = ''; };
        results.appendChild(back);
        var tags = data.tags || [];
        var header = document.createElement('div');
        UI.setStyle(header, 'font-size:13px;font-weight:600;margin-bottom:6px;padding:4px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:baseline;gap:8px');
        var nameSpan = document.createElement('span'); nameSpan.textContent = imageName + ' — pick a tag:';
        var count = document.createElement('span'); UI.setStyle(count, 'font-size:11px;font-weight:400;color:var(--muted)');
        // Use the explicit `truncated` + `upstream_total` fields now that
        // the server surfaces them; fall back to the length heuristic
        // (`length >= 100`) if an older server's response is in play.
        if (data.truncated && data.upstream_total) {
          count.textContent = 'showing ' + tags.length + ' of ' + data.upstream_total + ' tags — type to filter beyond the recent window';
        } else if (data.truncated) {
          count.textContent = tags.length + '+ tags (type to filter for older tags)';
        } else {
          count.textContent = tags.length + ' tags';
        }
        header.append(nameSpan, count);
        results.appendChild(header);
        if (!tags.length) {
          results.innerHTML += '<span style="font-size:12px;color:var(--muted)">No tags found.</span>';
          return;
        }
        // Filter box — Docker Hub returns up to 100 tags ordered by last_updated;
        // common pinned tags (3.12-slim, 3.11-alpine, etc.) can be buried below
        // rolling "latest" pushes. A client-side filter lets the user type
        // "3.12-slim" and jump straight to it.
        var filter = document.createElement('input');
        filter.type = 'search';
        filter.placeholder = 'Filter tags (e.g. 3.12-slim)';
        filter.setAttribute('data-testid', 'hub-tag-filter');
        UI.setStyle(filter, 'width:100%;padding:4px 8px;font-size:12px;margin-bottom:6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)');
        results.appendChild(filter);
        var tagList = document.createElement('div');
        UI.setStyle(tagList, 'display:flex;flex-direction:column;gap:4px');
        results.appendChild(tagList);
        // `tags` is the recent-100 list. When the user types a filter with
        // no local match (stable tags like `3.12-slim` often aren't in the
        // last 100 most-recently-updated tags), fall back to a server-side
        // query — /api/registry/tags?name=… asks Hub to filter by tag-name
        // substring, which reaches tags outside the recent window.
        var serverMatches = null; // set to array after a remote fetch
        var serverFetchPending = false;
        function _renderInto(container, list, needle) {
          container.innerHTML = '';
          list.forEach(function(tag) {
            var full = imageName + ':' + tag;
            var t = document.createElement('div');
            UI.setStyle(t, 'display:flex;align-items:center;justify-content:space-between;padding:5px 8px;border:1px solid var(--border);border-radius:6px;cursor:pointer;background:var(--bg)'); t.setAttribute('data-testid','hub-tag-row');
            t.innerHTML = '<span style="font-size:13px;font-family:monospace">' + esc(tag) + '</span>' +
              '<span style="font-size:11px;color:var(--accent,#0d9488)">Select ↵</span>';
            t.onclick = function() { onSelect(full); results.innerHTML = ''; hubInp.value = ''; };
            container.appendChild(t);
          });
        }
        function renderTags(q) {
          var needle = (q || '').toLowerCase();
          if (!needle) {
            _renderInto(tagList, tags, needle);
            return;
          }
          var localMatches = tags.filter(function(tag) { return tag.toLowerCase().indexOf(needle) !== -1; });
          if (localMatches.length) {
            _renderInto(tagList, localMatches, needle);
            return;
          }
          // No local match — try Hub's name filter. Guard with a minimum
          // query length to avoid hammering Hub on every keystroke, and
          // constrain to the same tag grammar the server enforces.
          if (needle.length < 2 || !/^[A-Za-z0-9_][A-Za-z0-9_.\-]*$/.test(q)) {
            tagList.innerHTML = '<span style="font-size:12px;color:var(--muted)">No tags match ' + esc(q) + ' in the latest ' + tags.length + '.</span>';
            return;
          }
          if (serverFetchPending) return;
          serverFetchPending = true;
          tagList.innerHTML = '<span style="font-size:12px;color:var(--muted)">Searching all tags for ' + esc(q) + '…</span>';
          apiFetch(API+'/registry/tags?image=' + encodeURIComponent(imageName) + '&name=' + encodeURIComponent(q))
            .then(function(d2) {
              serverFetchPending = false;
              var remote = d2.tags || [];
              // If the user has kept typing, re-run with the current value.
              if (filter.value !== q) { renderTags(filter.value); return; }
              if (!remote.length) {
                tagList.innerHTML = '<span style="font-size:12px;color:var(--muted)">No tags on Docker Hub match ' + esc(q) + '.</span>';
                return;
              }
              _renderInto(tagList, remote, needle);
            })
            .catch(function() {
              serverFetchPending = false;
              tagList.innerHTML = '<span style="font-size:12px;color:var(--red,#ef4444)">Tag search failed.</span>';
            });
        }
        filter.addEventListener('input', function() { renderTags(filter.value); });
        renderTags('');
      })
      .catch(function() { results.innerHTML = '<span style="font-size:12px;color:var(--red,#ef4444)">Failed to load tags.</span>'; });
  }

  function doSearch() {
    var q = hubInp.value.trim(); if (!q) return;
    results.innerHTML = '<span style="font-size:12px;color:var(--muted)">Searching…</span>';
    apiFetch(API+'/registry/search?q=' + encodeURIComponent(q))
      .then(function(data) {
        results.innerHTML = '';
        (data.results || []).forEach(function(item) {
          var name = item.repo_name || item.name;
          var pulls = item.pull_count > 1e6 ? Math.round(item.pull_count/1e6)+'M' : item.pull_count > 1e3 ? Math.round(item.pull_count/1e3)+'K' : (item.pull_count||'');
          var r2 = document.createElement('div');
          UI.setStyle(r2, 'display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;cursor:pointer;background:var(--bg)'); r2.setAttribute('data-testid','hub-result-row');
          r2.innerHTML = '<div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(name) + '</div>' +
            '<div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis">' + esc(item.short_description||'') + '</div></div>' +
            (pulls ? '<span style="font-size:11px;color:var(--muted);white-space:nowrap">' + esc(pulls) + ' pulls</span>' : '');
          r2.onclick = function() { showTags(name); };
          results.appendChild(r2);
        });
        if (!data.results || !data.results.length) results.innerHTML = '<span style="font-size:12px;color:var(--muted)">No results.</span>';
      })
      .catch(function() { results.innerHTML = '<span style="font-size:12px;color:var(--red,#ef4444)">Search failed.</span>'; });
  }
  hubInp.addEventListener('keydown', function(e) { if (e.key === 'Enter') doSearch(); });
  var btn = makeBtn('Search', doSearch, 'btn');
  row.append(hubInp, btn); section.appendChild(row); section.appendChild(results);
  // Hide if docker.io not allowed
  apiFetch(API+'/config').then(function(cfg) {
    if (!(cfg.allowed_registries||[]).some(function(r) { return r.replace(/\/$/, '') === 'docker.io'; })) UI.setStyle(section, 'display', 'none');
  }).catch(function(){});
  return { section: section, focus: function() { hubInp.focus(); } };
}

// ── Run Modal ──
/**
 * Open the "Run new container" modal and render the full creation form:
 * image selector (with Hub search), ports, volumes, environment variables,
 * labels, restart policy, network, and read-only flag.
 * @param {string} [prefillImage] - Optional image name to pre-populate the image field
 */
// showRunModal renders the "Run new container" modal. When `prefillSource` (an
// inspect-response object) is supplied, the modal switches to "Clone" mode:
// every editable field is pre-populated from the source and an inherit_from/
// replace_id pair is sent on submit so the server can preserve env values
// without exposing them to the client (zero-trust clone).
function showRunModal(prefillImage, prefillSource) {
  // `prefillImage` accepts either a string image ref or a template
  // object `{image, name, command, ports, env, volumes}` sent by
  // pages/templates.js _deployTemplate.
  var prefillTemplate = null;
  if (prefillImage && typeof prefillImage === 'object' && !Array.isArray(prefillImage)) {
    prefillTemplate = prefillImage;
    prefillImage = prefillTemplate.image || '';
  }
  if (typeof prefillImage !== 'string') prefillImage = '';
  if (prefillSource && typeof prefillSource !== 'object') prefillSource = null;
  if (document.querySelector('.modal-bg')) return;
  clearInterval(refreshTimer);
  var modal = document.createElement('div'); modal.className = 'modal-bg';
  modal.onclick = function(e) { if (e.target === modal) { modal.remove(); loadContainers(); } };
  var box = document.createElement('div'); box.className = 'modal';
  var h3 = document.createElement('h3');
  h3.textContent = prefillSource ? 'Clone container: ' + prefillSource.name : 'Run new container';
  box.appendChild(h3);
  if (prefillSource) {
    var sub = document.createElement('p');
    UI.setStyle(sub, 'font-size:12px;color:var(--muted);margin:-4px 0 12px');
    sub.textContent = 'Fields pre-filled from ' + prefillSource.name + '. Edit any field, then launch. Env values are preserved server-side and never displayed.';
    box.appendChild(sub);
  }

  // Sticky error banner — persistent replacement for the ephemeral toast.
  // Volume/port/env format errors used to flash for 3s then vanish, leaving
  // the user to reopen the modal with no idea what they'd entered wrong.
  // Now the banner sits under the header until the next submit clears it.
  var runErrorBanner = document.createElement('div');
  runErrorBanner.className = 'field-error';
  UI.setStyle(runErrorBanner, 'display', 'none');
  runErrorBanner.setAttribute('role', 'alert');
  runErrorBanner.setAttribute('aria-live', 'polite');
  box.appendChild(runErrorBanner);
  function _runError(msg) {
    if (msg) { runErrorBanner.textContent = msg; UI.setStyle(runErrorBanner, 'display', 'block'); }
    else     { runErrorBanner.textContent = ''; UI.setStyle(runErrorBanner, 'display', 'none'); }
  }

  // Available images quick-pick
  var pickSection = document.createElement('div');
  UI.setStyle(pickSection, 'margin-bottom:16px');
  var pickLabel = document.createElement('p');
  UI.setStyle(pickLabel, 'font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em');
  pickLabel.textContent = 'Available images on this engine';
  pickSection.appendChild(pickLabel);
  var pickList = document.createElement('div');
  UI.setStyle(pickList, 'display:flex;flex-wrap:wrap;gap:6px;min-height:28px');
  pickList.innerHTML = '<span style="font-size:12px;color:var(--muted)">Loading…</span>';
  pickSection.appendChild(pickList);
  box.appendChild(pickSection);

  function addField(label, fieldId, ph, type) {
    var lbl = document.createElement('label'); lbl.textContent = label; box.appendChild(lbl);
    var inp = document.createElement(type === 'textarea' ? 'textarea' : 'input');
    inp.id = fieldId; inp.placeholder = ph; if (type === 'textarea') inp.rows = 3;
    box.appendChild(inp);
    return inp;
  }
  // Docker Hub search
  var hubSearch = buildHubSearch(function(name) { imgInput.value = name; });
  UI.setStyle(hubSearch.section, 'marginBottom', '16px');
  box.appendChild(hubSearch.section);

  var imgInput = addField('Image','run-image', 'registry/image:tag');
  if (prefillImage) imgInput.value = prefillImage;
  else if (prefillSource) imgInput.value = prefillSource.image || '';
  var dl = document.createElement('datalist'); dl.id = 'image-list'; box.appendChild(dl);
  imgInput.setAttribute('list','image-list');

  var imgHint = document.createElement('p');
  UI.setStyle(imgHint, 'font-size:11px;color:var(--muted);margin-top:4px');
  imgHint.id = 'run-registry-hint';
  imgHint.textContent = 'Loading registry configuration…';
  box.appendChild(imgHint);

  var nameInp = addField('Name (optional)','run-name','my-container');
  var cmdInp = addField('Command (optional)','run-cmd','e.g. /bin/sh or sleep 3600');
  var portsInp = addField('Ports (e.g. 8080:80)','run-ports','host-port:container-port');
  var envInp = addField('Environment variables (one per line)','run-env','KEY=VALUE','textarea');
  var volInp = addField('Volume mounts (one per line)','run-volumes','volume_name:/container/path','textarea');
  // Inline hint — users kept submitting a bare volume name (e.g. "test_volume")
  // and seeing a transient toast; the mount-point syntax wasn't obvious. This
  // static hint sits directly under the field so it's visible while the user
  // is still filling it in.
  var volHint = document.createElement('p');
  UI.setStyle(volHint, 'font-size:11px;color:var(--muted);margin:-6px 0 10px');
  volHint.textContent = 'Format: NAME:/absolute/path — one per line. Example: test_volume:/data. The volume must already exist or will be created on first run.';
  box.appendChild(volHint);
  var labelsInp = addField('Labels (one per line, key=value)','run-labels','app=myapp','textarea');
  var lbl3 = document.createElement('label'); lbl3.textContent = 'Restart policy'; box.appendChild(lbl3);
  var selRestart = document.createElement('select'); selRestart.id = 'run-restart';
  ['no','on-failure','unless-stopped','always'].forEach(function(p) { var o = document.createElement('option'); o.value = p; o.textContent = p; selRestart.appendChild(o); });
  box.appendChild(selRestart);
  var lbl4 = document.createElement('label'); lbl4.textContent = 'Network (optional)'; box.appendChild(lbl4);
  var selNet = document.createElement('select'); selNet.id = 'run-network';
  var defOpt = document.createElement('option'); defOpt.value = ''; defOpt.textContent = '(default bridge)'; selNet.appendChild(defOpt);
  box.appendChild(selNet);
  apiFetch(API+'/networks').then(function(nets) {
    nets.forEach(function(n) { var o = document.createElement('option'); o.value = n.name; o.textContent = n.name + ' (' + n.driver + ')'; selNet.appendChild(o); });
    // Select the source's first network after the options are loaded
    if (prefillSource && prefillSource.network) {
      var srcNet = Object.keys(prefillSource.network)[0];
      if (srcNet && srcNet !== 'bridge') selNet.value = srcNet;
    }
  }).catch(function(){});

  // Template prefills ───────────────────────────────────────────────────────
  // When opened from pages/templates.js the template object carries
  // name_hint, ports, env, volumes, command. Populate each field so a
  // novice can review + click Deploy without typing anything.
  if (prefillTemplate) {
    if (prefillTemplate.name) nameInp.value = prefillTemplate.name;
    if (prefillTemplate.command) cmdInp.value = prefillTemplate.command;
    if (Array.isArray(prefillTemplate.ports) && prefillTemplate.ports.length) {
      portsInp.value = prefillTemplate.ports.map(function(p) {
        return (p.host + ':' + p.container);
      }).join(', ');
    }
    if (Array.isArray(prefillTemplate.env) && prefillTemplate.env.length) {
      envInp.value = prefillTemplate.env.map(function(e) {
        return e.key + '=' + (e.value || '');
      }).join('\n');
    }
    if (Array.isArray(prefillTemplate.volumes) && prefillTemplate.volumes.length) {
      volInp.value = prefillTemplate.volumes.map(function(v) {
        return (v.name_hint || v.name || 'data') + ':' + v.mount;
      }).join('\n');
    }
  }

  // Clone-mode prefills ────────────────────────────────────────────────────
  // Immutable create-time fields are pre-populated from prefillSource so the
  // user can tweak any of them in one place, rather than manually copying.
  // Env is NOT populated with values — server does the merge via inherit_from
  // so sensitive values never cross the UI.
  if (prefillSource) {
    nameInp.value = 'clone-' + prefillSource.name;
    var srcCmd = (prefillSource.config && prefillSource.config.cmd) || [];
    cmdInp.value = Array.isArray(srcCmd) ? srcCmd.join(' ') : String(srcCmd || '');
    // Ports: port_bindings is {"80/tcp": [{"HostIp":"", "HostPort":"8080"}]}
    // The run modal format is "HOST:CONTAINER[, HOST:CONTAINER]".
    var pb = (prefillSource.host_config && prefillSource.host_config.port_bindings) || {};
    var portEntries = [];
    Object.keys(pb).forEach(function(cp) {
      var bindings = pb[cp] || [];
      var cpNum = cp.split('/')[0];
      bindings.forEach(function(b) {
        if (b && b.HostPort) portEntries.push(b.HostPort + ':' + cpNum);
      });
    });
    portsInp.value = portEntries.join(', ');
    // Env display: show preserved KEY names (values redacted by server inspect).
    var srcEnv = (prefillSource.config && prefillSource.config.env) || [];
    var preservedKeys = srcEnv.map(function(e) { return String(e).split('=')[0]; }).filter(Boolean);
    if (preservedKeys.length) {
      envInp.placeholder = 'Add overrides as KEY=VALUE\u2026\n(preserved from source: ' + preservedKeys.join(', ') + ')';
    }
    // Volumes: binds are "name:/path[:mode]" strings — match the modal format directly
    volInp.value = ((prefillSource.host_config && prefillSource.host_config.binds) || []).join('\n');
    // Labels: strip Docker-managed labels (com.docker.*) so the user doesn't
    // accidentally re-apply compose metadata that would confuse stack listings.
    var srcLabels = (prefillSource.config && prefillSource.config.labels) || {};
    var userLabels = [];
    Object.keys(srcLabels).forEach(function(k) {
      if (!k.startsWith('com.docker.')) userLabels.push(k + '=' + srcLabels[k]);
    });
    labelsInp.value = userLabels.join('\n');
    // Restart policy
    var srcRp = (prefillSource.host_config && prefillSource.host_config.restart_policy) || {};
    if (srcRp.Name && VALID_RESTART_POLICIES_CLIENT.indexOf(srcRp.Name) >= 0) {
      selRestart.value = srcRp.Name;
    }
  }
  // Read-only rootfs toggle. Default on: tmpfs is auto-mounted on /tmp, /run,
  // /var/run, /var/cache so common images (nginx, redis, haproxy) boot cleanly.
  // Users can uncheck for images that need an unrestricted writable rootfs.
  // In clone mode: inherit the source's choice so we don't silently widen or
  // narrow the original container's hardening posture.
  var roWrap = document.createElement('div'); UI.setStyle(roWrap, 'display:flex;align-items:center;gap:8px;margin-top:12px;');
  var roCb = document.createElement('input'); roCb.type = 'checkbox'; roCb.id = 'run-readonly';
  roCb.checked = prefillSource ?
    !!(prefillSource.host_config && prefillSource.host_config.readonly_rootfs) :
    true;
  UI.setStyle(roCb, 'width:auto;margin:0;');
  var roLbl = document.createElement('label'); roLbl.htmlFor = 'run-readonly';
  UI.setStyle(roLbl, 'margin:0;font-size:13px;cursor:pointer;');
  roLbl.textContent = 'Read-only root filesystem (recommended)';
  roWrap.appendChild(roCb);
  roWrap.appendChild(roLbl);
  roWrap.appendChild(UI.helpIcon(
    'Prevents the container from writing to its own root filesystem. '
    + 'Common attack vector for malware. SKIFF auto-mounts tmpfs on '
    + '/tmp, /run, /var/run, /var/cache so nginx/redis/most images still '
    + 'work. Uncheck only for images that write to arbitrary rootfs paths '
    + '(some databases, some build tools).'
  ));
  box.appendChild(roWrap);
  var roHint = document.createElement('p');
  UI.setStyle(roHint, 'font-size:11px;color:var(--muted);margin:4px 0 8px 22px;');
  roHint.textContent = 'Auto-mounts tmpfs on /tmp, /run, /var/run, /var/cache so most images work. Uncheck for images that write elsewhere on rootfs.';
  box.appendChild(roHint);
  // Clone-only: "Replace original" checkbox. Off by default → both containers
  // coexist (user chose a new name). On → server stops + removes the source
  // after the new container starts. If the new container fails to create, the
  // source is preserved — safety-by-ordering at the server.
  var replaceCb = null;
  if (prefillSource) {
    var repWrap = document.createElement('div'); UI.setStyle(repWrap, 'display:flex;align-items:center;gap:8px;margin-top:8px;');
    replaceCb = document.createElement('input'); replaceCb.type = 'checkbox'; replaceCb.id = 'run-replace';
    UI.setStyle(replaceCb, 'width:auto;margin:0;');
    var repLbl = document.createElement('label'); repLbl.htmlFor = 'run-replace';
    UI.setStyle(repLbl, 'margin:0;font-size:13px;cursor:pointer;');
    repLbl.textContent = 'Replace original (stop & remove ' + prefillSource.name + ' after this one starts)';
    repWrap.appendChild(replaceCb); repWrap.appendChild(repLbl); box.appendChild(repWrap);
    var repHint = document.createElement('p');
    UI.setStyle(repHint, 'font-size:11px;color:var(--muted);margin:4px 0 8px 22px;');
    repHint.textContent = 'If this container fails to start, the original is preserved.';
    box.appendChild(repHint);
  }
  var actions = document.createElement('div'); actions.className = 'actions';
  actions.append(makeBtn('Cancel', function() { modal.remove(); loadContainers(); }),
    makeActionBtn('Run', async function() {
      _runError(null);
      var image = document.getElementById('run-image').value.trim();
      if (!image) { _runError('Image name is required'); throw new Error('no image'); }
      // Pre-flight volume format check — catches "test_volume" (no mount
      // path) and similar so the user gets an explanation without a round
      // trip to the server. Server still validates authoritatively.
      var volRawPreflight = document.getElementById('run-volumes').value;
      if (volRawPreflight) {
        var lines = volRawPreflight.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
        for (var i = 0; i < lines.length; i++) {
          if (lines[i].indexOf(':') < 0) {
            _runError('Volume mount line ' + (i + 1) + ' is missing a mount path. Use NAME:/path (e.g. ' + lines[i] + ':/data).');
            throw new Error('bad volume spec');
          }
        }
      }
      var name = document.getElementById('run-name').value || null;
      var cmd = document.getElementById('run-cmd').value || null;
      var portsRaw = document.getElementById('run-ports').value;
      var envRaw = document.getElementById('run-env').value;
      var volRaw = document.getElementById('run-volumes').value;
      var labelsRaw = document.getElementById('run-labels').value;
      var restart = document.getElementById('run-restart').value;
      var networkVal = document.getElementById('run-network').value || null;
      var ports = portsRaw ? Object.fromEntries(portsRaw.split(',').map(function(p) { var s = p.trim().split(':'); return [s[1], s[0]]; })) : null;
      var environment = envRaw ? envRaw.trim().split('\n').filter(Boolean) : null;
      var volumes = volRaw ? volRaw.trim().split('\n').filter(Boolean) : null;
      var labels = null;
      if (labelsRaw) { labels = {}; labelsRaw.trim().split('\n').filter(Boolean).forEach(function(l) { var eq = l.indexOf('='); if (eq > 0) labels[l.substring(0, eq)] = l.substring(eq + 1); }); }
      var readOnly = document.getElementById('run-readonly').checked;
      var params = new URLSearchParams(); params.set('image', image); if (name) params.set('name', name);
      var body = {
        ports: ports,
        environment: environment,
        command: cmd,
        volumes: volumes,
        restart_policy: restart,
        network: networkVal,
        labels: labels,
        read_only: readOnly,
      };
      // Clone-mode: attach inherit_from (env preservation) and optionally replace_id
      if (prefillSource) {
        body.inherit_from = prefillSource.id;
        if (replaceCb && replaceCb.checked) body.replace_id = prefillSource.id;
      }
      var resp;
      try {
        resp = await apiFetch(API+'/containers/run?'+params, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
      } catch (err) {
        // Surface the server's envelope message in the sticky banner so
        // the user can read it while scrolling the modal. Re-throw so
        // the button re-enables and the user can fix + retry.
        _runError((err && err.message) ? err.message : 'Run failed');
        throw err;
      }
      if (document.body.contains(modal)) modal.remove();
      var msg = prefillSource
        ? (resp && resp.replaced_old ? 'Cloned and replaced ' + prefillSource.name : 'Cloned from ' + prefillSource.name)
        : 'Container launched';
      toast(msg, 'success');
      loadContainers();
    }, 'btn primary', 'Launching\u2026'));
  box.appendChild(actions); modal.appendChild(box); document.body.appendChild(modal);
  imgInput.focus();

  // Load available images for quick-pick chips and datalist
  apiFetch(API+'/images').then(function(images) {
    pickList.innerHTML = '';
    if (!images || images.length === 0) {
      pickList.innerHTML = '<span style="font-size:12px;color:var(--muted)">No images on this engine yet — pull one from the Images page first.</span>';
      return;
    }
    var tags = [];
    images.forEach(function(img) { (img.tags || [img.tag]).forEach(function(t) { if (t && t !== '<none>:<none>') tags.push(t); }); });
    tags.slice(0, 20).forEach(function(tag) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.textContent = tag;
      UI.setStyle(chip, 'font-size:11px;padding:2px 8px;border:1px solid var(--border);border-radius:20px;background:var(--bg);color:var(--text);cursor:pointer;white-space:nowrap;max-width:240px;overflow:hidden;text-overflow:ellipsis');
      chip.title = tag;
      chip.onclick = function() { imgInput.value = tag; };
      pickList.appendChild(chip);
      var o = document.createElement('option'); o.value = tag; dl.appendChild(o);
    });
    if (tags.length === 0) {
      pickList.innerHTML = '<span style="font-size:12px;color:var(--muted)">No tagged images available.</span>';
    }
  }).catch(function() {
    pickList.innerHTML = '<span style="font-size:12px;color:var(--muted)">Could not load images.</span>';
  });

  // Load registry hint
  apiFetch(API+'/config').then(function(cfg) {
    var hint = document.getElementById('run-registry-hint');
    if (!hint) return;
    var regs = (cfg.allowed_registries || []);
    if (regs.length === 0) {
      hint.textContent = 'No registry restriction configured — any image is permitted.';
    } else {
      hint.textContent = 'Allowed registries: ' + regs.join(', ') + '. Images must be pulled to this engine before they can be run.';
    }
  }).catch(function() {});
}

// Per-page modules (images, volumes, networks, compose, system,
// wizard) load as separate <script> tags in index.html after app.js —
// see skiff/static/pages/*.js.

// ── Sidebar nav wiring (CSP-safe: no inline onclick) ──
document.querySelectorAll('.sidebar a[data-page]').forEach(function(a) {
  a.addEventListener('click', function() { showPage(a.getAttribute('data-page')); });
});

// ── Logout button ──
(function() {
  var logoutBtn = document.getElementById('sidebar-logout');
  if (!logoutBtn) return;
  function syncLogout() {
    logoutBtn.classList.toggle('visible', !!getToken());
  }
  logoutBtn.addEventListener('click', function() {
    sessionStorage.clear();
    sessionCleanup();
    syncLogout();
    showLogin();
  });
  // Show/hide on token changes by re-checking periodically and on visibility
  document.addEventListener('visibilitychange', syncLogout);
  setInterval(syncLogout, 2000);
  syncLogout();
})();

// ── Init ──
(async function() {
  if (await checkSetupState()) return;
  try {
    var cfg = await fetch(API+'/auth-required').then(function(r){return r.json();});
    if (cfg.required && !getToken()) { showLogin(); return; }
  } catch(e) { /* server down, try loading anyway */ }
  // Show logout button now that we know auth is needed
  var logoutBtn = document.getElementById('sidebar-logout');
  if (logoutBtn && getToken()) logoutBtn.classList.add('visible');
  // Fetch app config (docker_vm_host, docker_host, etc.) for context-aware UI
  var _appConfig = null;
  try {
    var appCfg = await apiFetch(API+'/config');
    _appConfig = appCfg;
    if (appCfg.docker_vm_host) _dockerVmHost = appCfg.docker_vm_host;
    if (appCfg.docker_host) _appDockerHost = appCfg.docker_host;
    _applySessionTimeoutsFromConfig(appCfg);
    // Reviewer persona is read-only by design. Server enforces it via
    // secure_route.mutate; the body class lets CSS hide destructive
    // buttons so a reviewer isn't clicking into 403s.
    if (appCfg.profile === 'reviewer') {
      document.body.classList.add('reviewer-mode');
      _renderReviewerBanner();
    } else {
      _enableProfileSwitcher();
    }
    // Insecure-mode banner. Server-side flag, so the client can surface
    // it but can't silence it. Triggers when bind != localhost AND
    // api_token is empty (anyone on the network reaches Docker).
    if (appCfg.insecure_mode) _renderInsecureBanner(appCfg.bind_host || '0.0.0.0');
  } catch(e) { /* ignore, defaults apply */ }
  // Restore last-viewed page so reload doesn't bounce the user back
  // to Containers. Only accept known INTERNAL page ids — malformed
  // storage (an old key, a dev-tools edit) falls back to the default.
  // External entries (api-docs) are not valid landing pages: they're
  // nav actions (open new tab), not SPA pages, and clicking them
  // deliberately doesn't persist.
  var ALLOWED_LANDING_PAGES = [
    'dashboard', 'containers', 'images', 'templates',
    'volumes', 'networks', 'compose', 'system', 'settings',
  ];
  var lastPage = 'containers';
  try {
    var stored = localStorage.getItem('skiff.last_page');
    if (stored && ALLOWED_LANDING_PAGES.indexOf(stored) !== -1) lastPage = stored;
  } catch (e) { /* ignore */ }
  showPage(lastPage);
})();

// Reviewer-mode switcher. One-way dropdown in the sidebar footer. An
// admin can hand the session to a reviewer after clicking; reviewer
// cannot exit back to a write-capable profile without a server restart.
function _enableProfileSwitcher() {
  var sel = document.getElementById('profile-switcher');
  if (!sel) return;
  sel.hidden = false;
  sel.addEventListener('change', function() {
    var target = sel.value;
    sel.value = '';  // reset placeholder so a re-selection fires again
    if (target !== 'reviewer') return;
    if (!confirm(t('reviewer.confirm_enter'))) return;
    apiFetch(API + '/profile/enter-reviewer', { method: 'POST' })
      .then(function() { window.location.reload(); })
      .catch(function(e) { toast(e.message || 'Switch failed', 'error'); });
  });
}

// Reviewer-mode banner. Sticky strip under the title bar letting the
// reviewer know mutations are disabled by profile, not by accident.
function _renderReviewerBanner() {
  if (document.getElementById('reviewer-mode-banner')) return;
  var banner = document.createElement('div');
  banner.id = 'reviewer-mode-banner';
  banner.className = 'reviewer-banner';
  var msg = document.createElement('span');
  msg.textContent = t('reviewer.banner');
  banner.appendChild(msg);
  document.body.insertBefore(banner, document.body.firstChild);
}

// Insecure-mode banner. Sticky red bar above the app when the
// server is bound to a non-loopback interface with no API_TOKEN set.
function _renderInsecureBanner(bind) {
  // Idempotent — only render once
  if (document.getElementById('insecure-mode-banner')) return;
  var banner = document.createElement('div');
  banner.id = 'insecure-mode-banner';
  banner.className = 'insecure-banner';
  var msg = document.createElement('span');
  msg.textContent = 'INSECURE MODE — server bound to ' + bind + ' with no API_TOKEN. Anyone on the network can control Docker. Set API_TOKEN and restart.';
  banner.appendChild(msg);
  document.body.insertBefore(banner, document.body.firstChild);
}
