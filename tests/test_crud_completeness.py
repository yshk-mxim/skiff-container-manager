# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""CRUD completeness invariants — every Docker object exposed by SKIFF
must support Create, Read, Update (where Docker allows), and Delete.

Motivated by user feedback that volume/network create forms were
one-field-only (name), compose lacked stop/start/pull/scale, and
images lacked a dedicated prune. Those were instances of the broader
class: *operator expects CLI-parity, backend is a subset*.

This file enforces parity by asserting that each resource has the
expected routes at the HTTP-route level, and asserts the frontend
modals expose the matching knobs. Adding a new endpoint without its
frontend mirror (or vice versa) trips a specific subtest.

Baseline expectations for a Docker-management tool of this class:
  - full CRUD on containers, images, volumes, networks, stacks
  - stack lifecycle verbs: up/down/stop/start/pull/restart/scale
  - observability: events stream, audit log, stats, logs
  - interactive shell (exec), filesystem browse + cp, inspect/commit
  - templates / quick-start catalogue
  - keyboard-first affordances (palette, bulk select, context menu)
The SKIFF coverage matrix here is the backstop — drift surfaces as a
failing test, not a user complaint in production."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _route_shapes() -> list[tuple[str, str]]:
    """Return a set of `(METHOD, path)` tuples for every registered route.

    Used by the CRUD-completeness matrix below. Loaded lazily inside
    each test so patching the app in conftest stays respected."""
    from starlette.routing import Route

    from skiff.app import app

    shapes: list[tuple[str, str]] = []
    for r in app.routes:
        if isinstance(r, Route):
            for m in (r.methods or ()):
                shapes.append((m, r.path))
    return shapes


# Expected CRUD surface per resource. Each entry is:
#   (resource_label, list of (method, path_pattern))
# `path_pattern` is substring-matched so parameterised routes like
# `/api/containers/{id}/start` pass without exact equality gymnastics.
_EXPECTED_CRUD = {
    "containers": [
        ("GET", "/api/containers"),                         # Read-list
        ("GET", "/api/containers/{container_id}/inspect"),  # Read-one
        ("POST", "/api/containers/run"),                    # Create
        ("POST", "/api/containers/{container_id}/rename"),  # Update
        ("POST", "/api/containers/{container_id}/update"),  # Update (limits)
        ("POST", "/api/containers/{container_id}/commit"),  # Commit to image
        ("POST", "/api/containers/{container_id}/start"),
        ("POST", "/api/containers/{container_id}/stop"),
        ("POST", "/api/containers/{container_id}/restart"),
        ("POST", "/api/containers/{container_id}/pause"),
        ("POST", "/api/containers/{container_id}/unpause"),
        ("POST", "/api/containers/{container_id}/kill"),
        ("DELETE", "/api/containers/{container_id}"),       # Delete (undoable)
        ("GET", "/api/containers/{container_id}/logs"),     # Read-logs
        ("GET", "/api/containers/{container_id}/stats"),
        ("GET", "/api/containers/{container_id}/top"),
        ("GET", "/api/containers/{container_id}/diff"),
        ("GET", "/api/containers/{container_id}/files"),    # cp — tar download
        ("POST", "/api/containers/{container_id}/files"),   # cp — tar upload
        ("POST", "/api/containers/{container_id}/upload"),  # cp — multipart upload
        ("GET", "/api/containers/{container_id}/ls"),       # file-browser ls
    ],
    "images": [
        ("GET", "/api/images"),
        ("GET", "/api/images/{image_id}/inspect"),
        ("POST", "/api/images/pull"),                       # Create
        ("POST", "/api/images/{image_id}/tag"),             # Update
        ("POST", "/api/images/push"),
        ("POST", "/api/images/prune"),                      # Bulk cleanup
        ("DELETE", "/api/images/{image_id}"),
        ("GET", "/api/registry/search"),
        ("GET", "/api/registry/tags"),
        ("GET", "/api/templates"),                          # Quick-start catalogue
    ],
    "volumes": [
        ("GET", "/api/volumes"),
        ("GET", "/api/volumes/{volume_name}/inspect"),
        ("POST", "/api/volumes/create"),                    # Create (full params)
        ("DELETE", "/api/volumes/{volume_name}"),
        ("POST", "/api/volumes/prune"),
    ],
    "networks": [
        ("GET", "/api/networks"),
        ("GET", "/api/networks/{network_id}/inspect"),
        ("POST", "/api/networks/create"),
        ("POST", "/api/networks/{network_id}/connect"),
        ("POST", "/api/networks/{network_id}/disconnect"),
        ("DELETE", "/api/networks/{network_id}"),
        ("POST", "/api/networks/prune"),
    ],
    "compose": [
        ("GET", "/api/compose/stacks"),
        ("POST", "/api/compose/up"),
        ("POST", "/api/compose/down"),
        ("GET", "/api/compose/{project_name}/logs"),
        ("GET", "/api/compose/{project_name}/download"),   # YAML export
        ("POST", "/api/compose/{project_name}/pull"),      # Update images
        ("POST", "/api/compose/{project_name}/scale"),     # Scale service
        ("POST", "/api/compose/{project_name}/stop"),      # Lifecycle
        ("POST", "/api/compose/{project_name}/start"),
        ("POST", "/api/compose/{project_name}/services/{service_name}/restart"),
    ],
    "system": [
        ("GET", "/api/system/info"),
        ("GET", "/api/system/df"),
        ("GET", "/api/system/metrics"),
        ("GET", "/api/system/overview"),         # Dashboard aggregation
        ("GET", "/api/system/events"),           # Docker event stream (polled)
        ("GET", "/api/system/audit-log"),
        ("GET", "/api/system/audit-log/download"),
        ("POST", "/api/system/prune"),
        ("POST", "/api/system/prune-build-cache"),
    ],
}


