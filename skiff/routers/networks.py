# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker network management routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from skiff import config
from skiff.auth import AUTH
from skiff.contract.errors import http_error
from skiff.contract.responses import NetworkSummary, OkResponse
from skiff.docker_client import docker_client_dep
from skiff.rate import RATE
from skiff.secure import secure_route
from skiff.validators import (
    NETWORK_ID_RE,
    NETWORK_NAME_RE,
    _get_container,
    safe_docker_call,
    validate_container_id,
)

router = APIRouter()

_NETWORK_ID_RE = NETWORK_ID_RE
# Builtin / valid-driver lists — sourced exclusively from
# skiff/_config/networks.toml. See that file for the zero-trust rationale
# (don't let a compromised caller delete bridge/host/none, and don't
# accept drivers we haven't security-reviewed).
_BUILTIN_NETWORKS = set(config._TOML_NETWORKS["builtin_names"])
_VALID_DRIVERS = set(config._TOML_NETWORKS["valid_drivers"])


@router.get("/api/networks", dependencies=AUTH, tags=["networks"])
@secure_route.read(RATE.READ)
def list_networks(request: Request, client=Depends(docker_client_dep)) -> list[NetworkSummary]:
    """Return all Docker networks with IPAM config and attached containers."""
    networks = safe_docker_call(client.networks.list)
    return [NetworkSummary.from_docker(n) for n in networks]


@router.post("/api/networks/create", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE, audit="network.created",
    audit_fields=lambda request, name, driver="bridge", **kw: {"name": name, "driver": driver},  # noqa: ARG005
)
def create_network(
    request: Request, name: str, driver: str = "bridge", client=Depends(docker_client_dep),
) -> OkResponse:
    """Create a new Docker network with the specified driver."""
    if not NETWORK_NAME_RE.fullmatch(name):
        raise http_error("network.bad_name")
    if driver not in _VALID_DRIVERS:
        raise http_error("network.bad_driver")
    net = safe_docker_call(client.networks.create, name, driver=driver)
    return OkResponse(id=net.short_id, name=name)


@router.delete("/api/networks/{network_id}", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE, audit="network.deleted",
    audit_fields=lambda request, network_id, **kw: {"id": network_id},  # noqa: ARG005
)
def delete_network(request: Request, network_id: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Remove a user-defined network (default networks are protected)."""
    if not _NETWORK_ID_RE.fullmatch(network_id):
        raise http_error("validation.bad_input")
    net = safe_docker_call(client.networks.get, network_id)
    if net.name in _BUILTIN_NETWORKS:
        raise http_error("network.builtin_protected")
    safe_docker_call(net.remove)
    return OkResponse()


@router.post("/api/networks/{network_id}/connect", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE, audit="network.connect",
    audit_fields=lambda request, network_id, container_id, **kw:  # noqa: ARG005
        {"network": network_id, "container": container_id},
)
def connect_container_to_network(
    request: Request, network_id: str, container_id: str, client=Depends(docker_client_dep),
) -> OkResponse:
    """Attach a container to a network."""
    if not _NETWORK_ID_RE.fullmatch(network_id):
        raise http_error("validation.bad_input")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.connect, container)
    return OkResponse()


@router.post("/api/networks/{network_id}/disconnect", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE, audit="network.disconnect",
    audit_fields=lambda request, network_id, container_id, **kw:  # noqa: ARG005
        {"network": network_id, "container": container_id},
)
def disconnect_container_from_network(
    request: Request, network_id: str, container_id: str, client=Depends(docker_client_dep),
) -> OkResponse:
    """Detach a container from a network."""
    if not _NETWORK_ID_RE.fullmatch(network_id):
        raise http_error("validation.bad_input")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.disconnect, container)
    return OkResponse()


@router.post("/api/networks/prune", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.BURST, audit="networks.pruned",
    audit_fields=lambda request, **kw: {"count": 0},  # noqa: ARG005 — count rewritten post-call is future work
)
def prune_networks(request: Request, client=Depends(docker_client_dep)) -> dict[str, Any]:
    """Delete all unused networks."""
    result = safe_docker_call(client.networks.prune)
    return {"deleted": result.get("NetworksDeleted") or []}
