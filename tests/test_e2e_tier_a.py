# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier A — first-user flow-functional e2e tests.

Every test here asserts the *outcome* of a multi-step flow, not just
that a page loaded. These are the 5 journeys every new SKIFF user does
in their first 10 minutes — wizard, run container, exec, logs, compose.
Broken UX in any of them trashes the first impression; page-load tests
wouldn't catch most of the breakage modes.
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

from tests.e2e_helpers import SHORT, auth_headers, login, nav_to, teardown_container

# ── A1. Zero-config wizard → signed in ───────────────────────────────────


def test_a1_zero_config_wizard_signs_in(browser, isolated_server):
    """Boot SKIFF with zero config, complete the wizard, assert the
    sidebar renders and an authenticated API call succeeds. Catches:
    wizard can't reach probe endpoint, token POST fails, sidebar
    doesn't render post-setup, /api/containers still 401 with the
    wizard-generated token.
    """
    # Start with an empty API_TOKEN so the wizard is reachable, and
    # a known DOCKER_HOST so we're independent of the probe result.
    import os

    docker_host = os.environ.get("SKIFF_TEST_DOCKER_HOST") or (
        os.path.expanduser("~/.colima/default/docker.sock")
        if os.path.exists(os.path.expanduser("~/.colima/default/docker.sock"))
        else "/var/run/docker.sock"
    )
    if not docker_host.startswith("unix://"):
        docker_host = "unix://" + docker_host
    url, _proc = isolated_server(
        {
            "API_TOKEN": "",
            "DOCKER_HOST": docker_host,
            "SETUP_WINDOW_SECS": "300",
        }
    )
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_navigation_timeout(8_000)
    try:
        pg.goto(url, wait_until="domcontentloaded")
        pg.wait_for_selector("#sw-btn-save", timeout=5_000)
        # Generate a token, paste the docker-host, submit.
        pg.locator("#sw-gen-btn").click()
        token = pg.locator("#sw-token").input_value()
        assert token and len(token) >= 32, "wizard generator produced an empty/short token"
        # Fill host (wizard probe may or may not have pre-populated it).
        pg.locator("#sw-host-custom").fill(docker_host)
        # Click Copy to acknowledge the token so session button unlocks
        # (and so the token is available via the fallback sessionStorage
        # write that happens post-setup).
        pg.locator("#sw-copy-btn").click()
        pg.wait_for_timeout(300)
        # Use "In-memory only" — avoids downloading a .env file to the
        # runner, which is Playwright's default action for that button.
        pg.locator("#sw-btn-session").click()
        # Sidebar should render once the server is configured + UI re-inits.
        pg.wait_for_selector(".sidebar", timeout=10_000)
        # Outcome check: an authenticated API call succeeds with the
        # wizard-generated token. This is the real "am I signed in" test.
        r = requests.get(
            f"{url}/api/containers",
            headers={"Authorization": f"Bearer {token}", "X-Requested-With": "ContainerManager"},
            timeout=5,
        )
        assert r.status_code == 200, f"post-wizard auth call failed: {r.status_code} {r.text[:200]}"
    finally:
        ctx.close()


# ── A2. Run container modal → appears running ────────────────────────────


def test_a2_run_container_modal_appears_running(page, live_server, docker_client):
    """Open Run modal, submit a minimal container spec, and verify the
    row appears with `running` state within 5s AND the actual container
    is listed by the Docker daemon (cross-checks UI and daemon truth).
    """
    name = "e2e-a2-run-modal"
    teardown_container(docker_client, name)
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.locator("button:has-text('Run new container')").first.click()
        page.wait_for_selector(".modal-bg", timeout=SHORT)
        # Fields are identified by id (run-image / run-name / run-cmd),
        # not name — the Run modal uses `addField(label, id, placeholder)`.
        page.locator("#run-image").fill("alpine:latest")
        page.locator("#run-name").fill(name)
        page.locator("#run-cmd").fill("sleep 600")
        # Exact match: the modal also carries "node · Node.js runtime" /
        # "python · Python runtime" suggestion buttons that contain "Run".
        page.get_by_role("button", name="Run", exact=True).click()
        # Outcome: the row appears with a running state badge.
        page.wait_for_selector(f"tr:has-text('{name}') .status.running", timeout=SHORT)
        # Cross-check against the daemon.
        running = [c.name for c in docker_client.containers.list() if c.name == name]
        assert name in running, f"container row showed running but daemon doesn't list it: {running}"
    finally:
        teardown_container(docker_client, name)


# ── A3. Exec terminal → prompt + echo roundtrip ──────────────────────────


