# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Input validation, Docker helper functions, and compose-file sandbox enforcement."""
from __future__ import annotations

import re
from typing import Any

import docker
import docker.errors
import structlog
import yaml
from fastapi import HTTPException

from skiff import config
from skiff.contract.errors import http_error
from skiff.docker_client import DOCKER_TRANSIENT, invalidate_client

log = structlog.get_logger(__name__)

# ── Identifier validation — one data table, regexes derived ─────────────────
# Each entry is (first-char class, body-char class, min-len, max-len). The
# shapes below match Docker's documented identifier alphabets; a spec
# change is one row. `min_len=1` means "one leading character then up to
# max_len-1 body characters"; `min_len=4` (hex_id only) enforces the
# 4-char short-ID floor the Docker CLI accepts.

_IDENT_SHAPES: dict[str, tuple[str, str, int, int]] = {
    "hex_id":         ("a-f0-9",    "a-f0-9",          4,  64),
    "compose_name":   ("a-z0-9",    "a-z0-9_-",        1,  63),
    "container_name": ("a-zA-Z0-9", "a-zA-Z0-9_.-",    1, 128),
    "network_name":   ("a-zA-Z0-9", "a-zA-Z0-9_.-",    1,  64),
    "image_tag":      ("a-zA-Z0-9", "a-zA-Z0-9_./:@-", 1, 256),
}


def _compile_ident(shape: str) -> re.Pattern[str]:
    first, body, min_len, max_len = _IDENT_SHAPES[shape]
    if min_len <= 1:
        return re.compile(rf"^[{first}][{body}]{{0,{max_len - 1}}}$")
    # min_len > 1 is the hex-ID case: no distinct leading char class.
    return re.compile(rf"^[{body}]{{{min_len},{max_len}}}$")


CONTAINER_ID_RE = _compile_ident("hex_id")
# Network IDs and volume-backing IDs share the Docker SHA-256 short-id shape.
NETWORK_ID_RE = CONTAINER_ID_RE
IMAGE_ID_RE = re.compile(rf"^(sha256:)?{CONTAINER_ID_RE.pattern[1:-1]}$")

PROJECT_NAME_RE = _compile_ident("compose_name")
# Compose service names use the same shape today. Aliased (not re-compiled)
# so a future spec divergence is a one-line change.
SERVICE_NAME_RE = PROJECT_NAME_RE

CONTAINER_NAME_RE = _compile_ident("container_name")
# Docker label keys share the container-name alphabet.
LABEL_KEY_RE = CONTAINER_NAME_RE

NETWORK_NAME_RE = _compile_ident("network_name")
# Volume names follow the same Docker CLI rules as networks.
VOLUME_NAME_RE = NETWORK_NAME_RE

IMAGE_TAG_RE = _compile_ident("image_tag")

# Docker Hub repo-name alphabet. Used by the registry proxy endpoints
# as a belt-and-braces filter before interpolating `repo` into the
# upstream URL path.
HUB_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9_./\-]{0,199}$")
_ENV_SENSITIVE_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|KEY|CREDENTIAL|AUTH|CERT|PRIVATE|API_KEY)",
    re.IGNORECASE,
)

# ── Path / ID validation ───────────────────────────────────

def validate_container_id(container_id: str) -> str:
    """Raise HTTP 400 if container_id is not a valid hex ID."""
    if not CONTAINER_ID_RE.fullmatch(container_id):
        raise http_error("container.bad_id")
    return container_id


def validate_project_name(project_name: str) -> str:
    """Raise HTTP 400 if project_name is not a valid compose project name."""
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise http_error("validation.bad_project_name")
    return project_name


def validate_image_id(image_id: str) -> str:
    """Raise HTTP 400 if image_id is not a valid image digest or short ID."""
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise http_error("image.bad_id")
    return image_id


