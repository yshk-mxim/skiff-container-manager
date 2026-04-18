# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Unit tests for the setup wizard endpoints (/api/setup-state, /api/setup, /api/setup/tunnel)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import skiff.config as config_module
import skiff.docker_client as docker_client_module
from skiff.app import app

CSRF = {"X-Requested-With": "ContainerManager"}


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    """Reset _cfg to unconfigured state before each test."""
    monkeypatch.setattr(config_module._cfg, "api_token", "")
    monkeypatch.setattr(config_module._cfg, "from_env", False)
    monkeypatch.setattr(config_module._cfg, "docker_host", "unix:///var/run/docker.sock")
    monkeypatch.setattr(config_module._cfg, "allowed_registries", [])
    monkeypatch.setattr(docker_client_module, "_tunnel_ctl_sock", "")
    monkeypatch.setattr(docker_client_module, "_tunnel_ssh_target", "")
    monkeypatch.setattr(docker_client_module, "_tunnel_socket_path", "")
    yield


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── /api/setup-state ──────────────────────────────────────────────────────
# FastAPI's TestClient sets `request.client.host == "testclient"`. The
# setup-state handler restricts the tunnel-path disclosure to loopback IPs
# so network callers can't enumerate the socket path. These unit tests
# treat the TestClient sentinel as loopback via a local patch so they
# exercise the loopback branch without leaking a test-specific literal
# into production code.

import skiff.routers.setup as _setup_mod


def _as_loopback(monkeypatch):
    """Include starlette TestClient's `testclient` sentinel as loopback."""
    monkeypatch.setattr(
        _setup_mod, "_LOOPBACK_HOSTS",
        frozenset({*_setup_mod._LOOPBACK_HOSTS, "testclient"}),
    )


def test_setup_state_unconfigured(client, monkeypatch):
    _as_loopback(monkeypatch)
    r = client.get("/api/setup-state")
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is False
    assert data["from_env"] is False
    assert "tunnel_active" in data
    assert "tunnel_socket" in data


def test_setup_state_configured(client, monkeypatch):
    monkeypatch.setattr(config_module._cfg, "api_token", "already-set-token-32chars")
    r = client.get("/api/setup-state")
    assert r.status_code == 200
    assert r.json()["configured"] is True


def test_setup_state_from_env(client, monkeypatch):
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    monkeypatch.setattr(config_module._cfg, "api_token", "env-token")
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
    assert config_module._cfg.api_token == "a" * 16
    assert config_module._cfg.docker_host == "unix:///var/run/docker.sock"
    assert any(r == "docker.io" for r in config_module._cfg.allowed_registries)


def test_setup_token_too_short(client):
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "short",
        "allowed_registries": "",
    }, headers=CSRF)
    assert r.status_code == 400
    assert "16" in r.json()["detail"]["message"]


