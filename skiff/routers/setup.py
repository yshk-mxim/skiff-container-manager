# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Setup wizard + post-setup account / tunnel lifecycle routes.

Wizard (no auth — gated on `not _cfg.api_token`):
    GET    /api/setup-state         current wizard state
    GET    /api/setup/probe-docker  probe common Docker sockets
    POST   /api/setup               commit docker_host + api_token
    POST   /api/setup/tunnel        start SSH ControlMaster tunnel
    DELETE /api/setup/tunnel        stop the managed tunnel

Authenticated lifecycle:
    GET    /api/tunnel/status       managed-tunnel state
    POST   /api/tunnel/reconnect    re-open tunnel with stored target
    POST   /api/auth/rotate-token   swap API_TOKEN in memory
    POST   /api/auth/reset-config   clear state + reopen setup window

The per-IP `_setup_failures` brute-force counter is cleared by the
autouse rate-limit fixture in tests/conftest.
"""
from __future__ import annotations

import contextlib
import ipaddress
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse

import docker.errors
import structlog
from fastapi import APIRouter, Body, Request

from skiff import auth, config, docker_client
from skiff.auth import AUTH
from skiff.contract.errors import http_error
from skiff.contract.responses import OkResponse
from skiff.rate import RATE
from skiff.secure import secure_route

log = structlog.get_logger(__name__)
router = APIRouter()

# ── Brute-force protection (per-IP failure counter) ──────────────────────────
# Maps client_ip → (failure_count, last_failure_monotonic). `_enforce_lockout`
# reads this before every setup attempt; `_fail` increments on bad input.
_setup_failures: dict[str, tuple[int, float]] = {}

# Loopback sentinel set used by the setup-state disclosure filter.
# `"testclient"` is intentionally absent — unit tests that need to hit
# the loopback branch override `request.client.host` via a starlette
# dependency override (see `tests/conftest.py::loopback_client`).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_setup_lock = threading.Lock()


def _fail(client_ip: str, reason: str) -> None:
    """Increment per-IP failure counter and emit an audit log entry."""
    now = time.monotonic()
    with _setup_lock:
        count, _ = _setup_failures.get(client_ip, (0, now))
        _setup_failures[client_ip] = (count + 1, now)
    log.info("audit.setup_failed", remote=client_ip, reason=reason)


# ── Docker socket probing (wizard zero-decision first boot) ──────────────────
# Probe-path list sourced exclusively from skiff/_config/docker_probe.toml — adding
# a runtime is one TOML row (see that file for the ordering rationale).
_DEFAULT_PROBE_PATHS = tuple(config._TOML_DOCKER_PROBE["paths"])


def _close_client_quiet(client) -> None:
    """Best-effort client.close(); swallows the known cleanup-time errors."""
    with contextlib.suppress(docker.errors.DockerException, OSError):
        client.close()


def _probe_docker_socket(path_hint: str) -> tuple[bool, str]:
    """Ping Docker at `path_hint`. Returns (reachable, expanded).

    Timeout is `config.PROBE_DOCKER_TIMEOUT` (see
    `skiff/_config/defaults.toml`). Expands `~` via Path.expanduser
    (uses `$HOME`). A missing socket short-circuits to False without
    attempting to open a client.
    """
    expanded = str(Path(path_hint).expanduser())
    if not Path(expanded).exists():
        return (False, expanded)
    try:
        import docker as _docker
        client = _docker.DockerClient(base_url=f"unix://{expanded}", timeout=config.PROBE_DOCKER_TIMEOUT)
        client.ping()
    except (docker.errors.DockerException, OSError):
        return (False, expanded)
    _close_client_quiet(client)
    return (True, expanded)


# ─────────────────────────────────────────────────────────────────────────────
# Setup preconditions and input validators (called by do_setup in that order)
# ─────────────────────────────────────────────────────────────────────────────


def _enforce_lockout(client_ip: str) -> None:
    """Raise `auth.setup_locked` if this IP exceeded SETUP_MAX_ATTEMPTS.

    Clears stale entries older than SETUP_LOCKOUT_SECS as a side effect —
    keeps the dict from growing unboundedly without a background sweeper.
    """
    now = time.monotonic()
    with _setup_lock:
        entry = _setup_failures.get(client_ip)
        if entry is None:
            return
        count, last_t = entry
        if count >= config.SETUP_MAX_ATTEMPTS and (now - last_t) < config.SETUP_LOCKOUT_SECS:
            log.warning(
                "audit.setup_lockout", remote=client_ip,
                remaining=config.SETUP_LOCKOUT_SECS - (now - last_t),
            )
            raise http_error(
                "auth.setup_locked",
                message="Too many failed setup attempts — try again later",
            )
        if (now - last_t) >= config.SETUP_LOCKOUT_SECS:
            del _setup_failures[client_ip]


def _enforce_preconditions() -> None:
    """Raise if the setup window has closed OR the server is already configured."""
    if time.monotonic() - config.APP_START_MONOTONIC > config.SETUP_WINDOW_SECS:
        raise http_error("setup.window_expired")
    if config._cfg.from_env:
        raise http_error("setup.env_managed")
    if config._cfg.api_token:
        raise http_error("setup.already_done")


_MAX_TOKEN_LENGTH = 512

# Bearer tokens travel in the HTTP `Authorization: Bearer <token>`
# header, which is ASCII-only (RFC 7235 + RFC 7230 §3.2.6). If an
# operator sets a token with Unicode / bidi / control chars via the
# setup wizard, the server stores it but NO HTTP client can ever
# re-present it — the operator is silently locked out with no UI
# recovery path (reset-config requires AUTH). Restrict to the
# unreserved URI charset plus a few common token separators.
_TOKEN_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._~+/=\-]+$")


def _validate_api_token(api_token: str, client_ip: str) -> str:
    """Return the stripped + charset-validated token, or raise.

    Length check (strip-then-measure) + charset check (`RFC 7235`
    token chars only). Either failure bumps the brute-force counter
    and raises `setup.token_too_short` or the new
    `setup.token_bad_charset` so an operator sees a clear diagnostic
    instead of a silent lockout.
    """
    stripped = (api_token or "").strip()
    if len(stripped) < config.MIN_TOKEN_LENGTH:
        _fail(client_ip, "token_too_short")
        raise http_error("setup.token_too_short", minimum=config.MIN_TOKEN_LENGTH)
    if len(stripped) > _MAX_TOKEN_LENGTH:
        _fail(client_ip, "token_too_long")
        raise http_error(
            "setup.token_too_short",
            minimum=config.MIN_TOKEN_LENGTH,
            message=f"api_token length must be at most {_MAX_TOKEN_LENGTH} characters",
        )
    if not _TOKEN_ALLOWED_RE.match(stripped):
        _fail(client_ip, "token_bad_charset")
        raise http_error("setup.token_bad_charset")
    return stripped


_DOCKER_HOST_SCHEMES = frozenset({"unix", "tcp", "npipe"})


def _reject_bad_tcp_host(parsed, client_ip: str) -> None:
    """For tcp:// — host must be a literal IP and port in 1..65535."""
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        _fail(client_ip, "bad_docker_host_address")
        raise http_error("setup.tcp_host_bad") from None
    if not (1 <= (parsed.port or 0) <= 65535):
        _fail(client_ip, "bad_docker_host_port")
        raise http_error("setup.tcp_port_bad")


