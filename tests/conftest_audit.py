# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Pytest fixtures for the persona-audit harness.

Auto-loaded by any test file that imports from `tests/journeys/` via
conftest discovery. Provides the `audit_observer` fixture and installs
the module-level observer variable used by `step(name)`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.audit_driver import (
    AuditObserver,
    current_pass_number,
    set_current_observer,
)
from tests.personas import Persona


@pytest.fixture
def audit_observer(request: pytest.FixtureRequest, persona: Persona) -> Iterator[AuditObserver]:
    """Per-test observer. Installed as the module-level current observer
    so `step(name)` finds it without plumbing. Journey names come from
    the pytest test function's name.
    """
    journey_name = request.node.originalname or request.node.name
    obs = AuditObserver(
        pass_n=current_pass_number(),
        persona=persona.tag,
        journey=journey_name,
    )
    set_current_observer(obs)
    try:
        yield obs
    finally:
        set_current_observer(None)


@pytest.fixture
def audited_page(audit_observer: AuditObserver, page):
    """Drop-in replacement for the bare `page` fixture that also
    registers the Playwright page with the audit observer so screenshot
    and console capture are automatic."""
    audit_observer.set_page(page)
    return page


def pytest_collection_modifyitems(config, items):
    """Mark every journey test with `persona_audit` so the full suite
    can be sliced via `-m persona_audit`."""
    for item in items:
        if item.fspath.strpath.rsplit("/tests/", 1)[-1].startswith("journeys/"):
            item.add_marker(pytest.mark.persona_audit)