def test_setup_missing_docker_host(client):
    r = client.post("/api/setup", json={
        "docker_host": "",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 400


def test_setup_disabled_when_from_env(client, monkeypatch):
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    r = client.post("/api/setup", json={
        "docker_host": "unix:///var/run/docker.sock",
        "api_token": "a" * 16,
    }, headers=CSRF)
    assert r.status_code == 403


def test_setup_disabled_when_already_configured(client, monkeypatch):
    monkeypatch.setattr(config_module._cfg, "api_token", "already-configured-token")
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
    assert config_module._cfg.allowed_registries == []


# ── /api/setup/tunnel ─────────────────────────────────────────────────────

def test_tunnel_invalid_target(client):
    r = client.post("/api/setup/tunnel", json={"ssh_target": "not-valid"}, headers=CSRF)
    assert r.status_code == 400
    assert "user@host" in r.json()["detail"]["message"]


def test_tunnel_extra_fields_ignored(client, monkeypatch):
    """Unknown fields in the JSON body are silently ignored — server always uses its
    own constant socket path, so socket_path in the body has no effect."""
    monkeypatch.setattr(docker_client_module, "_start_tunnel", lambda *_: None)
    r = client.post("/api/setup/tunnel", json={
        "ssh_target": "user@host",
        "socket_path": "/etc/evil",  # ignored — server uses TUNNEL_DEFAULT_SOCKET
    }, headers=CSRF)
    # _start_tunnel returns None (no error) → endpoint returns 200, not 400
    assert r.status_code == 200


def test_tunnel_disabled_when_from_env(client, monkeypatch):
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 403


def test_tunnel_disabled_when_configured(client, monkeypatch):
    monkeypatch.setattr(config_module._cfg, "api_token", "already-set-token-32chars")
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 403


def test_tunnel_start_success(client, monkeypatch):
    # Mock _start_tunnel — it now takes only ssh_target; socket path is internal constant.
    started = []
    def fake_start(ssh_target):
        started.append(ssh_target)
    monkeypatch.setattr(docker_client_module, "_start_tunnel", fake_start)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@myhost"}, headers=CSRF)
    assert r.status_code == 200
    assert "socket_path" in r.json()
    assert "docker_host" in r.json()
    assert started[0] == "user@myhost"


def test_tunnel_start_failure(client, monkeypatch):
    def fake_start(ssh_target):
        raise docker_client_module.TunnelError("SSH failed: Connection refused")
    monkeypatch.setattr(docker_client_module, "_start_tunnel", fake_start)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@badhost"}, headers=CSRF)
    assert r.status_code == 502
    # Structured detail after R4: {code, message, ...extras}. The
    # catalogue-level code is system.tunnel_failed; the TunnelError's
    # own classification (or "other" for plain ValueError) goes under
    # `tunnel_code` so clients can switch on both axes.
    detail = r.json()["detail"]
    assert "SSH" in detail["message"]
    assert detail["code"] == "system.tunnel_failed"
    assert detail["tunnel_code"] == "other"


def test_tunnel_start_classified_auth_failure(client, monkeypatch):
    """TunnelError with code=auth_failed surfaces to the client for UI guidance."""
    from skiff.docker_client import TunnelError
    def fake_start(ssh_target):
        raise TunnelError(
            "SSH failed: Permission denied (publickey)",
            "auth_failed",
            "Your SSH key isn't installed on the remote host.",
        )
    monkeypatch.setattr(docker_client_module, "_start_tunnel", fake_start)
    r = client.post("/api/setup/tunnel", json={"ssh_target": "user@host"}, headers=CSRF)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "system.tunnel_failed"
    assert detail["tunnel_code"] == "auth_failed"
    assert "key isn't installed" in detail["help"]


def test_tunnel_stop(client, monkeypatch):
    monkeypatch.setattr(docker_client_module, "_tunnel_socket_path", "/tmp/fake.sock")
    stop_called = []
    monkeypatch.setattr(docker_client_module, "stop_tunnel", lambda: stop_called.append(True))
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
    _as_loopback(monkeypatch)
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
    assert "IP address" in r.json()["detail"]["message"]


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
    assert "scheme" in r.json()["detail"]["message"]


# ── Post-setup tunnel lifecycle: /api/tunnel/status + /api/tunnel/reconnect ──

_AUTH_CSRF = {**CSRF, "Authorization": "Bearer configured-token-32chars-long"}


def _configure(monkeypatch):
    monkeypatch.setattr(config_module._cfg, "api_token", "configured-token-32chars-long")


def test_tunnel_status_requires_auth(client, monkeypatch):
    _configure(monkeypatch)
    r = client.get("/api/tunnel/status")  # no Bearer
    assert r.status_code == 401


def test_tunnel_status_unmanaged_when_no_target(client, monkeypatch):
    """No wizard-managed target AND no reachable docker host → managed=False,
    active=False. Point DOCKER_HOST at a socket path that doesn't exist so
    the unix-socket fallback can't accidentally flip `active` to True on a
    CI machine that happens to have /var/run/docker.sock."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        config_module._cfg, "docker_host",
        "unix:///tmp/skiff-definitely-does-not-exist.sock",
    )
    r = client.get("/api/tunnel/status", headers=_AUTH_CSRF)
    assert r.status_code == 200
    data = r.json()
    assert data["managed"] is False
    assert data["active"] is False


def test_tunnel_status_managed_when_target_stored(client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(docker_client_module, "_tunnel_ssh_target", "user@host")
    r = client.get("/api/tunnel/status", headers=_AUTH_CSRF)
    data = r.json()
    assert data["managed"] is True
    # ssh_target is NEVER in the response (zero-trust)
    assert "target" not in data and "ssh_target" not in data
    assert "user@host" not in r.text


def test_tunnel_reconnect_requires_auth(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post("/api/tunnel/reconnect", headers=CSRF)
    assert r.status_code == 401


def test_tunnel_reconnect_requires_csrf(client, monkeypatch):
    _configure(monkeypatch)
    # Auth but no CSRF header
    r = client.post("/api/tunnel/reconnect",
                    headers={"Authorization": _AUTH_CSRF["Authorization"]})
    assert r.status_code == 403


def test_tunnel_reconnect_no_stored_target_and_no_docker_host(client, monkeypatch):
    """Reconnect with no wizard target AND no unix-socket DOCKER_HOST
    returns 404 `tunnel.not_configured` — SKIFF has nothing to reconnect."""
    _configure(monkeypatch)
    # TCP docker_host — not a manual-tunnel path, so reconnect has no target.
    monkeypatch.setattr(config_module._cfg, "docker_host", "tcp://10.0.0.1:2376")
    r = client.post("/api/tunnel/reconnect", headers=_AUTH_CSRF)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "tunnel.not_configured"


def test_tunnel_reconnect_manual_tunnel_socket_missing(client, monkeypatch):
    """Reconnect with no wizard target BUT a unix:// DOCKER_HOST whose
    socket doesn't exist returns 503 `tunnel.manual_reconnect_required` —
    SKIFF can't re-open a tunnel it didn't open, but gives the operator
    the exact socket path to revive with `ssh -fNL`."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        config_module._cfg, "docker_host",
        "unix:///tmp/skiff-definitely-does-not-exist.sock",
    )
    r = client.post("/api/tunnel/reconnect", headers=_AUTH_CSRF)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "tunnel.manual_reconnect_required"
    assert "docker_host" in detail or "socket_path" in detail


def test_tunnel_reconnect_reuses_stored_target(client, monkeypatch):
    """Reconnect calls _start_tunnel with the server-stored target — client cannot override."""
    _configure(monkeypatch)
    monkeypatch.setattr(docker_client_module, "_tunnel_ssh_target", "user@host")
    called_with = []
    monkeypatch.setattr(docker_client_module, "_start_tunnel", called_with.append)
    monkeypatch.setattr(docker_client_module, "invalidate_client", lambda: None)
    # Attempt to poison with a client-supplied target — it must be ignored.
    r = client.post("/api/tunnel/reconnect",
                    json={"ssh_target": "attacker@evil"},
                    headers=_AUTH_CSRF)
    assert r.status_code == 200
    assert called_with == ["user@host"]


def test_tunnel_reconnect_surfaces_classified_error(client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(docker_client_module, "_tunnel_ssh_target", "user@host")
    from skiff.docker_client import TunnelError
    def fake_start(t):
        raise TunnelError("SSH failed: Permission denied (publickey)",
                          "auth_failed", "Install your key.")
    monkeypatch.setattr(docker_client_module, "_start_tunnel", fake_start)
    r = client.post("/api/tunnel/reconnect", headers=_AUTH_CSRF)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "system.tunnel_failed"
    assert detail["tunnel_code"] == "auth_failed"


# ── Phase 4: token rotation + config reset ────────────────────────────────


def test_rotate_token_requires_auth(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post("/api/auth/rotate-token", json={"new_token": "a" * 16}, headers=CSRF)
    assert r.status_code == 401


def test_rotate_token_requires_csrf(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post(
        "/api/auth/rotate-token",
        json={"new_token": "a" * 16},
        headers={"Authorization": _AUTH_CSRF["Authorization"]},
    )
    assert r.status_code == 403


def test_rotate_token_happy_path(client, monkeypatch):
    _configure(monkeypatch)
    assert config_module._cfg.api_token == "configured-token-32chars-long"
    r = client.post(
        "/api/auth/rotate-token",
        json={"new_token": "new-rotated-token-48chars-more-than-min"},
        headers=_AUTH_CSRF,
    )
    assert r.status_code == 200
    # In-memory token really changed
    assert config_module._cfg.api_token == "new-rotated-token-48chars-more-than-min"


def test_rotate_token_minimum_length(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post(
        "/api/auth/rotate-token",
        json={"new_token": "short"},
        headers=_AUTH_CSRF,
    )
    assert r.status_code == 400
    assert "characters" in r.json()["detail"]["message"]


def test_rotate_token_rejects_identical_token(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post(
        "/api/auth/rotate-token",
        json={"new_token": "configured-token-32chars-long"},
        headers=_AUTH_CSRF,
    )
    assert r.status_code == 400
    assert "identical" in r.json()["detail"]["message"]


def test_rotate_token_blocked_when_from_env(client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    r = client.post(
        "/api/auth/rotate-token",
        json={"new_token": "y" * 32},
        headers=_AUTH_CSRF,
    )
    assert r.status_code == 403
    assert "environment" in r.json()["detail"]["message"]
    # Token unchanged
    assert config_module._cfg.api_token == "configured-token-32chars-long"


def test_rotate_token_does_not_log_token_value(client, monkeypatch):
    """CRITICAL: audit entry must NOT contain the token value, only the suffix."""
    _configure(monkeypatch)
    captured: list[dict] = []
    def _capture(event, **kwargs):
        captured.append({"event": event, **kwargs})
    import skiff.routers.setup as setup_module
    monkeypatch.setattr(setup_module.log, "info", _capture)
    secret = "verysensitivetoken-42chars-needs-guard"
    r = client.post(
        "/api/auth/rotate-token",
        json={"new_token": secret},
        headers=_AUTH_CSRF,
    )
    assert r.status_code == 200
    events = [e for e in captured if e["event"] == "auth.token_rotated"]
    assert len(events) == 1
    # No full token should appear anywhere in the kwargs (only suffixes of <=8 chars)
    for val in events[0].values():
        assert secret not in str(val), f"Full token leaked to log field: {val!r}"


def test_reset_config_requires_auth(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post("/api/auth/reset-config", headers=CSRF)
    assert r.status_code == 401


def test_reset_config_requires_csrf(client, monkeypatch):
    _configure(monkeypatch)
    r = client.post(
        "/api/auth/reset-config",
        headers={"Authorization": _AUTH_CSRF["Authorization"]},
    )
    assert r.status_code == 403


def test_reset_config_clears_in_memory_state(client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(config_module._cfg, "docker_host", "unix:///var/run/docker.sock")
    monkeypatch.setattr(config_module._cfg, "allowed_registries", ["docker.io"])
    stop_called = []
    monkeypatch.setattr(docker_client_module, "stop_tunnel", lambda: stop_called.append(True))
    r = client.post("/api/auth/reset-config", headers=_AUTH_CSRF)
    assert r.status_code == 200
    assert config_module._cfg.api_token == ""
    assert config_module._cfg.docker_host == ""
    assert config_module._cfg.allowed_registries == []
    assert stop_called  # tunnel cleanup attempted


def test_reset_config_blocked_when_from_env(client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    r = client.post("/api/auth/reset-config", headers=_AUTH_CSRF)
    assert r.status_code == 403
    assert config_module._cfg.api_token == "configured-token-32chars-long"


def test_reset_config_reopens_setup_window(client, monkeypatch):
    """After reset, setup should be callable again even if startup was long ago.

    Without this, reset leaves the server in a state where the wizard loads but
    POST /api/setup returns 403 'window closed' and the user is stuck.
    """
    _configure(monkeypatch)
    # Simulate a server that started a long time ago
    import time as time_module

    import skiff.config as config_module
    old_start = time_module.monotonic() - 10_000
    monkeypatch.setattr(config_module, "APP_START_MONOTONIC", old_start)
    monkeypatch.setattr(docker_client_module, "stop_tunnel", lambda: None)
    r = client.post("/api/auth/reset-config", headers=_AUTH_CSRF)
    assert r.status_code == 200
    # APP_START_MONOTONIC must now be recent
    assert old_start + 9000 < config_module.APP_START_MONOTONIC


def test_reset_config_tunnel_cleanup_error_is_swallowed(client, monkeypatch):
    """If stop_tunnel raises, the reset still succeeds — tunnel cleanup is best-effort."""
    _configure(monkeypatch)
    def _raiser():
        # After R5 the router only catches (subprocess.SubprocessError, OSError).
        raise OSError("simulated tunnel stop failure")
    monkeypatch.setattr(docker_client_module, "stop_tunnel", _raiser)
    r = client.post("/api/auth/reset-config", headers=_AUTH_CSRF)
    assert r.status_code == 200
    assert config_module._cfg.api_token == ""


# ── R3: probe-docker pre-setup endpoint ───────────────────────────────────


def test_probe_docker_disabled_after_setup(client, monkeypatch):
    _configure(monkeypatch)
    r = client.get("/api/setup/probe-docker")
    assert r.status_code == 403


def test_probe_docker_returns_classification(client, monkeypatch):
    """Pre-setup call returns reachable/unreachable lists. On a test host
    without Docker running, all paths land in unreachable — that's fine, the
    response shape is still correct."""
    # Server is unconfigured by default via reset_config fixture
    # Mock the probe to deterministic results so we don't depend on the test host
    import skiff.routers.setup as setup_module
    monkeypatch.setattr(setup_module, "_probe_docker_socket",
                        lambda p: (p == "/var/run/docker.sock", p.replace("~", "/home/x")))
    r = client.get("/api/setup/probe-docker")
    assert r.status_code == 200
    body = r.json()
    assert "reachable" in body and "unreachable" in body
    assert "unix:///var/run/docker.sock" in body["reachable"]
    # Other HOME-relative paths end up unreachable
    assert all(u.startswith("unix:///home/x/") for u in body["unreachable"])


def test_probe_docker_no_real_docker_ok(client):
    """On a machine with no Docker at any of the probed paths, response is 200
    with an empty reachable list — not 500. This is the common case for a
    brand-new install."""
    r = client.get("/api/setup/probe-docker")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["reachable"], list)
    assert isinstance(body["unreachable"], list)
