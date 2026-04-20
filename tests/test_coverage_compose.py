# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for compose endpoints."""

import io
from unittest.mock import MagicMock, patch

import docker.errors
import pytest

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
        patch("skiff.config.COMPOSE_DIR", tmp_path),
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
        patch("skiff.config.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
        )
    assert resp.status_code == 200


def test_compose_up_no_file_no_existing(client, tmp_path):
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
        )
    assert resp.status_code == 400


def test_compose_up_blocked_key(client, tmp_path):
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(BLOCKED_COMPOSE_PRIVILEGED), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_host_volume(client, tmp_path):
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(BLOCKED_COMPOSE_HOST_VOLUME), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_network_mode_host(client, tmp_path):
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(BLOCKED_COMPOSE_NETWORK_MODE), "text/yaml")},
        )
    assert resp.status_code == 400


def test_compose_up_subprocess_failure(client, tmp_path):
    with (
        patch("skiff.config.COMPOSE_DIR", tmp_path),
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
        patch("skiff.config.COMPOSE_DIR", tmp_path),
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
        patch("skiff.config.COMPOSE_DIR", tmp_path),
        patch("skiff.routers.compose.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=120)),
    ):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(VALID_COMPOSE), "text/yaml")},
        )
    assert resp.status_code == 504


# ── Compose down ──────────────────────────────────────────────────────────────
#
# `compose down` now requires an existing project directory (the handler
# returns 404 `compose.not_found` if no deploy ever happened for this
# name, instead of silently creating an empty dir just to shell out to
# `docker compose down` against a non-existent stack). The fixture
# pre-creates the dir so the subprocess branches (404 / 400 / 504)
# stay exercised.
@pytest.fixture
def _existing_compose_dir():
    import shutil

    import skiff.config as _cfg

    proj_dir = _cfg.COMPOSE_DIR / "myproject"
    proj_dir.mkdir(parents=True, exist_ok=True)
    yield proj_dir
    shutil.rmtree(proj_dir, ignore_errors=True)


