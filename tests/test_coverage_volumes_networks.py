"""Tests for volume and network endpoints."""

from unittest.mock import MagicMock

from tests.conftest import AUTH_CSRF, AUTH_HEADER


def _make_volume(name="myvol"):
    v = MagicMock()
    v.name = name
    v.attrs = {
        "Driver": "local",
        "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
        "CreatedAt": "2026-01-01T00:00:00Z",
        "Labels": {},
    }
    return v


def _make_network(short_id="abcd1234", name="mynet"):
    n = MagicMock()
    n.short_id = short_id
    n.name = name
    n.attrs = {
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "IPAM": {"Config": []},
        "Containers": {},
    }
    return n


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
    resp = client.post("/api/volumes/prune", headers=AUTH_CSRF)
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
    resp = client.post("/api/networks/prune", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == ["net1"]
