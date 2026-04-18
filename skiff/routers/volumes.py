# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Named volume management routes."""

from __future__ import annotations

from typing import Any

import docker.errors
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


@router.post("/api/volumes/create", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(
    RATE.WRITE,
    audit="volume.created",
    audit_fields=lambda request, name, **kw: {"name": name},  # noqa: ARG005
)
def create_volume(request: Request, name: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Create a new named volume."""
    if not _VOLUME_NAME_RE.fullmatch(name):
        raise http_error("volume.bad_name")
    vol = safe_docker_call(client.volumes.create, name=name)
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


@router.post("/api/volumes/prune", dependencies=AUTH, tags=["volumes"])
@secure_route.mutate(RATE.BURST, audit="volumes.pruned")
def prune_volumes(request: Request, client=Depends(docker_client_dep)) -> dict:
    """Delete all unused named volumes and reclaim storage."""
    result = safe_docker_call(client.volumes.prune, filters={"all": True})
    deleted = result.get("VolumesDeleted") or []
    return {
        "deleted": deleted,
        "space_reclaimed_mb": round(result.get("SpaceReclaimed", 0) / 1024 / 1024, 1),
    }