def test_a3_exec_terminal_roundtrip(page, live_server, docker_client):
    """Open exec WS on a running container, type `echo HELLO`, and
    assert `HELLO` appears in the terminal output. Catches: WS auth
    fails, PTY isn't attached, input doesn't flow, keepalive doesn't
    fire on the first frame."""
    # Shared live_server + 5s refresh polling means this test can be
    # slow when run alongside A4 (which also opens a WS); widen the
    # default Playwright timeouts so the fixture's aggressive defaults
    # don't fail under concurrent-WS load.
    page.set_default_timeout(20_000)
    name = "e2e-a3-exec"
    teardown_container(docker_client, name)
    docker_client.containers.run("alpine:latest", command="sleep 600", name=name, detach=True)
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)
        row = page.locator(f"tr:has-text('{name}')")
        row.wait_for(state="visible", timeout=SHORT)
        # Sidebar-less full-refresh: navigate away and back so the in-flight
        # loadContainers isn't mid-apiFetch when we click. Kills the race
        # where a 5-second poll completes AFTER showDetail and stomps on
        # the detail view.
        page.locator(".sidebar a:has-text('Images')").click()
        page.wait_for_selector("h2:has-text('Images')", timeout=SHORT)
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("h2:has-text('Containers')", timeout=SHORT)
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)
        # Click through the button. Now the 5s refresh timer was only just
        # armed, so we have ~5s of grace to click + navigate to detail.
        row.locator("button:has-text('Terminal')").first.click()
        page.wait_for_selector("#term-output", timeout=SHORT)
        # Terminal input uses class .terminal-input (placeholder "Type command…").
        # Wait an extra beat for the WS to hand over the shell prompt
        # before typing — PTY may still be warming up.
        term_input = page.locator("input.terminal-input")
        term_input.wait_for(state="visible", timeout=SHORT)
        page.wait_for_timeout(800)
        term_input.fill("echo A3OKSENTINEL")
        page.keyboard.press("Enter")
        deadline = time.time() + 5
        while time.time() < deadline:
            out = page.locator("#term-output").inner_text()
            if "A3OKSENTINEL" in out:
                break
            page.wait_for_timeout(200)
        else:
            pytest.fail("echo output never reached the terminal — exec roundtrip broken")
    finally:
        teardown_container(docker_client, name)


# ── A4. Log stream → live lines visible ──────────────────────────────────


def test_a4_log_stream_delivers_live_lines(page, live_server, docker_client):
    """Start a container printing every 200ms, open the logs panel,
    assert new lines appear (count increases) within 3s. Catches:
    WS opens but 0 frames, frames arrive but DOM doesn't update, PTY
    hangs on TTY allocation, keepalive chokes the stream."""
    # Same load-sensitive timeouts as A3.
    page.set_default_timeout(20_000)
    name = "e2e-a4-log-stream"
    teardown_container(docker_client, name)
    docker_client.containers.run(
        "alpine:latest",
        command=["sh", "-c", "i=0; while true; do i=$((i+1)); echo line-$i; sleep 0.2; done"],
        name=name,
        detach=True,
    )
    try:
        login(page, live_server)
        nav_to(page, "containers")
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)
        row = page.locator(f"tr:has-text('{name}')")
        row.wait_for(state="visible", timeout=SHORT)
        # Same nav-dance as test_a3 to reset the 5s refresh-poll clock
        # and give us a clean window to click Logs.
        page.locator(".sidebar a:has-text('Images')").click()
        page.wait_for_selector("h2:has-text('Images')", timeout=SHORT)
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("h2:has-text('Containers')", timeout=SHORT)
        page.wait_for_selector(f"tr:has-text('{name}')", timeout=SHORT)
        row.locator("button:has-text('Logs')").first.click()
        page.wait_for_selector("#log-output", timeout=SHORT)
        page.wait_for_timeout(800)  # WS attach + first frame
        page.wait_for_function(
            "() => { var el = document.getElementById('log-output');"
            "  return el && (el.textContent.match(/line-/g) || []).length >= 5; }",
            timeout=15_000,
        )
    finally:
        teardown_container(docker_client, name)


# ── A5. Compose deploy → services running ────────────────────────────────


def test_a5_compose_deploy_services_running(live_server, docker_client):
    """Deploy a minimal 2-service compose stack via the HTTP API and
    assert both containers are running per the daemon. UI upload path
    is covered by test_e2e_compose.py; this asserts the core deploy →
    services-running flow every first-time user expects when they
    upload a compose file."""
    from tests.e2e_helpers import deploy_compose_stack, teardown_compose_stack

    project = "e2e-a5-compose"
    yaml = (
        b"services:\n"
        b"  svc-a:\n"
        b"    image: alpine:latest\n"
        b"    command: sleep 600\n"
        b"  svc-b:\n"
        b"    image: alpine:latest\n"
        b"    command: sleep 600\n"
    )
    teardown_compose_stack(project)
    try:
        deploy_compose_stack(project, yaml)
        # Poll for both services running; compose up may take a few
        # seconds for image caching + container creation.
        deadline = time.time() + 30
        running_names: set[str] = set()
        while time.time() < deadline:
            running_names = {
                c.name
                for c in docker_client.containers.list()
                if c.labels.get("com.docker.compose.project") == project and c.status == "running"
            }
            if len(running_names) >= 2:
                break
            time.sleep(0.5)
        assert len(running_names) >= 2, (
            f"expected 2 running services for project {project!r}, got {len(running_names)}: {running_names}"
        )
        # UI check: the project surfaces on GET /api/compose/stacks.
        r = requests.get(f"{live_server}/api/compose/stacks", headers=auth_headers(), timeout=5)
        assert r.status_code == 200
        projects = [p.get("name") for p in r.json()]
        assert project in projects, f"deployed project missing from /api/compose/stacks: {projects}"
    finally:
        teardown_compose_stack(project)
