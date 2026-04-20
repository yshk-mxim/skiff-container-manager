# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Capability gate against docs/dev/capability_matrix.csv.

For every row:
  * `SKIFF=Y` with a `skiff_route` field → the route must be registered
    in the app. Catches claims that drift when a route is renamed or
    removed.
  * `SKIFF=N` → must have either (a) `security_NO_reason` populated
    (intentional `N!` row), OR (b) `expectation=Y` (gap-finding — the
    test lists them so the iteration loop can address or reclassify).
  * `last_verified` date must be within 90 days.

The matrix compares SKIFF against an anonymised industry baseline via
a single `expectation` column (Y means "this capability is common in
tools of this class"). Running this test identifies drift between
claimed and actual behaviour without any manual review.
"""

from __future__ import annotations

import csv
import datetime as _dt
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


MATRIX_CSV = Path("docs/dev/capability_matrix.csv")
FRESHNESS_DAYS = 90
REQUIRED_COLUMNS = ("capability", "SKIFF", "expectation", "last_verified")


def _load_rows() -> list[dict[str, str]]:
    if not MATRIX_CSV.exists():
        pytest.skip(f"{MATRIX_CSV} missing")
    with MATRIX_CSV.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _normalise_path(path: str) -> str:
    """Collapse FastAPI path params to a generic `{param}` so a matrix
    claim like `/api/containers/{id}/start` matches a registered path
    `/api/containers/{container_id}/start`."""
    return re.sub(r"\{[^}]+\}", "{param}", path)


def _registered_routes() -> dict[tuple[str, str], bool]:
    """Return a lookup of (METHOD, normalised_path) → True for every
    registered route in the FastAPI app."""
    from starlette.routing import Route

    from skiff.app import app

    out: dict[tuple[str, str], bool] = {}
    for r in app.routes:
        if isinstance(r, Route):
            norm = _normalise_path(r.path)
            for m in r.methods or ():
                out[(m, norm)] = True
    return out


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    return _load_rows()


def test_matrix_is_non_empty_and_wellformed(rows):
    """Sanity: CSV has headers, no blank capability cells, no dup rows,
    every row has the required columns."""
    assert rows, "matrix is empty"
    seen: set[str] = set()
    for row in rows:
        cap = row.get("capability", "").strip()
        assert cap, f"row missing capability: {row}"
        assert cap not in seen, f"duplicate capability row: {cap!r}"
        seen.add(cap)
        for col in REQUIRED_COLUMNS:
            assert col in row, f"{cap}: missing column {col!r}"


def test_every_skiff_y_row_has_a_registered_route(rows):
    """If a row says SKIFF=Y and names a `skiff_route`, that route must
    actually be registered in the app. Catches renamed / removed routes
    that leave stale matrix claims."""
    registered = _registered_routes()
    bad: list[str] = []
    for row in rows:
        if row.get("SKIFF", "").strip() != "Y":
            continue
        route_claim = row.get("skiff_route", "").strip()
        if not route_claim:
            continue
        # Format: "METHOD /path" — allow multiple via + separator.
        for raw_part in re.split(r"\s*\+\s*|\s*/then/\s*", route_claim):
            part = raw_part.strip()
            if not part:
                continue
            m = re.match(r"(GET|POST|PUT|DELETE|WS)\s+(/\S+)", part)
            if not m:
                continue  # non-routable claim like "Export JSON button"
            method, path = m.group(1), m.group(2)
            # WS routes live on a different attribute; skip for simplicity.
            if method == "WS":
                continue
            base_path = _normalise_path(path.split("?", 1)[0])
            if (method, base_path) not in registered:
                bad.append(f"{row['capability']}: claims SKIFF=Y on {method} {base_path} but route not in app.routes")
    assert not bad, "\n".join(bad)


def test_every_skiff_n_row_has_a_reason_or_is_a_finding(rows):
    """A SKIFF=N row is either:
      - intentional (`security_NO_reason` populated), OR
      - a gap finding (`expectation=Y` — industry-standard feature).
    A SKIFF=N row with no reason AND `expectation=N` is dead weight
    (remove the row)."""
    dead: list[str] = []
    for row in rows:
        if row.get("SKIFF", "").strip() != "N":
            continue
        has_reason = bool(row.get("security_NO_reason", "").strip())
        is_expected = row.get("expectation", "").strip().upper().startswith("Y")
        if not has_reason and not is_expected:
            dead.append(row["capability"])
    assert not dead, f"SKIFF=N rows with no rationale and expectation=N (remove from matrix or add a reason): {dead}"


def test_matrix_freshness_90_days(rows):
    """Rows must be re-verified at least every 90 days so a stale matrix
    doesn't linger. `last_verified: YYYY-MM-DD` per row."""
    today = _dt.datetime.now(tz=_dt.UTC).date()
    cutoff = today - _dt.timedelta(days=FRESHNESS_DAYS)
    stale: list[str] = []
    for row in rows:
        date_str = row.get("last_verified", "").strip()
        if not date_str:
            stale.append(f"{row['capability']}: no last_verified date")
            continue
        try:
            d = _dt.date.fromisoformat(date_str)
        except ValueError:
            stale.append(f"{row['capability']}: bad date {date_str!r}")
            continue
        if d < cutoff:
            stale.append(f"{row['capability']}: last_verified {date_str} > 90 days ago")
    assert not stale, "\n".join(stale)


def test_gap_findings_surface_openwork(rows):
    """Summarise rows where SKIFF=N, expectation=Y, and no
    security_NO_reason. These aren't test failures in themselves (the
    previous test enforces the no-dead-row invariant); this test
    records them for the tracker + iteration-loop backlog."""
    findings: list[str] = []
    for row in rows:
        if row.get("SKIFF", "").strip() != "N":
            continue
        if row.get("security_NO_reason", "").strip():
            continue
        if row.get("expectation", "").strip().upper().startswith("Y"):
            findings.append(f"  - {row['capability']}")
    if findings:
        # Informational test — tracks gaps without blocking CI while the
        # iteration loop works them down.
        print("\nOpen capability gaps (SKIFF=N, expectation=Y, no reason):")
        print("\n".join(findings))
