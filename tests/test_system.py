# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for system info, disk usage, and prune endpoints."""

import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER


@pytest.mark.unit
def test_system_info(client, mock_docker):
    resp = client.get("/api/system/info", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["docker_version"] == "24.0.7"
    assert data["cpus"] == 4
    assert "memory_gb" in data


@pytest.mark.unit
def test_system_df(client, mock_docker):
    mock_docker.df.return_value = {
        "Images": [{"Size": 200 * 1024 * 1024, "Containers": 1}],
        "Containers": [{"SizeRw": 10 * 1024 * 1024}],
        "Volumes": [{"UsageData": {"Size": 50 * 1024 * 1024, "RefCount": 1}}],
        "BuildCache": [],
    }
    resp = client.get("/api/system/df", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["images_count"] == 1
    assert data["images_mb"] == pytest.approx(200.0, abs=0.1)


@pytest.mark.unit
def test_system_prune_offers_undo_by_default(client, mock_docker):
    """System prune now defaults to undo=true so a misclick is reversible.
    The envelope carries an undo_token; the actual Docker prune calls
    don't happen until the window elapses or the token is explicitly
    confirmed (consume-on-fire)."""
    mock_docker.containers.prune.return_value = {"ContainersDeleted": ["c1"], "SpaceReclaimed": 0}
    mock_docker.images.prune.return_value = {"ImagesDeleted": [], "SpaceReclaimed": 0}
    mock_docker.networks.prune.return_value = {"NetworksDeleted": []}
    resp = client.post("/api/system/prune", headers=AUTH_CSRF)
    assert resp.status_code == 200
    data = resp.json()
    assert "undo_token" in data, (
        f"system prune must surface an undo token by default; got keys {list(data)}. "
        f"This prevents the 'misclick purged everything' bug."
    )
    assert data["expires_in"] > 0


@pytest.mark.unit
def test_system_prune_undo_false_runs_immediately(client, mock_docker):
    """Scripts that need the legacy counts envelope can opt out with
    undo=false — preserves automation compat."""
    mock_docker.containers.prune.return_value = {"ContainersDeleted": ["c1"], "SpaceReclaimed": 0}
    mock_docker.images.prune.return_value = {"ImagesDeleted": [], "SpaceReclaimed": 0}
    mock_docker.networks.prune.return_value = {"NetworksDeleted": []}
    resp = client.post("/api/system/prune?undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 200
    data = resp.json()
    assert data["containers_deleted"] == 1
    assert "undo_token" not in data


@pytest.mark.unit
def test_system_prune_build_cache(client, mock_docker):
    mock_docker.api.prune_builds.return_value = {"SpaceReclaimed": 100 * 1024 * 1024}
    resp = client.post("/api/system/prune-build-cache?undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["space_reclaimed_mb"] == pytest.approx(100.0, abs=0.1)
