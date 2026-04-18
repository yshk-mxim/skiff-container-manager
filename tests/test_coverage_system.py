# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for system endpoints."""

import json
from pathlib import Path
from unittest.mock import patch

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
    with patch("skiff.config.AUDIT_LOG_PATH", missing):
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
    with patch("skiff.config.AUDIT_LOG_PATH", log_file):
        resp = client.get("/api/system/audit-log", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2  # json line + raw fallback line


def test_download_audit_log_not_exists(client, tmp_path):
    missing = tmp_path / "missing.jsonl"
    with patch("skiff.config.AUDIT_LOG_PATH", missing):
        resp = client.get("/api/system/audit-log/download", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.text == ""


def test_download_audit_log_exists(client, tmp_path):
    log_file = tmp_path / "audit.jsonl"
    log_file.write_text('{"event":"test"}\n', encoding="utf-8")
    with patch("skiff.config.AUDIT_LOG_PATH", log_file):
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
    with patch("skiff.routers.system.docker_client.get_client", return_value=mock_docker):
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_ready_docker_unreachable(client):
    import docker.errors
    with patch("skiff.routers.system.docker_client.get_client",
               side_effect=docker.errors.DockerException("no docker")):
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


# ── Debug threads ────────────────────────────────────────────────────────────

def test_debug_threads_disabled_by_default(client):
    """/debug/threads returns 403 unless SKIFF_DEBUG_THREADS=1 is set.

    Two-gate defense (AUTH + env) because stack traces can contain
    in-flight local-variable reprs.
    """
    resp = client.get("/debug/threads", headers=AUTH_HEADER)
    assert resp.status_code == 403
    assert resp.json().get("detail", {}).get("code") == "system.debug_disabled"


def test_debug_threads_when_enabled(client, monkeypatch):
    """With the env gate on, /debug/threads returns stack info."""
    import skiff.config as config_module
    monkeypatch.setattr(config_module, "DEBUG_THREADS_ENABLED", True)
    resp = client.get("/debug/threads", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "thread_count" in data
    assert data["thread_count"] >= 1
    assert "threads" in data


# ── LICENSE ──────────────────────────────────────────────────────────────────

def test_license_file(client, tmp_path):
    """Test that /LICENSE is served."""
    lic = Path("LICENSE")
    if lic.exists():
        resp = client.get("/LICENSE")
        assert resp.status_code == 200


# ── Phase 5: Prometheus metrics endpoint ──────────────────────────────────────

def test_system_metrics_prometheus_format(client, mock_docker):
    mock_docker.df.return_value = {
        "Images": [{"Size": 100 * 1024 * 1024, "Containers": 1}],
        "Containers": [{"SizeRw": 10 * 1024 * 1024}],
        "Volumes": [{"UsageData": {"Size": 5 * 1024 * 1024, "RefCount": 1}}],
        "BuildCache": [{"Size": 20 * 1024 * 1024, "InUse": True}],
    }
    resp = client.get("/api/system/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200
    # Prometheus exposition format content-type
    assert "text/plain" in resp.headers["content-type"]
    assert "version=0.0.4" in resp.headers["content-type"]
    body = resp.text
    # HELP/TYPE/value triple for a representative gauge
    assert "# HELP skiff_containers_running" in body
    assert "# TYPE skiff_containers_running gauge" in body
    assert "skiff_containers_running{docker_host=\"" in body
    # Expected values (mock returns ContainersRunning=2, Containers=5, Images=10)
    assert " 2\n" in body or " 2" in body  # running
    assert "skiff_containers_total" in body
    assert "skiff_images_total" in body
    assert "skiff_engine_cpus 4" in body
    assert "skiff_engine_memory_bytes 8589934592" in body  # 8 GiB
    # Disk gauges reflect the df mock
    assert f"skiff_disk_images_bytes {100 * 1024 * 1024}" in body
    assert f"skiff_disk_containers_bytes {10 * 1024 * 1024}" in body


def test_system_metrics_requires_auth(client, mock_docker):
    """Metrics may leak workload details (container names); require token."""
    resp = client.get("/api/system/metrics")
    assert resp.status_code == 401


def test_system_metrics_label_is_hashed_never_raw_path(client, mock_docker, monkeypatch):
    """The raw docker_host path is topology leak when a scraper is shared
    across a fleet. The label is a stable short-hash (sha256 prefix) that
    doesn't reveal the underlying socket path, regardless of what chars
    appear in DOCKER_HOST — quotes, newlines, user names, internal hosts."""
    import skiff.config as config_module
    monkeypatch.setattr(config_module._cfg, "docker_host", 'tcp://"evil"\nhost:2375')
    mock_docker.df.return_value = {}
    resp = client.get("/api/system/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.text
    # Raw path fragments MUST NOT leak into the label.
    assert "evil" not in body
    assert "2375" not in body
    # Hashed form present with the `h_` prefix the helper produces.
    assert 'docker_host="h_' in body


# ── R10: OpenAPI landing page ─────────────────────────────────────────────────

def test_openapi_schema_served_under_api_prefix(client):
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["openapi"].startswith("3.")
    assert "/api/containers" in body["paths"]


def test_default_docs_urls_disabled(client):
    """/docs and /redoc from the FastAPI defaults MUST be disabled — our CSP
    blocks their CDN assets so a live /docs would render broken. Our custom
    /api/docs replaces them."""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_api_docs_landing_renders(client):
    resp = client.get("/api/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Landing page must link to both the raw schema and an external renderer
    assert "openapi.json" in body
    assert "editor.swagger.io" in body


def test_api_docs_landing_has_loosened_csp_for_inline_script(client):
    """The landing page needs an inline script to stitch window.location into
    the editor URL. That requires 'unsafe-inline' which is NOT allowed by the
    global CSP. Per-response header override the global for this one page."""
    resp = client.get("/api/docs")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "'unsafe-inline'" in csp
    # Make sure we didn't accidentally widen beyond what this page needs
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
