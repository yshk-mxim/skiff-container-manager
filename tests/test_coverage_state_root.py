# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for the cross-platform user-state-root defaults (no HOME == no assumption).

These tests reload skiff.config to recompute module-level paths under
different env vars. The reload creates a fresh _Config singleton — routers
that read `config._cfg` at call time (R1 namespaced imports) will see the
new singleton, which has an empty api_token and confuses AUTH-dependent
tests in later files. The autouse fixture below restores the module to
its baseline so downstream tests see a normal _Config.
"""

import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_config_after_reload():
    """Snapshot the pristine skiff.config._cfg + reloadable state, then
    reload skiff.config back to it after each test.

    Why not just reload: tests elsewhere in the suite mutate _cfg.api_token
    through fixtures; the baseline `_cfg` object is the one captured at
    first skiff.config import. Reloading creates a NEW _cfg (empty token)
    which breaks auth-requires tests that run later. Copy the pristine
    state, reload, copy back.
    """
    import skiff.config as _cfg_mod

    # Capture attributes of the current _cfg BEFORE any reload.
    pristine = {
        "api_token": _cfg_mod._cfg.api_token,
        "docker_host": _cfg_mod._cfg.docker_host,
        "allowed_registries": list(_cfg_mod._cfg.allowed_registries),
        "allowed_origins": list(_cfg_mod._cfg.allowed_origins),
        "docker_vm_host": _cfg_mod._cfg.docker_vm_host,
        "from_env": _cfg_mod._cfg.from_env,
    }
    yield
    # monkeypatch restored env vars; reload picks up originals.
    importlib.reload(_cfg_mod)
    # Re-apply the captured state so downstream tests see the same
    # mutable singleton they expected.
    for k, v in pristine.items():
        setattr(_cfg_mod._cfg, k, v)
    # Rebind captured names in every module that imported _cfg (and other
    # reloaded-in-test constants) by name. Once R1 namespacing covers
    # skiff.auth / skiff.docker_client / skiff.validators / skiff.app, this
    # list shrinks to zero — each rebind is compensating for a legacy
    # direct-import binding.
    import skiff.app as _app
    import skiff.auth as _auth
    import skiff.docker_client as _dc
    import skiff.validators as _val

    for mod in (_app, _auth, _dc, _val):
        if hasattr(mod, "_cfg"):
            mod._cfg = _cfg_mod._cfg


def _reimport_config(monkeypatch_env: dict):
    """Reload skiff.config under the given env — module-level constants recompute."""
    import os

    for k, v in monkeypatch_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import skiff.config as _cfg

    importlib.reload(_cfg)
    return _cfg


def test_state_root_respects_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG", raising=False)
    monkeypatch.delenv("COMPOSE_DIR", raising=False)
    cfg = _reimport_config({})
    assert tmp_path / "skiff" / "audit.jsonl" == cfg.AUDIT_LOG_PATH
    assert tmp_path / "skiff" / "compose" == cfg.COMPOSE_DIR


def test_state_root_macos_uses_application_support(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG", raising=False)
    monkeypatch.delenv("COMPOSE_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = _reimport_config({})
    assert tmp_path / "Library" / "Application Support" / "skiff" / "audit.jsonl" == cfg.AUDIT_LOG_PATH


def test_state_root_linux_uses_xdg_default(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AUDIT_LOG", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = _reimport_config({})
    assert tmp_path / ".local" / "state" / "skiff" / "audit.jsonl" == cfg.AUDIT_LOG_PATH


def test_state_root_no_home_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("AUDIT_LOG", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = _reimport_config({})
    # Fallback uses .skiff (leading dot) so it's hidden in the working dir
    assert cfg.AUDIT_LOG_PATH.parent.name == ".skiff"
    assert str(tmp_path) in str(cfg.AUDIT_LOG_PATH)


def test_audit_log_env_override_wins_over_defaults(tmp_path, monkeypatch):
    """Production deployments set AUDIT_LOG=/var/log/... — must not be overridden by our defaults."""
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "custom-audit.jsonl"))
    monkeypatch.setenv("HOME", "/Users/whoever")
    cfg = _reimport_config({})
    assert tmp_path / "custom-audit.jsonl" == cfg.AUDIT_LOG_PATH


def test_compose_dir_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_DIR", str(tmp_path / "custom-compose"))
    cfg = _reimport_config({})
    assert tmp_path / "custom-compose" == cfg.COMPOSE_DIR


# ── R11: PROFILE preset tests ────────────────────────────────────────────────


def test_profile_ci_sets_rate_limit_scale(monkeypatch):
    monkeypatch.setenv("PROFILE", "ci")
    monkeypatch.delenv("RATE_LIMIT_SCALE", raising=False)
    cfg = _reimport_config({})
    assert cfg._RATE_SCALE == 100


def test_profile_homelab_sets_loose_limits(monkeypatch):
    monkeypatch.setenv("PROFILE", "homelab")
    monkeypatch.delenv("RATE_LIMIT_SCALE", raising=False)
    cfg = _reimport_config({})
    assert cfg._RATE_SCALE == 10


def test_profile_sre_sets_medium_limits(monkeypatch):
    monkeypatch.setenv("PROFILE", "sre")
    monkeypatch.delenv("RATE_LIMIT_SCALE", raising=False)
    cfg = _reimport_config({})
    assert cfg._RATE_SCALE == 3


def test_profile_dev_is_noop(monkeypatch):
    monkeypatch.setenv("PROFILE", "dev")
    monkeypatch.delenv("RATE_LIMIT_SCALE", raising=False)
    cfg = _reimport_config({})
    # dev preset doesn't override anything — default RATE_LIMIT_SCALE=1 applies
    assert cfg._RATE_SCALE == 1


def test_profile_unknown_raises(monkeypatch):
    monkeypatch.setenv("PROFILE", "obsessed-with-mushrooms")
    import pytest

    with pytest.raises(ValueError) as exc:
        _reimport_config({})
    assert "Unknown PROFILE" in str(exc.value)


def test_profile_explicit_env_wins_over_preset(monkeypatch):
    """Operator-specified RATE_LIMIT_SCALE beats the preset's suggestion —
    presets use setdefault, never os.environ[] =."""
    monkeypatch.setenv("PROFILE", "ci")  # normally → 100
    monkeypatch.setenv("RATE_LIMIT_SCALE", "5")  # but operator said 5
    cfg = _reimport_config({})
    assert cfg._RATE_SCALE == 5
