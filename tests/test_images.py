# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for image endpoints."""

import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER
from tests.factories import make_image


def _make_image(tag="docker.io/library/nginx:latest", short_id="sha256:abc123", size=100_000_000):
    """Legacy wrapper preserving this file's specific kwargs shape."""
    img = make_image(short_id=short_id, tags=[tag], size=size)
    img.attrs["Config"]["ExposedPorts"] = {}
    img.history.return_value = []
    return img


@pytest.mark.unit
def test_list_images(client, mock_docker):
    mock_docker.images.list.return_value = [_make_image()]
    resp = client.get("/api/images", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "docker.io/library/nginx:latest" in data[0]["tags"]
    assert data[0]["size_mb"] == pytest.approx(100_000_000 / 1024 / 1024, abs=0.1)


@pytest.mark.unit
def test_list_allowed_images_filters_by_registry(client, mock_docker):
    allowed = _make_image("docker.io/library/nginx:latest")
    blocked = _make_image("evil.example.com/img:latest")
    blocked.tags = ["evil.example.com/img:latest"]
    mock_docker.images.list.return_value = [allowed, blocked]
    resp = client.get("/api/images/allowed", headers=AUTH_HEADER)
    assert resp.status_code == 200
    tags = [i["tag"] for i in resp.json()]
    assert "docker.io/library/nginx:latest" in tags
    assert "evil.example.com/img:latest" not in tags


@pytest.mark.unit
def test_delete_image(client, mock_docker):
    mock_docker.images.remove.return_value = None
    resp = client.delete("/api/images/abc123def", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_id",
    [
        "UPPERCASE",  # uppercase not valid hex
        "xyz",  # too short
        "image name",  # spaces not allowed (URL-encoded as separate path segment)
        "a" * 65,  # too long
    ],
)
def test_delete_image_invalid_id_returns_400(client, bad_id):
    resp = client.delete(f"/api/images/{bad_id}", headers=AUTH_CSRF)
    assert resp.status_code == 400


@pytest.mark.unit
def test_pull_image_blocked_registry(client):
    resp = client.post(
        "/api/images/pull?image=evil.example.com/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_push_image_blocked_registry(client):
    resp = client.post(
        "/api/images/push?image=evil.example.com/img:latest",
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
