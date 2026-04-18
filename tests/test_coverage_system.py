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


def test_system_df_tolerates_null_sizes(client, mock_docker):
    """Reproduces the 1.0.1 bug: Docker returns `SizeRw: null` (not 0)
    for containers that haven't written past their image layer, `Size:
    null` on build-cache entries without a materialised layer, and
    `UsageData: null` for volumes on drivers that don't report usage.
    A `.get("SizeRw", 0)` returns None in each case and the downstream
    sum() crashed or returned 0, causing the System page to show
    "0 MB" for every row.

    The fixture mixes real numbers with nulls so the endpoint exercises
    both branches: real values are counted, null values are treated as 0.
    Pre-fix this test would raise TypeError in sum(); post-fix it returns
    a numeric total without the null rows contributing zero-bytes errors.
    """
    mock_docker.df.return_value = {
        "Images": [
            {"Size": 100 * 1024 * 1024, "Containers": 1},
            {"Size": None, "Containers": 0},  # unpopulated image layer
        ],
        "Containers": [
            {"SizeRw": 10 * 1024 * 1024},
            {"SizeRw": None},  # no RW-layer writes yet
            {"SizeRw": None},
        ],
        "Volumes": [
            {"UsageData": {"Size": 5 * 1024 * 1024, "RefCount": 1}},
            {"UsageData": None},  # driver doesn't report usage
            {"UsageData": {"Size": None, "RefCount": 0}},
        ],
        "BuildCache": [
            {"Size": 20 * 1024 * 1024, "InUse": True},
            {"Size": None, "InUse": False},  # empty cache entry
        ],
    }
    resp = client.get("/api/system/df", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    # Images: 100 MiB real + null entry → 100 MiB total.
    assert data["images_mb"] == 100.0
    assert data["images_count"] == 2
    # Containers: 10 MiB real + 2 null → 10 MiB (not 0, not TypeError).
    assert data["containers_mb"] == 10.0
    assert data["containers_count"] == 3
    # Volumes: 5 MiB real + null UsageData + null Size → 5 MiB.
    assert data["volumes_mb"] == 5.0
    assert data["volumes_count"] == 3
    # Build cache: 20 MiB real + null entry → 20 MiB.
    assert data["build_cache_mb"] == 20.0
    assert data["total_mb"] == 135.0


def test_system_metrics_tolerates_null_sizes(client, mock_docker):
    """Same null-tolerance invariant for the Prometheus metrics endpoint.
    Regression guard against the same 1.0.1 bug reintroducing in the
    metrics path — both endpoints walk df() results and both used to
    share the same `.get("Size", 0)` shape."""
    mock_docker.info.return_value = {
        "Containers": 2,
        "ContainersRunning": 1,
        "ContainersPaused": 0,
        "ContainersStopped": 1,
        "Images": 2,
        "NCPU": 4,
        "MemTotal": 8 * 1024**3,
        "OperatingSystem": "Linux",
        "ServerVersion": "27.0.0",
    }
    mock_docker.df.return_value = {
        "Images": [{"Size": None, "Containers": 0}],
        "Containers": [{"SizeRw": None}],
        "Volumes": [{"UsageData": None}],
        "BuildCache": [{"Size": None, "InUse": False}],
    }
    resp = client.get("/api/system/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.text
    # Every byte-valued gauge must be present and numeric (0 is fine).
    for metric in (
        "skiff_disk_images_bytes",
        "skiff_disk_containers_bytes",
        "skiff_disk_volumes_bytes",
        "skiff_disk_build_cache_bytes",
    ):
        assert metric in body
        # The gauge line format is `<metric>{...} <number>` — find the
        # value and ensure it parses. Null coercion means it should be 0.
        line = next(line for line in body.split("\n") if line.startswith((metric + "{", metric + " ")))
        value_str = line.rsplit(" ", 1)[1]
        assert float(value_str) == 0.0, f"{metric} should be 0 on all-null df, got {value_str}"


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
        json.dumps({"event": "test", "severity": "INFO"}) + "\n" + "not-json-line\n" + "\n",
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

    with patch("skiff.routers.system.docker_client.get_client", side_effect=docker.errors.DockerException("no docker")):
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
    assert 'skiff_containers_running{docker_host="' in body
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
    """The self-hosted Swagger UI page must load and reference the
    vendored assets under /static/swagger-ui/ — never an external CDN."""
    resp = client.get("/api/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Self-hosted Swagger UI references + opens against the same-origin spec.
    assert "/static/swagger-ui/swagger-ui.css" in body
    assert "/static/swagger-ui/swagger-ui-bundle.js" in body
    assert "/static/core/docs.js" in body
    # Negative: we removed the dead editor.swagger.io links — a regression
    # should fail this assertion instead of silently reintroducing a broken
    # UX path.
    assert "editor.swagger.io" not in body
    assert "petstore.swagger.io" not in body


def test_api_docs_csp_allows_inline_style_not_inline_script(client):
    """Swagger UI ships inline <style> attributes at runtime, so the CSP for
    /api/docs keeps `style-src 'self' 'unsafe-inline'`. It must NOT grant
    `'unsafe-inline'` for scripts — the integration script lives at
    /static/core/docs.js and the Swagger UI bundle at /static/swagger-ui/."""
    resp = client.get("/api/docs")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    # Regression guard: a strict script-src lives in this CSP. If a future
    # edit re-introduces `script-src 'self' 'unsafe-inline'`, the
    # self-hosted build should not need it — catch it here.
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
