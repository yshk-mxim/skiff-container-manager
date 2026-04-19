# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for network endpoints."""

from unittest.mock import MagicMock

import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER
from tests.factories import make_network as _make_network


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


def test_network_inspect_accepts_name(client, mock_docker):
    """hb-network-connect-by-name: inspect accepts hex id OR name.
    Earlier the regex was hex-only; now matches NETWORK_NAME_RE too."""
    net = _make_network()
    mock_docker.networks.get.return_value = net
    resp = client.get("/api/networks/my-app-bridge/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200


def test_network_delete_accepts_name(client, mock_docker):
    net = _make_network()
    net.name = "my-app"
    mock_docker.networks.get.return_value = net
    resp = client.delete("/api/networks/my-app", headers=AUTH_CSRF)
    assert resp.status_code == 200


def test_network_connect_accepts_name(client, mock_docker):
    net = _make_network()
    mock_docker.networks.get.return_value = net
    cont = MagicMock()
    cont.short_id = "abc123def456"
    mock_docker.containers.get.return_value = cont
    resp = client.post(
        "/api/networks/my-app/connect?container_id=abc123def456",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200


def test_network_disconnect_accepts_name(client, mock_docker):
    net = _make_network()
    mock_docker.networks.get.return_value = net
    cont = MagicMock()
    cont.short_id = "abc123def456"
    mock_docker.containers.get.return_value = cont
    resp = client.post(
        "/api/networks/my-app/disconnect?container_id=abc123def456",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200


def test_network_create_rejects_bad_label_key(client, mock_docker):
    """Invalid label-key hits line 84 of _parse_net_labels."""
    resp = client.post(
        "/api/networks/create?name=labtest&driver=bridge&labels=%21bad%3Dv",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "network.bad_labels"


def test_network_create_rejects_label_missing_equals(client, mock_docker):
    """Line 79: label entry without '='."""
    resp = client.post(
        "/api/networks/create?name=labtest2&driver=bridge&labels=noequalshere",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "network.bad_labels"


def test_network_create_rejects_bad_label_value(client, mock_docker):
    """Line 86: valid key but invalid value (non-printable byte)."""
    # \x01 is outside DOCKER_LABEL_VAL_RE's [\x20-\x7e] range.
    resp = client.post(
        "/api/networks/create?name=labtest3&driver=bridge&labels=key%3D%01",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "network.bad_labels"


def test_network_create_rejects_gateway_without_subnet(client, mock_docker):
    """Line 160-ish: gateway set but no subnet."""
    resp = client.post(
        "/api/networks/create?name=gwo&driver=bridge&gateway=10.0.0.1",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400
