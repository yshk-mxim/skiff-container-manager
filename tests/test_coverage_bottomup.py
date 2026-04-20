# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Gate: every row in docs/dev/coverage_bottomup.csv must either
exercise the engine endpoint via a journey OR declare
`security_justified_no=Y`.

This is the bottom-up half of the coverage sweep (engine endpoint →
SKIFF route → UI affordance → journey). Proves no Engine API surface
is silently unsupported — every omission is documented.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


CSV_PATH = Path("docs/dev/coverage_bottomup.csv")
REQUIRED_COLS = ("engine_endpoint", "skiff_route", "exercised_by_journey", "security_justified_no")


def _load_rows() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        pytest.skip(f"{CSV_PATH} missing")
    with CSV_PATH.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_header_has_required_columns() -> None:
    rows = _load_rows()
    assert rows, "bottomup CSV is empty"
    for col in REQUIRED_COLS:
        assert col in rows[0], f"column {col!r} missing"


def test_every_row_has_endpoint() -> None:
    rows = _load_rows()
    bad = [r for r in rows if not r.get("engine_endpoint", "").strip()]
    assert not bad, f"{len(bad)} rows missing engine_endpoint"


def test_every_row_has_exerciser_or_security_no() -> None:
    """An engine endpoint with no journey AND no security_justified_no
    is an unsupported surface the tracker has no opinion about.
    That's the gap this gate closes."""
    rows = _load_rows()
    orphans: list[str] = []
    for row in rows:
        journey = row.get("exercised_by_journey", "").strip()
        sn = row.get("security_justified_no", "").strip()
        has_journey = bool(journey) and journey not in {"-", "—"}
        # Accept 'Y', 'Y — reason', 'Y*', etc. (plan format lets the
        # reason live in the column itself).
        has_security_no = sn.upper().startswith("Y")
        if not has_journey and not has_security_no:
            orphans.append(row.get("engine_endpoint", "<blank>"))
    assert not orphans, (
        f"{len(orphans)} engine endpoints with neither journey nor security_justified_no: {orphans[:5]}..."
    )


def test_referenced_journeys_exist() -> None:
    """Every exercised_by_journey entry must name an actual test
    function — matches the topdown gate."""
    import re as _re

    rows = _load_rows()
    known: set[str] = set()
    for p in Path("tests/journeys").glob("test_*.py"):
        text = p.read_text(encoding="utf-8")
        for m in _re.finditer(r"^def (test_journey_\w+)", text, _re.MULTILINE):
            known.add(m.group(1))
    missing: list[str] = []
    for row in rows:
        j = row.get("exercised_by_journey", "").strip()
        if not j or j in {"-", "—"}:
            continue
        # Allow comma-separated list.
        for ref in [x.strip() for x in j.split(",")]:
            if not ref:
                continue
            if ref not in known:
                missing.append(f"{row['engine_endpoint']}: {ref!r}")
    assert not missing, f"bottomup CSV references non-existent journeys: {missing[:10]}..."


def test_security_justified_no_rows_have_reason() -> None:
    """A `Y` in security_justified_no must come with a non-blank
    reason — the column's value is the reason text. Bare `Y` with
    no explanation is dead weight and blocks audit review."""
    rows = _load_rows()
    thin: list[str] = []
    for row in rows:
        sn = row.get("security_justified_no", "").strip()
        if not sn.upper().startswith("Y"):
            continue
        # Real rows use 'Y — …' or 'Y* — …'. A bare 'Y' carries no
        # reason.
        trailing = sn[1:].strip().lstrip("*").strip().lstrip("—").strip("-").strip()
        if not trailing:
            thin.append(row["engine_endpoint"])
    assert not thin, (
        f"{len(thin)} rows have bare `Y` in security_justified_no with no "
        f"reason: {thin[:5]}. Add a ' — <reason>' suffix explaining the "
        f"intentional omission."
    )
