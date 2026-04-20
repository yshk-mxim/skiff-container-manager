# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for middleware, helpers, and validation functions."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

import skiff.config as config_module
from skiff.validators import (
    _sanitize_stderr,
    _validate_mount_target,
    validate_compose_file,
    validate_container_name,
    validate_image_registry,
)
from tests.conftest import AUTH_CSRF, AUTH_HEADER

# ── SecurityHeadersMiddleware ─────────────────────────────────────────────────


def test_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=(), usb=()"


def test_security_headers_hsts_for_https(client, monkeypatch):
    """HSTS emits on TLS — but only when the front proxy is trusted."""
    import skiff.config as cfg

    monkeypatch.setattr(cfg, "TRUST_FORWARDED_HEADERS", True)
    resp = client.get("/health", headers={"x-forwarded-proto": "https"})
    assert "Strict-Transport-Security" in resp.headers


def test_security_headers_no_hsts_for_http(client):
    resp = client.get("/health")
    assert "Strict-Transport-Security" not in resp.headers


def test_security_headers_no_hsts_when_forwarded_proto_untrusted(client):
    """Without TRUST_FORWARDED_HEADERS, X-Forwarded-Proto cannot flip HSTS."""
    resp = client.get("/health", headers={"x-forwarded-proto": "https"})
    assert "Strict-Transport-Security" not in resp.headers


# ── AuditLogMiddleware ────────────────────────────────────────────────────────


def test_audit_log_middleware_logs_api_requests(client, mock_docker):
    mock_docker.containers.list.return_value = []
    # Just make an API call and verify it doesn't crash (middleware logs it)
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200


# ── R17 BodySizeLimitMiddleware ───────────────────────────────────────────────


def test_body_size_limit_rejects_oversize_content_length(client):
    """Content-Length > cap returns 413 before touching the router."""
    import skiff.config as cfg

    oversize = b"x" * (cfg.MAX_BODY_BYTES + 1)
    resp = client.post(
        "/api/compose/up?project_name=demo",
        headers={**AUTH_CSRF, "Content-Type": "application/octet-stream"},
        content=oversize,
    )
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert detail["code"] == "validation.body_too_large"
    # Envelope must name the server knob AND quote the actual cap so the
    # user can fix it without digging through logs. Was previously a bare
    # "exceeds size cap" string that taught the user nothing.
    msg = detail["message"]
    assert "MAX_BODY_BYTES" in msg, msg
    assert "KiB" in msg or "MiB" in msg or "B" in msg, msg


def test_body_size_limit_allows_small_requests(client, mock_docker):
    mock_docker.containers.list.return_value = []
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
    validate_image_registry("docker.io/library/nginx:latest")


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
    with patch.object(config_module._cfg, "allowed_registries", ["ghcr.io"]):
        with pytest.raises(HTTPException) as exc:
            validate_image_registry("nginx")
        assert exc.value.status_code == 400


def test_validate_image_registry_empty_rejects_everything():
    """Empty allowed_registries is fail-CLOSED (Loop 10 M10-SWE-M2).

    Prior behaviour was permissive ("no allowlist → accept any"),
    which gave an operator who cleared the knob to "lock down"
    the OPPOSITE of what they expected. Now an empty list rejects
    every pull; the operator must express permissive posture
    positively by naming specific registries.
    """
    with patch.object(config_module._cfg, "allowed_registries", []):
        with pytest.raises(HTTPException) as exc:
            validate_image_registry("anyregistry.io/img:latest")
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "image.registry_blocked"


def test_validate_image_registry_docker_io_allowed():
    """Short names allowed when docker.io in allowed list."""
    with patch.object(config_module._cfg, "allowed_registries", ["docker.io"]):
        validate_image_registry("nginx")


# ── validate_compose_file ──────────────────────────────────────────────────────


def test_validate_compose_file_too_large():
    from skiff import config as _cfg

    content = b"a" * (_cfg.MAX_COMPOSE_SIZE + 1)
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400
    # Envelope message must quote the knob name so the user knows where
    # to raise the cap — "compose file too large" without a pointer left
    # ops digging through source.
    assert "MAX_COMPOSE_SIZE" in exc.value.detail["message"]


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
    image: docker.io/library/nginx:latest
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
    image: docker.io/library/nginx:latest
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


