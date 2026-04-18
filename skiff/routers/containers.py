# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Container HTTP lifecycle routes (create / start / stop / inspect / stats / …).

WebSocket handlers — log streaming and the interactive exec shell —
live in `skiff.routers.containers_ws`. The two halves share only the
validators + auth modules; keeping them separate makes each file fit
in one read.
"""
from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

import docker.errors
import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from skiff import config, validators
from skiff.auth import AUTH  # decorator arg — direct import for readability
from skiff.contract.errors import http_error
from skiff.contract.requests import RunContainerRequest
from skiff.contract.responses import (
    ContainerInspectResponse,
    ContainerSummary,
    OkResponse,
    UndoableResponse,
    _ContainerConfigSection,
    _ContainerHealthSection,
    _ContainerHostConfigSection,
    _ContainerMountEntry,
    _ContainerNetworkEntry,
)
from skiff.docker_client import docker_client_dep
from skiff.rate import RATE
from skiff.secure import secure_route

log = structlog.get_logger(__name__)
router = APIRouter()


# ── Container routes ───────────────────────────────────────

@router.get("/api/containers", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def list_containers(request: Request, client=Depends(docker_client_dep)) -> list[ContainerSummary]:
    """Return all containers (running and stopped)."""
    containers = validators.safe_docker_call(client.containers.list, all=True)
    return [ContainerSummary.from_docker(c) for c in containers]


# ── run_container builder + helpers ──────────────────────────────────────────
# Each helper validates or builds one facet of the run-container payload.
# `run_container` is a linear composition of named steps, not a 170-line
# procedural wall of nested ifs. This also gives the McCabe complexity
# scanner a handler of ~5 branches instead of 20+ — and each helper is
# independently unit-testable.

# _PORT_RE / _ENV_KV_RE are handler-specific — they validate a single
# field shape on the run_container body, not an identifier shared with
# other resources. They stay inline here per the AP011 exemption for
# "used once, not a shared identifier".
_PORT_RE = re.compile(r"^\d{1,5}(/tcp|/udp)?$")
_ENV_KV_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*=")
# Label-key regex matches Docker's identifier shape — shared with
# container names (same ≤128-char alphanumeric+._- rule). Alias rather
# than redeclare.
_LABEL_KEY_RE = validators.LABEL_KEY_RE

# Static restart-policy → Docker-SDK shape. Unknown keys get rejected by
# `_build_restart_policy` (see `validation.bad_restart_policy`).
_RESTART_POLICY_SHAPES: dict[str, dict[str, Any]] = {
    "no": {},
    "on-failure": {"Name": "on-failure", "MaximumRetryCount": config.MAX_RESTART_RETRIES},
    "unless-stopped": {"Name": "unless-stopped"},
    "always": {"Name": "always"},
}


def _ensure_container_ref(value: str | None, field: str) -> None:
    """Raise 400 if `value` is set but not a valid container-ID hex string."""
    if value is not None and not validators.CONTAINER_ID_RE.fullmatch(value):
        raise http_error("validation.bad_input", message=f"Invalid {field} container ID")


def _extract_host_port(hport: Any) -> int | None:
    """Return the numeric host port from Docker SDK's port-mapping value.

    Docker SDK accepts: `int | str | (ip, port) tuple | None (publish all)`.
    Returns None when there's no numeric host port — nothing to check
    against the privileged-port threshold.
    """
    if isinstance(hport, (list, tuple)) and len(hport) == 2:
        hport = hport[1]
    if hport is None:
        return None
    try:
        return int(str(hport).split(":")[-1])
    except (ValueError, TypeError) as exc:
        raise http_error(
            "container.port_format",
            message=f"Invalid host port: {str(hport)[:20]}",
        ) from exc


def _reject_bad_container_port(cport: str) -> None:
    if not _PORT_RE.match(str(cport)):
        raise http_error(
            "container.port_format",
            message=f"Invalid container port format: {str(cport)[:20]}",
        )


def _reject_privileged_host_port(hport: Any) -> None:
    hp = _extract_host_port(hport)
    if hp is not None and hp < config.PRIVILEGED_PORT_THRESHOLD:
        raise http_error(
            "container.port_host_privileged",
            port=hp, threshold=config.PRIVILEGED_PORT_THRESHOLD,
        )


def _validate_ports(ports: dict[str, str] | None) -> None:
    """Reject oversize maps, bad container-port format, privileged host ports."""
    if not ports:
        return
    if len(ports) > config.MAX_PORT_MAPPINGS:
        raise http_error("container.port_count_exceeds_cap", limit=config.MAX_PORT_MAPPINGS)
    for cport, hport in ports.items():
        _reject_bad_container_port(cport)
        _reject_privileged_host_port(hport)


def _validate_env_entries(environment: list[str] | None) -> None:
    """Reject entries that aren't in KEY=VALUE shape."""
    for entry in environment or ():
        if "=" not in entry or not _ENV_KV_RE.match(entry):
            raise http_error(
                "validation.bad_env",
                message=f"Invalid environment variable format: {entry[:50]}. Use KEY=VALUE.",
            )


