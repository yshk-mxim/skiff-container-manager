# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Contract tests for the pre-i18n UI strings bundle.

`skiff/static/strings.en.js` is the single home for user-facing UI copy,
and the `t(key)` helper in `ui.js` reads it at page-load time. These
tests enforce the two invariants the bundle must satisfy to remain
useful as infrastructure:

  1. The file parses as a JavaScript object literal we can read from
     Python (no syntax error, balanced braces).
  2. A well-known set of keys every call site relies on exists. Adding
     new keys is free; silently deleting one that a page still
     references would surface as the literal key rendered in the UI.

Why not parse as JSON? The bundle is a `.js` file that assigns to
`window.SKIFF_STRINGS` so it can ship without a build step. We extract
the object literal and normalise it to JSON via a minimal regex — good
enough for a static dict, and keeps the bundle editable by humans
without JSON's strict quoting rules.

The parser is intentionally narrow: if a future change adds features
that the regex can't handle (function values, computed keys, spreads),
the test fails loudly and the convention either bends or the parser
grows — not both silently.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

_STRINGS_PATH = pathlib.Path(__file__).resolve().parent.parent / "skiff" / "static" / "strings.en.js"

# Keys every call site in the shipped pages references. New keys can
# (and should) be added as more pages migrate off inline English; the
# test's job is to catch accidental deletion, not to freeze the surface.
_REQUIRED_KEYS = (
    # Cross-cutting verbs / nouns
    "common.cancel",
    "common.close",
    "common.copy",
    "common.error",
    "common.loading",
    "common.delete",
    "common.none",
    # Volumes page — fully migrated as the first exemplar.
    "volumes.title",
    "volumes.empty",
    "volumes.in_use",
    "volumes.unused",
    "volumes.description",
    "volumes.actions.create",
    "volumes.actions.prune",
    "volumes.actions.inspect",
    "volumes.confirm.remove",
    "volumes.confirm.prune",
    "volumes.toast.created",
    "volumes.toast.pruned",
    "volumes.columns.name",
    "volumes.columns.driver",
    "volumes.columns.mountpoint",
    "volumes.columns.created",
    "volumes.columns.actions",
    "volumes.create_placeholder",
    "volumes.inspect.scope",
    "volumes.inspect.usage_bytes",
    "volumes.inspect.ref_count",
    "volumes.inspect.labels",
    "volumes.inspect.options",
    "volumes.inspect.status",
    "volumes.inspect.used_by",
    "volumes.inspect.local_default",
    "volumes.inspect.driver_not_reported",
    # Undo UX strings — used by app.js `undoableDelete` helper.
    "undo.button",
    "undo.window_passed",
    "undo.deleted_suffix",
    "undo.action_in_progress",
    "undo.toast",
)


def _load_strings_dict() -> dict:
    """Read `window.SKIFF_STRINGS = { ... };` and parse it as JSON.

    Converts unquoted keys (`cancel: "Cancel"`) into JSON form
    (`"cancel": "Cancel"`), strips the trailing semicolon, drops single-
    line `// comments`, collapses trailing commas. The transformations
    are deliberate — a JSON parser is the simplest validator for shape
    and balance, and anything it can't digest is probably a bug.
    """
    text = _STRINGS_PATH.read_text(encoding="utf-8")
    # Locate the opening brace after `= `. Every other assignment in the
    # file is a comment or the `window.SKIFF_STRINGS` line itself.
    m = re.search(r"window\.SKIFF_STRINGS\s*=\s*(\{)", text)
    if not m:
        raise AssertionError("window.SKIFF_STRINGS assignment not found in strings.en.js")
    start = m.start(1)
    # Walk braces to find the matching close. Handles strings so a `}`
    # inside `"text"` doesn't fool the counter.
    depth = 0
    i = start
    in_str = False
    str_quote = ""
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == str_quote:
                in_str = False
        elif ch in ('"', "'"):
            in_str = True
            str_quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    else:
        raise AssertionError("unbalanced braces in strings.en.js")
    src = text[start:end]
    # Strip line comments so the JSON parser doesn't choke on `// …`.
    src = re.sub(r"//[^\n]*", "", src)
    # Quote bare keys: `  cancel:` → `  "cancel":` — only word chars
    # followed by `:`, at line start (after whitespace) and not already
    # inside a string literal.
    src = re.sub(r"(^|\{|,|\s)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', src)
    # Drop trailing commas before `}` or `]`.
    src = re.sub(r",(\s*[}\]])", r"\1", src)
    # Collapse Python-style `"…" + "…"` concatenation to one string so the
    # JSON parser accepts it (JSON has no concatenation operator, but the
    # JS source uses it for long help lines).
    src = re.sub(r'"\s*\+\s*"', "", src)
    return json.loads(src)


@pytest.fixture(scope="module")
def strings_dict() -> dict:
    return _load_strings_dict()


def test_strings_bundle_parses(strings_dict: dict) -> None:
    """Bundle is a non-empty dict. If this fails, the bundle has a
    syntax error the `t()` helper will surface as missing keys across
    the whole UI — fast-fail here instead."""
    assert isinstance(strings_dict, dict)
    assert strings_dict, "strings.en.js parsed to an empty dict"


@pytest.mark.parametrize("dotted", _REQUIRED_KEYS)
def test_required_key_present(strings_dict: dict, dotted: str) -> None:
    """Every key listed in `_REQUIRED_KEYS` resolves to a string value.

    Adding a new key to the bundle is free; deleting one a page relies on
    would make the UI render the literal key (e.g. `common.cancel`).
    This test catches that before a user does.
    """
    node = strings_dict
    for part in dotted.split("."):
        assert isinstance(node, dict), f"{dotted}: traversal hit non-dict at {part!r}"
        assert part in node, f"missing key: {dotted!r} (failed at {part!r})"
        node = node[part]
    assert isinstance(node, str), f"{dotted!r} must resolve to a string; got {type(node).__name__}"
    assert node.strip(), f"{dotted!r} is empty"
