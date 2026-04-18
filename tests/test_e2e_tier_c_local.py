# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier C — mid-session operator flows that exist only in
session-configured mode (wizard-started, not env-started).

Distinct from Tier A/B: those use the shared `live_server` which has
`API_TOKEN` set via env → `from_env=True` → `POST /api/auth/rotate-token`
and `POST /api/auth/reset-config` are gated off. The flows below cover
the operator journey that starts with the wizard:

  - rotate-token actually invalidates the old token mid-session
  - reset-config reopens the 5-min setup window
  - two tabs: rotating in tab A leaves tab B signed-out on next action
  - reload during an open WS doesn't leave a zombie server-side session
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import time

import pytest
import requests

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e

from tests.conftest_e2e import E2E_DOCKER_HOST
from tests.e2e_helpers import SHORT


def _complete_wizard(url: str, token: str) -> None:
    """POST /api/setup with the given token so the server is
    session-configured (from_env=False). Caller can then rotate
    or reset-config like a real wizard-started operator would."""
    r = requests.post(
        f"{url}/api/setup",
        headers={"X-Requested-With": "ContainerManager"},
        json={
            "docker_host": E2E_DOCKER_HOST,
            "api_token": token,
            "allowed_registries": "docker.io,ghcr.io",
        },
        timeout=10,
    )
    assert r.status_code == 200, f"/api/setup failed: {r.status_code} {r.text[:200]}"


# ── C1. Token rotation (session-configured) → old token invalidated ─────


def test_c1_token_rotation_invalidates_old_token(isolated_server):
    """Start with no API_TOKEN, wizard through, rotate — and assert the
    OLD token no longer authenticates while the NEW one does. Confirms
    rotation actually swaps the in-memory token (not just the UI copy)."""
    url, _proc = isolated_server({"API_TOKEN": ""})
    first_token = "c1-first-token-abcdefghijklmnopqr"
    second_token = "c1-second-token-abcdefghijklmnopqrs"
    _complete_wizard(url, first_token)

    # First token works.
    r = requests.get(
        f"{url}/api/containers",
        headers={"Authorization": f"Bearer {first_token}", "X-Requested-With": "ContainerManager"},
        timeout=5,
    )
    assert r.status_code == 200, f"first token should auth: {r.status_code}"

    # Rotate.
    r = requests.post(
        f"{url}/api/auth/rotate-token",
        headers={"Authorization": f"Bearer {first_token}", "X-Requested-With": "ContainerManager"},
        json={"new_token": second_token},
        timeout=5,
    )
    assert r.status_code == 200, f"rotate-token failed: {r.status_code} {r.text[:200]}"

    # First token rejected.
    r = requests.get(
        f"{url}/api/containers",
        headers={"Authorization": f"Bearer {first_token}", "X-Requested-With": "ContainerManager"},
        timeout=5,
    )
    assert r.status_code == 401, f"old token must be rejected, got {r.status_code}"

    # Second token works.
    r = requests.get(
        f"{url}/api/containers",
        headers={"Authorization": f"Bearer {second_token}", "X-Requested-With": "ContainerManager"},
        timeout=5,
    )
    assert r.status_code == 200, f"new token should auth: {r.status_code}"


# ── C2. Reset-config reopens the wizard + clears in-memory state ─────────


def test_c2_reset_config_reopens_wizard(isolated_server):
    """After /api/auth/reset-config, the server returns to unconfigured
    state, /api/setup-state reports `window_open=true` again, and the
    old token no longer authenticates. This flow hands off a running
    instance to another operator without restarting."""
    url, _proc = isolated_server({"API_TOKEN": "", "SETUP_WINDOW_SECS": "600"})
    original_token = "c2-token-aaaaaaaaaaaaaaaaaaaaaaa"
    _complete_wizard(url, original_token)

    headers = {"Authorization": f"Bearer {original_token}", "X-Requested-With": "ContainerManager"}
    # Baseline: setup-state reports configured.
    r = requests.get(f"{url}/api/setup-state", timeout=5)
    assert r.json()["configured"] is True

    # Reset.
    r = requests.post(f"{url}/api/auth/reset-config", headers=headers, timeout=5)
    assert r.status_code == 200, f"reset-config failed: {r.status_code} {r.text[:200]}"

    # Wizard reopens.
    r = requests.get(f"{url}/api/setup-state", timeout=5)
    body = r.json()
    assert body["configured"] is False
    assert body.get("window_open") is True, f"setup window should have reopened: {body}"
    assert body.get("window_expires_in", 0) > 0

    # Old token rejected OR docker host cleared — either means the
    # session is unusable, which is the operator contract. reset-config
    # clears both the token AND the docker_host, so /api/containers can
    # surface as 401 (auth drop) or 503 (docker host gone) depending on
    # which middleware fires first.
    r = requests.get(f"{url}/api/containers", headers=headers, timeout=5)
    assert r.status_code in (401, 503), f"old token must be unusable post-reset (401 or 503), got {r.status_code}"


# ── C3. Two tabs — rotate in one leaves the other signed out ─────────────


