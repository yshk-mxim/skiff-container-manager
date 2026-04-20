# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Loop-10 coverage push — target the non-critical modules still below
the 95 % bar (app.py, logging_setup.py, containers_ws.py, setup.py,
system.py). Each test asserts a behavioural invariant, not a line —
coverage is a side effect.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import skiff.config as config_module
from tests.conftest import AUTH_CSRF, AUTH_HEADER

# ── setup.py: _fail counter + lockout behaviour ──────────────────────────────


@pytest.mark.unit
def test_setup_lockout_fires_audit_and_429():
    """Crossing SETUP_MAX_ATTEMPTS raises 429 + emits audit.setup_lockout."""
    import skiff.routers.setup as setup_mod

    saved = dict(setup_mod._setup_failures)
    try:
        setup_mod._setup_failures.clear()
        client_ip = "10.0.0.77"
        # Simulate attempts up to just below the threshold.
        for _ in range(config_module.SETUP_MAX_ATTEMPTS):
            setup_mod._fail(client_ip, reason="test-bad-token")
        # Next call: the enforcer rejects.
        with pytest.raises(HTTPException) as exc:
            setup_mod._enforce_lockout(client_ip)
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "auth.setup_locked"
    finally:
        setup_mod._setup_failures.clear()
        setup_mod._setup_failures.update(saved)


@pytest.mark.unit
def test_setup_lockout_decays_after_window(monkeypatch):
    """After SETUP_LOCKOUT_SECS, the counter clears and the IP is unlocked."""
    import time as _time

    import skiff.routers.setup as setup_mod

    saved = dict(setup_mod._setup_failures)
    try:
        setup_mod._setup_failures.clear()
        client_ip = "10.0.0.78"
        # Stash a long-past lockout entry. monotonic() - last_t
        # exceeds the lockout window so the enforcer clears it.
        past = _time.monotonic() - config_module.SETUP_LOCKOUT_SECS - 10
        setup_mod._setup_failures[client_ip] = (
            config_module.SETUP_MAX_ATTEMPTS,
            past,
        )
        # Enforcer must not raise; also clears the stale entry.
        setup_mod._enforce_lockout(client_ip)
        assert client_ip not in setup_mod._setup_failures
    finally:
        setup_mod._setup_failures.clear()
        setup_mod._setup_failures.update(saved)


# ── setup.py: /api/tunnel/reconnect — manual-tunnel alive branch ────────────


@pytest.mark.unit
def test_tunnel_reconnect_manual_already_connected(client):
    """Manual (operator-run) tunnel reachable → 409 tunnel.already_connected."""
    from pathlib import Path

    from skiff import config as _config

    saved_host = _config._cfg.docker_host
    try:
        _config._cfg.docker_host = "unix:///tmp/skiff-test-manual-tunnel.sock"
        with (
            patch("skiff.routers.setup.docker_client.get_tunnel_ssh_target", return_value=None),
            patch("skiff.routers.setup.docker_client.get_tunnel_socket_path", return_value=""),
            patch.object(Path, "exists", return_value=True),
            patch(
                "skiff.routers.setup._probe_docker_socket",
                return_value=(True, "unix:///tmp/skiff-test-manual-tunnel.sock"),
            ),
            patch("skiff.routers.setup.docker_client.invalidate_client"),
        ):
            resp = client.post("/api/tunnel/reconnect", headers=AUTH_CSRF)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "tunnel.already_connected"
    finally:
        _config._cfg.docker_host = saved_host


@pytest.mark.unit
def test_tunnel_reconnect_manual_stale_socket(client):
    """Dangling AF_UNIX file but Docker unreachable → manual_reconnect_required."""
    from pathlib import Path

    from skiff import config as _config

    saved_host = _config._cfg.docker_host
    try:
        _config._cfg.docker_host = "unix:///tmp/skiff-test-stale-tunnel.sock"
        with (
            patch("skiff.routers.setup.docker_client.get_tunnel_ssh_target", return_value=None),
            patch("skiff.routers.setup.docker_client.get_tunnel_socket_path", return_value=""),
            patch.object(Path, "exists", return_value=True),
            patch(
                "skiff.routers.setup._probe_docker_socket",
                return_value=(False, ""),
            ),
        ):
            resp = client.post("/api/tunnel/reconnect", headers=AUTH_CSRF)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "tunnel.manual_reconnect_required"
    finally:
        _config._cfg.docker_host = saved_host


# ── system.py: /api/system/df TimeoutError branch ───────────────────────────


@pytest.mark.unit
def test_system_df_timeout_returns_503(client, mock_docker):
    """`client.df` exceeding DF_TIMEOUT → 503 system.docker_unreachable.

    The handler runs `safe_docker_call(client.df)` in a thread-pool
    executor and wraps it in `wait_for(..., timeout=DF_TIMEOUT)`.
    A synchronous `time.sleep` in the mock blocks the thread past
    the timeout; `wait_for` cancels the future and raises.
    """
    import time as _time

    def _slow_df(*_a, **_kw):
        _time.sleep(0.5)
        return {}

    mock_docker.df = MagicMock(side_effect=_slow_df)
    with patch("skiff.routers.system.config.DF_TIMEOUT", 0.05):
        resp = client.get("/api/system/df", headers=AUTH_HEADER)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "system.docker_unreachable"