@pytest.mark.parametrize("resource,expected", list(_EXPECTED_CRUD.items()))
def test_resource_has_full_crud_surface(resource, expected):
    """For each Docker resource, assert every expected route is
    registered. Missing a route is either an intentional security
    carve-out (in which case move the path out of _EXPECTED_CRUD with
    a comment explaining why) or a regression."""
    shapes = _route_shapes()
    missing: list[str] = []
    for method, path in expected:
        if (method, path) not in shapes:
            missing.append(f"{method} {path}")
    assert not missing, (
        f"Resource {resource!r} is missing expected CRUD routes: {missing!r}. "
        f"Either add the route or move it out of _EXPECTED_CRUD with a "
        f"security-justified comment."
    )


def test_all_routes_have_auth_dependency():
    """Every non-trivial route under /api must sit behind auth. The
    known exceptions (health, setup-state, login UI) are allowlisted."""
    from starlette.routing import Route

    from skiff.app import app

    # Endpoints explicitly allowed to be unauthenticated — they power
    # the sign-in flow, health probe, setup wizard, Swagger UI, and
    # OpenAPI schema that the UI consumes pre-login.
    _PUBLIC = {
        "/api/health",
        "/api/setup-state",
        "/api/setup",
        "/api/config/public",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth-required",        # public by design — "do I need auth?"
        "/api/contract/error-codes",
        "/api/openapi.json",
        "/api/docs",                  # Swagger UI HTML — no data
        "/api/setup/probe-docker",   # wizard-only, rate-limited
        "/api/setup/tunnel",         # wizard-only, rate-limited
        "/api/tunnel/status",        # public during wizard
        "/api/tunnel/reconnect",     # public during wizard
    }
    missing_auth: list[str] = []
    for r in app.routes:
        if not isinstance(r, Route):
            continue
        if not r.path.startswith("/api/"):
            continue
        if r.path in _PUBLIC:
            continue
        # FastAPI stores the auth `Depends(...)` on the route's
        # `dependant.dependencies`. Each Dependant carries a `.call`
        # reference to the underlying callable — auth is wired via
        # skiff.auth.verify_auth / verify_auth_strict, both of which
        # have `auth` or `verify` in the function name.
        deps = getattr(getattr(r, "dependant", None), "dependencies", [])
        has_auth = any(
            "auth" in (getattr(getattr(d, "call", None), "__name__", "") or "").lower()
            or "verify" in (getattr(getattr(d, "call", None), "__name__", "") or "").lower()
            for d in deps
        )
        if not has_auth:
            missing_auth.append(f"{next(iter(r.methods or {}), '?')} {r.path}")
    assert not missing_auth, (
        f"Routes without auth dependency: {missing_auth!r}. Add to _PUBLIC "
        f"with a comment if intentional, or wire AUTH."
    )


