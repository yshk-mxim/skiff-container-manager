# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""
SKIFF Container Manager — FastAPI application entrypoint.

This module wires together the FastAPI app, middleware, rate limiter, and routers.
Implementation is split across focused submodules:
  skiff.config          — runtime config, constants, rate-limiter
  skiff.auth            — authentication, CSRF, session tracking, WebSocket auth
  skiff.logging_setup   — structured logging, audit log, ASGI middlewares
  skiff.docker_client   — Docker client singleton, SSH tunnel management
  skiff.validators      — input validation, Docker helpers, compose sandboxing
  skiff.routers.*       — route handlers grouped by resource type
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

# ── logging_setup MUST be imported first to configure structlog
# before any other skiff module creates a logger. ──────────────
import skiff.logging_setup as _logging_setup  # noqa: F401 — side-effect import

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from skiff.config import (
    BIND_HOST,
    _APP_VERSION,
    _STATIC_DIR,
    _cfg,
    limiter,
)
from skiff.logging_setup import (
    AuditLogMiddleware,
    SecurityHeadersMiddleware,
    _loop_lag_monitor,
)
from skiff.docker_client import (
    _invalidate_client,
    _stop_tunnel,
)
from skiff.routers import containers, images, volumes, networks, compose, system

log = structlog.get_logger(__name__)


# ── Lifespan ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _cfg.api_token:
        log.warning("security.no_api_token", msg="Running without auth — set API_TOKEN for production")
    if "API_TOKEN" in os.environ and not os.environ["API_TOKEN"].strip():
        log.warning(
            "security.empty_api_token_env",
            msg="API_TOKEN env var is set but empty — setup endpoint is OPEN. "
                "Set a non-empty token or unset the variable.",
        )
    if not _cfg.allowed_registries:
        log.warning(
            "security.no_registry_allowlist",
            msg="ALLOWED_REGISTRIES is empty — all registries permitted. Set for production.",
        )
    _dh = _cfg.docker_host or ""
    if _dh.startswith("http://"):
        from urllib.parse import urlparse as _urlparse
        _parsed_dh = _urlparse(_dh)
        _dh_host = _parsed_dh.hostname or ""
        if _dh_host not in ("localhost", "127.0.0.1", "::1"):
            log.warning(
                "security.docker_host_unencrypted",
                msg="DOCKER_HOST is an unencrypted HTTP URL pointing to a non-localhost address. "
                    "Use a TLS-secured (https://) or SSH-tunnelled (unix://) connection instead.",
                docker_host=_dh,
            )
    log.info("app.started", docker_host=_cfg.docker_host, registries=_cfg.allowed_registries, bind=BIND_HOST)
    # Log installed dependency versions for post-incident forensics
    try:
        import importlib.metadata as _imeta
        _direct_deps = ["fastapi", "uvicorn", "docker", "structlog", "slowapi", "pyyaml", "python-multipart"]
        _versions = {pkg: _imeta.version(pkg) for pkg in _direct_deps if _imeta.version(pkg)}
        log.info("app.dependency_versions", **_versions)
    except Exception:
        pass  # best-effort; version logging failure must not block startup
    monitor = asyncio.create_task(_loop_lag_monitor(), name="loop-lag-monitor")
    yield
    monitor.cancel()
    log.info("app.shutdown")
    _stop_tunnel()
    _invalidate_client()


# ── FastAPI app ────────────────────────────────────────────

app = FastAPI(
    title="SKIFF Container Manager",
    version=_APP_VERSION,
    description=(
        "Lightweight web UI for Docker — works locally on your machine as a Docker Desktop "
        "alternative, or remotely over SSH. All mutating operations require authentication "
        "and CSRF verification."
    ),
    openapi_tags=[
        {"name": "auth",       "description": "Authentication and session state"},
        {"name": "setup",      "description": "Initial server configuration"},
        {"name": "containers", "description": "Container lifecycle and inspection"},
        {"name": "images",     "description": "Image listing, pulling, tagging, pushing"},
        {"name": "volumes",    "description": "Named volume management"},
        {"name": "networks",   "description": "Docker network management"},
        {"name": "compose",    "description": "Docker Compose stack operations"},
        {"name": "system",     "description": "Engine info, disk usage, pruning"},
        {"name": "audit",      "description": "Activity audit log"},
        {"name": "health",     "description": "Liveness and readiness probes"},
    ],
    lifespan=lifespan,
)

# ── Rate limiting ──────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Middleware (added last-to-first: outermost middleware first in list) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cfg.allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "X-Requested-With", "Content-Type"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuditLogMiddleware)

# ── Routers ────────────────────────────────────────────────
app.include_router(system.router)
app.include_router(containers.router)
app.include_router(images.router)
app.include_router(volumes.router)
app.include_router(networks.router)
app.include_router(compose.router)

