# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for volume endpoints."""

import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER
from tests.factories import make_volume as _make_volume


@pytest.mark.unit
def test_list_volumes(client, mock_docker):
    mock_docker.volumes.list.return_value = [_make_volume(name="mydata")]
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/volumes", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "mydata"
    assert data[0]["in_use"] is False


@pytest.mark.unit
def test_create_volume(client, mock_docker):
    vol = _make_volume("newvol")
    mock_docker.volumes.create.return_value = vol
    resp = client.post("/api/volumes/create?name=newvol", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["name"] == "newvol"


@pytest.mark.unit
def test_create_volume_invalid_name_returns_400(client):
    resp = client.post("/api/volumes/create?name=../evil", headers=AUTH_CSRF)
    assert resp.status_code == 400


@pytest.mark.unit
def test_delete_volume(client, mock_docker):
    vol = _make_volume()
    mock_docker.volumes.get.return_value = vol
    resp = client.delete("/api/volumes/mydata", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_name",
    [
        "has space",  # spaces not allowed
        "a" * 65,  # exceeds 64-char limit
        "!invalid@chars",  # special chars not allowed
    ],
)
def test_delete_volume_invalid_name_returns_400(client, bad_name):
    resp = client.delete(f"/api/volumes/{bad_name}", headers=AUTH_CSRF)
    assert resp.status_code == 400


@pytest.mark.unit
def test_prune_volumes(client, mock_docker):
    mock_docker.volumes.prune.return_value = {
        "VolumesDeleted": ["vol1", "vol2"],
        "SpaceReclaimed": 50 * 1024 * 1024,
    }
    resp = client.post("/api/volumes/prune", headers=AUTH_CSRF)
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == ["vol1", "vol2"]
    assert data["space_reclaimed_mb"] == pytest.approx(50.0, abs=0.1)
