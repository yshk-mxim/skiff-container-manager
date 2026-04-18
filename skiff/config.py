# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Runtime configuration, application constants, and rate-limiter setup.

Imported by every other skiff module — must not import from any other skiff module
to avoid circular-import chains.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slowapi import Limiter
from slowapi.util import get_remote_address

# ── TOML-backed data (R2a) ─────────────────────────────────
# Static configuration data lives in `skiff/_config/*.toml`, shipped as
# package data so `pip install` works without a separate data-files dir.
# Loaded here once at import time via stdlib tomllib (Python 3.12+).
# Operators can edit these files without changing code; tests point
# CONFIG_DIR at a fixture directory to substitute.
#
# TOML is authoritative — if a file is missing we raise rather than
# silently drift to embedded defaults.
_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(Path(__file__).parent / "_config")))


def _load_toml(name: str) -> dict[str, Any]:
    """Load `skiff/_config/<name>.toml`. Raises if the file is absent.

    CONFIG_DIR may be overridden (tests, packaged installs); if the
    override points somewhere that lacks the file, that's a deployment
    error and we fail fast rather than silently drift to embedded
    defaults.
    """
    p = _CONFIG_DIR / f"{name}.toml"
    if not p.exists():
        raise FileNotFoundError(
            f"Required config file {p} is missing. Either reinstall the package "
            f"or set CONFIG_DIR to a tree containing {name}.toml.",
        )
    with p.open("rb") as fh:
        return tomllib.load(fh)


_TOML_RATE = _load_toml("rate")
_TOML_PROFILES = _load_toml("profiles")
_TOML_TMPFS = _load_toml("tmpfs")
_TOML_SECURITY_HEADERS = _load_toml("security_headers")
_TOML_NETWORKS = _load_toml("networks")
_TOML_DOCKER_PROBE = _load_toml("docker_probe")
_TOML_SSH_TUNNEL = _load_toml("ssh_tunnel")
_TOML_COMPOSE_SANDBOX = _load_toml("compose_sandbox")
_TOML_MOUNT_TARGETS = _load_toml("mount_targets")
_TOML_CONNECT_SNIPPETS = _load_toml("connect_snippets")
_TOML_DEFAULTS = _load_toml("defaults")


def _knob_default(name: str) -> str:
    """Return the TOML-sourced default for a knob, as a string.

    `config_knob(default=...)` expects a string (it applies the validator
    on read); TOML gives us the value already typed. `str(v)` on int /
    float / bool produces the canonical form the int / float validators
    round-trip without loss. A knob absent from `defaults.toml` raises —
    the caller is expected to register every knob it uses in the TOML.
    """
    val = _TOML_DEFAULTS.get(name)
    if val is None:
        raise KeyError(
            f"config.{name} has no entry in skiff/_config/defaults.toml — add one, "
            f"or pass `default=` inline if the value is genuinely Python-only.",
        )
    return str(val)

# ── config_knob factory ────────────────────────────────────
# A knob is one env var whose value, default, validator, and doc live
# in ONE call site. Each `config_knob(...)` also registers itself in
# `_KNOBS` so `docs/config-knobs.md` can be generated from the registry,
# and `/api/config` can surface only the knobs with `expose=True` (the
# opt-in guards against accidental env leakage through introspection).

@dataclass(frozen=True)
class _KnobSpec:
    name: str
    default: str | None
    validator: Callable[[str], Any] | None
    doc: str
    expose: bool                  # surface via /api/config?
    secret: bool                  # redact value in audit/config dumps?


_KNOBS: dict[str, _KnobSpec] = {}


def config_knob(
    name: str,
    *,
    default: str | None = None,
    validator: Callable[[str], Any] | None = None,
    doc: str = "",
    expose: bool = False,
    secret: bool = False,
) -> Any:
    """Read one environment knob, register it in _KNOBS, return the parsed value.

    Example:
        BIND_HOST = config_knob(
            "BIND_HOST",
            default="127.0.0.1",
            doc="Interface uvicorn binds to. Override only for explicit remote serve.",
        )

    Calling this twice for the same name raises — tests rely on the registry
    being unique, so a typo in `name` surfaces as a loud failure.
    """
    if name in _KNOBS:
        raise ValueError(f"config_knob({name!r}) already registered")
    raw = os.environ.get(name, default) if default is not None else os.environ.get(name)
    _KNOBS[name] = _KnobSpec(
        name=name, default=default, validator=validator, doc=doc,
        expose=expose, secret=secret,
    )
    if raw is None:
        return None
    if validator is not None:
        return validator(raw)
    return raw


