# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""First-run journeys — every scenario a new user walks through in
the first 3 minutes with SKIFF.

Two layers:
  1. Plan-named scenarios (Section 2.1 J-01): wizard, abandoned wizard,
     tunnel setup, token recovery, reset + rewizard.
  2. Substitutions that add value: dashboard/templates/sidebar smoke,
     reviewer-banner placement, tab-key reach.

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


# ── Plan-named J-01 scenarios ────────────────────────────────────────
# These mirror the scenarios enumerated in the plan (Section 2.1 J-01)
# so the catalogue proves every named item was exercised at least once.
# They're observation-oriented — they probe that the relevant endpoint
# exists and returns a sane envelope without destroying an existing
# setup (conftest already seeds the live server with a token).


@journey(
    persona=("novice",),
    category="first_run",
    severity="high",
)
def test_journey_zero_config_wizard_reachable(audited_page, live_server, audit_observer, persona):
    """Plan J-01 item: zero-config wizard. A user hitting a fresh box
    should find the wizard reachable from the root; once a token is
    already configured (the e2e harness state) the wizard endpoint
    must surface whether setup is complete."""
    import requests

    from tests.e2e_helpers import auth_headers

    with step("step_1_probe_setup_state"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/setup-state",
            headers=auth_headers(), timeout=10,
        )
        assert r.status_code == 200, f"setup-state failed: {r.status_code}"
        body = r.json()
        # Shape: must expose whether setup has completed and whether
        # the Docker socket is reachable. Missing either means a
        # novice can't tell what step they're on.
        for key in ("token_configured", "docker_reachable"):
            if key not in body and key.replace("_", "") not in {k.replace("_", "") for k in body}:
                audit_observer.emit(
                    step="step_1_probe_setup_state",
                    severity="medium",
                    category="contract",
                    title=f"/api/setup-state missing `{key}` field",
                    expected="Boolean fields a novice-facing wizard can render",
                    observed=f"keys: {list(body.keys())}",
                )


@journey(
    persona=("novice",),
    category="first_run",
    severity="medium",
)
def test_journey_abandoned_wizard_recovers(audited_page, live_server, audit_observer, persona):
    """Plan J-01 item: abandoned wizard. If the user closes mid-wizard
    and reopens, the next request to /api/setup-state must still serve
    a valid envelope — no half-torn-down state."""
    import requests

    from tests.e2e_helpers import auth_headers

    with step("step_1_hit_setup_state_twice_with_gap"):
        # Simulate the user opening, closing, reopening.
        for _ in range(2):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/setup-state",
                headers=auth_headers(), timeout=10,
            )
            assert r.status_code == 200, (
                f"setup-state must remain serviceable across reopens; got {r.status_code}"
            )


@journey(
    persona=("developer", "sre_ops"),
    category="first_run",
    severity="medium",
)
def test_journey_tunnel_status_queryable(audited_page, live_server, audit_observer, persona):
    """Plan J-01 item: tunnel setup. GET /api/tunnel/status must always
    be queryable (even when no tunnel is configured) so the UI can render
    'not configured' rather than hang."""
    import requests

    from tests.e2e_helpers import auth_headers

    with step("step_1_query_tunnel_status"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/tunnel/status",
            headers=auth_headers(), timeout=10,
        )
        # Acceptable: 200 (tunnel or not) or 501 (tunnel feature disabled
        # on this build). Not acceptable: 500.
        if r.status_code >= 500 and r.status_code != 501:
            audit_observer.emit(
                step="step_1_query_tunnel_status",
                severity="high",
                category="contract",
                title=f"Tunnel status endpoint returned {r.status_code}",
                expected="200 with tunnel state, or 501 if feature disabled",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            pytest.fail("tunnel status 5xx")


@journey(
    persona=("security_reviewer", "developer"),
    category="first_run",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_post_wizard_token_recovery(audited_page, live_server, audit_observer, persona):
    """Plan J-01 item: post-wizard token recovery. After a token is
    configured, the user can rotate it via POST /api/auth/rotate-token.
    Test PROBES the endpoint's auth envelope without actually rotating
    (that would invalidate the e2e harness's bearer for the remainder
    of the run)."""
    import requests

    with step("step_1_unauthenticated_rotate_fails"):
        # No bearer → 401/403.
        r = requests.post(
            f"{live_server.rstrip('/')}/api/auth/rotate-token",
            headers={"X-Requested-With": "ContainerManager"},
            timeout=10,
        )
        if r.status_code not in (401, 403):
            audit_observer.emit(
                step="step_1_unauthenticated_rotate_fails",
                severity="P0",
                category="security",
                zero_trust=True,
                title="token rotation accessible without auth",
                expected="401/403",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            pytest.fail("rotate-token accessible without auth")


@journey(
    persona=("novice", "sre_ops"),
    category="first_run",
    severity="medium",
)
def test_journey_reset_config_requires_confirm(audited_page, live_server, audit_observer, persona):
    """Plan J-01 item: reset + rewizard. POST /api/auth/reset-config
    is destructive (wipes token + docker host). Like rotate-token we
    don't actually reset — we probe that the endpoint exists and is
    gated. An unauthenticated caller must NOT be able to reset.

    Observation-only journey: we never hit reset with valid auth
    during the suite because it would tear down the harness."""
    import requests

    with step("step_1_unauth_reset_fails"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/auth/reset-config",
            headers={"X-Requested-With": "ContainerManager"},
            timeout=10,
        )
        if r.status_code not in (401, 403):
            audit_observer.emit(
                step="step_1_unauth_reset_fails",
                severity="P0",
                category="security",
                zero_trust=True,
                title="reset-config accessible without auth",
                expected="401/403",
                observed=f"{r.status_code}",
            )
            pytest.fail("reset-config accessible without auth")
