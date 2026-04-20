# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Container WebSocket endpoints — live log streaming + interactive exec.

Separated from skiff.routers.containers so the HTTP-handler file
(~750 lines) and the WS-handler file (~150 lines) each fit in one
read. WS handlers use asyncio, starlette.websockets, and raw
docker-socket I/O; HTTP handlers are plain sync FastAPI routes.
They share no code beyond the validators + auth modules.

Per-IP connection limiting (`_ws_acquire` / `_ws_release`) lives
here because only WebSocket paths reach it. The shared mutable state
(`_ws_connections` dict, `_ws_lock` lock) is module-local — tests
patch it at `skiff.routers.containers_ws._ws_connections` etc.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import threading
import time
from typing import Any

import docker.errors
import structlog
from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from skiff import auth, config, validators
from skiff.docker_client import get_client

log = structlog.get_logger(__name__)
router = APIRouter()

# ── Per-IP WebSocket connection rate limiting ──────────────
_ws_connections: dict[str, int] = collections.defaultdict(int)
_ws_lock = threading.Lock()

# Track live exec sessions so a runtime profile switch to reviewer can
# forcibly close them. Set element is (WebSocket, container_id) so the
# close-all path can log which session was killed. Mutated from the
# async handler; the lock is still a threading.Lock since SIGINT / the
# profile-switch caller can be on a different thread than the event
# loop.
_active_exec_ws: set = set()


def _ws_acquire(ip: str) -> bool:
    """Reserve one WS slot for `ip`; return True on success.

    Historically this raised `HTTPException`, but WebSocket handlers
    cannot translate an HTTPException into a close frame — the socket
    is already upgraded by handshake time. Returning a bool lets the
    caller close the socket with the correct WebSocket close code
    (1013 "try again later") and then return cleanly.
    """
    with _ws_lock:
        if _ws_connections[ip] >= config.WS_MAX_PER_IP:
            return False
        _ws_connections[ip] += 1
        return True


def _ws_release(ip: str) -> None:
    with _ws_lock:
        _ws_connections[ip] = max(0, _ws_connections[ip] - 1)


def _try_register_exec_ws(websocket: WebSocket, container_id: str) -> bool:
    """Atomic PROFILE re-check + registration under `_ws_lock`.

    Returns False if PROFILE has moved to `reviewer` between the
    handshake and the registration. Closing the lock window between
    the check and the set insertion would reopen the TOCTOU where a
    handler that passed the earlier guard registers AFTER
    `close_active_exec_sessions` has snapshotted — leaving a live
    exec shell in reviewer mode.
    """
    import skiff.config as _config_module

    with _ws_lock:
        if _config_module.PROFILE == "reviewer":
            return False
        _active_exec_ws.add((websocket, container_id))
        return True


def _unregister_exec_ws(websocket: WebSocket, container_id: str) -> None:
    with _ws_lock:
        _active_exec_ws.discard((websocket, container_id))


async def close_active_exec_sessions(reason: str) -> int:
    """Close every live exec WebSocket with code 4003.

    Called when PROFILE transitions to reviewer at runtime so an
    insider can't keep mutating through a pre-switch exec shell.
    Callers MUST flip `PROFILE = "reviewer"` BEFORE invoking this —
    `_try_register_exec_ws` relies on the flag being set under the
    same lock, so any handler in the acquire→register gap will fail
    the re-check and be rejected. Closes are parallel (asyncio.gather)
    so a slow TCP send-buffer on one session does not delay the others.
    Returns the count for audit emission.
    """
    with _ws_lock:
        pending = list(_active_exec_ws)
        _active_exec_ws.clear()
    if not pending:
        return 0

    async def _close_one(ws: WebSocket, cid: str) -> None:
        with contextlib.suppress(RuntimeError, OSError):
            await ws.close(code=4003)
        log.info("audit.ws_exec_terminated", container=cid, reason=reason)

    await asyncio.gather(
        *(_close_one(ws, cid) for ws, cid in pending),
        return_exceptions=True,
    )
    return len(pending)


