# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Security + behaviour tests for PUT /api/config/knobs/<name>.

Every rejection path gets its own test so a future refactor can't
silently drop one of the four gates (unknown / secret / env / security
/ lifecycle). The happy path writes a known LIVE-editable knob and
verifies the value actually took effect in the module.

OWASP ASVS v5.0 mapping:
  V1.2   Verify that secure rendering is tested (secret knob → mask)
  V5.1   Verify input is validated server-side (validator raises 400)
  V7.1.1 Verify session-management knobs cannot be tightened without audit
  V9.2   Verify audit events for state-changing operations
  V14.1  Verify secure defaults are not weakened at runtime via this endpoint
"""

from __future__ import annotations

import pytest

from skiff import config
from skiff.routers.system import (
    _LIFECYCLE_READONLY,
    _LIVE_EDITABLE,
    _SECURITY_READONLY,
)
from tests.conftest import AUTH_CSRF


@pytest.mark.unit
def test_live_editable_does_not_overlap_security_or_lifecycle() -> None:
    """Each knob lands in EXACTLY ONE state. A knob appearing in both
    LIVE and SECURITY would let the UI offer an edit control for a
    policy-locked value — the exact "unintuitive surface" bug we're
    defending against."""
    live_vs_sec = _LIVE_EDITABLE & _SECURITY_READONLY
    live_vs_life = _LIVE_EDITABLE & _LIFECYCLE_READONLY
    sec_vs_life = _SECURITY_READONLY & _LIFECYCLE_READONLY
    assert not live_vs_sec, f"knobs listed as both LIVE and SECURITY: {live_vs_sec}"
    assert not live_vs_life, f"knobs listed as both LIVE and LIFECYCLE: {live_vs_life}"
    assert not sec_vs_life, f"knobs listed as both SECURITY and LIFECYCLE: {sec_vs_life}"


@pytest.mark.unit
def test_every_exposed_knob_has_edit_classification() -> None:
    """Every exposed knob must be in exactly one classification set.
    Unclassified would default to LIFECYCLE (safe), but the goal is to
    make the deliberate choice visible in the source."""
    exposed = {k for k, s in config.knobs().items() if s.expose}
    classified = _LIVE_EDITABLE | _SECURITY_READONLY | _LIFECYCLE_READONLY
    missing = exposed - classified
    assert not missing, (
        f"Exposed knobs with no edit classification: {sorted(missing)}. "
        f"Add each to exactly one of _LIVE_EDITABLE / _SECURITY_READONLY / "
        f"_LIFECYCLE_READONLY in skiff/routers/system.py."
    )


@pytest.mark.unit
def test_classification_knobs_are_actually_declared() -> None:
    """Every name in the three sets must be a real declared knob —
    catches typos like WS_LOG_IDL_TIMEOUT (missing E)."""
    declared = set(config.knobs().keys())
    for set_name, s in (
        ("LIVE_EDITABLE", _LIVE_EDITABLE),
        ("SECURITY_READONLY", _SECURITY_READONLY),
        ("LIFECYCLE_READONLY", _LIFECYCLE_READONLY),
    ):
        bogus = s - declared
        assert not bogus, f"{set_name} contains unknown knob names: {sorted(bogus)}"


def _put(client, name, value):
    import json

    return client.put(
        f"/api/config/knobs/{name}",
        content=json.dumps({"value": str(value)}),
        headers={**AUTH_CSRF, "Content-Type": "application/json"},
    )


@pytest.mark.unit
def test_put_rejects_unknown_knob(client):
    resp = _put(client, "NOT_A_REAL_KNOB", "1")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "config.knob_not_found"


@pytest.mark.unit
def test_put_rejects_invalid_knob_name_grammar(client):
    # Path traversal / special chars — rejected before registry lookup.
    resp = _put(client, "../../../etc/passwd", "evil")
    assert resp.status_code in (400, 404)


@pytest.mark.unit
def test_put_rejects_security_locked_knob(client):
    """ALLOWED_ORIGINS is policy-locked — attempting a runtime edit must
    return the dedicated security envelope so the UI can surface the
    right explanation. Would otherwise silently widen the origin
    allowlist on a running instance."""
    resp = _put(client, "ALLOWED_ORIGINS", "http://evil.example")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "config.knob_security_locked"


@pytest.mark.unit
def test_put_rejects_lifecycle_locked_knob(client):
    """DOCKER_CLIENT_TIMEOUT is baked into the SDK client at import;
    runtime edit would update the display but not the real behaviour.
    Reject with a specific envelope so the UI isn't misleading."""
    resp = _put(client, "DOCKER_CLIENT_TIMEOUT", "60")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "config.knob_lifecycle_locked"


@pytest.mark.unit
def test_put_rejects_secret_knob(client):
    """Secrets never carry expose=True (tests/test_config_precedence
    enforces), so this should 404 at the knob-not-found gate. But we
    explicitly test it as a zero-trust guarantee: even if a future
    expose=True slipped in, the dedicated secret-locked envelope would
    fire. Simulated by forcing expose=True on API_TOKEN for this test."""
    from dataclasses import replace as _replace

    from skiff import config as _cfg

    spec = _cfg._KNOBS["API_TOKEN"]
    _cfg._KNOBS["API_TOKEN"] = _replace(spec, expose=True)
    try:
        resp = _put(client, "API_TOKEN", "new-super-secret-token")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "config.knob_secret_locked"
    finally:
        _cfg._KNOBS["API_TOKEN"] = spec


