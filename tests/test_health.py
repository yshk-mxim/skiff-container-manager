"""Tests for health and readiness endpoints."""

from unittest.mock import patch

import docker.errors
import pytest


@pytest.mark.unit
def test_health_always_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data


@pytest.mark.unit
def test_health_no_auth_required(client):
    resp = client.get("/health")
    assert resp.status_code == 200


@pytest.mark.unit
def test_ready_ok(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert "docker_version" in data


@pytest.mark.unit
def test_ready_503_when_docker_unreachable(client):
    with patch("skiff.routers.system.get_client", side_effect=docker.errors.DockerException("no connection")):
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


@pytest.mark.unit
def test_auth_required_with_token(client):
    resp = client.get("/api/auth-required")
    assert resp.status_code == 200
    data = resp.json()
    assert data["required"] is True


@pytest.mark.unit
def test_auth_required_without_token(noauth_client):
    resp = noauth_client.get("/api/auth-required")
    assert resp.status_code == 200
    assert resp.json()["required"] is False