# ── WebSocket: log streaming ───────────────────────────────


async def _ws_close_safely(websocket: WebSocket, code: int, reason: str = "") -> None:
    """Close a WebSocket with a specific code, tolerating already-closed state.

    Starlette raises `RuntimeError: Unexpected ASGI message 'websocket.close'`
    if we try to close a socket the peer has already closed; that happens
    routinely on bad-token handshakes where the client disconnects as we
    reject. Suppressing it keeps stderr clean without swallowing real bugs.

    `reason` is forwarded to the peer as `evt.reason`; we use this for the
    auth-lockout branch to carry the remaining seconds so the client can
    paint a countdown banner without a second round-trip. Other branches
    pass an empty reason (default).
    """
    try:
        await websocket.close(code=code, reason=reason)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _ws_close_quiet(websocket: WebSocket) -> None:
    """Close a WebSocket (unspecified code), swallowing post-disconnect errors."""
    await _ws_close_safely(websocket, code=1000)


def _ws_query_carries_token(websocket: WebSocket) -> bool:
    """True if the WS upgrade URL carries a bearer token in the query string.

    SKIFF uses an AUTH message over the open socket; a `?token=…` query
    parameter would leak into any upstream HTTP access log (uvicorn,
    nginx, Caddy, oauth2-proxy). Reject early with 4008 policy-violation
    so operators see the mistake instead of silently leaking the bearer.

    Defensively handles mocked/partial ASGI scopes in tests: if `scope` is
    absent, or the raw query bytes aren't str/bytes, returns False so the
    guard never fabricates a false positive from a fixture.
    """
    scope = getattr(websocket, "scope", None)
    raw = scope.get("query_string") if isinstance(scope, dict) else None
    if isinstance(raw, (bytes, bytearray)):
        qs = raw.decode("latin-1", errors="replace").lower()
    elif isinstance(raw, str):
        qs = raw.lower()
    else:
        return False
    if not qs:
        return False
    return any(key in qs for key in ("token=", "api_token=", "bearer="))


async def _ws_handshake(websocket: WebSocket, container_id: str) -> bool:
    """Validate origin + query shape + container id + auth token. Returns True on success.

    Closes the socket with the correct code (4003 / 4008 / 4000) on
    failure so the caller only needs to `return` on False. Each failure
    path emits one `audit.ws_handshake_failed` entry so SIEM rules can
    correlate denied WS upgrades alongside the existing `auth.denied`
    HTTP signal.
    """
    client_ip = websocket.client.host if websocket.client else "unknown"
    if _ws_query_carries_token(websocket):
        log.warning("audit.ws_handshake_failed", reason="token_in_query", remote=client_ip)
        await _ws_close_safely(websocket, code=4008)
        return False
    if not auth._validate_ws_origin(websocket):
        log.warning("audit.ws_handshake_failed", reason="origin_denied", remote=client_ip)
        await _ws_close_safely(websocket, code=4003)
        return False
    if not validators.CONTAINER_ID_RE.fullmatch(container_id):
        log.warning(
            "audit.ws_handshake_failed",
            reason="bad_container_id",
            remote=client_ip,
            container=container_id[:32],
        )
        await _ws_close_safely(websocket, code=4000)
        return False
    await websocket.accept()
    if not await auth._validate_ws_token_from_message(websocket):
        log.warning("audit.ws_handshake_failed", reason="auth_failed", remote=client_ip)
        # If the failure tripped (or sustained) a per-IP brute-force lockout,
        # carry the remaining seconds in the close reason so the client can
        # paint a "WebSocket locked out — try again in Ns" banner without a
        # second round-trip. Non-lockout bad-token failures keep an empty
        # reason; the client distinguishes by the prefix.
        lockout_secs = auth.ws_lockout_remaining(client_ip)
        close_reason = f"ws_auth_lockout:{lockout_secs}" if lockout_secs > 0 else ""
        await _ws_close_safely(websocket, code=4003, reason=close_reason)
        return False
    return True


