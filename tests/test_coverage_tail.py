# SPDX-License-Identifier: MIT
"""Targeted tests for the last gaps in app / logging_setup / system /
images / volumes — the modules sitting below the 95% per-file bar after
the critical-path sweep brought auth/secure/validators/undo to 100%.

Where a function has ≥2 branches, prefer Hypothesis over example tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import skiff.app as app_module
import skiff.config as config_module
from skiff.app import app
from tests.conftest import AUTH_HEADER, CSRF_HEADER, TOKEN

CSRF = CSRF_HEADER

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_cfg(monkeypatch):
    monkeypatch.setattr(config_module._cfg, "api_token", TOKEN)
    monkeypatch.setattr(config_module._cfg, "from_env", True)
    yield


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ── skiff/app.py startup-warning helpers ─────────────────────────────────────

def test_warn_empty_api_token_env_fires(monkeypatch, caplog):
    """Line 71: warning emits when the env var is set but empty.

    The three-way flag (`api_token_set_but_empty`) is centralized in
    config; flip it and confirm the warning path runs.
    """
    monkeypatch.setattr(config_module._cfg, "api_token_set_but_empty", True)
    with patch.object(app_module.log, "warning") as mock_warn:
        app_module._warn_empty_api_token_env()
    mock_warn.assert_called_once()
    assert "empty_api_token_env" in mock_warn.call_args[0][0]


def test_warn_no_registry_allowlist_fires(monkeypatch):
    """Line 80: empty allowed_registries triggers the allowlist warning."""
    monkeypatch.setattr(config_module._cfg, "allowed_registries", [])
    with patch.object(app_module.log, "warning") as mock_warn:
        app_module._warn_no_registry_allowlist()
    mock_warn.assert_called_once()
    assert "no_registry_allowlist" in mock_warn.call_args[0][0]


@given(host=st.sampled_from(["example.com", "10.0.0.5", "my-remote-host"]))
def test_warn_unencrypted_docker_host_fires_on_remote_http(host: str) -> None:
    """Lines 90-94: http:// to a non-local host surfaces a warning.

    Loopback addresses (127.0.0.1, localhost, ::1) are exempt; everything
    else is flagged because the Docker daemon API over plain HTTP is
    credential-equivalent to anyone on the path.
    """
    with patch.object(config_module._cfg, "docker_host", f"http://{host}:2375"):
        with patch.object(app_module.log, "warning") as mock_warn:
            app_module._warn_unencrypted_docker_host()
    mock_warn.assert_called_once()
    assert "docker_host_unencrypted" in mock_warn.call_args[0][0]


def test_warn_unencrypted_docker_host_localhost_silent() -> None:
    """Loopback http:// hosts must NOT warn (the other branch of 92-93)."""
    # IPv6 literal needs the RFC 3986 bracket form for urlparse to give
    # back a hostname of `::1`; otherwise the scheme-authority split
    # eats the colons and there's no hostname to match against the list.
    for url in ("http://127.0.0.1:2375", "http://localhost:2375", "http://[::1]:2375"):
        with patch.object(config_module._cfg, "docker_host", url):
            with patch.object(app_module.log, "warning") as mock_warn:
                app_module._warn_unencrypted_docker_host()
        mock_warn.assert_not_called()


def test_log_dependency_versions_swallows_package_not_found():
    """Lines 123-124: a missing/unavailable dep doesn't crash startup."""
    import importlib.metadata as imeta
    with patch("importlib.metadata.version", side_effect=imeta.PackageNotFoundError("x")):
        # Must not raise
        app_module._log_dependency_versions()


# ── skiff/routers/system.py — connect_snippets + _resolve_knob ───────────────

def test_connect_snippets_endpoint_returns_tools(client):
    """Lines 162-190: connect_snippets assembles tools from the TOML catalogue."""
    r = client.get("/api/connect-snippets", headers=AUTH_HEADER)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["tools"], list)
    # The catalogue is non-empty; check the shape of the first entry.
    if body["tools"]:
        tool = body["tools"][0]
        assert {"id", "label", "hint", "note", "blocks"} <= tool.keys()
        for block in tool["blocks"]:
            assert {"kind", "filename", "content"} <= block.keys()


def test_connect_snippets_renders_runtime_context(client, monkeypatch):
    """Runtime context values (dockerHost, origin, audit_log_glob) get
    substituted into the `{placeholder}` tokens of each block."""
    monkeypatch.setattr(config_module._cfg, "docker_host", "unix:///tmp/probe.sock")
    r = client.get("/api/connect-snippets", headers={**AUTH_HEADER, "host": "skiff.example.com"})
    assert r.status_code == 200
    rendered = " ".join(
        b["content"]
        for t in r.json()["tools"]
        for b in t["blocks"]
    )
    # One of the snippets references {dockerHost}; after rendering the
    # placeholder must be gone. (Exact content depends on the TOML — we
    # just assert the substitution happened, not a specific string.)
    assert "{dockerHost}" not in rendered


