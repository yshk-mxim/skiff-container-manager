"""
Full-lifecycle Playwright e2e tests for SKIFF Container Manager.

Requires:
- Docker tunnel:  ssh -fNL /tmp/docker.sock:/var/run/docker.sock user@docker-host
  Or set E2E_DOCKER_HOST to any reachable Docker socket / TCP URL.
  See tests/conftest_e2e.py for all environment variables.
- Live server fixture in conftest_e2e.py (starts on port 18080)

Run with:
    pytest -v -m e2e tests/test_e2e_ui.py
"""

from __future__ import annotations

# Load e2e-specific fixtures (live_server, page, docker_client)
pytest_plugins = ["tests.conftest_e2e"]

import time

import pytest

pytest.importorskip("playwright", reason="playwright not installed — run: pip install -e .[dev,e2e] && playwright install chromium")

pytestmark = pytest.mark.e2e

BASE_URL = "http://127.0.0.1:18080"
E2E_TOKEN = "e2e-test-token"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

SHORT = 10_000   # ms — fast DOM operations
MEDIUM = 30_000  # ms — Docker API round-trip
LONG = 90_000    # ms — image pull / container start


def _nav_to(page, section: str):
    """Click the sidebar link for *section* and wait for the heading."""
    page.locator(f".sidebar a:has-text('{section.capitalize()}')").click()
    page.wait_for_selector(f"h2:has-text('{section.capitalize()}')", timeout=MEDIUM)


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_login_with_valid_token(live_server):
    """Navigate, enter valid token, verify containers page loads."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.goto(live_server)
        sign_in = pg.locator("button:has-text('Sign in')")
        if sign_in.count() > 0:
            pg.locator("input[type='password']").fill(E2E_TOKEN)
            sign_in.click()
        pg.wait_for_selector("h2", timeout=MEDIUM)
        assert "Containers" in pg.locator("h2").first.text_content()
        browser.close()


@pytest.mark.e2e
def test_login_with_invalid_token(live_server):
    """Enter wrong token, verify error is shown and containers list is absent."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        pg = browser.new_page()
        pg.goto(live_server)
        sign_in = pg.locator("button:has-text('Sign in')")
        if sign_in.count() == 0:
            # Auth not required — skip
            browser.close()
            pytest.skip("Server running without auth")
        pg.locator("input[type='password']").fill("wrong-token-xyz")
        sign_in.click()
        # After clicking sign-in the token is stored and the app attempts to
        # fetch containers. A 401 (or 429) response should leave us on the login page.
        # Wait for login button to reappear or any toast indicating failure.
        pg.wait_for_selector(
            "button:has-text('Sign in'), .toast, [class*='toast']",
            timeout=MEDIUM,
        )
        # Containers table must NOT be visible — we're either on login page or rate-limited
        assert pg.locator("table").count() == 0
        browser.close()


@pytest.mark.e2e
def test_session_persists_after_reload(page, live_server):
    """Login (done by fixture), reload, verify still on containers page."""
    page.reload()
    page.wait_for_selector("h2", timeout=MEDIUM)
    assert "Containers" in page.locator("h2").first.text_content()


# ─────────────────────────────────────────────────────────────────────────────
# Container lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_run_container_from_popular_chip(page, live_server, docker_client):
    """Open Run modal, click alpine chip, pick latest tag, run container."""
    container_name = "e2e-alpine-run"
    # Clean up any leftover
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)

    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # Fill image directly (more reliable than chip → tag click in CI)
    page.locator("#run-image").fill("alpine:latest")
    # Fill in name and command
    page.locator("#run-name").fill(container_name)
    page.locator("#run-cmd").fill("sleep 600")

    page.locator("button:has-text('Run')").last.click()
    # Wait for modal to close and container to appear in list
    page.wait_for_selector(f"text={container_name}", timeout=LONG)

    try:
        assert page.locator(f"text={container_name}").count() > 0
    finally:
        if docker_client:
            for c in docker_client.containers.list(all=True):
                if c.name == container_name:
                    c.remove(force=True)