def _inherit_env(
    client, inherit_from: str | None, overrides: list[str] | None,
) -> list[str] | None:
    """Merge env from a source container with caller overrides (override wins).

    Zero-trust clone: source values are read off the container's
    Config.Env and forwarded to the new container without crossing the
    UI. Returns `overrides` unchanged when no inherit is requested.
    """
    if not inherit_from:
        return overrides
    source = validators._get_container(client, inherit_from)
    inherited = source.attrs.get("Config", {}).get("Env", []) or []
    if not overrides:
        return list(inherited)
    override_keys = {e.split("=", 1)[0] for e in overrides}
    return [e for e in inherited if e.split("=", 1)[0] not in override_keys] + list(overrides)


def _parse_volume_spec(spec: str) -> tuple[str, dict[str, str]]:
    """Return `(name, {bind, mode})` for one `name:/path[:ro|rw]` entry.

    Raises on: missing colon, host-path escape attempts, disallowed mount
    targets, malformed volume name. `mode` defaults to `rw` when absent
    or unrecognised.
    """
    if ":" not in spec:
        raise http_error(
            "container.volume_format",
            message=f"Invalid volume format: {spec[:50]}. Use name:/path.",
        )
    parts = spec.split(":", 2)
    vol_name, mount_path = parts[0], parts[1]
    validators._validate_mount_target(mount_path)
    if vol_name.startswith(("/", "~", "..", "$")):
        raise http_error("container.volume_host_path_blocked")
    name_re = rf"^[a-zA-Z0-9][a-zA-Z0-9_.-]{{0,{config.MAX_VOLUME_NAME_LENGTH}}}$"
    if not re.match(name_re, vol_name):
        raise http_error(
            "container.volume_format",
            message=f"Invalid volume name: {vol_name[:50]}",
        )
    mode = parts[2] if len(parts) > 2 and parts[2] in ("ro", "rw") else "rw"
    return vol_name, {"bind": mount_path, "mode": mode}


def _build_volume_binds(volumes: list[str] | None) -> dict[str, dict[str, str]]:
    """Turn a list of volume spec strings into a Docker SDK binds dict."""
    return dict(_parse_volume_spec(v) for v in (volumes or ()))


def _build_restart_policy(name: str | None) -> dict[str, Any]:
    """Map a restart-policy name to a Docker SDK restart_policy dict."""
    key = name or "no"
    if key not in _RESTART_POLICY_SHAPES:
        raise http_error("validation.bad_restart_policy")
    return _RESTART_POLICY_SHAPES[key]


_LABEL_VALUE_MAX = 4096
_LABEL_COUNT_MAX = 50


def _reject_bad_label(lk: str, lv: str) -> None:
    if not _LABEL_KEY_RE.match(lk):
        raise http_error("container.label_bad", message=f"Invalid label key: {lk[:50]}")
    if len(str(lv)) > _LABEL_VALUE_MAX:
        raise http_error(
            "container.label_bad",
            message=f"Label value too long for key: {lk[:50]} (max {_LABEL_VALUE_MAX} chars)",
        )


def _validate_labels(labels: dict[str, str] | None) -> None:
    """Reject oversize maps (>50), bad keys, or values > 4096 chars."""
    if not labels:
        return
    if len(labels) > _LABEL_COUNT_MAX:
        raise http_error("container.label_count_exceeds_cap", limit=_LABEL_COUNT_MAX)
    for lk, lv in labels.items():
        _reject_bad_label(lk, lv)


def _resolve_tmpfs(tmpfs: dict[str, str] | None, read_only: bool) -> dict[str, str]:
    """Pick the effective tmpfs map.

      - `tmpfs=None` + `read_only=True`  → default runtime dirs from TOML
        (so stock nginx / redis / haproxy / postgres initdb images boot).
      - `tmpfs=None` + `read_only=False` → no tmpfs mounts.
      - Explicit dict (even `{}`)        → caller knows best; validate shape.
    """
    if tmpfs is None:
        return dict(config.DEFAULT_TMPFS) if read_only else {}
    validators._validate_tmpfs(tmpfs, config.MAX_TMPFS_MOUNTS, config.MAX_TMPFS_SIZE_MB)
    return tmpfs


