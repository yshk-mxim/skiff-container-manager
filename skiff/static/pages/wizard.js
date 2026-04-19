// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Setup Wizard — per-page module loaded by index.html.
 *
 * First-run wizard shown when the server has no API_TOKEN configured.
 * Handles the token-generate-or-paste flow, the SSH / Local tabs, the
 * tunnel probe, and the final /api/setup POST that configures the
 * server in memory.
 *
 * Uses globals from app.js + ui.js: API, apiFetch, toast, makeBtn,
 * makeActionBtn, esc, UI, and the apiFetch session key layer.
 */
"use strict";

// ── Setup Wizard ──────────────────────────────────────────────────────────
async function checkSetupState() {
    try {
        const r = await fetch('/api/setup-state');
        const state = await r.json();
        if (!state.configured && !state.from_env) {
            showSetupWizard(state);
            return true;
        }
    } catch (e) {}
    return false;
}

// Polling interval (ms) for /api/setup-state after the wizard renders.
// 10s is the floor — /api/setup-state sits in the PUBLIC rate-limit tier
// (120/min shared per-IP). 10s = 6 req/min, safe headroom. 5s offers no
// UX benefit and burns the shared tier.
const _WIZARD_POLL_INTERVAL_MS = 10_000;
let _wizardPollTimer = null;
// Local 1s ticker between server polls so the counter updates smoothly
// every second instead of jumping in 10-second chunks. Holds the
// absolute deadline (Date.now() + remaining_secs*1000) from the last
// server response so no drift accumulates.
let _wizardLocalTick = null;
let _wizardDeadlineMs = 0;

function _formatRemaining(secs) {
    if (secs >= 60) {
        const m = Math.floor(secs / 60);
        const s = secs % 60;
        return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
    }
    return secs + 's';
}

function _paintCountdown(secs) {
    const el = document.getElementById('sw-countdown');
    if (!el) return;
    if (secs <= 0) {
        el.textContent = '';
        el.style.display = 'none';
        return;
    }
    // Self-labelling copy — previous "Nm Ss left" didn't say what was
    // counting down, so reviewers thought it was their session.
    el.textContent = 'Setup window closes in ' + _formatRemaining(secs);
    el.style.display = '';
}

function _applyWizardState(state) {
    const countdown = document.getElementById('sw-countdown');
    const wrap = document.getElementById('sw-wrap');
    const submitBtn = document.getElementById('sw-btn-save');
    const sessionBtn = document.getElementById('sw-btn-session');
    if (!countdown || !wrap) return;  // wizard DOM already torn down
    const remaining = (state && typeof state.window_expires_in === 'number')
        ? state.window_expires_in : null;
    const open = (state && typeof state.window_open === 'boolean')
        ? state.window_open : (remaining === null ? true : remaining > 0);
    if (open && remaining !== null) {
        _wizardDeadlineMs = Date.now() + (remaining * 1000);
        _paintCountdown(remaining);
        // Kick a 1s ticker if not already running — self-computes from
        // _wizardDeadlineMs so each server poll just re-anchors the deadline.
        if (_wizardLocalTick === null) {
            _wizardLocalTick = setInterval(function() {
                const secs = Math.max(0, Math.ceil((_wizardDeadlineMs - Date.now()) / 1000));
                _paintCountdown(secs);
                if (secs <= 0 && _wizardLocalTick !== null) {
                    clearInterval(_wizardLocalTick);
                    _wizardLocalTick = null;
                    // Flip to expired state immediately — don't wait
                    // for the next 10s poll to notice.
                    _applyWizardState({ window_open: false, window_expires_in: 0 });
                }
            }, 1000);
        }
        wrap.classList.remove('wizard-expired');
        if (submitBtn) submitBtn.disabled = false;
        if (sessionBtn && sessionBtn.dataset.tokenAck === '1') sessionBtn.disabled = false;
        if (window.statusBanner) window.statusBanner.clear('setup_window_expired');
    } else {
        countdown.style.display = 'none';
        wrap.classList.add('wizard-expired');
        if (submitBtn) submitBtn.disabled = true;
        if (sessionBtn) sessionBtn.disabled = true;
        if (window.statusBanner) {
            const msg = typeof t === 'function'
                ? t('banner.setup_window_expired')
                : 'Setup window expired — restart the server to try again.';
            window.statusBanner.set('setup_window_expired', { severity: 'error', message: msg });
        }
        // Stop polling + local tick — the window cannot reopen without
        // a server restart.
        if (_wizardPollTimer !== null) { clearInterval(_wizardPollTimer); _wizardPollTimer = null; }
        if (_wizardLocalTick !== null) { clearInterval(_wizardLocalTick); _wizardLocalTick = null; }
    }
    // Surface any active per-IP lockout. Separate key from window-expired
    // so the banner can show both if they coexist (e.g. locked out late
    // in the window).
    if (state && typeof state.lockout_remaining_secs === 'number' && state.lockout_remaining_secs > 0) {
        if (window.statusBanner) {
            const tpl = typeof t === 'function'
                ? t('banner.setup_lockout', { seconds: state.lockout_remaining_secs })
                : 'Too many failed setup attempts — try again in ' + state.lockout_remaining_secs + 's.';
            // Using expiresInMs so the banner auto-clears on lockout end;
            // include {seconds} placeholder for the statusBanner ticker.
            window.statusBanner.set('setup_lockout', {
                severity: 'error',
                message: 'Too many failed setup attempts — try again in {seconds}s.',
                expiresInMs: state.lockout_remaining_secs * 1000,
            });
            // Fall-through use of `tpl` silences lint without second render.
            void tpl;
        }
    } else if (window.statusBanner) {
        window.statusBanner.clear('setup_lockout');
    }
}