def _extract_image_registry(image: str) -> str:
    """Return the registry host of `image` ("" when it's bare like 'nginx').

    An `@sha256:…` digest splits at @; a `:tag` splits at :. The host
    portion is present iff the first slash-component looks like a host
    (contains `.` or a port `:`).
    """
    sep = "@" if "@" in image else ":"
    image_no_tag = image.split(sep, maxsplit=1)[0]
    parts = image_no_tag.split("/")
    if len(parts) >= 2 and ("." in parts[0] or ":" in parts[0]):
        return parts[0]
    return ""


def _allow_bare_image() -> bool:
    """True when docker.io is in the allowlist — bare names resolve there."""
    return any(
        r.rstrip("/").lower() == "docker.io"
        for r in config._cfg.allowed_registries
    )


def _registry_matches_allowlist(image: str, image_registry: str) -> bool:
    """True if `image` (or its registry host) matches any allowlist entry."""
    lower = image_registry.lower()
    return any(
        lower == r.rstrip("/").lower()
        or image.lower().startswith((r if r.endswith("/") else r + "/").lower())
        for r in config._cfg.allowed_registries
    )


def _raise_bare_image_blocked() -> None:
    raise http_error(
        "image.registry_blocked",
        registry="(none)",
        message=(
            f"Image must include an explicit registry hostname. "
            f"Allowed: {', '.join(config._cfg.allowed_registries)}"
        ),
    )


def validate_image_registry(image: str) -> None:
    """Raise HTTP 400 if the image is not from an allowed registry."""
    if not IMAGE_TAG_RE.fullmatch(image):
        raise http_error("validation.bad_image_name")
    if not config._cfg.allowed_registries:
        return
    image_registry = _extract_image_registry(image)
    if not image_registry:
        if not _allow_bare_image():
            _raise_bare_image_blocked()
        return
    if not _registry_matches_allowlist(image, image_registry):
        raise http_error("image.registry_blocked", registry=image_registry)


def validate_container_name(name: str | None) -> str | None:
    """Raise HTTP 400 if name is not a valid Docker container name."""
    if name is None:
        return None
    if not CONTAINER_NAME_RE.fullmatch(name):
        raise http_error("container.bad_name")
    return name


# ── Docker helpers ─────────────────────────────────────────

def _get_container(client, container_id: str):
    """Fetch a container by ID with proper error handling."""
    validate_container_id(container_id)
    try:
        return client.containers.get(container_id)
    except docker.errors.NotFound as exc:
        raise http_error("container.not_found", id=container_id) from exc
    except DOCKER_TRANSIENT as e:
        log.warning("docker.transient_error", error=str(e))
        invalidate_client()
        raise http_error("system.docker_unreachable") from e


def _raise_docker_api_error(e: docker.errors.APIError) -> None:
    """Map a Docker-SDK APIError to the correct http_error shape.

    Preserves the upstream status when it's non-default (Docker daemon may
    return 422 / 500 / etc. for specific failures); the catalogue entry is
    400-only, so non-400 non-409 codes fall through to raw HTTPException.
    """
    if e.status_code == 409:
        raise http_error("container.conflict") from e
    message = str(e.explanation or "Container operation failed")[:500]
    if e.status_code and e.status_code != 400:
        raise HTTPException(e.status_code, message) from e
    raise http_error("container.op_failed", message=message) from e


def _is_object_bound(fn) -> bool:
    """True when fn is a bound method on an instance (not a class/module)."""
    self = getattr(fn, "__self__", None)
    return self is not None and not isinstance(self, type)


def safe_docker_call(fn, *args, **kwargs):
    """Execute a Docker SDK call with transient-error retry.

    - NotFound            → 404 (`resource.not_found`)
    - APIError            → preserves upstream status, or 400 op_failed
    - DOCKER_TRANSIENT    → one retry (client-level calls only) then 503

    Object-bound methods (container.start) skip the retry because the
    object retains a reference to the closed client; the caller's next
    request uses a fresh client via get_client().
    """
    attempts = 1 if _is_object_bound(fn) else 2
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except docker.errors.NotFound as exc:
            raise http_error("resource.not_found") from exc
        except docker.errors.APIError as e:
            _raise_docker_api_error(e)
        except DOCKER_TRANSIENT as e:
            if attempt == 0:
                log.warning("docker.transient_error", error=str(e), action="invalidating_client")
                invalidate_client()
                continue
            raise http_error("system.docker_unreachable") from e
    raise http_error("system.docker_unreachable")  # pragma: no cover