def knobs() -> dict[str, _KnobSpec]:
    """Return an immutable view of the knob registry for tests / docs gen."""
    return dict(_KNOBS)

# ── Package paths ──────────────────────────────────────────
_PKG_DIR = Path(__file__).parent
_STATIC_DIR = _PKG_DIR / "static"
_LICENSE_FILE = _PKG_DIR.parent / "LICENSE"
# Cache at import time — avoids thread-pool contention from anyio file I/O
# under heavy API load (FileResponse uses a thread even for async handlers on macOS).
_INDEX_HTML: bytes = (_STATIC_DIR / "index.html").read_bytes()

# ── Runtime configuration ──────────────────────────────────
def _csv_list(raw: str) -> list[str]:
    """Split a comma-separated string into a list of non-empty stripped entries."""
    return [x.strip() for x in raw.split(",") if x.strip()]


def _csv_list_no_wildcard(raw: str) -> list[str]:
    """Like _csv_list but rejects '*' entries (disables CSRF if applied to origins)."""
    items = _csv_list(raw)
    if "*" in items:
        raise ValueError(
            "ALLOWED_ORIGINS must not contain '*' — this disables CSRF protections. "
            "Set it to the exact origin(s) of your browser client, e.g. http://127.0.0.1:8080",
        )
    return items


# RFC 1123-ish hostname pattern with optional IPv4 literal. Accepts only
# characters safe to interpolate into an anchor href's host segment — no
# '/', no '@', no '#', no '?'. An operator who sets DOCKER_VM_HOST to a
# value like "attacker.com/#@trusted" gets a fail-fast ValueError at
# startup rather than a phishable port-link rendered in the UI.
_HOSTNAME_RE = re.compile(
    r"^(?:[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(?:\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$",
)


def _validate_hostname(raw: str) -> str:
    """Hostname validator for display-only knobs interpolated into UI hrefs."""
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) > 253 or not _HOSTNAME_RE.match(s):
        raise ValueError(
            f"Invalid hostname {s!r} — expected a bare hostname or IPv4 literal, "
            "no scheme / path / '@' / '#' / '?'. Display-only interpolation cannot "
            "accept a value that could smuggle URL components.",
        )
    return s


class _Config:
    """Mutable runtime configuration.

    Populated from env-via-config_knob on startup; can be updated at runtime
    via /api/setup when running without a pre-configured environment. Stays
    mutable because the setup endpoint writes back (api_token rotation,
    docker_host discovery, etc.).
    """

    def __init__(self) -> None:
        self.docker_host: str = config_knob(
            "DOCKER_HOST", default="unix:///var/run/docker.sock",
            doc="Docker daemon socket or TCP URL. Override for remote hosts or Colima.",
            expose=True,
        )
        # Default registry allowlist — docker.io,ghcr.io covers the common
        # open-source case. For GCP Artifact Registry set
        # ALLOWED_REGISTRIES=us-docker.pkg.dev/my-project/
        self.allowed_registries: list[str] = config_knob(
            "ALLOWED_REGISTRIES", default="docker.io,ghcr.io", validator=_csv_list,
            doc="Comma-separated image-registry allowlist. Pull/push reject images outside this list.",
            expose=True,
        )
        self.api_token: str = config_knob(
            "API_TOKEN", default="",
            doc="Shared bearer token for every authenticated request. Empty ⇒ no auth (localhost-only).",
            secret=True,
        )
        self.allowed_origins: list[str] = config_knob(
            "ALLOWED_ORIGINS", default="http://127.0.0.1:8080",
            validator=_csv_list_no_wildcard,
            doc="Comma-separated browser origin allowlist. Must exactly match the UI origin.",
            expose=True,
        )
        self.docker_vm_host: str = config_knob(
            "DOCKER_VM_HOST", default="",
            validator=_validate_hostname,
            doc="Optional hostname shown in audit log lines when Docker is remote (display only).",
            expose=True,
        )
        # True when config came from env with a non-empty token — setup endpoint disabled
        self.from_env: bool = bool(self.api_token.strip())
        # Tri-state: env var was set but empty (neither unset nor usable). The
        # default str knob can't represent this — os.environ is the only
        # source of truth. Centralized here so `skiff/app.py` stays env-free.
        raw = os.environ.get("API_TOKEN")
        self.api_token_set_but_empty: bool = raw is not None and not raw.strip()


