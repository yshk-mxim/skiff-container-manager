# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for R2a/R30/R31/R32 TOML-backed config data.

The TOML tree at `config/*.toml` is the single source of truth for
rate tiers, profile presets, tmpfs defaults, SSH error patterns,
security headers, Docker-network policy, and Docker socket probe
paths. These tests check:

  1. The contract surface (which tiers / profiles / paths the code
     depends on) still matches what the TOML supplies.
  2. A CONFIG_DIR override works end-to-end — operators can swap the
     tree wholesale, tests can point it at a fixture.
  3. A missing required TOML raises loudly instead of silently
     drifting to embedded Python defaults (no such defaults exist).
"""
from __future__ import annotations

import importlib
import pathlib
import shutil
import textwrap

import pytest


def _rebind_cfg(cfg_mod):
    """Rebind `_cfg` on every module that captured it at import time.

    Module-level `_cfg` references snapshot the singleton at import,
    so an importlib.reload of skiff.config leaves stale references
    elsewhere. Rebinding keeps fixtures hermetic.
    """
    import skiff.app as app_mod
    import skiff.auth as auth_mod
    import skiff.docker_client as dc_mod
    import skiff.validators as val_mod
    for m in (app_mod, auth_mod, dc_mod, val_mod):
        if hasattr(m, "_cfg"):
            m._cfg = cfg_mod._cfg


def test_rate_toml_surface():
    """skiff/_config/rate.toml supplies every tier the router decorators pick from."""
    from skiff.rate import _TIER_SPECS
    assert {"AUTH_SENSITIVE", "WRITE", "READ", "PUBLIC", "BURST"} <= set(_TIER_SPECS)
    for name, spec in _TIER_SPECS.items():
        assert "/" in spec, f"{name}: malformed spec {spec!r}"


def test_profiles_toml_surface():
    """Every persona preset is a dict of str→str env overrides."""
    from skiff.config import _PROFILE_PRESETS
    assert {"homelab", "dev", "sre", "reviewer", "tutor", "ci"} <= set(_PROFILE_PRESETS)
    for name, overrides in _PROFILE_PRESETS.items():
        assert isinstance(overrides, dict), f"{name} preset is not a dict"
        for k, v in overrides.items():
            assert isinstance(k, str) and isinstance(v, str), f"{name}.{k}={v!r}"


def test_tmpfs_toml_surface():
    """Default tmpfs mounts cover the canonical runtime directories."""
    from skiff.config import DEFAULT_TMPFS
    assert {"/tmp", "/run", "/var/run", "/var/cache"} <= set(DEFAULT_TMPFS)
    for path, opts in DEFAULT_TMPFS.items():
        assert opts.startswith("rw"), f"{path}: options should start with 'rw', got {opts!r}"


def test_security_headers_toml_surface():
    """CSP / Permissions-Policy / HSTS come from the TOML."""
    from skiff.config import _CSP, _PERMISSIONS_POLICY, HSTS_MAX_AGE
    assert "default-src 'self'" in _CSP
    assert "camera=()" in _PERMISSIONS_POLICY
    assert HSTS_MAX_AGE >= 31536000  # ≥1y (NIST/mozilla guidance)


def test_networks_toml_surface():
    """Builtin + valid-driver policy come from the TOML."""
    from skiff.routers.networks import _BUILTIN_NETWORKS, _VALID_DRIVERS
    assert {"bridge", "host", "none"} <= _BUILTIN_NETWORKS
    assert "bridge" in _VALID_DRIVERS


def test_docker_probe_toml_surface():
    """Probe-path list comes from the TOML (non-empty, Linux socket first)."""
    from skiff.routers.setup import _DEFAULT_PROBE_PATHS
    assert _DEFAULT_PROBE_PATHS, "probe list must be non-empty"
    assert _DEFAULT_PROBE_PATHS[0] == "/var/run/docker.sock"


def test_mount_targets_toml_surface():
    """Bind / tmpfs blocked-path lists load and cover the critical sandbox escapes."""
    from skiff.validators import _BLOCKED_MOUNT_TARGETS, _TMPFS_BLOCKED_TARGETS
    # Host-state exposure — bind mounts must refuse these
    for p in ("/etc", "/proc", "/sys", "/dev"):
        assert p in _BLOCKED_MOUNT_TARGETS, f"bind block missing {p}"
    # tmpfs overlay would mask container OS state on these
    for p in ("/", "/etc", "/bin", "/sbin", "/usr", "/lib"):
        assert p in _TMPFS_BLOCKED_TARGETS, f"tmpfs block missing {p}"


def test_compose_sandbox_toml_surface():
    """Compose sandbox lists all come from TOML and block the critical sandbox-escape keys."""
    from skiff.validators import (
        BLOCKED_COMPOSE_TOP_KEYS,
        BLOCKED_IPC_MODES,
        BLOCKED_NETWORK_MODES,
        BLOCKED_PRESENCE_KEYS,
        BLOCKED_TRUTHY_KEYS,
    )
    # Escape primitives that must always be blocked
    assert "privileged" in BLOCKED_PRESENCE_KEYS
    assert "cap_add" in BLOCKED_TRUTHY_KEYS
    assert "host" in BLOCKED_NETWORK_MODES
    assert "host" in BLOCKED_IPC_MODES
    assert "secrets" in BLOCKED_COMPOSE_TOP_KEYS


def test_ssh_tunnel_toml_surface():
    """SSH ControlMaster flag set is loaded and covers the P0 security flags."""
    from skiff.config import _TOML_SSH_TUNNEL
    static = _TOML_SSH_TUNNEL["static"]
    # Zero-trust required flags
    assert static["BatchMode"] == "yes", "BatchMode must stay 'yes' — see skiff/_config/ssh_tunnel.toml rationale"
    assert static["StrictHostKeyChecking"] in ("yes", "accept-new")
    assert "ControlPersist" in static
    # Dynamic flags must resolve to real skiff.config attributes
    import skiff.config as cfg
    for attr in _TOML_SSH_TUNNEL["dynamic"].values():
        assert hasattr(cfg, attr), f"dynamic SSH flag references missing config attr {attr!r}"


def test_known_tiers_introspection():
    from skiff.rate import RATE, known_tiers
    tiers = known_tiers()
    assert {"AUTH_SENSITIVE", "WRITE", "READ", "PUBLIC", "BURST"} <= tiers
    for tier_name in tiers:
        value = getattr(RATE, tier_name)
        count, _, period = value.partition("/")
        assert count.isdigit()
        assert period in {"second", "minute", "hour"}


@pytest.fixture
def _custom_config_dir(tmp_path, monkeypatch):
    """Point CONFIG_DIR at a writeable tmp dir seeded with the shipped TOMLs.

    Tests then overwrite individual files to assert overrides; everything
    else inherits from the real tree so required files are present.
    Teardown reloads skiff.config + skiff.rate and rebinds captured _cfg
    references, matching the dance in test_coverage_state_root.
    """
    src = pathlib.Path(__file__).resolve().parent.parent / "skiff" / "_config"
    for t in src.glob("*.toml"):
        shutil.copy2(t, tmp_path / t.name)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield tmp_path
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    import skiff.config as cfg_mod
    importlib.reload(cfg_mod)
    import skiff.rate as rate_mod
    importlib.reload(rate_mod)
    _rebind_cfg(cfg_mod)


def test_custom_rate_toml_takes_effect(_custom_config_dir):
    """Overwriting rate.toml in the seeded CONFIG_DIR changes RATE values."""
    (_custom_config_dir / "rate.toml").write_text(textwrap.dedent("""
        [tiers]
        AUTH_SENSITIVE = "5/minute"
        WRITE          = "99/minute"
        READ           = "60/minute"
        PUBLIC         = "120/minute"
        BURST          = "10/minute"
    """).strip(), encoding="utf-8")
    import skiff.config as cfg_mod
    importlib.reload(cfg_mod)
    import skiff.rate as rate_mod
    importlib.reload(rate_mod)
    assert rate_mod.RATE.WRITE.startswith("99/")
    assert rate_mod.RATE.READ.startswith("60/")


def test_missing_required_toml_raises(tmp_path, monkeypatch):
    """An empty CONFIG_DIR fails fast — no silent fallback to Python defaults."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    import skiff.config as cfg_mod
    try:
        with pytest.raises(FileNotFoundError, match="Required config file"):
            importlib.reload(cfg_mod)
    finally:
        monkeypatch.delenv("CONFIG_DIR", raising=False)
        importlib.reload(cfg_mod)
        _rebind_cfg(cfg_mod)


def test_config_dir_path_override(_custom_config_dir):
    """CONFIG_DIR env var moves the TOML search path."""
    import skiff.config as cfg_mod
    importlib.reload(cfg_mod)
    assert pathlib.Path(_custom_config_dir) == cfg_mod._CONFIG_DIR
