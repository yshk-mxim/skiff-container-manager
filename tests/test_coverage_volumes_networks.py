# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for volume and network endpoints."""

from unittest.mock import MagicMock

import docker.errors

from tests.conftest import AUTH_CSRF, AUTH_HEADER
from tests.factories import make_network as _make_network
from tests.factories import make_volume as _make_volume

# ── Volumes ───────────────────────────────────────────────────────────────────


def test_list_volumes(client, mock_docker):
    mock_docker.volumes.list.return_value = [_make_volume()]
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/volumes", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "myvol"


def test_list_volumes_with_container_usage(client, mock_docker):
    vol = _make_volume("data-vol")
    c = MagicMock()
    c.name = "web"
    c.attrs = {"Mounts": [{"Name": "data-vol"}]}
    mock_docker.volumes.list.return_value = [vol]
    mock_docker.containers.list.return_value = [c]
    resp = client.get("/api/volumes", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["in_use"] is True
    assert "web" in data[0]["containers"]


def test_create_volume(client, mock_docker):
    vol = _make_volume("newvol")
    mock_docker.volumes.create.return_value = vol
    resp = client.post("/api/volumes/create?name=newvol", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["name"] == "newvol"


def test_create_volume_invalid_name(client, mock_docker):
    resp = client.post("/api/volumes/create?name=invalid name!", headers=AUTH_CSRF)
    assert resp.status_code == 400
    # Envelope must TELL THE USER WHY the name is invalid — an empty
    # "invalid volume name" message left a novice staring at a silent form
    # with no hint about the space-in-name rule. Keep this assertion
    # strict: any future message change must keep the guidance.
    detail = resp.json()["detail"]
    assert detail["code"] == "volume.bad_name"
    msg = detail["message"].lower()
    assert "letters" in msg or "alphanumeric" in msg or "a-z" in msg, msg
    assert "space" in msg or "punctuation" in msg or "slash" in msg, msg


def test_delete_volume(client, mock_docker):
    vol = _make_volume()
    mock_docker.volumes.get.return_value = vol
    resp = client.delete("/api/volumes/myvol", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_volume_invalid_name(client, mock_docker):
    resp = client.delete("/api/volumes/invalid name!", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_prune_volumes(client, mock_docker):
    mock_docker.volumes.prune.return_value = {
        "VolumesDeleted": ["vol1", "vol2"],
        "SpaceReclaimed": 1024 * 1024 * 10,
    }
    resp = client.post("/api/volumes/prune?undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == ["vol1", "vol2"]
    assert data["space_reclaimed_mb"] == 10.0


# ── Networks ──────────────────────────────────────────────────────────────────


def test_list_networks(client, mock_docker):
    mock_docker.networks.list.return_value = [_make_network()]
    resp = client.get("/api/networks", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "mynet"


def test_create_network(client, mock_docker):
    net = _make_network()
    mock_docker.networks.create.return_value = net
    resp = client.post("/api/networks/create?name=mynet&driver=bridge", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["name"] == "mynet"


def test_create_network_invalid_name(client, mock_docker):
    resp = client.post("/api/networks/create?name=bad name!", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_create_network_invalid_driver(client, mock_docker):
    resp = client.post("/api/networks/create?name=mynet&driver=baddriver", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_delete_network(client, mock_docker):
    net = _make_network(short_id="abcd1234", name="mynet")
    mock_docker.networks.get.return_value = net
    resp = client.delete("/api/networks/abcd1234", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_network_builtin_rejected(client, mock_docker):
    net = _make_network(short_id="abcd1234", name="bridge")
    mock_docker.networks.get.return_value = net
    resp = client.delete("/api/networks/abcd1234", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_delete_network_invalid_id(client, mock_docker):
    resp = client.delete("/api/networks/invalid-id!", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_connect_container_to_network(client, mock_docker):
    net = _make_network()
    container = MagicMock()
    mock_docker.networks.get.return_value = net
    mock_docker.containers.get.return_value = container
    resp = client.post(
        "/api/networks/abcd1234/connect?container_id=abc1234567890123",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_connect_container_invalid_network_id(client, mock_docker):
    resp = client.post(
        "/api/networks/bad-id!/connect?container_id=abc1234",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


def test_disconnect_container_from_network(client, mock_docker):
    net = _make_network()
    container = MagicMock()
    mock_docker.networks.get.return_value = net
    mock_docker.containers.get.return_value = container
    resp = client.post(
        "/api/networks/abcd1234/disconnect?container_id=abc1234567890123",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200


def test_disconnect_container_invalid_network_id(client, mock_docker):
    resp = client.post(
        "/api/networks/bad-id!/disconnect?container_id=abc1234",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


def test_prune_networks(client, mock_docker):
    mock_docker.networks.prune.return_value = {"NetworksDeleted": ["net1"]}
    resp = client.post("/api/networks/prune?undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == ["net1"]


# ── Phase 6: Volume inspect endpoint ──────────────────────────────────────────


def test_volume_inspect_happy_path(client, mock_docker):
    v = _make_volume("analytics")
    v.attrs.update(
        {
            "Scope": "local",
            "Options": {"type": "nfs", "o": "addr=10.0.0.5,rw"},
            "UsageData": {"Size": 42 * 1024 * 1024, "RefCount": 2},
            "Labels": {"env": "prod"},
        }
    )
    mock_docker.volumes.get.return_value = v
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/volumes/analytics/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "analytics"
    assert data["driver"] == "local"
    assert data["scope"] == "local"
    assert data["options"] == {"type": "nfs", "o": "addr=10.0.0.5,rw"}
    assert data["usage_bytes"] == 42 * 1024 * 1024
    assert data["ref_count"] == 2
    assert data["labels"] == {"env": "prod"}


def test_volume_inspect_no_usage_data(client, mock_docker):
    """Drivers that don't report df data → usage_bytes=-1 (sentinel, not confused with 0)."""
    v = _make_volume("basic")
    # No UsageData key
    mock_docker.volumes.get.return_value = v
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/volumes/basic/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["usage_bytes"] == -1
    assert resp.json()["ref_count"] == -1


def test_volume_inspect_invalid_name(client, mock_docker):
    resp = client.get("/api/volumes/..%2Fetc%2Fpasswd/inspect", headers=AUTH_HEADER)
    # URL routing may 400 or 404 depending on the value; either is acceptable
    # as long as the Docker SDK is never called.
    assert resp.status_code in (400, 404)
    mock_docker.volumes.get.assert_not_called()


def test_volume_inspect_missing_volume(client, mock_docker):
    import docker.errors

    mock_docker.volumes.get.side_effect = docker.errors.NotFound("no such volume")
    resp = client.get("/api/volumes/ghostvol/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 404


def test_volume_inspect_containers_using_it(client, mock_docker):
    """Container usage scan returns the names of containers mounting this volume."""
    v = _make_volume("shared")
    mock_docker.volumes.get.return_value = v
    # Two containers: one mounts, one doesn't
    mounting = MagicMock()
    mounting.name = "consumer"
    mounting.attrs = {"Mounts": [{"Name": "shared", "Destination": "/data"}]}
    unrelated = MagicMock()
    unrelated.name = "unrelated"
    unrelated.attrs = {"Mounts": [{"Name": "other", "Destination": "/x"}]}
    mock_docker.containers.list.return_value = [mounting, unrelated]
    resp = client.get("/api/volumes/shared/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["containers"] == ["consumer"]


def test_volume_inspect_requires_auth(client):
    resp = client.get("/api/volumes/anything/inspect")
    assert resp.status_code == 401


def test_volume_inspect_invalid_name_direct(client, mock_docker):
    """Hit the 400 validation path with a bad-char name that still reaches the endpoint."""
    # Dots alone are not rejected by FastAPI routing but fail the regex (must start
    # with alphanumeric, not a dot)
    resp = client.get("/api/volumes/.hidden/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 400
    mock_docker.volumes.get.assert_not_called()


def test_volume_inspect_container_scan_error_swallowed(client, mock_docker):
    """If containers.list() raises, the inspect still returns (containers list empty)."""
    v = _make_volume("resilient")
    mock_docker.volumes.get.return_value = v
    # R5: narrowed except clause only catches docker.errors.DockerException now.
    mock_docker.containers.list.side_effect = docker.errors.DockerException("scan failed")
    resp = client.get("/api/volumes/resilient/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["containers"] == []