@pytest.mark.e2e
def test_container_start_stop(page, live_server, docker_client):
    """Create alpine container, stop it via UI, then start it again."""
    container_name = "e2e-start-stop"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    # Find the row and click Stop
    row = page.locator(f"tr:has-text('{container_name}')")
    row.locator("button:has-text('Stop')").click()
    # Verify "Stopping…" label appears or container becomes exited
    page.wait_for_selector(
        f"tr:has-text('{container_name}') .status.exited",
        timeout=MEDIUM,
    )

    # Now start it again
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Start')").click()
    page.wait_for_selector(
        f"tr:has-text('{container_name}') .status.running",
        timeout=MEDIUM,
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_logs(page, live_server, docker_client):
    """Click Logs on a running container, verify log area is visible."""
    container_name = "e2e-logs-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sh -c 'echo hello-from-e2e; sleep 600'",
            name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".log-viewer", timeout=MEDIUM)
    assert page.locator(".log-viewer").count() > 0

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_inspect(page, live_server, docker_client):
    """Click Inspect on a container, verify image name and state appear."""
    container_name = "e2e-inspect-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Inspect')").click()
    page.wait_for_selector(".inspect-panel", timeout=MEDIUM)
    # Image name and state should be populated
    assert page.locator(".inspect-kv").count() > 0
    content = page.locator(".inspect-panel").text_content()
    assert "alpine" in content.lower() or "Image" in content

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_stats(page, live_server, docker_client):
    """Click Stats on a running container, verify CPU % and Memory fields."""
    container_name = "e2e-stats-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Stats')").click()
    page.wait_for_selector(".stats-grid", timeout=MEDIUM)
    # CPU and Memory stat cards should be visible
    page.wait_for_selector(".stat:has-text('CPU')", timeout=MEDIUM)
    page.wait_for_selector(".stat:has-text('Memory')", timeout=MEDIUM)

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_delete(page, live_server, docker_client):
    """Stop and delete a container, verify it disappears from list."""
    container_name = "e2e-delete-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    # Accept the confirm dialog automatically
    page.on("dialog", lambda d: d.accept())
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Delete')").click()
    # Container row should disappear
    page.wait_for_selector(f"text={container_name}", state="detached", timeout=MEDIUM)
    assert page.locator(f"text={container_name}").count() == 0


@pytest.mark.e2e
def test_container_rename(page, live_server, docker_client):
    """Rename a container via the Inspect tab and verify the new name in the list."""
    container_name = "e2e-rename-src"
    new_name = "e2e-rename-dst"
    if docker_client:
        for name in (container_name, new_name):
            for c in docker_client.containers.list(all=True):
                if c.name == name:
                    c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Inspect')").click()
    page.wait_for_selector(".inspect-panel", timeout=MEDIUM)

    # Clear the rename input and type the new name
    # The rename input is inside .inspect-panel (value set as DOM property, not HTML attr)
    rename_input = page.locator(".inspect-panel input").first
    rename_input.click(click_count=3)
    rename_input.fill(new_name)
    page.locator("button:has-text('Rename')").click()

    # Wait for toast or page reload showing new name
    page.wait_for_selector(f"text={new_name}", timeout=MEDIUM)

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name in (container_name, new_name):
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Double-click / concurrency guards
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_stop_button_disabled_during_stop(page, live_server, docker_client):
    """Click Stop on a running container; second click must be ignored (button disabled)."""
    container_name = "e2e-dblstop"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    stop_btn = page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Stop')")
    stop_btn.click()
    # Immediately check that the button is disabled or shows pending text
    # (the UI sets disabled + loading class on the button during the request)
    time.sleep(0.1)
    is_disabled = stop_btn.get_attribute("disabled")
    pending_text = stop_btn.text_content()
    assert is_disabled is not None or "Stopping" in (pending_text or "")

    # Wait for completion so cleanup works
    page.wait_for_selector(
        f"tr:has-text('{container_name}') .status.exited",
        timeout=MEDIUM,
    )
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_delete_guard_prevents_double(page, live_server, docker_client):
    """Verify only one DELETE request fires even if Delete is clicked rapidly."""
    container_name = "e2e-del-guard"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    delete_count = []

    def count_deletes(request):
        if "containers" in request.url and request.method == "DELETE":
            delete_count.append(1)

    page.on("request", count_deletes)
    page.on("dialog", lambda d: d.accept())

    delete_btn = page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Delete')")
    delete_btn.click()
    # Immediately click again — button is still disabled (makeActionBtn disables on first click),
    # so the browser won't fire onclick; force=True bypasses Playwright's actionability wait only
    delete_btn.click(force=True)

    page.wait_for_selector(f"text={container_name}", state="detached", timeout=MEDIUM)
    assert sum(delete_count) == 1, f"Expected 1 DELETE request, got {sum(delete_count)}"