async function _pollSetupState() {
    if (document.visibilityState === 'hidden') return;  // pause when tab backgrounded
    try {
        const r = await fetch('/api/setup-state');
        if (!r.ok) return;
        const state = await r.json();
        if (state.configured) {
            // Wizard completed — stop polling and let app.js re-render.
            if (_wizardPollTimer !== null) { clearInterval(_wizardPollTimer); _wizardPollTimer = null; }
            return;
        }
        _applyWizardState(state);
    } catch (_e) { /* transient — next tick retries */ }
}

function _startWizardPolling(initialState) {
    _applyWizardState(initialState);
    if (_wizardPollTimer !== null) return;
    _wizardPollTimer = setInterval(_pollSetupState, _WIZARD_POLL_INTERVAL_MS);
    // Resume immediately after returning from a hidden tab.
    document.addEventListener('visibilitychange', function _onVisibility() {
        if (document.visibilityState === 'visible') _pollSetupState();
    });
}

function showSetupWizard(state) {
    document.body.innerHTML = '';
    // Restore #status-banner + #toast-container so statusBanner.set() and
    // toast() work from inside the wizard (polling may paint an expired
    // banner above the card). Both are at body-root per index.html.
    const banner = document.createElement('div');
    banner.id = 'status-banner';
    banner.className = 'status-banner';
    document.body.appendChild(banner);
    const toastContainer = document.createElement('div');
    toastContainer.id = 'toast-container';
    toastContainer.className = 'toast-container';
    document.body.appendChild(toastContainer);
    const wrap = document.createElement('div');
    wrap.id = 'sw-wrap';
    wrap.style.cssText = 'min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;font-family:system-ui,sans-serif;padding:20px;box-sizing:border-box;';
    // Use safe string values; server-supplied data assigned via textContent/value only
    const defaultSocket = String((state && state.tunnel_socket) || '/tmp/skiff-docker.sock');
    const tunnelActive = !!(state && state.tunnel_active);

    // Build the card using a static HTML template — no server data interpolated
    // Note: no inline onclick handlers — CSP script-src 'self' blocks them.
    // Event listeners are attached via addEventListener after appending to DOM.
    wrap.innerHTML =
      '<div style="background:#1e293b;border-radius:12px;padding:40px;width:520px;max-width:100%;box-shadow:0 20px 60px rgba(0,0,0,0.5);">' +
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">' +
          '<svg width="32" height="32" viewBox="0 0 28 28" fill="none"><rect width="28" height="28" rx="6" fill="#0d9488"/><rect x="6" y="6" width="16" height="16" rx="2" stroke="white" stroke-width="1.5" fill="none"/><line x1="6" y1="11" x2="22" y2="11" stroke="white" stroke-width="1.5"/><line x1="6" y1="16" x2="22" y2="16" stroke="white" stroke-width="1.5"/><circle cx="9" cy="8.5" r="1" fill="white"/><circle cx="9" cy="13.5" r="1" fill="white"/><circle cx="9" cy="18.5" r="1" fill="white"/></svg>' +
          '<span style="color:#f1f5f9;font-size:18px;font-weight:600;">SKIFF Container Manager</span>' +
          '<span id="sw-countdown" class="wizard-countdown" style="color:#94a3b8;font-size:12px;font-weight:400;margin-left:10px;display:none;"></span>' +
        '</div>' +
        '<p style="color:#94a3b8;font-size:13px;margin:0 0 24px;">First-run setup. Choose your Docker connection, generate a token, and start.</p>' +
        '<div style="display:flex;gap:0;margin-bottom:20px;border-radius:8px;overflow:hidden;border:1px solid #334155;">' +
          '<button id="sw-tab-local" style="flex:1;padding:10px;background:#0d9488;color:white;border:none;cursor:pointer;font-size:13px;font-weight:500;">Local / Custom</button>' +
          '<button id="sw-tab-tunnel" style="flex:1;padding:10px;background:transparent;color:#94a3b8;border:none;cursor:pointer;font-size:13px;font-weight:500;">SSH Tunnel</button>' +
        '</div>' +
        '<div id="sw-panel-local">' +
          '<label style="display:block;color:#94a3b8;font-size:12px;font-weight:500;margin-bottom:6px;letter-spacing:.05em;">DOCKER HOST</label>' +
          '<input id="sw-host-custom" type="text" style="width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #334155;color:#f1f5f9;border-radius:6px;padding:10px 12px;font-size:14px;margin-bottom:6px;outline:none;" placeholder="unix:///var/run/docker.sock"/>' +
          '<details style="color:#64748b;font-size:12px;margin:0 0 16px;"><summary style="cursor:pointer;color:#93c5fd;">Common values</summary>' +
            '<div style="margin-top:8px;font-family:monospace;font-size:11px;line-height:1.7;">' +
              '<div>Docker Desktop / OrbStack / dockerd / WSL2: <code style="color:#cbd5e1;">unix:///var/run/docker.sock</code></div>' +
              '<div>Colima: <code style="color:#cbd5e1;">unix://$HOME/.colima/default/docker.sock</code></div>' +
              '<div>Rancher Desktop: <code style="color:#cbd5e1;">unix://$HOME/.rd/docker.sock</code></div>' +
              '<div>Podman (rootless): <code style="color:#cbd5e1;">unix:///run/user/$UID/podman/podman.sock</code></div>' +
              '<div>Remote TCP (TLS): <code style="color:#cbd5e1;">tcp://10.0.0.5:2376</code></div>' +
            '</div>' +
          '</details>' +
        '</div>' +
        '<div id="sw-panel-tunnel" style="display:none;">' +
          '<label style="display:block;color:#94a3b8;font-size:12px;font-weight:500;margin-bottom:6px;letter-spacing:.05em;">SSH TARGET <span style="color:#64748b;font-weight:400;">user@host</span></label>' +
          '<div style="display:flex;gap:8px;margin-bottom:8px;">' +
            '<input id="sw-ssh-target" type="text" style="flex:1;background:#0f172a;border:1px solid #334155;color:#f1f5f9;border-radius:6px;padding:10px 12px;font-size:14px;outline:none;" placeholder="user@docker-host.example.com"/>' +
            '<button id="sw-tunnel-btn" style="background:#0d9488;color:white;border:none;border-radius:6px;padding:10px 16px;cursor:pointer;font-size:13px;white-space:nowrap;">Connect</button>' +
          '</div>' +
          '<div id="sw-tunnel-status" style="font-size:12px;margin-bottom:16px;min-height:18px;"></div>' +
        '</div>' +
        '<input id="sw-host" type="hidden"/>' +
        '<label style="display:block;color:#94a3b8;font-size:12px;font-weight:500;margin-bottom:6px;letter-spacing:.05em;">API TOKEN</label>' +
        '<div style="display:flex;gap:8px;margin-bottom:6px;">' +
          '<input id="sw-token" type="password" readonly autocomplete="off" style="flex:1;background:#0f172a;border:1px solid #334155;color:#f1f5f9;border-radius:6px;padding:10px 12px;font-size:13px;font-family:monospace;outline:none;" placeholder="Click Generate \u2192"/>' +
          '<button id="sw-gen-btn" style="background:#0d9488;color:white;border:none;border-radius:6px;padding:10px 16px;cursor:pointer;font-size:13px;white-space:nowrap;">Generate</button>' +
          '<button id="sw-copy-btn" style="background:#1e3a5f;color:#93c5fd;border:1px solid #1e40af;border-radius:6px;padding:10px 14px;cursor:pointer;font-size:13px;white-space:nowrap;">Copy</button>' +
        '</div>' +
        '<p id="sw-token-warn" style="color:#fbbf24;font-size:11px;margin:0 0 16px;display:none;">\u26a0 Copy this token or download the .env \u2014 it will not be shown again.</p>' +
        '<label style="display:block;color:#94a3b8;font-size:12px;font-weight:500;margin-bottom:6px;letter-spacing:.05em;">ALLOWED REGISTRIES <span style="color:#64748b;font-weight:400;">(comma-separated, empty = allow all)</span></label>' +
        '<input id="sw-regs" type="text" value="" style="width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #334155;color:#f1f5f9;border-radius:6px;padding:10px 12px;font-size:14px;margin-bottom:24px;outline:none;" placeholder="docker.io,ghcr.io,us-docker.pkg.dev/my-project/"/>' +
        '<div id="sw-error" style="display:none;color:#f87171;font-size:13px;margin-bottom:16px;"></div>' +
        '<div style="display:flex;gap:12px;">' +
          '<button id="sw-btn-save" style="flex:1;background:#0d9488;color:white;border:none;border-radius:6px;padding:12px;cursor:pointer;font-size:14px;font-weight:500;">Save .env &amp; Continue</button>' +
          '<button id="sw-btn-session" style="flex:1;background:#1e3a5f;color:#93c5fd;border:1px solid #1e40af;border-radius:6px;padding:12px;cursor:pointer;font-size:14px;font-weight:500;opacity:0.6;" disabled title="Copy the token first to unlock">In-memory only</button>' +
        '</div>' +
        '<p style="color:#475569;font-size:11px;text-align:center;margin:16px 0 0;"><strong>Save .env:</strong> downloads a config file; config persists across restarts. &nbsp; <strong>In-memory only:</strong> config lives in server RAM; setup again after any restart.</p>' +
      '</div>';
    document.body.appendChild(wrap);

    // Attach event listeners (CSP blocks inline onclick handlers)
    document.getElementById('sw-tab-tunnel').addEventListener('click', function() { swSetMode('tunnel'); });
    document.getElementById('sw-tab-local').addEventListener('click', function() { swSetMode('local'); });
    document.getElementById('sw-tunnel-btn').addEventListener('click', swConnectTunnel);
    // Token lifecycle: Generate → user must acknowledge (Copy or Save .env) → unlock
    // "In-memory only" button. This prevents the footgun where a generated token is
    // saved server-side but the user closes the tab without capturing it.
    function _unlockSessionBtn() {
        var btn = document.getElementById('sw-btn-session');
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.removeAttribute('title');
        // Mark as "ack'd by user so the setup-state poller doesn't re-lock
        // this button when it re-runs _applyWizardState on the next tick.
        btn.dataset.tokenAck = '1';
    }
    document.getElementById('sw-gen-btn').addEventListener('click', function() {
        document.getElementById('sw-token').value = Array.from(crypto.getRandomValues(new Uint8Array(24))).map(function(b) { return b.toString(16).padStart(2, '0'); }).join('');
        document.getElementById('sw-token-warn').style.display = 'block';
        // Re-lock session-only button when a fresh token is generated
        var sbtn = document.getElementById('sw-btn-session');
        sbtn.disabled = true;
        sbtn.style.opacity = '0.6';
        sbtn.title = 'Copy the token first to unlock';
        delete sbtn.dataset.tokenAck;  // keep the poller from auto-reenabling
    });
    document.getElementById('sw-copy-btn').addEventListener('click', function() {
        var val = document.getElementById('sw-token').value;
        if (!val) { return; }
        var btn = document.getElementById('sw-copy-btn');
        // Unlock the session-only button on click intent regardless of whether the
        // async clipboard write resolves. In some headless / iframed browser contexts
        // the Clipboard API returns a rejected promise even when the user pressed the
        // button — coupling the footgun guard to clipboard success would block legit
        // users from proceeding. Clipboard failure surfaces separately in the label.
        _unlockSessionBtn();
        navigator.clipboard.writeText(val).then(function() {
            btn.textContent = 'Copied!';
            setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
        }).catch(function() {
            btn.textContent = 'Select + \u2318C';
            setTimeout(function() { btn.textContent = 'Copy'; }, 2500);
            // Select the token so the user can press Cmd/Ctrl-C themselves
            var inp = document.getElementById('sw-token');
            inp.removeAttribute('readonly'); inp.select(); inp.setAttribute('readonly', 'true');
        });
    });
    document.getElementById('sw-btn-save').addEventListener('click', function() { swSubmit(true); });
    document.getElementById('sw-btn-session').addEventListener('click', function() {
        if (document.getElementById('sw-btn-session').disabled) return;
        swSubmit(false);
    });

    // Set server-supplied values safely via DOM properties (never via innerHTML interpolation)
    const hostInput = document.getElementById('sw-host');
    const statusEl = document.getElementById('sw-tunnel-status');
    const customInput = document.getElementById('sw-host-custom');
    customInput.value = 'unix:///var/run/docker.sock';
    customInput.addEventListener('input', function() {
        if (document.getElementById('sw-panel-local').style.display !== 'none') {
            hostInput.value = customInput.value.trim() || 'unix:///var/run/docker.sock';
        }
    });

    // Probe the local runtime so novice users see "✓ Docker detected" instead
    // of guessing which socket to paste. If the default /var/run/docker.sock is
    // reachable, leave it. If another path (e.g. Colima's) is reachable, pre-fill
    // that and flag it. Unauth endpoint, only active pre-setup.
    fetch(API + '/setup/probe-docker')
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(probe) {
        if (!probe) return;
        var hint = document.getElementById('sw-host-custom');
        var hintHolder = document.createElement('div');
        hintHolder.id = 'sw-host-detect';
        hintHolder.style.cssText = 'font-size:11px;margin-top:4px;font-weight:500';
        // Remove any existing detect hint (re-runs on page refresh)
        var old = document.getElementById('sw-host-detect');
        if (old) old.remove();
        if (probe.reachable && probe.reachable.length) {
          var winner = probe.reachable[0];
          hint.value = winner;
          hostInput.value = winner;
          hintHolder.style.color = '#4ade80';
          hintHolder.textContent = '\u2713 Runtime detected at ' + winner +
            (probe.reachable.length > 1 ? ' (and ' + (probe.reachable.length - 1) + ' other)' : '');
        } else {
          hintHolder.style.color = '#94a3b8';
          hintHolder.textContent = 'No runtime detected. Start one first (see Containers tab after setup for help).';
        }
        hint.parentNode.insertBefore(hintHolder, hint.nextSibling);
      })
      .catch(function() { /* unauth or rate-limited — never block the wizard */ });
    if (tunnelActive) {
        // A prior tunnel is still open — default to SSH Tunnel tab with the live socket
        // as the host, so the user can reuse it instead of accidentally switching to Local.
        swSetMode('tunnel');
        hostInput.value = 'unix://' + defaultSocket;
        statusEl.style.color = '#4ade80';
        statusEl.textContent = '\u2713 Tunnel active \u2014 ' + defaultSocket;
    } else {
        statusEl.style.color = '#64748b';
        statusEl.textContent = 'Requires key-based SSH auth (no passphrase).';
        // Default tab is Local — sw-host tracks the Local input
        hostInput.value = customInput.value;
    }
    // Kick off the setup-state poller so the countdown ticks + an expired
    // banner paints the moment the window closes. Uses the initial `state`
    // so the first paint happens without waiting for the first poll.
    _startWizardPolling(state);
}

