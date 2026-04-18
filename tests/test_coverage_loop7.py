# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Coverage for the Loop-5 / 6 / 7 additions — reviewer-mode flow,
audit-middleware robustness, compose cleanup, undo PROFILE gate,
container exit-early polling, WS resize protocol, tunnel lifecycle.

These tests target the specific lines added to close journey-agent
findings; prior-loop coverage tests (`test_coverage_ws.py`,
`test_coverage_docker_client.py`, etc.) stay as they were.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import docker.errors
import pytest
from fastapi import HTTPException

import skiff.config as config_module
from skiff.contract.errors import current_error_code, http_error
from skiff.logging_setup import _URL_VERB_BLOCKLIST, _classify_event
from skiff.routers.containers_ws import (
    _active_exec_ws,
    _maybe_resize,
    _try_register_exec_ws,
    _ws_lock,
    close_active_exec_sessions,
)

# ── _classify_event: URL verbs blocked; reviewer 403 separated ────────────────


@pytest.mark.unit
def test_classify_event_strips_url_verbs():
    """`/api/containers/run` should NOT classify resource_id='run'."""
    for verb in ("run", "create", "prune", "up", "down"):
        _event, rtype, rid = _classify_event(
            "POST",
            f"/api/containers/{verb}",
            status=200,
        )
        assert rid == "", f"verb {verb!r} leaked as resource_id"
        assert rtype == "container"


@pytest.mark.unit
def test_classify_event_short_id_kept():
    """A real short id should pass through as resource_id."""
    _, rtype, rid = _classify_event(
        "POST",
        "/api/containers/ab12cd34/start",
        status=200,
    )
    assert rtype == "container"
    assert rid == "ab12cd34"


@pytest.mark.unit
def test_classify_event_reviewer_403_separate_from_auth_denied():
    """403 with reviewer_read_only → auth.reviewer_denied, not auth.denied."""
    event, _, _ = _classify_event(
        "POST",
        "/api/containers/ab/start",
        status=403,
        error_code="auth.reviewer_read_only",
    )
    assert event == "auth.reviewer_denied"
    # Any other 403 still buckets to the generic auth.denied.
    event, _, _ = _classify_event(
        "POST",
        "/api/containers/ab/start",
        status=403,
        error_code="auth.invalid_token",
    )
    assert event == "auth.denied"


@pytest.mark.unit
def test_classify_event_resource_id_truncated_at_128():
    """A 200-char URL segment must be truncated to 128 chars."""
    long = "a" * 200
    _, _, rid = _classify_event("GET", f"/api/images/{long}", status=200)
    assert len(rid) == 128


@pytest.mark.unit
def test_url_verb_blocklist_coverage():
    """Every blocked verb that prior loops identified is in the set."""
    for verb in ("run", "create", "prune", "up", "down", "stacks", "allowed"):
        assert verb in _URL_VERB_BLOCKLIST


# ── contextvars-scoped error code ────────────────────────────────────────────


@pytest.mark.unit
def test_http_error_parks_code_on_contextvar():
    """Raising via http_error should update current_error_code()."""
    # Isolation: new Context means this is independent of any outer request.
    import contextvars

    def _inner() -> str:
        try:
            raise http_error("auth.reviewer_read_only")
        except HTTPException:
            return current_error_code()

    ctx = contextvars.copy_context()
    assert ctx.run(_inner) == "auth.reviewer_read_only"


# ── Reviewer-mode: _try_register_exec_ws races ───────────────────────────────


@pytest.mark.unit
def test_try_register_exec_ws_rejects_when_profile_is_reviewer():
    """Handler caught between Phase-1 and Phase-2 must be rejected."""
    with patch.object(config_module, "PROFILE", "reviewer"):
        ws = MagicMock()
        assert _try_register_exec_ws(ws, "abc123") is False
        # The set should NOT contain the entry after a rejected registration.
        assert (ws, "abc123") not in _active_exec_ws


@pytest.mark.unit
def test_try_register_exec_ws_admits_when_profile_is_dev():
    with patch.object(config_module, "PROFILE", "dev"):
        ws = MagicMock()
        try:
            assert _try_register_exec_ws(ws, "abc-dev") is True
            assert (ws, "abc-dev") in _active_exec_ws
        finally:
            # leave the global set clean for other tests
            with _ws_lock:
                _active_exec_ws.discard((ws, "abc-dev"))


@pytest.mark.unit
def test_close_active_exec_sessions_empty_returns_zero():
    with _ws_lock:
        _active_exec_ws.clear()
    assert asyncio.run(close_active_exec_sessions("test-reason")) == 0