_cfg = _Config()

# ── Filesystem / paths ─────────────────────────────────────
# Per-user state root. Picks the right location across platforms without pulling
# in platformdirs: XDG_STATE_HOME if set, else macOS-style Application Support,
# else ~/.local/state. Falls back to the current working directory only when
# HOME is unset — safe default without requiring root or a pre-provisioned /data
# or /var/log. Production operators override AUDIT_LOG / COMPOSE_DIR explicitly
# (see docs/hardening/production.md §6 / §Metrics).
def _user_state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "skiff"
    home = os.environ.get("HOME", "").strip()
    if not home:
        return Path.cwd() / ".skiff"
    if sys.platform == "darwin":
        # Apple HIG puts user state under Application Support
        return Path(home) / "Library" / "Application Support" / "skiff"
    return Path(home) / ".local" / "state" / "skiff"


_STATE_ROOT = _user_state_root()
COMPOSE_DIR = Path(config_knob(
    "COMPOSE_DIR", default=str(_STATE_ROOT / "compose"),
    doc="Directory where uploaded docker-compose.yml files are stored (one subdir per project).",
    expose=True,
))
DOCKER_BIN = shutil.which("docker") or "/usr/bin/docker"
AUDIT_LOG_PATH = Path(config_knob(
    "AUDIT_LOG", default=str(_STATE_ROOT / "audit.jsonl"),
    doc="Path to the rotating JSON-lines audit log. Set to /dev/null to disable file logging.",
    expose=True,
))


_COMPOSE_CMD_CACHE: list[str] | None = None


def _compose_cmd_probe() -> list[str]:
    """Detect the compose command prefix (v2 plugin preferred, v1 standalone fallback).

    Shells out to `docker compose version` with a 5-second timeout and
    caches the result. Lazy on purpose: running it at import time would
    add a 5-second-per-import tax that only the compose routes would
    ever benefit from.
    """
    global _COMPOSE_CMD_CACHE
    if _COMPOSE_CMD_CACHE is not None:
        return _COMPOSE_CMD_CACHE
    if _compose_plugin_available(DOCKER_BIN):
        _COMPOSE_CMD_CACHE = [DOCKER_BIN, "compose"]
        return _COMPOSE_CMD_CACHE
    standalone = shutil.which("docker-compose")
    _COMPOSE_CMD_CACHE = [standalone] if standalone else [DOCKER_BIN, "compose"]
    return _COMPOSE_CMD_CACHE


