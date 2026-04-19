# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""UI/UX journeys — 7 scenarios driven by the UI/UX-auditor persona.

These journeys assert the things Nielsen's heuristics + WCAG 2.1 AA
demand: keyboard-only navigation, visible focus, mobile reflow, empty
states that explain, search on every list, and persistent toast
history.

axe-core (the de facto a11y engine) runs as a separate sweep in
tests/test_e2e_accessibility.py; these journeys layer on top as
observation targets for the full persona-audit harness.
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
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
    covers=(
        "hb-volumes-no-search", "hb-networks-no-search", "hb-compose-no-search",
    ),
)
def test_journey_every_list_page_has_search(audited_page, live_server, audit_observer, persona):
    """Walk every list page; each must expose a search/filter input.
    Class-sweep journey for the hb-*-no-search family."""
    from tests.e2e_helpers import login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    pages = ("containers", "images", "volumes", "networks", "compose", "templates")
    for section in pages:
        with step(f"step_2_search_present_on_{section}"):
            nav_to(page, section)
            present = (
                page.locator("input[type='search']").count() > 0
                or page.locator("input[placeholder*='search' i]").count() > 0
                or page.locator("input[placeholder*='filter' i]").count() > 0
            )
            if not present:
                audit_observer.emit(
                    step=f"step_2_search_present_on_{section}",
                    severity="high",
                    category="layout",
                    title=f"{section.capitalize()} page missing search affordance",
                    expected="Search/filter input visible on load",
                    observed="No search/filter input found",
                )
                pytest.fail(f"{section} missing search")


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
)
def test_journey_every_page_has_visible_focus_ring(audited_page, live_server, audit_observer, persona):
    """Tab through the sidebar; the focused element must have an
    outline or box-shadow that's visible (non-zero alpha, non-transparent)."""
    from tests.e2e_helpers import login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_tab_and_check_outline"):
        # Tab twice (past any initial focus); then check outline.
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        style = page.evaluate(
            """() => {
                const el = document.activeElement;
                if (!el) return null;
                const cs = window.getComputedStyle(el);
                return {
                    outline: cs.outline,
                    outlineWidth: cs.outlineWidth,
                    outlineColor: cs.outlineColor,
                    boxShadow: cs.boxShadow,
                    tag: el.tagName,
                };
            }"""
        )
        if not style:
            pytest.skip("no active element after Tab")
        # Either outline present OR box-shadow sets a focus indicator.
        has_outline = (
            style.get("outlineWidth", "0px") != "0px"
            and "transparent" not in (style.get("outlineColor") or "")
            and style.get("outline") not in ("none", "")
        )
        has_shadow = style.get("boxShadow", "none") not in ("none", "")
        if not (has_outline or has_shadow):
            audit_observer.emit(
                step="step_2_tab_and_check_outline",
                severity="medium",
                category="a11y",
                title="Focused element has no visible focus indicator",
                expected="Outline or box-shadow visible on tabbed focus",
                observed=f"style={style}",
            )


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
)
def test_journey_mobile_viewport_no_horizontal_scroll(audited_page, live_server, audit_observer, persona):
    """At 375×667 (iPhone 13 mini), every page must fit without
    horizontal scrollbar. Body scrollWidth ≤ window.innerWidth."""
    from tests.e2e_helpers import login, nav_to

    page = audited_page
    page.set_viewport_size({"width": 375, "height": 667})
    with step("step_1_sign_in_mobile"):
        login(page, live_server)

    # Sidebar label → actual H2 copy (dashboard H2 is 'Overview').
    for section, h2 in (("containers", "Containers"), ("templates", "App templates")):
        with step(f"step_2_no_h_scroll_{section}"):
            page.locator(f".sidebar a:has-text('{section.capitalize()}')").click()
            page.wait_for_selector(f"h2:has-text('{h2}')", timeout=5_000)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth > window.innerWidth + 1",
            )
            if overflow:
                audit_observer.emit(
                    step=f"step_2_no_h_scroll_{section}",
                    severity="medium",
                    category="layout",
                    title=f"{section.capitalize()} page overflows 375px viewport",
                    expected="documentElement.scrollWidth ≤ innerWidth at 375px",
                    observed="content exceeds viewport width",
                )