class _RunKwargsBuilder:
    """Accumulate Docker SDK `containers.run` kwargs as a sequence of steps.

    Each `with_X` method validates its facet, stores the canonical form,
    and returns `self` so the caller chains the pipeline top-to-bottom.
    `build()` flattens the accumulated state into the final kwargs dict.

    Isolating each facet as a method lets:
      - unit tests exercise one concern at a time
      - McCabe treat the handler as ~5 straight-line calls, not 20 nested ifs
      - a reader follow the algorithm by reading method names, not by
        tracing indentation
    """

    def __init__(self, body: RunContainerRequest, name: str | None) -> None:
        self.body = body
        self.name = name
        self._kwargs: dict[str, Any] = {
            "name": name,
            "ports": body.ports,
            "detach": True,
            "mem_limit": config.MAX_CONTAINER_MEM,
            "nano_cpus": int(config.MAX_CONTAINER_CPU * 1e9),
            "security_opt": ["no-new-privileges:true"],
            "read_only": body.read_only,
        }

    def with_environment(self, client) -> _RunKwargsBuilder:
        self._kwargs["environment"] = _inherit_env(client, self.body.inherit_from, self.body.environment)
        return self

    def with_tmpfs(self) -> _RunKwargsBuilder:
        effective = _resolve_tmpfs(self.body.tmpfs, self.body.read_only)
        if effective:
            self._kwargs["tmpfs"] = effective
        return self

    def with_command(self) -> _RunKwargsBuilder:
        cmd = self.body.command
        if not cmd:
            return self
        if len(cmd) > 4096:
            raise http_error("container.command_too_long", limit=4096)
        self._kwargs["command"] = cmd
        return self

    def with_volumes(self) -> _RunKwargsBuilder:
        binds = _build_volume_binds(self.body.volumes)
        if binds:
            self._kwargs["volumes"] = binds
        return self

    def with_restart_policy(self) -> _RunKwargsBuilder:
        rp = _build_restart_policy(self.body.restart_policy)
        if self.body.restart_policy and self.body.restart_policy != "no":
            self._kwargs["restart_policy"] = rp
        return self

    def with_network(self) -> _RunKwargsBuilder:
        if self.body.network:
            self._kwargs["network"] = self.body.network
        return self

    def with_labels(self) -> _RunKwargsBuilder:
        if self.body.labels:
            self._kwargs["labels"] = self.body.labels
        return self

    def build(self) -> dict[str, Any]:
        return self._kwargs


def _maybe_replace(client, replace_id: str | None, new_container) -> bool:
    """Stop+remove the container referenced by `replace_id`. Safe on failure.

    Called AFTER the new container is running. Cleanup errors log a
    warning but don't 5xx — the caller has a live container and can
    retry the cleanup manually. Refuses to remove the new container
    if `replace_id` happens to match (belt-and-braces).
    """
    if not replace_id:
        return False
    try:
        old = validators._get_container(client, replace_id)
    except (HTTPException, docker.errors.DockerException) as exc:
        log.warning(
            "container.replace_cleanup_failed",
            new_id=new_container.short_id, old_id=replace_id, error=str(exc),
        )
        return False
    if old.id == new_container.id:
        log.warning(
            "container.replace_noop", id=new_container.short_id,
            reason="replace_id matches new container",
        )
        return False
    try:
        validators.safe_docker_call(old.remove, force=True)
    except (HTTPException, docker.errors.DockerException) as exc:
        log.warning(
            "container.replace_cleanup_failed",
            new_id=new_container.short_id, old_id=replace_id, error=str(exc),
        )
        return False
    log.info("container.replaced", new_id=new_container.short_id, old_id=replace_id)
    return True


