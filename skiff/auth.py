# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Authentication, CSRF verification, session tracking, and WebSocket auth helpers.

Imports only from skiff.config — no other skiff modules — to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
from urllib.parse import urlparse

import structlog
from fastapi import Depends, HTTPException, Request
from starlette.websockets import WebSocket, WebSocketDisconnect

from skiff import config
from skiff.contract.errors import http_error

log = structlog.get_logger(__name__)

# ── Constant-time comparison ───────────────────────────────


def constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


# ── Server-side session age tracking ──────────────────────
# Maps a non-secret cache key → first-seen timestamp.
# Rejects tokens older than config.SESSION_ABS_TIMEOUT.
# Cleared when config._cfg.api_token is rotated (setup endpoint calls _invalidate_session_cache).
#
# Design note: SKIFF uses a single shared token (not per-user tokens), so there is
# typically only one entry in this dict. The dict is capped to prevent unbounded growth
# in any future multi-token scenario. Server restart resets the clock.
_session_first_seen: dict[str, float] = {}
# Tracks the last-seen time per session cache key so the server can
# enforce SESSION_IDLE_SECS. Prior to this, the idle window was
# advertised as a knob but only applied client-side — a bearer token
# leaked to a log file stayed valid for the full absolute timeout
# (default 8 h) regardless of activity.
_session_last_seen: dict[str, float] = {}
_session_lock = threading.Lock()
# Process-local salt — rebound on every process start so the cache key is
# non-stable across restarts; an attacker cannot precompute the key space.
_SESSION_CACHE_SALT = secrets.token_bytes(32)


def _check_session_age(token: str) -> None:
    """Enforce both absolute + idle session lifetimes.

    Absolute: reject tokens first seen more than `SESSION_ABS_TIMEOUT`
    seconds ago.

    Idle: reject tokens whose most recent verified use was more than
    `SESSION_IDLE_SECS` seconds ago. Updates `_session_last_seen` on
    every successful verification so legitimate activity keeps the
    window alive.

    The cache key is an HMAC-SHA256 of the token under a process-local
    salt — non-reversible, collision-resistant, and does not store the
    token itself.
    """
    # Salted HMAC over the raw token — never log or persist.
    cache_key = hmac.new(_SESSION_CACHE_SALT, token.encode(), "sha256").hexdigest()
    now = time.monotonic()
    with _session_lock:
        first = _session_first_seen.get(cache_key)
        if first is None:
            if len(_session_first_seen) >= config._SESSION_CACHE_MAX:
                oldest = min(_session_first_seen, key=_session_first_seen.__getitem__)
                del _session_first_seen[oldest]
                _session_last_seen.pop(oldest, None)
            _session_first_seen[cache_key] = now
            _session_last_seen[cache_key] = now
            return
        if (now - first) > config.SESSION_ABS_TIMEOUT:
            raise http_error("auth.session_expired")
        last = _session_last_seen.get(cache_key, first)
        if (now - last) > config.SESSION_IDLE_SECS:
            raise http_error("auth.session_expired")
        _session_last_seen[cache_key] = now


def _invalidate_session_cache() -> None:
    """Clear session age tracking (call when token is rotated)."""
    with _session_lock:
        _session_first_seen.clear()
        _session_last_seen.clear()


# ── HTTP auth dependencies ─────────────────────────────────


def verify_auth(request: Request) -> None:
    """Dependency: verifies bearer token and enforces server-side session lifetime.

    Distinguishes three cases so SIEM rules can alert separately:

      * no server token configured → allow (insecure-mode, warned at boot);
      * Authorization header absent → `auth.missing_token`;
      * Authorization header present but wrong → `auth.invalid_token`.

    A burst of `auth.missing_token` from one IP means a script is
    probing without credentials; a burst of `auth.invalid_token`
    means the attacker has a token shape and is guessing values —
    distinct patterns worth distinct alerts.
    """
    if not config._cfg.api_token:
        return
    auth = request.headers.get("Authorization", "")
    if not auth:
        raise http_error("auth.missing_token")
    if not constant_time_compare(auth, f"Bearer {config._cfg.api_token}"):
        raise http_error("auth.invalid_token")
    _check_session_age(auth[7:])  # track age of valid tokens only


def verify_auth_strict(request: Request) -> None:
    """Like verify_auth but always requires a token — used for sensitive endpoints."""
    if not config._cfg.api_token:
        raise http_error("auth.not_configured")
    auth = request.headers.get("Authorization", "")
    if not auth:
        raise http_error("auth.missing_token")
    if not constant_time_compare(auth, f"Bearer {config._cfg.api_token}"):
        raise http_error("auth.invalid_token")
    _check_session_age(auth[7:])