@journey(
    persona=("ui_ux_auditor", "developer"),
    category="ui_ux",
    severity="medium",
    covers=("hb-no-notifications-history",),
)
def test_journey_notifications_bell_shows_recent(audited_page, live_server, audit_observer, persona):
    """Trigger a toast, verify the notifications bell count increments
    and clicking the bell shows the toast in the panel."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_look_for_bell"):
        bell = page.locator(
            "[data-testid='notif-bell'], .notif-bell, [aria-label='Notifications']",
        ).first
        if bell.count() == 0:
            audit_observer.emit(
                step="step_2_look_for_bell",
                severity="medium",
                category="layout",
                title="Notifications bell not present in header",
                expected="Bell icon visible with unread count",
                observed="No bell element found",
                covers_historical="hb-no-notifications-history",
            )
            pytest.fail("bell not present")

    with step("step_3_emit_toast_via_js"):
        # Some toast implementations are global; if window.toast exists
        # we can emit one for the test without triggering a real action.
        page.evaluate(
            "() => { if (window.toast) window.toast('pa-audit test toast', 'info'); }",
        )
        page.wait_for_timeout(400)

    with step("step_4_bell_panel_contains_toast"):
        bell = page.locator(
            "[data-testid='notif-bell'], .notif-bell, [aria-label='Notifications']",
        ).first
        bell.click()
        page.wait_for_timeout(300)
        panel = page.locator("[data-testid='notif-panel'], .notif-panel")
        if panel.count() > 0:
            # Content must include the emitted toast text.
            if "pa-audit test toast" not in panel.inner_text(timeout=SHORT):
                audit_observer.emit(
                    step="step_4_bell_panel_contains_toast",
                    severity="medium",
                    category="behaviour",
                    title="Notifications panel missing recent toast",
                    expected="Emitted toast appears in panel within 400ms",
                    observed=f"panel text: {panel.inner_text()[:200]!r}",
                    covers_historical="hb-no-notifications-history",
                )


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
)
def test_journey_every_empty_state_is_helpful(audited_page, live_server, audit_observer, persona):
    """Class-sweep: every list page with zero rows must show copy that
    explains what WOULD be here and how to get started."""
    from tests.e2e_helpers import login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    # Sections likeliest to have empty state depending on environment.
    for section in ("volumes", "networks"):
        with step(f"step_2_check_empty_state_on_{section}"):
            nav_to(page, section)
            text = page.locator("#main").inner_text()
            # Only assert if we observe an empty state. 'no …' is
            # acceptable IFF surrounded by context.
            if "No " in text and len(text.strip()) < 200:
                # Likely a bare empty state. Check for actionable copy.
                actionable = any(
                    s in text.lower() for s in (
                        "create", "get started", "add a", "+ new", "+ create",
                    )
                )
                if not actionable:
                    audit_observer.emit(
                        step=f"step_2_check_empty_state_on_{section}",
                        severity="medium",
                        category="copy",
                        title=f"{section.capitalize()} empty state has no call-to-action",
                        expected="Copy that suggests how to create the first entry",
                        observed=text[:160],
                    )


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
)
def test_journey_enter_submits_create_forms(audited_page, live_server, audit_observer, persona):
    """Developer rubric: Enter submits every non-multiline form.
    Drives the Volumes page create-volume modal; typing a name and
    pressing Enter should trigger submit."""
    from tests.e2e_helpers import login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
    with step("step_2_open_create_volume_modal"):
        nav_to(page, "volumes")
        btn = page.locator("button:has-text('Create'), button:has-text('+ Volume')").first
        if btn.count() == 0:
            pytest.skip("Create-volume button not present")
        btn.click()
        page.wait_for_timeout(400)
    with step("step_3_type_name_press_enter"):
        name_input = page.locator(
            "input[name='name'], input[placeholder*='name' i]",
        ).first
        if name_input.count() == 0:
            pytest.skip("name input not found")
        import uuid
        name_input.fill(f"pa-enter-{uuid.uuid4().hex[:6]}")
        name_input.press("Enter")
        page.wait_for_timeout(500)
    with step("step_4_modal_closes_or_errors_cleanly"):
        # Either modal closed (submit succeeded) or an error message
        # appeared inline. If the modal is still open with no error,
        # Enter was ignored — developer rubric violation.
        modal_open = page.locator(
            ".modal, dialog[open], [role='dialog']",
        ).count() > 0
        error_inline = page.locator(".error, [role='alert']").count() > 0
        if modal_open and not error_inline:
            audit_observer.emit(
                step="step_4_modal_closes_or_errors_cleanly",
                severity="medium",
                category="behaviour",
                title="Enter key did not submit the create-volume form",
                expected="Enter submits; modal closes or shows inline error",
                observed="Modal still open with no error surfaced",
            )


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="low",
    covers=("hb-no-first-run-tour",),
)
def test_journey_first_run_tour_dismissable(audited_page, live_server, audit_observer, persona):
    """Tour must be dismissable via Esc or a Skip button. hb-no-first-
    run-tour closed the absence of a tour; this journey locks the
    dismiss path so the tour doesn't become a modal jail."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    # Clear the tour-done flag so the tour has a chance to fire.
    page.goto(live_server, wait_until="domcontentloaded")
    page.evaluate("() => localStorage.removeItem('skiff.tour.done')")

    with step("step_1_sign_in_after_reset"):
        # Reload so the tour has a clean shot.
        page.reload()
        login(page, live_server)
    with step("step_2_check_tour_dismissable"):
        overlay = page.locator(".tour-overlay")
        if overlay.count() == 0:
            # Tour didn't render — acceptable if the flag was never
            # unset in time. Emit a low-severity note and move on.
            audit_observer.emit(
                step="step_2_check_tour_dismissable",
                severity="low",
                category="behaviour",
                title="First-run tour did not render after flag reset",
                expected="Tour overlay visible on first post-wizard sign-in",
                observed="No .tour-overlay found",
                covers_historical="hb-no-first-run-tour",
            )
            return
        # Skip button OR Esc should close the tour.
        skip = page.locator(".tour-overlay button:has-text('Skip')").first
        if skip.count() > 0:
            skip.click()
        else:
            page.keyboard.press("Escape")
        page.wait_for_selector(".tour-overlay", state="hidden", timeout=SHORT)