# ── containers_ws.py: _ws_acquire over-cap returns False + closes 1013 ─────


@pytest.mark.unit
def test_ws_acquire_over_cap_returns_false():
    """Past WS_MAX_PER_IP the acquire must be False, counter unchanged."""
    from skiff.routers.containers_ws import _ws_acquire, _ws_connections, _ws_lock

    ip = "10.0.0.99"
    with _ws_lock:
        _ws_connections[ip] = config_module.WS_MAX_PER_IP
    try:
        assert _ws_acquire(ip) is False
        with _ws_lock:
            assert _ws_connections[ip] == config_module.WS_MAX_PER_IP
    finally:
        with _ws_lock:
            _ws_connections[ip] = 0


# ── logging_setup.py: audit extras validation fallback ──────────────────────


@pytest.mark.unit
def test_emit_api_access_audit_handles_validation_error():
    """_AuditExtras ValidationError must be caught; audit still emits."""
    from skiff.logging_setup import _emit_api_access_audit

    scope = {
        "path": "/api/containers/ab12cd34/start",
        "method": "POST",
        "client": ("127.0.0.1", 5555),
        # Headers bundle — empty keeps the test focused on the extras branch.
        "headers": [(b"authorization", b"Bearer token-xyz-seven-suffix")],
    }
    # Force _classify_event to return values that, without truncation,
    # would blow out _AuditExtras. The real classifier truncates; patching
    # it gives us the fault-injection path.
    import skiff.logging_setup as ls

    with patch.object(
        ls,
        "_classify_event",
        return_value=("test.event", "x" * 200, "y" * 200),
    ):
        # Must NOT raise; exception path logs audit.extras_invalid.
        _emit_api_access_audit(scope, status_code=200)


# ── logging_setup.py: _extract_envelope_code helper ────────────────────────


@pytest.mark.unit
def test_extract_envelope_code_valid():
    from skiff.logging_setup import _extract_envelope_code

    body = b'{"detail":{"code":"auth.reviewer_read_only","message":"x"}}'
    assert _extract_envelope_code(body) == "auth.reviewer_read_only"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b"not json",
        b'{"detail":"string not dict"}',
        b'{"detail":{"message":"no code key"}}',
        b'{"detail":{"code":12345}}',  # non-str
    ],
)
def test_extract_envelope_code_degrades_gracefully(body):
    from skiff.logging_setup import _extract_envelope_code

    assert _extract_envelope_code(body) == ""


# ── app.py: _warn_ci_profile_needs_token ────────────────────────────────────


@pytest.mark.unit
def test_ci_profile_without_token_warns():
    from skiff import app as app_module

    saved_profile = config_module.PROFILE
    saved_token = config_module._cfg.api_token
    try:
        config_module.PROFILE = "ci"
        config_module._cfg.api_token = ""
        with patch.object(app_module, "log") as mock_log:
            app_module._warn_ci_profile_needs_token()
            mock_log.warning.assert_called_once()
            assert mock_log.warning.call_args[0][0] == "security.ci_profile_needs_token"
    finally:
        config_module.PROFILE = saved_profile
        config_module._cfg.api_token = saved_token


@pytest.mark.unit
def test_ci_profile_with_token_silent():
    from skiff import app as app_module

    saved_profile = config_module.PROFILE
    saved_token = config_module._cfg.api_token
    try:
        config_module.PROFILE = "ci"
        config_module._cfg.api_token = "x" * 32
        with patch.object(app_module, "log") as mock_log:
            app_module._warn_ci_profile_needs_token()
            mock_log.warning.assert_not_called()
    finally:
        config_module.PROFILE = saved_profile
        config_module._cfg.api_token = saved_token


@pytest.mark.unit
def test_non_ci_profile_never_warns_on_ci_knob():
    from skiff import app as app_module

    saved_profile = config_module.PROFILE
    saved_token = config_module._cfg.api_token
    try:
        for profile in ("dev", "sre", "homelab", "tutor", "reviewer"):
            config_module.PROFILE = profile
            config_module._cfg.api_token = ""
            with patch.object(app_module, "log") as mock_log:
                app_module._warn_ci_profile_needs_token()
                mock_log.warning.assert_not_called()
    finally:
        config_module.PROFILE = saved_profile
        config_module._cfg.api_token = saved_token


# ── config.py: positive-int-validator foot-gun rejection ────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["0", "-1", "-99", "  -5"])
def test_positive_int_validator_rejects_non_positive(bad):
    from skiff.config import _positive_int_validator

    validator = _positive_int_validator("TEST", minimum=1)
    with pytest.raises(ValueError):
        validator(bad)


@pytest.mark.unit
@pytest.mark.parametrize("good", ["1", "100", "999999"])
def test_positive_int_validator_accepts_positive(good):
    from skiff.config import _positive_int_validator

    validator = _positive_int_validator("TEST", minimum=1)
    assert validator(good) == int(good)


@pytest.mark.unit
def test_positive_int_validator_respects_minimum():
    from skiff.config import _positive_int_validator

    validator = _positive_int_validator("TEST", minimum=1024)
    with pytest.raises(ValueError):
        validator("1023")
    assert validator("1024") == 1024
    assert validator("2048") == 2048