@router.post("/api/containers/run", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE, audit="container.run",
    audit_fields=lambda request, image, name=None, **kw: {"image": image, "name": name or ""},  # noqa: ARG005
)
def run_container(
    request: Request,
    image: str,
    name: str | None = None,
    body: RunContainerRequest = Body(default_factory=RunContainerRequest),
    client=Depends(docker_client_dep),
) -> dict:
    """Create and start a new container.

    Linear pipeline: validate → enforce global cap → build kwargs → create
    → optionally replace. Each validator / builder is a named helper above;
    the handler itself fits on one screen.

    Phase 2 clone-to-recreate:
      - `body.inherit_from` → zero-trust env merge from a source container
        (values stay server-side — never round-trip through the UI).
      - `body.replace_id`   → stop+remove the old container AFTER the new
        one is successfully running. Create failure preserves the old.
    """
    # Input validation (each helper raises http_error on bad input).
    validators.validate_image_registry(image)
    validators.validate_container_name(name)
    _ensure_container_ref(body.inherit_from, "inherit_from")
    _ensure_container_ref(body.replace_id, "replace_id")
    _validate_ports(body.ports)
    _validate_env_entries(body.environment)
    _validate_labels(body.labels)
    if body.network and not validators.NETWORK_NAME_RE.fullmatch(body.network):
        raise http_error("network.bad_name")

    # Global container-count cap — read live from the engine.
    if len(client.containers.list(all=True)) >= config.MAX_CONTAINERS:
        raise http_error("container.limit_reached", limit=config.MAX_CONTAINERS)

    run_kwargs = (
        _RunKwargsBuilder(body, name)
        .with_environment(client)
        .with_tmpfs()
        .with_command()
        .with_volumes()
        .with_restart_policy()
        .with_network()
        .with_labels()
        .build()
    )

    container = validators.safe_docker_call(client.containers.run, image, **run_kwargs)
    log.info(
        "container.created",
        id=container.short_id, name=container.name, image=image,
        inherit_from=body.inherit_from or None,
    )

    # Phase 2 replace: AFTER the new container is running. Create failure
    # preserves the old; cleanup failure logs a warning but doesn't 5xx.
    replaced = _maybe_replace(client, body.replace_id, container)

    return {
        "id": container.short_id,
        "name": container.name,
        "status": container.status,
        "replaced_old": replaced,
    }


@router.post("/api/containers/{container_id}/start", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.started")
def start_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Start a stopped container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.start)
    log.info("container.started", id=container_id)
    return OkResponse()


