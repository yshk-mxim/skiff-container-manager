# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker network management routes."""
from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from skiff.auth import AUTH, verify_csrf
from skiff.config import RL_DEFAULT, RL_SLOW, _limit, limiter
from skiff.docker_client import docker_client_dep
from skiff.validators import NETWORK_NAME_RE, _get_container, safe_docker_call, validate_container_id

log = structlog.get_logger(__name__)
router = APIRouter()

_NETWORK_ID_RE = re.compile(r"^[a-f0-9]{4,64}$")


@router.get("/api/networks", dependencies=AUTH, tags=["networks"])
@limiter.limit(_limit(RL_DEFAULT))
def list_networks(request: Request, client=Depends(docker_client_dep)) -> list[dict]:
    """Return all Docker networks with IPAM config and attached containers."""
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


@router.post("/api/networks/create", dependencies=AUTH, tags=["networks"])
@limiter.limit(_limit(RL_SLOW))
def create_network(
    request: Request, name: str, driver: str = "bridge", client=Depends(docker_client_dep)
) -> dict:
    """Create a new Docker network with the specified driver."""
    verify_csrf(request)
    if not NETWORK_NAME_RE.match(name):
        raise HTTPException(400, "Invalid network name")
    if driver not in ("bridge", "overlay", "macvlan", "none"):
        raise HTTPException(400, "Invalid network driver")
    net = safe_docker_call(client.networks.create, name, driver=driver)
    log.info("network.created", name=name, driver=driver)
    return {"id": net.short_id, "name": name}


@router.delete("/api/networks/{network_id}", dependencies=AUTH, tags=["networks"])
@limiter.limit(_limit(RL_SLOW))
def delete_network(request: Request, network_id: str, client=Depends(docker_client_dep)) -> dict:
    """Remove a user-defined network (default networks are protected)."""
    verify_csrf(request)
    if not _NETWORK_ID_RE.match(network_id):
        raise HTTPException(400, "Invalid network ID")
    net = safe_docker_call(client.networks.get, network_id)
    if net.name in ("bridge", "host", "none"):
        raise HTTPException(400, "Cannot delete default network")
    safe_docker_call(net.remove)
    log.info("network.deleted", id=network_id)
    return {"ok": True}


@router.post("/api/networks/{network_id}/connect", dependencies=AUTH, tags=["networks"])
@limiter.limit(_limit(RL_SLOW))
def connect_container_to_network(
    request: Request, network_id: str, container_id: str, client=Depends(docker_client_dep)
) -> dict:
    """Attach a container to a network."""
    verify_csrf(request)
    if not _NETWORK_ID_RE.match(network_id):
        raise HTTPException(400, "Invalid network ID")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.connect, container)
    log.info("network.connect", network=network_id, container=container_id)
    return {"ok": True}


@router.post("/api/networks/{network_id}/disconnect", dependencies=AUTH, tags=["networks"])
@limiter.limit(_limit(RL_SLOW))
def disconnect_container_from_network(
    request: Request, network_id: str, container_id: str, client=Depends(docker_client_dep)
) -> dict:
    """Detach a container from a network."""
    verify_csrf(request)
    if not _NETWORK_ID_RE.match(network_id):
        raise HTTPException(400, "Invalid network ID")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.disconnect, container)
    log.info("network.disconnect", network=network_id, container=container_id)
    return {"ok": True}


@router.post("/api/networks/prune", dependencies=AUTH, tags=["networks"])
@limiter.limit("3/minute")
def prune_networks(request: Request, client=Depends(docker_client_dep)) -> dict:
    """Delete all unused networks."""
    verify_csrf(request)
    result = safe_docker_call(client.networks.prune)
    deleted = result.get("NetworksDeleted") or []
    log.info("networks.pruned", count=len(deleted))
    return {"deleted": deleted}
