# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Journey catalogue harness.

Every journey file declares functions decorated with `@journey(…)`.
Pytest collection treats the decorated function as a parametrised test
— one parameterisation per persona the journey supports.

Exports:
  - `journey(persona, category, severity, covers)` — the decorator
  - `JOURNEY_REGISTRY` — discovered journeys, populated at import
  - `step(name)` — context manager for a single step (observation hook)

The harness is split across this package (catalogue + decorators) and
`tests/audit_driver.py` (observation + finding emission) so journey
files import only from here and don't need to know the observation
implementation.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.personas import ALL_PERSONAS, PERSONAS_BY_TAG, Persona


@dataclass
class JourneyMeta:
    """Metadata captured by the `@journey(...)` decorator.

    Populated at import time so `pytest --collect-only` can show the
    full catalogue without running anything."""

    func: Callable
    file: str
    name: str
    category: str
    severity: str
    personas: tuple[Persona, ...]
    covers: tuple[str, ...]
    tags: frozenset[str] = field(default_factory=frozenset)


#: All journeys discovered at import time. Keyed by dotted function name.
JOURNEY_REGISTRY: dict[str, JourneyMeta] = {}


_VALID_CATEGORIES = frozenset(
    {
        "first_run", "quick_start", "container_lifecycle", "compose",
        "volumes_networks", "files_tab", "audit_observability",
        "security_reviewer", "error_recovery", "ui_ux",
    },
)
_VALID_SEVERITIES = frozenset({"P0", "high", "medium", "low"})


def journey(
    *,
    persona: str | tuple[str, ...] = (),
    category: str,
    severity: str = "medium",
    covers: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> Callable[[Callable], Callable]:
    """Decorator that turns a function into a parametrised journey test.

    Usage:
        @journey(persona=("novice", "developer"), category="first_run",
                 severity="high", covers=("hb-dashboard-missing",))
        def journey_wizard_to_dashboard(page, live_server, persona):
            ...

    The decorated function:
      - MUST take `persona` (Persona instance) as its first keyword arg
        after the usual pytest fixtures (page / live_server / etc).
      - Runs once per tag in `persona`.
      - Collected by pytest via the `tests/journeys/` package's
        `conftest.py` plugin (installed by the persona-audit entry point).

    `covers` cross-references tests/journeys/_history.py entries so the
    historical-bug coverage gate can prove every `hb-*` has a journey.
    `tags` are free-form — used by the runner to slice the catalogue
    (e.g. `--tag=smoke`).
    """
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"Unknown category {category!r}; valid: {sorted(_VALID_CATEGORIES)}")
    if severity not in _VALID_SEVERITIES:
        raise ValueError(f"Unknown severity {severity!r}; valid: {sorted(_VALID_SEVERITIES)}")

    persona_tags: tuple[str, ...] = (
        (persona,) if isinstance(persona, str) else tuple(persona)
    )
    if not persona_tags:
        # Default: every persona runs it. Only useful for the most
        # universal journeys (e.g. "can sign in").
        persona_tags = tuple(p.tag for p in ALL_PERSONAS)

    personas = tuple(PERSONAS_BY_TAG[t] for t in persona_tags)

    def _decorate(func: Callable) -> Callable:
        meta = JourneyMeta(
            func=func,
            file=func.__module__,
            name=func.__name__,
            category=category,
            severity=severity,
            personas=personas,
            covers=tuple(covers),
            tags=frozenset(tags),
        )
        key = f"{func.__module__}.{func.__name__}"
        JOURNEY_REGISTRY[key] = meta

        @functools.wraps(func)
        @pytest.mark.parametrize(
            "persona",
            personas,
            ids=[p.tag for p in personas],
        )
        def wrapper(*args: Any, persona: Persona, **kwargs: Any) -> Any:
            return func(*args, persona=persona, **kwargs)

        # Attach the meta so pytest collection / reporters can see it.
        wrapper._journey_meta = meta  # type: ignore[attr-defined]
        return wrapper

    return _decorate


def discover_journeys() -> dict[str, JourneyMeta]:
    """Return the populated registry. Pytest's collection pass imports
    every `tests/journeys/*.py` file, triggering the `@journey` decorators
    and populating `JOURNEY_REGISTRY` as a side effect. Call this AFTER
    collection (e.g. from a pytest_collection_finish hook)."""
    return dict(JOURNEY_REGISTRY)


# `step(name)` is re-exported from `tests.audit_driver` so journey files
# can import both symbols from the same namespace.
from tests.audit_driver import step  # noqa: E402,F401  (module-level re-export)
