# SPDX-License-Identifier: MIT
"""Static checks on skiff/static/ui.js exports.

We don't have a JS unit-test harness wired up, so this file performs the
cheapest useful check: every factory we advertise in the contract is
actually exported on `window.UI`. Catches the "I renamed the factory
but forgot to update the exports" bug without requiring a browser.

Deep interaction tests live in the e2e suite (Playwright) — they're the
source of truth for runtime behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_UI_PATH = Path(__file__).resolve().parent.parent / "skiff" / "static" / "ui.js"

# Every factory added must be listed here so the test catches drift.
_EXPECTED_EXPORTS: frozenset[str] = frozenset(
    {
        "el",
        # CSP-strict helpers: route JS-set styles through CSSOM rule mutation
        # on /static/styles.css instead of element.style.* so a strict
        # `style-src 'self'` (no 'unsafe-inline') CSP works. `getStyle`
        # is the symmetric read (since `element.style.X` returns ''
        # under strict CSP and call sites can't introspect that directly).
        "setStyle",
        "getStyle",
        "kvRow",
        "kvSection",
        "copy",
        "copyCmd",
        "copyBlock",
        "modal",
        "toast",
        "helpIcon",
        "form",
        "formModal",
        "table",
        "inspect",
        "downloadJson",
        "registerPage",
        "getPages",
        "getPage",
        # i18n — `t` also exposed as a bare window global (see ui.js); the
        # UI-namespaced form here is for call sites that prefer `UI.t(...)`.
        "t",
    }
)


def _ui_text() -> str:
    return _UI_PATH.read_text(encoding="utf-8")


def _ui_exports() -> set[str]:
    """Extract the keys from `root.UI = { ... };` — the single export block."""
    text = _ui_text()
    m = re.search(r"root\.UI\s*=\s*\{([^}]+)\}", text, re.DOTALL)
    if not m:
        raise AssertionError("Could not find `root.UI = { ... }` block in ui.js")
    body = m.group(1)
    # Each line "name: expr," — grab the left side before the colon.
    exports: set[str] = set()
    for raw_line in body.splitlines():
        clean = raw_line.strip().rstrip(",")
        if not clean or clean.startswith("//"):
            continue
        key = clean.split(":", 1)[0].strip()
        if key and key.isidentifier():
            exports.add(key)
    return exports


class TestUIExports:
    def test_every_expected_factory_exported(self) -> None:
        exports = _ui_exports()
        missing = _EXPECTED_EXPORTS - exports
        assert not missing, f"ui.js missing exports: {sorted(missing)}"

    def test_no_surprise_exports(self) -> None:
        """Reverse drift: anything exported must be in the known list,
        or an internal-only helper prefixed with `_`."""
        exports = _ui_exports()
        extra = {e for e in exports if not e.startswith("_")} - _EXPECTED_EXPORTS
        assert not extra, (
            f"ui.js exports names not in _EXPECTED_EXPORTS:\n  {sorted(extra)}\n"
            "Add them here or rename to start with `_` (internal)."
        )

    def test_every_factory_has_docstring(self) -> None:
        """Each factory must have a preceding /** ... */ block doc-commenting it."""
        text = _ui_text()
        for name in _EXPECTED_EXPORTS:
            # Match a function decl with that name preceded by a /** ... */ block
            pattern = rf"/\*\*(?:(?!\*/).)*?\*/\s*function\s+{re.escape(name)}\s*\("
            if not re.search(pattern, text, re.DOTALL):
                raise AssertionError(f"ui.js factory {name!r} has no /** ... */ docstring")

    def test_no_uses_of_raw_innerhtml_in_widget_core(self) -> None:
        """Widgets must not use innerHTML with interpolated data.

        The `html` attribute on `el()` is the explicit opt-in for callers
        who pass TRUSTED static markup. Inside `ui.js` itself we only see
        one reference: the `html` branch in `el()`. That's expected.
        """
        text = _ui_text()
        # Every literal `.innerHTML =` must be inside the el() html branch
        matches = re.findall(r"\.innerHTML\s*=", text)
        # el()'s `html` branch contains ONE `.innerHTML = v;`
        assert len(matches) == 1, (
            f"ui.js has {len(matches)} .innerHTML assignments — expected 1 "
            f"(only the explicit `html` opt-in branch of el())."
        )