def test_c3_two_tabs_rotate_token_signs_out_other(browser, isolated_server):
    """Two browser contexts both signed in with the same token. Tab A
    rotates. Tab B's next authenticated fetch MUST return 401 so the
    UI redirects to sign-in rather than silently failing mid-click."""
    url, _proc = isolated_server({"API_TOKEN": ""})
    first_token = "c3-first-token-aaaaaaaaaaaaaaaaaaaaa"
    second_token = "c3-second-token-bbbbbbbbbbbbbbbbbbbbb"
    _complete_wizard(url, first_token)

    ctx_a = browser.new_context()
    ctx_b = browser.new_context()
    try:
        # Prime both tabs with the first token in sessionStorage.
        for ctx in (ctx_a, ctx_b):
            pg = ctx.new_page()
            pg.goto(url)
            pg.wait_for_selector("button:has-text('Sign in')", timeout=SHORT)
            pg.locator("input[type='password']").fill(first_token)
            pg.locator("button:has-text('Sign in')").click()
            pg.wait_for_selector(".sidebar", timeout=SHORT)

        # Tab A rotates via API (faster than driving the UI).
        r = requests.post(
            f"{url}/api/auth/rotate-token",
            headers={"Authorization": f"Bearer {first_token}", "X-Requested-With": "ContainerManager"},
            json={"new_token": second_token},
            timeout=5,
        )
        assert r.status_code == 200

        # Tab B's next authenticated fetch (any containers/config/etc)
        # must come back 401. Force a fetch via the apiFetch wrapper.
        pg_b = ctx_b.pages[0]
        status = pg_b.evaluate("""
            async () => {
                try {
                    await window.apiFetch('/api/containers');
                    return 200;
                } catch (e) {
                    // apiFetch throws on 401; the wrapper also calls
                    // showLogin() which clears session storage.
                    return e.message.includes('Authentication required') ? 401 : 500;
                }
            }
        """)
        assert status == 401, f"tab B's fetch should have hit 401 after rotation; got {status}"
        # And the UI should show the sign-in surface (apiFetch calls
        # showLogin which swaps #main to a sign-in box).
        pg_b.wait_for_selector("button:has-text('Sign in')", timeout=SHORT)
    finally:
        ctx_a.close()
        ctx_b.close()


# ── C4. Browser reload during active WS → no zombie session ──────────────


def test_c4_reload_during_ws_cleans_up_server_side(page, live_server, docker_client):
    """Open an exec WS, then reload the browser. Assert the server's
    active-exec-session registry shrinks (no zombie session pinned
    by the old WS) — verifiable by opening a new WS on the same
    container without tripping the per-container exec cap.

    Before this was tested, a server-side leak could hold a stale
    session until WS_KEEPALIVE_INTERVAL x MAX_MISSED detected it,
    giving the user a cap-hit error on their next exec attempt."""
    from tests.e2e_helpers import SHORT, login, nav_to, teardown_container

    name = "e2e-c4-reload-ws"
    teardown_container(docker_client, name)
    docker_client.containers.run("alpine:latest", command="sleep 600", name=name, detach=True)
    page.set_default_timeout(20_000)
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)
        # Use the nav-dance to avoid the (now-fixed) refresh-race that
        # could previously swallow the Terminal click.
        page.locator(".sidebar a:has-text('Images')").click()
        page.wait_for_selector("h2:has-text('Images')", timeout=SHORT)
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("h2:has-text('Containers')", timeout=SHORT)
        page.locator(f"tr:has-text('{name}')").locator("button:has-text('Terminal')").first.click()
        page.wait_for_selector("#term-output", timeout=SHORT)
        page.wait_for_timeout(800)

        # Hard-reload the page — simulates the user hitting F5 without
        # clicking Disconnect. The browser tears down the WS without a
        # clean close frame.
        page.reload()
        page.wait_for_selector(".sidebar", timeout=SHORT)

        # Give the server a few seconds to reap the dead WS. Then try
        # to open a NEW exec WS on the same container. If the zombie
        # session is still holding the per-container slot (MAX_EXEC_PER_CONTAINER),
        # the new attach will fail. If the fix (or existing cleanup) works,
        # the new attach succeeds.
        page.wait_for_timeout(2_000)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)
        page.locator(".sidebar a:has-text('Images')").click()
        page.wait_for_selector("h2:has-text('Images')", timeout=SHORT)
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("h2:has-text('Containers')", timeout=SHORT)
        page.locator(f"tr:has-text('{name}')").locator("button:has-text('Terminal')").first.click()
        page.wait_for_selector("#term-output", timeout=SHORT)
        # If we reach this line, the new WS attached cleanly — no zombie
        # blocking the slot. Type a command to prove the PTY is live.
        term_input = page.locator("input.terminal-input")
        term_input.wait_for(state="visible", timeout=SHORT)
        page.wait_for_timeout(600)
        term_input.fill("echo C4OKRELOAD")
        page.keyboard.press("Enter")
        deadline = time.time() + 5
        while time.time() < deadline:
            if "C4OKRELOAD" in page.locator("#term-output").inner_text():
                break
            page.wait_for_timeout(200)
        else:
            pytest.fail("post-reload exec session didn't echo — zombie WS may be holding the slot")
    finally:
        teardown_container(docker_client, name)