def verify_csrf(request: Request) -> None:
    """Dependency: verify CSRF sentinel header on mutating requests.

    Distinguishes between the header being absent vs. the header being
    present with a wrong value — operators tracking CSRF incidents in a
    SIEM can separate "client never sent it" (probably misconfigured)
    from "client sent something else" (possibly scripted attack).
    """
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        xrw = request.headers.get("X-Requested-With")
        if xrw is None or xrw == "":
            raise http_error("auth.csrf_missing")
        if xrw != "ContainerManager":
            raise http_error("auth.csrf_invalid")


# Common dependency list for all authenticated endpoints
AUTH = [Depends(verify_auth)]


# ── WebSocket origin validation ────────────────────────────


def _origin_in_allowlist(origin: str) -> bool:
    """True if `origin` matches the configured allowlist (exact or prefix)."""
    return any(origin == o or origin.startswith(o.rstrip("/") + "/") for o in config._cfg.allowed_origins)


def _origin_matches_host(origin: str, host_header: str) -> bool:
    """True if Origin's host == Host header's host (same-host reverse-proxy case)."""
    try:
        origin_host = urlparse(origin).hostname or ""
    except (ValueError, AttributeError):
        return False
    request_host = host_header.split(":", 1)[0]
    return bool(origin_host and request_host and origin_host == request_host)


def _validate_ws_origin(websocket: WebSocket) -> bool:
    """Reject WebSocket upgrades from origins not in the CORS allowlist.

    Wildcard entries are rejected at config load (see
    `skiff.config._csv_list_no_wildcard`), so `allowed_origins` here is
    always a concrete list or empty.
    """
    allowlist = config._cfg.allowed_origins
    if not allowlist:
        return True
    origin = websocket.headers.get("origin", "")
    if not origin:
        return False  # No Origin header — browsers always send one for cross-origin WS
    if _origin_in_allowlist(origin):
        return True
    return _origin_matches_host(origin, websocket.headers.get("host", ""))


# ── WebSocket brute-force protection ──────────────────────
_ws_auth_failures: dict[str, tuple[int, float]] = {}
_ws_auth_lock = threading.Lock()


def _ws_is_locked_out(client_ip: str, now: float) -> bool:
    """Per-IP brute-force lockout check. Also evicts stale entries."""
    with _ws_auth_lock:
        entry = _ws_auth_failures.get(client_ip)
        if entry is None:
            return False
        count, last_t = entry
        if count >= config.WS_AUTH_MAX_ATTEMPTS and (now - last_t) < config.WS_AUTH_LOCKOUT_SECS:
            return True
        if (now - last_t) >= config.WS_AUTH_LOCKOUT_SECS:
            del _ws_auth_failures[client_ip]
    return False


def ws_lockout_remaining(client_ip: str) -> int:
    """Return seconds remaining on this IP's WS-auth lockout, 0 if not locked.

    Used by /api/config (so the UI can paint a banner on page load when a
    prior tab tripped the lockout) and by the WS handshake close-reason
    builder (so `evt.reason = 'ws_auth_lockout:<N>'` carries the countdown
    to the client without a second round-trip). Reveals only the caller's
    own remaining window — an attacker would already know they're locked.

    Does not evict stale entries; read-only vs. `_ws_is_locked_out` which
    also cleans up. Callers that want the enforcement side-effect should
    keep using `_ws_is_locked_out`.
    """
    now = time.monotonic()
    with _ws_auth_lock:
        entry = _ws_auth_failures.get(client_ip)
        if entry is None:
            return 0
        count, last_t = entry
        if count < config.WS_AUTH_MAX_ATTEMPTS:
            return 0
        remaining = config.WS_AUTH_LOCKOUT_SECS - (now - last_t)
        if remaining <= 0:
            return 0
        return int(remaining)


def _ws_record_failure(client_ip: str) -> None:
    """Increment per-IP failure counter. Emits `audit.ws_auth_lockout` on
    the exact attempt that crosses the threshold so SIEM rules can
    alert on lockout activation without tailing every failed attempt.
    """
    now = time.monotonic()
    with _ws_auth_lock:
        count, _ = _ws_auth_failures.get(client_ip, (0, now))
        new_count = count + 1
        _ws_auth_failures[client_ip] = (new_count, now)
        just_tripped = count < config.WS_AUTH_MAX_ATTEMPTS and new_count >= config.WS_AUTH_MAX_ATTEMPTS
    if just_tripped:
        log.warning(
            "audit.ws_auth_lockout",
            remote=client_ip,
            attempts=new_count,
            lockout_secs=config.WS_AUTH_LOCKOUT_SECS,
        )


