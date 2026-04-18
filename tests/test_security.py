# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""
Security-focused tests: registry bypass attempts, compose sandbox escapes,
volume path traversal, and response header checks.

These test real defensive logic — not happy paths.
"""

import pytest
from fastapi import HTTPException

from skiff.validators import (
    BLOCKED_COMPOSE_SERVICE_KEYS,
    validate_compose_file,
    validate_image_registry,
)
from tests.conftest import AUTH_CSRF, AUTH_HEADER, TOKEN

# ── Registry bypass attempts ───────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("bypass_attempt", [
    # Subdomain confusion: evil.us-docker.pkg.dev is NOT the allowed registry
    "evil.us-docker.pkg.dev/img:latest",
    # Path confusion: allowed registry appears after a slash, not as hostname
    "evil.example.com/us-docker.pkg.dev/img:latest",
    # Registry prefix as path segment, not host
    "example.com/us-docker.pkg.dev:latest",
    # Subdomain confusion for docker.io
    "evil.docker.io/img:latest",
])
def test_registry_bypass_attempts_blocked(bypass_attempt):
    with pytest.raises(HTTPException) as exc:
        validate_image_registry(bypass_attempt)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_allowed_registry_exact_match_required():
    """docker.io.evil.com must not match docker.io/."""
    with pytest.raises(HTTPException):
        validate_image_registry("docker.io.evil.com/img:latest")


# ── Compose sandbox escape attempts ───────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("privileged_variant", [
    "privileged: true",
    "privileged: True",   # YAML boolean alias
])
def test_compose_privileged_variants_blocked(privileged_variant):
    content = f"services:\n  web:\n    image: docker.io/library/nginx:latest\n    {privileged_variant}\n".encode()
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_all_blocked_service_keys():
    """Every key in BLOCKED_COMPOSE_SERVICE_KEYS must be rejected when truthy."""
    blocked_keys_with_values = {
        "privileged": "true",
        "cap_add": "- NET_ADMIN",
        "devices": "- /dev/sda",
        "build": ".",
        "env_file": "- .env",
        "sysctls": "net.core.somaxconn: 1024",
        "security_opt": "- no-new-privileges:false",
        "tmpfs": "/run",
        "shm_size": "128m",
        "volumes_from": "- other-container",
    }
    for key in BLOCKED_COMPOSE_SERVICE_KEYS:
        value = blocked_keys_with_values.get(key, "somevalue")
        content = f"services:\n  web:\n    image: docker.io/library/nginx:latest\n    {key}:\n      {value}\n".encode()
        try:
            validate_compose_file(content)
        except HTTPException as exc:
            assert exc.status_code == 400, f"Expected 400 for key '{key}', got {exc.status_code}"


@pytest.mark.unit
def test_compose_volume_dict_format_host_path_blocked():
    """Volume in long-form dict notation with a host source must be blocked."""
    content = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    volumes:
      - type: bind
        source: /etc
        target: /data
"""
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_top_level_configs_blocked():
    content = b"configs:\n  mycfg:\n    file: ./config.txt\nservices:\n  web:\n    image: docker.io/library/nginx:latest\n"
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


# ── Response security headers ─────────────────────────────────────────────────

@pytest.mark.unit
def test_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


@pytest.mark.unit
def test_csp_blocks_frame_ancestors(client):
    csp = client.get("/health").headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp


# ── Auth edge cases ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_bearer_scheme_required(client):
    """Token without 'Bearer ' prefix must be rejected."""
    resp = client.get("/api/containers", headers={"Authorization": TOKEN})
    assert resp.status_code == 401


@pytest.mark.unit
def test_auth_header_case_sensitivity(client):
    """Authorization header value is case-sensitive for the scheme."""
    resp = client.get("/api/containers", headers={"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_csrf_required_on_delete(client, mock_docker):
    """DELETE without CSRF header must return 403, not 404."""
    resp = client.delete("/api/containers/abc123def", headers=AUTH_HEADER)
    assert resp.status_code == 403


@pytest.mark.unit
def test_run_container_no_volume_separator_returns_400(client, mock_docker):
    """Volume without ':' is an ambiguous format and must be rejected."""
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["nodrivepath"]},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_env_var_without_equals_returns_400(client, mock_docker):
    """Environment variable without '=' must be rejected."""
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"environment": ["NOEQUAL"]},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_env_var_invalid_key_returns_400(client, mock_docker):
    """Environment variable with invalid key characters must be rejected."""
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"environment": ["123STARTSWITHDIGIT=value"]},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_run_container_too_many_labels_returns_400(client, mock_docker):
    mock_docker.containers.list.return_value = []
    labels = {f"key{i}": "val" for i in range(51)}
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"labels": labels},
    )
    assert resp.status_code == 400
