#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Verify the ASVS v5.0 self-assessment in `SECURITY.md` is complete.

OWASP ASVS v5.0 has 18 top-level chapters (V1-V18). `SECURITY.md`
promises a posture row for each chapter that's applicable to SKIFF
(everything except V9 self-contained tokens, V10 OAuth issuance, V17
WebRTC, V18 mobile — those are marked N/A but still listed so the
table's completeness is observable). When a chapter is missing, the
self-assessment silently drifts as ASVS evolves.

This script parses the ASVS mapping table in SECURITY.md and fails if
any of the 18 `V<N>` chapters is absent. It does NOT validate the
posture text — that's a human-review judgement — only that every
chapter is present in the table. Think of it as the same class of
check as `gen_catalogues.py --check`: a declarative-surface
completeness gate, not a substantive audit.

Usage:
    python3 tools/check_asvs_coverage.py           # human-readable
    python3 tools/check_asvs_coverage.py --check   # CI mode, exit 1 on gap

Exit codes: 0 complete, 1 missing rows.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECURITY_MD = ROOT / "SECURITY.md"

# ASVS v5.0 chapter roster. Kept as a module-level tuple so a future
# ASVS 5.1 / 6.0 bump is one edit here + one docs edit. See:
# https://github.com/OWASP/ASVS/tree/v5.0.0/5.0
ASVS_V5_CHAPTERS: tuple[str, ...] = tuple(f"V{n}" for n in range(1, 19))

# Match a table row like `| V3 Web Frontend Security | ... | ... |`.
# The chapter identifier must appear at the start of a cell (after the
# leading pipe + optional whitespace) so we don't match incidental `V3`
# references in prose.
_ROW_RE = re.compile(r"^\|\s*(V\d{1,2})\s", re.MULTILINE)


def _read_security_md() -> str:
    try:
        return SECURITY_MD.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {SECURITY_MD}: {exc}") from exc


def _found_chapters(text: str) -> set[str]:
    return {m.group(1) for m in _ROW_RE.finditer(text)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="CI mode: exit 1 if any chapter is missing (default: print only).",
    )
    args = parser.parse_args()

    text = _read_security_md()
    found = _found_chapters(text)
    required = set(ASVS_V5_CHAPTERS)
    missing = required - found
    unexpected = found - required

    if missing or unexpected:
        if missing:
            print(
                "SECURITY.md ASVS v5.0 mapping is missing rows for: "
                + ", ".join(sorted(missing, key=lambda s: int(s[1:]))),
                file=sys.stderr,
            )
        if unexpected:
            print(
                "SECURITY.md ASVS table has unexpected chapter ids "
                "(ASVS v5.0 only declares V1-V18): "
                + ", ".join(sorted(unexpected, key=lambda s: int(s[1:]))),
                file=sys.stderr,
            )
        return 1 if args.check else 0

    print(f"ok: ASVS v5.0 self-assessment covers all {len(required)} chapters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
