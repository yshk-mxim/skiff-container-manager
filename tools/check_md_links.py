#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Markdown cross-link checker.

Walks every `*.md` file tracked in the repo and verifies that each
relative link (`](path)` / `](path#anchor)`) resolves to a file that
actually exists. Anchors inside the target file are also checked
against the file's headings.

External links (`http://`, `https://`, `mailto:`) are not touched — a
network-dependent link checker would flake on CI. Internal links are
the ones we own and the ones that rot.

Usage:
    python tools/check_md_links.py           # human-readable report
    python tools/check_md_links.py --check   # CI mode: exit 1 on broken link

Exit codes: 0 clean, 1 broken link(s) found.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Markdown link regex: `[text](target)` where target is not a full URL
# and not a reference-style `[text][id]`. Captures `target` and any
# fragment suffix.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Heading regex: `#`, `##`, etc. at line start followed by the text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Skip these as non-broken: anchor-only, empty, external.
_IGNORE_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:", "ftp://")


def _slugify(heading: str) -> str:
    """GitHub-style heading → anchor slug.

    Lowercase, spaces → hyphens, drop everything outside [a-z0-9 -_].
    Matches what GitHub's markdown renderer generates for `[x](#heading-text)`.
    """
    s = heading.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _extract_anchors(path: pathlib.Path) -> set[str]:
    """Return the set of anchor slugs generated from the target's headings."""
    try:
        text = path.read_text()
    except OSError:
        return set()
    return {_slugify(m.group(2)) for m in _HEADING_RE.finditer(text)}


def _check_one_link(
    md_path: pathlib.Path,
    own_text: str,
    lineno: int,
    target: str,
) -> tuple[int, str, str] | None:
    """Return a (lineno, target, reason) finding for `target`, or None if clean."""
    if not target or target.startswith(_IGNORE_PREFIXES):
        return None
    path_part, _, anchor = target.partition("#")
    if not path_part:
        # Same-file anchor.
        if anchor and anchor not in {_slugify(m.group(2)) for m in _HEADING_RE.finditer(own_text)}:
            return lineno, target, f"anchor #{anchor} not found in same file"
        return None
    resolved = (md_path.parent / path_part).resolve()
    if not resolved.exists():
        return lineno, target, f"target file {resolved.relative_to(ROOT)!s} missing"
    if anchor and resolved.suffix == ".md" and anchor not in _extract_anchors(resolved):
        return lineno, target, f"anchor #{anchor} not found in {resolved.relative_to(ROOT)!s}"
    return None


def _check_file(md_path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Return a list of (line, target, reason) for each broken link in `md_path`."""
    try:
        text = md_path.read_text()
    except OSError:
        return []
    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _LINK_RE.finditer(line):
            finding = _check_one_link(md_path, text, lineno, m.group(1).strip())
            if finding is not None:
                findings.append(finding)
    return findings


def _iter_md_files() -> list[pathlib.Path]:
    # `.venv`, `venv`, `env` caught so a fresh-clone contributor's
    # virtualenv — which vendors thousands of unrelated .md files from
    # installed packages — doesn't trip `make docs-check`. Same for
    # `.tox`, `site-packages`, and the build / dist output dirs.
    skip_dirs = frozenset(
        {
            ".git",
            "__pycache__",
            "node_modules",
            "htmlcov",
            ".venv",
            "venv",
            "env",
            ".tox",
            "site-packages",
            "build",
            "dist",
            ".eggs",
        }
    )
    return sorted(p for p in ROOT.rglob("*.md") if not any(skip in p.parts for skip in skip_dirs))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 on any broken link (CI mode). Default prints a report.",
    )
    args = parser.parse_args()

    total_files = 0
    total_findings: list[tuple[pathlib.Path, int, str, str]] = []
    for md in _iter_md_files():
        total_files += 1
        for lineno, target, reason in _check_file(md):
            total_findings.append((md, lineno, target, reason))

    for md, lineno, target, reason in total_findings:
        print(f"{md.relative_to(ROOT)}:{lineno}: broken link [{target}] — {reason}")

    if total_findings:
        print(f"\nFAIL: {total_files} files, {len(total_findings)} broken links")
        return 1 if args.check else 0
    print(f"ok: {total_files} files, 0 broken links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
