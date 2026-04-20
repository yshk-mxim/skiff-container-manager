# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Class-of-bug regression tests for UI failure modes that slipped
past the existing page-load suite.

Each bug uncovered in the v1.0.1 pre-release was an instance of a
broader class. Page-load tests passed because the DOM rendered; the
class-level problems only showed up when the user clicked the NEXT
step or waited for server state to advance. These tests target the
class, not the individual instance, so future regressions in the same
shape are caught before shipping.

Classes (mapped to Nielsen's 10 Usability Heuristics where applicable):

  #1 Visibility of system status — silent state transitions
     e.g. setup-window expired without a UI cue. Covered by
     test_e2e_silent_expiry.py.

  #2 Match real world (copy/labelling) — past-tense wording on
     pending actions, labels without noun context.
     e.g. undo toast "Container deleted" when delete was pending.
     Covered by test_e2e_ux_flows.py::test_undo_toast_shows_pending_countdown.

  #5 Error prevention + #3 User control — dead links, missing
     confirms on destructive actions, buttons that point somewhere
     that can't work.
     e.g. /api/docs "Open in Swagger Editor" → hosted editor can't
     reach localhost. Covered partially by test_e2e_ux_flows.py and
     test_api_docs_has_no_external_redirects.

  Engineering: race conditions — intervals surviving nav, two async
     writers to the same DOM.
     e.g. refresh timer clobbering detail view; detail-tab Stats
     interval stomping Terminal rendering. Covered by
     test_containers_refresh_timer_does_not_clobber_detail_view and
     test_detail_tab_switching_does_not_clobber_content.

  Engineering: data-shape drift — JSON null where code expects 0,
     cgroup v1/v2 response shape divergence.
     Covered by test_docker_null_tolerance.py (hypothesis fuzz) +
     AP015 lint.

This file fills the remaining gaps:

  1. Dead-link scanner — walk the rendered app and assert every
     visible external link is either same-origin OR in an
     allowlist of known-good third-party destinations.

  2. Interval-lifecycle audit — visit every page, assert no
     managedInterval leaks into the next navigation context.
     Future page that forgets to self-clean on unmount gets caught.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import pytest

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e

from tests.e2e_helpers import SHORT, login, nav_to

# ── Class: Dead external links (Nielsen #5 Error prevention) ─────────────


# Third-party domains we deliberately link to from the SKIFF app or docs.
# Everything else is flagged — catches future /api/docs-style regressions
# where someone links to a page the user can't reach (hosted editors,
# localhost-referencing services, auth-gated admin UIs).
_KNOWN_EXTERNAL_DOMAINS = frozenset(
    {
        "github.com",
        "docs.github.com",
        "raw.githubusercontent.com",
        "www.bestpractices.dev",
        "bestpractices.dev",
        "www.softwaretransparency.org",
        "softwaretransparency.org",
        "osskb.org",
        "docs.osskb.org",
        "hub.docker.com",
        "docker.com",
        "www.docker.com",
        "dequeuniversity.com",
        "www.w3.org",
        "www.anthropic.com",
    }
)


def _domain_of(href: str) -> str:
    """Return the host part of an absolute URL, empty string for relative."""
    if not href or href.startswith(("mailto:", "javascript:", "#")):
        return ""
    if not href.startswith(("http://", "https://")):
        return ""
    # Strip scheme and path.
    rest = href.split("://", 1)[1]
    return rest.split("/", 1)[0].lower()


def test_no_dead_external_links_on_rendered_app(page, live_server):
    """Collect every `<a href>` on the signed-in SPA + /api/docs and
    assert each external destination is either same-origin (loopback)
    or on the _KNOWN_EXTERNAL_DOMAINS allowlist. A stranger domain
    means someone linked to a hosted tool that probably can't reach
    a localhost SKIFF — the same class as the Swagger Editor regression."""
    login(page, live_server)

    discovered_domains: set[str] = set()

    def _collect_current_page():
        hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
        for href in hrefs:
            d = _domain_of(href)
            if d and not d.startswith(("127.0.0.1", "localhost", "::1")):
                discovered_domains.add(d)

    # Walk the main SPA pages.
    for section in ("containers", "images", "volumes", "networks", "compose", "system"):
        nav_to(page, section)
        page.wait_for_timeout(300)
        _collect_current_page()

    # Also /api/docs — this is where the Swagger Editor regression lived.
    page.goto(f"{live_server}/api/docs")
    page.wait_for_selector(".opblock, a[href]", timeout=SHORT)
    _collect_current_page()

    # Strip host-local port variants.
    unknown = {d for d in discovered_domains if d.rsplit(":", 1)[0] not in _KNOWN_EXTERNAL_DOMAINS}
    # Filter out w3c-style schema URIs that axe-core etc. may have
    # injected into the DOM (they're namespace identifiers, not
    # clickable destinations — they have `xmlns` attribute context).
    unknown = {d for d in unknown if not d.endswith(".w3.org")}
    assert not unknown, (
        f"External link(s) to unknown domains found: {sorted(unknown)!r}. "
        f"Either (a) the link points somewhere that can't reach a localhost "
        f"SKIFF (the Swagger Editor bug class), or (b) it's a new legitimate "
        f"destination — add it to _KNOWN_EXTERNAL_DOMAINS in this file."
    )


# ── Class: managedInterval lifecycle (engineering race class) ────────────


def test_no_interval_leaks_on_page_navigation(page, live_server):
    """Walk every top-level page in sequence; after each navigation,
    the global `_activeIntervals` list MUST reach a bounded steady
    state (≤ 2 active intervals — the current page's own poll plus
    the sync-logout tick). A page that forgets to call
    `clearAllIntervals()` on unmount would grow the list unbounded,
    eventually burning the browser's setInterval slot budget and
    visibly lagging the UI.

    Future regression this catches: a new page that adds a 5s poll
    but doesn't register it with `managedInterval(...)` OR does
    register it but forgets to let `showPage()` clear it. Either
    way, the list grows past the bound.
    """
    login(page, live_server)

    # Walk multiple pages; each showPage() call should call
    # clearAllIntervals() before starting the new page's timers.
    page_sequence = [
        "containers",
        "images",
        "volumes",
        "networks",
        "compose",
        "system",
        "containers",  # back to start — should not accumulate
        "images",
        "volumes",
    ]
    max_observed = 0
    for section in page_sequence:
        nav_to(page, section)
        # Let the new page mount + its first managedInterval register.
        page.wait_for_timeout(400)
        count = page.evaluate("() => (window._activeIntervals || []).length")
        max_observed = max(max_observed, count)

    # 2 is the observed steady state: one refresh timer per mounted
    # page + one sync-logout tick. Allow up to 3 for jitter around
    # the nav transition. Anything more is a real leak.
    assert max_observed <= 3, (
        f"_activeIntervals grew to {max_observed} across a 9-step nav walk — "
        f"a page is registering managedInterval without yielding to "
        f"clearAllIntervals() on unmount. This is the same engineering "
        f"class as the 1.0.1 detail-tab contamination bug."
    )


# ── Class: Copy/labelling — every countdown has a "what for" prefix ──────


def test_every_countdown_label_names_what_it_is_counting(page, live_server):
    """Countdowns that say just "Nm Ss left" without naming what's
    ending fail Nielsen #2 (match real world). The 1.0.1 wizard counter
    originally said "4m 58s left" — users read it as "my session is
    about to expire" because no noun preceded the number.

    Tests a static lint invariant: every countdown-like element on
    the app must have a text prefix that names the thing counting down."""
    login(page, live_server)
    # Scan every visible element that could be a countdown indicator
    # (countdown-tagged spans, status banners with seconds-remaining
    # format). For each, the text MUST contain a noun/subject.
    countdown_texts = page.evaluate(
        """() => {
            const hits = [];
            document.querySelectorAll(
                '.wizard-countdown, .status-banner-item, .banner-countdown, .countdown'
            ).forEach(el => {
                const t = (el.innerText || '').trim();
                if (t) hits.push(t);
            });
            return hits;
        }"""
    )
    # Countdowns on the authenticated SPA may be zero; this just checks
    # the ones that ARE present comply.
    for text in countdown_texts:
        # Skip banner-module severity classes that don't carry a number.
        if not any(c.isdigit() for c in text):
            continue
        lower = text.lower()
        # Must contain some context word BEFORE the number — at least
        # one of these prefix keywords is required. An unlabelled
        # "4m 12s left" shouldn't appear.
        keywords = ("window", "session", "signing", "retry", "locked", "ends", "expires", "closes")
        assert any(k in lower for k in keywords), (
            f"Countdown text {text!r} has a number but no context word — "
            f"users can't tell what the countdown refers to. Add a prefix like "
            f"'Setup window closes in...' or 'Signing you out in...'."
        )