def test_resolve_knob_fallback_path(monkeypatch):
    """Lines 76-82: knob not mirrored on _cfg → read env + run validator."""
    from skiff.routers.system import _resolve_knob

    # Use a name that's deliberately NOT a config._cfg attribute so
    # hasattr() returns False without mutation gymnastics.
    name_bad = "XX_ABSENT_KNOB_BAD"
    spec_bad = MagicMock(default="default-fallback", validator=int)
    monkeypatch.setenv(name_bad, "not-an-int")
    assert not hasattr(config_module._cfg, name_bad.lower())
    assert _resolve_knob(name_bad, spec_bad) == "default-fallback"

    name_good = "XX_ABSENT_KNOB_GOOD"
    spec_good = MagicMock(default="1", validator=int)
    monkeypatch.setenv(name_good, "42")
    assert not hasattr(config_module._cfg, name_good.lower())
    assert _resolve_knob(name_good, spec_good) == 42


# ── skiff/routers/volumes.py + images.py undo-delete paths ──────────────────

def _override_docker_client(mock_client):
    """Attach a MagicMock in place of docker_client_dep so the handler
    doesn't try to reach a real daemon."""
    from skiff.docker_client import docker_client_dep
    app.dependency_overrides[docker_client_dep] = lambda: mock_client


def _clear_docker_client_override():
    from skiff.docker_client import docker_client_dep
    app.dependency_overrides.pop(docker_client_dep, None)


def test_volume_delete_with_undo_returns_token(client):
    """volumes.py lines 129-140: delete_volume?undo=true queues + returns token."""
    mock_client = MagicMock()
    mock_client.volumes.get.return_value = MagicMock()
    _override_docker_client(mock_client)
    try:
        r = client.delete("/api/volumes/my-vol?undo=true", headers={**AUTH_HEADER, **CSRF})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "undo_token" in body
        assert body["expires_in"] == config_module.UNDO_DELAY_SECS
        from skiff.undo import get_queue
        get_queue().cancel(body["undo_token"])
    finally:
        _clear_docker_client_override()


def test_history_created_int_coerced_to_iso():
    """Regression: Docker's image.history() returns `Created` as a Unix int,
    not an ISO string. _history_created_to_iso must coerce so the Pydantic
    submodel doesn't raise ValidationError on every inspect call.
    """
    from skiff.routers.images import _history_created_to_iso
    # Docker "history" entry: Unix timestamp int
    out = _history_created_to_iso(1_776_283_285)
    assert out.startswith("2026-") and out.endswith("Z")
    # ISO-string pass-through (if Docker ever changes format)
    assert _history_created_to_iso("2026-04-15T20:01:25Z") == "2026-04-15T20:01:25Z"
    # Empty / None → empty string
    assert _history_created_to_iso(None) == ""
    assert _history_created_to_iso(0) == ""


def test_image_delete_with_undo_returns_token(client):
    """images.py lines 229-239: delete_image?undo=true queues + returns token."""
    mock_client = MagicMock()
    mock_client.images.get.return_value = MagicMock(id="sha256:abc")
    _override_docker_client(mock_client)
    try:
        r = client.delete(
            "/api/images/abc123def456?undo=true",
            headers={**AUTH_HEADER, **CSRF},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "undo_token" in body
        from skiff.undo import get_queue
        get_queue().cancel(body["undo_token"])
    finally:
        _clear_docker_client_override()


# ── skiff/logging_setup.py: GCP init failure paths already covered by
# ── test_coverage_gaps.py. The remaining 54-69 lines are module-level
# ── import-time code that only runs on import. Lines 134-140 are the
# ── loop-lag monitor's stderr dump — exercised below.


def test_emit_loop_lag_warning_prints_stacks(capsys):
    """_emit_loop_lag_warning dumps every thread's stack to stderr with
    the [LOOP_LAG] marker. Direct unit test of the extracted helper —
    the surrounding infinite async loop is trivial glue."""
    import skiff.logging_setup as mod

    mod._emit_loop_lag_warning(0.45)
    captured = capsys.readouterr()
    assert "[LOOP_LAG] event loop blocked 450ms" in captured.err
    assert "Thread " in captured.err


# ── Hypothesis property: _resolve_knob always returns `spec.default`
# ── when the env var's value fails the validator. Covers the TypeError
# ── arm alongside ValueError (lines 81-82).

@given(raw=st.text(min_size=0, max_size=10).filter(lambda s: "\x00" not in s))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_resolve_knob_validator_failure_returns_default(monkeypatch, raw: str) -> None:
    """Lines 81-82: TypeError (any ValidationError) from the validator
    must fall through to spec.default, never propagate.

    We exclude NUL bytes because os.environ rejects them at setenv time
    (a platform-level restriction, not the code under test)."""
    from skiff.routers.system import _resolve_knob

    def always_fail(_v: str) -> int:
        raise TypeError("nope")

    spec = MagicMock(default="fallback", validator=always_fail)
    name = "XX_HYPOTHESIS_KNOB_NEVER_DEFINED"
    assert not hasattr(config_module._cfg, name.lower())
    monkeypatch.setenv(name, raw)
    assert _resolve_knob(name, spec) == "fallback"
