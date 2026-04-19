# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Shared helpers for Playwright e2e tests.

Every e2e file was growing its own `_login`, `_nav_to`, `_teardown_container`,
`_auth_headers` — subtle drift (one waited for `.sidebar`, another for
`h2:has-text('Containers')`) made cross-file refactors brittle. This module
is the one place they live.
"""

from __future__ import annotations

from typing import Any

import requests

from tests.conftest_e2e import BASE_URL, E2E_TOKEN

# Standard Playwright timeouts in milliseconds
SHORT = 10_000
MEDIUM = 30_000
LONG = 90_000


def auth_headers() -> dict[str, str]:
    """Bearer + CSRF headers for direct requests against the e2e server."""
    return {
        "X-Requested-With": "ContainerManager",
        "Authorization": f"Bearer {E2E_TOKEN}",
    }


def login(page: Any, live_server: str, token: str | None = None) -> None:
    """Open the live server and authenticate if a Sign in page is shown.

    Idempotent: safe to call when the sidebar is already visible.
    Dismisses the first-run tour overlay if it appears — otherwise its
    modal backdrop intercepts every click in the sidebar, blocking
    both tests and real users who want to dive in without the tour.
    """
    # Pre-set the tour-done flag so a fresh browser context doesn't
    # show the 4-step walkthrough (real users see it once and dismiss;
    # tests see it every time because localStorage is per-context).
    page.goto(live_server, wait_until="domcontentloaded")
    try:
        page.evaluate("() => localStorage.setItem('skiff.tour.done', '1')")
    except Exception:
        pass  # pre-login origin may not permit localStorage yet
    page.wait_for_selector("button:has-text('Sign in'), .sidebar", timeout=MEDIUM)
    if page.locator("button:has-text('Sign in')").count() > 0:
        page.locator("input[type='password']").fill(token or E2E_TOKEN)
        page.locator("button:has-text('Sign in')").click()
        page.wait_for_selector(".sidebar", timeout=MEDIUM)
    # Belt + braces: if the tour still rendered (page loaded before the
    # localStorage set took effect, or tour.js raced the read), dismiss
    # via the Skip button or Esc.
    tour = page.locator(".tour-overlay")
    if tour.count() > 0 and tour.first.is_visible():
        skip = page.locator(".tour-overlay button:has-text('Skip')")
        if skip.count() > 0:
            skip.first.click()
        else:
            page.keyboard.press("Escape")
        try:
            tour.first.wait_for(state="hidden", timeout=SHORT)
        except Exception:
            pass


def nav_to(page: Any, section: str) -> None:
    """Click a sidebar link and wait for the section's H2 to appear.

    Standardised on the H2 selector (test_e2e_ui_gaps originally used this;
    test_e2e_ui used `.sidebar` only which could race).
    """
    page.locator(f".sidebar a:has-text('{section.capitalize()}')").click()
    page.wait_for_selector(f"h2:has-text('{section.capitalize()}')", timeout=MEDIUM)


def teardown_container(docker_client: Any, name: str) -> None:
    """Best-effort: remove any container with this exact name. Used in
    test fixtures to guarantee a clean slate before running."""
    try:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)
    except Exception:
        pass


def teardown_compose_stack(project_name: str) -> None:
    """Best-effort: POST /api/compose/down for a project, ignore errors."""
    try:
        requests.post(
            f"{BASE_URL}/api/compose/down?project_name={project_name}",
            headers=auth_headers(),
            timeout=60,
        )
    except requests.exceptions.RequestException:
        pass


def deploy_compose_stack(project_name: str, yaml: bytes) -> None:
    """Upload a compose file and assert successful deploy via the HTTP API."""
    files = [("file", ("docker-compose.yml", yaml, "application/x-yaml"))]
    r = requests.post(
        f"{BASE_URL}/api/compose/up?project_name={project_name}",
        headers=auth_headers(),
        files=files,
        timeout=120,
    )
    assert r.status_code == 200, f"compose up failed: {r.status_code} {r.text}"


# ── Terminal (xterm.js) helpers ──────────────────────────────────────────────
# Starting with v1.0.1 the exec terminal is an xterm.js Terminal, not a plain
# <input>. Tests that type into the terminal should use these helpers so the
# input method matches the rendered widget.


def term_send(page: Any, text: str) -> None:
    """Type keystrokes into the currently-mounted xterm.js terminal.
    Uses `page.keyboard.type` after focusing `.xterm` so xterm's
    `onData` fires for each character (arrow keys, Ctrl-C, etc. would
    go through `page.keyboard.press('ArrowUp')` / `.press('Control+C')`
    on the same focused element).
    """
    page.locator(".xterm-helper-textarea, .xterm").first.focus()
    page.keyboard.type(text)


def term_read(page: Any) -> str:
    """Read the visible terminal text — reliable across xterm.js
    renderer backends (canvas vs DOM). Uses xterm's own active buffer
    as the source of truth rather than inner_text on the host div,
    which is noisy once xterm paints overlay rows.
    """
    return page.evaluate(
        """() => {
            const el = document.getElementById('term-output');
            if (!el || !el._term) return (el?.innerText || '');
            const t = el._term;
            const buf = t.buffer.active;
            const lines = [];
            for (let i = 0; i < buf.length; i++) {
                const line = buf.getLine(i);
                if (line) lines.push(line.translateToString(true));
            }
            return lines.join('\\n');
        }"""
    )
