"""Tests for network endpoints."""

from unittest.mock import MagicMock

import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER


def _make_network(short_id="net123", name="mynet", driver="bridge"):
    n = MagicMock()
    n.short_id = short_id
    n.name = name
    n.attrs = {
        "Driver": driver,
        "Scope": "local",
        "Internal": False,
        "IPAM": {"Config": []},
        "Containers": {},
    }
    return n


@pytest.mark.unit
def test_list_networks(client, mock_docker):
    mock_docker.networks.list.return_value = [_make_network()]
    resp = client.get("/api/networks", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "mynet"


@pytest.mark.unit
def test_create_network(client, mock_docker):
    net = _make_network()
    mock_docker.networks.create.return_value = net
    resp = client.post("/api/networks/create?name=mynet&driver=bridge", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["name"] == "mynet"


@pytest.mark.unit
def test_create_network_invalid_driver_returns_400(client):
    resp = client.post("/api/networks/create?name=mynet&driver=baddriver", headers=AUTH_CSRF)
    assert resp.status_code == 400


@pytest.mark.unit
def test_create_network_invalid_name_returns_400(client):
    resp = client.post("/api/networks/create?name=../evil&driver=bridge", headers=AUTH_CSRF)
    assert resp.status_code == 400


@pytest.mark.unit
def test_delete_default_network_blocked(client, mock_docker):
    for default_name in ("bridge", "host", "none"):
        net = _make_network(name=default_name)
        mock_docker.networks.get.return_value = net
        resp = client.delete("/api/networks/abc123def", headers=AUTH_CSRF)
        assert resp.status_code == 400


@pytest.mark.unit
def test_delete_custom_network(client, mock_docker):
    net = _make_network(name="custom-net")
    mock_docker.networks.get.return_value = net
    resp = client.delete("/api/networks/abc123def", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
def test_prune_networks(client, mock_docker):
    mock_docker.networks.prune.return_value = {"NetworksDeleted": ["oldnet"]}
    resp = client.post("/api/networks/prune", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["deleted"] == ["oldnet"]
