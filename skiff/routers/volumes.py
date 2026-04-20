# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Named volume management routes."""

from __future__ import annotations

import secrets
from typing import Any

import docker.errors
import structlog
from fastapi import APIRouter, Depends, Request

from skiff import config, validators
from skiff.auth import AUTH
from skiff.contract.errors import http_error
from skiff.contract.responses import (
    OkResponse,
    UndoableResponse,
    VolumeInspectResponse,
    VolumeSummary,
)
from skiff.docker_client import docker_client_dep
from skiff.rate import RATE
from skiff.secure import secure_route
from skiff.validators import safe_docker_call

log = structlog.get_logger(__name__)

router = APIRouter()


# Volume-name regex sourced from skiff.validators.VOLUME_NAME_RE so the
# identifier rules live in one place. Local alias keeps call sites short.
_VOLUME_NAME_RE = validators.VOLUME_NAME_RE


def _index_containers_by_volume(client) -> dict[str, list[str]]:
    """Map `volume_name → [container_name, …]` for all attached mounts.

    Non-fatal: an engine error here yields an empty index — the volume
    list is still returned, just without in-use data.
    """
    index: dict[str, list[str]] = {}
    try:
        containers = client.containers.list(all=True)
    except docker.errors.DockerException:
        return index
    for c in containers:
        for m in c.attrs.get("Mounts", []) or ():
            vol_name = m.get("Name")
            if vol_name:
                index.setdefault(vol_name, []).append(c.name)
    return index


@router.get("/api/volumes", dependencies=AUTH, tags=["volumes"])
@secure_route.read(RATE.READ)
def list_volumes(request: Request, client=Depends(docker_client_dep)) -> list[VolumeSummary]:
    """Return all named volumes and which containers are using each one."""
    volumes = safe_docker_call(client.volumes.list)
    index = _index_containers_by_volume(client)
    return [VolumeSummary.from_docker(v, index.get(v.name, [])) for v in volumes]


def _container_uses_volume(container, volume_name: str) -> bool:
    """True if the container has any mount whose Name matches volume_name."""
    return any(m.get("Name") == volume_name for m in (container.attrs.get("Mounts", []) or []))


def _containers_using_volume(client, volume_name: str) -> list[str]:
    """Return container names that mount `volume_name`. Tolerates SDK failure."""
    try:
        containers = client.containers.list(all=True)
    except docker.errors.DockerException:
        return []  # inspect still returns useful info without in-use data
    return [c.name for c in containers if _container_uses_volume(c, volume_name)]


@router.get("/api/volumes/{volume_name}/inspect", dependencies=AUTH, tags=["volumes"])
@secure_route.read(RATE.READ)
def inspect_volume(
    request: Request,
    volume_name: str,
    client=Depends(docker_client_dep),
) -> VolumeInspectResponse:
    """Return detailed volume metadata: driver options, scope, status, usage."""
    if not _VOLUME_NAME_RE.fullmatch(volume_name):
        raise http_error("volume.bad_name")
    vol = safe_docker_call(client.volumes.get, volume_name)
    return VolumeInspectResponse.from_docker(
        vol,
        _containers_using_volume(client, volume_name),
    )


# Drivers we allow for volume create. `local` is the default; others are
# opt-in and require the host to have the plugin installed. Listing them
# explicitly here (vs accepting any string) keeps surface area tight —
# unknown drivers are rejected at the API boundary, not by docker's own
# error path.
_VOLUME_VALID_DRIVERS = frozenset({"local", "nfs", "tmpfs"})

# Label / driver_opt key+value alphabet. Mirrors Docker's own permissive
# rules but rejects anything that would break an API request line or leak
# control chars into the audit log.
_VOLUME_LABEL_KEY_RE = validators.DOCKER_LABEL_KEY_RE
_VOLUME_LABEL_VAL_RE = validators.DOCKER_LABEL_VAL_RE