# ── GCP/Kubernetes-style resource quantity parsers ────────────────────────────
# parse_memory_quantity + parse_cpu_quantity underpin POST /api/containers/{id}/update.
# Parser correctness is CRITICAL: an off-by-unit bug could let a user bypass the
# MAX_CONTAINER_MEM / MAX_CONTAINER_CPU caps. Test every unit + a pile of errors.

from skiff.validators import parse_cpu_quantity, parse_memory_quantity


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (1024, 1024),
        ("0", 0),
        ("1024", 1024),
        ("256Mi", 256 * 1024 * 1024),
        ("1Gi", 1024**3),
        ("2Gi", 2 * 1024**3),
        ("500M", 500 * 10**6),
        ("1G", 10**9),
        ("512Ki", 512 * 1024),
        ("1Ti", 1024**4),
        ("100k", 100 * 1000),
        ("0.5Gi", int(0.5 * 1024**3)),
    ],
)
def test_parse_memory_quantity_valid(value, expected):
    assert parse_memory_quantity(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "256Xi",  # unknown unit
        "Mi256",  # unit before number
        "-100",  # negative int as string (regex rejects)
        "256 Mi 512",  # extra tokens
        None,  # not a str/int
        [],  # not a str/int
        True,  # bool is a subclass of int — must be rejected explicitly
        False,
    ],
)
def test_parse_memory_quantity_invalid(value):
    with pytest.raises(HTTPException) as exc:
        parse_memory_quantity(value)
    assert exc.value.status_code == 400


def test_parse_memory_quantity_empty_string_returns_zero():
    """Empty string parses to 0; `_apply_memory` then rejects 0 with
    `container.memory_uncap_unsupported` because Docker Engine ignores
    `memory=0` on a running container (silently) — the API must surface
    the reality instead of returning a misleading 200."""
    assert parse_memory_quantity("") == 0


def test_parse_memory_quantity_negative_int_rejected():
    with pytest.raises(HTTPException):
        parse_memory_quantity(-1)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0.0),
        (1, 1.0),
        (0.5, 0.5),
        ("0", 0.0),
        ("1", 1.0),
        ("2", 2.0),
        ("0.5", 0.5),
        ("500m", 0.5),
        ("100m", 0.1),
        ("2000m", 2.0),
    ],
)
def test_parse_cpu_quantity_valid(value, expected):
    assert parse_cpu_quantity(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "500mm",  # invalid suffix
        "m500",  # suffix before number
        "-1",
        None,
        [],
        True,  # bool subclass of int — must be rejected explicitly
        False,
    ],
)
def test_parse_cpu_quantity_invalid(value):
    with pytest.raises(HTTPException) as exc:
        parse_cpu_quantity(value)
    assert exc.value.status_code == 400


def test_parse_cpu_quantity_negative_float_rejected():
    with pytest.raises(HTTPException):
        parse_cpu_quantity(-0.5)


# ── _validate_tmpfs direct edge cases (for 100% critical-path coverage) ──────

from skiff.validators import _validate_tmpfs


def test_validate_tmpfs_not_dict_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs("not a dict", 10, 512)
    assert exc.value.status_code == 400


def test_validate_tmpfs_too_many_mounts():
    tmpfs = {f"/mnt{i}": "rw" for i in range(11)}
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs(tmpfs, 10, 512)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "validation.tmpfs_too_many"


def test_validate_tmpfs_opts_not_string():
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs({"/tmp": 123}, 10, 512)
    assert exc.value.status_code == 400


def test_validate_tmpfs_opts_too_long():
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs({"/tmp": "rw," * 200}, 10, 512)
    assert exc.value.status_code == 400


def test_validate_tmpfs_size_non_integer():
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs({"/tmp": "rw,size=abc"}, 10, 512)
    assert exc.value.status_code == 400


def test_validate_tmpfs_size_unit_variations():
    # All three size units should be parsed without raising
    _validate_tmpfs({"/tmp": "rw,size=1024k"}, 10, 512)  # 1 MB
    _validate_tmpfs({"/tmp": "rw,size=16m"}, 10, 512)  # 16 MB
    _validate_tmpfs({"/tmp": "rw,size=1g"}, 10, 2048)  # 1024 MB
    _validate_tmpfs({"/tmp": "rw,size=1048576"}, 10, 512)  # bytes (no unit)


def test_validate_tmpfs_non_string_path():
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs({123: "rw"}, 10, 512)
    assert exc.value.status_code == 400