async def _stream_logs_from_generator(websocket: WebSocket, gen, loop) -> None:
    """Pump log lines from the Docker SDK generator to the WebSocket.

    Emits the idle-timeout hint when wait_for exceeds WS_LOG_IDLE_TIMEOUT.
    Exits cleanly when the generator returns None (EOF).
    """
    while True:
        try:
            line = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: next(gen, None)),
                timeout=config.WS_LOG_IDLE_TIMEOUT,
            )
        except TimeoutError:
            await websocket.send_text(
                f"\n[Idle timeout — no new logs for {config.WS_LOG_IDLE_TIMEOUT}s]\n",
            )
            return
        if line is None:
            return
        await websocket.send_text(line.decode(errors="replace"))


async def _stream_logs_session(
    websocket: WebSocket,
    container_id: str,
    loop,
    ip: str,
) -> None:
    """Body of stream_logs after the handshake — get client, open generator, pump."""
    client = await loop.run_in_executor(None, get_client)
    container = await loop.run_in_executor(None, client.containers.get, container_id)
    gen = container.logs(stream=True, follow=True, tail=config.WS_LOG_TAIL, timestamps=True)

    read_task = asyncio.create_task(_stream_logs_from_generator(websocket, gen, loop))
    keepalive_task = asyncio.create_task(auth.ws_keepalive(websocket))
    try:
        # Hold the connection open until the client disconnects.
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        read_task.cancel()
        keepalive_task.cancel()
        # generator already closed / socket torn down — teardown is best-effort
        with contextlib.suppress(docker.errors.DockerException, OSError):
            gen.close()


@router.websocket("/ws/logs/{container_id}")
async def stream_logs(websocket: WebSocket, container_id: str):
    """Stream container logs over WebSocket."""
    if not await _ws_handshake(websocket, container_id):
        return
    ip = websocket.client.host if websocket.client else "unknown"
    if not _ws_acquire(ip):
        # Per-IP WS cap reached. Close with 1013 (Try Again Later) —
        # a close frame is the right ASGI-level reply here; raising
        # HTTPException after websocket.accept() would leak up to
        # uvicorn as an unhandled error.
        await _ws_close_safely(websocket, code=1013)
        return
    log.info("audit.ws_logs", container=container_id, remote=ip)
    try:
        await _stream_logs_session(websocket, container_id, asyncio.get_running_loop(), ip)
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        log.warning("ws.logs_error", container=container_id, error=str(exc))
    finally:
        _ws_release(ip)
        await _ws_close_quiet(websocket)


# ── WebSocket: interactive exec shell ─────────────────────

_EXEC_MAX_INPUT_BYTES = 65536


def _detect_shell(container) -> str:
    """Return `/bin/bash` if the container has it installed, else `/bin/sh`.

    Some minimal images (alpine, distroless) don't ship bash; exec_run
    will return non-zero or raise. Default to /bin/sh which is always
    safe on a POSIX container.
    """
    try:
        exit_code, _ = container.exec_run("which /bin/bash", demux=True)
    except docker.errors.DockerException:
        return "/bin/sh"
    return "/bin/bash" if exit_code == 0 else "/bin/sh"


def _start_exec_session(client, container, shell: str) -> tuple[Any, str]:
    """Create+start a Docker exec session. Returns (raw socket, exec_id).

    `exec_id` is retained so the handler can later call
    `client.api.exec_resize(exec_id, height, width)` on incoming
    resize frames from the client — without it the PTY stays at the
    default 80x24 and maximised browser windows render broken TUI
    apps.
    """
    exec_id = client.api.exec_create(container.id, shell, stdin=True, tty=True, stdout=True, stderr=True)
    sock = client.api.exec_start(exec_id, socket=True, tty=True)
    sock._sock.setblocking(True)
    sock._sock.settimeout(config.WS_EXEC_RECV_TIMEOUT)
    # `exec_create` returns either {"Id": "..."} or the bare id string
    # depending on SDK version. Normalise both shapes.
    eid = exec_id.get("Id") if isinstance(exec_id, dict) else str(exec_id)
    return sock, eid


