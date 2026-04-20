#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Regenerate README screenshots at a wide viewport.

Captures each featured page at 1600×1000 so button rows don't wrap and
full table content is visible. Seeds a deterministic Docker state
before capture (labelled containers / images / volume / compose stack)
so every re-run gives the same shot — no flaky per-run diffs.

Usage (server MUST already be running on :8080 with the API socket
reachable and no API token, i.e. naive mode):

    python scripts/regenerate_screenshots.py

Output files land in docs/ (overwriting the existing PNGs that README
references). Each capture is full-page — the viewport width is the key
constraint, the height grows to fit the page's actual content.

Seeded resources are labelled `skiff.shot=1` so a follow-up cleanup
command can wipe them without touching the operator's own state:

    docker ps -a --filter label=skiff.shot=1 -q | xargs -r docker rm -f
    docker volume ls --filter label=skiff.shot=1 -q | xargs -r docker volume rm
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("playwright not installed — pip install 'playwright>=1.40' && playwright install chromium")


BASE_URL = "http://127.0.0.1:8080"
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
VIEWPORT = {"width": 1600, "height": 1000}
# When the server runs with API_TOKEN set, sign in with this token so the
# sidebar + pages render. Reads the env var the operator used to start
# the server; falls back to the demo token this script ships with.
import os  # noqa: E402

SHOT_TOKEN = os.environ.get("API_TOKEN", "")

# Label every seeded resource so cleanup is unambiguous — `docker rm -f
# $(docker ps -aq --filter label=skiff.shot=1)` wipes exactly the ones
# this script created, nothing of the operator's own.
SHOT_LABEL = {"skiff.shot": "1"}


def _wait_server() -> None:
    for _ in range(30):
        try:
            if requests.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)
    sys.exit(f"server at {BASE_URL} is not responding — start it first")


def _seed() -> dict[str, str]:
    """Create a handful of named resources so screenshots aren't empty.

    Idempotent: drops any prior shot-labelled resources before re-seeding.
    Returns a dict of names so captures can reference specific containers."""
    import docker

    client = docker.from_env()

    # Wipe previous shot residue.
    for c in client.containers.list(all=True, filters={"label": "skiff.shot=1"}):
        try:
            c.remove(force=True)
        except docker.errors.APIError:
            pass
    for v in client.volumes.list(filters={"label": "skiff.shot=1"}):
        try:
            v.remove(force=True)
        except docker.errors.APIError:
            pass

    # Pre-pull the few images the shots use so the Containers page has
    # rows ready by the time we navigate.
    for img in ("nginx:alpine", "postgres:16-alpine", "redis:7-alpine", "alpine:3.20"):
        try:
            client.images.pull(img)
        except docker.errors.APIError:
            pass

    # One volume with data so the Volumes page looks populated.
    shot_vol = client.volumes.create(name="shot-data", labels=SHOT_LABEL)

    # Three containers in different states.
    running_web = client.containers.run(
        "nginx:alpine",
        name="web-frontend",
        detach=True,
        ports={"80/tcp": "9080"},
        labels=SHOT_LABEL,
        volumes={shot_vol.name: {"bind": "/usr/share/nginx/html", "mode": "rw"}},
    )
    running_db = client.containers.run(
        "postgres:16-alpine",
        name="api-db",
        detach=True,
        environment={"POSTGRES_PASSWORD": "demo-shot-only"},
        labels=SHOT_LABEL,
    )
    # One stopped container so the list shows a mixed state.
    stopped = client.containers.run(
        "alpine:3.20",
        name="batch-job",
        detach=True,
        command="true",
        labels=SHOT_LABEL,
    )
    time.sleep(1)  # let postgres finish its initdb; stopped exits

    return {
        "running_web": running_web.id,
        "running_db": running_db.id,
        "stopped": stopped.id,
        "volume": shot_vol.name,
    }


def _capture(page, out_path: Path, *, full_page: bool = False, settle_ms: int = 800) -> None:
    """Wait + shoot. Navigation happens via sidebar clicks in `_nav`
    before this helper is called, so we must NOT re-goto the root URL
    here — that would reset the SPA back to the default containers
    page. `full_page=True` for content-height pages (Dashboard,
    Templates); False for list pages where the viewport shot is enough."""
    page.wait_for_timeout(settle_ms)
    page.screenshot(path=str(out_path), full_page=full_page)
    print(f"  wrote {out_path.relative_to(DOCS_DIR.parent)} ({out_path.stat().st_size // 1024} KB)")