# ── Redaction helpers ──────────────────────────────────────

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


def _redact_value(k: Any, v: Any, depth: int) -> Any:
    """Decide redaction for one dict entry — dispatches on value type.

    A sensitive key name ('password', 'secret', etc.) redacts ANY value
    type, including dicts and lists. Only non-sensitive keys recurse —
    so a nested `{"labels": {"api_key": "..."}}` still gets redacted on
    the inner key even if the outer key is benign.
    """
    if _ENV_SENSITIVE_RE.search(str(k)):
        return "[REDACTED]"
    if isinstance(v, dict):
        return _redact_dict(v, depth + 1)
    if isinstance(v, list):
        return _redact_env(v) if all(isinstance(i, str) for i in v) else v
    return v


def _redact_dict(d: dict, _depth: int = 0) -> dict:
    """Recursively redact sensitive string values from a dict (labels, env mappings, etc.)."""
    if _depth > 10:
        return {"[truncated]": "..."}
    return {k: _redact_value(k, v, _depth) for k, v in d.items()}


# ── Volume / mount validation ──────────────────────────────
# Sourced from skiff/_config/mount_targets.toml — see that file for the
# rationale behind each blocked path.
_BLOCKED_MOUNT_TARGETS: frozenset[str] = frozenset(config._TOML_MOUNT_TARGETS["bind"]["paths"])


def _validate_mount_target(path: str) -> None:
    """Reject mounts to sensitive container paths."""
    if not path.startswith("/"):
        raise http_error("validation.bad_mount_target")
    normalized = path.rstrip("/")
    for blocked in _BLOCKED_MOUNT_TARGETS:
        if normalized == blocked or normalized.startswith(blocked + "/"):
            raise http_error("validation.mount_target_blocked", path=path)


# ── tmpfs mount validation ─────────────────────────────────
# Different blocklist than host-bind volumes: a fresh empty tmpfs over /run or
# /var/run is safe (and needed by nginx, redis, etc.), whereas bind-mounting the
# host's /run into a container is not. Still reject paths that would mask or
# corrupt the container's OS state.
_TMPFS_BLOCKED_TARGETS: frozenset[str] = frozenset(config._TOML_MOUNT_TARGETS["tmpfs"]["paths"])
# Allowlist of per-option tokens (comma-separated in the options string).
# Intentionally restrictive — no `exec`, `suid`, `dev` (only their "no" variants),
# no arbitrary mount flags. `size=` accepts k/m/g suffixes.
_TMPFS_OPT_RE = re.compile(r"^(rw|ro|noexec|nosuid|nodev|noatime|size=\d+[kmg]?|mode=[0-7]{3,4})$")


# ── Resource-quantity parsing (GCP / Kubernetes style) ────
# Accepts both IEC binary suffixes (Ki, Mi, Gi, Ti) and decimal Docker-style suffixes
# (k/K, m/M, g/G, t/T) for memory, matching Cloud Run / GKE conventions. CPU quantities
# accept a plain decimal ("0.5", "2") or the Kubernetes milli suffix ("500m" = 0.5 CPU).
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]i?|[kmgt])?\s*$")
_CPU_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m)?\s*$")
_MEM_UNIT_BYTES = {
    "": 1,
    "k": 1000, "K": 1000, "Ki": 1024,
    "m": 1_000_000, "M": 1_000_000, "Mi": 1024 ** 2,
    "g": 1_000_000_000, "G": 1_000_000_000, "Gi": 1024 ** 3,
    "t": 1_000_000_000_000, "T": 1_000_000_000_000, "Ti": 1024 ** 4,
}