async def _exec_pump_output(websocket: WebSocket, sock, loop) -> None:
    """Pump bytes from the exec socket to the WebSocket.

    Exits on EOF (b"") or on idle longer than WS_EXEC_IDLE_TIMEOUT.
    Socket/runtime errors break the loop cleanly.
    """
    idle_since = time.monotonic()
    while True:
        try:
            data = await loop.run_in_executor(None, sock._sock.recv, 4096)
        except TimeoutError:
            if time.monotonic() - idle_since > config.WS_EXEC_IDLE_TIMEOUT:
                await websocket.send_text("\r\n[Session idle timeout — 10 minutes]\r\n")
                return
            continue
        except (OSError, RuntimeError):
            return  # socket closed or starlette WS send-after-close
        if not data:
            return
        idle_since = time.monotonic()
        await websocket.send_text(data.decode(errors="replace"))


def _maybe_resize(data: str, client, exec_id: str) -> bool:
    """If `data` is a `{"type":"resize"}` JSON frame, apply it and return True.

    Resize frames are a structured protocol — `{"type":"resize","cols":int,"rows":int}` —
    NOT shell input. Without this check the literal JSON reached the
    PTY as input (the shell rendered "{type:: not found"). Every other
    message is forwarded verbatim to stdin.
    """
    # Cheap early-out so bytes-per-keystroke input isn't JSON-parsed.
    if not data.startswith(('{"type":"resize"', '{"type": "resize"')):
        return False
    try:
        frame = json.loads(data)
    except ValueError:
        return False
    if not (isinstance(frame, dict) and frame.get("type") == "resize"):
        return False
    try:
        cols = int(frame.get("cols", 0))
        rows = int(frame.get("rows", 0))
    except (TypeError, ValueError):
        return False
    # Clamp into a sane range. Docker's exec_resize tolerates 1-9999
    # but a pathological client could send {rows: -1} or 1e9; cap
    # defensively so the TTY ioctl never sees garbage.
    if not (4 <= cols <= 1024 and 4 <= rows <= 1024):
        return False
    with contextlib.suppress(docker.errors.DockerException, OSError, AttributeError):
        client.api.exec_resize(exec_id, height=rows, width=cols)
    return True


async def _exec_pump_input(
    websocket: WebSocket,
    sock,
    loop,
    container_id: str,
    ip: str,
    client=None,
    exec_id: str = "",
) -> None:
    """Pump text from the WebSocket to the exec socket's stdin.

    Enforces a 64 KiB single-message cap — anything larger closes with
    4008. Each input message is audit-logged with byte-count only; NO
    content is captured so that pasted credentials (`export TOKEN=…`,
    passwords echoed by sudo prompts, etc.) don't land in the audit log.

    Structured `{"type":"resize","cols":N,"rows":M}` frames are handled
    out-of-band via `exec_resize`; they do NOT reach the PTY as input.
    """
    while True:
        data = await websocket.receive_text()
        # Resize frame — consumed here, never forwarded to stdin.
        if client is not None and exec_id and _maybe_resize(data, client, exec_id):
            continue
        encoded = data.encode()
        if len(encoded) > _EXEC_MAX_INPUT_BYTES:
            # Over-cap is a policy violation (oversized paste, malformed
            # client); emit an audit entry so SIEM rules can correlate
            # before the 4008 close races the connection away. Byte count
            # only — never the content.
            log.warning(
                "audit.ws_exec_input_oversize",
                container=container_id,
                remote=ip,
                bytes=len(encoded),
                limit=_EXEC_MAX_INPUT_BYTES,
            )
            await _ws_close_safely(websocket, code=4008)
            return
        log.info("audit.ws_exec_input", container=container_id, remote=ip, bytes=len(encoded))
        await loop.run_in_executor(None, sock._sock.sendall, encoded)


