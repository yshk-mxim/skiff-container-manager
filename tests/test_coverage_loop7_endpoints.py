# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""HTTP-level coverage for Loop 6/7 endpoint additions.

Uses the standard `client` + `mock_docker` fixtures from conftest so
every test runs through the real FastAPI + middleware stack. The
prior file `test_coverage_loop7.py` covers pure helpers; this file
asserts the wire behaviour of the endpoints those helpers serve.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import skiff.config as config_module
from tests.conftest import AUTH_CSRF, AUTH_HEADER

# ── POST /api/profile/enter-reviewer ────────────────────────────────────────


@pytest.mark.unit
def test_enter_reviewer_succeeds_from_dev(client):
    """Fresh dev → reviewer flip: returns ok + exec_sessions_closed."""
    saved = config_module.PROFILE
    try:
        config_module.PROFILE = "dev"
        resp = client.post("/api/profile/enter-reviewer", headers=AUTH_CSRF)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["profile"] == "reviewer"
        assert "exec_sessions_closed" in body
        assert config_module.PROFILE == "reviewer"
    finally:
        config_module.PROFILE = saved


@pytest.mark.unit
def test_enter_reviewer_idempotent_when_already_reviewer(client):
    """Already-reviewer with a token set → gate fires 403.

    _reject_if_reviewer short-circuits the decorator before the
    handler body runs, which is the correct behaviour: the UI only
    exposes the dropdown when PROFILE != reviewer.
    """
    saved = config_module.PROFILE
    try:
        config_module.PROFILE = "reviewer"
        resp = client.post("/api/profile/enter-reviewer", headers=AUTH_CSRF)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "auth.reviewer_read_only"
    finally:
        config_module.PROFILE = saved


# ── GET /api/config includes `profile` + `rate_limit_scale` ─────────────────


@pytest.mark.unit
def test_api_config_surfaces_profile_and_rate_scale(client):
    resp = client.get("/api/config", headers=AUTH_HEADER)
    assert resp.status_code == 200
    cfg = resp.json()
    # Core persona fields. Type assertions keep the contract.
    assert isinstance(cfg.get("profile"), str) and cfg["profile"]
    assert isinstance(cfg.get("rate_limit_scale"), int)
    assert cfg.get("bind_host")
    # insecure_mode is server-computed, not operator-settable.
    assert isinstance(cfg["insecure_mode"], bool)


# ── POST /api/auth/reset-config restores PROFILE ────────────────────────────


@pytest.mark.unit
def test_reset_config_restores_boot_profile():
    """Directly invoke the handler's side effects — PROFILE resets.

    Mutates BOTH `config.PROFILE` and `config._cfg` during the test.
    Every mutated field is snapshotted AND restored in the finally
    block; a leak poisons unrelated tests (registry allow-list,
    tunnel status) that share the module-level `_cfg` singleton.
    """
    from fastapi import Request

    from skiff.routers.setup import reset_config

    saved_profile = config_module.PROFILE
    saved_from_env = config_module._cfg.from_env
    saved_token = config_module._cfg.api_token
    saved_host = config_module._cfg.docker_host
    saved_registries = list(config_module._cfg.allowed_registries)
    boot = config_module._BOOT_PROFILE
    try:
        config_module._cfg.from_env = False
        config_module.PROFILE = "reviewer"
        config_module._cfg.api_token = "t" * 16
        config_module._cfg.docker_host = ""
        mock_req = MagicMock(spec=Request)
        with patch("skiff.routers.setup.docker_client.stop_tunnel"):
            reset_config(mock_req)
        assert boot == config_module.PROFILE
        # Side effects: token + docker_host cleared.
        assert config_module._cfg.api_token == ""
        assert config_module._cfg.docker_host == ""
    finally:
        config_module.PROFILE = saved_profile
        config_module._cfg.from_env = saved_from_env
        config_module._cfg.api_token = saved_token
        config_module._cfg.docker_host = saved_host
        # reset_config also clears allowed_registries; restore them
        # so registry-allowlist tests that run after this one find
        # the global config in the same shape the conftest bootstrap
        # left it.
        config_module._cfg.allowed_registries = saved_registries


# ── GET /api/networks/{id}/inspect (new in Loop 7) ──────────────────────────


