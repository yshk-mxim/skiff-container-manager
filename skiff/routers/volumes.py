# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Named volume management routes."""
from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from skiff.auth import AUTH, verify_csrf
from skiff.config import RL_DEFAULT, RL_SLOW, _limit, limiter
from skiff.docker_client import docker_client_dep
from skiff.validators import safe_docker_call

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/api/volumes", dependencies=AUTH, tags=["volumes"])
@limiter.limit(_limit(RL_DEFAULT))
def list_volumes(request: Request, client=Depends(docker_client_dep)) -> list[dict]:
    """Return all named volumes and which containers are using each one."""
    volumes = safe_docker_call(client.volumes.list)
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


@router.post("/api/volumes/create", dependencies=AUTH, tags=["volumes"])
@limiter.limit(_limit(RL_SLOW))
def create_volume(request: Request, name: str, client=Depends(docker_client_dep)) -> dict:
    """Create a new named volume."""
    verify_csrf(request)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", name):
        raise HTTPException(400, "Invalid volume name")
    vol = safe_docker_call(client.volumes.create, name=name)
    log.info("volume.created", name=name)
    return {"name": vol.name}


@router.delete("/api/volumes/{volume_name}", dependencies=AUTH, tags=["volumes"])
@limiter.limit(_limit(RL_SLOW))
def delete_volume(
    request: Request, volume_name: str, force: bool = False, client=Depends(docker_client_dep)
) -> dict:
    """Remove a named volume."""
    verify_csrf(request)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", volume_name):
        raise HTTPException(400, "Invalid volume name")
    vol = safe_docker_call(client.volumes.get, volume_name)
    safe_docker_call(vol.remove, force=force)
    log.info("volume.deleted", name=volume_name)
    return {"ok": True}


@router.post("/api/volumes/prune", dependencies=AUTH, tags=["volumes"])
@limiter.limit("3/minute")
def prune_volumes(request: Request, client=Depends(docker_client_dep)) -> dict:
    """Delete all unused named volumes and reclaim storage."""
    verify_csrf(request)
    result = safe_docker_call(client.volumes.prune, filters={"all": True})
    deleted = result.get("VolumesDeleted") or []
    log.info("volumes.pruned", count=len(deleted))
    return {"deleted": deleted, "space_reclaimed_mb": round(result.get("SpaceReclaimed", 0) / 1024 / 1024, 1)}