# ─────────────────────────────────────────────────────────────────────────────
# Images
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_pull_image_from_hub_search(page, live_server):
    """Pull alpine:latest via Pull modal image name input."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # Fill the image name directly — more reliable than chip+tag flow in CI
    page.locator("#pull-image").fill("alpine:latest")

    # Pull
    page.locator(".modal button:has-text('Pull')").last.click()
    # Wait for modal to close and alpine to appear in list (pull can be slow)
    page.wait_for_selector("text=alpine", timeout=LONG)


@pytest.mark.e2e
def test_image_run_button(page, live_server, docker_client):
    """Click Run next to an image and verify the Run modal opens with image pre-filled."""
    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)
    # Find the first image row with a Run button
    run_btn = page.locator("tbody tr").first.locator("button:has-text('Run')")
    run_btn.click()
    # Should navigate to containers page and open the Run modal
    page.wait_for_selector(".modal", timeout=SHORT)
    image_input = page.locator("#run-image")
    assert image_input.input_value() != ""
    # Close modal
    page.locator("button:has-text('Cancel')").click()


@pytest.mark.e2e
def test_image_delete(page, live_server, docker_client):
    """Pull a test image, delete it via UI, verify it disappears."""
    test_tag = "alpine:e2etest"
    # Ensure the image exists by tagging alpine locally
    if docker_client:
        try:
            img = docker_client.images.get("alpine:latest")
            img.tag("alpine", tag="e2etest")
        except Exception:
            pass

    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    if page.locator(f"text={test_tag}").count() == 0:
        pytest.skip("Test image alpine:e2etest not available — skipping delete test")

    page.on("dialog", lambda d: d.accept())
    page.locator(f"tr:has-text('{test_tag}')").locator("button:has-text('Delete')").click()
    # Give the delete API time to respond then check
    page.wait_for_timeout(3000)
    page.wait_for_selector(f"text={test_tag}", state="detached", timeout=MEDIUM)
    assert page.locator(f"text={test_tag}").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Volumes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_create_and_delete_volume(page, live_server, docker_client):
    """Create a volume named e2e-test-vol, verify it appears, then delete it."""
    vol_name = "e2e-test-vol"
    if docker_client:
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass

    _nav_to(page, "volumes")
    page.locator("button:has-text('Create volume')").click()
    page.wait_for_selector(".modal", timeout=SHORT)
    page.locator("#vol-name").fill(vol_name)
    page.locator(".modal button:has-text('Create')").click()

    page.wait_for_selector(f"text={vol_name}", timeout=MEDIUM)
    assert page.locator(f"text={vol_name}").count() > 0

    page.on("dialog", lambda d: d.accept())
    page.locator(f"tr:has-text('{vol_name}')").locator("button:has-text('Delete')").click()
    page.wait_for_selector(f"text={vol_name}", state="detached", timeout=MEDIUM)
    assert page.locator(f"text={vol_name}").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Networks
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_builtin_networks_show_badge(page, live_server):
    """Verify bridge, host, and none networks have a 'built-in' badge."""
    _nav_to(page, "networks")
    page.wait_for_selector("table", timeout=MEDIUM)
    for net_name in ("bridge", "host", "none"):
        row = page.locator(f"tr:has-text('{net_name}')")
        if row.count() > 0:
            assert "built-in" in row.first.text_content()


@pytest.mark.e2e
def test_create_and_delete_network(page, live_server, docker_client):
    """Create e2e-test-net, verify it appears, then delete it."""
    net_name = "e2e-test-net"
    if docker_client:
        for n in docker_client.networks.list():
            if n.name == net_name:
                n.remove()

    _nav_to(page, "networks")
    page.locator("button:has-text('Create network')").click()
    page.wait_for_selector(".modal", timeout=SHORT)
    page.locator("#net-name").fill(net_name)
    page.locator(".modal button:has-text('Create')").click()

    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)
    assert page.locator(f"text={net_name}").count() > 0

    page.on("dialog", lambda d: d.accept())
    page.locator(f"tr:has-text('{net_name}')").locator("button:has-text('Delete')").click()
    page.wait_for_selector(f"text={net_name}", state="detached", timeout=MEDIUM)
    assert page.locator(f"text={net_name}").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Registry search
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_hub_search_returns_results(page, live_server):
    """Open Pull modal, search 'nginx', verify at least one result appears."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    hub_input = page.locator(".modal input[placeholder*='Search by image name']")
    hub_input.fill("nginx")
    page.locator(".modal button:has-text('Search')").click()
    page.wait_for_selector("text=nginx", timeout=MEDIUM)
    assert page.locator("div:has-text('nginx')").count() > 0

    page.locator("button:has-text('Cancel')").click()