def _validate_docker_host(docker_host: str, client_ip: str) -> str:
    """Return the stripped docker_host URL, or bump the counter and raise.

    Three passes:
      1. Non-empty string.
      2. Scheme in {unix, tcp, npipe}.
      3. For tcp:// — host is a literal IP (DNS rebinding resistance) and
         port is in 1..65535.
    """
    if not docker_host:
        _fail(client_ip, "missing_docker_host")
        raise http_error("setup.docker_host_required")
    value = docker_host.strip()
    parsed = urlparse(value)
    if parsed.scheme not in _DOCKER_HOST_SCHEMES:
        _fail(client_ip, "bad_docker_host_scheme")
        raise http_error("setup.scheme_bad")
    if parsed.scheme == "tcp":
        _reject_bad_tcp_host(parsed, client_ip)
    return value


def _clear_lockout(client_ip: str) -> None:
    """Wipe the per-IP failure counter on successful validation."""
    with _setup_lock:
        _setup_failures.pop(client_ip, None)


def _apply_setup(docker_host: str, api_token: str, allowed_registries: str) -> None:
    """Commit validated inputs to `_cfg` and invalidate caches.

    An empty `allowed_registries` value preserves the existing allowlist —
    the wizard form intentionally cannot widen to "allow everything" with
    an empty string, since that would silently remove a security control
    on a running instance after a `reset-config`.
    """
    config._cfg.api_token = api_token
    config._cfg.docker_host = docker_host
    new_registries = [r.strip() for r in allowed_registries.split(",") if r.strip()]
    if new_registries:
        config._cfg.allowed_registries = new_registries
    docker_client.invalidate_client()
    auth._invalidate_session_cache()
    log.info(
        "setup.configured",
        docker_host=config._cfg.docker_host,
        registries=config._cfg.allowed_registries,
    )


