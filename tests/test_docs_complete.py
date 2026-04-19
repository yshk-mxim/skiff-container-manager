# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Documentation completeness gates.

Extends `test_docs_sync.py` (if present) with stricter invariants. Each
gate is a unit test that reads the docs alongside the source of truth
and fails on drift.

Gates:
  * Every route registered in the app has a `description` in the
    generated OpenAPI schema.
  * Every error code catalogued in `skiff.contract.errors.known_codes()`
    has an entry in docs/errors.md (or docs/api-reference.md).
  * Every `config_knob` has an entry in docs/config-knobs.md.
  * Every structured audit event in `skiff.contract.events.EVENT_CATALOGUE`
    has an entry in docs/audit-events.md.
  * Every historical-bug ID in `tests/journeys/_history.py` has an
    entry in CHANGELOG.md (introduced after the fix-commit column is
    filled — skip with xfail until every hb row has a commit).
  * Every page in the sidebar has a README pointer.
  * Every screenshot referenced from README exists.
  * Every ⌘K palette entry is backed by a real page or route.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


DOCS_DIR = Path("docs")
README = Path("README.md")


# ── Gate 1: every route has an OpenAPI description ─────────────────────


def test_every_route_has_openapi_description():
    """A route without a description in the OpenAPI schema is invisible
    to super-users generating client SDKs from the spec. Every path
    in `app.routes` must contribute a `description` field — FastAPI
    picks it up from the handler's docstring."""
    from skiff.app import app

    schema = app.openapi()
    missing: list[str] = []
    for path, ops in schema.get("paths", {}).items():
        for method, op in ops.items():
            if method in {"parameters", "servers"}:
                continue
            if not isinstance(op, dict):
                continue
            # Either description OR summary is acceptable — FastAPI
            # fills one of them from the first line of the docstring.
            if not (op.get("description") or op.get("summary")):
                missing.append(f"{method.upper()} {path}")
    assert not missing, (
        f"Routes without OpenAPI description/summary (add a docstring to "
        f"the handler function): {missing!r}"
    )


# ── Gate 2: every error code documented ─────────────────────────────────


def test_every_error_code_documented():
    """Every code in known_codes() must appear in docs/errors.md OR
    docs/api-reference.md. Missing a code means a caller getting that
    code has no way to look up what it means."""
    from skiff.contract.errors import known_codes

    docs_text = ""
    for candidate in (DOCS_DIR / "errors.md", DOCS_DIR / "api-reference.md"):
        if candidate.exists():
            docs_text += candidate.read_text(encoding="utf-8")

    missing = [c for c in sorted(known_codes()) if c not in docs_text]
    assert not missing, (
        f"Error codes not documented: {missing!r}. Add to docs/errors.md "
        f"(or ensure the generator tools/gen_catalogues.py has been run)."
    )


# ── Gate 3: every config knob documented ────────────────────────────────


def test_every_knob_documented():
    """Every exposed config_knob must have an entry in
    docs/config-knobs.md. A knob that can affect production behaviour
    but isn't documented is an operational risk."""
    from skiff.config import knobs

    knobs_md = (DOCS_DIR / "config-knobs.md")
    if not knobs_md.exists():
        pytest.skip("docs/config-knobs.md missing — run `make docs` to generate")
    text = knobs_md.read_text(encoding="utf-8")
    missing: list[str] = []
    for name, spec in knobs().items():
        if not spec.expose:
            continue
        if f"`{name}`" not in text and name not in text:
            missing.append(name)
    assert not missing, (
        f"Knobs not documented: {missing!r}. Run `make docs` to regenerate "
        f"docs/config-knobs.md from the source."
    )


# ── Gate 4: every audit event documented ───────────────────────────────


def test_every_audit_event_documented():
    """Every key in the event catalogue must appear in docs/audit-events.md.
    A SIEM pipeline keyed on event_type can't triage an undocumented
    event — the docs are the event schema."""
    from skiff.contract.events import _EVENTS

    events_md = DOCS_DIR / "audit-events.md"
    if not events_md.exists():
        pytest.skip("docs/audit-events.md missing — run `make docs` to generate")
    text = events_md.read_text(encoding="utf-8")
    missing = [k for k in sorted(_EVENTS.keys()) if k not in text]
    assert not missing, (
        f"Audit events not documented: {missing!r}. Run `make docs` to "
        f"regenerate docs/audit-events.md."
    )


# ── Gate 5: every screenshot referenced from README exists ──────────────


def test_every_screenshot_reference_exists():
    """Broken screenshot links in README are an immediate credibility
    hit for a new visitor. Every `![...](path)` in README resolves to
    an existing file."""
    if not README.exists():
        pytest.skip("README.md missing")
    text = README.read_text(encoding="utf-8")
    # Match image references — [alt](path). Simple regex; no nested brackets.
    broken: list[str] = []
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
        ref = m.group(1).strip()
        # Skip external URLs; we only check local paths.
        if ref.startswith(("http://", "https://", "data:")):
            continue
        # Relative to README location.
        full = (README.parent / ref).resolve()
        if not full.exists():
            broken.append(ref)
    assert not broken, f"README references missing images: {broken!r}"


# ── Gate 6: every sidebar page has a README pointer ─────────────────────


def test_every_sidebar_page_has_readme_pointer():
    """README's Features section should mention every page in the
    sidebar (Dashboard / Containers / Images / Templates / Volumes /
    Networks / Compose / System). A page the README doesn't know
    about is discoverable only by clicking — not good for SEO or
    first-time visitors."""
    if not README.exists():
        pytest.skip("README.md missing")
    text = README.read_text(encoding="utf-8").lower()
    sidebar_pages = ("dashboard", "containers", "images", "templates",
                     "volumes", "networks", "compose", "system")
    missing = [p for p in sidebar_pages if p not in text]
    assert not missing, (
        f"README doesn't mention sidebar pages: {missing!r}. Add to the "
        f"Features section with a short description + screenshot pointer."
    )


# ── Gate 7: historical bugs have CHANGELOG entries ─────────────────────


def test_every_historical_bug_in_changelog():
    """CHANGELOG.md should enumerate every `hb-*` id with the fix commit
    short-hash so users can cross-reference a report to an upgrade."""
    from tests.journeys._history import HISTORICAL_BUGS

    changelog = Path("CHANGELOG.md")
    if not changelog.exists():
        pytest.skip("CHANGELOG.md missing")
    text = changelog.read_text(encoding="utf-8")
    missing = [hb.id for hb in HISTORICAL_BUGS if hb.id not in text]
    assert not missing, missing


# ── Gate 8: every persona's done-rubric goal has a UI reachability ─────


def test_every_persona_done_rubric_has_reachability():
    """Each persona's done_rubric goal should be reachable through the
    UI from wizard-exit using only sidebar + in-page affordances.
    Enforced via the journey catalogue: every persona that has a
    done_rubric must appear in at least one `@journey(persona=...)`
    decoration, proving the harness drives them at least once.
    """
    import importlib
    import pathlib as _pathlib

    for p in _pathlib.Path("tests/journeys").glob("test_*.py"):
        importlib.import_module(f"tests.journeys.{p.stem}")

    from tests.journeys import discover_journeys
    from tests.personas import ALL_PERSONAS

    personas_in_journeys: set[str] = set()
    for meta in discover_journeys().values():
        for pp in meta.personas:
            personas_in_journeys.add(pp.tag)

    for p in ALL_PERSONAS:
        assert p.done_rubric, f"persona {p.tag} has empty done_rubric"
        assert p.tag in personas_in_journeys, (
            f"persona {p.tag} has a done_rubric but no journey drives it"
        )
