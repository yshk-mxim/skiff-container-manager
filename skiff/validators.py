# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Input validation, Docker helper functions, and compose-file sandbox enforcement."""
from __future__ import annotations

import re

import docker
import docker.errors
import structlog
import yaml
from fastapi import HTTPException

from skiff.config import (
    MAX_COMPOSE_SIZE,
    _cfg,
)
from skiff.docker_client import DOCKER_TRANSIENT, _invalidate_client

log = structlog.get_logger(__name__)

# ── Regex patterns ─────────────────────────────────────────
CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{4,64}$")
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
IMAGE_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}$")
NETWORK_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
IMAGE_ID_RE = re.compile(r"^(sha256:)?[a-f0-9]{4,64}$")
_ENV_SENSITIVE_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|KEY|CREDENTIAL|AUTH|CERT|PRIVATE|API_KEY)",
    re.IGNORECASE,
)

# ── Path / ID validation ───────────────────────────────────

def validate_container_id(container_id: str) -> str:
    """Raise HTTP 400 if container_id is not a valid hex ID."""
    if not CONTAINER_ID_RE.match(container_id):
        raise HTTPException(400, "Invalid container ID format")
    return container_id


def validate_project_name(project_name: str) -> str:
    """Raise HTTP 400 if project_name is not a valid compose project name."""
    if not PROJECT_NAME_RE.match(project_name):
        raise HTTPException(400, "Invalid project name")
    return project_name


def validate_image_id(image_id: str) -> str:
    """Raise HTTP 400 if image_id is not a valid image digest or short ID."""
    if not IMAGE_ID_RE.match(image_id):
        raise HTTPException(400, "Invalid image ID format")
    return image_id


def validate_image_registry(image: str) -> None:
    """Raise HTTP 400 if the image is not from an allowed registry."""
    if not IMAGE_TAG_RE.match(image):
        raise HTTPException(400, "Invalid image name format")
    if not _cfg.allowed_registries:
        return
    image_no_tag = image.split(":", maxsplit=1)[0] if "@" not in image else image.split("@", maxsplit=1)[0]
    parts = image_no_tag.split("/")
    has_registry_host = len(parts) >= 2 and ("." in parts[0] or ":" in parts[0])
    image_registry = parts[0] if has_registry_host else ""
    if not image_registry:
        if any(r.rstrip("/").lower() == "docker.io" for r in _cfg.allowed_registries):
            return
        raise HTTPException(
            400, f"Image must include an explicit registry hostname. Allowed: {', '.join(_cfg.allowed_registries)}"
        )
    image_registry_lower = image_registry.lower()
    if not any(
        image_registry_lower == r.rstrip("/").lower()
        or image.lower().startswith((r if r.endswith("/") else r + "/").lower())
        for r in _cfg.allowed_registries
    ):
        allowed = ', '.join(_cfg.allowed_registries)
        raise HTTPException(400, f"Only images from approved registries are allowed: {allowed}")


def validate_container_name(name: str | None) -> str | None:
    """Raise HTTP 400 if name is not a valid Docker container name."""
    if name is None:
        return None
    if not CONTAINER_NAME_RE.match(name):
        raise HTTPException(400, "Invalid container name (alphanumeric, dots, hyphens, underscores)")
    return name


# ── Docker helpers ─────────────────────────────────────────

def _get_container(client, container_id: str):
    """Fetch a container by ID with proper error handling."""
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


def _redact_dict(d: dict, _depth: int = 0) -> dict:
    """Recursively redact sensitive string values from a dict (labels, env mappings, etc.)."""
    if _depth > 10:
        return {"[truncated]": "..."}
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and _ENV_SENSITIVE_RE.search(str(k)):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = _redact_dict(v, _depth + 1)
        elif isinstance(v, list):
            out[k] = _redact_env(v) if all(isinstance(i, str) for i in v) else v
        else:
            out[k] = v
    return out