def _raise_tunnel_error(exc: docker_client.TunnelError) -> NoReturn:
    """Translate a TunnelError into http_error shape with UI-guidance fields.

    Used by start_tunnel and tunnel_reconnect — both delegate to
    docker_client._start_tunnel and both need the same classified-error
    surfacing. `exc.code` / `exc.help_text` are preserved in the response
    `extra` for the UI to key off.
    """
    extra = {"tunnel_code": exc.code, "help": exc.help_text} if exc.help_text else {"tunnel_code": exc.code}
    raise http_error("system.tunnel_failed", message=str(exc), extra=extra) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/setup-state", tags=["setup"])
def setup_state(request: Request):
    """Return wizard state: configured? from_env? tunnel reachable?

    Unauthenticated — the wizard needs this before a token exists. We
    only reveal the tunnel socket path when BOTH (a) the wizard is
    still needed AND (b) the caller is on the loopback interface. A
    pre-auth caller from any other interface gets `{configured,
    from_env}` only — the tunnel path is an operator-only detail.
    """
    if config._cfg.api_token:
        return {"configured": True, "from_env": config._cfg.from_env}
    client_ip = request.client.host if request.client else ""
    # Same-host callers (loopback IPs only) get the tunnel path in the
    # unconfigured response. Network callers don't — they'd only leak the
    # path of a socket they can't reach anyway.
    is_same_host = client_ip in _LOOPBACK_HOSTS
    if not is_same_host:
        return {"configured": False, "from_env": config._cfg.from_env}
    safe_path = docker_client._safe_tunnel_socket_path(docker_client.get_tunnel_socket_path())
    return {
        "configured": False,
        "from_env": config._cfg.from_env,
        "tunnel_active": bool(safe_path and Path(safe_path).exists()),
        "tunnel_socket": safe_path or config.TUNNEL_DEFAULT_SOCKET,
    }


@router.get("/api/setup/probe-docker", tags=["setup"])
@secure_route.public(RATE.AUTH_SENSITIVE)
def probe_docker(request: Request) -> dict:
    """Probe the curated local-socket allowlist; return which responded.

    Pre-setup only (returns 403 once configured). The probed paths are a
    fixed server-side allowlist — a caller cannot enumerate arbitrary
    filesystem entries via this endpoint.
    """
    if config._cfg.api_token:
        raise http_error("setup.probe_disabled")
    reachable: list[str] = []
    unreachable: list[str] = []
    for p in _DEFAULT_PROBE_PATHS:
        ok, expanded = _probe_docker_socket(p)
        (reachable if ok else unreachable).append(f"unix://{expanded}")
    return {"reachable": reachable, "unreachable": unreachable}


