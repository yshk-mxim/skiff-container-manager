"""Tests for container lifecycle endpoints."""

from unittest.mock import MagicMock

import docker.errors
import pytest

from tests.conftest import AUTH_CSRF, AUTH_HEADER


def _make_container(
    short_id="abc123def",
    name="my-service",
    image_tag="docker.io/library/nginx:latest",
    status="running",
    state_status="running",
    ports=None,
    created="2026-01-01T00:00:00Z",
):
    c = MagicMock()
    c.short_id = short_id
    c.name = name
    c.image.tags = [image_tag]
    c.status = status
    c.ports = ports or {}
    c.attrs = {
        "Created": created,
        "State": {"Status": state_status, "Health": None},
    }
    return c


# ── List containers ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_list_containers_empty(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.unit
def test_list_containers_returns_fields(client, mock_docker):
    mock_docker.containers.list.return_value = [_make_container()]
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "abc123def"
    assert data[0]["name"] == "my-service"
    assert data[0]["status"] == "running"


@pytest.mark.unit
def test_list_containers_requires_auth(client):
    resp = client.get("/api/containers")
    assert resp.status_code == 401


# ── Start / stop / restart ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_start_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def/start", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.unit
def test_stop_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def/stop", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
def test_restart_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def/restart", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
def test_start_not_found_returns_404(client, mock_docker):
    mock_docker.containers.get.side_effect = docker.errors.NotFound("no such container")
    resp = client.post("/api/containers/abc123def/start", headers=AUTH_CSRF)
    assert resp.status_code == 404


# ── Kill ───────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_kill_valid_signal(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def/kill?signal=SIGTERM", headers=AUTH_CSRF)
    assert resp.status_code == 200


@pytest.mark.unit
def test_kill_invalid_signal_returns_400(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def/kill?signal=SIGPWN", headers=AUTH_CSRF)
    assert resp.status_code == 400


# ── Delete ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_delete_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.delete("/api/containers/abc123def", headers=AUTH_CSRF)
    assert resp.status_code == 200


# ── Logs ───────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_get_logs(client, mock_docker):
    container = _make_container()
    container.logs.return_value = b"2026-01-01 line one\n2026-01-01 line two\n"
    mock_docker.containers.get.return_value = container
    resp = client.get("/api/containers/abc123def/logs", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert "line one" in resp.json()["logs"]


@pytest.mark.unit
def test_logs_tail_too_large_clamped(client, mock_docker):
    container = _make_container()
    container.logs.return_value = b""
    mock_docker.containers.get.return_value = container
    # tail > MAX_LOG_TAIL (5000) should be rejected by Query validation
    resp = client.get("/api/containers/abc123def/logs?tail=99999", headers=AUTH_HEADER)
    assert resp.status_code == 422


# ── Run container ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_run_container_blocked_registry(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=evil.example.com/img:latest",
        headers=AUTH_CSRF,
        json={},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_host_path_volume_blocked(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["/etc:/etc"]},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_invalid_env_format(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"environment": ["NOEQUALSIGN"]},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_invalid_restart_policy(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"restart_policy": "never"},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_limit_enforced(client, mock_docker):
    """Returns 400 when container count is at MAX_CONTAINERS (50)."""
    mock_docker.containers.list.return_value = [MagicMock()] * 50
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_success(client, mock_docker):
    mock_docker.containers.list.return_value = []
    new_container = MagicMock()
    new_container.short_id = "new123"
    new_container.name = "myapp"
    new_container.status = "created"
    mock_docker.containers.run.return_value = new_container
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest&name=myapp",
        headers=AUTH_CSRF,
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "new123"


# ── Invalid container ID format ───────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("bad_id", [
    "ABC",          # uppercase not allowed
    "x",            # too short (< 4 hex chars)
    "a" * 65,       # too long (> 64 chars)
    "xyz-123",      # hyphen not allowed in hex ID
])
def test_invalid_id_format_returns_400(client, bad_id):
    resp = client.post(f"/api/containers/{bad_id}/start", headers=AUTH_CSRF)
    assert resp.status_code == 400
