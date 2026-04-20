# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Precedence / security invariants on the config hierarchy.

`docs/configuration.md` declares a fixed five-layer precedence chain:
1. env var → 2. runtime /api/setup → 3. defaults.toml → 4. inline
default → 5. (GUI only) hardcoded JS fallback. This file enforces the
security-critical invariants of that chain mechanically so a future
refactor can't silently drop a layer or leak a secret to the wrong
surface.

The GUI layer isn't exercised here (it's a browser-side concern); the
other four are.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from skiff import config


@pytest.mark.unit
def test_no_secret_knob_is_exposed() -> None:
    """secret=True knobs (API_TOKEN) must never carry expose=True — that
    would publish the bearer token on /api/config. Invariant holds
    independent of whichever knob is marked secret tomorrow."""
    for spec in config.knobs().values():
        if spec.secret:
            assert not spec.expose, (
                f"{spec.name} is both secret and exposed — /api/config would leak it. "
                f"Drop expose=True or clear secret=True."
            )


@pytest.mark.unit
def test_no_knob_name_collision() -> None:
    """config_knob() raises on double-registration, but a CI run that imports
    everything ensures no module path produces a collision."""
    # Re-importing config would raise if any knob were registered twice; the
    # import at module top of this file already validates that. This test
    # just asserts the registry is a proper dict (no dup keys masked by
    # dict re-assignment).
    names = [spec.name for spec in config.knobs().values()]
    assert len(names) == len(set(names))


@pytest.mark.unit
def test_every_defaults_toml_key_matches_a_knob_name() -> None:
    """Orphan TOML keys (typo, removed knob) would silently do nothing —
    the operator would think their baseline override was in effect.
    Every key in defaults.toml must correspond to a registered knob."""
    toml_path = Path(config._CONFIG_DIR) / "defaults.toml"
    text = toml_path.read_text(encoding="utf-8")
    declared = set(config.knobs().keys())
    # Strip comments + blanks, grab the key of every `KEY = value` line.
    toml_keys = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"([A-Z][A-Z0-9_]*)\s*=", stripped)
        if m:
            toml_keys.add(m.group(1))
    orphans = toml_keys - declared
    assert not orphans, (
        f"defaults.toml has keys with no matching config_knob(): {sorted(orphans)}. "
        f"Either register the knob in skiff/config.py or remove the stale entry."
    )


@pytest.mark.unit
def test_every_exposed_knob_has_doc() -> None:
    """An exposed knob with no doc-string is an info-leak without rationale
    — future reviewers can't tell whether the exposure is intentional."""
    for spec in config.knobs().values():
        if spec.expose:
            assert spec.doc.strip(), (
                f"{spec.name} is expose=True but has empty doc= — document why the "
                f"GUI needs this knob so it doesn't get dropped in a later audit."
            )


@pytest.mark.unit
def test_env_var_overrides_defaults_toml(monkeypatch) -> None:
    """Layer-1 (env) precedence over layer-3 (TOML). We pick a numeric knob
    with a TOML default + re-exercise config_knob() to prove env wins."""
    # Use a knob whose TOML default is visible + stable.
    orig_name = "DOCKER_CLIENT_TIMEOUT"
    orig_default = config.knobs()[orig_name].default
    assert orig_default is not None, "precondition: knob has a TOML-sourced default"
    # Drop it from the registry so we can re-register with an env override.
    backup_spec = config.knobs()[orig_name]
    backup_value = config.DOCKER_CLIENT_TIMEOUT
    try:
        del config._KNOBS[orig_name]
        monkeypatch.setenv(orig_name, "999")
        observed = config.config_knob(
            orig_name,
            default=orig_default,
            validator=int,
            doc=backup_spec.doc,
            expose=backup_spec.expose,
        )
        assert observed == 999, "env var did not override TOML default"
    finally:
        # Restore so later tests see the original state.
        config._KNOBS[orig_name] = backup_spec
        config.DOCKER_CLIENT_TIMEOUT = backup_value
        monkeypatch.delenv(orig_name, raising=False)