@router.post("/api/setup", tags=["setup"])
@secure_route.mutate(RATE.AUTH_SENSITIVE)
def do_setup(
    request: Request,
    docker_host: str = Body(...),
    api_token: str = Body(...),
    allowed_registries: str = Body(default=""),
):
    """Commit the server's in-memory configuration from the wizard.

    Decomposed into linear named steps so each bit is independently
    readable and unit-testable. The order matters:

      1. lockout — refuse this IP if it's over the brute-force cap
      2. preconditions — wizard window still open, not env-managed,
         not already configured
      3. validate inputs — token length + docker_host shape; any
         failure bumps the per-IP counter
      4. clear lockout on success
      5. apply — mutate _cfg + invalidate docker client + session cache
    """
    client_ip = request.client.host if request.client else "unknown"
    _enforce_lockout(client_ip)
    _enforce_preconditions()
    token = _validate_api_token(api_token, client_ip)
    host = _validate_docker_host(docker_host, client_ip)
    _clear_lockout(client_ip)
    _apply_setup(host, token, allowed_registries)
    return OkResponse()


@router.post("/api/setup/tunnel", tags=["setup"])
@secure_route.mutate(RATE.AUTH_SENSITIVE)
def start_tunnel(
    request: Request,
    ssh_target: str = Body(..., embed=True),
):
    """Start the managed SSH ControlMaster tunnel. Wizard-only.

    The socket path is the server-controlled constant
    config.TUNNEL_DEFAULT_SOCKET — user-supplied paths never reach the
    SSH subprocess, eliminating injection surface.
    """
    if config._cfg.from_env:
        raise http_error(
            "setup.env_managed",
            message="Setup endpoints disabled when configured via environment",
        )
    if config._cfg.api_token:
        raise http_error("setup.already_done")
    client_ip = request.client.host if request.client else "unknown"
    _enforce_lockout(client_ip)
    if not docker_client._SSH_TARGET_RE.match(ssh_target):
        _fail(client_ip, "bad_ssh_target")
        raise http_error("setup.ssh_target_bad")
    try:
        docker_client._start_tunnel(ssh_target)
    except docker_client.TunnelError as exc:
        _fail(client_ip, "tunnel_connect_failed")
        _raise_tunnel_error(exc)
    _clear_lockout(client_ip)
    sp = Path(config.TUNNEL_DEFAULT_SOCKET).resolve()
    return OkResponse(socket_path=str(sp), docker_host=f"unix://{sp}")


@router.delete("/api/setup/tunnel", tags=["setup"])
@secure_route.mutate(RATE.AUTH_SENSITIVE)
def stop_tunnel_endpoint(request: Request):
    """Stop the managed SSH tunnel (wizard flow only; AUTH'd path is reset-config)."""
    if config._cfg.from_env:
        raise http_error(
            "setup.env_managed",
            message="Setup endpoints disabled when configured via environment",
        )
    docker_client.stop_tunnel()
    return OkResponse()


# ── Tunnel lifecycle (authenticated) ─────────────────────────────────────────