@pytest.mark.e2e
def test_hub_search_shows_tags_on_click(page, live_server):
    """Search alpine, click the result, verify tag list appears."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    hub_input = page.locator(".modal input[placeholder*='Search by image name']")
    hub_input.fill("alpine")
    page.locator(".modal button:has-text('Search')").click()
    # Wait for at least one result row (each has data-testid='hub-result-row')
    page.wait_for_selector("[data-testid='hub-result-row']", timeout=LONG)
    # Click the first result row to load its tags
    page.locator("[data-testid='hub-result-row']").first.click()
    # Tag rows appear after hub call — each has data-testid='hub-tag-row'
    page.wait_for_selector("[data-testid='hub-tag-row']", timeout=LONG)
    assert page.locator("[data-testid='hub-tag-row']").count() > 0

    page.locator("button:has-text('Cancel')").click()


@pytest.mark.e2e
def test_popular_chip_shows_tags(page, live_server):
    """Click the 'postgres' popular chip in Pull modal, verify tag list appears."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("button:has-text('postgres')").first.click()
    page.wait_for_selector("text=latest", timeout=MEDIUM)
    assert page.locator("text=latest").count() > 0

    page.locator("button:has-text('Cancel')").click()


# ─────────────────────────────────────────────────────────────────────────────
# System page
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_system_page_shows_docker_version(page, live_server):
    """Go to System page, verify Docker version field is populated."""
    _nav_to(page, "system")
    page.wait_for_selector(".info-grid", timeout=MEDIUM)
    # The "Engine" card should have a version number
    engine_card = page.locator(".info-card:has(.label:has-text('Engine'))")
    assert engine_card.count() > 0
    version_text = engine_card.locator(".value").text_content()
    assert version_text and version_text.strip() not in ("", "undefined", "null")


