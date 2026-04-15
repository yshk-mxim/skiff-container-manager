# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Unit tests for the setup wizard endpoints (/api/setup-state, /api/setup, /api/setup/tunnel)."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import skiff.app as app_module
from skiff.app import app

CSRF = {"X-Requested-With": "ContainerManager"}


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    """Reset _cfg to unconfigured state before each test."""
    monkeypatch.setattr(app_module._cfg, "api_token", "")
    monkeypatch.setattr(app_module._cfg, "from_env", False)
    monkeypatch.setattr(app_module._cfg, "docker_host", "unix:///var/run/docker.sock")
    monkeypatch.setattr(app_module._cfg, "allowed_registries", [])
    monkeypatch.setattr(app_module, "_tunnel_ctl_sock", "")
    monkeypatch.setattr(app_module, "_tunnel_ssh_target", "")
    monkeypatch.setattr(app_module, "_tunnel_socket_path", "")
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
    assert "docker.io" in app_module._cfg.allowed_registries


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


def test_tunnel_invalid_socket_path(client):
    r = client.post("/api/setup/tunnel", json={
        "ssh_target": "user@host",
        "socket_path": "/etc/evil",
    }, headers=CSRF)
    assert r.status_code == 400
    assert "/tmp/" in r.json()["detail"]


def test_tunnel_disabled_when_from_env(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "from_env", True)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 403


def test_tunnel_disabled_when_configured(client, monkeypatch):
    monkeypatch.setattr(app_module._cfg, "api_token", "already-set-token-32chars")
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 403


def test_tunnel_start_success(client, monkeypatch):
    # Mock _start_tunnel to create the socket file and update globals
    def fake_start(ssh_target, socket_path):
        open(socket_path, "w").close()
        app_module._tunnel_ctl_sock = "/tmp/fake-ctl.sock"
        app_module._tunnel_ssh_target = ssh_target
        app_module._tunnel_socket_path = socket_path
    monkeypatch.setattr(app_module, "_start_tunnel", fake_start)
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        sock = os.path.join(td, "docker.sock")
        r = client.post("/api/setup/tunnel", json={
            "ssh_target": "user@myhost",
            "socket_path": sock,
        }, headers=CSRF)
    assert r.status_code == 200
    assert "socket_path" in r.json()
    assert "docker_host" in r.json()


def test_tunnel_start_failure(client, monkeypatch):
    def fake_start(ssh_target, socket_path):
        raise ValueError("SSH failed: Connection refused")
    monkeypatch.setattr(app_module, "_start_tunnel", fake_start)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@badhost"}, headers=CSRF)
    assert r.status_code == 502
    assert "SSH" in r.json()["detail"]


def test_tunnel_stop(client, monkeypatch):
    monkeypatch.setattr(app_module, "_tunnel_socket_path", "/tmp/fake.sock")
    stop_called = []
    monkeypatch.setattr(app_module, "_stop_tunnel", lambda: stop_called.append(True))
    r = client.delete("/api/setup/tunnel", headers=CSRF)
    assert r.status_code == 200
    assert stop_called


def test_setup_state_reflects_tunnel(client, monkeypatch, tmp_path):
    sock = tmp_path / "docker.sock"
    sock.touch()
    monkeypatch.setattr(app_module, "_tunnel_socket_path", str(sock))
    r = client.get("/api/setup-state")
    assert r.json()["tunnel_active"] is True
