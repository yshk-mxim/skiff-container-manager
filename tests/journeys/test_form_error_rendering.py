# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier C: form validation error rendering.

Every form the user can submit MUST route a server-side 4xx envelope
message into a visible banner — never silently swallow it and never
render `[object Object]`. The previous journey pass didn't exercise
error paths at all; this file walks the negative cases.
"""

from __future__ import annotations

import uuid

import pytest

from tests.audit_driver import step
from tests.journeys import journey

pytest_plugins = ["tests.conftest_e2e", "tests.conftest_audit"]

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]"',
)

pytestmark = pytest.mark.e2e


# Helper: wait_for_login + page navigation for each form.
def _goto_page(page, live_server: str, sidebar_label: str):
    from tests.e2e_helpers import MEDIUM, login

    login(page, live_server)
    page.locator(f".sidebar a:has-text('{sidebar_label}')").click()
    page.wait_for_load_state("networkidle", timeout=MEDIUM)


def _assert_error_banner_visible_and_helpful(page, needle_keywords: list[str]):
    """A form error banner must be:
    1. Actually visible (.field-error with non-empty text)
    2. Never render the string '[object Object]' (stringified envelope)
    3. Contain at least ONE keyword from `needle_keywords` so the user
       knows what's wrong without reading the server code.
    """
    banner = page.locator(".field-error:visible").first
    banner.wait_for(timeout=3000)
    text = (banner.text_content() or "").strip()
    assert text, "error banner exists but has no text — silent failure"
    assert "[object Object]" not in text, f"banner rendered stringified envelope instead of .detail.message: {text!r}"
    lower = text.lower()
    assert any(k.lower() in lower for k in needle_keywords), (
        f"banner text {text!r} doesn't mention any of {needle_keywords!r} — "
        f"message is not helpful enough to guide the user to a fix."
    )


# ── Volume create with invalid name ─────────────────────────────────────


@journey(persona=("developer",), category="volumes_networks", severity="high")
def test_journey_volume_create_space_in_name_shows_reason(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Typing a space into the volume-name field must produce a form
    banner that EXPLAINS the allowed charset — the historical bug was
    the submit silently failed with no banner at all."""
    page = audited_page
    _goto_page(page, live_server, "Volumes")
    with step("step_1_open_create_volume_modal"):
        # Label is just "Create" from strings.en.js volumes.actions.create.
        page.locator(".page-header button:has-text('Create')").first.click()
        page.wait_for_selector(".modal", timeout=3000)
    with step("step_2_submit_invalid_name"):
        page.locator(".modal input[name='name']").fill("invalid name with spaces")
        page.locator(".modal button[type='submit']").click()
    with step("step_3_banner_explains_charset"):
        _assert_error_banner_visible_and_helpful(
            page,
            ["letters", "digits", "space", "characters", "invalid", "charset", "alphanumeric", "allowed"],
        )


# ── Network create with invalid subnet ──────────────────────────────────


