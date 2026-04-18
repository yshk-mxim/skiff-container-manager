# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Smoke tests for tests.api helper (R13).

Verifies the helper preserves semantics vs. raw client calls that
existing tests use, so the mechanical migration of other test files
is safe.
"""

from __future__ import annotations

from tests import api
from tests.conftest import AUTH_CSRF, AUTH_HEADER


def test_get_attaches_auth_header(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = api.get(client, "/containers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_path_without_api_prefix(client, mock_docker):
    """Path may be '/containers' or '/api/containers' — both work."""
    mock_docker.containers.list.return_value = []
    r1 = api.get(client, "/containers")
    r2 = api.get(client, "/api/containers")
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_post_attaches_csrf_header(client, mock_docker):
    """POST with CSRF header present; CSRF-less POST would 403."""
    mock_docker.containers.list.return_value = []
    # Send an empty body and a bad registry to trigger a fast 400 without
    # touching the Docker SDK mock. CSRF+AUTH must pass (no 401/403).
    resp = api.post(
        client,
        "/containers/run",
        json={},
        image="evil.example.com/img:latest",
    )
    assert resp.status_code == 400  # registry rejected; NOT 401/403


def test_post_without_csrf_would_403(client):
    """Confirm the helper IS wiring CSRF — naked client.post(...) 403s.

    Uses /api/system/prune which has no required body / query so the
    CSRF check fires before any Pydantic validation would return 422.
    """
    resp = client.post(
        "/api/system/prune",
        headers={"Authorization": AUTH_HEADER["Authorization"]},
    )
    assert resp.status_code == 403  # missing X-Requested-With


def test_delete_attaches_csrf_header(client, mock_docker):
    """DELETE without CSRF would 403 — helper wraps AUTH_CSRF so we get
    a normal response path."""
    import docker.errors

    mock_docker.containers.get.side_effect = docker.errors.NotFound("gone")
    resp = api.delete(client, "/containers/abc123def")
    # Returns 404 (not found) or 200 — either proves CSRF + AUTH passed.
    assert resp.status_code != 403


def test_ok_returns_json(client, mock_docker):
    mock_docker.containers.list.return_value = []
    data = api.ok(api.get(client, "/containers"))
    assert data == []


def test_error_code_reads_structured_detail(client):
    """For a 422 from FastAPI (missing required query param), SKIFF wraps
    Pydantic's raw output into the `{code, message}` envelope so every
    4xx/5xx speaks the same shape (see `docs/errors.md`)."""
    resp = client.post("/api/images/pull", headers=AUTH_CSRF)
    code = api.error_code(resp)
    assert code == "validation.bad_input"
