"""Tests for system endpoints."""

import json
from pathlib import Path
from unittest.mock import patch

import app as app_module
from tests.conftest import AUTH_CSRF, AUTH_HEADER

# ── System info ───────────────────────────────────────────────────────────────

def test_system_info(client, mock_docker):
    mock_docker.version.return_value = {"ApiVersion": "1.43", "Version": "24.0.7"}
    resp = client.get("/api/system/info", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "docker_version" in data
    assert "cpus" in data


# ── System df ─────────────────────────────────────────────────────────────────

def test_system_df(client, mock_docker):
    mock_docker.df.return_value = {
        "Images": [{"Size": 100 * 1024 * 1024, "Containers": 0}],
        "Containers": [{"SizeRw": 10 * 1024 * 1024}],
        "Volumes": [{"UsageData": {"Size": 5 * 1024 * 1024, "RefCount": 0}}],
        "BuildCache": [{"Size": 20 * 1024 * 1024, "InUse": False}],
    }
    resp = client.get("/api/system/df", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "images_mb" in data
    assert "total_mb" in data


# ── System prune ──────────────────────────────────────────────────────────────

def test_system_prune(client, mock_docker):
    mock_docker.containers.prune.return_value = {"ContainersDeleted": ["abc"], "SpaceReclaimed": 1024}
    mock_docker.images.prune.return_value = {"ImagesDeleted": [], "SpaceReclaimed": 0}
    mock_docker.networks.prune.return_value = {"NetworksDeleted": []}
    resp = client.post("/api/system/prune", headers=AUTH_CSRF)
    assert resp.status_code == 200
    data = resp.json()
    assert data["containers_deleted"] == 1


# ── Prune build cache ─────────────────────────────────────────────────────────

def test_prune_build_cache(client, mock_docker):
    mock_docker.api.prune_builds.return_value = {"SpaceReclaimed": 50 * 1024 * 1024}
    resp = client.post("/api/system/prune-build-cache", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json()["space_reclaimed_mb"] == 50.0


# ── Audit log ─────────────────────────────────────────────────────────────────

def test_audit_log_file_not_exists(client, tmp_path):
    missing = tmp_path / "missing.jsonl"
    with patch.object(app_module, "AUDIT_LOG_PATH", missing):
        resp = client.get("/api/system/audit-log", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == []


def test_audit_log_file_exists(client, tmp_path):
    log_file = tmp_path / "audit.jsonl"
    log_file.write_text(
        json.dumps({"event": "test", "severity": "INFO"}) + "\n"
        + "not-json-line\n"
        + "\n",
        encoding="utf-8",
    )
    with patch.object(app_module, "AUDIT_LOG_PATH", log_file):
        resp = client.get("/api/system/audit-log", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2  # json line + raw fallback line


def test_download_audit_log_not_exists(client, tmp_path):
    missing = tmp_path / "missing.jsonl"
    with patch.object(app_module, "AUDIT_LOG_PATH", missing):
        resp = client.get("/api/system/audit-log/download", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.text == ""


def test_download_audit_log_exists(client, tmp_path):
    log_file = tmp_path / "audit.jsonl"
    log_file.write_text('{"event":"test"}\n', encoding="utf-8")
    with patch.object(app_module, "AUDIT_LOG_PATH", log_file):
        resp = client.get("/api/system/audit-log/download", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert "event" in resp.text


# ── Config ────────────────────────────────────────────────────────────────────

def test_get_config(client):
    resp = client.get("/api/config", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "allowed_registries" in data
    assert "docker_vm_host" in data


# ── Auth required ─────────────────────────────────────────────────────────────

def test_auth_required_with_token(client):
    resp = client.get("/api/auth-required")
    assert resp.status_code == 200
    assert resp.json()["required"] is True


def test_auth_required_no_token(noauth_client):
    resp = noauth_client.get("/api/auth-required")
    assert resp.status_code == 200
    assert resp.json()["required"] is False


# ── Health ────────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data


# ── Ready ─────────────────────────────────────────────────────────────────────

def test_ready_docker_reachable(client, mock_docker):
    mock_docker.info.return_value = {"ServerVersion": "24.0.7", "ContainersRunning": 1}
    with patch("app.get_client", return_value=mock_docker):
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_ready_docker_unreachable(client):
    with patch("app.get_client", side_effect=Exception("no docker")):
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


# ── LICENSE ──────────────────────────────────────────────────────────────────

def test_license_file(client, tmp_path):
    """Test that /LICENSE is served."""
    lic = Path("LICENSE")
    if lic.exists():
        resp = client.get("/LICENSE")
        assert resp.status_code == 200
