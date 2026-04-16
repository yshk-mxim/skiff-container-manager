# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Unit tests for the setup wizard endpoints (/api/setup-state, /api/setup, /api/setup/tunnel)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import skiff.app as app_module
import skiff.docker_client as docker_client_module
import skiff.routers.system as system_module
from skiff.app import app

CSRF = {"X-Requested-With": "ContainerManager"}


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    """Reset _cfg to unconfigured state before each test."""
    monkeypatch.setattr(app_module._cfg, "api_token", "")
    monkeypatch.setattr(app_module._cfg, "from_env", False)
    monkeypatch.setattr(app_module._cfg, "docker_host", "unix:///var/run/docker.sock")
    monkeypatch.setattr(app_module._cfg, "allowed_registries", [])
    monkeypatch.setattr(docker_client_module, "_tunnel_ctl_sock", "")
    monkeypatch.setattr(docker_client_module, "_tunnel_ssh_target", "")
    monkeypatch.setattr(docker_client_module, "_tunnel_socket_path", "")
    yield


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── /api/setup-state ──────────────────────────────────────────────────────

def test_setup_state_unconfigured(client):
    r = client.get("/api/setup-state")
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is False
    assert data["from_env"] is False
    assert "tunnel_active" in data
    assert "tunnel_socket" in data


def test_setup_state_configured(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "api_token", "already-set-token-32chars")
    r = client.get("/api/setup-state")
    assert r.status_code == 200
    assert r.json()["configured"] is True


def test_setup_state_from_env(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "from_env", True)
    monkeypatch.setattr(app_module._cfg, "api_token", "env-token")
    r = client.get("/api/setup-state")
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is True
    assert data["from_env"] is True


# ── /api/setup ────────────────────────────────────────────────────────────

def test_setup_success(client):
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "a" * 16,
        "allowed_registries": "docker.io,ghcr.io",
    }, headers=CSRF)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert app_module._cfg.api_token == "a" * 16
    assert app_module._cfg.docker_host == "unix:///var/run/docker.sock"
    assert any(r == "docker.io" for r in app_module._cfg.allowed_registries)


def test_setup_token_too_short(client):
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "short",
        "allowed_registries": "",
    }, headers=CSRF)
    assert r.status_code == 400
    assert "16" in r.json()["detail"]


def test_setup_missing_docker_host(client):
    r = client.post("/api/setup", json={
        "docker_host": "",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 400


def test_setup_disabled_when_from_env(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "from_env", True)
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 403


def test_setup_disabled_when_already_configured(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "api_token", "already-configured-token")
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "new-token-16chars",
    }, headers=CSRF)
    assert r.status_code == 403


def test_setup_empty_registries_allowed(client):
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "a" * 16,
        "allowed_registries": "",
    }, headers=CSRF)
    assert r.status_code == 200
    assert app_module._cfg.allowed_registries == []


# ── /api/setup/tunnel ─────────────────────────────────────────────────────

def test_tunnel_invalid_target(client):
    r = client.post("/api/setup/tunnel", json={"ssh_target": "not-valid"}, headers=CSRF)
    assert r.status_code == 400
    assert "user@host" in r.json()["detail"]


def test_tunnel_extra_fields_ignored(client, monkeypatch):
    """Unknown fields in the JSON body are silently ignored — server always uses its
    own constant socket path, so socket_path in the body has no effect."""
    monkeypatch.setattr(system_module, "_start_tunnel", lambda *_: None)
    r = client.post("/api/setup/tunnel", json={
        "ssh_target": "user@host",
        "socket_path": "/etc/evil",  # ignored — server uses TUNNEL_DEFAULT_SOCKET
    }, headers=CSRF)
    # _start_tunnel returns None (no error) → endpoint returns 200, not 400
    assert r.status_code == 200


def test_tunnel_disabled_when_from_env(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "from_env", True)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 403


def test_tunnel_disabled_when_configured(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "api_token", "already-set-token-32chars")
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 403


def test_tunnel_start_success(client, monkeypatch):
    # Mock _start_tunnel — it now takes only ssh_target; socket path is internal constant.
    started = []
    def fake_start(ssh_target):
        started.append(ssh_target)
    monkeypatch.setattr(system_module, "_start_tunnel", fake_start)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@myhost"}, headers=CSRF)
    assert r.status_code == 200
    assert "socket_path" in r.json()
    assert "docker_host" in r.json()
    assert started[0] == "user@myhost"


def test_tunnel_start_failure(client, monkeypatch):
    def fake_start(ssh_target):
        raise ValueError("SSH failed: Connection refused")
    monkeypatch.setattr(system_module, "_start_tunnel", fake_start)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@badhost"}, headers=CSRF)
    assert r.status_code == 502
    assert "SSH" in r.json()["detail"]


def test_tunnel_stop(client, monkeypatch):
    monkeypatch.setattr(docker_client_module, "_tunnel_socket_path", "/tmp/fake.sock")
    stop_called = []
    monkeypatch.setattr(system_module, "_stop_tunnel", lambda: stop_called.append(True))
    r = client.delete("/api/setup/tunnel", headers=CSRF)
    assert r.status_code == 200
    assert stop_called


def test_setup_state_reflects_tunnel(client, monkeypatch):
    """tunnel_active is True when the stored socket path exists on disk.

    The socket must live directly under the resolved /tmp/ root — setup_state
    reconstructs the path from the basename alone so that no user-controlled
    data reaches os.path.exists (CodeQL path-injection defence).
    """
    from pathlib import Path as _Path
    # Create the socket directly under the resolved /tmp so that setup_state's
    # basename-only reconstruction points to the same file on both Linux and macOS
    # (where /tmp resolves to /private/tmp).
    tmp_root = _Path("/tmp").resolve()
    sock = tmp_root / f"skiff-test-state-{os.getpid()}.sock"
    sock.touch()
    try:
        monkeypatch.setattr(docker_client_module, "_tunnel_socket_path", str(sock))
        r = client.get("/api/setup-state")
        assert r.json()["tunnel_active"] is True
    finally:
        sock.unlink(missing_ok=True)


# ── Security: CSRF required on setup endpoints ────────────────────────────

def test_setup_requires_csrf(client):
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "a" * 16,
    })  # No X-Requested-With header
    assert r.status_code == 403


def test_tunnel_start_requires_csrf(client):
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"})
    assert r.status_code == 403


def test_tunnel_stop_requires_csrf(client):
    r = client.delete("/api/setup/tunnel")
    assert r.status_code == 403


# ── Security: docker_host validation ─────────────────────────────────────

def test_setup_rejects_hostname_tcp(client):
    r = client.post("/api/setup", json={
        "docker_host": "tcp://evil.example.com:2375",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 400
    assert "IP address" in r.json()["detail"]


def test_setup_accepts_ip_tcp(client):
    r = client.post("/api/setup", json={
        "docker_host": "tcp://127.0.0.1:2375",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 200


def test_setup_rejects_http_scheme(client):
    r = client.post("/api/setup", json={
        "docker_host": "http://127.0.0.1:8080",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 400
    assert "scheme" in r.json()["detail"]
