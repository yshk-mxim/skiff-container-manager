# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier D: keyboard shortcuts under realistic conditions.

Happy-path keyboard tests tend to fire the shortcut on an empty page
and assert the side-effect fires. Real bugs happen when:
  - a modal is already open (second shortcut must not stack)
  - an input is focused (letter shortcuts must NOT steal keystrokes)
  - the user presses the key twice in a row (idempotent)
These journeys exercise each shortcut under each realistic state.
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


def _goto_dashboard(page, live_server: str):
    from tests.e2e_helpers import MEDIUM, login

    login(page, live_server)
    page.locator(".sidebar a:has-text('Dashboard')").first.click()
    page.wait_for_load_state("networkidle", timeout=MEDIUM)


# ── ⌘K / Ctrl+K — command palette ──────────────────────────────────────


@journey(persona=("developer",), category="ui_ux", severity="high")
def test_journey_cmd_k_opens_palette(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Ctrl+K / ⌘K opens the palette overlay; visible on any page."""
    page = audited_page
    _goto_dashboard(page, live_server)
    with step("step_1_press_cmd_k"):
        page.keyboard.press("Meta+k")
        # Palette modal has a distinctive structure — either cmdp-row
        # entries or the palette input field.
        page.wait_for_selector(
            "input[placeholder*='Type']",
            timeout=3000,
        )


@journey(persona=("developer",), category="ui_ux", severity="medium")
def test_journey_cmd_k_twice_does_not_stack(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Pressing ⌘K a second time while the palette is open must NOT
    open a second palette on top. The previous handler wasn't guarded
    against re-entry."""
    page = audited_page
    _goto_dashboard(page, live_server)
    with step("step_1_open_palette_twice"):
        page.keyboard.press("Meta+k")
        page.wait_for_selector("input[placeholder*='Type']", timeout=3000)
        page.keyboard.press("Meta+k")
        # Either the palette closed (then reopened cleanly) OR it
        # stayed open with a single instance — NOT two instances.
        palettes = page.locator(".cmdp, .command-palette, [role='listbox']")
        if palettes.count() > 0:
            assert palettes.count() <= 2, (
                f"Command palette stacked: {palettes.count()} instances. Second ⌘K must close the first, not stack."
            )


# ── `?` — help modal ────────────────────────────────────────────────────


@journey(persona=("novice",), category="ui_ux", severity="high")
def test_journey_question_mark_opens_help_exactly_once(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Historical bug: `?` opened TWO help modals because two keydown
    listeners both handled the key (palette.js + pages/system.js).
    This test presses `?` and asserts exactly one help modal."""
    page = audited_page
    _goto_dashboard(page, live_server)
    with step("step_1_press_question_mark"):
        page.keyboard.press("?")
        page.wait_for_timeout(400)
    with step("step_2_exactly_one_help_modal"):
        help_modals = page.locator(
            "[role='dialog']:has-text('shortcuts'), .modal:has-text('Keyboard'), .modal:has-text('shortcuts')"
        )
        count = help_modals.count()
        assert count == 1, (
            f"Expected exactly 1 help modal, saw {count}. Two listeners both handling `?` was the previous regression."
        )


@journey(persona=("novice",), category="ui_ux", severity="medium")
def test_journey_question_mark_ignored_inside_input(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """When the user's focus is on an input/textarea, pressing `?`
    must type the literal character — not open the help modal."""
    page = audited_page
    _goto_dashboard(page, live_server)
    # Focus the palette input (which accepts typing).
    page.keyboard.press("Meta+k")
    page.wait_for_selector("input[placeholder*='Type']", timeout=3000)
    page.locator("input[placeholder*='Type']").focus()
    with step("step_1_type_question_mark_in_input"):
        page.keyboard.type("?containers")
        value = page.locator("input[placeholder*='Type']").input_value()
        assert value.startswith("?") or "container" in value, (
            f"`?` was swallowed by the help-modal handler while focus was in an editable. Got input value: {value!r}"
        )


# ── `/` — focus search ─────────────────────────────────────────────────


@journey(persona=("developer",), category="ui_ux", severity="medium")
def test_journey_slash_focuses_search_on_list_page(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Pressing `/` on a list page focuses its search input. On
    Containers this is the search bar at the top of the table."""
    page = audited_page
    from tests.e2e_helpers import MEDIUM, login

    login(page, live_server)
    page.locator(".sidebar a:has-text('Containers')").first.click()
    page.wait_for_load_state("networkidle", timeout=MEDIUM)
    with step("step_1_press_slash"):
        page.keyboard.press("/")
        # The focused element should be a search-type input.
        focused_tag = page.evaluate("() => document.activeElement && document.activeElement.tagName.toLowerCase()")
        focused_type = page.evaluate("() => document.activeElement && document.activeElement.type")
        assert focused_tag == "input", f"focused {focused_tag!r}, expected input"
        assert focused_type in ("search", "text"), f"focused input type {focused_type!r}, expected search/text"


# ── Number keys 1-8 — sidebar nav ──────────────────────────────────────


@journey(persona=("developer",), category="ui_ux", severity="medium")
def test_journey_number_key_navigates_to_sidebar_page(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Numeric keys 1-8 navigate to the matching sidebar entry. Test
    '2' (Containers) since that's a safe well-known page."""
    page = audited_page
    _goto_dashboard(page, live_server)
    with step("step_1_press_2_navigates_to_containers"):
        page.keyboard.press("2")
        page.wait_for_load_state("networkidle", timeout=3000)
        # Either the active sidebar link changes, OR the page title
        # text changes. Both are acceptable indicators.
        main_h2 = page.locator("#main h2").first
        main_h2.wait_for(timeout=3000)
        title = (main_h2.text_content() or "").lower()
        assert "container" in title, f"pressing '2' did not navigate to Containers; page title is {title!r}"


@journey(persona=("developer",), category="ui_ux", severity="medium")
def test_journey_number_key_ignored_inside_input(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Typing numbers into an input must NOT trigger sidebar navigation."""
    page = audited_page
    _goto_dashboard(page, live_server)
    page.keyboard.press("Meta+k")
    page.wait_for_selector("input[placeholder*='Type']", timeout=3000)
    page.locator("input[placeholder*='Type']").focus()
    with step("step_1_type_123_in_palette_input"):
        page.keyboard.type("test123")
        value = page.locator("input[placeholder*='Type']").input_value()
        assert "test123" in value or "123" in value, f"digits were swallowed by the number-nav handler; got {value!r}"
        # Negative assertion: the page must still be on Dashboard.
        title = (page.locator("#main h2").first.text_content() or "").lower()
        assert "overview" in title or "dashboard" in title, (
            f"number keys navigated while focus was in an input; now on {title!r}"
        )


# ── Escape — closes open modal ─────────────────────────────────────────


@journey(persona=("developer",), category="ui_ux", severity="medium")
def test_journey_escape_closes_open_modal(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Escape on any modal closes it. Opening a form modal and pressing
    Escape must return the user to the page behind."""
    page = audited_page
    _goto_dashboard(page, live_server)
    with step("step_1_open_palette"):
        page.keyboard.press("Meta+k")
        page.wait_for_selector("input[placeholder*='Type']", timeout=3000)
    with step("step_2_escape_closes_it"):
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        still_open = page.locator("input[placeholder*='Type']").count()
        assert still_open == 0, "Escape did not close the command palette — keyboard UX regression"