def parse_memory_quantity(value: str | int) -> int:
    """Parse memory quantity to bytes. Accepts int (bytes), str with IEC or decimal suffix.

    Examples: "256Mi" → 268435456, "1Gi" → 1073741824, "500M" → 500000000.
    Raises HTTPException(400) on invalid format. The regex guarantees the numeric
    fragment is a valid non-negative decimal and the unit is in _MEM_UNIT_BYTES,
    so no redundant float()/range checks are needed past the regex.
    """
    if isinstance(value, bool):
        raise http_error(
            "validation.bad_memory",
            message="memory must be an integer (bytes) or a string like '256Mi'",
        )
    if isinstance(value, int):
        if value < 0:
            raise http_error("validation.bad_memory", message="memory must be non-negative")
        return value
    if not isinstance(value, str):
        raise http_error(
            "validation.bad_memory",
            message="memory must be an integer (bytes) or a string like '256Mi'",
        )
    m = _MEM_RE.match(value)
    if not m:
        raise http_error(
            "validation.bad_memory",
            message=f"invalid memory quantity: {value[:32]!r}",
        )
    return int(float(m.group(1)) * _MEM_UNIT_BYTES[m.group(2) or ""])


def parse_cpu_quantity(value: str | float) -> float:
    """Parse CPU quantity to fractional cores. Accepts numeric, "0.5", "500m", "2".

    Returns a float (e.g. 0.5 for half a core). Raises HTTPException(400) on invalid.
    The regex guarantees the numeric fragment is a valid non-negative decimal.
    """
    if isinstance(value, bool):
        raise http_error(
            "validation.bad_cpu",
            message="cpus must be a number or string like '0.5' or '500m'",
        )
    if isinstance(value, (int, float)):
        if value < 0:
            raise http_error("validation.bad_cpu", message="cpus must be non-negative")
        return float(value)
    if not isinstance(value, str):
        raise http_error(
            "validation.bad_cpu",
            message="cpus must be a number or string like '0.5' or '500m'",
        )
    m = _CPU_RE.match(value)
    if not m:
        raise http_error("validation.bad_cpu", message=f"invalid cpu quantity: {value[:32]!r}")
    num = float(m.group(1))
    return num / 1000.0 if m.group(2) == "m" else num


# ── _validate_tmpfs helpers ──────────────────────────────────────────────────

# Size-suffix → MB multiplier for tmpfs size= option parsing.
_TMPFS_SIZE_UNITS: dict[str, float] = {
    "": 1 / 1024 / 1024,  # raw bytes → MB
    "k": 1 / 1024,         # kilobytes → MB
    "m": 1.0,              # megabytes
    "g": 1024.0,           # gigabytes → MB
}


def _check_tmpfs_path(path: Any) -> None:
    """Reject paths that aren't absolute, have `..` components, or hit the blocklist."""
    if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
        raise http_error(
            "validation.tmpfs_bad_path",
            message=f"invalid tmpfs path: {str(path)[:50]}",
        )
    normalized = path.rstrip("/") or "/"
    for blocked in _TMPFS_BLOCKED_TARGETS:
        if normalized == blocked or normalized.startswith(blocked + "/"):
            raise http_error("validation.tmpfs_path_blocked", path=path)


def _size_opt_to_mb(opt: str) -> float:
    """Convert a `size=NNN[kmg]` option to megabytes."""
    val = opt[5:]
    unit = val[-1].lower() if val and val[-1].lower() in "kmg" else ""
    # Regex _TMPFS_OPT_RE guarantees digits-then-optional-suffix shape,
    # so int() cannot fail here.
    num = int(val[:-1] if unit else val)
    return num * _TMPFS_SIZE_UNITS[unit]


def _check_tmpfs_opts(path: str, opts: Any) -> float:
    """Validate option string; return the mount's size in MB (0 if no size= set)."""
    if not isinstance(opts, str) or len(opts) > 256:
        raise http_error(
            "validation.tmpfs_bad_options",
            message=f"invalid tmpfs options for {path!r}",
        )
    mount_mb = 0.0
    for raw in opts.split(","):
        opt = raw.strip()
        if not _TMPFS_OPT_RE.match(opt):
            raise http_error(
                "validation.tmpfs_bad_options",
                message=f"invalid tmpfs option {opt[:50]!r} for {path!r}",
            )
        if opt.startswith("size="):
            mount_mb += _size_opt_to_mb(opt)
    return mount_mb


