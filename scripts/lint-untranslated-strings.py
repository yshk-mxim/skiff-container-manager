#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Report user-facing string literals in skiff/static/**.js that bypass t().

Advisory linter — the pre-i18n infrastructure (strings.en.js + t() helper)
is in place, but the per-page JS modules still hold ad-hoc literals. This
scan surfaces those for incremental migration; it does NOT fail the build.

Heuristic, not AST — we look for known call patterns whose first positional
arg or key-value assignment is a quoted literal:

  makeBtn('...', ...)        / makeActionBtn('...', ...)   — button labels
  toast('...', 'success')                                   — toast text
  confirm('...')             / alert('...')                 — dialog copy
  text:  '...'               — UI.el text shorthand
  title: '...'               — UI.el / modal title
  label: '...'               — form-field label
  placeholder: '...'         — form-field placeholder

Anything already wrapped in t(...) is fine. Anything that uses a variable
(e.g. `text: name`) is also fine — the literal-detection regex only matches
quoted strings at that position. Technical / non-user-visible strings are
filtered out by an explicit allow-list (class names, URLs, element tags).

Usage:

    python3 scripts/lint-untranslated-strings.py           # human report
    python3 scripts/lint-untranslated-strings.py --count   # just the totals

Add the path as an argument to narrow the scan to a single file / dir.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = [
    REPO_ROOT / "skiff/static/app.js",
    REPO_ROOT / "skiff/static/pages",
    REPO_ROOT / "skiff/static/core",
]

# Patterns to flag. Each tuple: (regex, kind, capture-group-index).
# The regex captures the literal so we can render a readable line.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"""\bmakeBtn\s*\(\s*(['"])([^'"]+)\1"""), "button"),
    (re.compile(r"""\bmakeActionBtn\s*\(\s*(['"])([^'"]+)\1"""), "action-button"),
    (re.compile(r"""\btoast\s*\(\s*(['"])([^'"]+)\1"""), "toast"),
    (re.compile(r"""\bconfirm\s*\(\s*(['"])([^'"]+)\1"""), "confirm"),
    (re.compile(r"""\balert\s*\(\s*(['"])([^'"]+)\1"""), "alert"),
    (re.compile(r"""\btext\s*:\s*(['"])([^'"]+)\1"""), "text-prop"),
    (re.compile(r"""\btitle\s*:\s*(['"])([^'"]+)\1"""), "title-prop"),
    (re.compile(r"""\blabel\s*:\s*(['"])([^'"]+)\1"""), "label-prop"),
    (re.compile(r"""\bplaceholder\s*:\s*(['"])([^'"]+)\1"""), "placeholder-prop"),
]

# Literals that are NOT user-facing copy: tag names, css classes, CSS values,
# URL paths, data attributes, and short identifiers. The filter runs against
# the captured string.
_IGNORE_EXACT = {
    "div",
    "span",
    "button",
    "p",
    "a",
    "label",
    "input",
    "h1",
    "h2",
    "h3",
    "h4",
    "table",
    "tr",
    "td",
    "th",
    "thead",
    "tbody",
    "select",
    "option",
    "img",
    "hr",
    "br",
    "nav",
    "svg",
    "path",
    "pre",
    "code",
    "ul",
    "li",
    "ol",
    "form",
    "text",
    "password",
    "number",
    "email",
    "tel",
    "url",
    "search",
    "checkbox",
    "radio",
    "hidden",
    "file",
    "true",
    "false",
    "-",
}


def _is_ignorable(literal: str) -> bool:
    s = literal.strip()
    if not s:
        return True
    # Class-list strings / flat token lists (space-separated short tokens).
    if re.fullmatch(r"[a-z][a-z0-9_\-\s]*", s) and len(s) < 40 and " " in s and len(s.split()) <= 4:
        if all(len(tok) <= 16 for tok in s.split()):
            return True
    if s in _IGNORE_EXACT:
        return True
    # URL / path / api-route literals.
    if s.startswith(("/", "http://", "https://", "#", ":", ".")):
        return True
    # CSS tokens (px, em, %, hex colour, var(--x), rgba(...)).
    if re.fullmatch(r"-?\d+(\.\d+)?(px|em|rem|%|pt|vw|vh|ms|s)?", s):
        return True
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", s):
        return True
    if re.fullmatch(r"var\(--[\w-]+\)", s):
        return True
    # Identifiers / short technical tokens (no space, all lowercase or kebab).
    if " " not in s and re.fullmatch(r"[a-z][a-z0-9_\-]*", s) and len(s) <= 24:
        return True
    # Whitespace-only / punctuation-only.
    if re.fullmatch(r"[\s\-–—·]+", s):
        return True
    return False


def _line_has_t_call(line: str) -> bool:
    """True if the line calls t(...) — the literal is probably a fallback."""
    return bool(re.search(r"\bt\s*\(\s*['\"]", line))


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return hits
    for line_no, line in enumerate(text.splitlines(), start=1):
        # Skip comments — // and /* */ lines that happen to look like a call.
        stripped = line.strip()
        if stripped.startswith(("//", "/*", "*")):
            continue
        if _line_has_t_call(line):
            # Line already translates something; its extra literal is likely
            # a vars object value (substitution param) — skip to reduce noise.
            continue
        for rx, kind in _PATTERNS:
            for m in rx.finditer(line):
                literal = m.group(2)
                if _is_ignorable(literal):
                    continue
                hits.append((line_no, kind, literal))
    return hits


def _iter_targets(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".js":
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.rglob("*.js")))
    return out


def main(argv: list[str]) -> int:
    count_only = "--count" in argv
    user_paths = [Path(a) for a in argv[1:] if not a.startswith("--")]
    targets = _iter_targets(user_paths or DEFAULT_TARGETS)
    total = 0
    by_file: dict[Path, list[tuple[int, str, str]]] = {}
    for path in targets:
        # Skip strings.en.js — it IS the i18n dictionary; it MUST be literals.
        if path.name == "strings.en.js":
            continue
        hits = _scan_file(path)
        if hits:
            by_file[path] = hits
            total += len(hits)
    if count_only:
        print(f"untranslated-literal candidates: {total}")
        return 0
    if not by_file:
        print("No untranslated user-facing literals found.")
        return 0
    print(f"Untranslated-literal candidates ({total} across {len(by_file)} file(s)):")
    print()
    for path, hits in sorted(by_file.items()):
        rel = path.relative_to(REPO_ROOT)
        print(f"── {rel} ──")
        for line_no, kind, literal in hits:
            snippet = literal if len(literal) <= 70 else literal[:67] + "…"
            print(f"  {line_no:>4}  [{kind}]  {snippet!r}")
        print()
    print(
        "Advisory — not a CI gate. Migrate by adding a key to "
        "`skiff/static/strings.en.js` and wrapping the call site in `t('<key>')`."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