def _parse_kv_list(raw: str, key_re, val_re, err_code: str) -> dict[str, str]:
    """Parse a newline- or comma-separated `key=value` list.

    Blank lines / empty entries are skipped so the UI textarea can trail
    a newline without rejecting the submission."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    # Accept either newline or comma separators — the UI uses newlines,
    # URL-encoded forms use commas.
    for entry_raw in raw.replace(",", "\n").splitlines():
        entry = entry_raw.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise http_error(err_code, message=f"entry missing '=': {entry[:64]!r}")
        k, _, v = entry.partition("=")
        k = k.strip()
        v = v.strip()
        if not key_re.fullmatch(k):
            raise http_error(err_code, message=f"invalid key {k[:64]!r}")
        if not val_re.fullmatch(v):
            raise http_error(err_code, message=f"invalid value for {k[:64]!r}")
        out[k] = v
    return out


@router.post("/api/volumes/create", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(
    RATE.WRITE,
    audit="volume.created",
    audit_fields=lambda request, name, driver="local", **kw: {"name": name, "driver": driver},  # noqa: ARG005
)
def create_volume(
    request: Request,
    name: str,
    driver: str = "local",
    labels: str = "",
    driver_opts: str = "",
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Create a new named volume.

    - `driver`: `local` (default), or one of the allowlisted drivers.
    - `labels`: `key=value` pairs, one per line or comma-separated.
    - `driver_opts`: driver-specific options, same `key=value` format
      (e.g. for `local` driver: `type=nfs`, `device=:/path`, `o=addr=...`).

    Volumes are immutable after creation — set everything at this call.
    """
    if not _VOLUME_NAME_RE.fullmatch(name):
        raise http_error("volume.bad_name")
    if driver not in _VOLUME_VALID_DRIVERS:
        raise http_error("volume.bad_driver", message=f"driver {driver!r} not in allowlist")
    parsed_labels = _parse_kv_list(
        labels,
        _VOLUME_LABEL_KEY_RE,
        _VOLUME_LABEL_VAL_RE,
        "volume.bad_labels",
    )
    parsed_opts = _parse_kv_list(
        driver_opts,
        _VOLUME_LABEL_KEY_RE,
        _VOLUME_LABEL_VAL_RE,
        "volume.bad_driver_opts",
    )
    kwargs: dict[str, Any] = {"name": name, "driver": driver}
    if parsed_labels:
        kwargs["labels"] = parsed_labels
    if parsed_opts:
        kwargs["driver_opts"] = parsed_opts
    vol = safe_docker_call(client.volumes.create, **kwargs)
    return OkResponse(name=vol.name)


@router.delete("/api/volumes/{volume_name}", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(RATE.WRITE)
def delete_volume(
    request: Request,
    volume_name: str,
    force: bool = False,
    undo: bool = False,
    client=Depends(docker_client_dep),
) -> Any:
    """Remove a named volume. With `undo=true`, removal is delayed and the
    response includes an `undo_token` the caller can POST to `/api/undo/{token}`
    to cancel within the grace window."""
    if not _VOLUME_NAME_RE.fullmatch(volume_name):
        raise http_error("volume.bad_name")
    vol = safe_docker_call(client.volumes.get, volume_name)
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "volume",
            volume_name,
            safe_docker_call,
            vol.remove,
            force=force,
        )
        if token is not None:
            import structlog

            structlog.get_logger(__name__).info(
                "volume.delete_queued",
                name=volume_name,
                token_suffix=token[-6:],
            )
            return UndoableResponse(undo_token=token, expires_in=config.UNDO_DELAY_SECS)
    safe_docker_call(vol.remove, force=force)
    import structlog

    structlog.get_logger(__name__).info("volume.deleted", name=volume_name)
    return OkResponse()


def _do_volumes_prune(client) -> dict:
    result = safe_docker_call(client.volumes.prune, filters={"all": True})
    deleted = result.get("VolumesDeleted") or []
    return {
        "deleted": deleted,
        "space_reclaimed_mb": round(result.get("SpaceReclaimed", 0) / 1024 / 1024, 1),
    }


@router.post("/api/volumes/prune", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(RATE.BURST, audit="volumes.pruned")
def prune_volumes(
    request: Request,
    undo: bool = True,
    client=Depends(docker_client_dep),
):
    """Delete all unused named volumes. Default (`undo=true`) queues the
    op for `UNDO_DELAY_SECS` so a misclick is reversible — volume data
    is unrecoverable once pruned, so an undo window is the only safety
    net. Scripts that need immediate execution pass `undo=false`."""
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "volume",
            "prune:unused",
            _do_volumes_prune,
            client,
        )
        if token is not None:
            from skiff.contract.responses import UndoableResponse

            return UndoableResponse(
                undo_token=token,
                expires_in=config.UNDO_DELAY_SECS,
            )
    return _do_volumes_prune(client)


# ── Volume file browser (reuses container file endpoints) ────────────
#
# Docker has no direct filesystem API for a volume — bytes live on the
# host engine and are only reachable via a container that mounts them.
# Rather than duplicate the whole /ls + /files surface for volumes, we
# expose a single endpoint that returns a container_id + mount_path
# the UI can then use against the existing /api/containers/{id}/ls,
# /files (GET/POST/DELETE) endpoints. No horizontal proliferation.
#
# Preference order:
#   1. An already-attached container (running or stopped) — free, no
#      helper created, user's cleanup responsibility stays with their
#      own containers.
#   2. A short-lived helper container (`skiff-volbrowse-<random>`)
#      running `alpine:3.20 sleep infinity`. The UI passes the returned
#      `helper` name to DELETE /browse when done. An auto-cleanup
#      sweeper removes orphaned helpers older than 1h at every
#      /volumes list call (best-effort, idempotent).

_VOLBROWSE_IMAGE = "alpine:3.20"
_VOLBROWSE_LABEL = "skiff.helper"
_VOLBROWSE_VALUE = "volbrowse"
_VOLBROWSE_MOUNT = "/mnt"


