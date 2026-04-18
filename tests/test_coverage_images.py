# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for image endpoints."""

import docker.errors

from tests.conftest import AUTH_CSRF, AUTH_HEADER
from tests.factories import make_image as _factory_make_image


def _make_image(short_id="sha256abc123", tags=None, size=100 * 1024 * 1024):
    """Image-specific test mock — factory + ExposedPorts + history shape."""
    img = _factory_make_image(short_id=short_id, tags=tags, size=size)
    img.attrs["Config"]["ExposedPorts"] = {"80/tcp": {}}
    img.attrs["Config"]["Env"] = ["PATH=/usr/local/bin"]
    img.attrs["Config"]["WorkingDir"] = "/app"
    img.history.return_value = [{"Created": "2026-01-01T00:00:00Z", "CreatedBy": "/bin/sh", "Size": 1024}]
    return img


# ── List images ───────────────────────────────────────────────────────────────


def test_list_images(client, mock_docker):
    mock_docker.images.list.return_value = [_make_image()]
    resp = client.get("/api/images", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "docker.io/library/nginx:latest" in data[0]["tags"]


def test_list_images_no_tags(client, mock_docker):
    img = _make_image()
    img.tags = []
    mock_docker.images.list.return_value = [img]
    resp = client.get("/api/images", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()[0]["tags"] == []


def test_list_allowed_images(client, mock_docker):
    mock_docker.images.list.return_value = [_make_image()]
    resp = client.get("/api/images/allowed", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


def test_list_allowed_images_filters_unallowed(client, mock_docker):
    img = _make_image(tags=["evil.example.com/img:latest"])
    mock_docker.images.list.return_value = [img]
    resp = client.get("/api/images/allowed", headers=AUTH_HEADER)
    assert resp.status_code == 200
    # evil.example.com not in allowed registries (docker.io, ghcr.io), so empty
    assert resp.json() == []


# ── Pull ─────────────────────────────────────────────────────────────────────


def test_pull_image_success(client, mock_docker):
    mock_docker.images.pull.return_value = _make_image()
    resp = client.post(
        "/api/images/pull?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_pull_image_blocked_registry(client, mock_docker):
    resp = client.post(
        "/api/images/pull?image=badregistry.io/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


def test_pull_image_api_error(client, mock_docker):
    err = docker.errors.APIError("not found")
    err.explanation = "manifest not found"
    mock_docker.images.pull.side_effect = err
    resp = client.post(
        "/api/images/pull?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


# ── Push ─────────────────────────────────────────────────────────────────────


def test_push_image_success(client, mock_docker):
    mock_docker.images.push.return_value = '{"status": "pushed"}\n'
    resp = client.post(
        "/api/images/push?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_push_image_blocked_registry(client, mock_docker):
    resp = client.post(
        "/api/images/push?image=badregistry.io/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


def test_push_image_error_in_output(client, mock_docker):
    mock_docker.images.push.return_value = '{"error": "unauthorized"}\n'
    resp = client.post(
        "/api/images/push?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


# ── Tag ──────────────────────────────────────────────────────────────────────


def test_tag_image_success(client, mock_docker):
    img = _make_image()
    mock_docker.images.get.return_value = img
    resp = client.post(
        "/api/images/abcd1234/tag?repository=docker.io/library/nginx&tag=v2",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── Delete ────────────────────────────────────────────────────────────────────


def test_delete_image(client, mock_docker):
    resp = client.delete("/api/images/abcd1234", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── Inspect ───────────────────────────────────────────────────────────────────


def test_inspect_image(client, mock_docker):
    mock_docker.images.get.return_value = _make_image()
    mock_docker.api.history.return_value = [{"Created": "2026-01-01T00:00:00Z", "CreatedBy": "/bin/sh", "Size": 1024}]
    resp = client.get("/api/images/abcd1234/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert "history" in data
    assert len(data["history"]) > 0


def test_inspect_image_history_error(client, mock_docker):
    img = _make_image()
    img.history.side_effect = docker.errors.DockerException("history fail")
    mock_docker.images.get.return_value = img
    resp = client.get("/api/images/abcd1234/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["history"] == []