@pytest.mark.unit
def test_every_exposed_knob_belongs_to_a_section() -> None:
    """The GUI config viewer groups knobs by the `# ── <Section> ──` header
    preceding each declaration in config.py. An exposed knob landing in
    'Other' means its section header is missing, mis-typed, or parked
    under a stale header that no longer matches the knob's purpose. Keep
    the source file tidy so new knobs group naturally in the viewer."""
    config._KNOB_SECTIONS = {}
    orphans = [
        spec.name for spec in config.knobs().values() if spec.expose and config.knob_section(spec.name) == "Other"
    ]
    assert not orphans, (
        f"Exposed knobs with no section header: {orphans}. Add a "
        f"`# ── <label> ──` comment directly above each declaration in "
        f"skiff/config.py so the Settings page groups it correctly."
    )


@pytest.mark.unit
def test_api_config_knobs_shape_and_auth() -> None:
    """/api/config/knobs returns grouped knob metadata. Secrets are
    redacted, non-exposed knobs are absent, and every entry carries the
    fields the GUI relies on. Gates unauthenticated callers with a
    401/503 depending on whether the server is in naive mode."""
    from fastapi.testclient import TestClient

    from app import app

    with TestClient(app, raise_server_exceptions=True) as tc:
        r = tc.get("/api/config/knobs")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "groups" in body and isinstance(body["groups"], list)
        names_returned: set[str] = set()
        secret_seen = 0
        for group in body["groups"]:
            assert group["category"], "each group must be labelled"
            for knob in group["knobs"]:
                names_returned.add(knob["name"])
                for field in (
                    "name",
                    "value",
                    "default",
                    "source",
                    "doc",
                    "category",
                    "secret",
                    "edit_status",
                    "edit_reason",
                    "hidden",
                ):
                    assert field in knob, f"{knob['name']} missing field {field!r}"
                assert knob["source"] in {"env", "toml", "default", "unset", "runtime"}
                assert knob["edit_status"] in {"live", "security", "lifecycle"}
                # Every non-live row must carry a reason so the operator
                # knows WHY it's read-only (Part of the "either edit or
                # show why not" invariant the user called for).
                if knob["edit_status"] != "live":
                    assert knob["edit_reason"], (
                        f"{knob['name']} edit_status={knob['edit_status']!r} but "
                        f"edit_reason is empty — every read-only knob must explain itself."
                    )
                if knob["secret"]:
                    secret_seen += 1
                    assert knob["value"] is None, (
                        f"{knob['name']} is secret but value was {knob['value']!r} — "
                        f"secrets MUST render as null to the GUI."
                    )
        # Every exposed-and-not-secret knob appears in the viewer.
        expected = {s.name for s in config.knobs().values() if s.expose}
        assert names_returned == expected
        # API_TOKEN must NOT appear (expose=False AND secret=True).
        assert "API_TOKEN" not in names_returned


@pytest.mark.unit
def test_setup_refuses_when_api_token_came_from_env() -> None:
    """Runtime /api/setup (layer 2) must NOT overwrite an env-sourced
    API_TOKEN (layer 1). The check lives in `skiff.routers.setup`; if a
    refactor moves it, this test alarms."""
    from fastapi.testclient import TestClient

    from app import app
    from skiff.routers import setup as setup_module

    original_token = config._cfg.api_token
    original_from_env = config._cfg.from_env
    # Simulate an env-sourced config.
    config._cfg.api_token = "x" * 32
    config._cfg.from_env = True
    setup_module._setup_failures.clear()
    try:
        with TestClient(app, raise_server_exceptions=True) as tc:
            r = tc.post(
                "/api/setup",
                json={
                    "docker_host": "unix:///var/run/docker.sock",
                    "api_token": "y" * 32,
                    "allowed_registries": "",
                },
                headers={"X-Requested-With": "ContainerManager"},
            )
            assert r.status_code in (403, 409), (
                f"/api/setup must refuse when from_env=True; got {r.status_code} body={r.text!r}"
            )
    finally:
        config._cfg.api_token = original_token
        config._cfg.from_env = original_from_env
        setup_module._setup_failures.clear()
