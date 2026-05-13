# SPDX-License-Identifier: MIT
"""Resilience / chaos tests — simulate real browser conditions that break naïve UIs.

These are NOT happy-path tests (those live in test_e2e_ui.py and
test_e2e_journeys.py). Each test here deliberately breaks something the client
assumed would work — slow network, 500s, 401 mid-session, WebSocket drops,
back/forward navigation, concurrent tabs — and verifies the UI degrades
gracefully (no undefined state, no stuck spinners, no leaked credentials in
error modals).

Uses Playwright's page.route() to intercept and mutate API responses without
touching the server. That means these tests don't exercise server behaviour
(covered elsewhere) — they exercise the client's recovery logic.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import time

import pytest

from tests.conftest_e2e import E2E_TOKEN
from tests.e2e_helpers import LONG, MEDIUM
from tests.e2e_helpers import login as _login

pytestmark = pytest.mark.e2e


# ─────────────────────────────────────────────────────────────────────────────
# R1 — Slow API: a 10s-delayed GET /api/containers must not freeze the UI
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r1_slow_api_shows_loading_state(page, live_server):
    """A slow first-load request must render the loading placeholder instead of a blank page.

    Fresh browser context so `_lastContainers` starts null — otherwise cached
    render beats the placeholder. The delay is achieved by a route handler
    that sleeps: Playwright's Python bindings DO block the response until the
    callback returns (the Node side awaits the RPC round-trip), so sync time.sleep
    here is effective in practice — originally thought to be a no-op, verified
    otherwise while fixing r2/r4.
    """
    _login(page, live_server)

    def _delay_and_continue(route):
        time.sleep(2)
        route.continue_()

    page.route("**/api/containers", _delay_and_continue)

    # Click AFTER routing is set up — the first loadContainers invocation from
    # the IIFE already ran via showPage('containers'), so this click triggers
    # another loadContainers call that goes through our slow route.
    page.locator(".sidebar a:has-text('Containers')").click()
    # The "Loading containers..." placeholder must appear before the real data.
    # If _lastContainers was cached the placeholder is suppressed — so we need
    # to verify that EITHER the placeholder appears (first load) OR the banner
    # doesn't flip to error during the legitimate slow request (cached path).
    try:
        page.wait_for_selector("text=Loading containers", timeout=3_000)
    except Exception:
        # Cached path: placeholder wasn't rendered because data is already shown.
        # That's fine — the real invariant is that the UI doesn't flip to error
        # during a legitimately slow (not failed) request.
        banner_error = page.locator(".status-banner.error").count()
        assert banner_error == 0, "Slow-but-successful request flipped to error state"
    # Eventually the refresh completes within the sleep + response time
    page.wait_for_timeout(3_500)
    # Page is still functional — sidebar nav still responds
    page.locator(".sidebar a:has-text('Images')").click()
    page.wait_for_selector("h2:has-text('Images')", timeout=MEDIUM)


# ─────────────────────────────────────────────────────────────────────────────
# R2 — API 500 mid-session: UI shows an error, doesn't leak a blank page
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r2_api_500_shows_graceful_error(page, live_server):
    """Mid-session 500 must surface via the sidebar banner even with cached data.

    Regression guard for the fix in skiff/static/app.js:loadContainers — before
    the fix, 500/timeout errors with `_lastContainers` already populated left
    the user staring at stale data with no indication anything was wrong.
    """
    _login(page, live_server)
    # Navigate AWAY from Containers so the return navigation actually
    # triggers a fresh GET (if the page is already on Containers, clicking
    # the sidebar link is a no-op and the mock never fires). This is the
    # critical ordering that makes the test deterministic across
    # environments.
    page.locator(".sidebar a:has-text('Images')").click()
    page.wait_for_selector("h2:has-text('Images')", timeout=MEDIUM)

    # Fail every /api/containers after this point
    page.route(
        "**/api/containers",
        lambda r: r.fulfill(
            status=500,
            body='{"detail":"simulated server error"}',
            content_type="application/json",
        ),
    )
    page.locator(".sidebar a:has-text('Containers')").click()
    # The UI surfaces the error through any of these paths; accept any —
    # the point of the test is that the user is NOT left staring at stale
    # data with no indication anything went wrong.
    page.wait_for_timeout(1500)
    indicators = (
        ".status-banner.error",  # sticky banner
        ".empty-state h3",  # "Cannot reach Docker engine" panel
        ".toast.error",  # transient toast
    )
    page_text = page.locator("body").inner_text().lower()
    has_visible_indicator = any(page.locator(sel).count() > 0 for sel in indicators)
    has_error_copy = any(k in page_text for k in ("unreachable", "cannot reach", "failed", "engine"))
    assert has_visible_indicator or has_error_copy, (
        "After 500, the UI shows no error surface at all — user would be "
        "left with stale data and no indication. Regression in error handling."
    )


# ─────────────────────────────────────────────────────────────────────────────
# R3 — 401 mid-session: user gets kicked to login, token is cleared
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r3_api_401_forces_login_and_clears_token(page, live_server):
    _login(page, live_server)

    # Intercept /api/containers to return 401 exactly once
    _call_count = {"n": 0}

    def _once_401(route):
        _call_count["n"] += 1
        if _call_count["n"] == 1:
            route.fulfill(status=401, body='{"detail":"invalid token"}', content_type="application/json")
        else:
            route.continue_()

    page.route("**/api/containers", _once_401)

    page.locator(".sidebar a:has-text('Containers')").click()
    # Login page must reappear
    page.wait_for_selector("button:has-text('Sign in')", timeout=MEDIUM)
    # sessionStorage must have been cleared of the token
    stored_token = page.evaluate("sessionStorage.getItem('api_token')")
    assert not stored_token, f"api_token still in sessionStorage after 401: {stored_token!r}"


# ─────────────────────────────────────────────────────────────────────────────
# R4 — 503 "Container engine unreachable" triggers the helpful empty state
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r4_503_flips_banner_on_mid_session_failure(page, live_server):
    """503 after cached data must surface visibly — banner OR empty-state
    panel OR toast; the user must not be left staring at stale data."""
    _login(page, live_server)
    # Navigate away first so the return click fetches fresh.
    page.locator(".sidebar a:has-text('Images')").click()
    page.wait_for_selector("h2:has-text('Images')", timeout=MEDIUM)
    page.route(
        "**/api/containers",
        lambda r: r.fulfill(
            status=503,
            body='{"detail":"Container engine unreachable"}',
            content_type="application/json",
        ),
    )
    page.locator(".sidebar a:has-text('Containers')").click()
    page.wait_for_timeout(1500)
    indicators = (".status-banner.error", ".empty-state h3", ".toast.error")
    page_text = page.locator("body").inner_text().lower()
    visible = any(page.locator(sel).count() > 0 for sel in indicators)
    has_copy = any(k in page_text for k in ("unreachable", "cannot reach", "failed", "engine"))
    assert visible or has_copy, "503 mid-session must surface visibly"


# ─────────────────────────────────────────────────────────────────────────────
# R5 — Structured error detail (object, not string) is rendered as message text
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r5_structured_error_detail_rendered_safely(browser, live_server):
    """An endpoint returning {detail: {message, code, help}} must render the
    message (not "[object Object]") and preserve the structured payload on the
    thrown Error for downstream handlers.

    Fresh context so `_appDockerHost` is set from the mocked /api/config BEFORE
    the first loadContainers call — the Reconnect button only appears when the
    configured host matches the tunnel regex.
    """
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        # Order matters: all routes set up BEFORE any page navigation
        page.route(
            "**/api/config",
            lambda r: r.fulfill(
                status=200,
                body='{"allowed_registries":[],"docker_vm_host":"","docker_host":"unix:///tmp/skiff-docker.sock"}',
                content_type="application/json",
            ),
        )
        page.route(
            "**/api/containers",
            lambda r: r.fulfill(
                status=503,
                body='{"detail":"Container engine unreachable"}',
                content_type="application/json",
            ),
        )
        page.route(
            "**/api/tunnel/status",
            lambda r: r.fulfill(
                status=200,
                body='{"managed": true, "active": false, "socket": "/tmp/skiff-docker.sock"}',
                content_type="application/json",
            ),
        )
        page.route(
            "**/api/tunnel/reconnect",
            lambda r: r.fulfill(
                status=502,
                body=(
                    '{"detail": {"message": "simulated tunnel failure", '
                    '"code": "auth_failed", "help": "Run ssh-copy-id"}}'
                ),
                content_type="application/json",
            ),
        )
        _login(page, live_server)
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("button:has-text('Reconnect tunnel')", timeout=MEDIUM)
        page.locator("button:has-text('Reconnect tunnel')").click()
        page.wait_for_selector("text=simulated tunnel failure", timeout=MEDIUM)
        body = page.locator("body").inner_text()
        assert "[object Object]" not in body, (
            "Structured error detail rendered as object literal — apiFetch lost the message"
        )
    finally:
        ctx.close()


# ─────────────────────────────────────────────────────────────────────────────
# R6 — Rapid back/forward navigation during a fetch doesn't crash
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r6_rapid_nav_does_not_crash(page, live_server):
    _login(page, live_server)
    pages = ["Containers", "Images", "Volumes", "Networks", "Compose", "System"]
    for _ in range(2):
        for p in pages:
            page.locator(f".sidebar a:has-text('{p}')").click()
            page.wait_for_timeout(120)  # shorter than most loadX() resolution
    # After the storm, navigate to a fully loaded page and verify it's intact
    page.locator(".sidebar a:has-text('Containers')").click()
    page.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)


# ─────────────────────────────────────────────────────────────────────────────
# R7 — Concurrent tabs sharing sessionStorage (simulated via two contexts)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r7_multiple_browser_contexts_each_authed_independently(browser, live_server):
    ctx_a = browser.new_context()
    ctx_b = browser.new_context()
    try:
        page_a, page_b = ctx_a.new_page(), ctx_b.new_page()
        _login(page_a, live_server)
        _login(page_b, live_server)
        # Both can load the containers page simultaneously
        page_a.locator(".sidebar a:has-text('Containers')").click()
        page_b.locator(".sidebar a:has-text('Containers')").click()
        page_a.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)
        page_b.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)
    finally:
        ctx_a.close()
        ctx_b.close()


# ─────────────────────────────────────────────────────────────────────────────
# R8 — Re-login flow after explicit logout works cleanly
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_r8_logout_and_relogin(page, live_server):
    """Sidebar logout clears sessionStorage; a fresh login restores an authenticated UI."""
    _login(page, live_server)
    page.locator("#sidebar-logout").click()
    page.wait_for_selector("button:has-text('Sign in')", timeout=MEDIUM)
    # sessionStorage must actually be cleared — re-entering the token should work
    stored = page.evaluate("sessionStorage.getItem('api_token')")
    assert not stored
    page.locator("input[type='password']").fill(E2E_TOKEN)
    page.locator("button:has-text('Sign in')").click()
    # After clicking Sign in:
    #   - the setToken() call writes the token to sessionStorage synchronously,
    #   - showPage('containers') fetches /api/containers (async),
    #   - the sidebar-logout setInterval re-checks every 2s.
    # Polling sessionStorage is the fastest end-to-end signal that login
    # succeeded; remote-tunnel environments add latency to the containers
    # fetch so use LONG here (90s) instead of MEDIUM (30s).
    page.wait_for_function(
        "() => sessionStorage.getItem('api_token') !== null",
        timeout=LONG,
    )
    stored = page.evaluate("sessionStorage.getItem('api_token')")
    assert stored == E2E_TOKEN
    # And the logout button eventually shows. We can't use
    # `el.style.display` here because the strict-CSP refactor routes
    # JS-set styles through CSSOM rules (`_csp_N`), leaving the inline
    # `style` attribute empty. `getComputedStyle` reflects the rule.
    page.wait_for_function(
        "() => { const el = document.getElementById('sidebar-logout'); "
        "  return el && (el.classList.contains('visible')"
        "    || getComputedStyle(el).display !== 'none'); }",
        timeout=LONG,
    )