@router.get("/api/tunnel/status", dependencies=AUTH, tags=["tunnel"])
@secure_route.read(RATE.READ)
def tunnel_status(request: Request) -> dict:
    """Return the tunnel's live state, whether wizard-managed or manual.

    `managed` — True iff the wizard started a tunnel and the server
                remembers the ssh_target. The target itself is never
                returned (zero-trust; server-only state).
    `active`  — True iff the Docker host is actually reachable. For a
                unix socket this means the socket file exists on disk;
                for any DOCKER_HOST shape it's the ground truth "can we
                see the daemon right now". Works for BOTH wizard-managed
                and manual tunnels — an operator with a plain `ssh -fNL`
                gets an honest status answer instead of the previous
                always-false reading.
    `socket`  — The resolved socket path, when DOCKER_HOST is a unix
                socket. Otherwise empty.
    """
    target = docker_client.get_tunnel_ssh_target()
    safe_path = docker_client._safe_tunnel_socket_path(
        docker_client.get_tunnel_socket_path(),
    )
    # For manual tunnels, fall back to the configured DOCKER_HOST.
    if not safe_path:
        dh = config._cfg.docker_host or ""
        if dh.startswith("unix://"):
            safe_path = docker_client._safe_tunnel_socket_path(dh[len("unix://"):])
    active = bool(safe_path and Path(safe_path).exists())
    return {
        "managed": bool(target),
        "active": active,
        "socket": safe_path or "",
    }


@router.post("/api/tunnel/reconnect", dependencies=AUTH, tags=["tunnel"])
@secure_route.mutate(RATE.AUTH_SENSITIVE)
def tunnel_reconnect(request: Request) -> OkResponse:
    """Re-open or probe the SSH tunnel — handles both wizard-managed
    and operator-managed manual tunnels.

    Three cases:

    1. **Wizard-managed tunnel with a stored ssh_target** — SKIFF
       re-opens the ControlMaster tunnel to that target. The client
       cannot override the target (zero-trust: server-only state).

    2. **Manual tunnel (plain `ssh -fNL` by the operator), socket is
       currently reachable** — nothing to do, return 409
       `tunnel.already_connected` so the UI can show "already
       connected" instead of silently no-op'ing. The Docker client is
       proactively invalidated so any stale-connection error clears.

    3. **Manual tunnel, socket is NOT reachable** — SKIFF cannot
       re-open a tunnel it did not open itself (we never learned the
       operator's ssh target; storing one uploaded at runtime would be
       a fresh attack surface). Return 503
       `tunnel.manual_reconnect_required` with the socket path, so the
       UI shows the exact `ssh -fNL <socket>:...` command the operator
       needs to re-run. This makes the Reconnect button a useful
       status check for manual users instead of a dead 404.
    """
    target = docker_client.get_tunnel_ssh_target()
    if target:
        # Case 1: wizard-managed — re-open the ControlMaster tunnel.
        try:
            docker_client._start_tunnel(target)
        except docker_client.TunnelError as exc:
            _raise_tunnel_error(exc)
        sp = Path(config.TUNNEL_DEFAULT_SOCKET).resolve()
        docker_client.invalidate_client()
        log.info("tunnel.reconnected", socket=str(sp), managed=True)
        return OkResponse(socket_path=str(sp), docker_host=f"unix://{sp}")

    # Cases 2 + 3: no wizard-managed target, but the operator may have
    # opened a manual tunnel whose socket is named in DOCKER_HOST.
    dh = config._cfg.docker_host or ""
    if not dh.startswith("unix://"):
        raise http_error("tunnel.not_configured")
    sock_path = dh[len("unix://"):]
    safe_path = docker_client._safe_tunnel_socket_path(sock_path)
    if not safe_path:
        raise http_error("tunnel.not_configured")
    sp_resolved = Path(safe_path).resolve()
    if sp_resolved.exists():
        # Existence ≠ reachability. `ssh -fNL` can leave a dangling
        # AF_UNIX socket file after `ssh` dies; that file is present but
        # a Docker ping through it will timeout or fail. Probe with the
        # same helper the wizard uses to discover local daemons so the
        # reported state matches what `GET /api/system/info` would see.
        reachable, _ = _probe_docker_socket(safe_path)
        if not reachable:
            log.info(
                "tunnel.manual_reconnect_required",
                socket=str(sp_resolved), managed=False,
                reason="socket_present_but_unreachable",
            )
            raise http_error(
                "tunnel.manual_reconnect_required",
                extra={"socket_path": str(sp_resolved), "docker_host": dh},
            )
        # Case 2 — manual tunnel alive. Invalidate stale client
        # connections so the next request re-opens via the same socket.
        docker_client.invalidate_client()
        log.info("tunnel.reconnect_noop", socket=str(sp_resolved), managed=False)
        raise http_error(
            "tunnel.already_connected",
            extra={"socket_path": str(sp_resolved)},
        )
    # Case 3 — manual tunnel is down; only the operator can reopen it.
    log.info(
        "tunnel.manual_reconnect_required",
        socket=str(sp_resolved), managed=False,
    )
    raise http_error(
        "tunnel.manual_reconnect_required",
        extra={"socket_path": str(sp_resolved), "docker_host": dh},
    )