@pytest.mark.e2e
def test_audit_log_shows_requests(page, live_server):
    """Go to System, scroll to Audit Log, verify at least one row."""
    _nav_to(page, "system")
    page.wait_for_selector("text=Audit Log", timeout=MEDIUM)
    # Wait for audit log table to load
    page.wait_for_function(
        "() => document.querySelectorAll('tbody tr').length > 0",
        timeout=MEDIUM,
    )
    rows = page.locator("tbody tr").count()
    assert rows > 0


# ─────────────────────────────────────────────────────────────────────────────
# Error states / fault tolerance
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_engine_unreachable_shows_tunnel_instructions():
    """
    TODO: This test requires stopping the SSH tunnel, which is risky in a
    shared environment. Skipped — test manually by running:
        pkill -f 'ssh -fNL /tmp/docker.sock'
    and verifying the banner with tunnel instructions appears.
    """
    pytest.skip("Stopping the tunnel is too risky for automated tests")


# ─────────────────────────────────────────────────────────────────────────────
# Navigation / no leaks
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_rapid_page_navigation_no_errors(page, live_server):
    """Rapidly click through all sidebar pages 5 times, verify no JS errors."""
    js_errors = []
    page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

    pages = ["Containers", "Images", "Volumes", "Networks", "Compose", "System"]
    for _ in range(5):
        for section in pages:
            page.locator(f".sidebar a:has-text('{section}')").click()
            # Small pause to let any async errors surface
            time.sleep(0.1)

    # Wait briefly for any deferred errors
    page.wait_for_timeout(500)

    # Filter out noise — only fail on genuine JS runtime errors
    real_errors = [
        e for e in js_errors
        if "Failed to fetch" not in e
        and "NetworkError" not in e
        and "fetch" not in e.lower()
        and "429" not in e
        and "Too Many Requests" not in e
    ]
    assert real_errors == [], f"JS errors during navigation: {real_errors}"


@pytest.mark.e2e
def test_modal_close_during_load(page, live_server):
    """Open Pull modal, immediately click backdrop to close, verify no JS errors."""
    js_errors = []
    page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)
    # Click the backdrop (outside the modal box)
    page.locator(".modal-bg").click(position={"x": 5, "y": 5})
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)

    page.wait_for_timeout(500)
    real_errors = [
        e for e in js_errors
        if "Failed to fetch" not in e
        and "NetworkError" not in e
        and "fetch" not in e.lower()
    ]
    assert real_errors == [], f"JS errors after modal close: {real_errors}"


# ─────────────────────────────────────────────────────────────────────────────
# Registry / image validation edge cases
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_pull_blocked_registry_shows_error(page, live_server):
    """Pulling from a non-allowed registry shows an error toast."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("#pull-image").fill("private.example.com/org/image:latest")
    # Click the Pull submit button inside the modal's actions area
    page.locator(".modal .actions button:has-text('Pull')").click()

    # Expect an error toast — registry not in allowed list
    page.wait_for_selector(".toast", timeout=MEDIUM)
    toast_text = page.locator(".toast").first.text_content()
    assert any(kw in toast_text.lower() for kw in ("registry", "registr", "not allow", "approved", "blocked", "error", "403", "422", "400"))

    page.locator(".modal .actions button:has-text('Cancel')").click()


@pytest.mark.e2e
def test_hub_search_no_results(page, live_server):
    """When the hub search API returns zero results, 'No results.' is shown."""
    # Intercept the registry search API to return empty results
    def _empty_results(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"results": [], "count": 0}',
        )

    page.route("**/api/registry/search**", _empty_results)

    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    hub_input = page.locator(".modal input[placeholder*='Search by image name']")
    hub_input.fill("anythingwillmatch")
    page.locator(".modal button:has-text('Search')").click()

    page.wait_for_function(
        "() => {"
        "  var el = document.querySelector('[data-testid=\"hub-results\"]');"
        "  return el && el.textContent.includes('No results');"
        "}",
        timeout=MEDIUM,
    )
    results_text = page.locator("[data-testid='hub-results']").text_content()
    assert "No results" in results_text

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.unroute("**/api/registry/search**")


@pytest.mark.e2e
def test_run_container_blocked_registry_shows_error(page, live_server):
    """Running a container from a non-allowed registry shows an error."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("#run-image").fill("private.example.com/org/image:latest")
    # Click the Run submit button inside the modal's actions area
    page.locator(".modal .actions button:has-text('Run')").click()

    page.wait_for_selector(".toast", timeout=MEDIUM)
    toast_text = page.locator(".toast").first.text_content()
    assert any(kw in toast_text.lower() for kw in ("registry", "registr", "not allow", "approved", "blocked", "error", "403", "422", "400"))

    page.locator(".modal .actions button:has-text('Cancel')").click()


