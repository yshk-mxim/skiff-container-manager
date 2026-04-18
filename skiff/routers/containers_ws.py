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
import threading
import time

import docker.errors
import structlog
from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from skiff import auth, config, validators
from skiff.contract.errors import http_error
from skiff.docker_client import get_client

log = structlog.get_logger(__name__)
router = APIRouter()

# ── Per-IP WebSocket connection rate limiting ──────────────
_ws_connections: dict[str, int] = collections.defaultdict(int)
_ws_lock = threading.Lock()


def _ws_acquire(ip: str) -> None:
    with _ws_lock:
        if _ws_connections[ip] >= config.WS_MAX_PER_IP:
            raise http_error("ws.connections_exhausted")
        _ws_connections[ip] += 1


def _ws_release(ip: str) -> None:
    with _ws_lock:
        _ws_connections[ip] = max(0, _ws_connections[ip] - 1)


# ── WebSocket: log streaming ───────────────────────────────

async def _ws_close_safely(websocket: WebSocket, code: int) -> None:
    """Close a WebSocket with a specific code, tolerating already-closed state.

    Starlette raises `RuntimeError: Unexpected ASGI message 'websocket.close'`
    if we try to close a socket the peer has already closed; that happens
    routinely on bad-token handshakes where the client disconnects as we
    reject. Suppressing it keeps stderr clean without swallowing real bugs.
    """
    try:
        await websocket.close(code=code)
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
            "audit.ws_handshake_failed", reason="bad_container_id",
            remote=client_ip, container=container_id[:32],
        )
        await _ws_close_safely(websocket, code=4000)
        return False
    await websocket.accept()
    if not await auth._validate_ws_token_from_message(websocket):
        log.warning("audit.ws_handshake_failed", reason="auth_failed", remote=client_ip)
        await _ws_close_safely(websocket, code=4003)
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
            await websocket.send_text("\n[Idle timeout — no new logs for 5 minutes]\n")
            return
        if line is None:
            return
        await websocket.send_text(line.decode(errors="replace"))


async def _stream_logs_session(
    websocket: WebSocket, container_id: str, loop, ip: str,
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
    _ws_acquire(ip)
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


def _start_exec_session(client, container, shell: str):
    """Create+start a Docker exec session. Returns the raw socket object."""
    exec_id = client.api.exec_create(container.id, shell, stdin=True, tty=True, stdout=True, stderr=True)
    sock = client.api.exec_start(exec_id, socket=True, tty=True)
    sock._sock.setblocking(True)
    sock._sock.settimeout(config.WS_EXEC_RECV_TIMEOUT)
    return sock


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


async def _exec_pump_input(
    websocket: WebSocket, sock, loop, container_id: str, ip: str,
) -> None:
    """Pump text from the WebSocket to the exec socket's stdin.

    Enforces a 64 KiB single-message cap — anything larger closes with
    4008. Each input message is audit-logged with byte-count only; NO
    content is captured so that pasted credentials (`export TOKEN=…`,
    passwords echoed by sudo prompts, etc.) don't land in the audit log.
    """
    while True:
        data = await websocket.receive_text()
        encoded = data.encode()
        if len(encoded) > _EXEC_MAX_INPUT_BYTES:
            # Over-cap is a policy violation (oversized paste, malformed
            # client); emit an audit entry so SIEM rules can correlate
            # before the 4008 close races the connection away. Byte count
            # only — never the content.
            log.warning(
                "audit.ws_exec_input_oversize",
                container=container_id, remote=ip, bytes=len(encoded),
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
    shell = await loop.run_in_executor(None, _detect_shell, container)
    sock = await loop.run_in_executor(None, _start_exec_session, client, container, shell)
    read_task = asyncio.create_task(_exec_pump_output(websocket, sock, loop))
    keepalive_task = asyncio.create_task(auth.ws_keepalive(websocket))
    try:
        await _exec_pump_input(websocket, sock, loop, container_id, ip)
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
    """Open an interactive shell in a container over WebSocket."""
    if not await _ws_handshake(websocket, container_id):
        return
    ip = websocket.client.host if websocket.client else "unknown"
    _ws_acquire(ip)
    log.info("audit.ws_exec", container=container_id, remote=ip)
    try:
        await _exec_session(websocket, container_id, asyncio.get_running_loop(), ip)
    except (docker.errors.DockerException, OSError, RuntimeError) as exc:
        log.warning("ws.exec_error", container=container_id, error=str(exc))
    finally:
        _ws_release(ip)
        await _ws_close_quiet(websocket)