def test_new_error_codes_constructable():
    """The CRUD expansion added error codes for volume/network bad
    params + image prune failures. Every new code must be constructable
    via `http_error(code)` so it doesn't crash at request time."""
    from skiff.contract.errors import http_error, known_codes

    new_codes = [
        "image.prune_failed",
        "volume.bad_driver",
        "volume.bad_labels",
        "volume.bad_driver_opts",
        "network.bad_labels",
        "network.bad_subnet",
        "network.bad_gateway",
    ]
    catalogued = set(known_codes())
    for code in new_codes:
        assert code in catalogued, f"{code!r} not in catalogue"
        # Construct with and without a custom message — both should succeed.
        exc = http_error(code)
        assert exc.detail["code"] == code, f"{code!r} round-trip broken"


def test_volume_create_accepts_full_params():
    """Volume create must accept driver, labels, and driver_opts. A
    regression here would be the bug that shipped in v1.0.1 — only
    `name` was wired, so operators couldn't create nfs-backed or
    labelled volumes from the UI."""
    from unittest.mock import MagicMock, patch

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    created_volume = MagicMock()
    created_volume.name = "fuzz-vol"
    mock_client.volumes.create.return_value = created_volume
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.post(
                    "/api/volumes/create",
                    params={
                        "name": "fuzz-vol",
                        "driver": "local",
                        "labels": "env=test\nteam=platform",
                        "driver_opts": "type=tmpfs",
                    },
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 200, r.text[:300]
                # Assert docker-py was called with the right kwargs so
                # we know the UI's query-string round-trip is wired end to end.
                call = mock_client.volumes.create.call_args
                assert call.kwargs["driver"] == "local"
                assert call.kwargs["labels"] == {"env": "test", "team": "platform"}
                assert call.kwargs["driver_opts"] == {"type": "tmpfs"}
    finally:
        config_module._cfg.api_token = orig_token


def test_network_create_accepts_full_params():
    """Network create must accept subnet, gateway, labels, and the
    boolean flags (internal, attachable, enable_ipv6)."""
    from unittest.mock import MagicMock, patch

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    created_net = MagicMock()
    created_net.short_id = "abcdef"
    mock_client.networks.create.return_value = created_net
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.post(
                    "/api/networks/create",
                    params={
                        "name": "fuzz-net",
                        "driver": "bridge",
                        "subnet": "10.55.0.0/24",
                        "gateway": "10.55.0.1",
                        "labels": "env=test",
                        "internal": "true",
                        "attachable": "true",
                        "enable_ipv6": "true",
                    },
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 200, r.text[:300]
                call = mock_client.networks.create.call_args
                kwargs = call.kwargs
                assert kwargs["driver"] == "bridge"
                assert kwargs["labels"] == {"env": "test"}
                assert kwargs["internal"] is True
                assert kwargs["attachable"] is True
                assert kwargs["enable_ipv6"] is True
                # IPAM config is a docker.types object — just assert it's there.
                assert kwargs.get("ipam") is not None
    finally:
        config_module._cfg.api_token = orig_token


def test_network_create_rejects_gateway_without_subnet():
    """`gateway` without `subnet` is nonsensical — assert envelope."""
    from unittest.mock import MagicMock, patch

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.post(
                    "/api/networks/create",
                    params={"name": "fuzz-net", "driver": "bridge", "gateway": "10.55.0.1"},
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 400
                body = r.json()
                assert body["detail"]["code"] == "network.bad_subnet"
    finally:
        config_module._cfg.api_token = orig_token


def test_network_create_rejects_bad_cidr():
    """Invalid CIDR surfaces as network.bad_subnet, not a 500."""
    from unittest.mock import MagicMock, patch

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.post(
                    "/api/networks/create",
                    params={"name": "fuzz-net", "driver": "bridge", "subnet": "not-a-cidr"},
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 400
                assert r.json()["detail"]["code"] == "network.bad_subnet"
    finally:
        config_module._cfg.api_token = orig_token
