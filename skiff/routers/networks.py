# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker network management routes."""

from __future__ import annotations

import ipaddress as _ipaddress
from typing import Any

from fastapi import APIRouter, Depends, Request

from skiff import config
from skiff import validators as _skiff_validators
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

# Label key/value grammar — shared constants in skiff.validators so the
# volume and network routers use the same rules.
_NET_LABEL_KEY_RE = _skiff_validators.DOCKER_LABEL_KEY_RE
_NET_LABEL_VAL_RE = _skiff_validators.DOCKER_LABEL_VAL_RE

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


@router.get("/api/networks/{network_id}/inspect", dependencies=AUTH, tags=["networks"])
@secure_route.read(RATE.READ)
def inspect_network(request: Request, network_id: str, client=Depends(docker_client_dep)) -> dict:
    """Return the full Docker inspect payload for a network.

    Parity with `/api/volumes/{name}/inspect`. The list endpoint already
    carries IPAM + attached containers, but `inspect` exposes the raw
    `Options`, `Labels`, and driver-specific metadata that a scripted
    caller sometimes needs.
    """
    # Accept either a hex network id OR a user-defined network name.
    if not (_NETWORK_ID_RE.fullmatch(network_id) or NETWORK_NAME_RE.fullmatch(network_id)):
        raise http_error("validation.bad_input")
    net = safe_docker_call(client.networks.get, network_id, kind="network")
    return net.attrs


def _parse_net_labels(raw: str) -> dict[str, str]:
    """Parse `key=value` list used by volume + network create."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for entry_raw in raw.replace(",", "\n").splitlines():
        entry = entry_raw.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise http_error("network.bad_labels", message=f"entry missing '=': {entry[:64]!r}")
        k, _, v = entry.partition("=")
        k = k.strip()
        v = v.strip()
        if not _NET_LABEL_KEY_RE.fullmatch(k):
            raise http_error("network.bad_labels", message=f"invalid key {k[:64]!r}")
        if not _NET_LABEL_VAL_RE.fullmatch(v):
            raise http_error("network.bad_labels", message=f"invalid value for {k[:64]!r}")
        out[k] = v
    return out


def _validate_cidr(cidr: str, field: str) -> str:
    """Parse + normalise a CIDR using the stdlib. Accepts v4 or v6."""
    try:
        net = _ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError) as exc:
        raise http_error("network.bad_subnet", message=f"{field}: {exc}") from exc
    return str(net)


def _validate_ip(addr: str, field: str) -> str:
    try:
        return str(_ipaddress.ip_address(addr))
    except (ValueError, TypeError) as exc:
        raise http_error("network.bad_gateway", message=f"{field}: {exc}") from exc


@router.post("/api/networks/create", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE,
    audit="network.created",
    audit_fields=lambda request, name, driver="bridge", **kw: {"name": name, "driver": driver},  # noqa: ARG005
)
def create_network(
    request: Request,
    name: str,
    driver: str = "bridge",
    subnet: str = "",
    gateway: str = "",
    labels: str = "",
    internal: bool = False,
    attachable: bool = False,
    enable_ipv6: bool = False,
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Create a new Docker network.

    - `subnet` / `gateway`: optional CIDR + IP for an IPAM pool. Either
      both or neither. If only `subnet` is passed, Docker picks a gateway.
    - `labels`: `key=value` pairs, one per line or comma-separated.
    - `internal`: if true, no outbound connectivity from containers on this
      network (DB-tier, auth-tier use cases).
    - `attachable`: if true, standalone containers can join (not just
      services). Default false matches Docker's own `docker network create`.
    - `enable_ipv6`: allocate an IPv6 pool too.
    """
    if not NETWORK_NAME_RE.fullmatch(name):
        raise http_error("network.bad_name")
    if driver not in _VALID_DRIVERS:
        raise http_error("network.bad_driver")
    parsed_labels = _parse_net_labels(labels)
    kwargs: dict[str, Any] = {"driver": driver}
    if parsed_labels:
        kwargs["labels"] = parsed_labels
    if internal:
        kwargs["internal"] = True
    if attachable:
        kwargs["attachable"] = True
    if enable_ipv6:
        kwargs["enable_ipv6"] = True
    if subnet:
        subnet_norm = _validate_cidr(subnet, "subnet")
        pool_kwargs: dict[str, str] = {"subnet": subnet_norm}
        if gateway:
            pool_kwargs["gateway"] = _validate_ip(gateway, "gateway")
        from docker.types import IPAMConfig, IPAMPool

        kwargs["ipam"] = IPAMConfig(pool_configs=[IPAMPool(**pool_kwargs)])
    elif gateway:
        # Gateway without subnet is nonsensical — Docker will reject but
        # surface it earlier with a cleaner envelope.
        raise http_error("network.bad_subnet", message="gateway requires subnet")
    net = safe_docker_call(client.networks.create, name, **kwargs)
    return OkResponse(id=net.short_id, name=name)