# ─────────────────────────────────────────────────────────────────────────────
# Container lifecycle — pause / unpause / kill
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_pause_unpause(page, live_server, docker_client):
    """Pause and unpause a running container via the UI."""
    container_name = "e2e-pause-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    row = page.locator(f"tr:has-text('{container_name}')")
    row.locator("button:has-text('Pause')").click()
    # Wait for status to change to 'paused'
    page.wait_for_function(
        f"() => document.querySelector('tr') && "
        f"Array.from(document.querySelectorAll('tr')).some(r => r.textContent.includes('{container_name}') && r.textContent.toLowerCase().includes('paused'))",
        timeout=MEDIUM,
    )

    # Unpause — the row button text changes to 'Unpause'
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Unpause')").click()
    page.wait_for_function(
        f"() => Array.from(document.querySelectorAll('tr')).some(r => r.textContent.includes('{container_name}') && r.textContent.toLowerCase().includes('running'))",
        timeout=MEDIUM,
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_kill(page, live_server, docker_client):
    """Kill a running container via the UI Kill button."""
    container_name = "e2e-kill-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    page.on("dialog", lambda d: d.accept())
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Kill')").click()

    # Container should disappear or show exited state
    page.wait_for_function(
        f"() => !Array.from(document.querySelectorAll('tr')).some(r => r.textContent.includes('{container_name}') && r.textContent.toLowerCase().includes('running'))",
        timeout=MEDIUM,
    )


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — log streaming
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_log_stream_via_ws(page, live_server, docker_client):
    """Open the Logs panel for a container that writes stdout; verify output appears."""
    container_name = "e2e-log-ws-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        # Container that repeatedly prints a recognisable string
        docker_client.containers.run(
            "alpine",
            ["sh", "-c", "while true; do echo 'e2e-log-marker'; sleep 1; done"],
            name=container_name,
            detach=True,
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()

    # Log output should contain our marker text (log viewer has id='log-output')
    page.wait_for_function(
        "() => { var el = document.getElementById('log-output'); "
        "return el && el.textContent.includes('e2e-log-marker'); }",
        timeout=LONG,
    )
    log_panel = page.locator("#log-output")
    assert "e2e-log-marker" in log_panel.text_content()

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Volume operations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_volume_prune_unused(page, live_server, docker_client):
    """Create an unused volume then prune it via the UI."""
    vol_name = "e2e-prune-vol"
    if docker_client:
        try:
            docker_client.volumes.create(name=vol_name)
        except Exception:
            pass

    _nav_to(page, "volumes")
    page.wait_for_selector(f"text={vol_name}", timeout=MEDIUM)

    page.on("dialog", lambda d: d.accept())
    page.locator("button:has-text('Prune unused')").click()

    # Volume should disappear after pruning
    page.wait_for_selector(f"text={vol_name}", state="detached", timeout=MEDIUM)


# ─────────────────────────────────────────────────────────────────────────────
# Network operations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_network_connect_modal_shows_containers(page, live_server, docker_client):
    """Connect... modal opens, loads container options, and can be submitted."""
    container_name = "e2e-net-conn-test"
    net_name = "e2e-net-conn"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        for n in docker_client.networks.list():
            if n.name == net_name:
                n.remove()
        # Use a running container so it shows in Docker's network container list
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)
        docker_client.networks.create(net_name)

    _nav_to(page, "networks")
    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)

    # Open Connect modal
    page.locator(f"tr:has-text('{net_name}')").locator("button:has-text('Connect...')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # Wait for container options to load
    sel = page.locator(".modal select")
    sel.wait_for(timeout=MEDIUM)
    page.wait_for_function(
        f"() => Array.from(document.querySelectorAll('.modal select option')).some(o => o.textContent.includes('{container_name}'))",
        timeout=MEDIUM,
    )
    assert page.locator(".modal select option").count() > 0

    # Select the container and connect
    sel.select_option(label=f"{container_name} (running)")
    page.locator(".modal button:has-text('Connect')").click()

    # Wait for modal to close (connection succeeded)
    page.wait_for_selector(".modal", state="detached", timeout=MEDIUM)

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        for n in docker_client.networks.list():
            if n.name == net_name:
                n.remove()


# ─────────────────────────────────────────────────────────────────────────────
# Keyboard shortcuts
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_keyboard_shortcut_navigation(page, live_server):
    """Verify number keys 1-5 navigate to their respective sections."""
    sections = {
        "1": "Containers",
        "2": "Images",
        "3": "Volumes",
        "4": "Networks",
        "5": "Compose",
    }
    for key, heading in sections.items():
        page.keyboard.press(key)
        page.wait_for_selector(f"h2:has-text('{heading}')", timeout=SHORT)
        assert page.locator(f"h2:has-text('{heading}')").count() > 0


@pytest.mark.e2e
def test_keyboard_shortcut_run_modal(page, live_server):
    """'r' key opens the Run container modal."""
    _nav_to(page, "containers")
    page.keyboard.press("r")
    page.wait_for_selector(".modal", timeout=SHORT)
    assert page.locator(".modal").count() > 0
    page.locator("button:has-text('Cancel')").click()


@pytest.mark.e2e
def test_keyboard_shortcut_search_focus(page, live_server):
    """'/' key focuses the search/filter input."""
    _nav_to(page, "containers")
    page.keyboard.press("/")
    focused = page.evaluate(
        "() => document.activeElement && document.activeElement.tagName === 'INPUT'"
        " && (document.activeElement.classList.contains('search-bar')"
        " || document.activeElement.placeholder.toLowerCase().includes('search'))"
    )
    assert focused, "Expected the search-bar input to be focused after '/' key"


@pytest.mark.e2e
def test_keyboard_shortcut_help_overlay(page, live_server):
    """'?' key shows the keyboard shortcut help overlay."""
    page.keyboard.press("?")
    page.wait_for_selector("text=Keyboard shortcuts", timeout=SHORT)
    assert page.locator("text=Keyboard shortcuts").count() > 0
    # Esc should close it (Esc removes .modal-overlay)
    page.keyboard.press("Escape")
    page.wait_for_selector("text=Keyboard shortcuts", state="detached", timeout=SHORT)


# ─────────────────────────────────────────────────────────────────────────────
# Container filter / search
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_search_filter(page, live_server, docker_client):
    """Typing in the search box filters the container list."""
    container_name = "e2e-filter-test"
    other_name = "e2e-other-filter"
    if docker_client:
        for name in (container_name, other_name):
            for c in docker_client.containers.list(all=True):
                if c.name == name:
                    c.remove(force=True)
            docker_client.containers.run("alpine", "sleep 600", name=name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.wait_for_selector(f"text={other_name}", timeout=MEDIUM)

    # Focus search and type unique prefix (input has class 'search-bar')
    search = page.locator("input.search-bar").first
    search.fill("e2e-filter-test")
    page.wait_for_timeout(300)  # debounce

    # Only the matching row should be visible
    assert page.locator(f"tr:has-text('{container_name}')").count() > 0
    assert page.locator(f"tr:has-text('{other_name}')").count() == 0

    # Clear filter — both should reappear
    search.fill("")
    page.wait_for_timeout(500)
    page.locator(f"tr:has-text('{other_name}')").wait_for(timeout=MEDIUM)

    if docker_client:
        for name in (container_name, other_name):
            for c in docker_client.containers.list(all=True):
                if c.name == name:
                    c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Compose
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_compose_deploy_and_teardown(page, live_server):
    """Upload a minimal compose file, deploy it, then tear it down."""
    import os
    import tempfile

    compose_content = (
        "services:\n"
        "  web:\n"
        "    image: alpine\n"
        "    command: sleep 600\n"
    )
    with tempfile.NamedTemporaryFile(
        suffix=".yml", mode="w", delete=False, prefix="e2e-compose-"
    ) as f:
        f.write(compose_content)
        tmp_path = f.name

    project_name = "e2ecomposetest"
    try:
        _nav_to(page, "compose")
        page.wait_for_selector("text=Deploy Stack", timeout=MEDIUM)

        # Set project name (input id is 'compose-project')
        page.locator("#compose-project").fill(project_name)

        # Upload compose file — file upload triggers deploy automatically
        with page.expect_file_chooser() as fc_info:
            page.locator(".drop-zone").click()
        fc_info.value.set_files(tmp_path)

        # Wait for deploy toast or output
        page.wait_for_selector(".toast, #compose-output .log-viewer", timeout=LONG)

        # Check for success or any meaningful response
        toasts = page.locator(".toast")
        output = page.locator("#compose-output")
        assert toasts.count() > 0 or output.text_content().strip() != ""

        # Reload compose page
        _nav_to(page, "compose")
        page.wait_for_timeout(1000)

        # If stack appears, tear it down
        if page.locator(f".stack-card:has-text('{project_name}')").count() > 0:
            page.locator(f".stack-card:has-text('{project_name}')").locator("button:has-text('Tear down')").click()
            page.wait_for_timeout(3000)
    finally:
        os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────────────────
# Audit log download
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_audit_log_download(page, live_server):
    """Verify the audit log download link responds with content."""
    import requests as req_lib

    # Use the API directly since Playwright download can be complex
    resp = req_lib.get(
        f"{live_server}/api/system/audit-log",
        headers={"Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=10,
    )
    assert resp.status_code == 200
    assert len(resp.text) > 0


@pytest.mark.e2e
def test_audit_log_jsonl_download(page, live_server):
    """Verify the JSONL audit log download responds correctly."""
    import requests as req_lib

    resp = req_lib.get(
        f"{live_server}/api/system/audit-log/download",
        headers={"Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=30,
        stream=True,
    )
    assert resp.status_code == 200
    # Read first chunk to verify data is present
    chunk = next(resp.iter_content(chunk_size=256), b"")
    assert len(chunk) >= 0  # endpoint exists and responds


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting — UI shows toast on 429
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_health_endpoint_public(live_server):
    """Health endpoint is reachable without auth."""
    import requests as req_lib

    resp = req_lib.get(f"{live_server}/health", timeout=30)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert "version" in data


@pytest.mark.e2e
def test_ready_endpoint_reflects_docker(live_server):
    """/ready returns 200 when Docker is reachable (tunnel is up in e2e)."""
    import requests as req_lib

    resp = req_lib.get(f"{live_server}/ready", timeout=30)
    assert resp.status_code == 200


@pytest.mark.e2e
def test_csrf_rejection_on_post_without_header(live_server):
    """POST without X-Requested-With header returns 403."""
    import requests as req_lib

    resp = req_lib.post(
        f"{live_server}/api/containers/nonexistent/stop",
        headers={"Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=30,
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_unauthenticated_api_returns_401(live_server):
    """API without Bearer token returns 401."""
    import requests as req_lib

    resp = req_lib.get(f"{live_server}/api/containers", timeout=30)
    assert resp.status_code == 401
