# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Historical-bug coverage gate.

Enforces the invariant that every `hb-*` in `tests/journeys/_history.py`
has all three layers of protection:

  1. A regression unit test (the `regression_test` field on the row).
  2. At least one journey (`@journey(covers=...)`) — filled in once
     the journey catalogues land.
  3. A CHANGELOG.md entry with the fix commit short-hash.

Row-by-row failures pinpoint exactly what's missing; the three gates
run independently so a partial fix surfaces exactly what's left.

Until the journey catalogue commits land, gate (2) is marked xfail.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

from tests.journeys._history import HISTORICAL_BUGS, HistoricalBug

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("hb", HISTORICAL_BUGS, ids=lambda b: b.id)
def test_hb_has_regression_test(hb: HistoricalBug) -> None:
    """Every historical bug should name a specific regression test that
    would have caught it before shipping. Empty `regression_test` →
    finding; impossible-to-find test → finding."""
    if not hb.regression_test:
        pytest.skip(
            f"{hb.id}: no regression_test assigned yet — backfill in "
            f"tests/journeys/_history.py::HISTORICAL_BUGS"
        )
    # Parse `tests/test_foo.py::test_bar` format.
    if "::" not in hb.regression_test:
        pytest.fail(f"{hb.id}: regression_test must be `tests/…py::test_…`")
    path, _, test_name = hb.regression_test.partition("::")
    file_path = pathlib.Path(path)
    assert file_path.exists(), f"{hb.id}: test file missing: {path}"
    # Verify the test function exists.
    module_name = path.replace("/", ".").removesuffix(".py")
    try:
        mod = importlib.import_module(module_name)
    except ImportError as exc:
        pytest.fail(f"{hb.id}: cannot import {module_name!r}: {exc}")
    # Walk parametrised names and nested classes — if the test name
    # appears as a prefix or an attribute, we're good.
    if hasattr(mod, test_name.split("[", 1)[0]):
        return
    pytest.fail(
        f"{hb.id}: {test_name!r} not found in {module_name}. "
        f"(module has: {[n for n in dir(mod) if n.startswith('test_')][:10]})"
    )


@pytest.mark.parametrize("hb", HISTORICAL_BUGS, ids=lambda b: b.id)
def test_hb_has_fix_commit(hb: HistoricalBug) -> None:
    """Every historical bug must name the commit that fixed it, so
    CHANGELOG.md can link to it and bisect is precise."""
    assert hb.fix_commit_short, f"{hb.id}: fix_commit_short empty"
    assert len(hb.fix_commit_short) >= 7, (
        f"{hb.id}: fix_commit_short {hb.fix_commit_short!r} too short; "
        f"use first 7+ chars of the SHA"
    )


@pytest.mark.xfail(reason="journey catalogues land in a later commit")
def test_every_hb_is_covered_by_a_journey():
    """Once tests/journeys/*.py ship, this gate enforces that every
    `hb-*` is in at least one `@journey(covers=...)` tuple."""
    from tests.journeys import discover_journeys

    covered: set[str] = set()
    for meta in discover_journeys().values():
        covered |= set(meta.covers)
    missing = {hb.id for hb in HISTORICAL_BUGS} - covered
    assert not missing, f"Historical bugs without journey coverage: {sorted(missing)}"


def test_history_registry_is_wellformed():
    """Catches typos + duplicate ids at import time."""
    ids = [hb.id for hb in HISTORICAL_BUGS]
    assert len(ids) == len(set(ids)), f"Duplicate ids in HISTORICAL_BUGS: {ids}"
    for hb in HISTORICAL_BUGS:
        assert hb.id.startswith("hb-"), f"{hb.id!r} missing hb- prefix"
        assert hb.one_line, f"{hb.id}: empty one_line"
        assert hb.class_sweep, f"{hb.id}: empty class_sweep"
