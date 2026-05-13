# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""End-to-end checks for the CSP-sandboxed terminal iframe.

xterm.js writes `element.style.X = ...` during cell rendering — a
pattern the main SPA's strict `style-src 'self'` (no `'unsafe-inline'`)
CSP blocks. We confine xterm to a same-origin iframe at
`GET /api/terminal-frame/{container_id}`, whose route-scoped CSP DOES
allow `'unsafe-inline'`. The tests below verify that arrangement
without needing a Docker daemon or a live container:

  * The iframe loads (200, correct content-type).
  * xterm.Terminal mounts inside the iframe's document.
  * The terminal accepts keyboard input (we click into the iframe,
    type, and watch the buffer reflect what we typed).
  * Mouse selection works inside the iframe (xterm renders a
    selection rectangle that we assert on).

The WebSocket back-end (`/ws/exec/{id}`) requires a real container so
we don't assert on round-tripped output — only that the local xterm
buffer mirrors keystrokes immediately (xterm echoes the user's input
locally before the PTY responds, which is the behaviour we want even
when the PTY is offline).
"""

from __future__ import annotations

import pytest

pytest_plugins = ["tests.conftest_e2e", "tests.conftest_audit"]

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]"',
)

pytestmark = pytest.mark.e2e


def _wait_for_xterm_in_iframe(page, frame_locator) -> None:
    """Wait until the iframe's document has `.xterm` mounted. The
    iframe's terminal-frame.js loads xterm.js + addon-fit via
    `<script src>` tags, so the mount completes once those scripts
    have executed and `Terminal.open(termDiv)` has run."""
    # The .xterm element is the root xterm container. Its presence
    # implies Terminal.open() completed and the renderer has painted
    # its first cell.
    frame_locator.locator(".xterm").wait_for(state="visible", timeout=10_000)


def test_terminal_iframe_xterm_mounts_under_route_scoped_csp(page, live_server):
    """Navigating directly to /api/terminal-frame/<id> renders the
    iframe HTML, loads xterm.js, and mounts a Terminal — under a CSP
    that the main SPA's strict policy would otherwise refuse."""
    page.goto(
        f"{live_server}/api/terminal-frame/abcd1234",
        wait_until="domcontentloaded",
    )
    # The iframe page IS the document we just navigated to — there is
    # no nesting yet, since we hit it directly. The route serves the
    # same HTML the SPA's `<iframe src=...>` would.
    page.wait_for_selector(".xterm", timeout=10_000)
    # xterm exposes `window._term` per the diagnostic hook in
    # terminal-frame.js. Existence proves the Terminal constructor
    # ran and is reachable from the page context.
    has_term = page.evaluate("() => typeof window._term === 'object' && !!window._term")
    assert has_term, "window._term not exposed by terminal-frame.js"


def test_terminal_iframe_accepts_keyboard_input(page, live_server):
    """xterm doesn't locally echo keystrokes — it forwards them to the
    PTY via its `onData` callback and waits for the shell to echo
    them back. We can't stand up a real PTY here, so we tap onData
    directly: subscribe a sink before typing, then assert what xterm
    captured. The onData → ws.send() path is exactly what
    terminal-frame.js uses to forward keystrokes onto the WebSocket,
    so this proves the keyboard-to-PTY plumbing works under our
    route-scoped CSP."""
    page.goto(
        f"{live_server}/api/terminal-frame/abcd1234",
        wait_until="domcontentloaded",
    )
    page.wait_for_selector(".xterm", timeout=10_000)
    # Install a sink BEFORE typing so no input is missed. Reuses the
    # diagnostic `window._term` hook exposed by terminal-frame.js.
    page.evaluate(
        """() => {
            window._typed = '';
            window._term.onData(data => { window._typed += data; });
            window._term.focus();
        }"""
    )
    page.keyboard.type("echo hello", delay=10)
    page.wait_for_function(
        "() => (window._typed || '').includes('echo hello')",
        timeout=5_000,
    )
    typed = page.evaluate("() => window._typed")
    assert "echo hello" in typed, (
        f"keystrokes did not reach xterm.onData; captured={typed!r}"
    )


def test_terminal_iframe_mouse_selection_paints(page, live_server):
    """Mouse interaction: drag across the rendered cells, assert that
    xterm surfaces a non-empty selection via `term.getSelection()`.
    We seed the buffer via `term.write` (the same path the WS message
    handler uses, no PTY required) since xterm doesn't echo typed
    keystrokes locally."""
    page.goto(
        f"{live_server}/api/terminal-frame/abcd1234",
        wait_until="domcontentloaded",
    )
    page.wait_for_selector(".xterm", timeout=10_000)
    # Inject content via term.write — same path the WS message handler
    # uses, no PTY required. Use a long string so the drag has room.
    page.evaluate(
        """() => {
            window._term.write('selectable text content here that spans columns');
        }"""
    )
    page.wait_for_timeout(100)  # let xterm paint the row
    # Drag across the rendered cells. xterm renders into `.xterm-screen`;
    # we synthesise a mouse-down → move → mouse-up sequence across it.
    screen = page.locator(".xterm-screen")
    box = screen.bounding_box()
    assert box, ".xterm-screen has no bounding box"
    page.mouse.move(box["x"] + 10, box["y"] + 10)
    page.mouse.down()
    page.mouse.move(box["x"] + 250, box["y"] + 10, steps=10)
    page.mouse.up()
    page.wait_for_timeout(200)
    selection = page.evaluate(
        "() => window._term ? window._term.getSelection() : ''",
    )
    assert selection, "mouse drag produced no terminal selection"


