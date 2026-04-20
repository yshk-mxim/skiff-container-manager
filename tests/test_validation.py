# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for input validation: IDs, image names, registry, compose files."""

import pytest
from fastapi import HTTPException

from skiff.validators import (
    validate_compose_file,
    validate_container_id,
    validate_image_registry,
    validate_project_name,
)

# ── Container ID ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "valid_id",
    [
        "abc1",
        "a1b2c3d4",
        "abc123def456abc1",
        "a" * 64,
    ],
)
def test_valid_container_ids(valid_id):
    assert validate_container_id(valid_id) == valid_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_id",
    [
        # `validate_container_id` now accepts EITHER a hex id OR a container
        # name (Docker's SDK resolves either). "ABC123", "abc-123", "xyz"
        # are valid container NAMES, so they pass. These remaining cases
        # fail both regexes — empty, path traversal, and over-long.
        "",
        "../etc",  # path traversal char ('/' not in name or id regex)
        "a" * 129,  # exceeds 128-char name cap
        "-leading-dash",  # name must start with an alphanumeric
        "has space",  # whitespace in neither regex
    ],
)
def test_invalid_container_ids_raise_400(bad_id):
    with pytest.raises(HTTPException) as exc:
        validate_container_id(bad_id)
    assert exc.value.status_code == 400


# ── Project name ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("valid", ["dev", "my-project", "proj123", "a"])
def test_valid_project_names(valid):
    assert validate_project_name(valid) == valid


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "-starts-with-dash",
        "UPPER",
        "has space",
        "a" * 65,
        "",
    ],
)
def test_invalid_project_names_raise_400(bad):
    with pytest.raises(HTTPException) as exc:
        validate_project_name(bad)
    assert exc.value.status_code == 400


# ── Image registry ────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "allowed_image",
    [
        "docker.io/library/nginx:latest",
        "ghcr.io/owner/repo:tag",
    ],
)
def test_allowed_registry_passes(allowed_image):
    # Should not raise
    validate_image_registry(allowed_image)


@pytest.mark.unit
@pytest.mark.parametrize(
    "blocked_image",
    [
        "evil.example.com/image:tag",
        "us-docker.pkg.dev/my-project/repo/image:latest",
    ],
)
def test_blocked_registry_raises_400(blocked_image):
    with pytest.raises(HTTPException) as exc:
        validate_image_registry(blocked_image)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_image_format_validation():
    with pytest.raises(HTTPException) as exc:
        validate_image_registry("image name with spaces")
    assert exc.value.status_code == 400


# ── Compose file validation ───────────────────────────────────────────────────


def _compose(services_yaml: str) -> bytes:
    return f"services:\n{services_yaml}".encode()


@pytest.mark.unit
def test_valid_compose_passes():
    content = b"""
services:
  web:
    image: docker.io/library/nginx:latest
    ports:
      - "8080:8080"
    volumes:
      - data:/app/data
volumes:
  data:
"""
    result = validate_compose_file(content)
    assert "services" in result


@pytest.mark.unit
def test_compose_too_large_raises_400():
    from skiff import config as _cfg

    content = b"x" * (_cfg.MAX_COMPOSE_SIZE + 1)
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_invalid_yaml_raises_400():
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(b"services: [\ninvalid yaml")
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_not_mapping_raises_400():
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(b"- item1\n- item2")
    assert exc.value.status_code == 400


@pytest.mark.unit
@pytest.mark.parametrize(
    "blocked_key",
    [
        "privileged: true",
        "cap_add:\n      - NET_ADMIN",
        "devices:\n      - /dev/sda",
        "build: .",
        "env_file:\n      - .env",
    ],
)
def test_compose_blocked_service_keys_raise_400(blocked_key):
    content = _compose(f"  web:\n    image: docker.io/library/nginx:latest\n    {blocked_key}\n")
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
@pytest.mark.parametrize("blocked_mode", ["host", "container:other", "service:sidecar"])
def test_compose_blocked_network_mode_raises_400(blocked_mode):
    content = _compose(f"  web:\n    image: docker.io/library/nginx:latest\n    network_mode: {blocked_mode}\n")
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_pid_host_raises_400():
    content = _compose("  web:\n    image: docker.io/library/nginx:latest\n    pid: host\n")
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_ipc_host_raises_400():
    content = _compose("  web:\n    image: docker.io/library/nginx:latest\n    ipc: host\n")
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
@pytest.mark.parametrize("bad_vol", ["/etc/passwd:/data", "~/data:/data", "../data:/data", "$HOME:/data"])
def test_compose_host_path_volume_raises_400(bad_vol):
    content = _compose(f"  web:\n    image: docker.io/library/nginx:latest\n    volumes:\n      - {bad_vol}\n")
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_named_volume_is_allowed():
    content = _compose("  web:\n    image: docker.io/library/nginx:latest\n    volumes:\n      - mydata:/app/data\n")
    result = validate_compose_file(content)
    assert result is not None


@pytest.mark.unit
def test_compose_blocked_top_level_secrets():
    content = (
        b"secrets:\n  mysecret:\n    file: ./secret.txt\nservices:\n  web:\n    image: docker.io/library/nginx:latest\n"
    )
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_compose_image_from_unapproved_registry_raises_400():
    content = _compose("  web:\n    image: evil.example.com/img:latest\n")
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_redact_env_masks_sensitive_keys():
    from skiff.validators import _redact_env

    env = [
        "DATABASE_URL=postgres://user:pass@host/db",
        "API_KEY=secret123",
        "PASSWORD=hunter2",
        "PORT=8080",
        "MY_SECRET=abc",
        "AWS_SECRET_ACCESS_KEY=AKIA...",
        "LOG_LEVEL=info",
    ]
    result = _redact_env(env)
    # Sensitive values must be redacted
    assert "DATABASE_URL=[REDACTED]" not in result  # DATABASE_URL has no sensitive keyword
    assert any(e == "API_KEY=[REDACTED]" for e in result)
    assert any(e == "PASSWORD=[REDACTED]" for e in result)
    assert any(e == "MY_SECRET=[REDACTED]" for e in result)
    assert any(e == "AWS_SECRET_ACCESS_KEY=[REDACTED]" for e in result)
    # Non-sensitive values must be preserved
    assert "PORT=8080" in result
    assert "LOG_LEVEL=info" in result
