// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/*
 * SKIFF container-shell iframe module.
 *
 * Hosts an xterm.js Terminal inside the route-scoped HTML returned by
 * `GET /api/terminal-frame/{container_id}`. The iframe exists because
 * xterm.js writes inline element.style.X assignments during render —
 * something the main SPA's strict `style-src 'self'` (no
 * 'unsafe-inline') CSP blocks. Confining xterm to its own document
 * with a relaxed CSP lets the rest of the app run under the strict
 * policy without losing the terminal feature.
 *
 * Responsibilities:
 *   - parse `container_id` from the URL path
 *   - construct an xterm.js Terminal + FitAddon
 *   - open a WebSocket to /ws/exec/{container_id}; send the AUTH token
 *     pulled from sessionStorage (same-origin, shared with the parent)
 *   - keystrokes → ws.send(raw bytes); ws messages → term.write()
 *   - reconnect with exponential backoff on unexpected close
 *   - postMessage events to the parent SPA so it can react (banner,
 *     session-expiry toast, status counters)
 *   - listen for parent commands: { type: 'focus' | 'disconnect' }
 *
 * Communication protocol (window.parent.postMessage):
 *   parent ← iframe:
 *     { type: 'terminal-ready' }        — Terminal mounted, WS open
 *     { type: 'terminal-disconnected', code, reason }
 *     { type: 'terminal-session-expired' }   — WS code 4003
 *     { type: 'terminal-error', message }
 *   iframe ← parent:
 *     { type: 'focus' }                 — pull focus back to the terminal
 *     { type: 'disconnect' }            — user clicked Disconnect in the SPA
 *
 * All messages carry `targetOrigin === window.location.origin`, so a
 * cross-origin embedder cannot inject commands.
 */
"use strict";

