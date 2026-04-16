# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Runtime configuration, application constants, and rate-limiter setup.

Imported by every other skiff module — must not import from any other skiff module
to avoid circular-import chains.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from slowapi import Limiter
from slowapi.util import get_remote_address

# ── Package paths ──────────────────────────────────────────
_PKG_DIR = Path(__file__).parent
_STATIC_DIR = _PKG_DIR / "static"
_LICENSE_FILE = _PKG_DIR.parent / "LICENSE"
# Cache at import time — avoids thread-pool contention from anyio file I/O
# under heavy API load (FileResponse uses a thread even for async handlers on macOS).
_INDEX_HTML: bytes = (_STATIC_DIR / "index.html").read_bytes()

# ── Runtime configuration ──────────────────────────────────
class _Config:
    """Mutable runtime configuration. Populated from env on startup; can be
    updated via /api/setup when running without a pre-configured environment."""

    def __init__(self) -> None:
        self.docker_host: str = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        # Default registry allowlist — docker.io,ghcr.io covers the common open-source case.
        # For GCP Artifact Registry set: ALLOWED_REGISTRIES=us-docker.pkg.dev/my-project/
        _reg_default = "docker.io,ghcr.io"
        self.allowed_registries: list[str] = [
            r.strip()
            for r in os.environ.get("ALLOWED_REGISTRIES", _reg_default).split(",")
            if r.strip()
        ]
        self.api_token: str = os.environ.get("API_TOKEN", "")
        self.allowed_origins: list[str] = [
            o.strip()
            for o in os.environ.get("ALLOWED_ORIGINS", "http://127.0.0.1:8080").split(",")
            if o.strip()
        ]
        if "*" in self.allowed_origins:
            raise ValueError(
                "ALLOWED_ORIGINS must not contain '*' — this disables CSRF protections. "
                "Set it to the exact origin(s) of your browser client, e.g. http://127.0.0.1:8080"
            )
        self.docker_vm_host: str = os.environ.get("DOCKER_VM_HOST", "")
        # True when config came from env with a non-empty token — setup endpoint disabled
        self.from_env: bool = bool(os.environ.get("API_TOKEN", "").strip())


_cfg = _Config()

# ── Filesystem / paths ─────────────────────────────────────
COMPOSE_DIR = Path(os.environ.get("COMPOSE_DIR", "/data/compose"))
DOCKER_BIN = shutil.which("docker") or "/usr/bin/docker"
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG", "/var/log/skiff-audit.jsonl"))


def _find_compose_cmd() -> list[str]:
    """Return the compose command prefix.

    Prefers the Docker Compose v2 plugin (``docker compose``).  Falls back to
    the standalone ``docker-compose`` binary (either v1 or v2) if the plugin
    is not available — common with Docker Desktop on macOS/Linux where the two
    are shipped separately.
    """
    docker = DOCKER_BIN
    try:
        r = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True, timeout=5, check=False,
        )
        if r.returncode == 0:
            return [docker, "compose"]
    except Exception:
        pass
    standalone = shutil.which("docker-compose")
    if standalone:
        return [standalone]
    return [docker, "compose"]  # best-effort: let the caller surface the error


COMPOSE_CMD: list[str] = _find_compose_cmd()
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")

# ── Application version ────────────────────────────────────
_APP_VERSION = "1.0.0"

# ── Server start time (monotonic for interval math, wall-clock for uptime) ──
APP_START_MONOTONIC = time.monotonic()  # used for setup window enforcement
APP_START_WALL = time.time()            # used for uptime display in /health

# ── Named constants (no magic values) ──────────────────────
MAX_COMPOSE_SIZE = 1024 * 256  # 256KB
MAX_LOG_TAIL = 5000
MAX_AUDIT_LINES = 2000
MAX_CONTAINERS = 50
MAX_CONTAINER_MEM = "2g"
MAX_CONTAINER_CPU = 2.0

