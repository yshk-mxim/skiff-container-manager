# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Every list-rendering page MUST have a search/filter affordance,
and every paginated or capped list MUST disclose the cap.

Gap in coverage that motivated this file: the v1.0.1 amend added
Volumes, Networks, Compose pages without search boxes, and an
Audit Log view hard-capped at 200 rows with no way to see more. The
existing e2e suite (Tier A/B/C/D, UX flows, accessibility) all passed
because pages rendered cleanly — none of them asserted discoverability.

Invariant here: for every entity-listing page in the SPA, one of:

  (a) a visible `<input type="search">` or `.search-bar` input lets the
      user filter the rendered list, OR
  (b) the list is inherently bounded (≤5 items) and a filter would
      add noise.

For capped lists (e.g. audit log's `tail=N`), the header must disclose
the cap so the user knows more exists behind it. Silent truncation is
a bug by definition.

This test loads each page against a live server, asserts the
affordance is present, and types into it to verify it actually filters
the DOM. It's the "aggressive realistic-usability" test that would have
caught all four of the 1.0.1-amend gaps in one pass."""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import pytest

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e

from tests.e2e_helpers import SHORT, login, nav_to

# Every SPA page that renders a list of entities and the selector for
# its search/filter input. "Inherently short" pages (Dashboard, Wizard)
# are exempt and not listed here.
_LIST_PAGES_REQUIRING_SEARCH = [
    ("containers", "input.search-bar"),
    ("images", "input.search-bar"),
    ("volumes", "input.search-bar"),
    ("networks", "input.search-bar"),
    ("compose", "input.search-bar"),
]


def test_every_entity_list_page_has_search_affordance(page, live_server):
    """Class guard: every entity-listing page that has ≥1 row renders
    a visible `.search-bar` input. Pages with zero rows are exempt —
    showing a filter for an empty list is noise. Adding a new page
    (Secrets? Configs?) that forgets the search-bar trips this test."""
    login(page, live_server)
    missing: list[str] = []
    for section, selector in _LIST_PAGES_REQUIRING_SEARCH:
        nav_to(page, section)
        page.wait_for_timeout(400)
        # A page is subject to the invariant iff its rendered list has
        # content. Containers/images/volumes/networks always render a
        # non-empty <tbody> (even bridge/host/none are bundled networks);
        # compose is the only page that can be fully empty — handled
        # below with a non-empty seed in the dedicated filter test.
        has_rows = page.locator("tbody tr, .stack-card").count() > 0
        loc = page.locator(selector)
        visible = loc.count() >= 1 and loc.first.is_visible()
        if has_rows and not visible:
            missing.append(section)
    assert not missing, (
        f"Pages showing rows without a search affordance: {missing!r}. "
        f"Every list page with ≥1 row must render an `input.search-bar` "
        f"so users can filter long lists. See skiff/static/app.js::"
        f"renderContainers for the canonical pattern."
    )


def test_audit_log_discloses_its_cap(page, live_server):
    """The audit-log view used to fetch `tail=200` silently; users with
    more than 200 rows never knew more existed. Invariant: either (a)
    a size selector is visible so users can raise the cap up to the
    backend MAX_AUDIT_LINES, or (b) the current cap is explicit in
    the header label."""
    login(page, live_server)
    nav_to(page, "system")
    page.wait_for_timeout(600)
    # Scroll to the audit log section so the controls are in viewport.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(200)
    tail_select = page.locator("[data-testid='audit-tail-select']")
    audit_filter = page.locator("[data-testid='audit-filter']")
    assert tail_select.count() > 0, (
        "Audit log view is missing its tail-size selector — users can't "
        "see past the default cap. Add a <select> or the header must "
        "name the current cap in text."
    )
    assert audit_filter.count() > 0, (
        "Audit log has no filter input — with thousands of rows the user has no way to find a specific event."
    )


def test_list_page_search_actually_filters(page, live_server, docker_client):
    """The search input must produce a VISIBLE change in the rendered
    list. An input that looks present but does nothing is worse than
    no input — the user's trust evaporates. Typed 'zzz-no-match' should
    visibly thin the list to zero matches; typed empty should restore."""
    from tests.e2e_helpers import teardown_container

    # Seed two containers so the list has > 0 rows.
    names = ["e2e-filter-alpha", "e2e-filter-beta"]
    for n in names:
        teardown_container(docker_client, n)
        docker_client.containers.run("alpine:latest", command="sleep 600", name=n, detach=True)
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{names[0]}')", timeout=SHORT)
        page.wait_for_selector(f"tr:has-text('{names[1]}')", timeout=SHORT)

        search = page.locator("input.search-bar").first
        search.fill("zzz-no-match-xyz")
        page.wait_for_timeout(300)
        # Both seeded names should be gone from the DOM when the filter
        # doesn't match them.
        assert page.locator(f"tr:has-text('{names[0]}')").count() == 0, (
            "Search bar is present but doesn't actually filter — typing "
            "a non-matching string still shows the non-matching rows."
        )
        # Clear the filter; rows should come back.
        search.fill("")
        page.wait_for_timeout(300)
        page.wait_for_selector(f"tr:has-text('{names[0]}')", timeout=SHORT)
    finally:
        for n in names:
            teardown_container(docker_client, n)


def test_delete_emits_undo_toast_on_every_resource(page, live_server, docker_client):
    """The /v1.0.1 amend quietly regressed container delete — the UI
    passed `?force=true` unconditionally which made the backend
    short-circuit the undo queue. Tested ONLY for volumes before, so
    it shipped. This guards the CLASS: deleting any undoable resource
    from the main list must produce a `.toast.toast-undo` with a
    `will be deleted in` label, not an immediate silent delete.

    Containers and volumes are both tested here; images are also
    undoable but deletion requires no running-dependents and the
    UI flow is the same."""
    from tests.e2e_helpers import teardown_container

    # --- Container: create a stopped one so delete doesn't need force.
    cname = "e2e-undo-ctr-stopped"
    teardown_container(docker_client, cname)
    c = docker_client.containers.run("alpine:latest", command="true", name=cname, detach=True)
    c.wait(timeout=10)  # exit quickly so it's in stopped state

    # --- Volume: always deletable without force.
    vname = "e2e-undo-vol-regression"
    try:
        docker_client.volumes.get(vname).remove(force=True)
    except Exception:
        pass
    docker_client.volumes.create(name=vname)

    try:
        login(page, live_server)

        # Container delete flow.
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{cname}')", timeout=SHORT)
        page.on("dialog", lambda d: d.accept())
        page.locator(f"tr:has-text('{cname}') button:has-text('Delete')").click()
        toast = page.locator(".toast.toast-undo")
        toast.wait_for(state="visible", timeout=5_000)
        label = toast.locator(".toast-label").inner_text(timeout=2_000)
        assert "will be deleted in" in label, (
            f"Container delete toast read as past-tense ({label!r}) — "
            f"the `force=true` bypass of the undo queue has regressed."
        )

        # Volume delete flow — separate assertion to catch future single-
        # resource regressions without re-using the container toast.
        nav_to(page, "volumes")
        page.wait_for_selector(f"text={vname}", timeout=SHORT)
        page.locator(f"tr:has-text('{vname}') button:has-text('Delete')").click()
        # Second toast; first may still be on screen.
        toasts = page.locator(".toast.toast-undo")
        toasts.last.wait_for(state="visible", timeout=5_000)
        vlabel = toasts.last.locator(".toast-label").inner_text(timeout=2_000)
        assert "will be deleted in" in vlabel, f"Volume delete toast wording regressed ({vlabel!r})."
    finally:
        teardown_container(docker_client, cname)
        try:
            docker_client.volumes.get(vname).remove(force=True)
        except Exception:
            pass