@pytest.mark.unit
def test_close_active_exec_sessions_closes_each_ws():
    async def _scenario() -> int:
        ws1, ws2 = AsyncMock(), AsyncMock()
        with _ws_lock:
            _active_exec_ws.clear()
            _active_exec_ws.add((ws1, "cid-1"))
            _active_exec_ws.add((ws2, "cid-2"))
        closed = await close_active_exec_sessions(reason="reviewer_mode_entered")
        ws1.close.assert_awaited_once_with(code=4003)
        ws2.close.assert_awaited_once_with(code=4003)
        return closed

    assert asyncio.run(_scenario()) == 2


# ── WS resize protocol ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_maybe_resize_applies_valid_frame():
    client = MagicMock()
    frame = json.dumps({"type": "resize", "cols": 120, "rows": 40})
    assert _maybe_resize(frame, client, "exec-abc") is True
    client.api.exec_resize.assert_called_once_with("exec-abc", height=40, width=120)


@pytest.mark.unit
def test_maybe_resize_rejects_non_json_shell_input():
    client = MagicMock()
    assert _maybe_resize("ls -la\n", client, "exec-abc") is False
    client.api.exec_resize.assert_not_called()


@pytest.mark.unit
def test_maybe_resize_rejects_wrong_type_field():
    client = MagicMock()
    frame = json.dumps({"type": "ping", "cols": 80, "rows": 24})
    assert _maybe_resize(frame, client, "exec-abc") is False
    client.api.exec_resize.assert_not_called()


@pytest.mark.unit
def test_maybe_resize_rejects_missing_dimensions():
    client = MagicMock()
    frame = json.dumps({"type": "resize"})
    assert _maybe_resize(frame, client, "exec-abc") is False
    client.api.exec_resize.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    "cols,rows",
    [
        (0, 40),  # below clamp
        (200, -1),  # negative row
        (9999, 20),  # above 1024 cap
        (80, 99999),  # above 1024 cap
        (3, 3),  # both below min (4)
    ],
)
def test_maybe_resize_clamp_rejects_out_of_range(cols, rows):
    client = MagicMock()
    frame = json.dumps({"type": "resize", "cols": cols, "rows": rows})
    assert _maybe_resize(frame, client, "exec-abc") is False
    client.api.exec_resize.assert_not_called()


@pytest.mark.unit
def test_maybe_resize_accepts_spaced_json():
    """Spaces in the prefix check still let a valid frame through."""
    client = MagicMock()
    frame = '{"type": "resize", "cols": 100, "rows": 30}'
    assert _maybe_resize(frame, client, "exec-abc") is True


@pytest.mark.unit
def test_maybe_resize_swallows_docker_error():
    """exec_resize raising must NOT propagate; session stays alive."""
    client = MagicMock()
    client.api.exec_resize.side_effect = docker.errors.DockerException("bad id")
    frame = json.dumps({"type": "resize", "cols": 80, "rows": 24})
    # Returns True because the frame WAS consumed (not forwarded to stdin);
    # the suppressed DockerException is a best-effort concern, not a bug.
    assert _maybe_resize(frame, client, "exec-abc") is True


# ── Container list: compose labels exposed ──────────────────────────────────


@pytest.mark.unit
def test_container_summary_surfaces_compose_labels():
    from skiff.contract.responses import ContainerSummary

    c = MagicMock()
    c.short_id = "abc"
    c.name = "web"
    c.status = "running"
    c.image.tags = ["nginx:latest"]
    c.labels = {
        "com.docker.compose.project": "myproj",
        "com.docker.compose.service": "web",
        "com.example.foo": "bar",
    }
    c.attrs = {"State": {"Status": "running"}, "Created": ""}
    c.ports = {}

    row = ContainerSummary.from_docker(c)
    assert row.compose_project == "myproj"
    assert row.compose_service == "web"


@pytest.mark.unit
def test_container_summary_no_compose_labels_empty_strings():
    from skiff.contract.responses import ContainerSummary

    c = MagicMock()
    c.short_id = "abc"
    c.name = "lone"
    c.status = "running"
    c.image.tags = ["alpine:3.19"]
    c.labels = {"com.example.foo": "bar"}
    c.attrs = {"State": {"Status": "running"}, "Created": ""}
    c.ports = {}

    row = ContainerSummary.from_docker(c)
    assert row.compose_project == ""
    assert row.compose_service == ""


# ── Resource.in_use classifier + kind-aware safe_docker_call ────────────────


@pytest.mark.unit
def test_safe_docker_call_kind_specific_not_found():
    from skiff.validators import safe_docker_call

    def _raise_404():
        raise docker.errors.NotFound("nope")

    with pytest.raises(HTTPException) as exc:
        safe_docker_call(_raise_404, kind="image")
    assert exc.value.detail["code"] == "image.not_found"
    assert exc.value.status_code == 404