def test_compose_down_success(client, _existing_compose_dir):
    with patch("skiff.routers.compose.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="down", stderr="")
        resp = client.post("/api/compose/down?project_name=myproject&undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_compose_down_failure(client, _existing_compose_dir):
    with patch("skiff.routers.compose.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="/some/path: not found")
        resp = client.post("/api/compose/down?project_name=myproject&undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_compose_down_no_stderr(client, _existing_compose_dir):
    with patch("skiff.routers.compose.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        resp = client.post("/api/compose/down?project_name=myproject&undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_compose_down_timeout(client, _existing_compose_dir):
    import subprocess

    with patch("skiff.routers.compose.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=60)):
        resp = client.post("/api/compose/down?project_name=myproject&undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 504


def test_compose_down_missing_project_returns_404(client):
    """Teardown on a name that was never deployed returns 404 without creating a dir."""
    import skiff.config as _cfg

    resp = client.post("/api/compose/down?project_name=nonexistent-xyz&undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "compose.not_found"
    # Prove the handler did NOT create the project dir as a side effect.
    assert not (_cfg.COMPOSE_DIR / "nonexistent-xyz").exists()


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
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
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
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
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
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
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
    with patch("skiff.config.COMPOSE_DIR", tmp_path):
        resp = client.post(
            "/api/compose/up?project_name=myproject",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(content), "text/yaml")},
        )
    assert resp.status_code == 400


# ── Phase 3: Per-service logs + restart ───────────────────────────────────────


def _svc_container(cid, project, service, state="running"):
    c = MagicMock()
    c.short_id = cid
    c.id = cid + "0" * (64 - len(cid))
    c.name = project + "-" + service + "-1"
    c.labels = {"com.docker.compose.project": project, "com.docker.compose.service": service}
    c.attrs = {"State": {"Status": state}}
    return c


def test_compose_project_logs_aggregates_services(client, mock_docker):
    """Per-service logs are prefixed with service name and concatenated."""
    web = _svc_container("web1234567890", "demo", "web")
    db = _svc_container("db1234567890ab", "demo", "db")
    web.logs.return_value = b"2026-01-01T00:00:00Z hello from web\n"
    db.logs.return_value = b"2026-01-01T00:00:01Z postgres ready\n"
    mock_docker.containers.list.return_value = [web, db]
    resp = client.get("/api/compose/demo/logs", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "demo"
    assert data["service"] is None
    combined = "\n".join(data["lines"])
    assert "web | " in combined
    assert "db | " in combined
    assert "hello from web" in combined
    assert "postgres ready" in combined


def test_compose_project_logs_filters_by_service(client, mock_docker):
    """?service=<name> returns only that service's lines."""
    web = _svc_container("web1234567890", "demo", "web")
    web.logs.return_value = b"2026-01-01T00:00:00Z web only line\n"
    # Docker's list(filters=...) is what filters — we return just web in the mock
    mock_docker.containers.list.return_value = [web]
    resp = client.get("/api/compose/demo/logs?service=web", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "web"
    assert all("web |" in line for line in data["lines"])


def test_compose_project_logs_no_containers_404(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/compose/ghostproject/logs", headers=AUTH_HEADER)
    assert resp.status_code == 404


def test_compose_project_logs_invalid_project(client, mock_docker):
    """Project name outside the allowlist regex is rejected up front."""
    resp = client.get("/api/compose/Bad..Name/logs", headers=AUTH_HEADER)
    assert resp.status_code == 400
    mock_docker.containers.list.assert_not_called()


def test_compose_project_logs_invalid_service(client, mock_docker):
    resp = client.get("/api/compose/demo/logs?service=../etc", headers=AUTH_HEADER)
    assert resp.status_code == 400
    mock_docker.containers.list.assert_not_called()


def test_compose_project_logs_tail_clamped(client, mock_docker):
    """Huge tail values are silently clamped to MAX_LOG_TAIL."""
    web = _svc_container("web1234567890", "demo", "web")
    web.logs.return_value = b""
    mock_docker.containers.list.return_value = [web]
    resp = client.get("/api/compose/demo/logs?tail=9999999", headers=AUTH_HEADER)
    assert resp.status_code == 200
    # Verify the kwarg passed to c.logs was within the cap
    from skiff.config import MAX_LOG_TAIL

    assert web.logs.call_args.kwargs["tail"] <= MAX_LOG_TAIL


def test_compose_project_logs_per_service_failure_is_soft(client, mock_docker):
    """If one service's logs() raises, other services still return logs."""
    web = _svc_container("web1234567890", "demo", "web")
    db = _svc_container("db1234567890ab", "demo", "db")
    web.logs.side_effect = docker.errors.DockerException("boom")
    db.logs.return_value = b"2026-01-01T00:00:01Z db ok\n"
    mock_docker.containers.list.return_value = [web, db]
    resp = client.get("/api/compose/demo/logs", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    combined = "\n".join(data["lines"])
    assert "db ok" in combined
    assert "web | " not in combined  # nothing from the failing service


def test_compose_service_restart_happy_path(client, mock_docker):
    web = _svc_container("web1234567890", "demo", "web")
    mock_docker.containers.list.return_value = [web]
    resp = client.post("/api/compose/demo/services/web/restart", headers=AUTH_CSRF)
    assert resp.status_code == 200
    web.restart.assert_called_once()
    assert resp.json()["restarted"] == ["web1234567890"]


def test_compose_service_restart_requires_csrf(client, mock_docker):
    web = _svc_container("web1234567890", "demo", "web")
    mock_docker.containers.list.return_value = [web]
    resp = client.post(
        "/api/compose/demo/services/web/restart",
        headers={"Authorization": AUTH_CSRF["Authorization"]},  # no X-Requested-With
    )
    assert resp.status_code == 403
    web.restart.assert_not_called()


def test_compose_service_restart_unknown_service_404(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post("/api/compose/demo/services/ghost/restart", headers=AUTH_CSRF)
    assert resp.status_code == 404


def test_compose_service_restart_invalid_service_name(client, mock_docker):
    resp = client.post("/api/compose/demo/services/..%2Fetc/restart", headers=AUTH_CSRF)
    # URL path routing will reject the slash, but the regex also rejects ".." separately
    assert resp.status_code in (400, 404)
    mock_docker.containers.list.assert_not_called()


def test_compose_service_restart_invalid_project(client, mock_docker):
    resp = client.post("/api/compose/BAD..NAME/services/web/restart", headers=AUTH_CSRF)
    assert resp.status_code == 400
    mock_docker.containers.list.assert_not_called()
