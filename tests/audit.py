# SPDX-License-Identifier: MIT
"""Audit-event assertion helper for tests.

Usage:
    def test_container_started_emits_audit(log_capture):
        # ... trigger an action ...
        assert_audit_event(log_capture, "container.started", id="abc123")

The helper:
  1. Verifies the event appears in log_capture.entries.
  2. Verifies every required field from skiff.contract.events is present.
  3. If `**fields` kwargs are passed, asserts they appear with the given
     values (partial match).
  4. Produces a clear failure message that lists what WAS captured when
     the assertion fails — saves minutes of "why didn't this log?".

A companion `log_capture` fixture in this file is imported by tests that
want the canonical shape.
"""
from __future__ import annotations

from typing import Any

import pytest
import structlog

from skiff.contract.events import known_events, required_fields


@pytest.fixture
def log_capture():
    """Replace structlog's processor chain with LogCapture for the test.

    Restores the original chain on teardown. Yields the capture object;
    entries are available on `.entries`.
    """
    cap = structlog.testing.LogCapture()
    original = structlog.get_config()["processors"]
    structlog.configure(processors=[cap])
    try:
        yield cap
    finally:
        structlog.configure(processors=original)


def assert_audit_event(
    log_capture: structlog.testing.LogCapture,
    name: str,
    **expected_fields: Any,
) -> dict[str, Any]:
    """Assert that `name` was emitted, with every required field plus
    any `expected_fields` passed as kwargs.

    Returns the matching entry for optional further assertions.

    Raises AssertionError with a useful diff when:
      - The event wasn't emitted.
      - A required field from the catalogue is missing.
      - An expected field value doesn't match.
    """
    if name not in known_events():
        raise AssertionError(
            f"Test asserts on audit event {name!r} but it's not in the "
            f"catalogue. Add it to skiff/contract/events.py first.",
        )
    matches = [e for e in log_capture.entries if e.get("event") == name]
    if not matches:
        emitted = sorted({e.get("event") for e in log_capture.entries})
        raise AssertionError(
            f"Expected audit event {name!r}, but it wasn't emitted.\n"
            f"Events captured: {emitted}",
        )
    # Use the most recent match
    entry = matches[-1]
    # Every catalogue-declared required field must be present
    missing_required = [f for f in required_fields(name) if f not in entry]
    if missing_required:
        raise AssertionError(
            f"Audit event {name!r} missing catalogue-required fields: "
            f"{missing_required}. Entry: {entry}",
        )
    # Any expected_fields kwargs must match exactly
    mismatches: list[str] = []
    for key, want in expected_fields.items():
        if key not in entry:
            mismatches.append(f"  {key}: <missing> (expected {want!r})")
        elif entry[key] != want:
            mismatches.append(f"  {key}: got {entry[key]!r} (expected {want!r})")
    if mismatches:
        raise AssertionError(
            f"Audit event {name!r} has mismatched fields:\n"
            + "\n".join(mismatches)
            + f"\nFull entry: {entry}",
        )
    return entry


__all__ = ["assert_audit_event", "log_capture"]