@pytest.mark.unit
def test_network_inspect_returns_attrs(client, mock_docker):
    net = MagicMock()
    net.attrs = {
        "Id": "abc123",
        "Name": "skiff-net",
        "Driver": "bridge",
        "Options": {"com.docker.network.bridge.name": "br-abc"},
    }
    mock_docker.networks.get.return_value = net

    resp = client.get(
        "/api/networks/abc1234567890123/inspect",
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["Name"] == "skiff-net"
    assert body["Driver"] == "bridge"
    assert "Options" in body


@pytest.mark.unit
def test_network_inspect_bad_id_400(client):
    resp = client.get(
        "/api/networks/not-a-hex-id/inspect",
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "validation.bad_input"


# ── DELETE /api/containers/{id} defaults to undo=true ───────────────────────


@pytest.mark.unit
def test_delete_container_default_undo_true(client, mock_docker):
    c = MagicMock()
    c.short_id = "def456abc789"
    c.remove = MagicMock()
    mock_docker.containers.get.return_value = c

    resp = client.delete("/api/containers/def456abc789", headers=AUTH_CSRF)
    assert resp.status_code == 200
    body = resp.json()
    # Default undo=true means the response is an UndoableResponse with a token.
    assert "undo_token" in body
    # The immediate remove should NOT have been called.
    c.remove.assert_not_called()


@pytest.mark.unit
def test_delete_container_explicit_undo_false_hard_deletes(client, mock_docker):
    c = MagicMock()
    c.short_id = "def456abc789"
    c.remove = MagicMock()
    mock_docker.containers.get.return_value = c

    resp = client.delete(
        "/api/containers/def456abc789?undo=false",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    c.remove.assert_called_once()


# ── /api/containers/run surfaces early exit + logs tail ─────────────────────


@pytest.mark.unit
def test_run_container_surfaces_exit_code_on_early_exit(client, mock_docker):
    """Fast-exit containers should surface exit_code + logs_tail.

    image + name are query params per `RunContainerRequest`.
    """
    container = MagicMock()
    container.short_id = "exit-abc"
    container.name = "quick-fail"
    container.status = "exited"
    container.attrs = {"State": {"ExitCode": 1}}
    container.logs.return_value = b"nginx: mkdir failed\n"
    container.image.tags = ["nginx:1.25-alpine"]
    container.reload = MagicMock()
    mock_docker.containers.run.return_value = container

    with patch("skiff.routers.containers.time.sleep"):
        resp = client.post(
            "/api/containers/run?image=nginx:1.25-alpine&name=quick-fail",
            headers=AUTH_CSRF,
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["exit_code"] == 1
    assert "nginx: mkdir failed" in body["logs_tail"]


@pytest.mark.unit
def test_run_container_healthy_no_logs_tail(client, mock_docker):
    """Running containers: no exit_code / logs_tail in response."""
    container = MagicMock()
    container.short_id = "run-abc"
    container.name = "runner"
    container.status = "running"
    container.attrs = {"State": {"ExitCode": None}}
    container.image.tags = ["alpine:3.19"]
    container.reload = MagicMock()
    mock_docker.containers.run.return_value = container

    with patch("skiff.routers.containers.time.sleep"):
        resp = client.post(
            "/api/containers/run?image=alpine:3.19&name=runner",
            headers=AUTH_CSRF,
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "exit_code" not in body
    assert "logs_tail" not in body


# ── Audit `auth.reviewer_denied` vs `auth.denied` on the same wire path ─────


@pytest.mark.unit
def test_reviewer_mutation_audit_uses_reviewer_denied(client):
    """End-to-end: a mutation attempt while PROFILE=reviewer produces
    `event_type=auth.reviewer_denied` on the audit.api_access record.

    Loop 9 OSS caught a bug where the contextvars-scoped code didn't
    survive the anyio task boundary; the AuditLogMiddleware now peeks
    the serialized response body for `detail.code`. This test asserts
    the middleware classifies the same 403 uniquely — spying on the
    internal emit function instead of the rendered stderr, which
    structlog writes outside pytest's capture.
    """
    import skiff.logging_setup as ls

    seen: list[tuple[str, str]] = []
    real_log = ls.log

    class _Spy:
        def info(self, event, **kw):
            seen.append(("info", kw.get("event_type", "")))
            real_log.info(event, **kw)

        def warning(self, event, **kw):
            seen.append(("warning", kw.get("event_type", "")))
            real_log.warning(event, **kw)

        def error(self, event, **kw):
            seen.append(("error", kw.get("event_type", "")))
            real_log.error(event, **kw)

        def __getattr__(self, name):
            return getattr(real_log, name)

    saved = config_module.PROFILE
    try:
        config_module.PROFILE = "reviewer"
        with patch.object(ls, "log", _Spy()):
            resp = client.post(
                "/api/containers/abc1234567890123/start",
                headers=AUTH_CSRF,
            )
    finally:
        config_module.PROFILE = saved
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "auth.reviewer_read_only"
    assert ("warning", "auth.reviewer_denied") in seen, f"expected (warning, auth.reviewer_denied) in {seen}"


# ── compose down 404 for never-deployed project ─────────────────────────────


@pytest.mark.unit
def test_compose_down_nonexistent_project_returns_404(client):
    resp = client.post(
        "/api/compose/down?project_name=nonexistent-xyz",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "compose.not_found"


# ── /api/containers/{name}/inspect works for Docker-resolvable name ─────────


@pytest.mark.unit
def test_validator_accepts_container_name():
    """Validator now accepts both hex id and name at the function level.

    The full HTTP round-trip has an elaborate Pydantic InspectResponse
    that mocking chokes on; the validator behaviour is what changed
    in Loop 7, and it is covered at unit-level here.
    """
    from skiff.validators import validate_container_id

    assert validate_container_id("abc1234567890123") == "abc1234567890123"
    assert validate_container_id("my-app") == "my-app"
    assert validate_container_id("web_api.01") == "web_api.01"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        validate_container_id("bad space")
    assert exc.value.status_code == 400
