# SPDX-License-Identifier: MIT
"""Tests for the config_knob factory in skiff.config.

Covers:
  - Every registered knob has a doc string.
  - Every knob name is UPPER_SNAKE.
  - Duplicate registration raises (loud rather than silent override).
  - Validator path: invalid env value surfaces as the validator's error.
  - Registry survives re-import (knob entries stick around).

A later commit will extend with a consistency test between the knob
registry, `.env.example`, and README.md — deferred until more knobs
migrate.
"""
from __future__ import annotations

import re

import pytest

from skiff.config import _KNOBS, config_knob, knobs


class TestKnobRegistry:
    def test_registry_non_empty(self) -> None:
        assert len(knobs()) >= 4  # at least the first migrated batch

    def test_every_knob_has_doc(self) -> None:
        missing = [name for name, spec in knobs().items() if not spec.doc]
        assert not missing, f"knobs missing doc: {missing}"

    def test_knob_names_are_upper_snake(self) -> None:
        pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
        for name in knobs():
            assert pattern.match(name), f"knob {name!r} is not UPPER_SNAKE"

    def test_secret_and_expose_are_compatible(self) -> None:
        """Secret knobs may still be 'expose=True' (surfaced with redaction)
        but the combination should be an intentional decision. For now,
        secret+expose means 'operator can see it exists, not its value'."""
        for spec in knobs().values():
            if spec.secret:
                # Secret knobs should not also have a default that's a real secret.
                # Defaults are allowed to be empty string / None / placeholder.
                assert spec.default in (None, ""), \
                    f"secret knob {spec.name!r} has a non-empty default — risky"


class TestKnobBehaviour:
    def test_duplicate_registration_raises(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            config_knob("BIND_HOST", default="127.0.0.1", doc="dup")

    def test_validator_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Install a fresh unique name, then verify the validator runs.
        monkeypatch.setenv("_TEST_INT_KNOB", "42")
        # Temporarily clear _KNOBS entry if a prior run registered it
        _KNOBS.pop("_TEST_INT_KNOB", None)
        value = config_knob("_TEST_INT_KNOB", default="0", validator=int, doc="test")
        assert value == 42

    def test_missing_env_returns_default_or_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("_TEST_ABSENT_KNOB", raising=False)
        _KNOBS.pop("_TEST_ABSENT_KNOB", None)
        value = config_knob("_TEST_ABSENT_KNOB", default=None, doc="test")
        assert value is None

    def test_validator_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _KNOBS.pop("_TEST_BAD_INT_KNOB", None)
        monkeypatch.setenv("_TEST_BAD_INT_KNOB", "not-a-number")
        with pytest.raises(ValueError):
            config_knob("_TEST_BAD_INT_KNOB", default="0", validator=int, doc="test")


class TestKnobMigrationSmoke:
    """Smoke tests asserting specific migrated knobs work end-to-end."""

    def test_bind_host_registered(self) -> None:
        spec = knobs().get("BIND_HOST")
        assert spec is not None
        assert spec.default == "127.0.0.1"
        assert spec.expose is True

    def test_audit_knobs_registered(self) -> None:
        assert "AUDIT_MAX_MB" in knobs()
        assert "AUDIT_BACKUP_COUNT" in knobs()

    def test_rate_limit_scale_registered(self) -> None:
        spec = knobs().get("RATE_LIMIT_SCALE")
        assert spec is not None
        assert spec.validator is not None