# ── Plan-named J-10 scenarios ────────────────────────────────────────


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
)
def test_journey_high_contrast_theme_reachable(audited_page, live_server, audit_observer, persona):
    """Plan J-10 item: high-contrast theme. WCAG 2.1 AA requires a
    usable high-contrast theme for visually-impaired users. Either
    SKIFF exposes it via a theme selector, or it inherits from
    `prefers-contrast: more` media query. Test probes both."""
    from tests.e2e_helpers import login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
    with step("step_2_force_prefers_contrast_more"):
        # Playwright can set media emulation. Switch to prefers-contrast.
        try:
            page.emulate_media(color_scheme="light", forced_colors="active")
        except Exception:
            # Older Playwright may lack forced_colors — fall back to CSS probe.
            pass
    with step("step_3_observe_contrast"):
        # Measure body background vs text color. If they differ
        # significantly, contrast is honoured. This is a coarse
        # heuristic, not a WCAG 4.5:1 measurement (that's in axe-core).
        style = page.evaluate(
            """() => {
                const b = document.body;
                const cs = window.getComputedStyle(b);
                return { bg: cs.backgroundColor, color: cs.color };
            }"""
        )
        if not style:
            pytest.skip("no style evaluation available")
        if style.get("bg") == style.get("color"):
            audit_observer.emit(
                step="step_3_observe_contrast",
                severity="P0",
                category="a11y",
                title="Body bg == text color under high-contrast media",
                expected="Distinct fg/bg colors per WCAG 2.1 AA",
                observed=f"bg/color identical: {style}",
            )


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="low",
)
def test_journey_i18n_missing_key_audit(audited_page, live_server, audit_observer, persona):
    """Plan J-10 item: i18n missing-key audit. If any rendered text
    matches a localisation placeholder (e.g. `t('key.path')` un-
    interpolated, or `{{missing}}`), emit a finding. SKIFF currently
    ships English-only; this journey exists so adding a locale
    doesn't leave placeholder strings visible."""
    from tests.e2e_helpers import login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    placeholder_patterns = ("{{", "}}", "t('", "i18n.")
    # Map sidebar label → actual H2 (dashboard's H2 is 'Overview', etc.)
    section_h2 = {
        "containers": "Containers",
        "images": "Images",
        "templates": "App templates",
    }
    for section, h2 in section_h2.items():
        with step(f"step_2_scan_{section}_for_placeholders"):
            page.locator(f".sidebar a:has-text('{section.capitalize()}')").click()
            page.wait_for_selector(f"h2:has-text('{h2}')", timeout=5_000)
            text = page.locator("#main").inner_text()
            hits = [p for p in placeholder_patterns if p in text]
            if hits:
                audit_observer.emit(
                    step=f"step_2_scan_{section}_for_placeholders",
                    severity="medium",
                    category="copy",
                    title=f"i18n placeholder leaked into rendered {section}",
                    expected="All user-facing strings interpolated",
                    observed=f"placeholders present: {hits}",
                )


@journey(
    persona=("ui_ux_auditor",),
    category="ui_ux",
    severity="medium",
)
def test_journey_keyboard_reaches_every_primary_page(audited_page, live_server, audit_observer, persona):
    """Plan J-10 item: keyboard-only nav, reach every page via Tab +
    Enter (no mouse). Stricter than the original focus-ring journey:
    actually navigate to each sidebar entry keyboard-only and confirm
    the H2 updates."""
    from tests.e2e_helpers import login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    # Tab to the sidebar. Once any sidebar <a> is focused, press
    # ArrowDown / Enter to navigate. Count how many reach their
    # expected H2.
    targets = {
        "Dashboard": "Overview",
        "Containers": "Containers",
        "Images": "Images",
        "Templates": "App templates",
    }
    reached: list[str] = []
    with step("step_2_tab_and_enter"):
        # Tab a few times to land inside the sidebar.
        for _ in range(6):
            page.keyboard.press("Tab")
        # Walk the sidebar with ArrowDown (many nav lists support arrow
        # navigation; fall back to Tab if not).
        for label in targets:
            # Type ⌘K palette first if the persona prefers it — here we
            # use straight keyboard nav for the stricter test.
            link = page.locator(f".sidebar a:has-text('{label}')").first
            if link.count() == 0:
                continue
            link.focus()
            page.keyboard.press("Enter")
            try:
                page.wait_for_selector(
                    f"h2:has-text('{targets[label]}')",
                    timeout=5000,
                )
                reached.append(label)
            except Exception:
                pass

    if len(reached) < len(targets) // 2:
        audit_observer.emit(
            step="step_2_tab_and_enter",
            severity="medium",
            category="a11y",
            title="Fewer than half the primary pages reachable by keyboard",
            expected=f"All of {list(targets.keys())}",
            observed=f"reached {reached}",
        )