function swSetMode(mode) {
    const isTunnel = mode === 'tunnel';
    document.getElementById('sw-panel-tunnel').style.display = isTunnel ? 'block' : 'none';
    document.getElementById('sw-panel-local').style.display = isTunnel ? 'none' : 'block';
    document.getElementById('sw-tab-tunnel').style.background = isTunnel ? '#0d9488' : 'transparent';
    document.getElementById('sw-tab-tunnel').style.color = isTunnel ? 'white' : '#94a3b8';
    document.getElementById('sw-tab-local').style.background = isTunnel ? 'transparent' : '#0d9488';
    document.getElementById('sw-tab-local').style.color = isTunnel ? '#94a3b8' : 'white';
    if (!isTunnel) {
        document.getElementById('sw-host').value = document.getElementById('sw-host-custom').value.trim() || 'unix:///var/run/docker.sock';
    }
}

async function swConnectTunnel() {
    const target = document.getElementById('sw-ssh-target').value.trim();
    const statusEl = document.getElementById('sw-tunnel-status');
    const btn = document.getElementById('sw-tunnel-btn');
    if (!target) { statusEl.style.color = '#f87171'; statusEl.textContent = 'Enter user@host first.'; return; }
    statusEl.style.color = '#94a3b8';
    statusEl.textContent = 'Connecting\u2026';
    btn.disabled = true;
    try {
        const r = await fetch('/api/setup/tunnel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Requested-With': 'ContainerManager'},
            body: JSON.stringify({ssh_target: target}),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            _renderTunnelError(statusEl, d && d.detail, target);
        } else {
            statusEl.style.color = '#4ade80';
            // Use DOM methods to insert server-supplied socket_path safely
            while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
            statusEl.appendChild(document.createTextNode('\u2713 Tunnel active \u2014 ' + d.socket_path));
            document.getElementById('sw-host').value = d.docker_host;
            // Clear tunnel credentials — they have served their purpose
            sessionStorage.removeItem('tunnelUser');
            sessionStorage.removeItem('tunnelHost');
        }
    } catch (e) {
        statusEl.style.color = '#f87171';
        statusEl.textContent = '\u2717 Could not reach server';
    }
    btn.disabled = false;
}