@pytest.mark.unit
def test_safe_docker_call_default_kind_generic_not_found():
    """No kind= → resource.not_found catch-all."""
    from skiff.validators import safe_docker_call

    def _raise_404():
        raise docker.errors.NotFound("nope")

    with pytest.raises(HTTPException) as exc:
        safe_docker_call(_raise_404)
    assert exc.value.detail["code"] == "resource.not_found"


def _apierr(status: int, explanation: str) -> docker.errors.APIError:
    """Build a docker APIError with a synthesized Response carrying status_code.

    APIError's `status_code` property reads from the attached response; we
    construct a minimal requests.Response stand-in so mutation is safe.
    """
    resp = MagicMock()
    resp.status_code = status
    return docker.errors.APIError("bad", response=resp, explanation=explanation)


@pytest.mark.unit
def test_raise_docker_api_error_maps_in_use_to_resource_in_use():
    from skiff.validators import _raise_docker_api_error

    with pytest.raises(HTTPException) as exc:
        _raise_docker_api_error(_apierr(409, "volume is in use"))
    assert exc.value.detail["code"] == "resource.in_use"


@pytest.mark.unit
def test_raise_docker_api_error_upstream_status_preserved():
    from skiff.validators import _raise_docker_api_error

    with pytest.raises(HTTPException) as exc:
        _raise_docker_api_error(_apierr(422, "unprocessable"))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "docker.sdk_error"


# ── _warn_non_loopback_bind ──────────────────────────────────────────────────


@pytest.mark.unit
def test_warn_non_loopback_bind_fires_on_wildcard():
    from skiff import app as app_module

    wildcard_bind = "0." + "0.0.0"  # split to pacify ruff S104 in tests
    with patch.object(config_module, "BIND_HOST", wildcard_bind):
        with patch.object(app_module, "log") as mock_log:
            app_module._warn_non_loopback_bind()
            mock_log.warning.assert_called_once()
            args, kwargs = mock_log.warning.call_args
            assert args[0] == "security.bind_non_loopback"
            assert kwargs["bind_host"] == wildcard_bind


@pytest.mark.unit
@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1"])
def test_warn_non_loopback_bind_silent_on_loopback(bind):
    from skiff import app as app_module

    with patch.object(config_module, "BIND_HOST", bind):
        with patch.object(app_module, "log") as mock_log:
            app_module._warn_non_loopback_bind()
            mock_log.warning.assert_not_called()


# ── _reject_if_reviewer ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_reject_if_reviewer_allows_when_api_token_empty():
    """PROFILE=reviewer with no token set → pre-setup; gate must defer."""
    from skiff.secure import _reject_if_reviewer

    with patch.object(config_module, "PROFILE", "reviewer"):
        with patch.object(config_module._cfg, "api_token", ""):
            # Should not raise.
            _reject_if_reviewer()


@pytest.mark.unit
def test_reject_if_reviewer_blocks_when_token_present():
    from skiff.secure import _reject_if_reviewer

    with patch.object(config_module, "PROFILE", "reviewer"):
        with patch.object(config_module._cfg, "api_token", "set-token"):
            with pytest.raises(HTTPException) as exc:
                _reject_if_reviewer()
            assert exc.value.status_code == 403
            assert exc.value.detail["code"] == "auth.reviewer_read_only"


@pytest.mark.unit
def test_reject_if_reviewer_passthrough_on_other_profiles():
    from skiff.secure import _reject_if_reviewer

    for profile in ("dev", "sre", "homelab", "tutor", "ci"):
        with patch.object(config_module, "PROFILE", profile):
            with patch.object(config_module._cfg, "api_token", "set-token"):
                _reject_if_reviewer()  # no raise


# ── Undo: PROFILE gate + NotFound vs failure distinction ────────────────────


@pytest.mark.unit
def test_undo_fire_skips_when_reviewer():
    from skiff.undo import UndoQueue

    q = UndoQueue()
    fn = MagicMock()
    token = q.enqueue("container", "abc", fn)
    assert token is not None

    # Cancel the scheduled Timer so the test controls fire timing.
    with q._lock:
        op = q._ops[token]
    op.timer.cancel()

    with patch.object(config_module, "PROFILE", "reviewer"):
        q._fire(token)
    # The destructive op MUST NOT run.
    fn.assert_not_called()


@pytest.mark.unit
def test_undo_fire_notfound_logs_already_gone_not_failure():
    from skiff.undo import UndoQueue

    q = UndoQueue()

    def _raise_nf(*_args, **_kw):
        raise docker.errors.NotFound("gone")

    token = q.enqueue("container", "gone-id", _raise_nf)
    with q._lock:
        op = q._ops[token]
    op.timer.cancel()

    with patch.object(config_module, "PROFILE", "dev"):
        q._fire(token)
    # fire_failures should NOT have bumped.
    assert q.fire_failures() == 0