# Docker client
DOCKER_CLIENT_TIMEOUT = 15          # seconds for Docker SDK HTTP requests
DOCKER_POOL_SIZE = 5                # urllib3 connection pool size
DOCKER_PING_TTL = 3                 # skip ping if last success was within this window (seconds)
DOCKER_BACKOFF = 5                  # seconds to wait after a failed connection before retrying
# TCP keepalive
TCP_KEEPALIVE_IDLE = 60             # seconds before first keepalive probe
TCP_KEEPALIVE_INTERVAL = 10         # seconds between probes
TCP_KEEPALIVE_COUNT = 3             # probes before declaring dead
# SSH tunnel
TUNNEL_DEFAULT_SOCKET = "/tmp/skiff-docker.sock"  # noqa: S108
TUNNEL_CONNECT_TIMEOUT = 15         # seconds for SSH to establish connection
TUNNEL_SOCKET_WAIT = 10             # seconds to wait for socket file to appear after SSH starts
TUNNEL_SOCKET_POLL = 0.3            # seconds between socket existence polls
TUNNEL_SERVER_ALIVE_INTERVAL = 30   # SSH ServerAliveInterval
TUNNEL_SERVER_ALIVE_COUNT = 3       # SSH ServerAliveCountMax
# WebSocket
WS_LOG_TAIL = 50                    # initial tail lines for log streaming
WS_KEEPALIVE_INTERVAL = 15          # seconds between WebSocket ping frames
WS_LOG_IDLE_TIMEOUT = 30            # seconds of no log output before closing stream
WS_EXEC_IDLE_TIMEOUT = 600          # seconds of exec terminal inactivity before closing
WS_EXEC_RECV_TIMEOUT = 0.5          # seconds for exec socket recv timeout
WS_TOKEN_TIMEOUT = 5.0              # seconds to wait for auth token as first WS message
WS_MAX_PER_IP = 5                   # max concurrent WebSocket connections per IP
WS_AUTH_MAX_ATTEMPTS = 3            # failed WS auth attempts before IP lockout
WS_AUTH_LOCKOUT_SECS = 300          # seconds to lock out IP after max failed WS auth attempts
WS_KEEPALIVE_REVALIDATE_EVERY = 4   # re-check session age every N keepalive ticks (~60s at 15s interval)
# Auth
MIN_TOKEN_LENGTH = 16               # minimum API token length enforced by setup
TOKEN_AUDIT_SUFFIX_LEN = 8          # chars of token shown in audit log
_SESSION_CACHE_MAX = 1000           # safety cap on in-memory session cache entries
# Server-side absolute session lifetime matching the JS constant (8 hours)
SESSION_ABS_TIMEOUT = 8 * 60 * 60  # seconds
# Rate limits (base values, scaled by RATE_LIMIT_SCALE)
RL_DEFAULT = "30/minute"
RL_FAST = "60/minute"
RL_SLOW = "10/minute"
# Audit log — configurable for retention requirements
# Default: 10 MB x 5 files ~= 50 MB (~13 days). For 1-year retention at ~4 MB/day:
#   AUDIT_MAX_MB=200 AUDIT_BACKUP_COUNT=20  → 4 GB (covers 13 months at 10 MB/day)
AUDIT_MAX_BYTES = int(os.environ.get("AUDIT_MAX_MB", "10")) * 1024 * 1024
AUDIT_BACKUP_COUNT = int(os.environ.get("AUDIT_BACKUP_COUNT", "5"))
# Registry
REGISTRY_SEARCH_PAGE_SIZE = 10
REGISTRY_MAX_TAGS = 20
REGISTRY_TIMEOUT = 8                # seconds for Docker Hub API requests
REGISTRY_DESC_MAX = 200             # max chars of registry description to return
# Containers
CONTAINER_STOP_TIMEOUT = 5          # seconds for graceful stop before kill
CONTAINER_RESTART_TIMEOUT = 10      # seconds for restart
CONTAINER_STATS_TIMEOUT = 10.0      # seconds for stats call
IMAGE_PULL_TIMEOUT = 300.0          # seconds for image pull
MAX_PORT_MAPPINGS = 10              # max published port mappings per container
PRIVILEGED_PORT_THRESHOLD = 1024    # host ports below this require elevated privilege
MAX_VOLUME_NAME_LENGTH = 63         # Docker volume name max length (chars after the first)
MAX_RESTART_RETRIES = 5             # on-failure restart maximum retry count
# Setup
SETUP_WINDOW_SECS = 300             # setup endpoint only callable within this many seconds of startup
SETUP_MAX_ATTEMPTS = 3              # failed POST /api/setup attempts before IP lockout
SETUP_LOCKOUT_SECS = 300            # seconds to lock out IP after max failed setup attempts
# Security headers
HSTS_MAX_AGE = 31536000             # 1 year in seconds
HSTS_HEADER = f"max-age={HSTS_MAX_AGE}; includeSubDomains"
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
    " connect-src 'self' ws: wss:; img-src 'self' data:; frame-ancestors 'none'"
)
_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), usb=()"
# Optional GCP Cloud Logging sink
_GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_GCP_LOG_NAME = os.environ.get("GCP_LOG_NAME", "skiff-audit")

# ── Rate limiting ──────────────────────────────────────────
_RATE_SCALE_RAW = int(os.environ.get("RATE_LIMIT_SCALE", "1"))
if not (1 <= _RATE_SCALE_RAW <= 100):
    raise ValueError(f"RATE_LIMIT_SCALE must be between 1 and 100, got {_RATE_SCALE_RAW}")
_RATE_SCALE = _RATE_SCALE_RAW


def _limit(spec: str) -> str:
    """Scale a rate limit spec by RATE_LIMIT_SCALE (e.g. '10/minute' → '100/minute')."""
    if _RATE_SCALE == 1:
        return spec
    count, _, period = spec.partition("/")
    return f"{int(count) * _RATE_SCALE}/{period}"


limiter = Limiter(key_func=get_remote_address)
