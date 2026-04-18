# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Full-SPA WCAG 2.1 AA sweep via axe-core.

The pa11y CI job (.github/workflows/a11y.yml) covers the pre-auth
login flow. These tests pick up after login and walk every registered
page (containers, images, volumes, networks, compose, system)
running axe-core against each. A real defect in any of them fails
the suite — "AA" is claimed for the whole SPA, not just the entry
screen.

axe-core is injected into the page via CDN (the test fixture adds
the script tag after login is complete), evaluated with WCAG 2.0/2.1
rule tags, and violations are asserted empty.
"""

from __future__ import annotations

# Load e2e-specific fixtures (live_server, page, docker_client) — same pattern
# as the other test_e2e_* files. Without this, pytest-playwright's built-in
# `page` fixture is used instead of our logged-in one, and axe runs against
# a blank Chromium tab rather than SKIFF.
pytest_plugins = ["tests.conftest_e2e"]

import json
import urllib.request

import pytest

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e

# axe-core UMD bundle — pinned so upstream rule changes don't silently alter
# the pass/fail surface. Bump deliberately when updating.
#
# We fetch the source once at module load and inject it via `page.evaluate()`
# rather than `add_script_tag(url=...)` — SKIFF's production CSP is strict
# `script-src 'self'`, so CDN script tags are blocked (correctly). Evaluating
# the source text through the CDP evaluate binding installs `window.axe`
# without tripping CSP, and keeps the test running against the SAME CSP the
# user's browser sees in production.
_AXE_VERSION = "4.10.0"
_AXE_CDN = f"https://cdn.jsdelivr.net/npm/axe-core@{_AXE_VERSION}/axe.min.js"


_AXE_SOURCE: str | None = None


def _axe_source() -> str:
    """Fetch and cache the axe-core bundle the first time a test needs it.

    Fetching at module-import time would fail `pytest --collect-only` on a
    sandboxed runner with no egress, even for test selections that skip the
    e2e marker. Defer the fetch until a test that actually requests it runs.
    """
    global _AXE_SOURCE
    if _AXE_SOURCE is None:
        with urllib.request.urlopen(_AXE_CDN, timeout=30) as r:  # noqa: S310 — pinned jsDelivr URL, test-only
            _AXE_SOURCE = r.read().decode("utf-8")
    return _AXE_SOURCE


# WCAG 2.1 AA rule surface — the tags axe-core uses to select checks.
# `wcag2a` + `wcag2aa` cover 2.0; `wcag21a` + `wcag21aa` add 2.1's
# new success criteria (reflow, non-text contrast, target size, etc).
_AXE_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")


def _run_axe(page) -> list[dict]:
    """Inject axe-core, run it, return the list of violations."""
    # Evaluate the axe source so `window.axe` is installed without a
    # `<script>` tag (CSP `script-src 'self'` would block a CDN script).
    page.evaluate(_axe_source())
    # Run axe.run() against the whole document with the WCAG 2.1 AA tag set.
    result = page.evaluate(
        """
        async (tags) => {
            const res = await axe.run(document, {
                runOnly: { type: 'tag', values: tags },
                // Exclude the terminal output region from contrast checks:
                // shell output intentionally uses ANSI escape colours the
                // user's own shell chose, not SKIFF's palette.
                rules: {
                    'color-contrast': { enabled: true },
                }
            });
            return res.violations;
        }
        """,
        list(_AXE_TAGS),
    )
    return result or []


def _fmt_violations(violations: list[dict]) -> str:
    """Render violations to a readable block for assertion messages."""
    if not violations:
        return "(none)"
    out = []
    for v in violations:
        impact = v.get("impact", "?")
        rule = v.get("id", "?")
        desc = v.get("description", "?")
        nodes = v.get("nodes", [])
        out.append(f"  [{impact}] {rule}: {desc} ({len(nodes)} node(s))")
        for n in nodes[:3]:
            html = (n.get("html") or "")[:160]
            target = ",".join(n.get("target") or [])[:120]
            out.append(f"      at {target}")
            out.append(f"         html: {html}")
        if len(nodes) > 3:
            out.append(f"      … +{len(nodes) - 3} more nodes")
    return "\n".join(out)


# Registered pages in SKIFF at HEAD. Each entry is (nav-selector,
# description). The `data-page` attribute is stable — set by
# `UI.registerPage({...})` in every page module (see
# `skiff/static/core/sidebar.js`).
_PAGES = [
    ("containers", "Containers"),
    ("images", "Images"),
    ("volumes", "Volumes"),
    ("networks", "Networks"),
    ("compose", "Compose"),
    ("system", "System"),
]


@pytest.mark.parametrize("page_id,label", _PAGES, ids=[p[0] for p in _PAGES])
def test_wcag_2_1_aa_full_spa_sweep(page, page_id, label):
    """Every SKIFF page must be WCAG 2.1 AA clean.

    The `page` fixture logs in and lands on Containers. We navigate to
    each registered page, let it settle, then run axe-core with the
    full WCAG 2.0 + 2.1 AA rule surface.

    A violation in ANY page fails the test for that page — the
    parametrisation keeps the failure localised so a maintainer knows
    exactly which screen needs attention without debugging a grab-bag.
    """
    # Click the sidebar entry if we're not already there.
    nav = page.locator(f"a[data-page='{page_id}']")
    if nav.count() > 0:
        nav.first.click()
        # Small settle — wait for the main region to update.
        page.wait_for_load_state("networkidle", timeout=5000)

    violations = _run_axe(page)

    # Filter out violations that are known to be out-of-scope for the
    # SKIFF project's accessibility commitment. Empty today; each
    # future entry gets a file-line fix OR an inline justification.
    #
    # Do NOT add an entry here without updating
    # `docs/compliance/wcag-2-1-aa.md` — that's the source of truth
    # for which rules are claimed "AA clean" vs "AA with documented
    # carve-out".
    known_carveouts: set[str] = set()

    real_violations = [v for v in violations if v.get("id") not in known_carveouts]

    assert not real_violations, (
        f"WCAG 2.1 AA violations on page '{page_id}' ({label}):\n"
        + _fmt_violations(real_violations)
        + f"\n\nFull axe report ({len(violations)} items):\n{json.dumps(violations, indent=2)[:2000]}"
    )