def main() -> None:
    _wait_server()
    print("seeding demo state (labelled skiff.shot=1)…")
    names = _seed()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Dark mode is SKIFF's more striking screenshot theme (teal
        # accents pop against the slate-900 background). Playwright's
        # color_scheme=dark drives the app's theme-init script via the
        # `prefers-color-scheme` media query.
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2, color_scheme="dark")
        page = ctx.new_page()

        # Pre-set tour-done before any route fires its overlay.
        page.goto(f"{BASE_URL}/")
        page.wait_for_timeout(500)
        page.evaluate("() => localStorage.setItem('skiff.tour.done', '1')")
        # Sign in if the login form is rendered (server started with
        # API_TOKEN set). SHOT_TOKEN is read from the env; when the
        # operator started the server in naive mode (no token) the
        # login form won't appear and this branch is skipped.
        if SHOT_TOKEN and page.locator("button:has-text('Sign in')").count() > 0:
            page.locator("input[type='password']").fill(SHOT_TOKEN)
            page.locator("button:has-text('Sign in')").click()
            page.wait_for_selector(".sidebar a", timeout=15_000)
            page.wait_for_timeout(500)
            page.evaluate("() => localStorage.setItem('skiff.tour.done', '1')")
        else:
            # Even in naive mode we need the sidebar to render before the
            # first click — the registry loads async from pages/*.js.
            page.wait_for_selector(".sidebar a", timeout=15_000)

        # Dismiss the first-run tour overlay — its modal backdrop
        # intercepts every sidebar click. Wait for it to *appear* (up
        # to 2s) before dismissing: tour.js may not have rendered yet
        # at the moment we check, so the original "if count>0" race
        # falls through and the next sidebar click hits the overlay.
        page.wait_for_timeout(1500)
        tour = page.locator(".tour-overlay")
        if tour.count() > 0 and tour.first.is_visible():
            skip = page.locator(".tour-overlay button:has-text('Skip')")
            if skip.count() > 0:
                skip.first.click()
            else:
                page.keyboard.press("Escape")
            try:
                tour.first.wait_for(state="hidden", timeout=5_000)
            except Exception:  # nosec B110
                pass
        # Belt-and-braces: force-hide any residual overlay that might
        # race back in between captures.
        page.evaluate(
            "() => { const t = document.querySelector('.tour-overlay');"
            " if (t) t.style.display = 'none'; }"
        )

        def _nav(label: str, wait_selector: str, timeout_ms: int = 10_000) -> None:
            page.locator(f".sidebar a:has-text('{label}')").first.click()
            page.wait_for_selector(wait_selector, timeout=timeout_ms)
            page.wait_for_timeout(1000)

        print("capturing dashboard…")
        _nav("Dashboard", "h2:has-text('Overview'), h2:has-text('Dashboard')")
        _capture(page, DOCS_DIR / "screenshot-dashboard.png", full_page=True, settle_ms=400)

        print("capturing containers list…")
        _nav("Containers", "table", timeout_ms=15_000)
        _capture(page, DOCS_DIR / "screenshot-containers.png", settle_ms=800)

        print("capturing files tab (web-frontend container)…")
        # Click the container row's Inspect or navigate via showDetail global.
        page.evaluate(
            f"window.showDetail && window.showDetail('{names['running_web']}', 'web-frontend', 'files')"
        )
        page.wait_for_selector(".detail-subtabs button, h2:has-text('web-frontend')", timeout=10_000)
        page.wait_for_timeout(1500)
        _capture(page, DOCS_DIR / "screenshot-files.png")

        print("capturing settings page…")
        _nav("Settings", ".settings-row", timeout_ms=10_000)
        _capture(page, DOCS_DIR / "screenshot-settings.png", settle_ms=400)

        print("capturing templates page (Apps + Stacks)…")
        _nav("Templates", ".template-card", timeout_ms=10_000)
        _capture(page, DOCS_DIR / "screenshot-templates.png", full_page=True, settle_ms=400)

        browser.close()

    print()
    print("done — inspect the PNGs under docs/ and commit the ones you keep.")
    print("cleanup:")
    print(
        "  docker ps -a --filter label=skiff.shot=1 -q | xargs -r docker rm -f && "
        "docker volume ls --filter label=skiff.shot=1 -q | xargs -r docker volume rm"
    )


if __name__ == "__main__":
    main()
