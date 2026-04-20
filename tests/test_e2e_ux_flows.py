# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""E2E tests for UX flows that need the next step to actually work.

These exist because two real defects slipped past the rest of the suite:

1. /api/docs rendered an HTML landing page with "Open in Swagger Editor"
   buttons that pointed at editor.swagger.io — which cannot reach a
   localhost URL. Page-load tests were green, but the user clicked the
   button and landed on a dead interactive experience.

2. The undoableDelete toast said "<kind> deleted" in past tense with no
   progress indication. WCAG and existing e2e tests all passed — page
   load was fine — but the user's first-read interpretation was "this
   failed, here's an Undo link to retry."

Generic lesson: "page loads and renders" is not the same as "the flow
the user expects actually works." The tests below assert the NEXT step,
not just the render.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import pytest

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e


# ── /api/docs — Swagger UI is self-hosted and interactive ────────────────


def test_api_docs_renders_interactive_swagger_ui(page, live_server):
    """/api/docs must load a functional Swagger UI — not an external link
    to a hosted editor that can't reach localhost.

    Checks that (a) Swagger UI's opblock list actually renders so at least
    one route is clickable, and (b) the Authorize button is present —
    which only happens when the spec declares `securitySchemes.bearerAuth`.
    """
    page.goto(f"{live_server}/api/docs")
    # Swagger UI renders one `.opblock-tag` per tag and one `.opblock` per
    # endpoint. Wait for at least one opblock to prove the spec fetched
    # and the UI rendered it — not that the HTML shell loaded.
    page.wait_for_selector(".opblock", timeout=10_000)
    assert page.locator(".opblock").count() > 0, "Swagger UI rendered zero opblocks — spec load failed"

    # The Authorize button is the entry point for Try-it-out to actually
    # ship a bearer header. If it's missing the spec didn't declare
    # bearerAuth and the interactive flow is cosmetic-only.
    authorize = page.locator("button.authorize")
    assert authorize.count() > 0, "Authorize button missing — bearerAuth not declared in spec"

    # Sanity: the operation list includes the /health route every visitor
    # has seen work. If this disappears, the spec fetch or rendering is
    # broken in a subtle way.
    assert page.locator("text=/health").count() > 0


def test_api_docs_has_no_external_redirects(page, live_server):
    """The /api/docs page must not advertise buttons that send users to
    external editors that can't reach localhost. Specifically:
    editor.swagger.io and petstore.swagger.io fetch the spec URL
    server-side (or run into browser CORS) and do NOT work for a
    local SKIFF instance. Regression guard against re-introducing them."""
    page.goto(f"{live_server}/api/docs")
    page.wait_for_load_state("domcontentloaded")
    html = page.content()
    assert "editor.swagger.io" not in html, "Dead external link — editor.swagger.io cannot fetch a localhost URL"
    assert "petstore.swagger.io" not in html, "Dead external link — petstore.swagger.io cannot fetch a localhost URL"


# ── Undo toast — pending countdown is visible, not past-tense ────────────


def test_undo_toast_shows_pending_countdown(page, live_server, docker_client):
    """Delete a volume and assert the undo toast tells the user the
    delete is PENDING with a visible countdown, not that it already
    happened. Would have caught the "looks like a failure" report."""
    name = "e2e-undo-ux-vol"
    if docker_client:
        try:
            docker_client.volumes.get(name).remove(force=True)
        except Exception:
            pass
        docker_client.volumes.create(name=name)
    try:
        page.goto(live_server)
        page.locator(".sidebar a:has-text('Volumes')").click()
        page.wait_for_selector(f"text={name}", timeout=10_000)

        page.on("dialog", lambda d: d.accept())
        page.locator(f"tr:has-text('{name}') button:has-text('Delete')").click()

        # The toast must carry the pending wording "will be deleted in Ns",
        # not the past-tense "deleted" form. The countdown is a freshly-
        # rendered number; matching on the literal "will be deleted in"
        # prefix is the invariant.
        toast = page.locator(".toast.toast-undo")
        toast.wait_for(state="visible", timeout=5_000)
        label = toast.locator(".toast-label").inner_text(timeout=2_000)
        assert "will be deleted in" in label, (
            f"Undo toast read as past-tense ({label!r}) — user will think the delete already finished"
        )

        # A progress bar must be present and mid-transition. asserting the
        # element exists is enough to prove the countdown UX is wired;
        # width-timing assertions would be flaky.
        assert toast.locator(".toast-progress-bar").count() > 0, "Progress bar missing — no visible pending indicator"

        # Undo link is present and actionable.
        undo_link = toast.locator(".undo-link")
        assert undo_link.count() > 0
        undo_link.click()

        # After undo, the success toast replaces the pending one and the
        # volume reappears on refresh.
        page.wait_for_selector(".toast.success", timeout=5_000)
        page.wait_for_selector(f"text={name}", timeout=5_000)
    finally:
        if docker_client:
            try:
                docker_client.volumes.get(name).remove(force=True)
            except Exception:
                pass


# ── Detail-view tab switching: no cross-tab DOM contamination ────────────