// Render a classified SSH tunnel error with actionable guidance.
// `detail` is either a string (plain error) or object {message, code, help}.
function _renderTunnelError(statusEl, detail, target) {
    statusEl.style.color = '#f87171';
    while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
    var code = (detail && typeof detail === 'object') ? (detail.code || 'other') : 'other';
    var msg = (detail && typeof detail === 'object') ? (detail.message || 'Connection failed') : String(detail || 'Connection failed');
    var help = (detail && typeof detail === 'object') ? (detail.help || '') : '';
    var line1 = document.createElement('div');
    line1.textContent = '\u2717 ' + msg;
    statusEl.appendChild(line1);
    if (help) {
        var line2 = document.createElement('div');
        line2.style.cssText = 'color:#94a3b8;margin-top:4px;';
        line2.textContent = help;
        statusEl.appendChild(line2);
    }
    // For auth_failed / no_key / host_key_mismatch, surface a copyable terminal command.
    // The command is constructed from the user-entered target (already validated as user@host
    // by the regex on the submit path) — assigned via textContent so no HTML injection.
    if (code === 'auth_failed' || code === 'no_key' || code === 'host_key_mismatch') {
        var cmd = document.createElement('div');
        cmd.style.cssText = 'margin-top:8px;background:#0f172a;border:1px solid #334155;border-radius:4px;padding:8px 10px;font-family:monospace;font-size:12px;color:#e2e8f0;cursor:pointer;user-select:all;';
        cmd.title = 'Click to copy';
        var shellCmd = (code === 'host_key_mismatch')
            ? 'ssh ' + target
            : (code === 'no_key')
                ? 'ssh-keygen -t ed25519 && ssh-copy-id ' + target
                : 'ssh-copy-id ' + target;
        cmd.textContent = shellCmd;
        cmd.addEventListener('click', function() {
            navigator.clipboard.writeText(shellCmd).then(function() {
                cmd.style.outline = '2px solid #22c55e';
                setTimeout(function() { cmd.style.outline = ''; }, 1200);
            });
        });
        statusEl.appendChild(cmd);
        var note = document.createElement('div');
        note.style.cssText = 'color:#64748b;font-size:11px;margin-top:4px;';
        note.textContent = 'Run this in your terminal, then click Connect again. SKIFF never sees your password.';
        statusEl.appendChild(note);
    }
}