def _validate_tmpfs(tmpfs: dict, max_mounts: int, max_total_mb: int) -> None:
    """Validate a tmpfs mapping {container_path: options_string}.

    Linear pipeline: shape → count → per-path checks → per-opts checks →
    total-size cap. Each step is a named helper above.
    """
    if not isinstance(tmpfs, dict):
        raise http_error("validation.bad_tmpfs_shape")
    if len(tmpfs) > max_mounts:
        raise http_error("validation.tmpfs_too_many", max_mounts=max_mounts)
    total_mb = 0.0
    for path, opts in tmpfs.items():
        _check_tmpfs_path(path)
        total_mb += _check_tmpfs_opts(path, opts)
    if total_mb > max_total_mb:
        raise http_error(
            "validation.tmpfs_size_exceeds_cap",
            total_mb=total_mb,
            max_total_mb=max_total_mb,
        )


# ── Compose file validation ────────────────────────────────
# Sourced from skiff/_config/compose_sandbox.toml — see that file for the
# zero-trust rationale. Adding / removing a key is a security decision
# that belongs in a TOML commit with CODEOWNERS review.
BLOCKED_PRESENCE_KEYS: frozenset[str] = frozenset(config._TOML_COMPOSE_SANDBOX["forbidden"]["presence"])
BLOCKED_TRUTHY_KEYS: frozenset[str] = frozenset(config._TOML_COMPOSE_SANDBOX["forbidden"]["truthy"])
BLOCKED_COMPOSE_TOP_KEYS: frozenset[str] = frozenset(config._TOML_COMPOSE_SANDBOX["forbidden"]["top"])
BLOCKED_NETWORK_MODES: frozenset[str] = frozenset(config._TOML_COMPOSE_SANDBOX["blocked"]["network_modes"])
BLOCKED_IPC_MODES: frozenset[str] = frozenset(config._TOML_COMPOSE_SANDBOX["blocked"]["ipc_modes"])
BLOCKED_COMPOSE_SERVICE_KEYS = BLOCKED_PRESENCE_KEYS | BLOCKED_TRUTHY_KEYS


# ── validate_compose_file helpers ────────────────────────────────────────────
# validate_compose_file was one 65-line body with four levels of nested
# loops. Split into named checks — each covers one service-level concern
# and returns None (or raises http_error). `_check_service(svc_name, svc)`
# is the per-service pipeline; `validate_compose_file` is the top-level.


def _parse_compose_yaml(content: bytes) -> dict:
    """Size-cap + YAML parse. Returns the mapping or raises."""
    if len(content) > config.MAX_COMPOSE_SIZE:
        raise http_error(
            "compose.too_large",
            message=f"compose file too large (max {config.MAX_COMPOSE_SIZE // 1024}KB)",
        )
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise http_error("compose.bad_yaml") from exc
    if not isinstance(data, dict):
        raise http_error("compose.bad_yaml", message="compose file must be a YAML mapping")
    return data


def _check_top_level_keys(data: dict) -> None:
    """Reject `secrets:` / `configs:` at the top level."""
    for blocked_top in BLOCKED_COMPOSE_TOP_KEYS:
        if data.get(blocked_top):
            raise http_error("compose.forbidden_key", key=blocked_top)


def _check_service_shape(svc_name: str, svc: Any) -> None:
    """Reject a service entry that isn't a mapping."""
    if not isinstance(svc, dict):
        raise http_error(
            "compose.service_not_mapping",
            svc_name=svc_name,
            message=f"service '{svc_name}' must be a mapping, got {type(svc).__name__}",
        )


def _check_forbidden_keys(svc_name: str, svc: dict) -> None:
    """Reject presence-blocked keys (any value forbidden) and truthy-blocked keys."""
    for key in BLOCKED_PRESENCE_KEYS:
        if key in svc:
            raise http_error("compose.service_forbidden_key", svc_name=svc_name, key=key)
    for key in BLOCKED_TRUTHY_KEYS:
        val = svc.get(key)
        if val is True or (isinstance(val, (list, dict, str)) and val):
            raise http_error("compose.service_forbidden_key", svc_name=svc_name, key=key)


