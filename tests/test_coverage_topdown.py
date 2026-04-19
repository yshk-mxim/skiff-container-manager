# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Gate: every row in docs/dev/coverage_topdown.csv must be non-blank
AND the referenced journey function must exist.

This is the top-down half of the coverage sweep (persona → journey
→ UI affordance → API → engine endpoint). Maintained by hand; this
test catches drift when a journey is renamed or removed without
updating the CSV.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


CSV_PATH = Path("docs/dev/coverage_topdown.csv")
REQUIRED_COLS = ("persona", "journey", "covered")


def _load_rows() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        pytest.skip(f"{CSV_PATH} missing")
    with CSV_PATH.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_header_has_required_columns() -> None:
    rows = _load_rows()
    assert rows, "topdown CSV is empty"
    for col in REQUIRED_COLS:
        assert col in rows[0], f"column {col!r} missing"


def test_every_row_is_fully_populated() -> None:
    """persona + journey + covered must all be non-blank."""
    rows = _load_rows()
    bad: list[str] = [
        f"line {i}: {col} blank in {row.get('journey')!r}"
        for i, row in enumerate(rows, start=2)  # +2 = header + 1-index
        for col in REQUIRED_COLS
        if not row.get(col, "").strip()
    ]
    assert not bad, "\n".join(bad)


def test_every_covered_flag_is_Y() -> None:  # noqa: N802 — Y is semantic
    """`covered=N` means we shipped a row for an unexercised path —
    dead weight. Remove the row OR add a journey."""
    rows = _load_rows()
    uncovered = [r for r in rows if r.get("covered", "").strip() != "Y"]
    assert not uncovered, (
        f"{len(uncovered)} rows marked covered!=Y (remove or add journey): {[r['journey'] for r in uncovered[:5]]}..."
    )


def test_every_journey_reference_exists() -> None:
    """The CSV's `journey` column must name a real pytest function
    in tests/journeys/."""
    import re as _re

    rows = _load_rows()
    # Gather every test_journey_* function declared under tests/journeys/.
    known: set[str] = set()
    for p in Path("tests/journeys").glob("test_*.py"):
        text = p.read_text(encoding="utf-8")
        for m in _re.finditer(r"^def (test_journey_\w+)", text, _re.MULTILINE):
            known.add(m.group(1))
    missing: list[str] = []
    for row in rows:
        j = row.get("journey", "").strip()
        if not j or not j.startswith("test_journey_"):
            continue
        if j not in known:
            missing.append(j)
    assert not missing, f"topdown CSV references journeys that don't exist (renamed/removed?): {missing}"


def test_every_journey_appears_at_least_once() -> None:
    """Inverse check: every declared journey function should appear
    as at least one row in the topdown CSV. Otherwise the CSV is
    lagging behind new journey work."""
    import re as _re

    rows = _load_rows()
    referenced = {r.get("journey", "").strip() for r in rows}
    declared: set[str] = set()
    for p in Path("tests/journeys").glob("test_*.py"):
        text = p.read_text(encoding="utf-8")
        for m in _re.finditer(r"^def (test_journey_\w+)", text, _re.MULTILINE):
            declared.add(m.group(1))
    missing = sorted(declared - referenced)
    assert not missing, f"{len(missing)} journeys not yet in topdown CSV: {missing[:10]}..."