# ── Static files ───────────────────────────────────────────
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ── Backwards-compatible re-exports ───────────────────────
# These allow tests and external code to continue importing names from skiff.app
# even though the implementations live in submodules.
from skiff.config import (  # noqa: E402, F401
    _Config,
    AUDIT_BACKUP_COUNT,
    AUDIT_LOG_PATH,
    AUDIT_MAX_BYTES,
    BIND_HOST,
    COMPOSE_DIR,
    CONTAINER_RESTART_TIMEOUT,
    CONTAINER_STATS_TIMEOUT,
    CONTAINER_STOP_TIMEOUT,
    DOCKER_BACKOFF,
    DOCKER_BIN,
    DOCKER_CLIENT_TIMEOUT,
    DOCKER_PING_TTL,
    DOCKER_POOL_SIZE,
    HSTS_HEADER,
    HSTS_MAX_AGE,
    IMAGE_PULL_TIMEOUT,
    MAX_AUDIT_LINES,
    MAX_COMPOSE_SIZE,
    MAX_CONTAINER_CPU,
    MAX_CONTAINER_MEM,
    MAX_CONTAINERS,
    MAX_LOG_TAIL,
    MAX_PORT_MAPPINGS,
    MAX_RESTART_RETRIES,
    MAX_VOLUME_NAME_LENGTH,
    MIN_TOKEN_LENGTH,
    PRIVILEGED_PORT_THRESHOLD,
    REGISTRY_DESC_MAX,
    REGISTRY_MAX_TAGS,
    REGISTRY_SEARCH_PAGE_SIZE,
    REGISTRY_TIMEOUT,
    RL_DEFAULT,
    RL_FAST,
    RL_SLOW,
    SETUP_WINDOW_SECS,
    SESSION_ABS_TIMEOUT,
    TCP_KEEPALIVE_COUNT,
    TCP_KEEPALIVE_IDLE,
    TCP_KEEPALIVE_INTERVAL,
    TUNNEL_CONNECT_TIMEOUT,
    TUNNEL_DEFAULT_SOCKET,
    TUNNEL_SERVER_ALIVE_COUNT,
    TUNNEL_SERVER_ALIVE_INTERVAL,
    TUNNEL_SOCKET_POLL,
    TUNNEL_SOCKET_WAIT,
    WS_AUTH_LOCKOUT_SECS,
    WS_AUTH_MAX_ATTEMPTS,
    WS_EXEC_IDLE_TIMEOUT,
    WS_EXEC_RECV_TIMEOUT,
    WS_KEEPALIVE_INTERVAL,
    WS_KEEPALIVE_REVALIDATE_EVERY,
    WS_LOG_IDLE_TIMEOUT,
    WS_LOG_TAIL,
    WS_MAX_PER_IP,
    WS_TOKEN_TIMEOUT,
    _APP_VERSION,
    _CSP,
    _GCP_LOG_NAME,
    _GCP_PROJECT,
    _PERMISSIONS_POLICY,
    _RATE_SCALE,
    _SESSION_CACHE_MAX,
    _limit,
)
from skiff.auth import (  # noqa: E402, F401
    AUTH,
    _check_session_age,
    _constant_time_compare,
    _invalidate_session_cache,
    _session_first_seen,
    _session_lock,
    _validate_ws_origin,
    _validate_ws_token_from_message,
    _ws_auth_failures,
    _ws_auth_lock,
    verify_auth,
    verify_auth_strict,
    verify_csrf,
    ws_keepalive,
)
from skiff.logging_setup import (  # noqa: E402, F401
    AuditLogMiddleware,
    SecurityHeadersMiddleware,
    _audit_file_sink,
    _audit_handler,
    _classify_event,
    _gcp_logger,
    _loop_lag_monitor,
    _make_audit_handler,
)
from skiff.docker_client import (  # noqa: E402, F401
    DOCKER_TRANSIENT,
    _SSH_TARGET_RE,
    _build_client,
    _client,
    _client_failed_at,
    _client_last_ping,
    _client_lock,
    _invalidate_client,
    _start_tunnel,
    _stop_tunnel,
    _stop_tunnel_locked,
    _tunnel_ctl_sock,
    _tunnel_lock,
    _tunnel_socket_path,
    _tunnel_ssh_target,
    docker_client_dep,
    get_client,
    get_tunnel_socket_path,
)
from skiff.validators import (  # noqa: E402, F401
    BLOCKED_COMPOSE_SERVICE_KEYS,
    BLOCKED_COMPOSE_TOP_KEYS,
    BLOCKED_NETWORK_MODES,
    BLOCKED_PRESENCE_KEYS,
    BLOCKED_TRUTHY_KEYS,
    CONTAINER_ID_RE,
    CONTAINER_NAME_RE,
    IMAGE_ID_RE,
    IMAGE_TAG_RE,
    NETWORK_NAME_RE,
    PROJECT_NAME_RE,
    _ENV_SENSITIVE_RE,
    _BLOCKED_MOUNT_TARGETS,
    _get_container,
    _redact_dict,
    _redact_env,
    _sanitize_stderr,
    _validate_mount_target,
    safe_docker_call,
    validate_compose_file,
    validate_container_id,
    validate_container_name,
    validate_image_id,
    validate_image_registry,
    validate_project_name,
)
from skiff.routers.containers import (  # noqa: E402, F401
    _ws_acquire,
    _ws_connections,
    _ws_lock,
    _ws_release,
)


def _main():
    """Entrypoint for `pip install` / `skiff` CLI command."""
    import uvicorn
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("skiff.app:app", host=host, port=port, workers=1, log_level="warning")


if __name__ == "__main__":
    _main()