def _check_namespace_modes(svc_name: str, svc: dict) -> None:
    """Reject host-namespace modes (network / pid / ipc)."""
    net_mode = str(svc.get("network_mode", ""))
    if any(net_mode.startswith(m) for m in BLOCKED_NETWORK_MODES):
        raise http_error(
            "compose.service_bad_network_mode",
            svc_name=svc_name, net_mode=net_mode,
        )
    if str(svc.get("pid", "")) == "host":
        raise http_error("compose.service_host_pid", svc_name=svc_name)
    ipc_mode = str(svc.get("ipc", ""))
    if ipc_mode in BLOCKED_IPC_MODES:
        raise http_error("compose.service_bad_ipc", svc_name=svc_name, ipc_mode=ipc_mode)


def _volume_source(vol: Any) -> str:
    """Return the source string for a volume entry (string, long form, or other)."""
    if isinstance(vol, str):
        return vol
    if isinstance(vol, dict):
        return vol.get("source", "") or ""
    return str(vol)


def _check_volumes(svc_name: str, svc: dict) -> None:
    """Reject host-path mounts (absolute, ~, .., $)."""
    for vol in svc.get("volumes", []) or ():
        if _volume_source(vol).startswith(("/", "~", "..", "$")):
            raise http_error("compose.service_host_volume", svc_name=svc_name)


def _check_image_registry(svc: dict) -> None:
    """Reject images from registries outside the allowlist."""
    image = svc.get("image", "")
    if image:
        validate_image_registry(image)


# Registered service-level checks, run in order. Each takes (svc_name, svc)
# and either returns None or raises http_error. Adding a new compose-safety
# check is one line below — no handler body to thread the new branch through.
_SERVICE_CHECKS: tuple = (
    _check_forbidden_keys,
    _check_namespace_modes,
    _check_volumes,
    lambda _svc_name, svc: _check_image_registry(svc),
)


def _check_service(svc_name: str, svc: Any) -> None:
    """Run every registered service-level check."""
    _check_service_shape(svc_name, svc)
    for check in _SERVICE_CHECKS:
        check(svc_name, svc)


def validate_compose_file(content: bytes) -> dict:
    """Parse + sandbox-validate a compose file. Returns the parsed mapping.

    Linear pipeline:
      1. parse (size cap + YAML + mapping check)
      2. reject forbidden top-level keys (secrets / configs)
      3. for each service: run _check_service (shape → forbidden keys →
         namespace modes → volumes → image registry)
    """
    data = _parse_compose_yaml(content)
    _check_top_level_keys(data)
    services = data.get("services", {})
    if not isinstance(services, dict):
        raise http_error("compose.bad_services")
    for svc_name, svc in services.items():
        _check_service(svc_name, svc)
    return data


def _sanitize_stderr(stderr: str) -> str:
    """Strip internal paths and hostnames from subprocess error output before returning to client.

    Substitutions are applied iteratively so a multi-segment path like
    `/var/lib/skiff/compose/proj/docker-compose.yml` collapses to a
    single `[path]` rather than leaking the first segment.
    """
    # Any run of `/segment[/segment…]` becomes one `[path]`. The character
    # class includes a literal `/` so consecutive path segments are eaten in
    # one match. Repeated until stable to catch any edge-case residuals.
    prev = None
    sanitized = stderr
    while prev != sanitized:
        prev = sanitized
        sanitized = re.sub(r'(/[^\s:,\'"]+)', '[path]', sanitized)
    sanitized = re.sub(r'\b([a-zA-Z0-9-]+\.){2,}[a-zA-Z]{2,}\b', '[host]', sanitized)
    # Collapse `[path][path]…` runs that the iterative substitution may leave.
    sanitized = re.sub(r'(?:\[path\]){2,}', '[path]', sanitized)
    return sanitized[:400].strip()
