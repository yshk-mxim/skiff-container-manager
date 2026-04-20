# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""E2E regressions for states that used to expire silently.

Seven time-bounded states in SKIFF were audited; each previously failed
the "user learns by getting an error on their next click" test. This
file asserts that each state now paints a visible banner as soon as the
server-side window changes, not on the next request attempt.

Covered here:
1. Setup wizard 5-minute window expiry → banner + disabled Submit.
2. Per-IP setup-lockout after 3 failed POSTs → banner with remaining seconds.
3. Rate-limit 429 → banner with Retry-After countdown.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import pytest
import requests

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e


# ── Setup window expiry ──────────────────────────────────────────────────


def test_setup_window_expired_banner(page, browser, isolated_server):
    """Boot SKIFF with a 3-second SETUP_WINDOW_SECS, wait 4s, and assert
    the wizard paints the expired banner + disables the Submit button.
    Page-load tests never caught this because the form renders fine —
    the failure was only visible on the next POST attempt."""
    url, _proc = isolated_server(
        {
            "SETUP_WINDOW_SECS": "3",
            "API_TOKEN": "",  # ensure wizard is reachable at boot
        }
    )
    # Fresh context so sessionStorage / polling state doesn't cross tests.
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_navigation_timeout(8_000)
    try:
        pg.goto(url, wait_until="domcontentloaded")
        # Wizard renders immediately; wait for the Submit button to confirm.
        pg.wait_for_selector("#sw-btn-save", timeout=5_000)
        # Now wait past the window.
        pg.wait_for_timeout(4_000)
        # Force an immediate poll so we don't have to wait for the 10s
        # timer tick. `_pollSetupState` is a top-level function in
        # wizard.js and is attached to `window` as a side-effect of
        # script-tag declaration. Awaiting here ensures _applyWizardState
        # has run before the banner-state assertions below.
        pg.evaluate("window._pollSetupState && window._pollSetupState()")
        pg.wait_for_timeout(500)
        banner_text = pg.locator("#status-banner").inner_text()
        assert "expired" in banner_text.lower() or "restart" in banner_text.lower(), (
            f"Setup-window-expired banner missing or wrong copy: {banner_text!r}"
        )
        submit = pg.locator("#sw-btn-save")
        assert submit.is_disabled(), "Submit should be disabled when the window has expired"
    finally:
        ctx.close()


# ── Setup lockout banner ─────────────────────────────────────────────────


def test_setup_lockout_surfaces_remaining(page, browser, isolated_server):
    """After 3 bad /api/setup POSTs from the caller's IP, reload the
    wizard and assert the banner shows the remaining lockout seconds.
    Without this the operator's next click was rejected with a cryptic
    429 and no hint of how long to wait."""
    url, _proc = isolated_server(
        {
            "SETUP_WINDOW_SECS": "300",
            "API_TOKEN": "",
            "SETUP_MAX_ATTEMPTS": "3",
            "SETUP_LOCKOUT_SECS": "120",
        }
    )
    ctx = browser.new_context()
    pg = ctx.new_page()
    try:
        # Prime the per-IP failure counter with 3 bad POSTs. The counter
        # persists on the server side keyed by client IP (testclient from
        # Starlette, 127.0.0.1 here via requests) — reloading the wizard
        # then surfaces the active lockout via /api/setup-state.
        headers = {"X-Requested-With": "ContainerManager"}
        for _ in range(3):
            try:
                requests.post(
                    f"{url}/api/setup",
                    headers=headers,
                    json={"docker_host": "unix:///var/run/docker.sock", "api_token": "short", "allowed_registries": ""},
                    timeout=3,
                )
            except requests.exceptions.RequestException:
                pass
        pg.goto(url, wait_until="domcontentloaded")
        pg.wait_for_selector("#sw-btn-save", timeout=5_000)
        # Force a fresh setup-state fetch so the banner paints without
        # waiting for the next poll tick.
        pg.evaluate("fetch('/api/setup-state').then(r => r.json())")
        pg.wait_for_timeout(800)
        banner_text = pg.locator("#status-banner").inner_text().lower()
        assert "failed setup attempts" in banner_text or "try again in" in banner_text, (
            f"Setup-lockout banner missing: {banner_text!r}"
        )
    finally:
        ctx.close()


# ── 429 Retry-After surfacing ────────────────────────────────────────────


def test_retry_after_parsed_into_banner(page, live_server):
    """apiFetch parses the Retry-After header on 429 and surfaces a
    banner with the remaining seconds. Uses `page.route` to mock a 429
    response deterministically — the real server-side rate limit is
    already covered by existing tier tests; the property we care about
    here is the CLIENT-SIDE parser + banner wiring."""
    # Mock exactly one 429 with a known Retry-After header so the assertion
    # can match an exact value. Subsequent requests pass through normally.
    matched = {"count": 0}

    def handler(route):
        if matched["count"] == 0:
            matched["count"] += 1
            route.fulfill(
                status=429,
                headers={"Retry-After": "47"},
                body='{"detail":{"code":"auth.rate_limited","message":"too many requests (60 per minute)"}}',
                content_type="application/json",
            )
        else:
            route.continue_()

    page.route("**/api/config", handler)
    page.goto(live_server)
    # Hitting /api/config on page load triggers the mocked 429 via apiFetch.
    # Wait for the banner to contain the parsed Retry-After value.
    page.wait_for_function(
        "() => (document.getElementById('status-banner')?.innerText || '').includes('retry in 47')",
        timeout=10_000,
    )
    banner_text = page.locator("#status-banner").inner_text().lower()
    assert "retry in 47" in banner_text, f"Rate-limited banner missing/wrong countdown: {banner_text!r}"
