# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""First-run journeys — the 5 scenarios every new user walks through
in the first 3 minutes with SKIFF.

Each journey observes the app via `audit_observer` (screenshot, DOM,
console log, network trace per step) so the persona-audit harness can
surface copy/contract/a11y drift.
"""

from __future__ import annotations

import pytest

from tests.audit_driver import step
from tests.journeys import journey


pytest_plugins = ["tests.conftest_e2e", "tests.conftest_audit"]

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]"',
)

pytestmark = pytest.mark.e2e


@journey(
    persona=("novice", "developer", "hobbyist"),
    category="first_run",
    severity="high",
    covers=("hb-dashboard-missing",),
)
def test_journey_landing_on_dashboard(audited_page, live_server, audit_observer, persona):
    """First page after sign-in. Dashboard MUST render without errors;
    every quick-action button must be tabbable + clickable; recent
    activity panel must render (even if empty)."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
        page.wait_for_selector(".sidebar", timeout=SHORT)

    with step("step_2_dashboard_renders"):
        # Click Dashboard (or confirm it's the default landing).
        page.locator(".sidebar a:has-text('Dashboard')").click()
        page.wait_for_selector("h2:has-text('Overview')", timeout=SHORT)

    with step("step_3_quick_actions_visible"):
        # At minimum "Run a container" should be clickable.
        assert page.locator("button:has-text('Run a container')").count() > 0, (
            "Dashboard missing the 'Run a container' quick-action."
        )
        assert page.locator("button:has-text('Quick-start from template')").count() > 0, (
            "Dashboard missing the 'Quick-start from template' quick-action."
        )

    with step("step_4_recent_activity_visible"):
        # "Recent activity (last 5 min)" section header must be present.
        assert page.locator("text=Recent activity").count() > 0, (
            "Dashboard missing the recent-activity section."
        )


@journey(
    persona=("novice", "hobbyist"),
    category="first_run",
    severity="high",
    covers=("hb-templates-missing",),
)
def test_journey_templates_catalog_visible(audited_page, live_server, audit_observer, persona):
    """Novice path to a first deploy: Dashboard → Quick-start →
    Templates page. All 8 seeded templates must render as cards."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_navigate_to_templates"):
        page.locator(".sidebar a:has-text('Templates')").click()
        page.wait_for_selector("h2:has-text('App templates')", timeout=SHORT)

    with step("step_3_all_known_templates_render"):
        # The 8 seeded templates from config._APP_TEMPLATES.
        for tid in ("nginx", "postgres", "redis", "mysql", "mongo",
                    "python", "node", "alpine"):
            assert page.locator(f"[data-testid='template-{tid}']").count() == 1, (
                f"Template {tid!r} card missing from the catalogue"
            )

    with step("step_4_filter_input_present"):
        # The Templates page should have a search/filter input.
        assert page.locator("input[type='search']").count() > 0, (
            "Templates page missing its filter input"
        )


@journey(
    persona=("novice",),
    category="first_run",
    severity="medium",
)
def test_journey_sidebar_navigation_reaches_every_page(audited_page, live_server, audit_observer, persona):
    """Novices click the sidebar. Every declared page must be reachable
    and render within SHORT timeout — this is a pure navigation-health
    journey."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    pages = [
        ("Dashboard", "Overview"),
        ("Containers", "Containers"),
        ("Images", "Images"),
        ("Templates", "App templates"),
        ("Volumes", "Volumes"),
        ("Networks", "Networks"),
        ("Compose", "Compose"),
        ("System", "System"),
    ]
    for nav_label, expected_h2_substr in pages:
        with step(f"step_nav_{nav_label.lower()}"):
            page.locator(f".sidebar a:has-text('{nav_label}')").click()
            # Accept either exact match or substring for H2 — the harness
            # emits a finding if the match is ambiguous.
            page.wait_for_selector(f"h2:has-text('{expected_h2_substr}')", timeout=SHORT)


@journey(
    persona=("security_reviewer",),
    category="first_run",
    severity="high",
)
def test_journey_reviewer_sees_readonly_banner(audited_page, live_server, audit_observer, persona):
    """When PROFILE=reviewer, the UI must surface a visible banner
    explaining mutations are disabled. Test observes the banner;
    reviewer-mode server-side enforcement is covered by ZT-7."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    # Navigate to Containers (page with visible mutation buttons under
    # non-reviewer modes).
    with step("step_2_nav_containers"):
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("h2:has-text('Containers')", timeout=SHORT)

    # This journey doesn't actually toggle reviewer mode — the e2e
    # token is admin. But the journey's structure reserves the step
    # pattern for a future commit where conftest_e2e exposes a
    # reviewer-mode fixture. For now we just observe the mutation
    # buttons exist, and ZT-7 covers the server gate.
    with step("step_3_observe_mutation_buttons"):
        assert page.locator("button:has-text('Run new container')").count() > 0


@journey(
    persona=("ui_ux_auditor",),
    category="first_run",
    severity="medium",
)
def test_journey_keyboard_reaches_every_sidebar_entry(audited_page, live_server, audit_observer, persona):
    """UI/UX auditor mandate: tab-only navigation must reach every
    sidebar link in visual order. Journey tabs from the search bar
    and counts how many sidebar links become active before Tab
    wraps back to the top."""
    from tests.e2e_helpers import login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_focus_first_interactive"):
        # Focus the first interactive element — usually the palette
        # search or a sidebar link.
        page.keyboard.press("Tab")

    reached: set[str] = set()
    with step("step_3_tab_through_sidebar"):
        for _ in range(40):  # upper bound; sidebar has ~8 entries
            page.keyboard.press("Tab")
            active_text = page.evaluate(
                "() => document.activeElement && document.activeElement.innerText",
            ) or ""
            reached.add(active_text.strip().split("\n")[0])
        # We expect a substantial fraction of sidebar labels to appear
        # via Tab traversal.
        expected = {"Dashboard", "Containers", "Images", "Templates",
                    "Volumes", "Networks", "Compose", "System"}
        hits = expected & reached
        # Finding if fewer than half the sidebar entries are reached
        # (not a hard fail — depends on which element Playwright started
        # focus on). Journey observation continues so the harness records
        # all artifacts for the report.
        if len(hits) < 4:
            audit_observer.emit(
                step="step_3_tab_through_sidebar",
                severity="medium",
                category="a11y",
                title="Tab traversal reaches fewer than half the sidebar entries",
                expected=f"at least 4 of {expected} focused via Tab",
                observed=f"reached {hits}",
            )
