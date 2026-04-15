# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""
SKIFF Container Manager — FastAPI backend.
Cloud-native container manager; connects to a remote Docker engine via SSH tunnel.
Authentication: Bearer token required on all endpoints (configurable).
"""

import asyncio
import collections
import hmac
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import docker
import docker.errors
import requests
import requests.exceptions
import structlog
import yaml
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from requests.adapters import HTTPAdapter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

# Resolve bundled asset paths relative to this file so they work after pip install.
_PKG_DIR = Path(__file__).parent
_STATIC_DIR = _PKG_DIR / "static"
_LICENSE_FILE = _PKG_DIR.parent / "LICENSE"

# ── Configuration ──────────────────────────────────────────
class _Config:
    """Mutable runtime configuration. Populated from env on startup; can be
    updated via /api/setup when running without a pre-configured environment."""

    def __init__(self) -> None:
        self.docker_host: str = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        _reg_default = os.environ.get("ALLOWED_REGISTRY", "us-docker.pkg.dev/")
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
        # True when config came from environment — setup endpoint disabled in that case
        # True when config came from env with a non-empty token — setup endpoint disabled
        self.from_env: bool = bool(os.environ.get("API_TOKEN", "").strip())

_cfg = _Config()

COMPOSE_DIR = Path(os.environ.get("COMPOSE_DIR", "/data/compose"))
DOCKER_BIN = shutil.which("docker") or "/usr/bin/docker"
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG", "/var/log/skiff-audit.jsonl"))
MAX_COMPOSE_SIZE = 1024 * 256  # 256KB
MAX_LOG_TAIL = 5000
MAX_AUDIT_LINES = 2000
MAX_CONTAINERS = 50
MAX_CONTAINER_MEM = "2g"
MAX_CONTAINER_CPU = 2.0
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")

# ── Named constants (no magic values) ──────────────────────
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
# Auth
MIN_TOKEN_LENGTH = 16               # minimum API token length enforced by setup
TOKEN_AUDIT_SUFFIX_LEN = 8          # chars of token shown in audit log
# Rate limits (base values, scaled by RATE_LIMIT_SCALE)
RL_DEFAULT = "30/minute"
RL_FAST = "60/minute"
RL_SLOW = "10/minute"
# Audit log — configurable for SOC 2 retention requirements
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
# Security headers
HSTS_MAX_AGE = 31536000             # 1 year in seconds
HSTS_HEADER = f"max-age={HSTS_MAX_AGE}; includeSubDomains"

# ── Logging ────────────────────────────────────────────────
def _level_to_severity(logger, method_name, event_dict):
    """Map Python log levels to Cloud Logging severity field."""
    level = event_dict.pop("level", method_name)
    severity_map = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR", "critical": "CRITICAL"}
    event_dict["severity"] = severity_map.get(level, "DEFAULT")
    return event_dict


def _make_audit_handler() -> logging.handlers.RotatingFileHandler | None:
    """Return a RotatingFileHandler for the audit log, or None if the path is not writable."""
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            AUDIT_LOG_PATH,
            maxBytes=AUDIT_MAX_BYTES,
            backupCount=AUDIT_BACKUP_COUNT,
            encoding="utf-8",
        )
        return handler
    except OSError as exc:
        print(f"WARNING: audit log path {AUDIT_LOG_PATH} is not writable ({exc}). Audit log disabled.", flush=True)  # noqa: T201
        return None


_audit_handler = _make_audit_handler()

# ── Optional GCP Cloud Logging sink ───────────────────────
# Activated when GOOGLE_CLOUD_PROJECT env var is set and google-cloud-logging is installed.
# Dual-writes every structured log entry to Cloud Logging alongside local file + stdout.
_gcp_logger = None
_GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_GCP_LOG_NAME = os.environ.get("GCP_LOG_NAME", "skiff-audit")
if _GCP_PROJECT:
    try:
        import google.cloud.logging as _gcl  # type: ignore[import]
        _gcp_client = _gcl.Client(project=_GCP_PROJECT)
        _gcp_logger = _gcp_client.logger(_GCP_LOG_NAME)
        print(f"INFO: GCP Cloud Logging sink active → project={_GCP_PROJECT} log={_GCP_LOG_NAME}", flush=True)  # noqa: T201
    except ImportError:
        pass
    except Exception as _gcp_exc:
        print(f"WARNING: GCP Cloud Logging init failed: {_gcp_exc}", flush=True)  # noqa: T201


def _audit_file_sink(_, __, event_dict):
    """Write every log line to the rotating audit JSONL file and optionally GCP Cloud Logging."""
    if _audit_handler is not None:
        line = json.dumps(event_dict) + "\n"
        try:
            _audit_handler.stream.write(line)
            _audit_handler.stream.flush()
        except OSError:
            pass
    if _gcp_logger is not None:
        try:
            severity = event_dict.get("severity", "DEFAULT")
            _gcp_logger.log_struct(event_dict, severity=severity)
        except Exception:
            pass
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _level_to_severity,
        structlog.processors.TimeStamper(fmt="iso"),
        _audit_file_sink,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()

# ── Docker Client Singleton ────────────────────────────────
# Note: _client_lock is held during SSH connect (~2-15s). With --workers 1,
# FastAPI runs sync endpoints in a threadpool. If SSH hangs, threads queue
# on the lock until backoff kicks in on subsequent attempts.
_client_lock = threading.Lock()
_client: docker.DockerClient | None = None
_client_failed_at: float = 0.0
_client_last_ping: float = 0.0

DOCKER_TRANSIENT = (
    docker.errors.DockerException,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    socket.timeout,
    OSError,
)


def _build_client() -> docker.DockerClient:
    client = docker.DockerClient(
        base_url=_cfg.docker_host,
        timeout=DOCKER_CLIENT_TIMEOUT,
        max_pool_size=DOCKER_POOL_SIZE,
    )
    # TCP keepalive to detect silent SSH tunnel drops
    try:
        _ka_opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        if hasattr(socket, "TCP_KEEPIDLE"):
            _ka_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE))
        if hasattr(socket, "TCP_KEEPINTVL"):
            _ka_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL))
        if hasattr(socket, "TCP_KEEPCNT"):
            _ka_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT))
        _adapter = HTTPAdapter()
        _adapter.poolmanager.connection_pool_kw["socket_options"] = _ka_opts
        client.api.mount("http://", _adapter)
        client.api.mount("http+docker://", _adapter)
    except Exception:
        pass
    client.ping()
    return client


def get_client() -> docker.DockerClient:
    global _client, _client_failed_at, _client_last_ping
    with _client_lock:
        now = time.monotonic()
        if _client is None and (now - _client_failed_at) < DOCKER_BACKOFF:
            raise docker.errors.DockerException("Docker connection in backoff")
        if _client is not None:
            # Skip ping if we pinged recently (avoids SSH round-trip on every request)
            if (now - _client_last_ping) < DOCKER_PING_TTL:
                return _client
            try:
                _client.ping()
                _client_last_ping = now
                return _client
            except Exception:
                log.warning("docker.client_stale", action="reconnecting")
                try:
                    _client.close()
                except Exception:
                    pass
                _client = None
        try:
            _client = _build_client()
            _client_last_ping = time.monotonic()
            log.info("docker.connected", host=_cfg.docker_host)
            return _client
        except Exception as exc:
            _client = None
            _client_failed_at = time.monotonic()
            log.error("docker.connection_failed", host=_cfg.docker_host, error=str(exc))
            raise


def _invalidate_client():
    global _client, _client_last_ping
    with _client_lock:
        _client_last_ping = 0.0
        if _client:
            try:
                _client.close()
            except Exception:
                pass
        _client = None


# ── SSH Tunnel Management ──────────────────────────────────
_SSH_TARGET_RE = re.compile(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$")
_tunnel_lock = threading.Lock()
_tunnel_ctl_sock: str = ""
_tunnel_ssh_target: str = ""
_tunnel_socket_path: str = ""


def _stop_tunnel_locked() -> None:
    """Stop the managed SSH tunnel. Must be called with _tunnel_lock held."""
    global _tunnel_ctl_sock, _tunnel_ssh_target, _tunnel_socket_path
    if _tunnel_ctl_sock and _tunnel_ssh_target:
        try:
            subprocess.run(
                ["ssh", "-S", _tunnel_ctl_sock, "-O", "exit", _tunnel_ssh_target],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except Exception:
            pass
        for path in (_tunnel_ctl_sock, _tunnel_socket_path):
            try:
                if path and os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
    _tunnel_ctl_sock = ""
    _tunnel_ssh_target = ""
    _tunnel_socket_path = ""


def _start_tunnel(ssh_target: str, socket_path: str) -> None:
    """Start an SSH ControlMaster tunnel. Raises ValueError on failure."""
    global _tunnel_ctl_sock, _tunnel_ssh_target, _tunnel_socket_path
    with _tunnel_lock:
        _stop_tunnel_locked()
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        ctl_sock = f"/tmp/skiff-tunnel-ctl-{os.getpid()}.sock"  # noqa: S108
        cmd = [
            "ssh", "-fNM",
            "-S", ctl_sock,
            "-o", "ControlPersist=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={TUNNEL_CONNECT_TIMEOUT}",
            "-o", f"ServerAliveInterval={TUNNEL_SERVER_ALIVE_INTERVAL}",
            "-o", f"ServerAliveCountMax={TUNNEL_SERVER_ALIVE_COUNT}",
            "-L", f"{socket_path}:/var/run/docker.sock",
            ssh_target,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=TUNNEL_CONNECT_TIMEOUT + 5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("SSH connection timed out") from exc
        except FileNotFoundError as exc:
            raise ValueError("ssh binary not found") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise ValueError(f"SSH failed: {stderr[:200] or 'unknown error'}")
        deadline = time.monotonic() + TUNNEL_SOCKET_WAIT
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                break
            time.sleep(TUNNEL_SOCKET_POLL)
        else:
            raise ValueError(f"Tunnel socket did not appear at {socket_path}")
        _tunnel_ctl_sock = ctl_sock
        _tunnel_ssh_target = ssh_target
        _tunnel_socket_path = socket_path
        log.info("tunnel.started", target=ssh_target, socket=socket_path)


def _stop_tunnel() -> None:
    """Stop the managed SSH tunnel (public, acquires lock)."""
    with _tunnel_lock:
        _stop_tunnel_locked()


def docker_client_dep():
    try:
        return get_client()
    except Exception as exc:
        raise HTTPException(503, "Container engine unreachable") from exc


# ── Auth & Validation ──────────────────────────────────────
def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ── Server-side session age tracking ──────────────────────
# Maps token_hash → first-seen timestamp. Rejects tokens older than SESSION_ABS_TIMEOUT.
# Cleared when _cfg.api_token is rotated (setup endpoint calls _invalidate_session_cache).
_session_first_seen: dict[str, float] = {}
_session_lock = threading.Lock()
# Server-side absolute session lifetime matching the JS constant (8 hours)
SESSION_ABS_TIMEOUT = 8 * 60 * 60  # seconds


def _check_session_age(token: str) -> None:
    """Reject tokens that have been active longer than SESSION_ABS_TIMEOUT."""
    import hashlib
    token_hash = hashlib.sha256(b"skiff-session:" + token.encode()).hexdigest()[:16]  # short fingerprint
    now = time.monotonic()
    with _session_lock:
        first = _session_first_seen.get(token_hash)
        if first is None:
            _session_first_seen[token_hash] = now
        elif (now - first) > SESSION_ABS_TIMEOUT:
            raise HTTPException(401, "Session expired — please reload and re-authenticate")


def _invalidate_session_cache() -> None:
    """Clear session age tracking (call when token is rotated)."""
    with _session_lock:
        _session_first_seen.clear()


def verify_auth(request: Request):
    """Dependency: verifies bearer token and enforces server-side session lifetime."""
    if not _cfg.api_token:
        return
    auth = request.headers.get("Authorization", "")
    if not _constant_time_compare(auth, f"Bearer {_cfg.api_token}"):
        raise HTTPException(401, "Invalid or missing API token")
    _check_session_age(auth[7:])  # track age of valid tokens only


def verify_auth_strict(request: Request):
    """Like verify_auth but always requires a token — used for sensitive endpoints."""
    if not _cfg.api_token:
        raise HTTPException(503, "Server not configured — set API_TOKEN before accessing this endpoint")
    auth = request.headers.get("Authorization", "")
    if not _constant_time_compare(auth, f"Bearer {_cfg.api_token}"):
        raise HTTPException(401, "Invalid or missing API token")
    _check_session_age(auth[7:])


def verify_csrf(request: Request):
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        xrw = request.headers.get("X-Requested-With", "")
        if xrw != "ContainerManager":
            raise HTTPException(403, "Missing or invalid X-Requested-With header")


CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{4,64}$")
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
IMAGE_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}$")
NETWORK_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def validate_container_id(container_id: str) -> str:
    if not CONTAINER_ID_RE.match(container_id):
        raise HTTPException(400, "Invalid container ID format")
    return container_id


def validate_project_name(project_name: str) -> str:
    if not PROJECT_NAME_RE.match(project_name):
        raise HTTPException(400, "Invalid project name")
    return project_name


IMAGE_ID_RE = re.compile(r"^(sha256:)?[a-f0-9]{4,64}$")


def validate_image_id(image_id: str) -> str:
    if not IMAGE_ID_RE.match(image_id):
        raise HTTPException(400, "Invalid image ID format")
    return image_id


def validate_image_registry(image: str):
    if not IMAGE_TAG_RE.match(image):
        raise HTTPException(400, "Invalid image name format")
    if not _cfg.allowed_registries:
        return
    # Extract registry hostname (everything before first /)
    image_no_tag = image.split(":", maxsplit=1)[0] if "@" not in image else image.split("@", maxsplit=1)[0]
    parts = image_no_tag.split("/")
    # Images with dots or colons in the first segment have an explicit registry
    has_registry_host = len(parts) >= 2 and ("." in parts[0] or ":" in parts[0])
    image_registry = parts[0] if has_registry_host else ""
    if not image_registry:
        # Short names (e.g. "nginx", "alpine") implicitly belong to docker.io.
        # Allow them when docker.io is in the allowlist; reject otherwise.
        if any(r.rstrip("/") == "docker.io" for r in _cfg.allowed_registries):
            return
        raise HTTPException(
            400, f"Image must include an explicit registry hostname. Allowed: {', '.join(_cfg.allowed_registries)}"
        )
    # Check registry matches an allowed registry (exact domain or domain prefix with /)
    if not any(
        image_registry == r.rstrip("/") or image.startswith(r if r.endswith("/") else r + "/")
        for r in _cfg.allowed_registries
    ):
        allowed = ', '.join(_cfg.allowed_registries)
        raise HTTPException(400, f"Only images from approved registries are allowed: {allowed}")


def validate_container_name(name: str | None) -> str | None:
    if name is None:
        return None
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(400, "Invalid container name (alphanumeric, dots, hyphens, underscores)")
    return name


def _get_container(client, container_id: str):
    """Get container with proper error handling."""
    validate_container_id(container_id)
    try:
        return client.containers.get(container_id)
    except docker.errors.NotFound as exc:
        raise HTTPException(404, "Container not found") from exc
    except DOCKER_TRANSIENT as e:
        log.warning("docker.transient_error", error=str(e))
        _invalidate_client()
        raise HTTPException(503, "Container engine unreachable") from e


def safe_docker_call(fn, *args, **kwargs):
    """Execute a Docker SDK call with transient-error handling.

    For top-level client methods (client.containers.list) a single retry is
    attempted after invalidating the client, since a fresh client will work.
    For object-bound methods (container.start) the retry is skipped — the
    object retains a reference to the closed client and would fail again.
    The caller's next request will use a fresh client via get_client().
    """
    self = getattr(fn, "__self__", None)
    is_object_bound = self is not None and not isinstance(self, type)
    attempts = 1 if is_object_bound else 2
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except docker.errors.NotFound as exc:
            raise HTTPException(404, "Resource not found") from exc
        except docker.errors.APIError as e:
            if e.status_code == 409:
                raise HTTPException(409, "Container conflict (already started/stopped?)") from e
            raise HTTPException(e.status_code or 400, str(e.explanation or "Container operation failed")[:500]) from e
        except DOCKER_TRANSIENT as e:
            if attempt == 0:
                log.warning("docker.transient_error", error=str(e), action="invalidating_client")
                _invalidate_client()
                continue
            raise HTTPException(503, "Container engine unreachable") from e
    raise HTTPException(503, "Container engine unreachable")  # pragma: no cover


# ── WebSocket Rate Limiting ────────────────────────────────
_ws_connections: dict[str, int] = collections.defaultdict(int)
_ws_lock = threading.Lock()


def _ws_acquire(ip: str) -> None:
    with _ws_lock:
        if _ws_connections[ip] >= WS_MAX_PER_IP:
            raise HTTPException(429, "Too many WebSocket connections from this IP")
        _ws_connections[ip] += 1


def _ws_release(ip: str) -> None:
    with _ws_lock:
        _ws_connections[ip] = max(0, _ws_connections[ip] - 1)


# ── Compose File Validation ────────────────────────────────
_BLOCKED_MOUNT_TARGETS = {"/etc", "/proc", "/sys", "/dev", "/var/run", "/run"}


def _validate_mount_target(path: str) -> None:
    """Reject mounts to sensitive container paths."""
    if not path.startswith("/"):
        raise HTTPException(400, "Volume mount target must be an absolute path")
    normalized = path.rstrip("/")
    for blocked in _BLOCKED_MOUNT_TARGETS:
        if normalized == blocked or normalized.startswith(blocked + "/"):
            raise HTTPException(400, f"Mount target {path!r} is not permitted")


# Keys blocked regardless of value (their presence alone is disallowed)
BLOCKED_PRESENCE_KEYS = {"privileged", "configs", "secrets", "build", "devices"}

# Keys blocked only when set to a truthy value (false/null/[] is a valid override)
BLOCKED_TRUTHY_KEYS = {
    "cap_add", "userns_mode", "sysctls", "security_opt", "shm_size",
    "extends", "volumes_from", "env_file",
    "cgroup_parent", "dns", "dns_search", "extra_hosts", "tmpfs",
    "uts", "cgroupns_mode", "storage_opt", "device_cgroup_rules",
}
BLOCKED_COMPOSE_SERVICE_KEYS = BLOCKED_PRESENCE_KEYS | BLOCKED_TRUTHY_KEYS  # backwards-compat alias
BLOCKED_COMPOSE_TOP_KEYS = {"configs", "secrets"}
BLOCKED_NETWORK_MODES = {"host", "container", "service"}


def validate_compose_file(content: bytes) -> dict:
    if len(content) > MAX_COMPOSE_SIZE:
        raise HTTPException(400, f"Compose file too large (max {MAX_COMPOSE_SIZE // 1024}KB)")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise HTTPException(400, "Invalid YAML in compose file") from exc

    if not isinstance(data, dict):
        raise HTTPException(400, "Compose file must be a YAML mapping")

    # Block top-level keys that can reference host files
    for blocked_top in BLOCKED_COMPOSE_TOP_KEYS:
        if data.get(blocked_top):
            raise HTTPException(400, f"Top-level '{blocked_top}' is not allowed — cannot reference host files")

    services = data.get("services", {})
    if not isinstance(services, dict):
        raise HTTPException(400, "Invalid services section")

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            raise HTTPException(400, f"Service '{svc_name}' must be a mapping, got {type(svc).__name__}")

        for key in BLOCKED_PRESENCE_KEYS:
            if key in svc:
                raise HTTPException(
                    400, f"Service '{svc_name}': '{key}' is not allowed for security reasons",
                )

        for key in BLOCKED_TRUTHY_KEYS:
            if key in svc:
                val = svc[key]
                if val is True or (isinstance(val, (list, dict, str)) and val):
                    raise HTTPException(
                        400, f"Service '{svc_name}': '{key}' is not allowed for security reasons",
                    )

        net_mode = str(svc.get("network_mode", ""))
        if any(net_mode.startswith(m) for m in BLOCKED_NETWORK_MODES):
            raise HTTPException(400, f"Service '{svc_name}': network_mode '{net_mode}' is not allowed")

        pid_mode = str(svc.get("pid", ""))
        if pid_mode == "host":
            raise HTTPException(400, f"Service '{svc_name}': pid mode 'host' is not allowed")

        ipc_mode = str(svc.get("ipc", ""))
        if ipc_mode == "host":
            raise HTTPException(400, f"Service '{svc_name}': ipc mode 'host' is not allowed")

        for vol in svc.get("volumes", []):
            vol_str = str(vol) if isinstance(vol, str) else vol.get("source", "") if isinstance(vol, dict) else str(vol)
            if vol_str.startswith(("/", "~", "..", "$")):
                raise HTTPException(400, f"Service '{svc_name}': host path mounts are not allowed")

        image = svc.get("image", "")
        if image:
            validate_image_registry(image)

    return data


# ── Lifespan ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _cfg.api_token:
        log.warning("security.no_api_token", msg="Running without auth — set API_TOKEN for production")
    if not _cfg.allowed_registries:
        log.warning(
            "security.no_registry_allowlist",
            msg="ALLOWED_REGISTRIES is empty — all registries permitted. Set for production.",
        )
    log.info("app.started", docker_host=_cfg.docker_host, registries=_cfg.allowed_registries, bind=BIND_HOST)
    yield
    log.info("app.shutdown")
    _stop_tunnel()
    _invalidate_client()


# ── App ────────────────────────────────────────────────────
app = FastAPI(title="SKIFF Container Manager", lifespan=lifespan)

# Rate limits can be scaled up for testing via RATE_LIMIT_SCALE env var (e.g. "10" = 10x)
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
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cfg.allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "X-Requested-With", "Content-Type"],
)

# ── Audit event classification ─────────────────────────────
# Maps (HTTP method, path prefix) → semantic event_type label for SIEM ingestion.
# More-specific prefixes must appear before less-specific ones.
_AUDIT_EVENT_MAP: list[tuple[str, str, str]] = [
    # Containers
    ("POST",   "/api/containers/",        "container.run"),
    ("POST",   "/api/containers",         "container.list_refresh"),
    ("DELETE", "/api/containers/",        "container.removed"),
    # Container sub-actions (start/stop/restart/kill/pause/unpause/rename/exec)
    ("POST",   "/api/containers",         "container.action"),
    # Logs / exec WebSocket
    ("GET",    "/ws/logs/",               "container.logs_stream"),
    ("GET",    "/ws/exec/",               "container.exec_session"),
    # Images
    ("POST",   "/api/images/pull",        "image.pull"),
    ("POST",   "/api/images/push",        "image.push"),
    ("POST",   "/api/images/tag",         "image.tag"),
    ("DELETE", "/api/images/",            "image.removed"),
    ("GET",    "/api/images",             "image.list"),
    # Volumes
    ("POST",   "/api/volumes",            "volume.created"),
    ("DELETE", "/api/volumes/",           "volume.removed"),
    ("POST",   "/api/volumes/prune",      "volume.pruned"),
    # Networks
    ("POST",   "/api/networks",           "network.created"),
    ("DELETE", "/api/networks/",          "network.removed"),
    ("POST",   "/api/networks/prune",     "network.pruned"),
    # Compose
    ("POST",   "/api/compose/up",         "compose.deployed"),
    ("DELETE", "/api/compose/",           "compose.torn_down"),
    # System
    ("POST",   "/api/system/prune",       "system.pruned"),
    ("GET",    "/api/system/audit-log",   "audit.log_read"),
    # Setup
    ("POST",   "/api/setup/tunnel",       "setup.tunnel_start"),
    ("DELETE", "/api/setup/tunnel",       "setup.tunnel_stop"),
    ("POST",   "/api/setup",              "setup.configured"),
    # Auth failures (status 401/403 — applied after method/path match)
    ("*",      "/api/",                   "api.request"),
]

# Regex to extract resource type and ID from common path patterns
_RESOURCE_PATH_RE = re.compile(
    r"/api/(?P<rtype>containers|images|volumes|networks|compose)/(?P<rid>[^/]+)"
)


def _classify_event(method: str, path: str, status: int) -> tuple[str, str, str]:
    """Return (event_type, resource_type, resource_id) for an API request."""
    if status in {401, 403}:
        return "auth.denied", "", ""
    if status == 429:
        return "rate_limit.exceeded", "", ""
    for m, prefix, label in _AUDIT_EVENT_MAP:
        if (m in ("*", method)) and path.startswith(prefix):
            event_type = label
            break
    else:
        event_type = "api.request"
    # Extract resource identity from path
    m2 = _RESOURCE_PATH_RE.match(path)
    resource_type = m2.group("rtype").rstrip("s") if m2 else ""  # containers→container
    resource_id = m2.group("rid") if m2 else ""
    return event_type, resource_type, resource_id


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Log all authenticated API requests for governance compliance (SOC 2 CC7.1/CC7.2)."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            auth_header = request.headers.get("authorization", "")
            token_hint = ""
            token_suffix = ""
            if auth_header.startswith("Bearer ") and _cfg.api_token:
                provided = auth_header[7:]
                if _constant_time_compare(provided, _cfg.api_token):
                    token_hint = "authenticated"
                    token_suffix = (
                        provided[-TOKEN_AUDIT_SUFFIX_LEN:]
                        if len(provided) >= TOKEN_AUDIT_SUFFIX_LEN
                        else provided
                    )
                else:
                    token_hint = "invalid"

            # Per-user identity: populated by oauth2-proxy or any identity-aware reverse proxy
            forwarded_user = request.headers.get("x-forwarded-user", "")

            event_type, resource_type, resource_id = _classify_event(
                request.method, request.url.path, response.status_code
            )

            level = "warning" if response.status_code in (401, 403, 429) else (
                "error" if response.status_code >= 500 else "info"
            )
            extra: dict = {}
            if token_suffix:
                extra["token_suffix"] = token_suffix
            if forwarded_user:
                extra["user"] = forwarded_user
            if resource_type:
                extra["resource_type"] = resource_type
            if resource_id:
                extra["resource_id"] = resource_id

            getattr(log, level)(
                "audit.api_access",
                event_type=event_type,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                remote=request.client.host if request.client else "unknown",
                auth=token_hint or ("none" if not auth_header else "present"),
                **extra,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
            " connect-src 'self' ws: wss:; img-src 'self' data:; frame-ancestors 'none'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), usb=()"
        # Check both direct scheme and X-Forwarded-Proto (Cloud Workstation proxy terminates TLS)
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        if scheme == "https":
            response.headers["Strict-Transport-Security"] = HSTS_HEADER
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuditLogMiddleware)

APP_START = time.time()

# Common dependency list for all authenticated endpoints
AUTH = [Depends(verify_auth)]


# ── Auth Info (no auth needed) ─────────────────────────────
@app.get("/api/auth-required")
def auth_required():
    """Returns whether auth is required and frontend config. No secrets exposed."""
    return {"required": bool(_cfg.api_token)}


# ── Setup (only active when unconfigured) ─────────────────
@app.get("/api/setup-state")
def setup_state():
    """Returns configuration state for the setup wizard. No auth required."""
    return {
        "configured": bool(_cfg.api_token),
        "from_env": _cfg.from_env,
        "tunnel_active": bool(_tunnel_socket_path and os.path.exists(_tunnel_socket_path)),
        "tunnel_socket": _tunnel_socket_path or TUNNEL_DEFAULT_SOCKET,
    }


@app.post("/api/setup")
@limiter.limit(_limit(RL_SLOW))
def do_setup(
    request: Request,
    docker_host: str = Body(...),
    api_token: str = Body(...),
    allowed_registries: str = Body(default=""),
):
    """Configure the server in-memory. Only callable when unconfigured and not from env."""
    verify_csrf(request)
    if _cfg.from_env:
        raise HTTPException(403, "Server is configured via environment variables — setup endpoint disabled")
    if _cfg.api_token:
        raise HTTPException(403, "Already configured")
    if not api_token or len(api_token) < MIN_TOKEN_LENGTH:
        raise HTTPException(400, f"api_token must be at least {MIN_TOKEN_LENGTH} characters")
    if not docker_host:
        raise HTTPException(400, "docker_host is required")
    _dh = docker_host.strip()
    _parsed = urlparse(_dh)
    if _parsed.scheme not in ("unix", "tcp", "npipe"):
        raise HTTPException(400, "docker_host must use unix://, tcp://, or npipe:// scheme")
    if _parsed.scheme == "tcp":
        # Only allow explicit IP:port — no arbitrary hostnames
        import ipaddress
        _host = _parsed.hostname or ""
        try:
            ipaddress.ip_address(_host)
        except ValueError:
            raise HTTPException(400, "tcp:// docker_host must specify an IP address, not a hostname") from None
        if not (1 <= (_parsed.port or 0) <= 65535):
            raise HTTPException(400, "tcp:// docker_host must include a valid port")
    _cfg.api_token = api_token.strip()
    _cfg.docker_host = _dh
    _cfg.allowed_registries = [r.strip() for r in allowed_registries.split(",") if r.strip()]
    _invalidate_client()
    _invalidate_session_cache()
    log.info("setup.configured", docker_host=_cfg.docker_host, registries=_cfg.allowed_registries)
    return {"ok": True}


@app.post("/api/setup/tunnel")
@limiter.limit(_limit(RL_SLOW))
def start_tunnel(
    request: Request,
    ssh_target: str = Body(...),
    socket_path: str = Body(default=TUNNEL_DEFAULT_SOCKET),
):
    """Start an SSH ControlMaster tunnel to the Docker VM. Setup-only endpoint."""
    verify_csrf(request)
    if _cfg.from_env:
        raise HTTPException(403, "Setup endpoints disabled when configured via environment")
    if _cfg.api_token:
        raise HTTPException(403, "Already configured")
    if not _SSH_TARGET_RE.match(ssh_target):
        raise HTTPException(400, "ssh_target must be user@host")
    _sp_resolved = Path(socket_path).resolve()
    # /tmp is a symlink to /private/tmp on macOS — check both canonical paths
    _tmp_resolved = Path("/tmp").resolve()  # noqa: S108
    if not str(_sp_resolved).startswith(str(_tmp_resolved) + "/"):
        raise HTTPException(400, "socket_path must resolve to a path under /tmp/")
    try:
        _start_tunnel(ssh_target, socket_path)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "socket_path": socket_path, "docker_host": f"unix://{socket_path}"}


@app.delete("/api/setup/tunnel")
@limiter.limit(_limit(RL_SLOW))
def stop_tunnel_endpoint(request: Request):
    """Stop the managed SSH tunnel."""
    verify_csrf(request)
    if _cfg.from_env:
        raise HTTPException(403, "Setup endpoints disabled when configured via environment")
    _stop_tunnel()
    return {"ok": True}


# ── Health Endpoints (no auth) ─────────────────────────────
@app.get("/api/registry/search", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def registry_search(request: Request, q: str = Query(..., min_length=1, max_length=100)):
    """Proxy Docker Hub image search to avoid browser CORS restrictions."""
    try:
        resp = requests.get(
            "https://hub.docker.com/v2/search/repositories/",
            params={"query": q, "page_size": REGISTRY_SEARCH_PAGE_SIZE},
            timeout=REGISTRY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {
                "repo_name": item.get("repo_name") or item.get("name", ""),
                "short_description": (item.get("short_description") or "")[:REGISTRY_DESC_MAX],
                "pull_count": item.get("pull_count", 0),
                "is_official": bool(item.get("is_official")),
            }
            for item in data.get("results", [])
            if item.get("repo_name") or item.get("name")
        ]
        return {"results": results}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(502, f"Registry search failed: {exc}") from exc


@app.get("/api/registry/tags", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def registry_tags(request: Request, image: str = Query(..., min_length=1, max_length=200)):
    """Fetch available tags for a Docker Hub image."""
    # Normalize: official images like 'nginx' become 'library/nginx'
    repo = image.strip("/")
    if "/" not in repo:
        repo = f"library/{repo}"
    try:
        resp = requests.get(
            f"https://hub.docker.com/v2/repositories/{repo}/tags/",
            params={"page_size": REGISTRY_MAX_TAGS, "ordering": "last_updated"},
            timeout=REGISTRY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        tags = [
            t["name"] for t in data.get("results", [])
            if isinstance(t.get("name"), str) and t["name"]
        ]
        return {"image": image, "tags": tags[:REGISTRY_MAX_TAGS]}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(502, f"Tag fetch failed: {exc}") from exc


@app.get("/api/config", dependencies=AUTH)
@limiter.limit(_limit(RL_FAST))
def get_config(request: Request):
    """Return non-secret server configuration for the UI."""
    return {
        "allowed_registries": _cfg.allowed_registries,
        "docker_vm_host": _cfg.docker_vm_host,
        "docker_host": _cfg.docker_host,
    }


_APP_VERSION = "1.0.0"


@app.get("/health")
def health():
    """Liveness — never checks Docker to avoid restart loops."""
    return {"status": "ok", "uptime_seconds": int(time.time() - APP_START), "version": _APP_VERSION}


@app.get("/ready")
def ready():
    """Readiness — returns 503 if Docker is unreachable."""
    try:
        client = get_client()
        info = client.info()
        return {
            "status": "ready",
            "docker_version": info.get("ServerVersion", "unknown"),
            "containers_running": info.get("ContainersRunning", 0),
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(exc)})


# ── Containers ─────────────────────────────────────────────
@app.get("/api/containers", dependencies=AUTH)
@limiter.limit(_limit(RL_FAST))
def list_containers(request: Request, client=Depends(docker_client_dep)):
    containers = safe_docker_call(client.containers.list, all=True)
    result = []
    for c in containers:
        try:
            image_name = c.image.tags[0] if c.image.tags else c.image.short_id
        except Exception:
            image_name = "unknown"
        result.append({
            "id": c.short_id,
            "name": c.name,
            "image": image_name,
            "status": c.status,
            "state": c.attrs.get("State", {}).get("Status", "unknown"),
            "health": c.attrs.get("State", {}).get("Health", {}).get("Status", "none")
            if isinstance(c.attrs.get("State", {}).get("Health"), dict) else "none",
            "ports": c.ports,
            "created": c.attrs.get("Created", ""),
        })
    return result


@app.post("/api/containers/run", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def run_container(
    request: Request,
    image: str,
    name: str | None = None,
    ports: dict[str, str] | None = Body(default=None),
    environment: list[str] | None = Body(default=None),
    command: str | None = Body(default=None),
    volumes: list[str] | None = Body(default=None),
    restart_policy: str | None = Body(default=None),
    network: str | None = Body(default=None),
    labels: dict[str, str] | None = Body(default=None),
    read_only: bool = Body(default=True),
    client=Depends(docker_client_dep),
):
    verify_csrf(request)
    validate_image_registry(image)
    validate_container_name(name)

    if environment:
        for env in environment:
            if "=" not in env or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*=", env):
                raise HTTPException(400, f"Invalid environment variable format: {env[:50]}. Use KEY=VALUE.")

    # Validate volumes — only named volumes allowed, no host paths
    volume_binds = {}
    if volumes:
        for vol in volumes:
            if ":" not in vol:
                raise HTTPException(400, f"Invalid volume format: {vol[:50]}. Use name:/path.")
            parts = vol.split(":", 2)
            vol_name, mount_path = parts[0], parts[1]
            _validate_mount_target(mount_path)
            if vol_name.startswith(("/", "~", "..", "$")):
                raise HTTPException(400, "Host path mounts are not allowed — use named volumes only.")
            if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", vol_name):
                raise HTTPException(400, f"Invalid volume name: {vol_name[:50]}")
            mode = parts[2] if len(parts) > 2 and parts[2] in ("ro", "rw") else "rw"
            volume_binds[vol_name] = {"bind": mount_path, "mode": mode}

    # Validate restart policy
    valid_restart = {
        "no": {},
        "on-failure": {"Name": "on-failure", "MaximumRetryCount": 5},
        "unless-stopped": {"Name": "unless-stopped"},
        "always": {"Name": "always"},
    }
    rp = valid_restart.get(restart_policy or "no")
    if rp is None:
        raise HTTPException(400, "Invalid restart policy")

    # Validate network name if provided
    if network:
        if not NETWORK_NAME_RE.match(network):
            raise HTTPException(400, "Invalid network name")

    # Validate labels
    if labels:
        if len(labels) > 50:
            raise HTTPException(400, "Too many labels (max 50)")
        for lk, lv in labels.items():
            if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$", lk):
                raise HTTPException(400, f"Invalid label key: {lk[:50]}")
            if len(str(lv)) > 4096:
                raise HTTPException(400, f"Label value too long for key: {lk[:50]} (max 4096 chars)")

    existing = len(client.containers.list(all=True))
    if existing >= MAX_CONTAINERS:
        raise HTTPException(400, f"Container limit ({MAX_CONTAINERS}) reached")

    run_kwargs = dict(
        name=name,
        ports=ports,
        environment=environment,
        detach=True,
        mem_limit=MAX_CONTAINER_MEM,
        nano_cpus=int(MAX_CONTAINER_CPU * 1e9),
        security_opt=["no-new-privileges:true"],
        read_only=read_only,
    )
    if command:
        run_kwargs["command"] = command
    if volume_binds:
        run_kwargs["volumes"] = volume_binds
    if restart_policy and restart_policy != "no":
        run_kwargs["restart_policy"] = rp
    if network:
        run_kwargs["network"] = network
    if labels:
        run_kwargs["labels"] = labels

    container = safe_docker_call(
        client.containers.run,
        image,
        **run_kwargs,
    )
    log.info("container.created", id=container.short_id, name=container.name, image=image)
    return {"id": container.short_id, "name": container.name, "status": container.status}


@app.post("/api/containers/{container_id}/start", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def start_container(request: Request, container_id: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.start)
    log.info("container.started", id=container_id)
    return {"ok": True}


@app.post("/api/containers/{container_id}/stop", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def stop_container(request: Request, container_id: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.stop, timeout=CONTAINER_STOP_TIMEOUT)
    log.info("container.stopped", id=container_id)
    return {"ok": True}


@app.post("/api/containers/{container_id}/restart", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def restart_container(request: Request, container_id: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.restart, timeout=CONTAINER_RESTART_TIMEOUT)
    log.info("container.restarted", id=container_id)
    return {"ok": True}


@app.post("/api/containers/{container_id}/pause", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def pause_container(request: Request, container_id: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.pause)
    log.info("container.paused", id=container_id)
    return {"ok": True}


@app.post("/api/containers/{container_id}/unpause", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def unpause_container(request: Request, container_id: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.unpause)
    log.info("container.unpaused", id=container_id)
    return {"ok": True}


@app.post("/api/containers/{container_id}/kill", dependencies=AUTH)
@limiter.limit("20/minute")
def kill_container(request: Request, container_id: str, signal: str = "SIGKILL", client=Depends(docker_client_dep)):
    verify_csrf(request)
    if signal not in ("SIGKILL", "SIGTERM", "SIGINT", "SIGHUP"):
        raise HTTPException(400, "Invalid signal")
    container = _get_container(client, container_id)
    safe_docker_call(container.kill, signal=signal)
    log.info("container.killed", id=container_id, signal=signal)
    return {"ok": True}


@app.post("/api/containers/{container_id}/rename", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def rename_container(request: Request, container_id: str, name: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    validate_container_name(name)
    container = _get_container(client, container_id)
    safe_docker_call(container.rename, name)
    log.info("container.renamed", id=container_id, new_name=name)
    return {"ok": True}


@app.delete("/api/containers/{container_id}", dependencies=AUTH)
@limiter.limit("20/minute")
def delete_container(request: Request, container_id: str, force: bool = False, client=Depends(docker_client_dep)):
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.remove, force=force)
    log.info("container.deleted", id=container_id, force=force)
    return {"ok": True}


@app.get("/api/containers/{container_id}/logs", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def container_logs(
    request: Request,
    container_id: str,
    tail: int = Query(default=200, le=MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp — return logs after this time"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp — return logs before this time"),
    client=Depends(docker_client_dep),
):
    container = _get_container(client, container_id)
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    logs = safe_docker_call(container.logs, **kwargs)
    return {"logs": logs.decode(errors="replace")}


@app.get("/api/containers/{container_id}/logs/download", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def download_container_logs(
    request: Request,
    container_id: str,
    tail: int = Query(default=5000, le=MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
):
    """Download container logs as plain text file. Auth via Authorization header (no query param token)."""
    container = _get_container(client, container_id)
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    logs = safe_docker_call(container.logs, **kwargs)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', container.name)
    return PlainTextResponse(
        content=logs.decode(errors="replace"),
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-logs.txt"'},
    )


@app.get("/api/containers/{container_id}/logs/download.jsonl", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def download_container_logs_jsonl(
    request: Request,
    container_id: str,
    tail: int = Query(default=5000, le=MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
):
    """Download container logs as JSONL (one JSON object per line with timestamp + message)."""
    container = _get_container(client, container_id)
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    logs = safe_docker_call(container.logs, **kwargs)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', container.name)
    lines = []
    for line in logs.decode(errors="replace").splitlines():
        if " " in line:
            ts, _, msg = line.partition(" ")
        else:
            ts, msg = "", line
        lines.append(json.dumps({"timestamp": ts, "message": msg}))
    return PlainTextResponse(
        content="\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-logs.jsonl"'},
    )


@app.get("/api/containers/{container_id}/inspect", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def inspect_container(request: Request, container_id: str, client=Depends(docker_client_dep)):
    container = _get_container(client, container_id)
    attrs = container.attrs
    return {
        "id": attrs["Id"][:12],
        "name": attrs["Name"].lstrip("/"),
        "image": attrs["Config"]["Image"],
        "created": attrs["Created"],
        "state": attrs["State"],
        "restart_count": attrs.get("RestartCount", 0),
        "platform": attrs.get("Platform", ""),
        "config": {
            "env": _redact_env(attrs["Config"].get("Env", [])),
            "cmd": attrs["Config"].get("Cmd"),
            "entrypoint": attrs["Config"].get("Entrypoint"),
            "working_dir": attrs["Config"].get("WorkingDir", ""),
            "labels": _redact_dict(attrs["Config"].get("Labels", {})),
            "hostname": attrs["Config"].get("Hostname", ""),
            "user": attrs["Config"].get("User", ""),
        },
        "host_config": {
            "memory_limit_mb": round(attrs.get("HostConfig", {}).get("Memory", 0) / 1024 / 1024, 1),
            "cpu_shares": attrs.get("HostConfig", {}).get("CpuShares", 0),
            "restart_policy": attrs.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", ""),
            "readonly_rootfs": attrs.get("HostConfig", {}).get("ReadonlyRootfs", False),
            "security_opt": attrs.get("HostConfig", {}).get("SecurityOpt", []),
        },
        "network": {
            name: {"ip": net.get("IPAddress", ""), "gateway": net.get("Gateway", ""), "mac": net.get("MacAddress", "")}
            for name, net in attrs.get("NetworkSettings", {}).get("Networks", {}).items()
        },
        "mounts": [
            {"type": m["Type"], "source": m["Source"], "destination": m["Destination"],
             "rw": m["RW"], "mode": m.get("Mode", "")}
            for m in attrs.get("Mounts", [])
        ],
        "ports": attrs.get("NetworkSettings", {}).get("Ports", {}),
        "health_check": {
            "test": attrs.get("Config", {}).get("Healthcheck", {}).get("Test"),
            "interval_ns": attrs.get("Config", {}).get("Healthcheck", {}).get("Interval", 0),
            "timeout_ns": attrs.get("Config", {}).get("Healthcheck", {}).get("Timeout", 0),
            "retries": attrs.get("Config", {}).get("Healthcheck", {}).get("Retries", 0),
            "status": attrs.get("State", {}).get("Health", {}).get("Status", "none")
            if isinstance(attrs.get("State", {}).get("Health"), dict) else "none",
            "failing_streak": attrs.get("State", {}).get("Health", {}).get("FailingStreak", 0)
            if isinstance(attrs.get("State", {}).get("Health"), dict) else 0,
            "log": (attrs.get("State", {}).get("Health", {}).get("Log") or [])[-3:]
            if isinstance(attrs.get("State", {}).get("Health"), dict) else [],
        },
    }


@app.get("/api/containers/{container_id}/stats", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
async def container_stats(request: Request, container_id: str, client=Depends(docker_client_dep)):
    container = _get_container(client, container_id)
    loop = asyncio.get_running_loop()
    try:
        stats = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: container.stats(stream=False)),
            timeout=CONTAINER_STATS_TIMEOUT,
        )
    except TimeoutError as exc:
        raise HTTPException(504, "Stats request timed out") from exc

    # CPU
    cpu_delta = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) - \
                stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
    system_delta = stats.get("cpu_stats", {}).get("system_cpu_usage", 0) - \
                   stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
    num_cpus = stats.get("cpu_stats", {}).get("online_cpus", 1) or 1
    cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0 if system_delta > 0 else 0.0

    # Memory
    mem_usage = stats.get("memory_stats", {}).get("usage", 0)
    mem_limit = stats.get("memory_stats", {}).get("limit", 1) or 1
    mem_percent = (mem_usage / mem_limit) * 100.0

    # Network (guard against None)
    networks = stats.get("networks") or {}
    net_rx = sum(v.get("rx_bytes", 0) for v in networks.values())
    net_tx = sum(v.get("tx_bytes", 0) for v in networks.values())

    # Block I/O
    blkio = stats.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    disk_read = sum(e.get("value", 0) for e in blkio if str(e.get("op", "")).lower() == "read")
    disk_write = sum(e.get("value", 0) for e in blkio if str(e.get("op", "")).lower() == "write")

    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "memory_limit_mb": round(mem_limit / 1024 / 1024, 1),
        "memory_percent": round(mem_percent, 2),
        "net_rx_mb": round(net_rx / 1024 / 1024, 2),
        "net_tx_mb": round(net_tx / 1024 / 1024, 2),
        "disk_read_mb": round(disk_read / 1024 / 1024, 2),
        "disk_write_mb": round(disk_write / 1024 / 1024, 2),
    }


@app.get("/api/containers/{container_id}/top", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def container_top(request: Request, container_id: str, client=Depends(docker_client_dep)):
    """List processes running inside a container (like docker top)."""
    container = _get_container(client, container_id)
    try:
        top = safe_docker_call(container.top)
    except HTTPException as e:
        if e.status_code == 409:
            raise HTTPException(409, "Container is not running") from e
        raise
    return {"titles": top.get("Titles", []), "processes": top.get("Processes", [])}


@app.get("/api/containers/{container_id}/diff", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def container_diff(request: Request, container_id: str, client=Depends(docker_client_dep)):
    """Show filesystem changes in a container (like docker diff)."""
    container = _get_container(client, container_id)
    diff = safe_docker_call(container.diff)
    if diff is None:
        diff = []
    kind_map = {0: "Modified", 1: "Added", 2: "Deleted"}
    return [{"path": d.get("Path", ""), "kind": kind_map.get(d.get("Kind", 0), "Unknown")} for d in diff[:500]]


# ── Log streaming via WebSocket ────────────────────────────
def _validate_ws_origin(websocket: WebSocket):
    """Validate WebSocket origin against allowed list or same-origin.

    On Cloud Workstations, the browser accesses the app through a proxy
    (e.g. https://8080-xxx.cloudworkstations.dev) which becomes the Origin.
    We allow same-origin: if the origin's host matches the Host header,
    the request came from our own page.
    """
    origin = websocket.headers.get("origin", "")
    if _cfg.allowed_origins == ["*"]:
        return True
    if not origin:
        return False  # Fail closed: reject missing origin
    # Explicit allowlist match
    if origin in _cfg.allowed_origins:
        return True
    # Same-origin check: origin host matches request Host header
    # This handles Cloud Workstation proxy URLs automatically
    try:
        origin_host = urlparse(origin).netloc
        request_host = websocket.headers.get("host", "")
        if origin_host and request_host and origin_host == request_host:
            return True
    except Exception:
        pass
    return False


async def _validate_ws_token_from_message(websocket: WebSocket) -> bool:
    """Validate token sent as first WS message ('AUTH <token>') instead of URL query param.

    Avoids leaking token in proxy/access logs via query string.
    """
    if not _cfg.api_token:
        return True
    try:
        first_msg = await asyncio.wait_for(websocket.receive_text(), timeout=WS_TOKEN_TIMEOUT)
    except Exception:
        return False
    if first_msg.startswith("AUTH "):
        return _constant_time_compare(first_msg[5:], _cfg.api_token)
    return False


@app.websocket("/ws/logs/{container_id}")
async def stream_logs(websocket: WebSocket, container_id: str):
    if not _validate_ws_origin(websocket):
        await websocket.close(code=4003)
        return
    if not CONTAINER_ID_RE.match(container_id):
        await websocket.close(code=4000)
        return
    await websocket.accept()
    if not await _validate_ws_token_from_message(websocket):
        await websocket.close(code=4003)
        return
    ip = websocket.client.host if websocket.client else "unknown"
    _ws_acquire(ip)
    log.info("audit.ws_logs", container=container_id, remote=websocket.client.host if websocket.client else "unknown")
    try:
        loop = asyncio.get_running_loop()
        client = await loop.run_in_executor(None, get_client)
        container = await loop.run_in_executor(None, client.containers.get, container_id)
        # Run blocking log iterator in executor to avoid blocking the event loop
        gen = container.logs(stream=True, follow=True, tail=WS_LOG_TAIL, timestamps=True)

        async def read_logs():
            while True:
                try:
                    line = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: next(gen, None)),
                        timeout=WS_LOG_IDLE_TIMEOUT,
                    )
                    if line is None:
                        break
                    await websocket.send_text(line.decode(errors="replace"))
                except TimeoutError:
                    await websocket.send_text("\n[Idle timeout — no new logs for 5 minutes]\n")
                    break
                except StopIteration:
                    break

        # Run log reader and also listen for client disconnect
        read_task = asyncio.create_task(read_logs())
        async def _ws_keepalive():
            while True:
                await asyncio.sleep(WS_KEEPALIVE_INTERVAL)
                try:
                    await websocket.send_text("\x00")
                except Exception:
                    break
        keepalive_task = asyncio.create_task(_ws_keepalive())
        try:
            while True:
                await websocket.receive_text()  # just wait for disconnect
        except Exception:
            pass
        finally:
            read_task.cancel()
            keepalive_task.cancel()
            # Close the blocking log generator to free the HTTP connection
            try:
                gen.close()
            except Exception:
                pass
    except Exception as exc:
        log.warning("ws.logs_error", container=container_id, error=str(exc))
    finally:
        _ws_release(ip)
        try:
            await websocket.close()
        except Exception:
            pass


# ── Shell via WebSocket ────────────────────────────────────
@app.websocket("/ws/exec/{container_id}")
async def exec_shell(websocket: WebSocket, container_id: str):
    if not _validate_ws_origin(websocket):
        await websocket.close(code=4003)
        return
    if not CONTAINER_ID_RE.match(container_id):
        await websocket.close(code=4000)
        return
    await websocket.accept()
    if not await _validate_ws_token_from_message(websocket):
        await websocket.close(code=4003)
        return
    ip = websocket.client.host if websocket.client else "unknown"
    _ws_acquire(ip)
    log.info("audit.ws_exec", container=container_id, remote=websocket.client.host if websocket.client else "unknown")
    try:
        loop = asyncio.get_running_loop()
        client = await loop.run_in_executor(None, get_client)
        container = await loop.run_in_executor(None, client.containers.get, container_id)
        shell = "/bin/sh"
        try:
            exit_code, _ = container.exec_run("which /bin/bash", demux=True)
            if exit_code == 0:
                shell = "/bin/bash"
        except Exception:
            pass
        exec_id = client.api.exec_create(container.id, shell, stdin=True, tty=True, stdout=True, stderr=True)
        sock = client.api.exec_start(exec_id, socket=True, tty=True)
        # Use blocking socket with a short timeout so recv yields control periodically
        sock._sock.setblocking(True)
        sock._sock.settimeout(WS_EXEC_RECV_TIMEOUT)

        async def read_output():
            idle_since = time.monotonic()
            while True:
                try:
                    data = await loop.run_in_executor(None, sock._sock.recv, 4096)
                    if not data:
                        break
                    idle_since = time.monotonic()
                    await websocket.send_text(data.decode(errors="replace"))
                except TimeoutError:
                    # Socket timeout — no data, check idle limit
                    if time.monotonic() - idle_since > WS_EXEC_IDLE_TIMEOUT:
                        await websocket.send_text("\r\n[Session idle timeout — 10 minutes]\r\n")
                        break
                    continue
                except Exception:
                    break

        read_task = asyncio.create_task(read_output())
        async def _ws_keepalive():
            while True:
                await asyncio.sleep(WS_KEEPALIVE_INTERVAL)
                try:
                    await websocket.send_text("\x00")
                except Exception:
                    break
        keepalive_task = asyncio.create_task(_ws_keepalive())
        try:
            while True:
                data = await websocket.receive_text()
                if len(data) > 65536:
                    await websocket.close(code=4008)
                    break
                await loop.run_in_executor(None, sock._sock.sendall, data.encode())
        except Exception:
            pass
        finally:
            read_task.cancel()
            keepalive_task.cancel()
            sock.close()
    except Exception as exc:
        log.warning("ws.exec_error", container=container_id, error=str(exc))
    finally:
        _ws_release(ip)
        try:
            await websocket.close()
        except Exception:
            pass


# ── Images ─────────────────────────────────────────────────
@app.get("/api/images", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def list_images(request: Request, client=Depends(docker_client_dep)):
    images = safe_docker_call(client.images.list)
    result = []
    for img in images:
        tags = img.tags or [img.short_id]
        size_mb = round(img.attrs["Size"] / 1024 / 1024, 1)
        created = img.attrs.get("Created", "")
        result.extend(
            {"tag": tag, "id": img.short_id, "size_mb": size_mb, "created": created}
            for tag in tags
        )
    return result


@app.get("/api/images/allowed", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def list_allowed_images(request: Request, client=Depends(docker_client_dep)):
    images = safe_docker_call(client.images.list)
    result = []
    for img in images:
        for tag in (img.tags or []):
            tag_parts = tag.split(":")[0].split("/")
            tag_registry = tag_parts[0] if len(tag_parts) >= 2 and ("." in tag_parts[0] or ":" in tag_parts[0]) else ""
            if tag_registry and any(
                tag_registry == r.rstrip("/") or tag.startswith(r if r.endswith("/") else r + "/")
                for r in _cfg.allowed_registries
            ):
                result.append({"tag": tag, "id": img.short_id, "size_mb": round(img.attrs["Size"] / 1024 / 1024, 1)})
    return result


@app.post("/api/images/pull", dependencies=AUTH)
@limiter.limit(_limit("5/minute"))
async def pull_image(request: Request, image: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    validate_image_registry(image)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.images.pull(image)),
            timeout=IMAGE_PULL_TIMEOUT,
        )
        log.info("image.pulled", image=image)
        return {"ok": True, "image": image}
    except TimeoutError as exc:
        raise HTTPException(504, "Image pull timed out (5 min limit)") from exc
    except docker.errors.APIError as e:
        raise HTTPException(400, str(e.explanation or "Failed to pull image")[:500]) from e


@app.post("/api/images/{image_id}/tag", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def tag_image(request: Request, image_id: str, repository: str, tag: str = "latest", client=Depends(docker_client_dep)):
    verify_csrf(request)
    validate_image_id(image_id)
    validate_image_registry(repository + ":" + tag)
    img = safe_docker_call(client.images.get, image_id)
    safe_docker_call(img.tag, repository, tag=tag)
    log.info("image.tagged", id=image_id, repo=repository, tag=tag)
    return {"ok": True}


@app.post("/api/images/push", dependencies=AUTH)
@limiter.limit("3/minute")
async def push_image(request: Request, image: str, client=Depends(docker_client_dep)):
    """Push an image to an allowed registry (GCP Artifact Registry)."""
    verify_csrf(request)
    validate_image_registry(image)
    loop = asyncio.get_running_loop()
    try:
        # Docker SDK push returns a generator of JSON status lines
        output = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: client.images.push(image, stream=False)),
            timeout=600.0,  # 10 min for large images
        )
        # Parse newline-delimited JSON output for error messages
        if isinstance(output, str):
            for line in output.strip().splitlines():
                try:
                    msg = json.loads(line)
                    if "error" in msg:
                        raise docker.errors.APIError(msg["error"])
                except (ValueError, TypeError):
                    pass
        log.info("image.pushed", image=image)
        return {"ok": True, "image": image}
    except TimeoutError as exc:
        raise HTTPException(504, "Image push timed out (10 min limit)") from exc
    except docker.errors.APIError as e:
        raise HTTPException(400, str(e.explanation or "Failed to push image")[:500]) from e


@app.delete("/api/images/{image_id}", dependencies=AUTH)
@limiter.limit("20/minute")
def delete_image(request: Request, image_id: str, force: bool = False, client=Depends(docker_client_dep)):
    verify_csrf(request)
    validate_image_id(image_id)
    safe_docker_call(client.images.remove, image_id, force=force)
    log.info("image.deleted", id=image_id, force=force)
    return {"ok": True}


@app.get("/api/images/{image_id}/inspect", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def inspect_image(request: Request, image_id: str, client=Depends(docker_client_dep)):
    validate_image_id(image_id)
    img = safe_docker_call(client.images.get, image_id)
    attrs = img.attrs
    history = []
    try:
        history.extend(
            {
                "created": layer.get("Created", ""),
                "created_by": layer.get("CreatedBy", ""),
                "size_mb": round(layer.get("Size", 0) / 1024 / 1024, 2),
            }
            for layer in img.history()
        )
    except Exception:
        pass
    return {
        "id": attrs["Id"][:19],
        "tags": img.tags,
        "size_mb": round(attrs["Size"] / 1024 / 1024, 1),
        "created": attrs.get("Created", ""),
        "architecture": attrs.get("Architecture", ""),
        "os": attrs.get("Os", ""),
        "layers": len(attrs.get("RootFS", {}).get("Layers", [])),
        "config": {
            "env": attrs.get("Config", {}).get("Env", []),
            "cmd": attrs.get("Config", {}).get("Cmd"),
            "entrypoint": attrs.get("Config", {}).get("Entrypoint"),
            "exposed_ports": list(attrs.get("Config", {}).get("ExposedPorts", {}).keys()),
            "labels": _redact_dict(attrs.get("Config", {}).get("Labels", {})),
            "working_dir": attrs.get("Config", {}).get("WorkingDir", ""),
            "user": attrs.get("Config", {}).get("User", ""),
        },
        "history": history[:20],
    }


# ── Volumes ────────────────────────────────────────────────
@app.get("/api/volumes", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def list_volumes(request: Request, client=Depends(docker_client_dep)):
    volumes = safe_docker_call(client.volumes.list)
    # Build volume->containers lookup in one pass (avoid N+1)
    vol_containers: dict[str, list[str]] = {}
    try:
        for c in client.containers.list(all=True):
            for m in c.attrs.get("Mounts", []):
                vol_name = m.get("Name")
                if vol_name:
                    vol_containers.setdefault(vol_name, []).append(c.name)
    except Exception:
        pass
    result = []
    for v in volumes:
        containers_using = vol_containers.get(v.name, [])
        result.append({
            "name": v.name,
            "driver": v.attrs.get("Driver", ""),
            "mountpoint": v.attrs.get("Mountpoint", ""),
            "created": v.attrs.get("CreatedAt", ""),
            "labels": v.attrs.get("Labels") or {},
            "in_use": len(containers_using) > 0,
            "containers": containers_using,
        })
    return result


@app.post("/api/volumes/create", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def create_volume(request: Request, name: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", name):
        raise HTTPException(400, "Invalid volume name")
    vol = safe_docker_call(client.volumes.create, name=name)
    log.info("volume.created", name=name)
    return {"name": vol.name}


@app.delete("/api/volumes/{volume_name}", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def delete_volume(request: Request, volume_name: str, force: bool = False, client=Depends(docker_client_dep)):
    verify_csrf(request)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", volume_name):
        raise HTTPException(400, "Invalid volume name")
    vol = safe_docker_call(client.volumes.get, volume_name)
    safe_docker_call(vol.remove, force=force)
    log.info("volume.deleted", name=volume_name)
    return {"ok": True}


@app.post("/api/volumes/prune", dependencies=AUTH)
@limiter.limit("3/minute")
def prune_volumes(request: Request, client=Depends(docker_client_dep)):
    verify_csrf(request)
    result = safe_docker_call(client.volumes.prune)
    deleted = result.get("VolumesDeleted") or []
    log.info("volumes.pruned", count=len(deleted))
    return {"deleted": deleted, "space_reclaimed_mb": round(result.get("SpaceReclaimed", 0) / 1024 / 1024, 1)}


# ── Networks ───────────────────────────────────────────────
@app.get("/api/networks", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def list_networks(request: Request, client=Depends(docker_client_dep)):
    networks = safe_docker_call(client.networks.list)
    return [
        {
            "id": n.short_id,
            "name": n.name,
            "driver": n.attrs.get("Driver", ""),
            "scope": n.attrs.get("Scope", ""),
            "internal": n.attrs.get("Internal", False),
            "ipam": n.attrs.get("IPAM", {}).get("Config", []),
            "containers": {
                cid[:12]: info.get("Name", "")
                for cid, info in (n.attrs.get("Containers") or {}).items()
            },
        }
        for n in networks
    ]


@app.post("/api/networks/create", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def create_network(request: Request, name: str, driver: str = "bridge", client=Depends(docker_client_dep)):
    verify_csrf(request)
    if not NETWORK_NAME_RE.match(name):
        raise HTTPException(400, "Invalid network name")
    if driver not in ("bridge", "overlay", "macvlan", "none"):
        raise HTTPException(400, "Invalid network driver")
    net = safe_docker_call(client.networks.create, name, driver=driver)
    log.info("network.created", name=name, driver=driver)
    return {"id": net.short_id, "name": name}


@app.delete("/api/networks/{network_id}", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def delete_network(request: Request, network_id: str, client=Depends(docker_client_dep)):
    verify_csrf(request)
    if not re.match(r"^[a-f0-9]{4,64}$", network_id):
        raise HTTPException(400, "Invalid network ID")
    net = safe_docker_call(client.networks.get, network_id)
    if net.name in ("bridge", "host", "none"):
        raise HTTPException(400, "Cannot delete default network")
    safe_docker_call(net.remove)
    log.info("network.deleted", id=network_id)
    return {"ok": True}


@app.post("/api/networks/{network_id}/connect", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def connect_container_to_network(
    request: Request, network_id: str, container_id: str, client=Depends(docker_client_dep)
):
    verify_csrf(request)
    if not re.match(r"^[a-f0-9]{4,64}$", network_id):
        raise HTTPException(400, "Invalid network ID")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.connect, container)
    log.info("network.connect", network=network_id, container=container_id)
    return {"ok": True}


@app.post("/api/networks/{network_id}/disconnect", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def disconnect_container_from_network(
    request: Request, network_id: str, container_id: str, client=Depends(docker_client_dep)
):
    verify_csrf(request)
    if not re.match(r"^[a-f0-9]{4,64}$", network_id):
        raise HTTPException(400, "Invalid network ID")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.disconnect, container)
    log.info("network.disconnect", network=network_id, container=container_id)
    return {"ok": True}


@app.post("/api/networks/prune", dependencies=AUTH)
@limiter.limit("3/minute")
def prune_networks(request: Request, client=Depends(docker_client_dep)):
    verify_csrf(request)
    result = safe_docker_call(client.networks.prune)
    deleted = result.get("NetworksDeleted") or []
    log.info("networks.pruned", count=len(deleted))
    return {"deleted": deleted}


# ── Compose ────────────────────────────────────────────────
@app.get("/api/compose/stacks", dependencies=AUTH)
@limiter.limit(_limit(RL_DEFAULT))
def list_compose_stacks(request: Request, client=Depends(docker_client_dep)):
    """List running compose stacks by inspecting container labels."""
    containers = safe_docker_call(client.containers.list, all=True)
    stacks = {}
    for c in containers:
        project = (c.labels or {}).get("com.docker.compose.project", "")
        if not project:
            continue
        if project not in stacks:
            stacks[project] = {"name": project, "services": [], "status": "stopped"}
        service = (c.labels or {}).get("com.docker.compose.service", "")
        svc_state = c.attrs.get("State", {}).get("Status", "unknown")
        stacks[project]["services"].append({
            "name": service,
            "container_id": c.short_id,
            "status": c.status,
            "state": svc_state,
        })
        if svc_state == "running":
            stacks[project]["status"] = "running"
    return list(stacks.values())


_ENV_SENSITIVE_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|KEY|CREDENTIAL|AUTH|CERT|PRIVATE|API_KEY)",
    re.IGNORECASE,
)


def _redact_env(env_list: list[str]) -> list[str]:
    """Redact environment variable values whose names suggest sensitive data."""
    redacted = []
    for entry in env_list:
        name, sep, _ = entry.partition("=")
        if sep and _ENV_SENSITIVE_RE.search(name):
            redacted.append(f"{name}=[REDACTED]")
        else:
            redacted.append(entry)
    return redacted


def _redact_dict(d: dict) -> dict:
    """Recursively redact sensitive string values from a dict (labels, env mappings, etc.)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and _ENV_SENSITIVE_RE.search(str(k)):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif isinstance(v, list):
            out[k] = _redact_env(v) if all(isinstance(i, str) for i in v) else v
        else:
            out[k] = v
    return out


def _sanitize_stderr(stderr: str) -> str:
    """Strip internal paths and hostnames from subprocess error output before returning to client."""
    # Remove absolute paths (any length)
    sanitized = re.sub(r'(/[^\s:,\'"]+)', '[path]', stderr)
    # Remove hostnames embedded in error messages (word.word patterns)
    sanitized = re.sub(r'\b([a-zA-Z0-9-]+\.){2,}[a-zA-Z]{2,}\b', '[host]', sanitized)
    return sanitized[:400].strip()


@app.post("/api/compose/up", dependencies=AUTH)
@limiter.limit(_limit("5/minute"))
def compose_up(request: Request, file: UploadFile | None = None, project_name: str = "dev"):
    verify_csrf(request)
    validate_project_name(project_name)

    COMPOSE_DIR.mkdir(parents=True, exist_ok=True)
    project_dir = COMPOSE_DIR / project_name
    project_dir.mkdir(exist_ok=True)
    # Prevent symlink-based path traversal
    if not project_dir.resolve().is_relative_to(COMPOSE_DIR.resolve()):
        raise HTTPException(400, "Invalid project directory")
    compose_path = project_dir / "docker-compose.yml"

    if file and file.filename:
        content = file.file.read()
        parsed = validate_compose_file(content)
        # Audit: log compose structure with sensitive env values redacted
        services_summary = {
            svc: {"image": cfg.get("image", ""), "env_keys": list(cfg.get("environment", {}).keys())}
            for svc, cfg in (parsed.get("services") or {}).items()
            if isinstance(cfg, dict)
        }
        log.info("compose.upload", project=project_name, services=services_summary)
        compose_path.write_bytes(content)
    elif not compose_path.exists():
        raise HTTPException(400, "No compose file uploaded and no existing file found for this project")
    else:
        # Re-validate existing file on restart (defense against manual tampering)
        validate_compose_file(compose_path.read_bytes())

    minimal_env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "DOCKER_HOST": _cfg.docker_host,
        "HOME": os.environ.get("HOME", "/root"),
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
    }
    # Remove empty values
    minimal_env = {k: v for k, v in minimal_env.items() if v}
    try:
        result = subprocess.run(
            [DOCKER_BIN, "compose", "-f", str(compose_path), "-p", project_name, "up", "-d"],
            capture_output=True, text=True, env=minimal_env, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Compose up timed out (2 min limit)") from exc
    if result.returncode != 0:
        log.warning("compose.up_failed", project=project_name, stderr=result.stderr[:500])
        detail = _sanitize_stderr(result.stderr) if result.stderr \
            else "Compose deployment failed. Check compose file syntax and image availability."
        raise HTTPException(400, detail)
    log.info("compose.up", project=project_name)
    return {"ok": True, "output": result.stdout}


@app.post("/api/compose/down", dependencies=AUTH)
@limiter.limit(_limit("5/minute"))
def compose_down(request: Request, project_name: str = "dev"):
    verify_csrf(request)
    validate_project_name(project_name)
    minimal_env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "DOCKER_HOST": _cfg.docker_host,
        "HOME": os.environ.get("HOME", "/root"),
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
    }
    minimal_env = {k: v for k, v in minimal_env.items() if v}
    try:
        result = subprocess.run(
            [DOCKER_BIN, "compose", "-p", project_name, "down"],
            capture_output=True, text=True, env=minimal_env, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Compose down timed out") from exc
    if result.returncode != 0:
        log.warning("compose.down_failed", project=project_name, stderr=result.stderr[:500])
        detail = _sanitize_stderr(result.stderr) if result.stderr else "Compose teardown failed"
        raise HTTPException(400, detail)
    log.info("compose.down", project=project_name)
    return {"ok": True, "output": result.stdout}


# ── System ─────────────────────────────────────────────────
@app.get("/api/system/info", dependencies=AUTH)
@limiter.limit(_limit(RL_SLOW))
def system_info(request: Request, client=Depends(docker_client_dep)):
    info = safe_docker_call(client.info)
    ver = safe_docker_call(client.version)
    return {
        "docker_version": info.get("ServerVersion", ""),
        "api_version": ver.get("ApiVersion", ""),
        "os": info.get("OperatingSystem", ""),
        "os_type": info.get("OSType", ""),
        "architecture": info.get("Architecture", ""),
        "kernel": info.get("KernelVersion", ""),
        "cpus": info.get("NCPU", 0),
        "memory_gb": round(info.get("MemTotal", 0) / 1024 / 1024 / 1024, 1),
        "containers": info.get("Containers", 0),
        "containers_running": info.get("ContainersRunning", 0),
        "containers_paused": info.get("ContainersPaused", 0),
        "containers_stopped": info.get("ContainersStopped", 0),
        "images": info.get("Images", 0),
        "storage_driver": info.get("Driver", ""),
        "logging_driver": info.get("LoggingDriver", ""),
        "cgroup_driver": info.get("CgroupDriver", ""),
        "docker_root_dir": info.get("DockerRootDir", ""),
        "security_options": info.get("SecurityOptions", []),
        "registries": info.get("RegistryConfig", {}).get("IndexConfigs", {}),
    }


@app.get("/api/system/df", dependencies=AUTH)
@limiter.limit(_limit("5/minute"))
def system_disk_usage(request: Request, client=Depends(docker_client_dep)):
    df = safe_docker_call(client.df)
    images = df.get("Images") or []
    containers = df.get("Containers") or []
    volumes = df.get("Volumes") or []
    build_cache = df.get("BuildCache") or []
    image_size = sum(i.get("Size", 0) for i in images)
    image_reclaimable = sum(i.get("Size", 0) for i in images if i.get("Containers", 0) == 0)
    container_size = sum(c.get("SizeRw", 0) for c in containers)
    volume_size = sum(v.get("UsageData", {}).get("Size", 0) for v in volumes)
    volume_reclaimable = sum(
        v.get("UsageData", {}).get("Size", 0) for v in volumes if v.get("UsageData", {}).get("RefCount", 0) == 0
    )
    build_cache_size = sum(b.get("Size", 0) for b in build_cache)
    build_cache_reclaimable = sum(b.get("Size", 0) for b in build_cache if not b.get("InUse", False))
    total = image_size + container_size + volume_size + build_cache_size
    return {
        "images_mb": round(image_size / 1024 / 1024, 1),
        "images_reclaimable_mb": round(image_reclaimable / 1024 / 1024, 1),
        "images_count": len(images),
        "containers_mb": round(container_size / 1024 / 1024, 1),
        "containers_count": len(containers),
        "volumes_mb": round(volume_size / 1024 / 1024, 1),
        "volumes_reclaimable_mb": round(volume_reclaimable / 1024 / 1024, 1),
        "volumes_count": len(volumes),
        "build_cache_mb": round(build_cache_size / 1024 / 1024, 1),
        "build_cache_reclaimable_mb": round(build_cache_reclaimable / 1024 / 1024, 1),
        "total_mb": round(total / 1024 / 1024, 1),
    }


@app.post("/api/system/prune", dependencies=AUTH)
@limiter.limit("2/minute")
def system_prune(request: Request, client=Depends(docker_client_dep)):
    verify_csrf(request)
    containers = safe_docker_call(client.containers.prune)
    images = safe_docker_call(client.images.prune)
    networks = safe_docker_call(client.networks.prune)
    log.info("system.pruned",
             containers=len(containers.get("ContainersDeleted") or []),
             images=len(images.get("ImagesDeleted") or []),
             networks=len(networks.get("NetworksDeleted") or []))
    return {
        "containers_deleted": len(containers.get("ContainersDeleted") or []),
        "images_deleted": len(images.get("ImagesDeleted") or []),
        "networks_deleted": len(networks.get("NetworksDeleted") or []),
        "space_reclaimed_mb": round(
            (containers.get("SpaceReclaimed", 0) + images.get("SpaceReclaimed", 0)) / 1024 / 1024, 1,
        ),
    }


@app.post("/api/system/prune-build-cache", dependencies=AUTH)
@limiter.limit("2/minute")
def prune_build_cache(request: Request, client=Depends(docker_client_dep)):
    verify_csrf(request)
    result = safe_docker_call(client.api.prune_builds)
    space = result.get("SpaceReclaimed", 0)
    log.info("build_cache.pruned", space_mb=round(space / 1024 / 1024, 1))
    return {"space_reclaimed_mb": round(space / 1024 / 1024, 1)}


@app.get("/api/system/audit-log", dependencies=[Depends(verify_auth_strict)])
@limiter.limit("20/minute")
def get_audit_log(request: Request, tail: int = Query(default=200, le=MAX_AUDIT_LINES, ge=1)):
    """Return the last N lines of the app audit log."""
    if not AUDIT_LOG_PATH.exists():
        return []
    lines = AUDIT_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    result = []
    for raw_line in lines[-tail:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            result.append(json.loads(stripped))
        except json.JSONDecodeError:
            result.append({"raw": stripped})
    return result


@app.get("/api/system/audit-log/download", dependencies=[Depends(verify_auth_strict)])
@limiter.limit(_limit(RL_SLOW))
def download_audit_log(request: Request):
    """Download the full audit log as a JSONL file (streamed to avoid memory spikes)."""
    if not AUDIT_LOG_PATH.exists():
        return PlainTextResponse("", media_type="application/x-ndjson",
                                 headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'})
    return FileResponse(
        path=str(AUDIT_LOG_PATH),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'},
    )


# ── Frontend ───────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/LICENSE")
def license_file():
    return FileResponse(_LICENSE_FILE, media_type="text/plain")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _main():
    """Entrypoint for `pip install` / `skiff` CLI command."""
    import uvicorn
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("skiff.app:app", host=host, port=port, workers=1, log_level="warning")


if __name__ == "__main__":
    _main()
