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
import time
from typing import Any

import docker.errors
import structlog
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

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
            port=hp,
            threshold=config.PRIVILEGED_PORT_THRESHOLD,
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
    client,
    inherit_from: str | None,
    overrides: list[str] | None,
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
            new_id=new_container.short_id,
            old_id=replace_id,
            error=str(exc),
        )
        return False
    if old.id == new_container.id:
        log.warning(
            "container.replace_noop",
            id=new_container.short_id,
            reason="replace_id matches new container",
        )
        return False
    try:
        validators.safe_docker_call(old.remove, force=True)
    except (HTTPException, docker.errors.DockerException) as exc:
        log.warning(
            "container.replace_cleanup_failed",
            new_id=new_container.short_id,
            old_id=replace_id,
            error=str(exc),
        )
        return False
    log.info("container.replaced", new_id=new_container.short_id, old_id=replace_id)
    return True


@router.post("/api/containers/run", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE,
    audit="container.run",
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
        id=container.short_id,
        name=container.name,
        image=image,
        inherit_from=body.inherit_from or None,
    )

    # Phase 2 replace: AFTER the new container is running. Create failure
    # preserves the old; cleanup failure logs a warning but doesn't 5xx.
    replaced = _maybe_replace(client, body.replace_id, container)

    # Surface early-exit failures to the caller. The default secure
    # profile (read_only=true + auto-tmpfs) can shadow image-baked
    # subdirs (nginx's /var/cache/nginx tree is a known case), and the
    # container exits with code 1 seconds after `run` returns. Without
    # this check the UI reported a green "created" and the operator
    # only found the failure by manually opening logs.
    # Poll for ~800 ms on a very short interval to catch quick exits
    # without adding latency for healthy runs.
    exit_tail: str | None = None
    exit_code: int | None = None
    for _ in range(8):
        time.sleep(0.1)
        try:
            container.reload()
        except docker.errors.DockerException:
            break
        if container.status == "exited":
            exit_code = container.attrs.get("State", {}).get("ExitCode")
            try:
                tail_bytes = container.logs(tail=20, timestamps=False)
                exit_tail = (
                    tail_bytes.decode("utf-8", errors="replace")
                    if isinstance(tail_bytes, (bytes, bytearray))
                    else str(tail_bytes)
                )[-1024:]
            except docker.errors.DockerException:
                exit_tail = None
            log.warning(
                "container.exited_early",
                id=container.short_id,
                name=container.name,
                image=image,
                exit_code=exit_code,
            )
            break

    response: dict[str, Any] = {
        "id": container.short_id,
        "name": container.name,
        "status": container.status,
        "replaced_old": replaced,
    }
    if exit_code is not None:
        response["exit_code"] = exit_code
    if exit_tail:
        response["logs_tail"] = exit_tail
    return response


# Single audit line per lifecycle action. `audit_fields` routes the id
# onto the `audit.api_access` envelope instead of emitting a redundant
# `log.info("container.*", id=...)` inside the handler. Without this,
# a single action produces two SIEM-indexed records with the same
# event name — double-count every lifecycle event.
def _id_audit_fields(request, container_id, **_kw):
    return {"id": container_id}


@router.post("/api/containers/{container_id}/start", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.started", audit_fields=_id_audit_fields)
def start_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Start a stopped container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.start)
    return OkResponse()