@router.post("/api/containers/{container_id}/stop", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.stopped")
def stop_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Stop a running container gracefully (SIGTERM, then SIGKILL after timeout)."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.stop, timeout=config.CONTAINER_STOP_TIMEOUT)
    log.info("container.stopped", id=container_id)
    return OkResponse()


@router.post("/api/containers/{container_id}/restart", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.restarted")
def restart_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Restart a container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.restart, timeout=config.CONTAINER_RESTART_TIMEOUT)
    log.info("container.restarted", id=container_id)
    return OkResponse()


@router.post("/api/containers/{container_id}/pause", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.paused")
def pause_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Pause (freeze) all processes in a running container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.pause)
    log.info("container.paused", id=container_id)
    return OkResponse()


@router.post("/api/containers/{container_id}/unpause", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.unpaused")
def unpause_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Resume a paused container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.unpause)
    log.info("container.unpaused", id=container_id)
    return OkResponse()


@router.post("/api/containers/{container_id}/kill", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.killed")
def kill_container(
    request: Request, container_id: str, signal: str = "SIGKILL", client=Depends(docker_client_dep)
) -> OkResponse:
    """Send a signal to a container (default SIGKILL)."""
    if signal not in ("SIGKILL", "SIGTERM", "SIGINT", "SIGHUP"):
        raise http_error("container.signal_bad")
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.kill, signal=signal)
    log.info("container.killed", id=container_id, signal=signal)
    return OkResponse()


@router.post("/api/containers/{container_id}/rename", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.renamed")
def rename_container(request: Request, container_id: str, name: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Rename a container."""
    validators.validate_container_name(name)
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.rename, name)
    log.info("container.renamed", id=container_id, new_name=name)
    return OkResponse()


@router.delete("/api/containers/{container_id}", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.removed")
def delete_container(
    request: Request, container_id: str, force: bool = False,
    undo: bool = False, client=Depends(docker_client_dep),
) -> Any:
    """Remove a container. If `undo=true`, the removal is delayed by 5 seconds
    and the response includes an `undo_token` that can be POSTed to
    /api/undo/{token} to cancel. After the window elapses the removal runs
    unconditionally. If the undo queue is full, we fall back to synchronous
    removal so the caller never gets a silent no-op.
    """
    container = validators._get_container(client, container_id)
    if undo:
        from skiff.undo import get_queue
        token = get_queue().enqueue(
            "container", container.short_id,
            validators.safe_docker_call, container.remove, force=force,
        )
        if token is not None:
            log.info("container.delete_queued", id=container_id, force=force,
                     token_suffix=token[-6:])
            return UndoableResponse(undo_token=token, expires_in=config.UNDO_DELAY_SECS)
        # Queue full → fall through to synchronous removal
    validators.safe_docker_call(container.remove, force=force)
    log.info("container.deleted", id=container_id, force=force)
    return OkResponse()


def _log_window_kwargs(tail: int, since: str, until: str) -> dict[str, Any]:
    """Compose Docker SDK `container.logs(**kwargs)` for the three log endpoints.

    The empty-string defaults on the Query params mean "no filter", so we
    drop them from the kwargs instead of letting Docker interpret an
    empty since/until. Keeps each log handler at CC=1 instead of
    conditional-dict-assign chains in three places.
    """
    kwargs: dict[str, Any] = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    return kwargs


@router.get("/api/containers/{container_id}/logs", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def container_logs(
    request: Request,
    container_id: str,
    tail: int = Query(default=200, le=config.MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
) -> dict:
    """Fetch container log lines with optional time-range filtering."""
    container = validators._get_container(client, container_id)
    logs = validators.safe_docker_call(container.logs, **_log_window_kwargs(tail, since, until))
    return {"logs": logs.decode(errors="replace")}


@router.get("/api/containers/{container_id}/logs/download", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.AUTH_SENSITIVE)  # large response — low limit
def download_container_logs(
    request: Request,
    container_id: str,
    tail: int = Query(default=5000, le=config.MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
):
    """Download container logs as plain text. Auth via Authorization header."""
    container = validators._get_container(client, container_id)
    logs = validators.safe_docker_call(container.logs, **_log_window_kwargs(tail, since, until))
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', container.name)
    return PlainTextResponse(
        content=logs.decode(errors="replace"),
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-logs.txt"'},
    )


@router.get("/api/containers/{container_id}/logs/download.jsonl", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.AUTH_SENSITIVE)  # large response — low limit
def download_container_logs_jsonl(
    request: Request,
    container_id: str,
    tail: int = Query(default=5000, le=config.MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
):
    """Download container logs as JSONL (one JSON object per line with timestamp + message)."""
    import json
    container = validators._get_container(client, container_id)
    logs = validators.safe_docker_call(container.logs, **_log_window_kwargs(tail, since, until))
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


# ── inspect_container builders ───────────────────────────────────────────────
# Each builder takes the raw Docker SDK attrs dict and returns a Pydantic
# submodel from skiff.contract.responses. The server assembles the full
# validated payload so the UI doesn't JOIN engine-level fields client-side.


def _build_config_section(cfg: dict) -> _ContainerConfigSection:
    return _ContainerConfigSection(
        env=validators._redact_env(cfg.get("Env", [])),
        cmd=cfg.get("Cmd"),
        entrypoint=cfg.get("Entrypoint"),
        labels=cfg.get("Labels") or {},
        exposed_ports=list((cfg.get("ExposedPorts") or {}).keys()),
        working_dir=cfg.get("WorkingDir") or "",
        user=cfg.get("User") or "",
        hostname=cfg.get("Hostname") or "",
        tty=cfg.get("Tty", False),
    )


# HostConfig flattening map: model-field → (Docker attr key, default).
# Declaring the map once makes `_build_host_config_section` a dict comp.
_HOST_CONFIG_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("port_bindings",             "PortBindings",        {}),
    ("restart_policy",             "RestartPolicy",       {}),
    ("binds",                      "Binds",               []),
    ("memory_bytes",               "Memory",              0),
    ("memory_reservation_bytes",   "MemoryReservation",   0),
    ("cpu_shares",                 "CpuShares",           0),
    ("cpu_quota",                  "CpuQuota",            0),
    ("cpu_period",                 "CpuPeriod",           0),
    ("nano_cpus",                  "NanoCpus",            0),
    ("pids_limit",                 "PidsLimit",           0),
    ("security_opt",               "SecurityOpt",         []),
    ("tmpfs",                      "Tmpfs",               {}),
)


def _build_host_config_section(hc: dict) -> _ContainerHostConfigSection:
    fields = {key: hc.get(docker_key) or default for key, docker_key, default in _HOST_CONFIG_FIELDS}
    fields["readonly_rootfs"] = bool(hc.get("ReadonlyRootfs", False))
    return _ContainerHostConfigSection(**fields)


def _build_health_section(cfg: dict, health_raw: dict) -> _ContainerHealthSection:
    if not (health_raw or cfg.get("Healthcheck")):
        return _ContainerHealthSection()
    return _ContainerHealthSection(
        status=health_raw.get("Status", "none"),
        failing_streak=health_raw.get("FailingStreak", 0),
        test=(cfg.get("Healthcheck") or {}).get("Test"),
        log=(health_raw.get("Log") or [])[-3:],
    )


def _build_network_section(attrs: dict) -> dict[str, _ContainerNetworkEntry]:
    networks = (attrs.get("NetworkSettings") or {}).get("Networks", {})
    return {
        net: _ContainerNetworkEntry(
            ip_address=info.get("IPAddress") or "",
            gateway=info.get("Gateway") or "",
            mac_address=info.get("MacAddress") or "",
        )
        for net, info in networks.items()
    }


def _build_mounts_section(attrs: dict) -> list[_ContainerMountEntry]:
    return [
        _ContainerMountEntry(
            type=m.get("Type") or "",
            name=m.get("Name") or "",
            source=m.get("Source") or "",
            destination=m.get("Destination") or "",
            mode=m.get("Mode") or "",
            rw=m.get("RW", True),
        )
        for m in attrs.get("Mounts", []) or ()
    ]


@router.get("/api/containers/{container_id}/inspect", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def inspect_container(
    request: Request, container_id: str, client=Depends(docker_client_dep),
) -> ContainerInspectResponse:
    """Return detailed container metadata (config, state, mounts, network, health)."""
    container = validators._get_container(client, container_id)
    attrs = container.attrs
    hc = attrs.get("HostConfig", {}) or {}
    cfg = attrs.get("Config", {}) or {}
    state = attrs.get("State", {}) or {}
    return ContainerInspectResponse(
        id=attrs["Id"][:12],
        name=attrs["Name"].lstrip("/"),
        image=cfg.get("Image") or "",
        created=attrs["Created"],
        state=state,
        restart_count=attrs.get("RestartCount", 0),
        platform=attrs.get("Platform") or "",
        config=_build_config_section(cfg),
        host_config=_build_host_config_section(hc),
        health_check=_build_health_section(cfg, state.get("Health", {}) or {}),
        network=_build_network_section(attrs),
        mounts=_build_mounts_section(attrs),
    )


# Live-updatable container resources. Mirrors Docker Engine API /containers/{id}/update
# but filters to a safe subset and accepts GCP/Kubernetes-style unit strings.
# Cap enforcement: values cannot exceed the global MAX_CONTAINER_* thresholds even
# if the container was originally created with a higher limit out-of-band.
# _MAX_MEM_BYTES is computed once at import so every request uses the same ceiling.
_MAX_MEM_BYTES = validators.parse_memory_quantity(config.MAX_CONTAINER_MEM)


# ── update_container helpers ─────────────────────────────────────────────────
# Each accessor on `_UpdateFieldValidator` handles one tunable resource
# (memory / cpus / cpu_shares / pids_limit / restart_policy). `apply(...)`
# runs through every validator that has a non-None value and mutates the
# shared `update_kwargs` dict passed at construction. This mirrors the
# builder pattern used by `_RunKwargsBuilder` for `run_container` — the
# handler body ends up as a linear sequence of named steps.

# Map of "kwarg key we set on the Docker SDK call" → "HostConfig key Docker
# returns in attrs". The update handler's audit diff uses this table to
# emit before/after pairs for only the fields the caller actually touched.
_UPDATE_KWARG_TO_HOSTCONFIG = {
    "mem_limit": "Memory",
    "mem_reservation": "MemoryReservation",
    "cpu_shares": "CpuShares",
    "cpu_quota": "CpuQuota",
    "cpu_period": "CpuPeriod",
    "pids_limit": "PidsLimit",
}


class _UpdateFieldValidator:
    """Apply `/containers/{id}/update` fields into a kwargs dict, one at a time.

    Each `_apply_X` method validates one resource and mutates the shared
    dict. Public `apply(**values)` dispatches to the right method for
    every non-None value. Splitting the 130-line handler this way lets
    each tunable be unit-tested in isolation (pass a dict and a value,
    observe what lands in the dict or what error_code raises).
    """

    def __init__(self, update_kwargs: dict) -> None:
        self.k = update_kwargs  # shared mutable dict

    def apply(self, **values) -> None:
        dispatch = {
            "memory": self._apply_memory,
            "memory_reservation": self._apply_memory_reservation,
            "cpus": self._apply_cpus,
            "cpu_shares": self._apply_cpu_shares,
            "pids_limit": self._apply_pids_limit,
            "restart_policy": self._apply_restart_policy,
        }
        for name, value in values.items():
            if value is None:
                continue
            dispatch[name](value)

    def _apply_memory(self, memory: str | int) -> None:
        mem = validators.parse_memory_quantity(memory)
        if mem and mem < config.DOCKER_MIN_MEM_BYTES:
            raise http_error("container.memory_below_minimum", minimum=config.DOCKER_MIN_MEM_BYTES)
        if mem > _MAX_MEM_BYTES:
            raise http_error(
                "container.memory_above_cap",
                cap=f"{config.MAX_CONTAINER_MEM} ({_MAX_MEM_BYTES} bytes)",
            )
        self.k["mem_limit"] = mem

    def _apply_memory_reservation(self, memory_reservation: str | int) -> None:
        res = validators.parse_memory_quantity(memory_reservation)
        if res > _MAX_MEM_BYTES:
            raise http_error(
                "container.memory_above_cap",
                cap=config.MAX_CONTAINER_MEM,
                message=f"memory_reservation exceeds cap of {config.MAX_CONTAINER_MEM}",
            )
        self.k["mem_reservation"] = res

    def _apply_cpus(self, cpus: str | float) -> None:
        parsed = validators.parse_cpu_quantity(cpus)
        if parsed <= 0:
            raise http_error("validation.bad_cpu", message="cpus must be > 0")
        if parsed > config.MAX_CONTAINER_CPU:
            raise http_error("container.cpu_above_cap", cap=config.MAX_CONTAINER_CPU)
        # Docker SDK update() takes cpu_period / cpu_quota. Default 100_000 us
        # period means quota = cpus * period microseconds.
        self.k["cpu_period"] = 100_000
        self.k["cpu_quota"] = int(parsed * 100_000)

    def _apply_cpu_shares(self, cpu_shares: int) -> None:
        if not isinstance(cpu_shares, int) or not (2 <= cpu_shares <= 1024):
            raise http_error("container.cpu_shares_bad")
        self.k["cpu_shares"] = cpu_shares

    def _apply_pids_limit(self, pids_limit: int) -> None:
        if not isinstance(pids_limit, int) or not (1 <= pids_limit <= config.MAX_PIDS_LIMIT):
            raise http_error("container.pids_limit_bad", cap=config.MAX_PIDS_LIMIT)
        self.k["pids_limit"] = pids_limit

    def _apply_restart_policy(self, restart_policy: dict) -> None:
        if not isinstance(restart_policy, dict):
            raise http_error("container.restart_policy_shape")
        name = restart_policy.get("Name", "")
        if name not in config.VALID_RESTART_POLICIES:
            raise http_error(
                "validation.bad_restart_policy",
                message=f"restart_policy.Name must be one of {sorted(config.VALID_RESTART_POLICIES)}",
            )
        rp: dict[str, Any] = {"Name": name}
        if name == "on-failure":
            retries = restart_policy.get("MaximumRetryCount", config.MAX_RESTART_RETRIES)
            if not isinstance(retries, int) or not (0 <= retries <= config.MAX_RESTART_RETRIES):
                raise http_error("container.restart_retry_bad", cap=config.MAX_RESTART_RETRIES)
            rp["MaximumRetryCount"] = retries
        self.k["restart_policy"] = rp


def _diff_update_fields(
    update_kwargs: dict, before_hc: dict, after_hc: dict,
) -> dict[str, dict[str, Any]]:
    """Return `{HostConfigKey: {before, after}}` for every kwarg the caller set."""
    changes: dict[str, dict[str, Any]] = {
        hc_key: {"before": before_hc.get(hc_key), "after": after_hc.get(hc_key)}
        for kwarg_key, hc_key in _UPDATE_KWARG_TO_HOSTCONFIG.items()
        if kwarg_key in update_kwargs
    }
    if "restart_policy" in update_kwargs:
        changes["RestartPolicy"] = {
            "before": before_hc.get("RestartPolicy"),
            "after": after_hc.get("RestartPolicy"),
        }
    return changes


# Fields returned in the /update response's host_config block. Subset of
# `_ContainerHostConfigSection` — only the live-updatable surface, because
# the caller just mutated those specific fields.
_UPDATE_RESPONSE_HOST_CONFIG_FIELDS: frozenset[str] = frozenset({
    "memory_bytes", "memory_reservation_bytes",
    "cpu_shares", "cpu_quota", "cpu_period",
    "pids_limit", "restart_policy",
})


def _flatten_host_config(host_config: dict) -> dict[str, Any]:
    """Return the live-updatable subset of HostConfig for the /update response.

    Delegates the attrs-dict unpacking to the shared Pydantic builder
    (`_build_host_config_section`) so /update and /inspect share a single
    normalization path. Filters to the mutable subset so the response
    reflects only what /update can actually change.
    """
    section = _build_host_config_section(host_config)
    return section.model_dump(include=_UPDATE_RESPONSE_HOST_CONFIG_FIELDS)


@router.post("/api/containers/{container_id}/update", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.updated")
def update_container(
    request: Request,
    container_id: str,
    memory: str | int | None = Body(default=None),
    memory_reservation: str | int | None = Body(default=None),
    cpus: str | float | None = Body(default=None),
    cpu_shares: int | None = Body(default=None),
    pids_limit: int | None = Body(default=None),
    restart_policy: dict | None = Body(default=None),
    client=Depends(docker_client_dep),
) -> dict:
    """Update a running or stopped container's live-mutable resource constraints.

    Payload mirrors the Docker Engine API /containers/{id}/update (v1.47) shape but
    accepts GCP/Kubernetes-style quantity strings:
      - `memory`, `memory_reservation`: int (bytes), or str like "256Mi", "1Gi", "500M"
      - `cpus`: number, or str like "0.5", "500m", "2"
      - `cpu_shares`: raw int (2-1024; Docker weight, not fractional CPU)
      - `pids_limit`: int (1..4096)
      - `restart_policy`: {"Name": "on-failure"|"unless-stopped"|"always"|"no",
                          "MaximumRetryCount": int}

    Caps: memory ≤ config.MAX_CONTAINER_MEM, cpus ≤ config.MAX_CONTAINER_CPU, pids ≤ 4096,
    retry count ≤ config.MAX_RESTART_RETRIES. Immutable params (ports, volumes, env,
    network, command, read_only, tmpfs) cannot be changed here — use Clone & Run.
    """
    container = validators._get_container(client, container_id)
    # Snapshot the current host-config so the audit entry can carry before→after.
    # Deep copy: attrs["HostConfig"] is a live reference the SDK may rewrite
    # in place on container.reload(), which would make "before" silently track
    # the "after" state.
    before_hc = copy.deepcopy(container.attrs.get("HostConfig", {}) or {})

    update_kwargs: dict = {}
    _UpdateFieldValidator(update_kwargs).apply(
        memory=memory,
        memory_reservation=memory_reservation,
        cpus=cpus,
        cpu_shares=cpu_shares,
        pids_limit=pids_limit,
        restart_policy=restart_policy,
    )
    if not update_kwargs:
        raise http_error("container.update_no_fields")

    validators.safe_docker_call(container.update, **update_kwargs)
    # Re-fetch to capture the actual post-update state (Docker may round period/quota).
    container.reload()
    after_hc = container.attrs.get("HostConfig", {}) or {}
    changes = _diff_update_fields(update_kwargs, before_hc, after_hc)
    log.info("container.updated", id=container.short_id, name=container.name, changes=changes)
    return {
        "id": container.short_id,
        "name": container.name,
        "updated": sorted(changes.keys()),
        "host_config": _flatten_host_config(after_hc),
    }


@router.get("/api/containers/{container_id}/stats", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
async def container_stats(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Return real-time CPU, memory, network, and disk I/O stats."""

    container = validators._get_container(client, container_id)
    try:
        loop = asyncio.get_running_loop()
        raw = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: container.stats(stream=False)),
            timeout=config.CONTAINER_STATS_TIMEOUT,
        )
    except TimeoutError as exc:
        raise http_error("container.stats_timeout") from exc
    # CPU delta calculation
    cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    sys_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
    num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
    cpu_pct = (cpu_delta / sys_delta * num_cpus * 100.0) if sys_delta > 0 else 0.0

    mem = raw.get("memory_stats", {})
    mem_usage = mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)
    mem_limit = mem.get("limit", 0)
    mem_pct = (mem_usage / mem_limit * 100.0) if mem_limit > 0 else 0.0

    nets = raw.get("networks", {})
    net_rx = sum(v.get("rx_bytes", 0) for v in nets.values())
    net_tx = sum(v.get("tx_bytes", 0) for v in nets.values())

    bio = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    blk_r = sum(b.get("value", 0) for b in bio if b.get("op") == "read")
    blk_w = sum(b.get("value", 0) for b in bio if b.get("op") == "write")

    return {
        "cpu_percent": round(cpu_pct, 2),
        "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
        "mem_percent": round(mem_pct, 2),
        "net_rx_mb": round(net_rx / 1024 / 1024, 3),
        "net_tx_mb": round(net_tx / 1024 / 1024, 3),
        "blk_read_mb": round(blk_r / 1024 / 1024, 3),
        "blk_write_mb": round(blk_w / 1024 / 1024, 3),
    }


@router.get("/api/containers/{container_id}/top", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def container_top(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """List processes running inside a container (like docker top)."""
    container = validators._get_container(client, container_id)
    result = validators.safe_docker_call(container.top)
    return {"titles": result.get("Titles", []), "processes": result.get("Processes", [])}


@router.get("/api/containers/{container_id}/diff", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def container_diff(request: Request, container_id: str, client=Depends(docker_client_dep)) -> list[dict]:
    """Show filesystem changes in a container's writable layer since it was created."""
    container = validators._get_container(client, container_id)
    changes = validators.safe_docker_call(container.diff) or []
    kind_map = {0: "Modified", 1: "Added", 2: "Deleted"}
    return [{"path": c.get("Path", ""), "kind": kind_map.get(c.get("Kind", 0), "unknown")} for c in changes]