async def _exec_session(websocket: WebSocket, container_id: str, loop, ip: str) -> None:
    """Body of exec_shell after the handshake — build session, run both pumps."""
    client = await loop.run_in_executor(None, get_client)
    container = await loop.run_in_executor(None, client.containers.get, container_id)
    # `_detect_shell` runs `which /bin/bash` inside the container via
    # `exec_run`, which on a frozen container under heavy load can hang
    # indefinitely. Cap with a 5 s timeout and fall back to /bin/sh on
    # timeout — every POSIX container has /bin/sh so the session still
    # opens; the reviewer just doesn't get bash tab-complete niceties.
    try:
        shell = await asyncio.wait_for(
            loop.run_in_executor(None, _detect_shell, container),
            timeout=5.0,
        )
    except TimeoutError:
        log.warning("ws.detect_shell_timeout", container=container_id)
        shell = "/bin/sh"
    sock, exec_id = await loop.run_in_executor(None, _start_exec_session, client, container, shell)
    read_task = asyncio.create_task(_exec_pump_output(websocket, sock, loop))
    keepalive_task = asyncio.create_task(auth.ws_keepalive(websocket))
    try:
        await _exec_pump_input(
            websocket,
            sock,
            loop,
            container_id,
            ip,
            client=client,
            exec_id=exec_id,
        )
    except (WebSocketDisconnect, OSError, RuntimeError):
        # Disconnect: client hung up. OSError: socket gone.
        # RuntimeError: starlette on send-after-close.
        pass
    finally:
        read_task.cancel()
        keepalive_task.cancel()
        sock.close()
        log.info("audit.ws_exec_disconnect", container=container_id, remote=ip)


@router.websocket("/ws/exec/{container_id}")
async def exec_shell(websocket: WebSocket, container_id: str):
    """Open an interactive shell in a container over WebSocket.

    Reviewer-mode gating is enforced in two phases to close the TOCTOU
    against a concurrent `POST /api/profile/enter-reviewer`:

      Phase 1 (cheap pre-check):  reject the handshake if PROFILE is
        already `reviewer` before we acquire any slots.

      Phase 2 (atomic under `_ws_lock`):  `_try_register_exec_ws`
        re-reads PROFILE and inserts into `_active_exec_ws` inside
        the same lock that `close_active_exec_sessions` takes when
        snapshotting. Any handler in the Phase-1→Phase-2 gap at the
        moment of a profile flip fails the re-check and is closed
        before it can execute a shell.
    """
    if not await _ws_handshake(websocket, container_id):
        return
    ip = websocket.client.host if websocket.client else "unknown"
    # Phase 1 — cheap pre-check.
    import skiff.config as _config_module

    if _config_module.PROFILE == "reviewer":
        log.warning("audit.ws_handshake_failed", reason="reviewer_read_only", remote=ip, container=container_id)
        await _ws_close_safely(websocket, code=4003)
        return
    if not _ws_acquire(ip):
        await _ws_close_safely(websocket, code=1013)
        return
    # Phase 2 — atomic PROFILE re-check + registration. Must come
    # AFTER _ws_acquire so the per-IP slot is tracked either way.
    if not _try_register_exec_ws(websocket, container_id):
        log.warning("audit.ws_handshake_failed", reason="reviewer_read_only", remote=ip, container=container_id)
        _ws_release(ip)
        await _ws_close_safely(websocket, code=4003)
        return
    log.info("audit.ws_exec", container=container_id, remote=ip)
    try:
        await _exec_session(websocket, container_id, asyncio.get_running_loop(), ip)
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        log.warning("ws.exec_error", container=container_id, error=str(exc))
    finally:
        _unregister_exec_ws(websocket, container_id)
        _ws_release(ip)
        await _ws_close_quiet(websocket)