# ── Volume / mount validation ──────────────────────────────
_BLOCKED_MOUNT_TARGETS = {"/etc", "/proc", "/sys", "/dev", "/var/run", "/run"}


def _validate_mount_target(path: str) -> None:
    """Reject mounts to sensitive container paths."""
    if not path.startswith("/"):
        raise HTTPException(400, "Volume mount target must be an absolute path")
    normalized = path.rstrip("/")
    for blocked in _BLOCKED_MOUNT_TARGETS:
        if normalized == blocked or normalized.startswith(blocked + "/"):
            raise HTTPException(400, f"Mount target {path!r} is not permitted")


# ── Compose file validation ────────────────────────────────
BLOCKED_PRESENCE_KEYS = {"privileged", "configs", "secrets", "build", "devices"}
BLOCKED_TRUTHY_KEYS = {
    "cap_add", "userns_mode", "sysctls", "security_opt", "shm_size",
    "extends", "volumes_from", "env_file",
    "cgroup_parent", "dns", "dns_search", "extra_hosts", "tmpfs",
    "uts", "cgroupns_mode", "storage_opt", "device_cgroup_rules",
}
BLOCKED_IPC_MODES = {"host", "shareable"}
BLOCKED_COMPOSE_SERVICE_KEYS = BLOCKED_PRESENCE_KEYS | BLOCKED_TRUTHY_KEYS
BLOCKED_COMPOSE_TOP_KEYS = {"configs", "secrets"}
BLOCKED_NETWORK_MODES = {"host", "container", "service"}


def validate_compose_file(content: bytes) -> dict:
    """Parse and sandbox-validate a compose file. Returns parsed dict or raises HTTP 400."""
    if len(content) > MAX_COMPOSE_SIZE:
        raise HTTPException(400, f"Compose file too large (max {MAX_COMPOSE_SIZE // 1024}KB)")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise HTTPException(400, "Invalid YAML in compose file") from exc

    if not isinstance(data, dict):
        raise HTTPException(400, "Compose file must be a YAML mapping")

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
                raise HTTPException(400, f"Service '{svc_name}': '{key}' is not allowed for security reasons")

        for key in BLOCKED_TRUTHY_KEYS:
            if key in svc:
                val = svc[key]
                if val is True or (isinstance(val, (list, dict, str)) and val):
                    raise HTTPException(400, f"Service '{svc_name}': '{key}' is not allowed for security reasons")

        net_mode = str(svc.get("network_mode", ""))
        if any(net_mode.startswith(m) for m in BLOCKED_NETWORK_MODES):
            raise HTTPException(400, f"Service '{svc_name}': network_mode '{net_mode}' is not allowed")

        pid_mode = str(svc.get("pid", ""))
        if pid_mode == "host":
            raise HTTPException(400, f"Service '{svc_name}': pid mode 'host' is not allowed")

        ipc_mode = str(svc.get("ipc", ""))
        if ipc_mode in BLOCKED_IPC_MODES:
            raise HTTPException(400, f"Service '{svc_name}': ipc mode '{ipc_mode}' is not allowed")

        for vol in svc.get("volumes", []):
            vol_str = str(vol) if isinstance(vol, str) else vol.get("source", "") if isinstance(vol, dict) else str(vol)
            if vol_str.startswith(("/", "~", "..", "$")):
                raise HTTPException(400, f"Service '{svc_name}': host path mounts are not allowed")

        image = svc.get("image", "")
        if image:
            validate_image_registry(image)

    return data


def _sanitize_stderr(stderr: str) -> str:
    """Strip internal paths and hostnames from subprocess error output before returning to client."""
    sanitized = re.sub(r'(/[^\s:,\'"]+)', '[path]', stderr)
    sanitized = re.sub(r'\b([a-zA-Z0-9-]+\.){2,}[a-zA-Z]{2,}\b', '[host]', sanitized)
    return sanitized[:400].strip()
