"""Tests for middleware, helpers, and validation functions."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

import app as app_module
from app import (
    _sanitize_stderr,
    _validate_mount_target,
    validate_compose_file,
    validate_container_name,
    validate_image_registry,
)
from tests.conftest import AUTH_HEADER

# ── SecurityHeadersMiddleware ─────────────────────────────────────────────────

def test_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=(), usb=()"


def test_security_headers_hsts_for_https(client):
    resp = client.get("/health", headers={"x-forwarded-proto": "https"})
    assert "Strict-Transport-Security" in resp.headers


def test_security_headers_no_hsts_for_http(client):
    resp = client.get("/health")
    assert "Strict-Transport-Security" not in resp.headers


# ── AuditLogMiddleware ────────────────────────────────────────────────────────

def test_audit_log_middleware_logs_api_requests(client, mock_docker):
    mock_docker.containers.list.return_value = []
    # Just make an API call and verify it doesn't crash (middleware logs it)
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200


# ── _sanitize_stderr ──────────────────────────────────────────────────────────

def test_sanitize_stderr_strips_paths():
    result = _sanitize_stderr("/some/internal/path/docker error here")
    assert "/some/internal/path" not in result
    assert "[path]" in result


def test_sanitize_stderr_truncates():
    long_stderr = "error " * 200
    result = _sanitize_stderr(long_stderr)
    assert len(result) <= 400


# ── _validate_mount_target ────────────────────────────────────────────────────

def test_validate_mount_target_valid():
    # Should not raise
    _validate_mount_target("/data")
    _validate_mount_target("/app/data")
    _validate_mount_target("/home/user/stuff")


def test_validate_mount_target_not_absolute():
    with pytest.raises(HTTPException) as exc:
        _validate_mount_target("relative/path")
    assert exc.value.status_code == 400


def test_validate_mount_target_blocked_etc():
    with pytest.raises(HTTPException) as exc:
        _validate_mount_target("/etc")
    assert exc.value.status_code == 400


def test_validate_mount_target_blocked_etc_subpath():
    with pytest.raises(HTTPException) as exc:
        _validate_mount_target("/etc/passwd")
    assert exc.value.status_code == 400


def test_validate_mount_target_blocked_proc():
    with pytest.raises(HTTPException) as exc:
        _validate_mount_target("/proc/self")
    assert exc.value.status_code == 400


def test_validate_mount_target_blocked_sys():
    with pytest.raises(HTTPException) as exc:
        _validate_mount_target("/sys/kernel")
    assert exc.value.status_code == 400


def test_validate_mount_target_blocked_dev():
    with pytest.raises(HTTPException) as exc:
        _validate_mount_target("/dev/null")
    assert exc.value.status_code == 400


# ── validate_container_name ────────────────────────────────────────────────────

def test_validate_container_name_none_returns_none():
    assert validate_container_name(None) is None


def test_validate_container_name_valid():
    assert validate_container_name("mycontainer") == "mycontainer"
    assert validate_container_name("my-container.1") == "my-container.1"


def test_validate_container_name_invalid():
    with pytest.raises(HTTPException) as exc:
        validate_container_name("invalid name!")
    assert exc.value.status_code == 400


# ── validate_image_registry ────────────────────────────────────────────────────

def test_validate_image_registry_allowed():
    # Should not raise for allowed registry
    validate_image_registry("us-docker.pkg.dev/p/r/img:latest")


def test_validate_image_registry_blocked():
    with pytest.raises(HTTPException) as exc:
        validate_image_registry("badregistry.io/img:latest")
    assert exc.value.status_code == 400


def test_validate_image_registry_invalid_format():
    with pytest.raises(HTTPException) as exc:
        validate_image_registry("!invalid")
    assert exc.value.status_code == 400


def test_validate_image_registry_short_name_no_docker_io():
    """Short names (nginx) rejected when docker.io not in allowed list."""
    with pytest.raises(HTTPException) as exc:
        validate_image_registry("nginx")
    assert exc.value.status_code == 400


def test_validate_image_registry_empty_allowed():
    """Empty ALLOWED_REGISTRIES allows all registries."""
    with patch.object(app_module, "ALLOWED_REGISTRIES", []):
        # Should not raise
        validate_image_registry("anyregistry.io/img:latest")


def test_validate_image_registry_docker_io_allowed():
    """Short names allowed when docker.io in allowed list."""
    with patch.object(app_module, "ALLOWED_REGISTRIES", ["docker.io"]):
        validate_image_registry("nginx")


# ── validate_compose_file ──────────────────────────────────────────────────────

def test_validate_compose_file_too_large():
    content = b"a" * (256 * 1024 + 1)
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


def test_validate_compose_file_invalid_yaml():
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(b"{ invalid: yaml: :")
    assert exc.value.status_code == 400


def test_validate_compose_file_not_mapping():
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(b"- item1\n- item2\n")
    assert exc.value.status_code == 400


def test_validate_compose_file_invalid_services():
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(b"services: not-a-mapping\n")
    assert exc.value.status_code == 400


def test_validate_compose_file_service_not_mapping():
    content = b"services:\n  web: not-a-mapping\n"
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


def test_validate_compose_file_blocked_truthy_false_allowed():
    """cap_add: false should be allowed (falsy value override)."""
    content = b"""
services:
  web:
    image: us-docker.pkg.dev/p/r/img:latest
    cap_add: false
"""
    # Should not raise
    result = validate_compose_file(content)
    assert "services" in result


def test_validate_compose_volume_dict_format(tmp_path):
    """Volume as dict with source key."""
    content = b"""
services:
  web:
    image: us-docker.pkg.dev/p/r/img:latest
    volumes:
      - source: /host/path
        target: /app
        type: bind
"""
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


# ── Rate limiting ─────────────────────────────────────────────────────────────

def test_rate_limit_health_not_limited(client):
    """Health endpoint is not rate limited — should always return 200."""
    for _ in range(5):
        resp = client.get("/health")
        assert resp.status_code == 200
