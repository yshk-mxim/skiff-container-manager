"""Tests for image endpoints."""

from unittest.mock import MagicMock

import docker.errors

from tests.conftest import AUTH_CSRF, AUTH_HEADER


def _make_image(short_id="sha256abc123", tags=None, size=100 * 1024 * 1024):
    img = MagicMock()
    img.short_id = short_id
    img.tags = tags or ["us-docker.pkg.dev/p/r/img:latest"]
    img.attrs = {
        "Id": "sha256:" + short_id,
        "Size": size,
        "Created": "2026-01-01T00:00:00Z",
        "Architecture": "amd64",
        "Os": "linux",
        "RootFS": {"Layers": ["layer1", "layer2"]},
        "Config": {
            "Env": ["PATH=/usr/local/bin"],
            "Cmd": ["/bin/sh"],
            "Entrypoint": None,
            "ExposedPorts": {"80/tcp": {}},
            "Labels": {},
            "WorkingDir": "/app",
            "User": "",
        },
    }
    img.history.return_value = [
        {"Created": "2026-01-01T00:00:00Z", "CreatedBy": "/bin/sh", "Size": 1024}
    ]
    return img


# ── List images ───────────────────────────────────────────────────────────────

def test_list_images(client, mock_docker):
    mock_docker.images.list.return_value = [_make_image()]
    resp = client.get("/api/images", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["tag"] == "us-docker.pkg.dev/p/r/img:latest"


def test_list_images_no_tags(client, mock_docker):
    img = _make_image()
    img.tags = []
    mock_docker.images.list.return_value = [img]
    resp = client.get("/api/images", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()[0]["tag"] == img.short_id


def test_list_allowed_images(client, mock_docker):
    mock_docker.images.list.return_value = [_make_image()]
    resp = client.get("/api/images/allowed", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


def test_list_allowed_images_filters_unallowed(client, mock_docker):
    img = _make_image(tags=["docker.io/library/nginx:latest"])
    mock_docker.images.list.return_value = [img]
    resp = client.get("/api/images/allowed", headers=AUTH_HEADER)
    assert resp.status_code == 200
    # docker.io not in allowed registries (only us-docker.pkg.dev), so empty
    assert resp.json() == []


# ── Pull ─────────────────────────────────────────────────────────────────────

def test_pull_image_success(client, mock_docker):
    mock_docker.images.pull.return_value = _make_image()
    resp = client.post(
        "/api/images/pull?image=us-docker.pkg.dev/p/r/img:latest",
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
        "/api/images/pull?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


# ── Push ─────────────────────────────────────────────────────────────────────

def test_push_image_success(client, mock_docker):
    mock_docker.images.push.return_value = '{"status": "pushed"}\n'
    resp = client.post(
        "/api/images/push?image=us-docker.pkg.dev/p/r/img:latest",
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
        "/api/images/push?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


# ── Tag ──────────────────────────────────────────────────────────────────────

def test_tag_image_success(client, mock_docker):
    img = _make_image()
    mock_docker.images.get.return_value = img
    resp = client.post(
        "/api/images/abcd1234/tag?repository=us-docker.pkg.dev/p/r/img&tag=v2",
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
    resp = client.get("/api/images/abcd1234/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert "history" in data
    assert len(data["history"]) > 0


def test_inspect_image_history_error(client, mock_docker):
    img = _make_image()
    img.history.side_effect = Exception("history fail")
    mock_docker.images.get.return_value = img
    resp = client.get("/api/images/abcd1234/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["history"] == []