def test_detail_tab_switching_does_not_clobber_content(page, live_server, docker_client):
    """Each detail tab (Logs, Terminal, Inspect, Stats, Processes, Files)
    owns #detail-content and some of them arm their own 3s refresh
    interval via managedInterval. Before the fix, switching from a tab
    with an active interval (Stats, Processes) to another tab left the
    old interval running; its next tick would stomp on the new tab's
    content every few seconds.

    User-visible symptom: open Stats (live CPU/mem boxes), click
    Terminal → shell renders → ~3 seconds later the terminal is
    replaced with the ps output from Processes (or vice versa).

    Fix: `showDetail()` calls `clearAllIntervals()` before mounting the
    new tab content. This test clicks through every detail tab in
    sequence, waits past the 3-second refresh window, and asserts each
    tab's characteristic DOM marker is still present (not replaced by
    a previous tab's content).
    """
    from tests.e2e_helpers import SHORT, login, nav_to, teardown_container

    name = "e2e-tab-switch-ctr"
    teardown_container(docker_client, name)
    docker_client.containers.run("alpine:latest", command="sleep 600", name=name, detach=True)
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)

        # Get the short container ID for direct showDetail calls —
        # same pattern as the refresh-timer race regression test.
        container_id = page.evaluate(f"""
            () => {{
                const rows = Array.from(document.querySelectorAll('tbody tr'));
                const r = rows.find(el => el.textContent.includes({name!r}));
                return r ? (r.querySelector('.container-id')?.textContent || '').trim() : '';
            }}
        """)
        assert container_id, "couldn't extract container id"

        # Tab sequence designed to cross every interval-owning boundary:
        # Stats arms an interval → Processes arms one → Files renders
        # once then sits quiet → back to Stats. If any previous tab's
        # interval leaked, its next tick would overwrite the current
        # tab's content.
        tabs_and_markers = [
            # (tab_name, DOM marker that proves this tab is rendered)
            ("stats", "#stats-grid"),
            ("processes", ".detail-tabs"),  # Processes renders a table in detail-content
            ("files", ".detail-tabs"),
            ("stats", "#stats-grid"),
            ("inspect", "#detail-content"),
        ]
        for tab, marker in tabs_and_markers:
            page.evaluate(f"() => showDetail({container_id!r}, {name!r}, {tab!r})")
            page.wait_for_selector(marker, timeout=SHORT)
            # Wait past one full interval tick (3 seconds) so any leaked
            # refresh from the previous tab would have fired by now.
            page.wait_for_timeout(3_500)
            # Marker must STILL be present — previous tab's refresh
            # must not have overwritten the current tab.
            assert page.locator(marker).count() > 0, (
                f"tab '{tab}' content lost after 3.5s — a leaked interval from a "
                f"previous tab clobbered #detail-content (regression of the "
                f"5-second managedInterval cross-tab contamination bug)"
            )
            # Extra: the active-tab class must still be on the right tab.
            active = page.locator(".detail-tab.active").inner_text().lower()
            assert tab in active, f"active tab label is {active!r}, expected {tab!r}"
    finally:
        teardown_container(docker_client, name)


# ── Containers list 5-second refresh-timer race (bug #14) ────────────────


def test_containers_refresh_timer_does_not_clobber_detail_view(page, live_server, docker_client):
    """A `loadContainers()` that was already in-flight when the user
    clicked Logs/Terminal/Inspect must NOT call `renderContainers` on
    completion — that would wipe the detail view the user just
    navigated into, dumping them back to the list mid-session.

    The fix (app.js `loadContainers`) short-circuits when
    `#detail-content` is present. This test simulates the exact race
    via the page's own function handles and asserts the detail view
    stays mounted.
    """
    from tests.e2e_helpers import SHORT, login, nav_to, teardown_container

    name = "e2e-race-ctr"
    teardown_container(docker_client, name)
    docker_client.containers.run("alpine:latest", command="sleep 600", name=name, detach=True)
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)

        # Simulate the race:
        # 1. Kick off loadContainers() but don't await — it hits apiFetch + await
        # 2. Immediately call showDetail which mounts #detail-content
        # 3. When loadContainers resolves, it MUST observe #detail-content and
        #    short-circuit rather than renderContainers-clobbering the detail.
        page.evaluate(f"""
            () => {{
                // Kick refresh (async).
                window._racePromise = loadContainers();
                // Simulate user click mid-race — synchronously invoke the
                // containers list's Logs/Terminal handler for this row.
                const rows = Array.from(document.querySelectorAll('tbody tr'));
                const row = rows.find(r => r.textContent.includes({name!r}));
                const containerId = (row.querySelector('.container-id')?.textContent || '').trim();
                showDetail(containerId, {name!r}, 'inspect');
            }}
        """)
        # Wait enough for the in-flight loadContainers to resolve. If the
        # fix works, detail-content stays mounted. If it regressed,
        # renderContainers would have replaced #main.
        page.wait_for_selector("#detail-content", timeout=SHORT)
        page.wait_for_timeout(1_500)  # well past apiFetch resolution
        # The detail layout must still be mounted — wrap in a try for a
        # clearer failure message than a vanilla wait_for_selector timeout.
        assert page.locator("#detail-content").count() > 0, (
            "refresh-timer race regression: detail view was wiped by "
            "in-flight loadContainers completing after showDetail"
        )
        # Defensive: the tabs row should also still be present.
        assert page.locator(".detail-tabs").count() > 0, "refresh-timer race regression: detail tabs disappeared"
    finally:
        teardown_container(docker_client, name)
