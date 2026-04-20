#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Verify NOTICE mentions every direct dependency declared in pyproject.toml.

Purpose: NOTICE is the shipped attribution file. Adopters and distros
re-use it to comply with the licenses of SKIFF's dependencies. When a
new direct dep is added to `pyproject.toml` without updating NOTICE,
the attribution becomes silently incomplete — a compliance gap that's
invisible on the `pyproject.toml` PR diff.

This script parses `[project].dependencies` + `[project.optional-dependencies]`
(all extras merged) and fails if any package's distribution name is not
mentioned in NOTICE. Transitive deps are NOT required; the maintainer
attributes direct deps with one line each plus the "Full dependency list
via pip-licenses" escape hatch for anything transitive.

Usage:
    python3 tools/check_notice_coverage.py           # print report
    python3 tools/check_notice_coverage.py --check   # CI mode, exit 1 on gap

Exit codes: 0 complete, 1 missing attribution.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTICE = ROOT / "NOTICE"
PYPROJECT = ROOT / "pyproject.toml"

# Case-insensitive, hyphen/underscore-insensitive normaliser for PyPI
# distribution names. PEP 503 says `fastapi`, `Fast-API`, `fast_api` all
# refer to the same project.
_NAME_NORMALISE_RE = re.compile(r"[-_.]+")


def _normalise(name: str) -> str:
    return _NAME_NORMALISE_RE.sub("-", name.strip().lower())


def _direct_dep_names() -> set[str]:
    """Return the set of direct dependency distribution names from pyproject.toml.

    Covers `[project].dependencies` plus every list under
    `[project.optional-dependencies]`. Strips version specifiers and
    extras markers (`uvicorn[standard]>=0.30` → `uvicorn`).
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    raw: list[str] = list(project.get("dependencies", []))
    for extra_deps in (project.get("optional-dependencies") or {}).values():
        raw.extend(extra_deps)
    names: set[str] = set()
    for spec in raw:
        head = re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0].strip()
        if head:
            names.add(_normalise(head))
    return names


# Packages we don't require attribution for: development / test tools
# that aren't shipped to adopters. Extras-only deps and runtime deps
# DO require attribution.
_DEV_ONLY_PACKAGES: frozenset[str] = frozenset(
    _normalise(n)
    for n in (
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "pytest-timeout",
        "pytest-playwright",
        "playwright",
        "httpx",
        "websocket-client",
        "hypothesis",
        "ruff",
        "pip-audit",
        "pip-tools",
        "pip-compile",
        "cyclonedx-bom",
        "cyclonedx-python-lib",
        "pre-commit",
        "coverage",
    )
)


def _notice_text() -> str:
    return NOTICE.read_text(encoding="utf-8").lower()


def _mentions(notice: str, name: str) -> bool:
    # A mention can be either the normalised form (fastapi) or the
    # display form (FastAPI) — both lowercase under _notice_text().
    candidates = {name, name.replace("-", "_"), name.replace("-", "")}
    return any(c in notice for c in candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: exit 1 if any direct runtime dep is unattributed.",
    )
    args = parser.parse_args()

    declared = _direct_dep_names()
    runtime_deps = declared - _DEV_ONLY_PACKAGES
    notice = _notice_text()

    missing = {name for name in runtime_deps if not _mentions(notice, name)}
    if missing:
        print(
            "NOTICE is missing attribution for these direct runtime "
            "dependencies declared in pyproject.toml:\n  " + "\n  ".join(sorted(missing)),
            file=sys.stderr,
        )
        return 1 if args.check else 0

    print(f"ok: NOTICE covers all {len(runtime_deps)} direct runtime dependencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
