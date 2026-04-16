# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Authentication, CSRF verification, session tracking, and WebSocket auth helpers.

Imports only from skiff.config — no other skiff modules — to avoid circular imports.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
import time
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from starlette.websockets import WebSocket

from skiff.config import (
    _SESSION_CACHE_MAX,
    MIN_TOKEN_LENGTH,  # noqa: F401 — re-exported for setup validation
    SESSION_ABS_TIMEOUT,
    WS_AUTH_LOCKOUT_SECS,
    WS_AUTH_MAX_ATTEMPTS,
    WS_KEEPALIVE_INTERVAL,
    WS_KEEPALIVE_REVALIDATE_EVERY,
    WS_TOKEN_TIMEOUT,
    _cfg,
)

# ── Constant-time comparison ───────────────────────────────

def _constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


# ── Server-side session age tracking ──────────────────────
# Maps token_hash → first-seen timestamp. Rejects tokens older than SESSION_ABS_TIMEOUT.
# Cleared when _cfg.api_token is rotated (setup endpoint calls _invalidate_session_cache).
#
# Design note: SKIFF uses a single shared token (not per-user tokens), so there is
# typically only one entry in this dict. The dict is capped to prevent unbounded growth
# in any future multi-token scenario. Server restart resets the clock.
_session_first_seen: dict[str, float] = {}
_session_lock = threading.Lock()


def _check_session_age(token: str) -> None:
    """Reject tokens that have been active longer than SESSION_ABS_TIMEOUT.

    HMAC-SHA256 is used (not plain SHA256) so the digest is keyed, making it
    unsuitable as a direct lookup for brute-force on the original token value.
    This is a session-presence cache key, not a password hash.
    """
    # Use HMAC-SHA256 (keyed MAC) to derive the cache key — CodeQL recognises
    # hmac.new as an appropriate use of SHA256, unlike bare hashlib.sha256().
    token_hash = hmac.new(b"skiff-session-cache-v1", token.encode(), "sha256").hexdigest()
    now = time.monotonic()
    with _session_lock:
        first = _session_first_seen.get(token_hash)
        if first is None:
            if len(_session_first_seen) >= _SESSION_CACHE_MAX:
                oldest = min(_session_first_seen, key=_session_first_seen.__getitem__)
                del _session_first_seen[oldest]
            _session_first_seen[token_hash] = now
        elif (now - first) > SESSION_ABS_TIMEOUT:
            raise HTTPException(401, "Session expired — please reload and re-authenticate")


def _invalidate_session_cache() -> None:
    """Clear session age tracking (call when token is rotated)."""
    with _session_lock:
        _session_first_seen.clear()


# ── HTTP auth dependencies ─────────────────────────────────

def verify_auth(request: Request) -> None:
    """Dependency: verifies bearer token and enforces server-side session lifetime."""
    if not _cfg.api_token:
        return
    auth = request.headers.get("Authorization", "")
    if not _constant_time_compare(auth, f"Bearer {_cfg.api_token}"):
        raise HTTPException(401, "Invalid or missing API token")
    _check_session_age(auth[7:])  # track age of valid tokens only


def verify_auth_strict(request: Request) -> None:
    """Like verify_auth but always requires a token — used for sensitive endpoints."""
    if not _cfg.api_token:
        raise HTTPException(503, "Server not configured — set API_TOKEN before accessing this endpoint")
    auth = request.headers.get("Authorization", "")
    if not _constant_time_compare(auth, f"Bearer {_cfg.api_token}"):
        raise HTTPException(401, "Invalid or missing API token")
    _check_session_age(auth[7:])


def verify_csrf(request: Request) -> None:
    """Dependency: verify CSRF sentinel header on mutating requests."""
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        xrw = request.headers.get("X-Requested-With", "")
        if xrw != "ContainerManager":
            raise HTTPException(403, "Missing or invalid X-Requested-With header")


# Common dependency list for all authenticated endpoints
AUTH = [Depends(verify_auth)]


# ── WebSocket origin validation ────────────────────────────

def _validate_ws_origin(websocket: WebSocket) -> bool:
    """Reject WebSocket upgrades from origins not in the CORS allowlist."""
    if not _cfg.allowed_origins:
        return True
    if "*" in _cfg.allowed_origins:
        return True
    origin = websocket.headers.get("origin", "")
    if not origin:
        return False  # No Origin header — reject (browsers always send for cross-origin WS)
    # Check against the explicit allowlist
    if any(origin == o or origin.startswith(o.rstrip("/") + "/") for o in _cfg.allowed_origins):
        return True
    # Allow same-host (reverse proxy / direct access where Origin host == Host header)
    try:
        origin_host = urlparse(origin).hostname or ""
        request_host = websocket.headers.get("host", "").split(":")[0]
        return bool(origin_host and request_host and origin_host == request_host)
    except Exception:
        return False


# ── WebSocket brute-force protection ──────────────────────
_ws_auth_failures: dict[str, tuple[int, float]] = {}
_ws_auth_lock = threading.Lock()


async def _validate_ws_token_from_message(websocket: WebSocket) -> bool:
    """Authenticate a WebSocket connection via the first text message.

    Tokens are sent as 'AUTH <token>' rather than in URL query params to avoid
    leaking into proxy access logs. Applies per-IP lockout after WS_AUTH_MAX_ATTEMPTS
    consecutive failures within WS_AUTH_LOCKOUT_SECS.
    """
    if not _cfg.api_token:
        return True
    client_ip = websocket.client.host if websocket.client else "unknown"
    now = time.monotonic()
    with _ws_auth_lock:
        if client_ip in _ws_auth_failures:
            count, last_t = _ws_auth_failures[client_ip]
            if count >= WS_AUTH_MAX_ATTEMPTS and (now - last_t) < WS_AUTH_LOCKOUT_SECS:
                return False
            if (now - last_t) >= WS_AUTH_LOCKOUT_SECS:
                del _ws_auth_failures[client_ip]
    try:
        first_msg = await asyncio.wait_for(websocket.receive_text(), timeout=WS_TOKEN_TIMEOUT)
    except Exception:
        return False
    token = first_msg[5:] if first_msg.startswith("AUTH ") else ""
    if not token or not _constant_time_compare(token, _cfg.api_token):
        if token:  # had AUTH prefix but wrong value — count as a failed attempt
            with _ws_auth_lock:
                count, _ = _ws_auth_failures.get(client_ip, (0, now))
                _ws_auth_failures[client_ip] = (count + 1, time.monotonic())
        return False
    try:
        _check_session_age(token)
    except HTTPException:
        return False
    with _ws_auth_lock:
        _ws_auth_failures.pop(client_ip, None)
    return True


async def ws_keepalive(websocket: WebSocket) -> None:
    """Periodic keepalive coroutine for WebSocket connections.

    Sends a null byte every WS_KEEPALIVE_INTERVAL seconds and revalidates the
    session age every WS_KEEPALIVE_REVALIDATE_EVERY ticks. Closes with code 4003
    if the session has expired.
    """
    ticks = 0
    while True:
        await asyncio.sleep(WS_KEEPALIVE_INTERVAL)
        ticks += 1
        try:
            if ticks >= WS_KEEPALIVE_REVALIDATE_EVERY:
                ticks = 0
                _check_session_age(_cfg.api_token)
            await websocket.send_text("\x00")
        except HTTPException:
            await websocket.close(code=4003)
            break
        except Exception:
            break
