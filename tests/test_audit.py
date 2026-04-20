# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Unit tests for audit log event classification and session age enforcement."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import skiff.app as app_module  # noqa: F401
import skiff.auth as auth_module
import skiff.config as config_module
from skiff.app import app
from skiff.logging_setup import _classify_event

AUTH = {"Authorization": "Bearer testtoken1234567890"}
CSRF = {"X-Requested-With": "ContainerManager"}
TOKEN = "testtoken1234567890"


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(config_module._cfg, "api_token", TOKEN)
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    auth_module._invalidate_session_cache()
    yield
    auth_module._invalidate_session_cache()


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── _classify_event ────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "method,path,status,expected_type",
    [
        # After R26 the classification layer uses the decorator-declared
        # `audit=<name>` directly — no more parallel "historical" names
        # drifting from what handlers actually emit. The names below match
        # @secure_route.mutate(audit=...) in each router module.
        ("POST", "/api/containers/abc123/start", 200, "container.started"),
        ("POST", "/api/containers/run", 200, "container.run"),
        ("DELETE", "/api/containers/abc123", 200, "container.removed"),
        ("POST", "/api/images/pull", 200, "image.pulled"),
        ("POST", "/api/compose/up", 200, "compose.up"),
        ("POST", "/api/compose/down", 200, "compose.down"),
        ("GET", "/api/system/audit-log", 200, "audit.log_read"),
        # /api/setup has no audit= annotation (intentional — the setup
        # endpoint's own log line is the authoritative audit event), so
        # classification falls through to the /api/ catch-all.
        ("POST", "/api/setup", 200, "api.request"),
        ("GET", "/api/volumes", 401, "auth.denied"),
        ("GET", "/api/containers", 429, "rate_limit.exceeded"),
        ("GET", "/api/unknown/path", 200, "api.request"),
    ],
)
def test_classify_event(method, path, status, expected_type):
    event_type, _, _ = _classify_event(method, path, status)
    assert event_type == expected_type


@pytest.mark.unit
def test_classify_event_extracts_resource_identity():
    _, rtype, rid = _classify_event("DELETE", "/api/containers/abc123def456", 200)
    assert rtype == "container"
    assert rid == "abc123def456"


@pytest.mark.unit
def test_classify_event_image_resource():
    _, rtype, rid = _classify_event("DELETE", "/api/images/sha256abc", 200)
    assert rtype == "image"
    assert rid == "sha256abc"


# ── Server-side session age ────────────────────────────────────────────────


@pytest.mark.unit
def test_session_accepted_within_timeout(client):
    r = client.get("/api/containers", headers=AUTH)
    assert r.status_code != 401


@pytest.mark.unit
def test_session_rejected_after_timeout(client, monkeypatch):
    # First request establishes the session
    client.get("/api/containers", headers=AUTH)

    # Fast-forward past the absolute timeout
    monkeypatch.setattr(config_module, "SESSION_ABS_TIMEOUT", -1)

    r = client.get("/api/containers", headers=AUTH)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "auth.session_expired"


@pytest.mark.unit
def test_session_cache_cleared_on_reconfigure(client, monkeypatch):
    # Seed a session
    client.get("/api/containers", headers=AUTH)
    assert TOKEN[:16] in str(auth_module._session_first_seen) or len(auth_module._session_first_seen) > 0

    # Clearing the cache (token rotation simulation)
    auth_module._invalidate_session_cache()
    assert len(auth_module._session_first_seen) == 0


# ── X-Forwarded-User logged ────────────────────────────────────────────────


@pytest.mark.unit
def test_forwarded_user_accepted_without_error(client):
    """X-Forwarded-User header must not cause errors — logged by middleware."""
    r = client.get("/api/containers", headers={**AUTH, "X-Forwarded-User": "alice@example.com"})
    assert r.status_code != 500


# ── Audit retention env vars ───────────────────────────────────────────────


@pytest.mark.unit
def test_audit_max_bytes_default():
    assert config_module.AUDIT_MAX_BYTES == 10 * 1024 * 1024


@pytest.mark.unit
def test_audit_backup_count_default():
    assert config_module.AUDIT_BACKUP_COUNT == 5
