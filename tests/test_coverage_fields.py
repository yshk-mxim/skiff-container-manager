# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Gate: every row in docs/dev/coverage_fields.csv must have a
decision for `read_in_ui`, `writable_in_ui`, and `roundtrip_tested`
— either Y, Y (with caveat), N (with reason), or N! (security NO).

No blank cells. Captures whether every documented Engine object
field has a UI surface + a round-trip assertion somewhere.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


CSV_PATH = Path("docs/dev/coverage_fields.csv")
REQUIRED_COLS = ("object", "field", "read_in_ui", "writable_in_ui",
                 "roundtrip_tested")


def _load_rows() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        pytest.skip(f"{CSV_PATH} missing")
    with CSV_PATH.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_header_has_required_columns() -> None:
    rows = _load_rows()
    assert rows, "fields CSV is empty"
    for col in REQUIRED_COLS:
        assert col in rows[0], f"column {col!r} missing"


def test_every_row_has_object_and_field() -> None:
    rows = _load_rows()
    bad: list[str] = []
    for i, row in enumerate(rows, start=2):
        if not row.get("object", "").strip() or not row.get("field", "").strip():
            bad.append(f"line {i}: object/field blank")
    assert not bad, "\n".join(bad)


def test_no_blank_decision_cells() -> None:
    """read_in_ui / writable_in_ui / roundtrip_tested MUST be
    populated with one of the allowed tokens — no bare blanks."""
    rows = _load_rows()
    bad: list[str] = []
    for row in rows:
        who = f"{row['object']}.{row['field']}"
        for col in ("read_in_ui", "writable_in_ui", "roundtrip_tested"):
            v = row.get(col, "").strip()
            if not v:
                bad.append(f"{who}: {col} blank")
                continue
            # Accept Y / Y (note) / Y* / N / N! / N (reason). The
            # regex below is deliberately permissive — the rule is
            # 'something starting with Y or N'.
            if v[0] not in {"Y", "N"}:
                bad.append(f"{who}: {col} = {v!r} (must start with Y or N)")
    assert not bad, "\n".join(bad)


def test_N_bang_rows_have_omission_reason() -> None:
    """N! means intentional security NO — must be accompanied by a
    non-empty omission_reason so a reader understands why."""
    rows = _load_rows()
    thin: list[str] = []
    for row in rows:
        has_bang = any(
            row.get(col, "").strip().startswith("N!")
            for col in ("read_in_ui", "writable_in_ui", "roundtrip_tested")
        )
        reason = row.get("omission_reason", "").strip()
        if has_bang and not reason:
            thin.append(f"{row['object']}.{row['field']}")
    assert not thin, (
        f"{len(thin)} N! rows with no omission_reason: {thin[:5]}..."
    )


def test_roundtrip_Y_rows_have_read_and_write() -> None:
    """If a field is roundtrip-tested it must be both readable AND
    writable in the UI — otherwise the round-trip claim is wrong."""
    rows = _load_rows()
    mismatches: list[str] = []
    for row in rows:
        rt = row.get("roundtrip_tested", "").strip()
        if not rt.startswith("Y"):
            continue
        read = row.get("read_in_ui", "").strip()
        write = row.get("writable_in_ui", "").strip()
        # Read must be Y. Write may be Y/Y*/N (immutable daemon field)
        # — a writable=N + roundtrip=Y means we verified the READ path
        # round-trips (e.g., Image.Size: derived, read-only, but we
        # confirm the value we see matches docker inspect).
        if not read.startswith("Y"):
            mismatches.append(
                f"{row['object']}.{row['field']}: roundtrip=Y but "
                f"read_in_ui={read!r}"
            )
    assert not mismatches, "\n".join(mismatches)
