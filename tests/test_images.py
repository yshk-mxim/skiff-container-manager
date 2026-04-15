"""Tests for image endpoints."""

from unittest.mock import MagicMock

import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER


def _make_image(tag="us-docker.pkg.dev/p/r/app:latest", short_id="sha256:abc123", size=100_000_000):
    img = MagicMock()
    img.tags = [tag]
    img.short_id = short_id
    img.attrs = {
        "Size": size,
        "Created": "2026-01-01T00:00:00Z",
        "Id": f"sha256:{short_id}abc",
        "Architecture": "amd64",
        "Os": "linux",
        "RootFS": {"Layers": ["layer1", "layer2"]},
        "Config": {
            "Env": [], "Cmd": None, "Entrypoint": None, "ExposedPorts": {}, "Labels": {}, "WorkingDir": "", "User": ""
        },
    }
    img.history.return_value = []
    return img


@pytest.mark.unit
def test_list_images(client, mock_docker):
    mock_docker.images.list.return_value = [_make_image()]
    resp = client.get("/api/images", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["tag"] == "us-docker.pkg.dev/p/r/app:latest"
    assert data[0]["size_mb"] == pytest.approx(100_000_000 / 1024 / 1024, abs=0.1)


@pytest.mark.unit
def test_list_allowed_images_filters_by_registry(client, mock_docker):
    allowed = _make_image("us-docker.pkg.dev/p/r/app:latest")
    blocked = _make_image("docker.io/library/nginx:latest")
    blocked.tags = ["docker.io/library/nginx:latest"]
    mock_docker.images.list.return_value = [allowed, blocked]
    resp = client.get("/api/images/allowed", headers=AUTH_HEADER)
    assert resp.status_code == 200
    tags = [i["tag"] for i in resp.json()]
    assert "us-docker.pkg.dev/p/r/app:latest" in tags
    assert "docker.io/library/nginx:latest" not in tags


@pytest.mark.unit
def test_delete_image(client, mock_docker):
    mock_docker.images.remove.return_value = None
    resp = client.delete("/api/images/abc123def", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize("bad_id", [
    "UPPERCASE",    # uppercase not valid hex
    "xyz",          # too short
    "image name",   # spaces not allowed (URL-encoded as separate path segment)
    "a" * 65,       # too long
])
def test_delete_image_invalid_id_returns_400(client, bad_id):
    resp = client.delete(f"/api/images/{bad_id}", headers=AUTH_CSRF)
    assert resp.status_code == 400


@pytest.mark.unit
def test_pull_image_blocked_registry(client):
    resp = client.post(
        "/api/images/pull?image=docker.io/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_push_image_blocked_registry(client):
    resp = client.post(
        "/api/images/push?image=docker.io/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_inspect_image(client, mock_docker):
    mock_docker.images.get.return_value = _make_image()
    resp = client.get("/api/images/abc123def/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert "size_mb" in data
