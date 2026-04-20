# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""E2E smoke tests for the Settings page (config viewer).

Requirements the page must uphold in the browser — each covered here:
  1. Navigable via the sidebar "Settings" link; URL round-trips.
  2. Renders one row per exposed knob, grouped by section.
  3. The ENV / TOML / DEFAULT / UNSET source badges are present and
     correct for at least one well-known knob (SESSION_IDLE_SECS).
  4. Search box filters name + doc substring live (no reload).
  5. Secret knobs never appear with a raw value — the page uses the
     "(redacted)" placeholder instead.
  6. The editable-vs-restart badge renders for every row so the operator
     can't mistake a read-only viewer for a control surface.

This is a UI smoke test. Content-correctness (does the viewer show ALL
knobs?) is covered by the unit test in tests/test_config_precedence.py;
this test only validates the browser rendering + interactive filter.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import pytest

from tests.e2e_helpers import MEDIUM
from tests.e2e_helpers import login as _login

pytestmark = pytest.mark.e2e


def _open_settings(page):
    page.locator(".sidebar a:has-text('Settings')").click()
    page.wait_for_selector("h2:has-text('Settings')", timeout=MEDIUM)
    # Wait for the table to populate — paint() only renders once the
    # /api/config/knobs fetch resolves.
    page.wait_for_selector(".settings-row", timeout=MEDIUM)


def test_settings_page_reachable_and_renders_groups(live_server, page):
    _login(page, live_server)
    _open_settings(page)
    # At least a few distinct section headers visible.
    group_count = page.locator(".settings-group").count()
    assert group_count >= 5, (
        f"Expected multiple knob sections; saw {group_count}. Either the "
        f"config.py section headers regressed or the parser is off."
    )
    # Row count roughly matches the exposed-knob count; we don't pin
    # exactly because that couples the test to the current knob inventory.
    row_count = page.locator(".settings-row").count()
    assert row_count >= 30, f"Expected most exposed knobs to render; saw {row_count}."


def test_settings_page_shows_source_badges_for_session_idle(live_server, page):
    _login(page, live_server)
    _open_settings(page)
    # SESSION_IDLE_SECS is exposed + present in defaults.toml — no env
    # override in the test server, so its badge must be TOML (never env
    # when the test harness runs without setting SESSION_IDLE_SECS).
    row = page.locator(".settings-row:has(.settings-name-id:text-is('SESSION_IDLE_SECS'))")
    row.wait_for(timeout=MEDIUM)
    source_text = row.locator(".settings-badge-source").text_content() or ""
    assert source_text.strip() == "TOML", (
        f"SESSION_IDLE_SECS source badge should be TOML when no env override "
        f"is set; saw {source_text!r}. If the test runner sets the env var, "
        f"adjust the test fixture to un-set it."
    )


def test_settings_page_search_filters_live(live_server, page):
    _login(page, live_server)
    _open_settings(page)
    baseline = page.locator(".settings-row").count()
    page.locator("[data-testid='settings-search']").fill("session")
    # Give the input listener a tick to re-paint.
    page.wait_for_timeout(150)
    filtered = page.locator(".settings-row").count()
    assert 0 < filtered < baseline, (
        f"Search for 'session' should narrow but not eliminate results — "
        f"baseline={baseline} filtered={filtered}. The live filter is "
        f"either broken or the doc text is missing."
    )
    # Every surviving row has 'session' in its name OR doc (case-insensitive).
    for idx in range(filtered):
        row = page.locator(".settings-row").nth(idx)
        text = (row.text_content() or "").lower()
        assert "session" in text, (
            f"Row {idx} survived the 'session' filter but its text {text!r} "
            f"doesn't contain the needle — the filter is broken."
        )


def test_settings_page_never_shows_api_token_value(live_server, page):
    """API_TOKEN is marked secret+unexposed in config.py, so it should NOT
    appear in the knob viewer at all. This test guards against a future
    refactor that flips expose=True while leaving secret=True — the
    server response would redact to null, but the row name would still
    be present. Either state is a leak; block both."""
    _login(page, live_server)
    _open_settings(page)
    # The API_TOKEN name must not appear anywhere in the viewer.
    count = page.locator(".settings-row:has(.settings-name-id:text-is('API_TOKEN'))").count()
    assert count == 0, "API_TOKEN leaked into the Settings viewer"


def test_settings_page_every_row_has_exactly_one_edit_status_badge(live_server, page):
    """Every knob row must disclose its edit status (LIVE / SECURITY /
    LIFECYCLE) so operators aren't misled about whether an edit control
    will actually work. A missing badge or two badges on one row breaks
    the "either edit or show why not" invariant."""
    _login(page, live_server)
    _open_settings(page)
    rows = page.locator(".settings-row").count()
    for idx in range(rows):
        row = page.locator(".settings-row").nth(idx)
        live = row.locator(".settings-badge-live").count()
        security = row.locator(".settings-badge-security").count()
        lifecycle = row.locator(".settings-badge-lifecycle").count()
        total = live + security + lifecycle
        assert total == 1, (
            f"Row {idx} has live={live} security={security} lifecycle={lifecycle} — "
            f"exactly one edit-status badge is required."
        )