(function () {
  var MAX_RECONNECTS = 5;
  var SESSION_EXPIRED_CODE = 4003;

  function parseContainerId() {
    // /api/terminal-frame/<id> — take the last non-empty segment.
    var parts = window.location.pathname.split('/').filter(Boolean);
    return parts.length ? parts[parts.length - 1] : '';
  }

  function getAuthToken() {
    try { return sessionStorage.getItem('api_token') || ''; } catch (e) { return ''; }
  }

  function postToParent(payload) {
    try {
      // window.parent === window when this page is loaded standalone
      // (devs hitting the URL directly). Skip the postMessage in that
      // case — there is no embedder to notify.
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(payload, window.location.origin);
      }
    } catch (e) { /* parent navigated away or origin mismatch — drop */ }
  }

  function setStatus(state, msg) {
    var el = document.getElementById('status');
    if (!el) return;
    if (!state) {
      el.removeAttribute('data-state');
      el.textContent = '';
    } else {
      el.dataset.state = state;
      el.textContent = msg || state;
    }
  }

  function wsUrlFor(id) {
    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + '/ws/exec/' + encodeURIComponent(id);
  }

  // Render an in-iframe "Start new session" / "Reconnect" affordance
  // once the WS has closed and won't auto-reconnect. Without this the
  // operator either has to use the parent SPA's Disconnect button (and
  // re-navigate through the tab to start over) or refresh the whole
  // page. `window.location.reload()` re-runs terminal-frame.js end-to-
  // end which spawns a fresh xterm + WS for the same container.
  function _showRestartButton(label) {
    if (document.getElementById('restart-btn')) return;
    var btn = document.createElement('button');
    btn.id = 'restart-btn';
    btn.type = 'button';
    btn.textContent = label;
    btn.addEventListener('click', function () {
      try { window.location.reload(); } catch (e) { /* navigation race */ }
    });
    document.body.appendChild(btn);
    // Re-focus so the operator can press Enter to activate without
    // moving the mouse. xterm has hold of focus while the WS was up;
    // ceding it now is correct since the terminal is no longer live.
    try { btn.focus(); } catch (e) { /* ignore */ }
  }

  function start() {
    var containerId = parseContainerId();
    if (!containerId) {
      setStatus('error', 'No container ID in URL');
      return;
    }
    var termDiv = document.getElementById('term');
    if (!termDiv || !window.Terminal) {
      setStatus('error', 'xterm.js failed to load');
      postToParent({ type: 'terminal-error', message: 'xterm.js failed to load' });
      return;
    }

    var term = new window.Terminal({
      cursorBlink: true,
      fontFamily: '"DejaVu Sans Mono","Liberation Mono","Noto Sans Mono","Courier New",monospace',
      fontSize: 13,
      theme: {
        background: '#0d1117',
        foreground: '#e6edf3',
        cursor: '#e6edf3',
      },
      scrollback: 10000,
      convertEol: true,
    });
    var fit = null;
    if (window.FitAddon && window.FitAddon.FitAddon) {
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
    }
    term.open(termDiv);
    if (fit) { try { fit.fit(); } catch (e) { /* ignore */ } }
    // Expose the Terminal instance on the iframe's window so the
    // parent's diagnostic helpers (tests/e2e_helpers.py:term_read) can
    // read scrollback. Same-origin iframe → parent can reach this via
    // `iframe.contentWindow._term` without postMessage. Not used by
    // production code; purely a test introspection hook.
    window._term = term;
    window._fit = fit;

    var ws = null;
    var attempt = 0;
    var userClosed = false;

    function sendResize() {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (fit) { try { fit.fit(); } catch (e) { /* ignore */ } }
      try {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      } catch (e) { /* WS may have flapped between checks */ }
    }

    var resizeTimer = null;
    function onWindowResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(sendResize, 150);
    }
    window.addEventListener('resize', onWindowResize);

    function connect() {
      if (userClosed) return;
      setStatus(attempt > 0 ? 'reconnecting' : 'connecting',
        attempt > 0 ? 'Reconnecting…' : 'Connecting…');
      ws = new WebSocket(wsUrlFor(containerId));
      ws.onopen = function () {
        var t = getAuthToken();
        if (t) { try { ws.send('AUTH ' + t); } catch (e) { /* ignore */ } }
        setStatus(null);
        if (attempt > 0) { term.write('\r\n[Reconnected]\r\n'); }
        // Wait a tick before sending the initial resize so the page has
        // settled into its final layout (matters during iframe mount).
        setTimeout(sendResize, 100);
        attempt = 0;
        // Clear any "Reconnect now" affordance left over from an
        // earlier reconnect cycle — the session is live again.
        var rb = document.getElementById('restart-btn');
        if (rb && rb.parentNode) rb.parentNode.removeChild(rb);
        postToParent({ type: 'terminal-ready' });
      };
      ws.onmessage = function (e) {
        // xterm.js accepts both string and Uint8Array; the server emits
        // raw PTY bytes as text frames so a direct write is fine.
        term.write(e.data);
      };
      ws.onerror = function () {
        setStatus('error', 'Connection error');
        postToParent({ type: 'terminal-error', message: 'WebSocket error' });
      };
      ws.onclose = function (evt) {
        if (userClosed) {
          // Operator clicked the SPA's Disconnect button. The cached
          // iframe stays mounted in `#_term-host` and we surface the
          // same in-iframe "Start new session" affordance the
          // server-initiated close uses, so the operator has a
          // one-click path to a fresh shell without leaving the
          // Terminal tab.
          setStatus('disconnected', 'Session ended');
          term.write('\r\n[Session ended]\r\n');
          _showRestartButton('Start new session');
          postToParent({ type: 'terminal-disconnected', code: evt.code, reason: evt.reason || '' });
          return;
        }
        if (evt.code === 1000) {
          // Clean close from the server side — the operator typed
          // `exit` or Ctrl-D, the PTY closed, and the WS handler
          // shut down. Without an affordance to start over, the
          // terminal sits visibly dead and the only escape is the
          // parent SPA's Disconnect → switch-tab → re-enter dance.
          // Show an in-iframe "Start new session" button instead.
          setStatus('disconnected', 'Closed');
          term.write('\r\n[Shell exited]\r\n');
          _showRestartButton('Start new session');
          postToParent({ type: 'terminal-disconnected', code: 1000, reason: 'shell-exited' });
          return;
        }
        if (evt.code === SESSION_EXPIRED_CODE) {
          setStatus('error', 'Session expired');
          term.write('\r\n[Session expired — please log in again]\r\n');
          postToParent({ type: 'terminal-session-expired' });
          return;
        }
        if (attempt >= MAX_RECONNECTS) {
          setStatus('error', 'Reconnect limit reached');
          term.write('\r\n[Max reconnect attempts reached]\r\n');
          // Same affordance as the clean-close case — give the user
          // an explicit way to re-establish a session without having
          // to navigate back through the parent SPA's tab nav.
          _showRestartButton('Reconnect');
          postToParent({ type: 'terminal-disconnected', code: evt.code, reason: 'max_retries' });
          return;
        }
        // Surface a manual "Reconnect now" affordance while the auto-
        // backoff is running too. The operator may not want to wait
        // through five doubling delays (1 + 2 + 4 + 8 + 16 = 31s) when
        // they know the underlying daemon just came back. Auto-
        // reconnect continues in the background; clicking the button
        // reloads the iframe end-to-end and produces a fresh WS.
        _showRestartButton('Reconnect now');
        var delay = Math.min(1000 * Math.pow(2, attempt), 16000);
        attempt += 1;
        term.write('\r\n[Reconnecting in ' + (delay / 1000) + 's…]\r\n');
        setTimeout(connect, delay);
      };
    }

    // Send keystrokes raw — xterm's Terminal.onData fires with the
    // PTY-ready encoding for everything (arrow keys, Ctrl chords, Tab).
    term.onData(function (data) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(data); } catch (e) { /* ignore */ }
      }
    });

    // Parent → iframe commands. Reject any message whose source isn't
    // our parent window (defence-in-depth on top of the route's
    // `frame-ancestors 'self'`).
    window.addEventListener('message', function (e) {
      if (e.source !== window.parent) return;
      // Origin check — postMessage from a same-origin parent reports
      // window.location.origin; an opaque (sandboxed) embedder would
      // be "null" and we reject those.
      if (e.origin !== window.location.origin) return;
      var data = e.data || {};
      if (data.type === 'focus') {
        try { term.focus(); } catch (err) { /* ignore */ }
      } else if (data.type === 'disconnect') {
        userClosed = true;
        if (ws && ws.readyState < WebSocket.CLOSING) {
          try { ws.close(1000, 'user-disconnect'); } catch (err) { /* ignore */ }
        }
      }
    });

    // Focus the terminal once and let the iframe own keystroke focus
    // from there on out.
    try { term.focus(); } catch (e) { /* ignore */ }
    connect();
  }

  // Defensive: xterm.js + FitAddon load via `<script>` tags ABOVE this
  // file in terminal-frame.html. By the time this file evaluates the
  // globals should exist, but if some asset failed to load we still
  // post an error to the parent rather than throw silently.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