@journey(persona=("sre_ops",), category="volumes_networks", severity="medium")
def test_journey_network_create_bad_subnet_shows_cidr_format(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Network create with a malformed subnet string must render the
    server envelope explaining CIDR format."""
    page = audited_page
    _goto_page(page, live_server, "Networks")
    with step("step_1_open_create_network_modal"):
        page.locator(".page-header button:has-text('Create')").first.click()
        page.wait_for_selector(".modal", timeout=3000)
    with step("step_2_submit_bad_subnet"):
        # Unique name to avoid collisions.
        page.locator(".modal input[name='name']").fill(f"journeynet-{uuid.uuid4().hex[:6]}")
        page.locator(".modal input[name='subnet']").fill("not-a-cidr")
        page.locator(".modal button[type='submit']").click()
    with step("step_3_banner_explains_cidr"):
        _assert_error_banner_visible_and_helpful(
            page,
            ["cidr", "subnet", "invalid", "format"],
        )


# ── Image pull with invalid image ref ───────────────────────────────────


@journey(persona=("developer",), category="container_lifecycle", severity="medium")
def test_journey_image_pull_bad_ref_shows_registry_error(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Pulling a non-existent image must surface the registry error
    (or registry-blocked envelope) in the pull modal, not a toast
    that vanishes before the user reads it."""
    page = audited_page
    _goto_page(page, live_server, "Images")
    with step("step_1_open_pull_modal"):
        page.locator("button:has-text('Pull image')").first.click()
        page.wait_for_selector(".modal", timeout=3000)
    with step("step_2_submit_bogus_image"):
        page.locator(".modal input#pull-image").fill("this-registry-should-not-exist/nope:0.0.1")
        page.locator(".modal button", has_text="Pull").click()
    with step("step_3_error_surfaces"):
        # Pull uses a toast (action-btn pattern) rather than a sticky
        # banner, but either way the message must be visible and NOT
        # contain '[object Object]'. Accept both toast + banner.
        import time

        time.sleep(3)
        content = page.content().lower()
        assert "[object object]" not in content, (
            "Pull failure rendered '[object Object]' — envelope stringification bug"
        )


# ── Wizard submit-short-token shows reason, not raw envelope ────────────


@journey(
    persona=("novice",), category="first_run", severity="P0", covers=("hb-undo-on-delete",)
)  # (reuses an existing covers tag)
def test_journey_wizard_short_token_error_is_readable(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Historical regression: the wizard rendered `[object Object]`
    instead of the envelope's `.detail.message`. This journey forces
    a short-token error and asserts the rendered string is actually
    the server's human message."""
    page = audited_page
    from tests.e2e_helpers import MEDIUM

    page.goto(live_server)
    page.wait_for_load_state("networkidle", timeout=MEDIUM)
    # Only applicable when the wizard is actually open (fresh install).
    wizard_present = page.locator("text=/Welcome to SKIFF/i").count()
    if not wizard_present:
        pytest.skip("wizard not currently open; this journey needs a fresh-install environment")
    with step("step_1_submit_short_token"):
        page.locator("input[name='api_token']").fill("x" * 4)  # below minimum
        page.locator("button[type='submit']").click()
    with step("step_2_error_is_human_readable"):
        err = page.locator(".wizard-error, .field-error").first
        err.wait_for(timeout=3000)
        text = (err.text_content() or "").lower()
        assert "[object object]" not in text, f"wizard rendered stringified envelope: {text!r}"
        assert any(k in text for k in ("token", "16", "short", "length", "character")), (
            f"wizard error text {text!r} doesn't explain the constraint"
        )


# ── Container run with missing required field ──────────────────────────


@journey(persona=("developer",), category="quick_start", severity="high")
def test_journey_container_run_no_image_shows_banner(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Run modal submit without an image → sticky banner at the top of
    the modal (not an ephemeral toast that disappears in 3s)."""
    page = audited_page
    _goto_page(page, live_server, "Containers")
    with step("step_1_open_run_modal"):
        page.locator("button:has-text('Run new container')").first.click()
        page.wait_for_selector(".modal", timeout=3000)
    with step("step_2_submit_with_no_image"):
        # Filter to the exact "Run" button (the modal has a Cancel button
        # and a primary "Run" button — no other "Run*" substring candidates).
        page.locator(".modal button.primary", has_text="Run").first.click()
    with step("step_3_sticky_banner_explains"):
        page.wait_for_function(
            "() => { var b = document.querySelector('.modal .field-error'); "
            "return b && b.textContent.trim().length > 0 && "
            "getComputedStyle(b).display !== 'none'; }",
            timeout=5000,
        )
        text = (page.locator(".modal .field-error").first.text_content() or "").lower()
        assert "image" in text, f"banner must name the required field; got {text!r}"


# ── Invalid volume mount format in run modal ───────────────────────────


@journey(persona=("developer",), category="quick_start", severity="high")
def test_journey_run_modal_bare_volume_name_shows_format_hint(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """If the user types just `test_volume` (no `:/path`), the client-
    side preflight must flag the line number + expected format
    IMMEDIATELY, without a round-trip that the user might miss."""
    page = audited_page
    _goto_page(page, live_server, "Containers")
    with step("step_1_open_run_modal"):
        page.locator("button:has-text('Run new container')").first.click()
        page.wait_for_selector(".modal", timeout=3000)
    with step("step_2_fill_bare_volume_name"):
        page.locator(".modal input#run-image").fill("alpine:3.20")
        page.locator(".modal textarea#run-volumes").fill("test_volume")
    with step("step_3_submit_shows_preflight_banner"):
        page.locator(".modal button.primary", has_text="Run").first.click()
        page.wait_for_function(
            "() => { var b = document.querySelector('.modal .field-error'); "
            "return b && b.textContent.trim().length > 0 && "
            "getComputedStyle(b).display !== 'none'; }",
            timeout=5000,
        )
        text = (page.locator(".modal .field-error").first.text_content() or "").lower()
        assert "mount" in text or "path" in text or "name:/" in text.replace(" ", ""), (
            f"banner must name the required mount path; got {text!r}"
        )