# ── Account lifecycle (authenticated) ────────────────────────────────────────


@router.post("/api/auth/rotate-token", dependencies=AUTH, tags=["auth"])
@secure_route.mutate(RATE.AUTH_SENSITIVE)
def rotate_token(request: Request, new_token: str = Body(..., embed=True)) -> dict:  # returns model_dump'd dict
    """Replace the in-memory API_TOKEN. Old token stops working on success.

    Disabled when the token was set via the environment (the admin would
    need to update .env and restart). Caller must already hold a valid
    token (AUTH dep). Audit suffixes only — never the raw token value.
    """
    if config._cfg.from_env:
        raise http_error("auth.env_managed")
    # Strip-then-measure: the same whitespace-only bypass as do_setup
    # would otherwise let `"                "` pass the length check and
    # clear the server token on store. See _validate_api_token() for the
    # same guard on the wizard path.
    new_token = (new_token or "").strip()
    if len(new_token) < config.MIN_TOKEN_LENGTH:
        raise http_error(
            "setup.token_too_short",
            minimum=config.MIN_TOKEN_LENGTH,
            message=f"new_token must be at least {config.MIN_TOKEN_LENGTH} characters",
        )
    if not _TOKEN_ALLOWED_RE.match(new_token):
        raise http_error("setup.token_bad_charset")
    old_suffix = config._cfg.api_token[-8:] if len(config._cfg.api_token) >= 8 else ""
    new_suffix = new_token[-8:]
    if old_suffix == new_suffix and config._cfg.api_token == new_token.strip():
        raise http_error("auth.token_unchanged")
    config._cfg.api_token = new_token.strip()
    auth._invalidate_session_cache()
    log.info("auth.token_rotated", old_suffix=old_suffix, new_suffix=new_suffix)
    return OkResponse().model_dump(exclude_none=True)


@router.post("/api/auth/reset-config", dependencies=AUTH, tags=["auth"])
@secure_route.mutate(RATE.AUTH_SENSITIVE)
def reset_config(request: Request) -> OkResponse:
    """Clear in-memory state + stop tunnel + reopen the setup window.

    Disabled when the server is configured via environment (nothing
    in-memory to reset — state lives in .env). The setup-window reopen
    is safe because the caller just proved authority via AUTH.
    """
    if config._cfg.from_env:
        raise http_error("auth.reset_env_managed")
    old_suffix = config._cfg.api_token[-8:] if len(config._cfg.api_token) >= 8 else ""
    config._cfg.api_token = ""
    config._cfg.docker_host = ""
    config._cfg.allowed_registries = []
    auth._invalidate_session_cache()
    try:
        docker_client.stop_tunnel()
    except (subprocess.SubprocessError, OSError) as exc:
        # Best-effort: if the tunnel teardown fails (SSH process gone,
        # socket file missing), the auth reset should still proceed.
        log.warning("auth.reset_tunnel_cleanup_failed", error=str(exc))
    # Reopen the setup window (normally locked 5 min after startup).
    import skiff.config as _config_module
    _config_module.APP_START_MONOTONIC = time.monotonic()
    log.info("auth.config_reset", old_suffix=old_suffix)
    return OkResponse()