@router.delete("/api/networks/{network_id}", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE,
    audit="network.deleted",
    audit_fields=lambda request, network_id, **kw: {"id": network_id},  # noqa: ARG005
)
def delete_network(
    request: Request,
    network_id: str,
    undo: bool = False,
    client=Depends(docker_client_dep),
):
    """Remove a user-defined network (default networks are protected).
    With `undo=true`, removal is queued for `UNDO_DELAY_SECS` and the
    response carries an undo token — matches every other destructive
    single-resource delete (container / image / volume)."""
    if not (_NETWORK_ID_RE.fullmatch(network_id) or NETWORK_NAME_RE.fullmatch(network_id)):
        raise http_error("validation.bad_input")
    net = safe_docker_call(client.networks.get, network_id)
    if net.name in _BUILTIN_NETWORKS:
        raise http_error("network.builtin_protected")
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "network",
            network_id,
            safe_docker_call,
            net.remove,
        )
        if token is not None:
            from skiff.contract.responses import UndoableResponse

            return UndoableResponse(
                undo_token=token,
                expires_in=config.UNDO_DELAY_SECS,
            )
    safe_docker_call(net.remove)
    return OkResponse()


@router.post("/api/networks/{network_id}/connect", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE,
    audit="network.connect",
    audit_fields=lambda request, network_id, container_id, **kw: (  # noqa: ARG005
        {"network": network_id, "container": container_id}
    ),
)
def connect_container_to_network(
    request: Request,
    network_id: str,
    container_id: str,
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Attach a container to a network."""
    # Accept either a hex network id OR a user-defined network name.
    # docker-py's networks.get handles both; matches the inspect route.
    if not (_NETWORK_ID_RE.fullmatch(network_id) or NETWORK_NAME_RE.fullmatch(network_id)):
        raise http_error("validation.bad_input")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.connect, container)
    return OkResponse()


@router.post("/api/networks/{network_id}/disconnect", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.WRITE,
    audit="network.disconnect",
    audit_fields=lambda request, network_id, container_id, **kw: (  # noqa: ARG005
        {"network": network_id, "container": container_id}
    ),
)
def disconnect_container_from_network(
    request: Request,
    network_id: str,
    container_id: str,
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Detach a container from a network."""
    # Accept either a hex network id OR a user-defined network name.
    # docker-py's networks.get handles both; matches the inspect route.
    if not (_NETWORK_ID_RE.fullmatch(network_id) or NETWORK_NAME_RE.fullmatch(network_id)):
        raise http_error("validation.bad_input")
    validate_container_id(container_id)
    net = safe_docker_call(client.networks.get, network_id)
    container = _get_container(client, container_id)
    safe_docker_call(net.disconnect, container)
    return OkResponse()


def _do_networks_prune(client) -> dict[str, Any]:
    result = safe_docker_call(client.networks.prune)
    return {"deleted": result.get("NetworksDeleted") or []}


@router.post("/api/networks/prune", dependencies=AUTH, tags=["networks"])
@secure_route.mutate(
    RATE.BURST,
    audit="networks.pruned",
    audit_fields=lambda request, **kw: {"count": 0},  # noqa: ARG005
)
def prune_networks(
    request: Request,
    undo: bool = True,
    client=Depends(docker_client_dep),
):
    """Delete all unused networks. Default queues the op so a misclick
    is reversible within the undo window; `undo=false` fires now."""
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "network",
            "prune:unused",
            _do_networks_prune,
            client,
        )
        if token is not None:
            from skiff.contract.responses import UndoableResponse

            return UndoableResponse(
                undo_token=token,
                expires_in=config.UNDO_DELAY_SECS,
            )
    return _do_networks_prune(client)