def _ws_clear_failures(client_ip: str) -> None:
    """Reset the per-IP counter after a successful authentication."""
    with _ws_auth_lock:
        _ws_auth_failures.pop(client_ip, None)


async def _ws_receive_token(websocket: WebSocket) -> str | None:
    """Read the first `AUTH <token>` message. Returns token or None on any failure.

    Tokens are sent as WebSocket messages (not URL query params) to avoid
    leaking into proxy access logs.
    """
    try:
        first_msg = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=config.WS_TOKEN_TIMEOUT,
        )
    except (TimeoutError, WebSocketDisconnect, RuntimeError):
        # TimeoutError: client never sent AUTH within the window.
        # WebSocketDisconnect: client closed mid-handshake.
        # RuntimeError: starlette on message after disconnect.
        return None
    return first_msg[5:] if first_msg.startswith("AUTH ") else None


async def _validate_ws_token_from_message(websocket: WebSocket) -> bool:
    """Authenticate a WebSocket connection via its first `AUTH <token>` message.

    Linear check: lockout → receive token → constant-time compare → session
    age. Failure paths record the per-IP counter; success clears it. On
    success, the authenticated token is attached to the socket for the
    keepalive to recheck against the current server token — so an
    operator token rotation force-closes every active WS.
    """
    if not config._cfg.api_token:
        return True
    client_ip = websocket.client.host if websocket.client else "unknown"
    if _ws_is_locked_out(client_ip, time.monotonic()):
        return False
    token = await _ws_receive_token(websocket)
    if not token:
        # Malformed first message (missing `AUTH ` prefix, empty body,
        # non-text frame, timeout) is ALSO a brute-force signal —
        # flooding the handshake path with non-AUTH frames would
        # otherwise bypass `_ws_record_failure` and never trip
        # `audit.ws_auth_lockout`. Record as a failure so the lockout
        # threshold catches persistent noise, malformed clients, and
        # fuzzers alike.
        _ws_record_failure(client_ip)
        return False
    if not constant_time_compare(token, config._cfg.api_token):
        _ws_record_failure(client_ip)
        return False
    try:
        _check_session_age(token)
    except HTTPException:
        return False
    _ws_clear_failures(client_ip)
    websocket.state.auth_token = token  # bound for keepalive revalidation
    return True


def _ws_tick(ticks: int) -> tuple[int, bool]:
    """Advance the keepalive tick counter. Returns (next_ticks, should_revalidate)."""
    ticks += 1
    if ticks >= config.WS_KEEPALIVE_REVALIDATE_EVERY:
        return 0, True
    return ticks, False


def _ws_revalidate(ws_token: str) -> None:
    """Raise HTTPException if the WS session should end (rotation or age)."""
    server_token = config._cfg.api_token
    if server_token and ws_token and not constant_time_compare(ws_token, server_token):
        raise http_error("auth.session_expired")
    _check_session_age(ws_token or server_token)


def _ws_read_token(websocket: WebSocket) -> str:
    """Extract the auth token bound at handshake, if present."""
    state = websocket.state
    token = state.auth_token if hasattr(state, "auth_token") else ""
    return token if isinstance(token, str) else ""


async def ws_keepalive(websocket: WebSocket) -> None:
    """Periodic keepalive coroutine for WebSocket connections.

    Sends a null byte every config.WS_KEEPALIVE_INTERVAL seconds and revalidates
    every config.WS_KEEPALIVE_REVALIDATE_EVERY ticks. Revalidation checks both
    absolute session age AND that the token presented at handshake still equals
    the current server token — a rotation (setup endpoint) mismatches, so every
    live WS closes with 4003.
    """
    ticks = 0
    ws_token = _ws_read_token(websocket)
    while True:
        await asyncio.sleep(config.WS_KEEPALIVE_INTERVAL)
        ticks, revalidate_now = _ws_tick(ticks)
        try:
            if revalidate_now:
                _ws_revalidate(ws_token)
            await websocket.send_text("\x00")
        except HTTPException:
            await websocket.close(code=4003)
            break
        except (WebSocketDisconnect, RuntimeError, OSError):
            break