async function swSubmit(saveEnv) {
    const isTunnel = document.getElementById('sw-panel-tunnel').style.display !== 'none';
    let host = document.getElementById('sw-host').value.trim();
    if (!isTunnel) {
        host = document.getElementById('sw-host-custom').value.trim() || host;
    }
    const token = document.getElementById('sw-token').value.trim();
    const regs = document.getElementById('sw-regs').value.trim();
    const errEl = document.getElementById('sw-error');
    errEl.style.display = 'none';
    if (isTunnel) {
        const statusEl = document.getElementById('sw-tunnel-status');
        if (!statusEl || !statusEl.textContent.startsWith('\u2713')) {
            errEl.textContent = 'Connect the SSH tunnel first, or switch to Local / Custom.';
            errEl.style.display = 'block';
            return;
        }
    }
    if (!host) { errEl.textContent = 'Docker host is required.'; errEl.style.display = 'block'; return; }
    if (!token || token.length < 16) { errEl.textContent = 'Generate a token first (minimum 16 characters).'; errEl.style.display = 'block'; return; }

    if (saveEnv) {
        const lines = [
            'API_TOKEN=' + token,
            'DOCKER_HOST=' + host,
            regs ? 'ALLOWED_REGISTRIES=' + regs : '# ALLOWED_REGISTRIES=docker.io,ghcr.io',
            '# ALLOWED_ORIGINS=http://127.0.0.1:8080',
            '# AUDIT_LOG=./audit.jsonl',
        ].join('\n');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([lines], {type: 'text/plain'}));
        a.download = '.env';
        a.click();
    }

    try {
        const r = await fetch('/api/setup', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Requested-With': 'ContainerManager'},
            body: JSON.stringify({docker_host: host, api_token: token, allowed_registries: regs}),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            // `detail` is either a string (legacy envelope) or the new
            // {code, message, help} object. Extract `.message` so the
            // user sees the actual reason rather than "[object Object]".
            var detail = d && d.detail;
            var msg;
            if (detail && typeof detail === 'object') {
                msg = detail.message || detail.code || 'Setup failed.';
            } else {
                msg = detail || 'Setup failed.';
            }
            errEl.textContent = msg;
            errEl.style.display = 'block';
            return;
        }
    } catch (e) {
        errEl.textContent = 'Could not reach server.';
        errEl.style.display = 'block';
        return;
    }

    sessionStorage.setItem('api_token', token);
    location.reload();
}
