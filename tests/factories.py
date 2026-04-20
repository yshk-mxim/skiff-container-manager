# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Shared mock builders for unit tests.

Every Docker resource type gets ONE canonical builder here with optional
overrides, replacing the ~8 nearly-identical `_make_container` /
`_make_volume` / `_make_image` / `_make_network` functions that drifted
across test files. Drift was causing subtle mock-vs-reality mismatches
(e.g., containers.py `_make_container` omitted fields that the container
routes read via .attrs, so some tests silently tested the wrong thing).

Design:
- Each `make_*` returns a MagicMock configured to look like the Docker SDK
  object SKIFF's routers interact with.
- Accepts kwargs for the handful of fields commonly customised. Anything
  else the caller can still patch via `mock.attrs['Config']['X'] = ...`
  — we don't enumerate every field.
- Builders are pure functions — no shared state, no class hierarchy.
  New variants are ONE function call with overrides, not a subclass.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def make_container(
    short_id: str = "abc123def456",
    name: str = "test-container",
    image_tag: str = "docker.io/library/nginx:latest",
    status: str = "running",
    state_status: str | None = None,
    env: list[str] | None = None,
    cmd: list[str] | None = None,
    ports: dict | None = None,
    host_config: dict | None = None,
    labels: dict | None = None,
    network_settings: dict | None = None,
    mounts: list | None = None,
    health: dict | None = None,
) -> MagicMock:
    """Build a mock container object matching what `client.containers.get(id)` returns.

    Fields that commonly need customisation are exposed as kwargs; everything
    else defaults to a reasonable shape. Always returns a fresh MagicMock —
    callers can mutate .attrs further for test-specific needs.
    """
    c = MagicMock()
    c.short_id = short_id
    c.id = short_id + "0" * max(0, 64 - len(short_id))
    c.name = name
    c.image.tags = [image_tag]
    c.image.short_id = "sha256:abcdef"
    c.status = status
    c.ports = ports if ports is not None else {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
    c.labels = labels if labels is not None else {}
    c.attrs = {
        "Id": c.id,
        "Name": "/" + name,
        "Created": "2026-01-01T00:00:00Z",
        "State": {"Status": state_status or status, "Health": health, "ExitCode": 0},
        "Config": {
            "Image": image_tag,
            "Env": env if env is not None else ["FOO=bar"],
            "Cmd": cmd if cmd is not None else ["/bin/sh"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Labels": labels or {},
            "Hostname": "myhost",
            "User": "",
            "Healthcheck": {},
        },
        "HostConfig": host_config
        if host_config is not None
        else {
            "Memory": 2 * 1024**3,
            "CpuShares": 0,
            "RestartPolicy": {"Name": "no"},
            "ReadonlyRootfs": False,
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "NetworkSettings": network_settings
        if network_settings is not None
        else {
            "Networks": {
                "bridge": {"IPAddress": "172.17.0.2", "Gateway": "172.17.0.1", "MacAddress": "02:42:ac:11:00:02"}
            },
            "Ports": {},
        },
        "Mounts": mounts if mounts is not None else [],
        "RestartCount": 0,
        "Platform": "linux",
    }
    c.reload = MagicMock()
    c.update = MagicMock()
    c.remove = MagicMock()
    c.start = MagicMock()
    c.stop = MagicMock()
    c.restart = MagicMock()
    c.pause = MagicMock()
    c.unpause = MagicMock()
    return c


def make_container_with_hc(hc_attrs: dict) -> MagicMock:
    """Shortcut for tests that only care about HostConfig values.

    Used by resource-update tests where we inspect what the endpoint passed
    to `container.update(**kw)` — we don't need the full container shape,
    just a reload-able stub whose `attrs` looks like the Docker SDK's.
    """
    return make_container(host_config=hc_attrs)


def make_volume(
    name: str = "myvol",
    driver: str = "local",
    mountpoint: str | None = None,
    labels: dict | None = None,
    scope: str = "local",
    options: dict | None = None,
    usage_data: dict | None = None,
    status: dict | None = None,
    created: str = "2026-01-01T00:00:00Z",
) -> MagicMock:
    """Mock for `client.volumes.get(name)` and entries from `client.volumes.list()`."""
    v = MagicMock()
    v.name = name
    v.attrs = {
        "Name": name,
        "Driver": driver,
        "Mountpoint": mountpoint or f"/var/lib/docker/volumes/{name}/_data",
        "CreatedAt": created,
        "Labels": labels if labels is not None else {},
        "Scope": scope,
        "Options": options or {},
        **({"UsageData": usage_data} if usage_data is not None else {}),
        **({"Status": status} if status is not None else {}),
    }
    v.remove = MagicMock()
    return v


def make_network(
    short_id: str = "netabcd1234",
    name: str = "mynet",
    driver: str = "bridge",
    scope: str = "local",
    internal: bool = False,
    ipam: dict | None = None,
    containers: dict | None = None,
) -> MagicMock:
    """Mock for Docker network list / inspect entries."""
    n = MagicMock()
    n.short_id = short_id[:12]
    n.id = short_id
    n.name = name
    n.attrs = {
        "Id": short_id,
        "Name": name,
        "Driver": driver,
        "Scope": scope,
        "Internal": internal,
        "IPAM": ipam or {"Config": []},
        "Containers": containers or {},
    }
    n.remove = MagicMock()
    n.connect = MagicMock()
    n.disconnect = MagicMock()
    return n


def make_image(
    short_id: str = "sha256:abc123",
    tags: list[str] | None = None,
    size: int = 100 * 1024 * 1024,
    created: str = "2026-01-01T00:00:00Z",
    labels: dict | None = None,
    architecture: str = "amd64",
) -> MagicMock:
    """Mock for `client.images.get(id)` / `client.images.list()` entries."""
    i = MagicMock()
    i.short_id = short_id
    i.id = short_id
    i.tags = tags if tags is not None else ["docker.io/library/nginx:latest"]
    i.labels = labels if labels is not None else {}
    i.attrs = {
        "Id": short_id,
        "RepoTags": i.tags,
        "Created": created,
        "Size": size,
        "Architecture": architecture,
        "Os": "linux",
        "Config": {"Env": [], "Cmd": ["/bin/sh"], "Labels": i.labels},
        "RootFS": {"Type": "layers", "Layers": ["sha256:layer1", "sha256:layer2"]},
        "History": [
            {"Created": created, "CreatedBy": "/bin/sh -c ...", "EmptyLayer": False},
        ],
    }
    i.remove = MagicMock()
    i.tag = MagicMock(return_value=True)
    return i


# ── Helpers frequently repeated in test bodies ────────────────────────────────


def set_container_hc(container: MagicMock, **hc_fields: Any) -> None:
    """Mutate a make_container()'s HostConfig in place. Used when a test needs
    multiple containers that share most attrs but differ on one HostConfig field.
    """
    container.attrs.setdefault("HostConfig", {}).update(hc_fields)


def stub_list_with(mock_docker: MagicMock, resource: str, items: list[MagicMock]) -> None:
    """Stub `mock_docker.<resource>.list.return_value = items` in one call.

    `resource` in {"containers", "volumes", "networks", "images"}.
    """
    getattr(mock_docker, resource).list.return_value = items