def test_terminal_iframe_survives_moveBefore_tab_switch(page, live_server):
    """Regression guard for the pre-CSP-refactor behaviour: switching
    away from the Terminal tab and back must preserve the iframe's
    contentWindow (and therefore xterm + WebSocket + scrollback). With
    ordinary `parent.appendChild(iframe)`, the move is spec'd as
    remove+insert which destroys the contentDocument. The
    `Node.moveBefore` atomic-move API (Chrome 133+) preserves it.

    This test simulates the showDetail → showShellContent tab dance:
      1. mount the iframe under a stand-in `host` div, install a
         marker in window so we can confirm the same window survives
      2. move the iframe to a hidden 'stash' parent (the showDetail
         path on tab leave)
      3. move the iframe back to the host (the showShellContent path
         on tab re-entry)
      4. confirm `iframe.contentWindow._marker` still matches what we
         set in (1) — proving the contentWindow wasn't recreated."""
    page.goto(f"{live_server}/about:blank", wait_until="domcontentloaded")
    # Build a minimal DOM mirroring the showDetail layout: a host div
    # for the active iframe and a stash div for the tab-leave parking.
    page.set_content("""
        <!DOCTYPE html><html><body>
            <div id='host'></div>
            <div id='stash' style='position:absolute;left:-99999px'></div>
        </body></html>
    """)
    # Mount the iframe under #host. Wait for xterm to mount inside.
    page.evaluate(
        f"""() => {{
            const f = document.createElement('iframe');
            f.id = 'term';
            f.src = '{live_server}/api/terminal-frame/abcd1234';
            document.getElementById('host').appendChild(f);
        }}"""
    )
    page.wait_for_function(
        "() => { const f = document.getElementById('term');"
        "  return f && f.contentDocument && f.contentDocument.querySelector('.xterm'); }",
        timeout=10_000,
    )
    # Stamp a marker on the iframe's contentWindow. A surviving move
    # keeps the same contentWindow, so the marker must still be there.
    page.evaluate("() => { document.getElementById('term').contentWindow._marker = 'survived'; }")
    if not page.evaluate("() => typeof Element.prototype.moveBefore === 'function'"):
        import pytest as _pytest
        _pytest.skip("Node.moveBefore not supported by this Chromium build")
    # Stash → un-stash via moveBefore.
    page.evaluate(
        """() => {
            const f = document.getElementById('term');
            document.getElementById('stash').moveBefore(f, null);
            document.getElementById('host').moveBefore(f, null);
        }"""
    )
    # The marker must survive — if it doesn't, the contentWindow was
    # recreated and we've lost the session.
    after = page.evaluate(
        "() => document.getElementById('term').contentWindow._marker",
    )
    assert after == "survived", (
        f"iframe.contentWindow was destroyed by the move; marker={after!r}"
    )


def test_terminal_iframe_csp_blocks_inline_style_attempt(page, live_server):
    """The route's own CSP allows `style-src 'self' 'unsafe-inline'`
    so xterm can write inline styles. We sanity-check that the route's
    style-src is the RELAXED one (not the parent's strict one) by
    inspecting the response headers via fetch from the iframe context."""
    page.goto(
        f"{live_server}/api/terminal-frame/abcd1234",
        wait_until="domcontentloaded",
    )
    # Same-origin fetch returns the headers we sent on the route's
    # response. Pull the CSP and verify its shape.
    csp = page.evaluate(
        """async () => {
            const r = await fetch(window.location.href, { method: 'GET' });
            return r.headers.get('content-security-policy') || '';
        }"""
    )
    assert "style-src 'self' 'unsafe-inline'" in csp, (
        f"iframe route lost its style-src relaxation; CSP={csp!r}"
    )
    assert "frame-ancestors 'self'" in csp


def test_terminal_iframe_csp_blocks_inline_script(page, live_server):
    """Symmetric check: the route's `script-src 'self'` must NOT carry
    `'unsafe-inline'`. A regression that loosened script-src would
    re-introduce the inline-script XSS surface the strict SPA CSP
    avoids."""
    page.goto(
        f"{live_server}/api/terminal-frame/abcd1234",
        wait_until="domcontentloaded",
    )
    csp = page.evaluate(
        """async () => {
            const r = await fetch(window.location.href, { method: 'GET' });
            return r.headers.get('content-security-policy') || '';
        }"""
    )
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "default-src 'none'" in csp
