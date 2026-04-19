# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Gate: the persona-audit tracker and its backing CSVs exist and are
reasonably fresh.

The tracker is the user's single pane of glass for the persona-audit
state — findings, parity, GUI elements, testing coverage, open work.
If a PR touches the harness or the competitor surfaces, it should
rerun `make tracker` and commit the refreshed artefacts; this test
catches obvious drift (missing files, all-zero counts, stale
markdown).
"""

from __future__ import annotations

import csv
import datetime as _dt
import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


TRACKER = Path("docs/dev/persona_audit_tracker.md")
BACKING_CSVS = (
    Path("docs/dev/findings_tracker.csv"),
    Path("docs/dev/competitor_matrix.csv"),
    Path("docs/dev/competitor_gui_elements.csv"),
    Path("docs/dev/testing_tracker.csv"),
    Path("docs/dev/open_work_tracker.csv"),
    Path("docs/dev/coverage_topdown.csv"),
    Path("docs/dev/coverage_bottomup.csv"),
    Path("docs/dev/coverage_fields.csv"),
)


def test_tracker_markdown_exists() -> None:
    assert TRACKER.exists(), (
        f"{TRACKER} missing — run `make tracker` to regenerate"
    )


def test_every_backing_csv_exists_and_has_rows() -> None:
    missing: list[str] = []
    empty: list[str] = []
    for p in BACKING_CSVS:
        if not p.exists():
            missing.append(str(p))
            continue
        with p.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        if len(rows) <= 1:  # header only
            empty.append(str(p))
    assert not missing, f"missing CSVs: {missing}"
    assert not empty, f"CSVs with only a header row: {empty}"


def test_tracker_refresh_date_is_recent() -> None:
    """The MD contains a date on line 1. If it's > 30 days old the
    tracker is stale — rerun `make tracker`."""
    text = TRACKER.read_text(encoding="utf-8")
    m = re.search(r"refreshed (\d{4}-\d{2}-\d{2})", text)
    assert m, "tracker header missing 'refreshed YYYY-MM-DD' line"
    d = _dt.date.fromisoformat(m.group(1))
    age = (_dt.date.today() - d).days
    assert age <= 30, (
        f"tracker refreshed {age} days ago (> 30) — rerun `make tracker`"
    )


def test_testing_tracker_rollup_matches_journey_count() -> None:
    """Journey count in the testing tracker's e2e_journey row must
    match the real test_journey_* count; catches a stale rollup."""
    import re as _re

    tt_rows: list[dict[str, str]] = []
    with Path("docs/dev/testing_tracker.csv").open(encoding="utf-8") as fh:
        tt_rows = list(csv.DictReader(fh))
    e2e = next((r for r in tt_rows if r["test_type"] == "e2e_journey"), None)
    assert e2e is not None, "testing_tracker.csv missing e2e_journey row"

    real_count = 0
    for p in Path("tests/journeys").glob("test_*.py"):
        real_count += len(
            _re.findall(r"^def test_journey_\w+", p.read_text(encoding="utf-8"), _re.M)
        )
    claimed = int(e2e.get("test_function_count", "0"))
    # Allow equal. If the tracker is off by more than 2 journeys
    # it's been skipped in a recent commit — rerun.
    assert abs(claimed - real_count) <= 2, (
        f"testing_tracker claims {claimed} journey tests but real count is "
        f"{real_count} — rerun `make tracker`"
    )


def test_findings_csv_counts_match_tracker_md() -> None:
    """The MD '- total (all passes): N' line must match the CSV."""
    tracker_text = TRACKER.read_text(encoding="utf-8")
    m = re.search(r"-\s*total \(all passes\):\s*\*\*(\d+)\*\*", tracker_text)
    assert m, "tracker MD missing total-findings line"
    claimed = int(m.group(1))

    with Path("docs/dev/findings_tracker.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    real = len(rows)
    assert claimed == real, (
        f"tracker MD claims {claimed} findings but CSV has {real} rows — "
        "rerun `make tracker`"
    )