def _compose_plugin_available(docker_bin: str) -> bool:
    """Probe `docker compose version` — True if the subcommand exits 0.

    A missing binary / failed probe returns False without raising — the
    startup path falls through to the docker-compose standalone lookup.
    """
    try:
        r = subprocess.run(
            [docker_bin, "compose", "version"],
            capture_output=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return r.returncode == 0


class _ComposeCmd:
    """Lazy-evaluated `COMPOSE_CMD` list that probes on first iteration.

    Callers write `list(config.COMPOSE_CMD)` or splat as `*config.COMPOSE_CMD`
    the same way they always have; the probe runs when iteration forces it.
    """

    def __iter__(self):
        return iter(_compose_cmd_probe())

    def __getitem__(self, i):
        return _compose_cmd_probe()[i]

    def __len__(self):
        return len(_compose_cmd_probe())

    def __repr__(self):
        return repr(_compose_cmd_probe())


COMPOSE_CMD: _ComposeCmd = _ComposeCmd()


# ── Runtime-tunable environment knobs ─────────────────────
# Every env var the server reads flows through `config_knob()` so the
# knob's name, default, validator, and doc live in ONE place (the call
# below). _KNOBS is queryable at runtime for /api/config, docs gen,
# and tests. A typo in the name raises at import because the registry
# disallows duplicate registration.
BIND_HOST = config_knob(
    "BIND_HOST",
    default="127.0.0.1",
    doc="Interface uvicorn binds to. Use 127.0.0.1 for local; override only with an explicit front proxy.",
    expose=True,
)
TRUST_FORWARDED_HEADERS = config_knob(
    "TRUST_FORWARDED_HEADERS", default="false", validator=lambda s: s.strip().lower() in {"1", "true", "yes", "on"},
    doc=(
        "When set, honour X-Forwarded-Proto / X-Forwarded-Host / X-Forwarded-User "
        "from the front proxy. Leave false unless SKIFF is behind a trusted reverse "
        "proxy (Caddy, nginx, oauth2-proxy) — otherwise the headers are caller-controlled."
    ),
    expose=True,
)
APP_PORT = config_knob(
    "PORT", default="8080", validator=int,
    doc="TCP port uvicorn listens on. Override via PORT env var.",
    expose=True,
)
UVICORN_WORKERS = config_knob(
    "UVICORN_WORKERS", default="1", validator=int,
    doc="uvicorn worker count. Must be 1 for the module-level Docker client singleton.",
    expose=True,
)
UVICORN_LOG_LEVEL = config_knob(
    "UVICORN_LOG_LEVEL", default="warning",
    doc="uvicorn log level (critical|error|warning|info|debug|trace).",
    expose=True,
)

# ── FastAPI wiring constants ───────────────────────────────
# Kept here (not in app.py) so `skiff/app.py` is pure wiring and every
# policy value lives in one module. The lint_antipatterns.py AP005 rule
# rejects hardcoded wiring literals in app.py to prevent drift.
OPENAPI_URL = "/api/openapi.json"
CORS_ALLOW_METHODS: tuple[str, ...] = ("GET", "POST", "DELETE")
CORS_ALLOW_HEADERS: tuple[str, ...] = ("Authorization", "X-Requested-With", "Content-Type")
APP_TITLE = "SKIFF Container Manager"
APP_DESCRIPTION = (
    "Lightweight web UI for any Docker Engine API-compatible container runtime — "
    "local socket or remote host over SSH. All mutating operations require "
    "authentication and CSRF verification."
)
OPENAPI_TAGS: tuple[dict[str, str], ...] = (
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
)

# ── Application version ────────────────────────────────────
_APP_VERSION = "1.0.0"

# ── Server start time (monotonic for interval math, wall-clock for uptime) ──
APP_START_MONOTONIC = time.monotonic()  # used for setup window enforcement
APP_START_WALL = time.time()            # used for uptime display in /health

# ── Tunable knobs (TOML-sourced defaults + env-var override) ──────────────
# Every operational tunable below goes through config_knob() so operators
# can override via env var; the default values live in skiff/_config/defaults.toml
# so a fleet-wide tweak is a TOML edit + restart, not a source patch.
#
# Values NOT in this section are intentionally Python-only — either a
# security policy (MAX_CONTAINER_CPU, MIN_TOKEN_LENGTH, PRIVILEGED_PORT_THRESHOLD)
# or a Docker/OS invariant (DOCKER_MIN_MEM_BYTES, VALID_RESTART_POLICIES).
# Changing those SHOULD be a reviewed source patch, not an env tweak.

def _int_knob(name: str, *, expose: bool = True, doc: str = "") -> int:
    return config_knob(name, default=_knob_default(name), validator=int, doc=doc, expose=expose)


def _float_knob(name: str, *, expose: bool = True, doc: str = "") -> float:
    return config_knob(name, default=_knob_default(name), validator=float, doc=doc, expose=expose)


def _str_knob(name: str, *, expose: bool = True, doc: str = "") -> str:
    return config_knob(name, default=_knob_default(name), doc=doc, expose=expose)


# ── Operational caps ───────────────────────────────────────
MAX_COMPOSE_SIZE = _int_knob("MAX_COMPOSE_SIZE", doc="Max compose file upload size in bytes.")
MAX_BODY_BYTES = config_knob(
    "MAX_BODY_BYTES", default=str(1024 * 512), validator=int,
    doc="Maximum request body size in bytes. 413 returned if exceeded.",
    expose=True,
)
BODY_READ_TIMEOUT_SECS = config_knob(
    "BODY_READ_TIMEOUT_SECS", default="30", validator=int,
    doc=(
        "Per-chunk timeout (seconds) for reading a request body. A "
        "client that drips one byte every 5 s would otherwise hold an "
        "ASGI worker open for minutes; at this timeout the middleware "
        "responds 408 `validation.body_timeout` instead. Set higher "
        "for very slow uploads over WAN links; lower for tighter "
        "slow-POST defence."
    ),
    expose=True,
)
MAX_LOG_TAIL = _int_knob("MAX_LOG_TAIL", doc="Max lines in a single /logs response.")
MAX_AUDIT_LINES = _int_knob("MAX_AUDIT_LINES", doc="Max lines in a single /audit-log response.")
MAX_CONTAINERS = _int_knob("MAX_CONTAINERS", doc="Max containers the UI enumerates.")
MAX_PORT_MAPPINGS = _int_knob("MAX_PORT_MAPPINGS", doc="Max published ports per `docker run`.")
# Security-policy caps (intentional — operator change = fork): changing
# these weakens the sandbox, so they stay Python. See
# docs/hardening/production.md §Sandbox caps for rationale.
MAX_CONTAINER_MEM = "2g"
MAX_CONTAINER_CPU = 2.0

# ── Docker client ─────────────────────────────────────────
DOCKER_CLIENT_TIMEOUT = _int_knob("DOCKER_CLIENT_TIMEOUT", doc="Seconds per Docker SDK HTTP request.")
DOCKER_POOL_SIZE = _int_knob("DOCKER_POOL_SIZE", doc="urllib3 connection pool size.")
DOCKER_PING_TTL = _int_knob("DOCKER_PING_TTL", doc="Skip ping if last success < this (seconds).")
DOCKER_BACKOFF = _int_knob("DOCKER_BACKOFF", doc="Seconds to wait after a failed connection before retrying.")

# ── TCP keepalive (remote TCP Docker hosts only) ──────────
TCP_KEEPALIVE_IDLE = _int_knob("TCP_KEEPALIVE_IDLE", doc="Seconds before first keepalive probe.")
TCP_KEEPALIVE_INTERVAL = _int_knob("TCP_KEEPALIVE_INTERVAL", doc="Seconds between keepalive probes.")
TCP_KEEPALIVE_COUNT = _int_knob("TCP_KEEPALIVE_COUNT", doc="Probes before declaring the socket dead.")

# ── SSH tunnel ─────────────────────────────────────────────
TUNNEL_DEFAULT_SOCKET = _str_knob(
    "TUNNEL_DEFAULT_SOCKET", doc="Default local Unix socket path for the SSH tunnel.",
)
TUNNEL_CONNECT_TIMEOUT = _int_knob("TUNNEL_CONNECT_TIMEOUT", doc="Seconds for SSH to establish.")
TUNNEL_SOCKET_WAIT = _int_knob("TUNNEL_SOCKET_WAIT", doc="Seconds to wait for tunnel socket to appear.")
TUNNEL_SOCKET_POLL = _float_knob("TUNNEL_SOCKET_POLL", doc="Seconds between socket existence polls.")
TUNNEL_SERVER_ALIVE_INTERVAL = _int_knob(
    "TUNNEL_SERVER_ALIVE_INTERVAL", doc="SSH ServerAliveInterval.",
)
TUNNEL_SERVER_ALIVE_COUNT = _int_knob(
    "TUNNEL_SERVER_ALIVE_COUNT", doc="SSH ServerAliveCountMax.",
)
TUNNEL_STOP_TIMEOUT = _int_knob(
    "TUNNEL_STOP_TIMEOUT", doc="Seconds for `ssh -O exit` teardown subprocess.",
)
PROBE_DOCKER_TIMEOUT = _int_knob(
    "PROBE_DOCKER_TIMEOUT", doc="Seconds for the wizard's ping probe.",
)

# ── Undo queue ─────────────────────────────────────────────
UNDO_DELAY_SECS = _float_knob(
    "UNDO_DELAY_SECS", doc="Grace period in seconds before an undo-queued destructive op fires.",
)
UNDO_QUEUE_MAX_DEPTH = _int_knob(
    "UNDO_QUEUE_MAX_DEPTH", doc="Max pending undo ops; new enqueues past the cap run synchronously.",
)

# ── Debug (off by default — stack dumps can leak locals) ──
DEBUG_THREADS_ENABLED = config_knob(
    "SKIFF_DEBUG_THREADS", default="0", validator=lambda v: v not in ("", "0", "false", "False"),
    doc="Enable /debug/threads endpoint (AUTH-gated). Default disabled because "
        "thread stacks can contain sensitive local-variable reprs.",
    expose=True,
)

# ── Compose subprocess env fallbacks ──────────────────────
# Minimum-privilege fallbacks when the host process's PATH/HOME env vars
# aren't set. The compose subprocess needs these to find the docker
# binary; empty strings would cause "command not found". Python-only
# because (a) they're forensic safety nets, not operational tunables,
# and (b) the operator setting these has already wrapped the process
# in a shell that supplies PATH/HOME.
COMPOSE_PATH_FALLBACK = "/usr/bin"
COMPOSE_HOME_FALLBACK = "/root"
COMPOSE_UP_TIMEOUT = _int_knob("COMPOSE_UP_TIMEOUT", doc="Seconds for `docker compose up -d`.")
COMPOSE_DOWN_TIMEOUT = _int_knob("COMPOSE_DOWN_TIMEOUT", doc="Seconds for `docker compose down`.")

# ── WebSocket ──────────────────────────────────────────────
WS_LOG_TAIL = _int_knob("WS_LOG_TAIL", doc="Initial tail lines for log streams.")
WS_KEEPALIVE_INTERVAL = _int_knob(
    "WS_KEEPALIVE_INTERVAL", doc="Seconds between WebSocket ping frames.",
)
WS_LOG_IDLE_TIMEOUT = _int_knob(
    "WS_LOG_IDLE_TIMEOUT", doc="Close log stream after N seconds of silence.",
)
WS_EXEC_IDLE_TIMEOUT = _int_knob(
    "WS_EXEC_IDLE_TIMEOUT", doc="Close exec session after N seconds of inactivity.",
)
WS_EXEC_RECV_TIMEOUT = _float_knob(
    "WS_EXEC_RECV_TIMEOUT", doc="Exec socket recv timeout (seconds).",
)
WS_TOKEN_TIMEOUT = _float_knob(
    "WS_TOKEN_TIMEOUT", doc="Seconds to wait for the first-message AUTH token.",
)
WS_MAX_PER_IP = _int_knob("WS_MAX_PER_IP", doc="Max concurrent WS connections per IP.")
WS_AUTH_MAX_ATTEMPTS = _int_knob(
    "WS_AUTH_MAX_ATTEMPTS", doc="Failed WS auth attempts before IP lockout.",
)
WS_AUTH_LOCKOUT_SECS = _int_knob(
    "WS_AUTH_LOCKOUT_SECS", doc="Seconds to lock out an IP after max failed WS auth attempts.",
)
WS_KEEPALIVE_REVALIDATE_EVERY = _int_knob(
    "WS_KEEPALIVE_REVALIDATE_EVERY",
    doc="Revalidate session age every N keepalive ticks.",
)

# ── Auth policy (Python-only: weakening these weakens auth) ──
MIN_TOKEN_LENGTH = 16               # minimum API token length enforced by setup
TOKEN_AUDIT_SUFFIX_LEN = 8          # chars of token shown in audit log
_SESSION_CACHE_MAX = 1000           # safety cap on in-memory session cache entries
SESSION_ABS_TIMEOUT = config_knob(
    "SESSION_ABS_TIMEOUT",
    default=_knob_default("SESSION_ABS_TIMEOUT"),
    validator=int,
    doc=(
        "Server-side absolute session lifetime, seconds. The client reads "
        "this via /api/config so a deployment tightening the window "
        "doesn't require a JS edit."
    ),
    expose=True,
)
SESSION_IDLE_SECS = config_knob(
    "SESSION_IDLE_SECS",
    default=_knob_default("SESSION_IDLE_SECS"),
    validator=int,
    doc=(
        "Client idle-timeout window in seconds. Read by app.js from "
        "/api/config at boot; falls back to the JS default if /api/config "
        "is unreachable."
    ),
    expose=True,
)

# ── Audit log rotation ─────────────────────────────────────
# Default: 10 MiB x 5 files ~= 50 MiB (~13 days). For 1-year retention:
#   AUDIT_MAX_MB=200 AUDIT_BACKUP_COUNT=20  → ~4 GiB (covers 13 months).
AUDIT_MAX_BYTES = config_knob(
    "AUDIT_MAX_MB", default="10", validator=lambda v: int(v) * 1024 * 1024,
    doc="Audit log rotation file size in MiB. Multiplied by AUDIT_BACKUP_COUNT for total retention.",
    expose=True,
)
AUDIT_BACKUP_COUNT = config_knob(
    "AUDIT_BACKUP_COUNT", default="5", validator=int,
    doc="Number of rotated audit log files to keep. Higher = longer retention, more disk.",
    expose=True,
)

# ── Docker Hub registry proxy ─────────────────────────────
REGISTRY_SEARCH_PAGE_SIZE = _int_knob(
    "REGISTRY_SEARCH_PAGE_SIZE", doc="Results per /api/registry/search call.",
)
REGISTRY_MAX_TAGS = _int_knob("REGISTRY_MAX_TAGS", doc="Tags per /api/registry/tags call.")
REGISTRY_TIMEOUT = _int_knob("REGISTRY_TIMEOUT", doc="Seconds for Docker Hub API requests.")
REGISTRY_DESC_MAX = _int_knob(
    "REGISTRY_DESC_MAX", doc="Max chars of registry description echoed back.",
)

# ── Container operation budgets ───────────────────────────
CONTAINER_STOP_TIMEOUT = _int_knob(
    "CONTAINER_STOP_TIMEOUT", doc="Seconds for graceful stop before kill.",
)
CONTAINER_RESTART_TIMEOUT = _int_knob("CONTAINER_RESTART_TIMEOUT", doc="Seconds for restart.")
CONTAINER_STATS_TIMEOUT = _float_knob("CONTAINER_STATS_TIMEOUT", doc="Seconds for stats call.")
IMAGE_PULL_TIMEOUT = _float_knob(
    "IMAGE_PULL_TIMEOUT", doc="Seconds for image pull (network slow).",
)

# ── Sandbox caps (Python-only: policy) ────────────────────
PRIVILEGED_PORT_THRESHOLD = 1024    # OS constant; host ports below require elevated privilege
MAX_VOLUME_NAME_LENGTH = 63         # Docker spec constraint
MAX_RESTART_RETRIES = 5             # on-failure restart maximum retry count
MAX_PIDS_LIMIT = 4096               # cap on per-container PIDs
DOCKER_MIN_MEM_BYTES = 6 * 1024 * 1024  # Docker rejects Memory<6MiB at the engine level
MAX_TMPFS_MOUNTS = 10               # max tmpfs mounts per container
MAX_TMPFS_SIZE_MB = 512             # cumulative tmpfs size cap (prevents RAM exhaustion)
# Shared across run_container and update_container. Matches Docker Engine API
# RestartPolicy.Name enum; on-failure additionally honours MaximumRetryCount.
VALID_RESTART_POLICIES: set[str] = {"no", "on-failure", "unless-stopped", "always"}
# Default tmpfs mounts applied when read_only=True and the caller didn't specify tmpfs.
# Source: skiff/_config/tmpfs.toml — every [<path>] section becomes one entry.
DEFAULT_TMPFS: dict[str, str] = {
    path: entry.get("opts", "rw") for path, entry in _TOML_TMPFS.items()
}

# ── Setup wizard ──────────────────────────────────────────
SETUP_WINDOW_SECS = _int_knob(
    "SETUP_WINDOW_SECS", doc="Wizard reachable for N seconds post-boot.",
)
SETUP_MAX_ATTEMPTS = _int_knob(
    "SETUP_MAX_ATTEMPTS", doc="POST /api/setup failures before IP lockout.",
)
SETUP_LOCKOUT_SECS = _int_knob(
    "SETUP_LOCKOUT_SECS", doc="Seconds to lock out an IP after max failed setup attempts.",
)
# Security headers — CSP / Permissions-Policy / HSTS live in
# skiff/_config/security_headers.toml. See that file for the zero-trust
# rationale behind each directive.
HSTS_MAX_AGE = int(_TOML_SECURITY_HEADERS["hsts_max_age_seconds"])
HSTS_HEADER = f"max-age={HSTS_MAX_AGE}; includeSubDomains"
_CSP = _TOML_SECURITY_HEADERS["csp"]
_PERMISSIONS_POLICY = _TOML_SECURITY_HEADERS["permissions_policy"]
# Optional GCP Cloud Logging sink
_GCP_PROJECT = config_knob(
    "GOOGLE_CLOUD_PROJECT", default="",
    doc="Google Cloud project ID. When set, audit log events are also mirrored to Cloud Logging.",
)
_GCP_LOG_NAME = config_knob(
    "GCP_LOG_NAME", default="skiff-audit",
    doc="Cloud Logging log name used when GOOGLE_CLOUD_PROJECT is set.",
)

# ── Persona presets ────────────────────────────────────────
# Each profile bundles a small set of default overrides keyed by the same
# env var names an operator would set individually. Setting `PROFILE=...`
# applies them up-front so the user doesn't hand-roll a config. Anything
# already in the environment wins — presets never clobber explicit choices.
#
# Personas:
#   homelab  — local Pi / NAS install. Loose rate limits; everything visible.
#   dev      — developer workstation. Current defaults (unchanged).
#   sre      — remote Docker host via tunnel. Faster rate limits for audit
#              log / metrics scraping.
#   reviewer — security-review mode. Rate limits tight, no destructive actions
#              surfaced beyond what's essential (future work: toggle UI bits).
#   tutor    — classroom. Loose limits, skip confirmations for a teaching
#              environment where blast radius is intentionally low.
#   ci       — CI runner. Rate limits wide open; API_TOKEN must be in env;
#              wizard never triggers.
# Profile presets are the top-level tables in skiff/_config/profiles.toml —
# each [<name>] section becomes one preset. Values are str so TOML
# strings pass through unchanged; os.environ.setdefault() below
# applies them without clobbering explicit env vars.
_PROFILE_PRESETS: dict[str, dict[str, str]] = _TOML_PROFILES


def _apply_profile(raw: str) -> str:
    """Validate PROFILE and apply its preset env overrides as a side effect.

    This function is both a validator and an applier — it mutates
    os.environ via setdefault so later `config_knob()` calls pick up
    the preset defaults. The `validator=` protocol of config_knob() is
    "(str) -> parsed value"; this callable also has the documented
    side effect of writing presets to the process env. Explicit env
    always wins because setdefault is a no-op when the key exists.
    """
    name = raw.strip().lower()
    if not name:
        return ""
    if name not in _PROFILE_PRESETS:
        raise ValueError(f"Unknown PROFILE={name!r}. Valid: {sorted(_PROFILE_PRESETS)}")
    for k, v in _PROFILE_PRESETS[name].items():
        os.environ.setdefault(k, v)
    return name


# PROFILE knob — the side effect (seeding preset env vars) is the point;
# the returned string is also exposed on the module so `/api/config` and
# anything else that wants the active preset has a single source of truth.
# Default is "dev" when unset so the UI never shows an empty profile.
PROFILE = _apply_profile_result = config_knob(
    "PROFILE", default="", validator=_apply_profile,
    doc=(
        "Persona preset that seeds sensible defaults for RATE_LIMIT_SCALE and related "
        "knobs. One of: " + ", ".join(sorted(_PROFILE_PRESETS)) + ". Explicit env wins."
    ),
    expose=True,
) or "dev"

# ── Rate limiting ──────────────────────────────────────────
def _rate_scale_validator(raw: str) -> int:
    v = int(raw)
    if not (1 <= v <= 100):
        raise ValueError(f"RATE_LIMIT_SCALE must be between 1 and 100, got {v}")
    return v


_RATE_SCALE = config_knob(
    "RATE_LIMIT_SCALE", default="1", validator=_rate_scale_validator,
    doc="Multiplier applied to every rate-limit spec. 1=default, 100=CI (effectively uncapped).",
    expose=True,
)


def _limit(spec: str) -> str:
    """Scale a rate limit spec by RATE_LIMIT_SCALE (e.g. '10/minute' → '100/minute')."""
    if _RATE_SCALE == 1:
        return spec
    count, _, period = spec.partition("/")
    return f"{int(count) * _RATE_SCALE}/{period}"


limiter = Limiter(key_func=get_remote_address)