def _find_attached_container(client, volume_name: str) -> tuple[str, str] | None:
    """Return (container_id, mount_destination) for the first container
    that has `volume_name` mounted, or None. Prefers running containers
    so the user's browse target is live; falls back to stopped."""
    try:
        containers = safe_docker_call(client.containers.list, all=True)
    except Exception:  # nosec B110
        return None
    candidates: list[tuple[str, str, bool]] = []
    for c in containers or []:
        mounts = c.attrs.get("Mounts") or []
        for m in mounts:
            if m.get("Type") == "volume" and m.get("Name") == volume_name:
                dest = m.get("Destination") or "/"
                is_running = c.status == "running"
                candidates.append((c.id, dest, is_running))
    if not candidates:
        return None
    # Prefer running over stopped.
    candidates.sort(key=lambda t: 0 if t[2] else 1)
    return candidates[0][0], candidates[0][1]


@router.post("/api/volumes/{volume_name}/browse", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(
    RATE.WRITE,
    audit="volume.browse_opened",
    audit_fields=lambda request, volume_name, **kw: {"volume": volume_name},  # noqa: ARG005
)
def volume_browse_open(
    request: Request,
    volume_name: str,
    client=Depends(docker_client_dep),
) -> dict:
    """Return a (container_id, mount_path) the UI can use with the
    existing /api/containers/{id}/ls + /files endpoints to browse the
    volume's contents. Spawns an alpine helper if no attached
    container exists; `helper` in the response names it so the UI can
    DELETE /browse on close."""
    if not _VOLUME_NAME_RE.fullmatch(volume_name):
        raise http_error("volume.bad_name")
    # Confirm the volume exists (404 otherwise).
    safe_docker_call(client.volumes.get, volume_name)
    attached = _find_attached_container(client, volume_name)
    if attached is not None:
        container_id, mount_path = attached
        log.info(
            "volume.browse_via_attached",
            volume=volume_name,
            container_id=container_id[:12],
            path=mount_path,
        )
        return {
            "container_id": container_id,
            "mount_path": mount_path,
            "helper": None,
        }
    # Spawn a helper. Name includes a random suffix so concurrent
    # browses don't collide.
    helper_name = f"skiff-volbrowse-{secrets.token_urlsafe(6)}"
    try:
        helper = safe_docker_call(
            client.containers.run,
            image=_VOLBROWSE_IMAGE,
            command="sleep infinity",
            detach=True,
            name=helper_name,
            volumes={volume_name: {"bind": _VOLBROWSE_MOUNT, "mode": "rw"}},
            labels={
                "skiff-audit-run": "1",
                _VOLBROWSE_LABEL: _VOLBROWSE_VALUE,
                "skiff.volume": volume_name,
            },
            read_only=False,  # user needs to write into the volume
        )
    except docker.errors.APIError as exc:
        raise http_error(
            "validation.bad_input",
            message=f"Could not spawn browse helper: {exc}",
        ) from exc
    log.info(
        "volume.browse_helper_spawned",
        volume=volume_name,
        container_id=helper.id[:12],
        name=helper_name,
    )
    return {
        "container_id": helper.id,
        "mount_path": _VOLBROWSE_MOUNT,
        "helper": helper_name,
    }


@router.delete("/api/volumes/{volume_name}/browse", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(
    RATE.WRITE,
    audit="volume.browse_closed",
    audit_fields=lambda request, volume_name, **kw: {"volume": volume_name},  # noqa: ARG005
)
def volume_browse_close(
    request: Request,
    volume_name: str,
    container_id: str,
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Stop + remove a helper created by POST /browse. Refuses to
    touch a container that ISN'T a volbrowse helper — defence against
    a malicious client DELETE-browse'ing a real container by ID."""
    if not _VOLUME_NAME_RE.fullmatch(volume_name):
        raise http_error("volume.bad_name")
    # Call the SDK directly (not via safe_docker_call) so a NotFound
    # maps to the "already gone — idempotent" path rather than a 404
    # envelope. Double-clicking the close button on the UI shouldn't
    # 404 the second click.
    try:
        c = client.containers.get(container_id)
    except docker.errors.NotFound:
        return OkResponse()  # already gone — idempotent
    labels = (c.attrs.get("Config") or {}).get("Labels") or {}
    if labels.get(_VOLBROWSE_LABEL) != _VOLBROWSE_VALUE:
        raise http_error(
            "validation.bad_input",
            message="target is not a volume-browse helper",
        )
    if labels.get("skiff.volume") != volume_name:
        raise http_error(
            "validation.bad_input",
            message="helper belongs to a different volume",
        )
    safe_docker_call(c.remove, force=True)
    log.info(
        "volume.browse_helper_removed",
        volume=volume_name,
        container_id=container_id[:12],
    )
    return OkResponse()
