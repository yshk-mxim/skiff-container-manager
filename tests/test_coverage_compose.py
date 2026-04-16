"""Tests for compose endpoints."""

import io
from unittest.mock import MagicMock, patch

from tests.conftest import AUTH_CSRF, AUTH_HEADER

VALID_COMPOSE = b"""
services:
  web:
    image: docker.io/library/nginx:latest
"""

BLOCKED_COMPOSE_PRIVILEGED = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    privileged: true
"""

BLOCKED_COMPOSE_HOST_VOLUME = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    volumes:
      - /host/path:/data
"""

BLOCKED_COMPOSE_NETWORK_MODE = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    network_mode: host
"""


# ── List stacks ───────────────────────────────────────────────────────────────

def test_list_compose_stacks_empty(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/compose/stacks", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_compose_stacks_with_data(client, mock_docker):
    c = MagicMock()
    c.labels = {
        "com.docker.compose.project": "myproject",
        "com.docker.compose.service": "web",
    }
    c.short_id = "abc123"
    c.status = "running"
    c.attrs = {"State": {"Status": "running"}}
    mock_docker.containers.list.return_value = [c]
    resp = client.get("/api/compose/stacks", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "myproject"
    assert data[0]["status"] == "running"


# ── Compose up ────────────────────────────────────────────────────────────────

def test_compose_up_success(client, tmp_path):
    with (
        patch("skiff.routers.compose.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(VALID_COMPOSE), "text/yaml")},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_compose_up_existing_file(client, tmp_path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    (project_dir / "docker-compose.yml").write_bytes(VALID_COMPOSE)
    with (
        patch("skiff.routers.compose.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
        )
    assert resp.status_code == 200


def test_compose_up_no_file_no_existing(client, tmp_path):
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
        )
    assert resp.status_code == 400


def test_compose_up_blocked_key(client, tmp_path):
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(BLOCKED_COMPOSE_PRIVILEGED), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_host_volume(client, tmp_path):
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(BLOCKED_COMPOSE_HOST_VOLUME), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_network_mode_host(client, tmp_path):
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(BLOCKED_COMPOSE_NETWORK_MODE), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_subprocess_failure(client, tmp_path):
    with (
        patch("skiff.routers.compose.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="/some/path/docker error: image not found",
        )
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(VALID_COMPOSE), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_subprocess_failure_no_stderr(client, tmp_path):
    with (
        patch("skiff.routers.compose.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(VALID_COMPOSE), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_timeout(client, tmp_path):
    import subprocess
    with (
        patch("skiff.routers.compose.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=120)),
    ):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(VALID_COMPOSE), "text/yaml")},
        )
    assert resp.status_code == 504


# ── Compose down ──────────────────────────────────────────────────────────────

def test_compose_down_success(client):
    with patch("skiff.routers.compose.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="down", stderr="")
        resp = client.post("/api/compose/down?project_name=myproject", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_compose_down_failure(client):
    with patch("skiff.routers.compose.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="/some/path: not found")
        resp = client.post("/api/compose/down?project_name=myproject", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_compose_down_no_stderr(client):
    with patch("skiff.routers.compose.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        resp = client.post("/api/compose/down?project_name=myproject", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_compose_down_timeout(client):
    import subprocess
    with patch("skiff.routers.compose.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=60)):
        resp = client.post("/api/compose/down?project_name=myproject", headers=AUTH_CSRF)
    assert resp.status_code == 504


# ── Compose validation: blocked top-level keys ────────────────────────────────

def test_compose_blocked_top_level_secrets(client, tmp_path):
    content = b"""
secrets:
  mysecret:
    file: ./mysecret.txt
services:
  web:
    image: docker.io/library/nginx:latest
"""
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(content), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_pid_host_blocked(client, tmp_path):
    content = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    pid: host
"""
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(content), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_ipc_host_blocked(client, tmp_path):
    content = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    ipc: host
"""
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(content), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_cap_add_blocked(client, tmp_path):
    content = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    cap_add:
      - NET_ADMIN
"""
    with patch("skiff.routers.compose.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(content), "text/yaml")},
        )
    assert resp.status_code == 400