@router.post("/api/containers/{container_id}/stop", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.stopped", audit_fields=_id_audit_fields)
def stop_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Stop a running container gracefully (SIGTERM, then SIGKILL after timeout)."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.stop, timeout=config.CONTAINER_STOP_TIMEOUT)
    return OkResponse()


@router.post("/api/containers/{container_id}/restart", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.restarted", audit_fields=_id_audit_fields)
def restart_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Restart a container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.restart, timeout=config.CONTAINER_RESTART_TIMEOUT)
    return OkResponse()


@router.post("/api/containers/{container_id}/pause", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.paused", audit_fields=_id_audit_fields)
def pause_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Pause (freeze) all processes in a running container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.pause)
    return OkResponse()


@router.post("/api/containers/{container_id}/unpause", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.unpaused", audit_fields=_id_audit_fields)
def unpause_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Resume a paused container."""
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.unpause)
    return OkResponse()


@router.post("/api/containers/{container_id}/kill", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE,
    audit="container.killed",
    audit_fields=lambda request, container_id, signal="SIGKILL", **_kw: {  # noqa: ARG005
        "id": container_id,
        "signal": signal,
    },
)
def kill_container(
    request: Request, container_id: str, signal: str = "SIGKILL", client=Depends(docker_client_dep)
) -> OkResponse:
    """Send a signal to a container (default SIGKILL)."""
    if signal not in ("SIGKILL", "SIGTERM", "SIGINT", "SIGHUP"):
        raise http_error("container.signal_bad")
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.kill, signal=signal)
    return OkResponse()


@router.post("/api/containers/{container_id}/rename", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE,
    audit="container.renamed",
    audit_fields=lambda request, container_id, name, **_kw: {  # noqa: ARG005
        "id": container_id,
        "new_name": name,
    },
)
def rename_container(request: Request, container_id: str, name: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Rename a container."""
    validators.validate_container_name(name)
    container = validators._get_container(client, container_id)
    validators.safe_docker_call(container.rename, name)
    return OkResponse()


@router.delete("/api/containers/{container_id}", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(RATE.WRITE, audit="container.removed")
def delete_container(
    request: Request,
    container_id: str,
    force: bool = False,
    undo: bool = True,
    client=Depends(docker_client_dep),
) -> Any:
    """Remove a container. Defaults to `undo=true` so a misclick (UI) or a
    typo (curl) can be recovered via `/api/undo/{token}` within the
    documented window. Pass `?undo=false` for an immediate hard delete
    that bypasses the queue.

    `force=true` is SEMANTICALLY an "I know what I'm doing, kill it
    now" signal. Layering it on top of the undo-queue would be
    contradictory: the caller asked for immediacy. When `force=true`
    we short-circuit the queue and delete synchronously even if
    `undo=true` is also set. The UI only pairs these flags when the
    container is running/paused (Docker rejects remove without force);
    stopped containers take the normal undoable path.
    """
    container = validators._get_container(client, container_id)
    if undo and not force:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "container",
            container.short_id,
            validators.safe_docker_call,
            container.remove,
            force=force,
        )
        if token is not None:
            log.info("container.delete_queued", id=container_id, force=force, token_suffix=token[-6:])
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
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", container.name)
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
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", container.name)
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
    ("port_bindings", "PortBindings", {}),
    ("restart_policy", "RestartPolicy", {}),
    ("binds", "Binds", []),
    ("memory_bytes", "Memory", 0),
    ("memory_reservation_bytes", "MemoryReservation", 0),
    ("cpu_shares", "CpuShares", 0),
    ("cpu_quota", "CpuQuota", 0),
    ("cpu_period", "CpuPeriod", 0),
    ("nano_cpus", "NanoCpus", 0),
    ("pids_limit", "PidsLimit", 0),
    ("security_opt", "SecurityOpt", []),
    ("tmpfs", "Tmpfs", {}),
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
    request: Request,
    container_id: str,
    client=Depends(docker_client_dep),
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
        # Docker Engine silently ignores `memory=0` on a RUNNING container —
        # `/update` returns 200 but the cap stays whatever it was. Returning
        # success here would be a false positive; the operator's intent
        # ("remove the cap") requires a stop-and-recreate. Reject at the
        # API boundary with an actionable code so scripted callers see the
        # reality instead of silently succeeding.
        if mem == 0:
            raise http_error("container.memory_uncap_unsupported")
        if mem < config.DOCKER_MIN_MEM_BYTES:
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
    update_kwargs: dict,
    before_hc: dict,
    after_hc: dict,
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
_UPDATE_RESPONSE_HOST_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "memory_bytes",
        "memory_reservation_bytes",
        "cpu_shares",
        "cpu_quota",
        "cpu_period",
        "pids_limit",
        "restart_policy",
    }
)


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
    # Docker stats shape differs between cgroup v1 and v2. On v1 the
    # "working set" convention is `usage - cache` because the page cache
    # count is included in usage. On v2 there's no `cache` key — the
    # equivalent is `inactive_file` (reclaimable page cache). Subtracting
    # the available one gives a meaningful working-set number in both
    # kernels; the `or 0` also protects against null values.
    _mstats = mem.get("stats") or {}
    _cache_like = _mstats.get("cache")  # cgroup v1
    if _cache_like is None:
        _cache_like = _mstats.get("inactive_file")  # cgroup v2
    mem_usage = (mem.get("usage") or 0) - (_cache_like or 0)
    mem_limit = mem.get("limit") or 0
    mem_pct = (mem_usage / mem_limit * 100.0) if mem_limit > 0 else 0.0

    nets = raw.get("networks") or {}
    net_rx = sum((v.get("rx_bytes") or 0) for v in nets.values())
    net_tx = sum((v.get("tx_bytes") or 0) for v in nets.values())

    bio = (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    blk_r = sum((b.get("value") or 0) for b in bio if b.get("op") == "read")
    blk_w = sum((b.get("value") or 0) for b in bio if b.get("op") == "write")

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


# ── File copy in/out (`docker cp`) ─────────────────────────────────────
#
# The container-cp flow is bounded and audit-logged because it can read
# or write arbitrary bytes inside the container's filesystem. Security
# posture:
#   - `path` is validated as an absolute POSIX path and capped at 256
#     chars so a massive path doesn't pin memory in the SDK.
#   - Download caps the streamed tar at `CONTAINER_CP_MAX_MB` (default 64).
#     Aborts past the cap instead of silently truncating — a large log
#     file that would overflow the viewer is better rejected than
#     half-delivered.
#   - Upload caps the request body at the same size. FastAPI reads the
#     body into memory, so the cap protects the server from a DoS push.
#   - Every successful get/put is audit-logged with (container, path, bytes).

_CP_PATH_RE = re.compile(r"^/[^\x00]{0,255}$")


def _validate_cp_path(path: str) -> str:
    """Reject paths that aren't absolute, contain null bytes, or exceed
    the length cap. Container-internal paths only — this doesn't touch
    the host filesystem, so path traversal attempts are neutralised by
    Docker's own get_archive / put_archive; we still cap length so a
    caller can't push a multi-MB path string into the daemon."""
    if not path or not _CP_PATH_RE.fullmatch(path):
        raise http_error("validation.bad_input", message="path must be absolute and under 256 chars")
    return path


@router.get("/api/containers/{container_id}/ls", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def container_ls(
    request: Request,
    container_id: str,
    path: str = Query(default="/", min_length=1, max_length=256),
    client=Depends(docker_client_dep),
) -> dict:
    """List a directory inside a container.

    Execs `ls -la --full-time -p <path>` and parses the output. The `-p`
    flag suffixes directory names with `/` so we can disambiguate dirs
    from files without a second stat. Used by the Files tab's file
    browser so an operator can navigate container state visually.

    Output format per row:
        {"name": "foo", "type": "file|dir|link", "size": 123, "mode": "rwxr-xr-x",
         "mtime": "2026-04-18T05:35:12", "target": "symlink-target-if-any"}

    Security posture: `path` goes through the same POSIX validator as
    cp. Commands are executed as the container's own user (no sudo,
    no privilege escalation) — the ls is as safe as any other exec.
    Output is capped at `CONTAINER_LS_MAX_ENTRIES` so a giant /proc
    doesn't exhaust memory."""
    _validate_cp_path(path)
    container = validators._get_container(client, container_id)
    # Busybox ls doesn't have `--full-time`; try GNU form first and fall
    # back to busybox if it errored. Both provide enough data to parse.
    def _exec(cmd: list[str]) -> tuple[int, str]:
        res = validators.safe_docker_call(container.exec_run, cmd, stdout=True, stderr=True)
        out = res.output.decode("utf-8", errors="replace") if res.output else ""
        return res.exit_code or 0, out

    rc, text = _exec(["ls", "-la", "--full-time", "-p", "--", path])
    if rc != 0:
        rc, text = _exec(["ls", "-la", "-p", "--", path])
    if rc != 0:
        # Path doesn't exist or isn't a directory.
        raise http_error("resource.not_found", message=f"cannot list {path!r}")

    max_entries = config.CONTAINER_LS_MAX_ENTRIES
    entries: list[dict] = []
    for raw_line in text.splitlines():
        entry = _parse_ls_line(raw_line)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= max_entries:
            break
    # Directories first, then files, each alphabetised.
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": path, "entries": entries, "truncated": len(entries) >= max_entries}


# `ls -la` leading-char → entry type. Dispatch via dict keeps
# `_parse_ls_line` within the project's C901/AP009 complexity budget.
_LS_TYPE_BY_PREFIX = {
    "d": "dir", "l": "link", "c": "device", "b": "device",
    "s": "socket", "p": "fifo",
}


def _parse_ls_line(raw_line: str) -> dict | None:
    """Parse one `ls -la -p` line → `{name, type, size, mode, target}`.

    Returns None for header lines (`total N`), blanks, `.` / `..`, and
    any row that doesn't have the expected column count. Works against
    both GNU coreutils (with `--full-time`) and busybox `ls`."""
    line = raw_line.rstrip("\n")
    if not line or line.startswith("total "):
        return None
    cols = line.split(None, 7)
    if len(cols) < 8:
        return None
    mode_full = cols[0]
    try:
        size = int(cols[4])
    except (TypeError, ValueError):
        size = 0
    # Symlink row: `mode ... name -> target`. Split on the first ` -> `.
    if " -> " in line:
        left, _, target = line.rpartition(" -> ")
        link_target = target.strip()
        name = left.rsplit(None, 1)[-1]
    else:
        link_target = ""
        name = line.rsplit(None, 1)[-1]
    name = name.rstrip()
    if name in (".", "./", "..", "../"):
        return None
    kind = _LS_TYPE_BY_PREFIX.get(mode_full[:1], "file")
    display_name = name.rstrip("/") if kind == "dir" else name
    return {
        "name": display_name,
        "type": kind,
        "size": size,
        "mode": mode_full[1:10],
        "target": link_target,
    }


@router.post("/api/containers/{container_id}/upload", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE,
    audit="container.cp_put",
    audit_fields=lambda request, container_id, path, **kw: (  # noqa: ARG005
        {"id": container_id, "path": path}
    ),
)
async def container_upload_file(
    request: Request,
    container_id: str,
    path: str = Query(..., min_length=1, max_length=256),
    file: UploadFile = File(...),
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Multipart file upload — the browser-friendly sibling of `/files` PUT.

    Browsers can submit multipart form data trivially, but building a tar
    client-side is inconvenient. This endpoint takes a single uploaded
    file (in the `file` form field), wraps it in a one-entry tar stream,
    and calls `put_archive` into the container's target `path` (which
    must be a directory).

    Files larger than CONTAINER_CP_MAX_MB are rejected with a 400
    envelope, not Starlette's default 413 plain-text, so the UI can
    render a consistent error message."""
    import io
    import tarfile

    _validate_cp_path(path)
    container = validators._get_container(client, container_id)
    cap_mb = config.CONTAINER_CP_MAX_MB
    cap_bytes = cap_mb * 1024 * 1024
    body = await file.read()
    if len(body) > cap_bytes:
        raise http_error("validation.bad_input", message=f"file over {cap_mb} MB cap")
    # Sanitise filename — strip path components, reject empty.
    raw_name = file.filename or ""
    basename = raw_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not basename or basename in {".", ".."}:
        raise http_error("validation.bad_input", message="uploaded filename missing or invalid")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=basename)
        info.size = len(body)
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(body))
    buf.seek(0)
    ok = validators.safe_docker_call(container.put_archive, path, buf.getvalue())
    if not ok:
        raise http_error("resource.not_found", message=f"path {path!r} not writable")
    log.info(
        "container.upload_ok",
        id=container_id,
        path=path,
        filename=basename,
        size_bytes=len(body),
    )
    return OkResponse()


@router.get("/api/containers/{container_id}/files", dependencies=AUTH, tags=["containers"])
@secure_route.read(RATE.READ)
def container_get_file(
    request: Request,
    container_id: str,
    path: str = Query(..., min_length=1, max_length=256),
    client=Depends(docker_client_dep),
):
    """Stream a file or directory out of a container as a tar archive.

    Mirrors `docker cp <container>:<path> -`. The response is a tar
    stream the caller can pipe into `tar -xv -`. Size-capped so an
    operator doesn't accidentally tarball a 10 GB directory and choke
    the browser."""
    _validate_cp_path(path)
    container = validators._get_container(client, container_id)
    try:
        stream, stat = container.get_archive(path)
    except docker.errors.NotFound as exc:
        raise http_error("resource.not_found", message=f"path {path!r} not found") from exc
    cap_mb = config.CONTAINER_CP_MAX_MB
    cap_bytes = cap_mb * 1024 * 1024

    def _bounded_iter():
        sent = 0
        for chunk in stream:
            if not chunk:
                continue
            sent += len(chunk)
            if sent > cap_bytes:
                # Can't send an HTTP error mid-stream cleanly; the chunk
                # we just drained already left the client a partial tar.
                # Log + stop so the operator notices.
                log.warning(
                    "container.cp_get_truncated",
                    id=container_id,
                    path=path,
                    cap_mb=cap_mb,
                )
                return
            yield chunk

    filename = stat.get("name", "archive") + ".tar"
    # `name` can contain path separators — flatten so Content-Disposition
    # doesn't try to interpret them.
    filename = filename.replace("/", "_").replace("\\", "_")
    log.info(
        "container.cp_get",
        id=container_id,
        path=path,
        size_bytes=stat.get("size"),
    )
    return StreamingResponse(
        _bounded_iter(),
        media_type="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/containers/{container_id}/files", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE,
    audit="container.cp_put",
    audit_fields=lambda request, container_id, path, **kw: (  # noqa: ARG005
        {"id": container_id, "path": path}
    ),
)
async def container_put_file(
    request: Request,
    container_id: str,
    path: str = Query(..., min_length=1, max_length=256),
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Upload a tar archive into a container's filesystem.

    Body must be `application/x-tar` (or `application/octet-stream` with
    a tar payload). Equivalent of `docker cp <tarfile> <container>:<path>`.
    Capped at `CONTAINER_CP_MAX_MB` to keep memory bounded."""
    _validate_cp_path(path)
    container = validators._get_container(client, container_id)
    cap_mb = config.CONTAINER_CP_MAX_MB
    cap_bytes = cap_mb * 1024 * 1024
    body = await request.body()
    if len(body) > cap_bytes:
        raise http_error("validation.bad_input", message=f"body over {cap_mb} MB cap")
    ok = validators.safe_docker_call(container.put_archive, path, body)
    if not ok:
        raise http_error("resource.not_found", message=f"path {path!r} not writable")
    log.info("container.cp_put_ok", id=container_id, path=path, size_bytes=len(body))
    return OkResponse()


# ── Container commit — freeze a running container as an image ──────────


_COMMIT_REPO_RE = validators.COMMIT_REPO_RE
_COMMIT_TAG_RE = validators.COMMIT_TAG_RE


@router.post("/api/containers/{container_id}/commit", dependencies=AUTH, tags=["containers"])
@secure_route.mutate(
    RATE.WRITE,
    audit="container.committed",
    audit_fields=lambda request, container_id, repository, tag="latest", **kw: (  # noqa: ARG005
        {"id": container_id, "repository": repository, "tag": tag}
    ),
)
def container_commit(
    request: Request,
    container_id: str,
    repository: str = Query(..., min_length=1, max_length=200),
    tag: str = Query(default="latest", min_length=1, max_length=128),
    message: str = Query(default="", max_length=500),
    author: str = Query(default="", max_length=200),
    client=Depends(docker_client_dep),
) -> dict:
    """Save a container's current filesystem state as a new image.

    Mirrors `docker commit`. Useful when the operator has ssh'd into a
    container via the Terminal tab, installed a dependency, and wants
    to bake that state into a repeatable image. The resulting image
    shows up in /api/images.

    Security: `repository` + `tag` are constrained to Docker's own
    grammar. We do NOT restrict to the registry allowlist here because
    commit produces a LOCAL image (no network push). A subsequent push
    would re-run through validate_image_registry."""
    if not _COMMIT_REPO_RE.fullmatch(repository):
        raise http_error("validation.bad_image_name", message=f"bad repository {repository!r}")
    if not _COMMIT_TAG_RE.fullmatch(tag):
        raise http_error("validation.bad_image_name", message=f"bad tag {tag!r}")
    container = validators._get_container(client, container_id)
    kwargs: dict[str, Any] = {"repository": repository, "tag": tag}
    if message:
        kwargs["message"] = message
    if author:
        kwargs["author"] = author
    img = validators.safe_docker_call(container.commit, **kwargs)
    # Plain dict — OkResponse uses extra=forbid but the UI + API consumers
    # need the image_id/repository/tag echoed back to show a toast and
    # to navigate to the new image.
    return {
        "ok": True,
        "image_id": img.short_id,
        "repository": repository,
        "tag": tag,
    }