@pytest.mark.unit
def test_put_rejects_env_sourced_knob(client, monkeypatch):
    """Env precedence wins at runtime too — a value set by env var must
    refuse runtime GUI edits so the operator's intent (env setting)
    isn't silently overridden from inside the UI."""
    from dataclasses import replace as _replace

    from skiff import config as _cfg

    spec = _cfg._KNOBS["MAX_LOG_TAIL"]
    _cfg._KNOBS["MAX_LOG_TAIL"] = _replace(spec, source="env")
    try:
        resp = _put(client, "MAX_LOG_TAIL", "10")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "config.knob_env_sourced"
    finally:
        _cfg._KNOBS["MAX_LOG_TAIL"] = spec


@pytest.mark.unit
def test_put_rejects_invalid_value_through_validator(client):
    """Validator errors must surface their own message so the user sees
    WHY the value was rejected. MAX_LOG_TAIL is int-validated; 'abc'
    fails with the int validator's own ValueError."""
    resp = _put(client, "MAX_LOG_TAIL", "not-a-number")
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["code"] == "validation.bad_input"
    assert "MAX_LOG_TAIL" in body["message"]


@pytest.mark.unit
def test_put_happy_path_updates_live_value(client):
    """The canonical working edit: MAX_LOG_TAIL is LIVE-editable + TOML-
    sourced. After a PUT, config.MAX_LOG_TAIL reflects the new value AND
    a subsequent GET /api/config/knobs reports source="runtime"."""
    original = config.MAX_LOG_TAIL
    try:
        resp = _put(client, "MAX_LOG_TAIL", "42")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"name": "MAX_LOG_TAIL", "value": 42, "source": "runtime"}
        assert config.MAX_LOG_TAIL == 42
        # Subsequent GET must reflect the new value + runtime source.
        listed = client.get("/api/config/knobs", headers=AUTH_CSRF).json()
        found = None
        for group in listed["groups"]:
            for knob in group["knobs"]:
                if knob["name"] == "MAX_LOG_TAIL":
                    found = knob
        assert found is not None
        assert found["value"] == 42
        assert found["source"] == "runtime"
    finally:
        config.MAX_LOG_TAIL = original
        spec = config._KNOBS["MAX_LOG_TAIL"]
        from dataclasses import replace as _replace

        config._KNOBS["MAX_LOG_TAIL"] = _replace(spec, source="toml")


@pytest.mark.unit
def test_settings_js_renders_secrets_through_mask_only() -> None:
    """Source-level invariant: the Settings page MUST render a secret
    knob's value through `_secretMask()`, never directly. Any future
    refactor that wires `k.value` into a secret row's textContent /
    attribute / innerHTML is a leak — the server already redacts to
    null, but defense-in-depth demands the client never sees a chance
    to print the real value even if a bug ships `value` populated.

    Maps to OWASP ASVS v5.0 V1.2 (secure rendering) + V8.3.4 (UI does
    not render sensitive data unless necessary). Zero-trust posture:
    browsers are assumed to be under adversarial control by ext-
    installed extensions, so the rendering layer is treated as a
    distinct protection surface from the API response."""
    from pathlib import Path

    src = Path("skiff/static/pages/settings.js").read_text(encoding="utf-8")
    # Every code path for a secret knob must go through _secretMask().
    # Negative check: no path that reaches a textContent/title/value
    # assignment with `k.value` inside an `if (k.secret)` branch.
    # We grep for the forbidden pattern directly.
    forbidden_patterns = [
        "k.value",  # would be forbidden inside a secret branch
    ]
    # Extract the secret branch (everything inside `if (k.secret) { ... }`).
    # Crude: find `if (k.secret)` and grab the matching block. If the
    # refactor changes structure, test fails loudly — easier to update
    # than to regress.
    idx = src.find("if (k.secret) {")
    assert idx != -1, "settings.js must branch on k.secret for secure rendering"
    brace_depth = 0
    end = idx
    started = False
    for i in range(idx, len(src)):
        if src[i] == "{":
            brace_depth += 1
            started = True
        elif src[i] == "}":
            brace_depth -= 1
            if started and brace_depth == 0:
                end = i
                break
    secret_branch = src[idx:end]
    for pat in forbidden_patterns:
        assert pat not in secret_branch, (
            f"settings.js secret branch references {pat!r} — secrets must NEVER "
            f"read k.value in the rendering path. Use _secretMask() only."
        )
    # Positive check: the mask helper is actually invoked on the secret branch.
    assert "_secretMask()" in secret_branch, (
        "settings.js secret branch does not call _secretMask() — client-side defense-in-depth protection removed."
    )


@pytest.mark.unit
def test_secrets_never_returned_with_value_to_gui():
    """Zero-trust invariant: no code path should let a secret knob's
    value reach the viewer response, even if expose=True slipped in.
    This test forces expose=True on API_TOKEN and verifies the envelope
    still has value=null."""
    from dataclasses import replace as _replace

    from fastapi.testclient import TestClient

    from app import app
    from skiff import config as _cfg

    spec = _cfg._KNOBS["API_TOKEN"]
    _cfg._KNOBS["API_TOKEN"] = _replace(spec, expose=True)
    try:
        with TestClient(app, raise_server_exceptions=True) as tc:
            r = tc.get("/api/config/knobs")
            groups = r.json()["groups"]
            api_token = None
            for g in groups:
                for k in g["knobs"]:
                    if k["name"] == "API_TOKEN":
                        api_token = k
            assert api_token is not None, "with expose=True the knob should appear"
            assert api_token["secret"] is True
            assert api_token["value"] is None, (
                "Even when API_TOKEN is accidentally exposed, the value must be "
                "redacted (None) — client-side mask is defense-in-depth, not the "
                "primary control."
            )
    finally:
        _cfg._KNOBS["API_TOKEN"] = spec
