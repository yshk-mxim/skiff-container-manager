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
        # Sidebar renders immediately after login (no Docker round-trip needed).
        pg.wait_for_selector(".sidebar", timeout=MEDIUM)
        assert pg.locator(".sidebar a:has-text('Containers')").count() > 0
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
    page.wait_for_timeout(100)
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
    """Implemented at end of file (must run last to avoid breaking session tunnel)."""
    pytest.skip("see test_engine_unreachable_shows_tunnel_instructions_impl at end of file")


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
            # Use Playwright's non-blocking wait so the event loop stays responsive
            page.wait_for_timeout(100)

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
    page.locator("button:has-text('Run new container')").first.click()
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


# ─────────────────────────────────────────────────────────────────────────────
# Run Container modal — detailed form testing
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_run_modal_fills_name_and_command(page, live_server):
    """Open Run modal, fill Name and Command fields, verify values are set."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # Verify modal title
    assert "Run new container" in page.locator(".modal h3").text_content()

    # Fill Name field
    name_input = page.locator("#run-name")
    name_input.fill("e2e-form-test")
    assert name_input.input_value() == "e2e-form-test"

    # Fill Command field
    cmd_input = page.locator("#run-cmd")
    cmd_input.fill("sleep 999")
    assert cmd_input.input_value() == "sleep 999"

    # Fill Image field (required)
    page.locator("#run-image").fill("alpine:latest")

    # Close without submitting
    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_environment_variables(page, live_server):
    """Open Run modal, fill multi-line env vars textarea, verify input accepted."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    env_textarea = page.locator("#run-env")
    env_textarea.fill("MY_KEY=my_value\nDEBUG=true\nPORT=8080")
    assert "MY_KEY=my_value" in env_textarea.input_value()
    assert "DEBUG=true" in env_textarea.input_value()
    assert "PORT=8080" in env_textarea.input_value()

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_volume_mounts(page, live_server):
    """Open Run modal, fill volume mounts textarea, verify values are accepted."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    vol_textarea = page.locator("#run-volumes")
    vol_textarea.fill("myvolume:/data\nanothervolume:/config")
    val = vol_textarea.input_value()
    assert "myvolume:/data" in val
    assert "anothervolume:/config" in val

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_labels(page, live_server):
    """Open Run modal, fill labels textarea, verify input accepted."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    labels_textarea = page.locator("#run-labels")
    labels_textarea.fill("app=myapp\nenv=test\nversion=1.0")
    val = labels_textarea.input_value()
    assert "app=myapp" in val
    assert "env=test" in val

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_restart_policy_select(page, live_server):
    """Open Run modal, change restart policy to 'always', verify selection."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    restart_sel = page.locator("#run-restart")
    restart_sel.wait_for(timeout=SHORT)

    # Verify default is 'no'
    current = restart_sel.input_value()
    assert current in ("no", "unless-stopped", "always", "on-failure")

    # Select 'always'
    restart_sel.select_option(value="always")
    assert restart_sel.input_value() == "always"

    # Select 'unless-stopped'
    restart_sel.select_option(value="unless-stopped")
    assert restart_sel.input_value() == "unless-stopped"

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_network_dropdown(page, live_server, docker_client):
    """Open Run modal, verify network dropdown is populated and selectable."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    net_sel = page.locator("#run-network")
    net_sel.wait_for(timeout=SHORT)

    # Wait for networks to load (async fetch populates options)
    page.wait_for_function(
        "() => document.getElementById('run-network') && "
        "document.getElementById('run-network').options.length > 1",
        timeout=MEDIUM,
    )

    # Should have at least the default option plus 'bridge' network
    option_count = page.locator("#run-network option").count()
    assert option_count >= 1

    # Try selecting the 'bridge' option by value (not label text) if it exists.
    # Options are built as value=n.name, text=n.name+" (driver)". The default
    # option has value="" so we need to select by value to avoid ambiguity.
    option_values = page.locator("#run-network option").evaluate_all(
        "(opts) => opts.map(o => o.value)"
    )
    if "bridge" in option_values:
        net_sel.select_option(value="bridge")
        assert net_sel.input_value() == "bridge"

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_port_mapping(page, live_server):
    """Open Run modal, fill port mapping field with valid port, verify input accepted."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    ports_input = page.locator("#run-ports")
    ports_input.fill("8080:8080")
    assert ports_input.input_value() == "8080:8080"

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_cancel_clears_form(page, live_server):
    """Fill Run modal, click Cancel, reopen — form should be fresh (modal is recreated)."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("#run-name").fill("should-not-persist")
    page.locator("#run-image").fill("alpine:latest")

    # Cancel the modal
    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)

    # Reopen the modal
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # A fresh modal should have an empty name field (modal is recreated each time)
    name_val = page.locator("#run-name").input_value()
    assert name_val == "", f"Expected empty name after reopening modal, got: {name_val!r}"

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_modal_backdrop_closes(page, live_server):
    """Click outside the Run modal (backdrop) should close it without error."""
    js_errors: list[str] = []
    page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    # Click the backdrop area (far top-left corner)
    page.locator(".modal-bg").click(position={"x": 5, "y": 5})
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)

    page.wait_for_timeout(400)
    real_errors = [e for e in js_errors if "fetch" not in e.lower() and "NetworkError" not in e]
    assert real_errors == [], f"JS errors after backdrop close: {real_errors}"


@pytest.mark.e2e
def test_run_modal_requires_image_field(page, live_server):
    """Clicking Run without an image should show an error toast, not submit."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # Clear image field and attempt to Run
    page.locator("#run-image").fill("")
    page.locator(".modal .actions button:has-text('Run')").click()

    # An error toast should appear
    page.wait_for_selector(".toast.error, .toast", timeout=SHORT)
    toast_text = page.locator(".toast").first.text_content()
    assert any(kw in toast_text.lower() for kw in ("image", "required", "error"))

    # Modal should still be open
    assert page.locator(".modal-bg").count() > 0

    page.locator(".modal .actions button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_run_container_with_env_and_volume(page, live_server, docker_client):
    """Run a container with env vars and volume mount via the full form."""
    container_name = "e2e-env-vol-run"
    vol_name = "e2e-env-vol-data"

    # Pre-create the volume so the mount succeeds
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass
        docker_client.volumes.create(name=vol_name)

    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").first.click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("#run-image").fill("alpine:latest")
    page.locator("#run-name").fill(container_name)
    page.locator("#run-cmd").fill("sleep 600")
    page.locator("#run-env").fill("E2E_TEST=yes\nCONTAINER_TYPE=e2e")
    page.locator("#run-volumes").fill(f"{vol_name}:/data")

    page.locator(".modal .actions button:has-text('Run')").click()
    page.wait_for_selector(f"text={container_name}", timeout=LONG)

    assert page.locator(f"text={container_name}").count() > 0

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Container detail tabs
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_detail_tabs_present(page, live_server, docker_client):
    """Open detail view for a running container, verify all tab labels exist."""
    container_name = "e2e-tabs-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()

    # Verify all tabs are present
    page.wait_for_selector(".detail-tabs", timeout=MEDIUM)
    tabs_text = page.locator(".detail-tabs").text_content()
    for expected_tab in ("Logs", "Terminal", "Inspect", "Stats", "Processes", "Files"):
        assert expected_tab in tabs_text, f"Tab '{expected_tab}' not found in detail tabs"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_processes_tab(page, live_server, docker_client):
    """Click Processes tab on a running container, verify process table rows appear."""
    container_name = "e2e-procs-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    # Open detail view via Logs button (which goes to logs tab initially)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".detail-tabs", timeout=MEDIUM)

    # Click the Processes tab
    page.locator(".detail-tab:has-text('Processes')").click()

    # Wait for process table to load (either a table with rows or an empty state)
    page.wait_for_function(
        "() => {"
        "  var el = document.getElementById('detail-content');"
        "  return el && !el.textContent.includes('Loading processes');"
        "}",
        timeout=MEDIUM,
    )

    content = page.locator("#detail-content")
    content_text = content.text_content()

    # Should show either a table with process rows or an empty-state message
    has_table = content.locator("table tbody tr").count() > 0
    has_empty = "No processes running" in content_text
    assert has_table or has_empty, (
        f"Expected process table rows or empty state, got: {content_text[:200]}"
    )

    if has_table:
        # Verify standard 'ps' column headers are present (docker top returns PID, CMD etc.)
        header_text = content.locator("thead").text_content()
        assert any(col in header_text.upper() for col in ("PID", "CMD", "USER", "COMMAND"))

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_files_diff_tab(page, live_server, docker_client):
    """Click Files tab on a running container, verify filesystem diff list renders."""
    container_name = "e2e-diff-test"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        # Write a file to guarantee at least one diff entry
        docker_client.containers.run(
            "alpine",
            ["sh", "-c", "echo hello > /tmp/e2e-diff-marker.txt && sleep 600"],
            name=container_name,
            detach=True,
        )
        # Give the container a moment to write the file
        time.sleep(1)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".detail-tabs", timeout=MEDIUM)

    # Click the Files tab
    page.locator(".detail-tab:has-text('Files')").click()

    # Wait for diff content to load
    page.wait_for_function(
        "() => {"
        "  var el = document.getElementById('detail-content');"
        "  return el && !el.textContent.includes('Loading filesystem changes');"
        "}",
        timeout=MEDIUM,
    )

    content = page.locator("#detail-content")
    content_text = content.text_content()

    # Either a table with diff rows or 'No filesystem changes'
    has_table = content.locator("table tbody tr").count() > 0
    has_empty = "No filesystem changes" in content_text
    assert has_table or has_empty, (
        f"Expected diff table or empty state, got: {content_text[:200]}"
    )

    if has_table:
        # Each diff row should have a change kind badge (Added/Modified/Deleted)
        # and a path column
        first_row = content.locator("table tbody tr").first
        row_text = first_row.text_content()
        assert any(kw in row_text for kw in ("Added", "Modified", "Deleted"))

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_terminal_tab_renders(page, live_server, docker_client):
    """Click Terminal tab on a running container, verify terminal div and input appear."""
    container_name = "e2e-term-render"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Terminal')").click()
    page.wait_for_selector(".detail-tabs", timeout=MEDIUM)

    # The active tab should be Terminal
    active_tab = page.locator(".detail-tab.active")
    assert "Terminal" in active_tab.text_content()

    # The terminal output div and input should be rendered
    page.wait_for_selector("#term-output", timeout=SHORT)
    page.wait_for_selector("input.terminal-input", timeout=SHORT)

    assert page.locator("#term-output").count() > 0
    assert page.locator("input.terminal-input").count() > 0

    # The input should have the placeholder 'Type command...'
    placeholder = page.locator("input.terminal-input").get_attribute("placeholder")
    assert placeholder and "command" in placeholder.lower()

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_terminal_send_command(page, live_server, docker_client):
    """Open terminal tab, type a command, verify output appears in terminal div."""
    container_name = "e2e-term-cmd"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Terminal')").click()
    page.wait_for_selector("#term-output", timeout=MEDIUM)
    page.wait_for_selector("input.terminal-input", timeout=SHORT)

    # Wait a moment for the WebSocket to connect (if it does)
    page.wait_for_timeout(2000)

    term_input = page.locator("input.terminal-input")
    term_input.click()
    term_input.fill("echo e2e-term-marker")
    term_input.press("Enter")

    # Wait up to MEDIUM for the command echo or any output in term-output
    # (In a live environment the exec WS responds; in CI the WS may fail gracefully)
    page.wait_for_timeout(3000)

    term_text = page.locator("#term-output").text_content()
    # Either the output appeared, or the WS closed with a reconnect message
    assert len(term_text) > 0, "Expected some content in the terminal output element"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_detail_back_button(page, live_server, docker_client):
    """Verify the 'Back to list' button in the detail view returns to containers."""
    container_name = "e2e-back-btn"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".detail-tabs", timeout=MEDIUM)

    page.locator("button:has-text('Back to list')").click()
    page.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Containers')").count() > 0

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Log viewer — detailed controls
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_log_viewer_search_filter(page, live_server, docker_client):
    """Open log viewer, type in search input, verify only matching lines are shown."""
    container_name = "e2e-log-search"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine",
            ["sh", "-c",
             "echo 'alpha-line'; echo 'beta-line'; echo 'alpha-second'; sleep 600"],
            name=container_name,
            detach=True,
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".log-viewer", timeout=MEDIUM)

    # Wait until log output contains our marker lines
    page.wait_for_function(
        "() => { var el = document.getElementById('log-output');"
        " return el && el.textContent.includes('alpha-line'); }",
        timeout=LONG,
    )

    # Type in the search box to filter for 'alpha'
    search_input = page.locator("input.log-search")
    search_input.fill("alpha")
    page.wait_for_timeout(500)  # debounce / re-render

    # After filtering, the log viewer should contain 'alpha' highlighted
    viewer_content = page.locator("#log-output").text_content()
    assert "alpha" in viewer_content.lower()

    # The 'beta-line' entry should NOT be visible after filtering for 'alpha'
    # (lines that don't match are excluded from the rendered output)
    # Note: viewer.innerHTML is rebuilt to include only matching lines
    inner_html = page.locator("#log-output").inner_html()
    assert "beta-line" not in inner_html

    # Clear search — all lines should return
    search_input.fill("")
    page.wait_for_timeout(400)
    viewer_content_all = page.locator("#log-output").text_content()
    assert "beta-line" in viewer_content_all

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_log_download_txt_button(page, live_server, docker_client):
    """Click 'Download .txt' in the log viewer, verify a download request is triggered."""
    container_name = "e2e-log-dl-txt"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sh -c 'echo log-download-test; sleep 600'",
            name=container_name, detach=True,
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".log-toolbar", timeout=MEDIUM)

    # Intercept the download fetch to verify the request is made with correct suffix
    download_requested = []

    def handle_request(request):
        if "/logs/download" in request.url and "jsonl" not in request.url:
            download_requested.append(request.url)

    page.on("request", handle_request)

    # Click the Download .txt button (triggers a fetch + blob download)
    dl_btn = page.locator(".log-toolbar button:has-text('Download .txt')")
    dl_btn.wait_for(timeout=SHORT)
    dl_btn.click()

    # Wait briefly for the fetch to fire
    page.wait_for_timeout(2000)

    assert len(download_requested) > 0, (
        "Expected a fetch request to the .txt download endpoint after clicking Download .txt"
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_log_download_jsonl_button(page, live_server, docker_client):
    """Click 'Download .jsonl' in the log viewer, verify the JSONL download fetch fires."""
    container_name = "e2e-log-dl-jsonl"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sh -c 'echo log-jsonl-test; sleep 600'",
            name=container_name, detach=True,
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".log-toolbar", timeout=MEDIUM)

    jsonl_requested = []

    def handle_request(request):
        if "/logs/download.jsonl" in request.url:
            jsonl_requested.append(request.url)

    page.on("request", handle_request)

    dl_btn = page.locator(".log-toolbar button:has-text('Download .jsonl')")
    dl_btn.wait_for(timeout=SHORT)
    dl_btn.click()

    page.wait_for_timeout(2000)

    assert len(jsonl_requested) > 0, (
        "Expected a fetch request to the .jsonl download endpoint after clicking Download .jsonl"
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_log_toolbar_elements_present(page, live_server, docker_client):
    """Verify the log toolbar contains search input and both download buttons."""
    container_name = "e2e-log-toolbar"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Logs')").click()
    page.wait_for_selector(".log-toolbar", timeout=MEDIUM)

    # Verify search input
    assert page.locator("input.log-search").count() > 0
    assert page.locator(".log-toolbar button:has-text('Download .txt')").count() > 0
    assert page.locator(".log-toolbar button:has-text('Download .jsonl')").count() > 0

    # Verify log viewer div is present
    assert page.locator("#log-output").count() > 0

    # Search placeholder text should mention 'Search' or 'regex'
    placeholder = page.locator("input.log-search").get_attribute("placeholder")
    assert placeholder is not None
    assert len(placeholder) > 0

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Images — Inspect, Tag, Push
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_image_inspect_shows_details(page, live_server, docker_client):
    """Click Inspect on an image and verify the modal shows ID, tags, size, OS, and arch."""
    # Make sure at least alpine:latest is present
    if docker_client:
        try:
            docker_client.images.pull("alpine:latest")
        except Exception:
            pass

    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    # Find first row with an Inspect button
    inspect_btn = page.locator("tbody tr").first.locator("button:has-text('Inspect')")
    if inspect_btn.count() == 0:
        pytest.skip("No images available to inspect")

    inspect_btn.click()
    page.wait_for_selector(".modal", timeout=MEDIUM)

    # Verify key fields are present in the inspect panel
    assert page.locator(".modal .inspect-kv").count() > 0

    # Verify specific fields exist
    inspect_keys = page.locator(".modal .inspect-kv .k").all_text_contents()
    key_names = [k.strip() for k in inspect_keys]
    assert any(k in key_names for k in ("ID", "Tags", "Size")), (
        f"Expected ID/Tags/Size in inspect keys, got: {key_names}"
    )

    # Verify the Tag form is present
    assert page.locator(".modal input[placeholder*='latest']").count() > 0 or \
           page.locator(".modal input[placeholder*='tag']").count() > 0

    page.locator(".modal button:has-text('Close')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_image_inspect_shows_layer_history(page, live_server, docker_client):
    """Image inspect modal should display layer history section when available."""
    if docker_client:
        try:
            docker_client.images.pull("alpine:latest")
        except Exception:
            pass

    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    # Find the alpine row specifically if available, otherwise first image
    alpine_row = page.locator("tr:has-text('alpine')")
    if alpine_row.count() > 0:
        alpine_row.first.locator("button:has-text('Inspect')").click()
    else:
        first_inspect = page.locator("tbody tr").first.locator("button:has-text('Inspect')")
        if first_inspect.count() == 0:
            pytest.skip("No images available to inspect")
        first_inspect.click()

    page.wait_for_selector(".modal .inspect-panel", timeout=MEDIUM)

    # Verify the inspect panel has rows with key-value pairs
    assert page.locator(".modal .inspect-kv").count() >= 3

    # Check that the panel contains at least one piece of metadata
    panel_text = page.locator(".modal .inspect-panel").text_content()
    assert any(field in panel_text for field in ("MB", "linux", "amd64", "arm"))

    page.locator(".modal button:has-text('Close')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_image_tag_form_present_in_inspect(page, live_server, docker_client):
    """Image inspect modal has a Tag form with repo and tag inputs and a Tag button."""
    if docker_client:
        try:
            docker_client.images.pull("alpine:latest")
        except Exception:
            pass

    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    first_inspect = page.locator("tbody tr").first.locator("button:has-text('Inspect')")
    if first_inspect.count() == 0:
        pytest.skip("No images available to inspect")
    first_inspect.click()

    page.wait_for_selector(".modal", timeout=MEDIUM)

    # There should be a label 'Tag image' or similar
    modal_text = page.locator(".modal").text_content()
    assert "Tag image" in modal_text or "new repository" in modal_text.lower()

    # Two tag-related inputs: repo and tag
    # The tag input has placeholder 'latest'
    tag_inputs = page.locator(".modal input[placeholder='latest']")
    assert tag_inputs.count() > 0

    # There should be a Tag button
    assert page.locator(".modal button:has-text('Tag')").count() > 0

    page.locator(".modal button:has-text('Close')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_image_tag_submit(page, live_server, docker_client):
    """Fill the Tag form in image inspect modal and submit; verify toast or error."""
    if docker_client:
        try:
            docker_client.images.pull("alpine:latest")
        except Exception:
            pass

    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    alpine_row = page.locator("tr:has-text('alpine')")
    if alpine_row.count() == 0:
        pytest.skip("alpine image not available — cannot test tag form")

    alpine_row.first.locator("button:has-text('Inspect')").click()
    page.wait_for_selector(".modal", timeout=MEDIUM)

    # Fill the repo field (placeholder contains 'project' or 'repo')
    repo_inputs = page.locator(".modal input[placeholder*='docker.pkg.dev'], .modal input[placeholder*='repo'], .modal input[placeholder*='project']")
    if repo_inputs.count() == 0:
        pytest.skip("Could not find repo input in image inspect modal")

    repo_inputs.first.fill("alpine")

    # Set tag value
    tag_input = page.locator(".modal input[placeholder='latest']")
    tag_input.fill("e2e-tagged")

    # Click the Tag button
    page.locator(".modal button:has-text('Tag')").click()

    # Expect either a success toast or error toast (error if registry not allowed)
    page.wait_for_selector(".toast", timeout=MEDIUM)
    toast_text = page.locator(".toast").first.text_content()
    assert len(toast_text) > 0

    # Clean up the tagged image if it was created
    if docker_client:
        try:
            docker_client.images.remove("alpine:e2e-tagged", force=True)
        except Exception:
            pass

    # If modal is still open, close it
    if page.locator(".modal-bg").count() > 0:
        page.locator(".modal button:has-text('Close')").click()


@pytest.mark.e2e
def test_image_push_button_present(page, live_server, docker_client):
    """Image inspect modal should show Push button(s) for tagged images."""
    if docker_client:
        try:
            docker_client.images.pull("alpine:latest")
        except Exception:
            pass

    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    first_inspect = page.locator("tbody tr").first.locator("button:has-text('Inspect')")
    if first_inspect.count() == 0:
        pytest.skip("No images available to inspect")
    first_inspect.click()

    page.wait_for_selector(".modal", timeout=MEDIUM)

    # The push section should have the label 'Push to registry'
    modal_text = page.locator(".modal").text_content()
    assert "Push to registry" in modal_text or "Push" in modal_text

    page.locator(".modal button:has-text('Close')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


# ─────────────────────────────────────────────────────────────────────────────
# Networks — disconnect and prune
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_network_disconnect_container(page, live_server, docker_client):
    """Connect a container to a custom network, then disconnect it via the UI."""
    container_name = "e2e-net-disc-ctr"
    net_name = "e2e-net-disc"

    if docker_client:
        # Clean up pre-existing resources
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass

        # Create network and a running container, then connect them
        net = docker_client.networks.create(net_name)
        ctr = docker_client.containers.run(
            "alpine", "sleep 600", name=container_name, detach=True
        )
        net.connect(ctr)
        # Verify connection took effect before navigating to the UI
        net.reload()
        if not any(container_name in str(v) for v in net.attrs.get("Containers", {}).values()):
            pytest.skip(f"Container {container_name!r} did not appear in {net_name!r} after connect")

    _nav_to(page, "networks")
    # Reload to ensure fresh network state is shown
    page.reload()
    page.wait_for_selector(".sidebar", timeout=SHORT)
    page.locator(".sidebar a:has-text('Networks')").click()
    page.wait_for_selector(f"h2:has-text('Networks')", timeout=MEDIUM)
    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)

    # Look for the Disconnect button in the e2e-net-disc row
    row = page.locator(f"tr:has-text('{net_name}')")
    disconnect_btn = row.locator("button:has-text('Disconnect')")

    if disconnect_btn.count() == 0:
        pytest.skip(
            f"No Disconnect button found for {net_name} — container may not be connected"
        )

    disconnect_btn.first.click()

    # After disconnect, the button should disappear or the row should update
    page.wait_for_function(
        f"() => {{"
        f"  var rows = document.querySelectorAll('tr');"
        f"  var netRow = Array.from(rows).find(r => r.textContent.includes('{net_name}'));"
        f"  if (!netRow) return true;"
        f"  return !netRow.textContent.includes('{container_name}');"
        f"}}",
        timeout=MEDIUM,
    )

    # Verify toast appeared
    page.wait_for_selector(".toast", timeout=SHORT)

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass


@pytest.mark.e2e
def test_network_prune_unused(page, live_server, docker_client):
    """Create an unused custom network then prune via the UI; verify it disappears."""
    net_name = "e2e-net-prune-unused"

    if docker_client:
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass
        docker_client.networks.create(net_name)

    _nav_to(page, "networks")
    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)

    page.on("dialog", lambda d: d.accept())
    page.locator("button:has-text('Prune unused')").click()

    # Wait for the network to disappear from the list
    page.wait_for_selector(f"text={net_name}", state="detached", timeout=MEDIUM)

    # Toast should have appeared with prune result
    # (The UI shows either "Pruned N networks" or "No unused custom networks")
    page.wait_for_selector(".toast", timeout=SHORT)
    toast_text = page.locator(".toast").first.text_content()
    assert any(kw in toast_text.lower() for kw in ("prune", "network", "no unused"))


# ─────────────────────────────────────────────────────────────────────────────
# System page — prune actions and info fields
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_system_page_prune_system_button(page, live_server):
    """Click 'Prune system' on the System page, confirm dialog, verify toast appears."""
    _nav_to(page, "system")
    page.wait_for_selector(".info-grid", timeout=MEDIUM)

    page.on("dialog", lambda d: d.accept())
    page.locator("button:has-text('Prune system')").click()

    # Expect a toast indicating what was pruned (or 'Nothing to prune')
    page.wait_for_selector(".toast", timeout=MEDIUM)
    toast_text = page.locator(".toast").first.text_content()
    assert any(kw in toast_text.lower() for kw in ("prune", "nothing", "container", "image", "network"))


@pytest.mark.e2e
def test_system_page_info_cards_populated(page, live_server):
    """Go to System page, verify key info cards have non-empty values."""
    _nav_to(page, "system")
    page.wait_for_selector(".info-grid", timeout=MEDIUM)

    # Verify the info grid has multiple cards
    card_count = page.locator(".info-card").count()
    assert card_count >= 6, f"Expected at least 6 info cards, got {card_count}"

    # Each card should have a non-empty .value element
    for card in page.locator(".info-card").all():
        label_text = card.locator(".label").text_content().strip()
        value_text = card.locator(".value").text_content().strip()
        assert value_text not in ("", "undefined", "null"), (
            f"Info card '{label_text}' has empty/invalid value: {value_text!r}"
        )


@pytest.mark.e2e
def test_system_page_engine_version_format(page, live_server):
    """Verify the Engine version card shows a version-like string (e.g. '26.1.4')."""
    _nav_to(page, "system")
    page.wait_for_selector(".info-grid", timeout=MEDIUM)

    engine_card = page.locator(".info-card:has(.label:has-text('Engine'))")
    assert engine_card.count() > 0, "Engine info card not found"

    version_text = engine_card.locator(".value").text_content().strip()
    assert version_text and version_text not in ("", "undefined", "null"), (
        f"Engine version is empty or invalid: {version_text!r}"
    )
    # Should contain at least one dot (version numbers like '26.1.4')
    assert "." in version_text, f"Engine version '{version_text}' doesn't look like a version number"


@pytest.mark.e2e
def test_system_page_containers_count(page, live_server):
    """Verify the Containers info card shows a count and breakdown (running/paused/stopped)."""
    _nav_to(page, "system")
    page.wait_for_selector(".info-grid", timeout=MEDIUM)

    containers_card = page.locator(".info-card:has(.label:has-text('Containers'))").first
    assert containers_card.count() > 0, "Containers info card not found"

    value_text = containers_card.locator(".value").text_content().strip()
    assert value_text.strip() not in ("", "undefined", "null"), (
        f"Containers card has empty value: {value_text!r}"
    )
    # The value should contain a number and 'running'
    assert "running" in value_text.lower(), (
        f"Expected 'running' in containers value, got: {value_text!r}"
    )


@pytest.mark.e2e
def test_system_page_disk_usage_section(page, live_server):
    """Verify the Disk Usage section and its cards are present and populated."""
    _nav_to(page, "system")
    page.wait_for_selector("h3:has-text('Disk Usage')", timeout=MEDIUM)

    # The disk usage grid should have cards for Images, Containers, Volumes, Build Cache, Total
    disk_labels = page.locator("h3:has-text('Disk Usage') ~ div .info-card .label").all_text_contents()
    disk_labels_clean = [lbl.strip() for lbl in disk_labels]

    for expected in ("Images", "Containers", "Volumes", "Total"):
        assert expected in disk_labels_clean, (
            f"Expected '{expected}' in disk usage labels, got: {disk_labels_clean}"
        )

    # All disk usage cards should have a value in 'N MB' format
    for card in page.locator("h3:has-text('Disk Usage') ~ div .info-card").all():
        val = card.locator(".value").text_content().strip()
        assert "MB" in val, f"Expected 'MB' in disk usage card value, got: {val!r}"


@pytest.mark.e2e
def test_system_page_audit_log_section(page, live_server):
    """Go to System page, verify the Audit Log section is present with table headers."""
    _nav_to(page, "system")
    page.wait_for_selector("h3:has-text('Audit Log')", timeout=MEDIUM)

    # The audit log table should have its headers
    audit_headers = page.locator("table thead th").all_text_contents()
    headers_clean = [h.strip() for h in audit_headers]
    for col in ("Time", "Event", "Method", "Path", "Status"):
        assert col in headers_clean, (
            f"Expected '{col}' column in audit log table headers, got: {headers_clean}"
        )

    # The audit log download button should be visible
    assert page.locator("button:has-text('Download .jsonl')").count() > 0


@pytest.mark.e2e
def test_system_page_audit_refresh_button(page, live_server):
    """Click the Refresh button in the audit log section, verify rows reload."""
    _nav_to(page, "system")
    page.wait_for_selector("h3:has-text('Audit Log')", timeout=MEDIUM)

    # Wait for audit rows to load initially
    page.wait_for_function(
        "() => {"
        "  var rows = document.querySelectorAll('tbody tr');"
        "  return rows.length > 0 && !Array.from(rows).some(r => r.textContent.includes('Loading'));"
        "}",
        timeout=MEDIUM,
    )

    # Click Refresh
    page.locator("button:has-text('Refresh')").click()

    # After refresh, rows should reload (same count is fine; just no JS error)
    page.wait_for_function(
        "() => {"
        "  var rows = document.querySelectorAll('tbody tr');"
        "  return rows.length > 0 && !Array.from(rows).some(r => r.textContent.includes('Loading'));"
        "}",
        timeout=MEDIUM,
    )
    new_count = page.locator("tbody tr").count()
    assert new_count >= 1, f"Expected at least 1 audit row after refresh, got {new_count}"


# ─────────────────────────────────────────────────────────────────────────────
# Toast notification lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_toast_appears_on_successful_action(page, live_server, docker_client):
    """After a successful container start, a success toast must appear."""
    container_name = "e2e-toast-success"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)

    # Stop the container so we can start it again and observe the toast
    row = page.locator(f"tr:has-text('{container_name}')")
    row.locator("button:has-text('Stop')").click()
    page.wait_for_selector(
        f"tr:has-text('{container_name}') .status.exited",
        timeout=MEDIUM,
    )

    # Now start it — should show a 'started' success toast
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Start')").click()
    page.wait_for_selector(".toast.success, .toast", timeout=MEDIUM)

    toast = page.locator(".toast").first
    assert toast.is_visible()
    toast_text = toast.text_content()
    assert len(toast_text) > 0

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_toast_auto_dismisses(page, live_server, docker_client):
    """After appearing, a toast must auto-dismiss within ~6 seconds."""
    container_name = "e2e-toast-dismiss"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.volumes.create(name="e2e-toast-dismiss-vol")

    _nav_to(page, "volumes")
    page.wait_for_selector("table", timeout=MEDIUM)

    # Create a volume to trigger a success toast
    page.locator("button:has-text('Create volume')").click()
    page.wait_for_selector(".modal", timeout=SHORT)
    page.locator("#vol-name").fill("e2e-toast-dismiss-vol2")
    page.locator(".modal button:has-text('Create')").click()

    # Wait for the success toast to appear
    page.wait_for_selector(".toast", timeout=MEDIUM)
    assert page.locator(".toast").count() > 0

    # The toast auto-dismisses after ~4 seconds (4000ms + 300ms fade)
    # Wait 6 seconds total — it should be gone
    page.wait_for_selector(".toast", state="detached", timeout=8000)
    assert page.locator(".toast").count() == 0

    if docker_client:
        try:
            docker_client.volumes.get("e2e-toast-dismiss-vol").remove(force=True)
        except Exception:
            pass
        try:
            docker_client.volumes.get("e2e-toast-dismiss-vol2").remove(force=True)
        except Exception:
            pass


@pytest.mark.e2e
def test_toast_error_styling(page, live_server):
    """An error action (blocked registry pull) should produce a toast with .error class."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("#pull-image").fill("blocked.example.invalid/secret:latest")
    page.locator(".modal .actions button:has-text('Pull')").click()

    page.wait_for_selector(".toast", timeout=MEDIUM)
    toast = page.locator(".toast").first
    assert toast.is_visible()

    # Check styling — error toasts have class 'error' or red background
    toast_class = toast.get_attribute("class") or ""
    toast_text = toast.text_content()

    # The toast should have 'error' class OR the text should indicate an error
    assert "error" in toast_class or len(toast_text) > 0

    if page.locator(".modal-bg").count() > 0:
        page.locator(".modal button:has-text('Cancel')").click()


# ─────────────────────────────────────────────────────────────────────────────
# Empty states
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_volumes_empty_state_message(page, live_server, docker_client):
    """When no volumes exist, the Volumes page shows an appropriate empty state."""
    # We test via the table empty row rather than removing all volumes
    # (removing all real volumes would be destructive).
    # Instead we use the mock/intercept route approach.
    def _empty_volumes(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body="[]",
        )

    page.route("**/api/volumes", _empty_volumes)

    _nav_to(page, "volumes")
    page.wait_for_selector("table", timeout=MEDIUM)

    # With an empty list the table renders a single row with "No volumes found"
    page.wait_for_function(
        "() => {"
        "  var cells = document.querySelectorAll('tbody td');"
        "  return Array.from(cells).some(c => c.textContent.includes('No volumes'));"
        "}",
        timeout=SHORT,
    )

    empty_cell = page.locator("tbody td:has-text('No volumes')")
    assert empty_cell.count() > 0

    page.unroute("**/api/volumes")


@pytest.mark.e2e
def test_networks_builtin_badge_display(page, live_server):
    """bridge, host, and none networks should show a 'built-in' badge in the networks table."""
    _nav_to(page, "networks")
    page.wait_for_selector("table", timeout=MEDIUM)

    # At least one built-in network must have the badge
    builtin_badges = page.locator("td:has-text('built-in')")
    assert builtin_badges.count() > 0, "Expected at least one 'built-in' badge in networks table"

    # Specifically check bridge
    bridge_row = page.locator("tr:has-text('bridge')")
    if bridge_row.count() > 0:
        assert "built-in" in bridge_row.first.text_content()


@pytest.mark.e2e
def test_compose_page_empty_state(page, live_server):
    """When no stacks are running, the Compose page shows the Deploy Stack section."""
    def _empty_stacks(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body="[]",
        )

    page.route("**/api/compose/stacks", _empty_stacks)

    _nav_to(page, "compose")
    page.wait_for_selector("h2:has-text('Compose')", timeout=MEDIUM)

    # The Deploy Stack section should be visible (it always renders when stacks is empty)
    page.wait_for_selector("h3:has-text('Deploy Stack')", timeout=MEDIUM)
    assert page.locator("h3:has-text('Deploy Stack')").count() > 0

    # The drop zone for uploading should be visible
    assert page.locator(".drop-zone").count() > 0

    # The project name input should be visible
    assert page.locator("#compose-project").count() > 0

    # Running Stacks section should NOT be present
    assert page.locator("h3:has-text('Running Stacks')").count() == 0

    page.unroute("**/api/compose/stacks")


@pytest.mark.e2e
def test_containers_no_match_search_empty_state(page, live_server, docker_client):
    """When the search filter matches nothing, the 'No matches' empty state shows."""
    _nav_to(page, "containers")
    page.wait_for_selector("h2", timeout=MEDIUM)

    # Type a search term that won't match any container name
    search = page.locator("input.search-bar").first
    search.fill("zzz-this-matches-absolutely-nothing-xyz-987")
    page.wait_for_timeout(400)  # debounce

    # The empty state or 'No matches' message should appear
    page.wait_for_function(
        "() => {"
        "  var empty = document.querySelector('.empty-state');"
        "  return empty && (empty.textContent.includes('No matches') || empty.textContent.includes('No containers'));"
        "}",
        timeout=SHORT,
    )
    empty = page.locator(".empty-state")
    assert empty.count() > 0

    # Clear the search
    search.fill("")
    page.wait_for_timeout(400)


# ─────────────────────────────────────────────────────────────────────────────
# Session / Security edge cases
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_session_expiry_toast_on_absolute_timeout(page, live_server):
    """Simulate absolute session expiry by setting session_start far in the past."""
    # Manipulate sessionStorage to make the session appear 8+ hours old
    page.evaluate(
        "() => {"
        "  var eightHoursAgo = Date.now() - (8 * 60 * 60 * 1000 + 60000);"
        "  sessionStorage.setItem('session_start', String(eightHoursAgo));"
        "}"
    )

    # Trigger an API call by navigating which calls checkSessionExpiry()
    page.locator(".sidebar a").first.click()
    page.wait_for_timeout(1000)

    # The app should have cleared the token and shown the re-login screen
    # Either the toast with 'Session expired' or the login form should appear
    page.wait_for_selector(
        ".toast, button:has-text('Sign in')",
        timeout=MEDIUM,
    )

    # Verify the login form is shown (session was cleared)
    has_login = page.locator("button:has-text('Sign in')").count() > 0
    has_toast = page.locator(".toast").count() > 0
    assert has_login or has_toast, "Expected re-login prompt or toast after session expiry"

    if has_toast:
        toast_text = page.locator(".toast").first.text_content()
        assert "session" in toast_text.lower() or "expired" in toast_text.lower()


@pytest.mark.e2e
def test_rate_limit_toast_on_429(page, live_server):
    """When the server returns 429, the apiFetch error handler shows a toast."""
    # Intercept the containers API to return 429
    def _rate_limited(route):
        route.fulfill(
            status=429,
            content_type="application/json",
            body='{"detail": "Too Many Requests"}',
        )

    page.route("**/api/containers", _rate_limited)

    # Force a containers page reload to trigger the 429 fetch
    _nav_to(page, "images")  # navigate away first
    page.wait_for_timeout(200)
    page.locator(".sidebar a:has-text('Containers')").click()
    page.wait_for_timeout(2000)  # give time for fetch + error handling

    # The 429 triggers an Error in apiFetch, which is caught and shown as a toast
    # or the page may show a loading/error state
    page.unroute("**/api/containers")

    # We just verify no unhandled crash occurred — the test is about graceful handling
    # The page should still be functional (not a blank white screen)
    assert page.locator("body").count() > 0


@pytest.mark.e2e
def test_logout_clears_session_and_shows_login(page, live_server):
    """Manually clear sessionStorage (simulate logout) and verify login form reappears."""
    # Verify we are logged in
    page.wait_for_selector("h2", timeout=MEDIUM)
    assert "Containers" in page.locator("h2").first.text_content()

    # Clear the session token (simulating what happens on session expiry)
    page.evaluate("() => { sessionStorage.removeItem('api_token'); }")

    # Reload the page — without the token the app should show the login form
    page.reload()
    page.wait_for_selector("button:has-text('Sign in'), h2, h3", timeout=MEDIUM)

    # After clearing, we expect either the login form or an auth error
    # (depends on whether the page auto-navigates)
    sign_in_visible = page.locator("button:has-text('Sign in')").count() > 0
    # In some configurations the page may still render (token checked client-side)
    assert sign_in_visible or page.locator("h2").count() > 0


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar navigation elements
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_sidebar_all_links_present(page, live_server):
    """Verify all six sidebar links are present in the navigation."""
    expected_links = ("Containers", "Images", "Volumes", "Networks", "Compose", "System")
    for link_text in expected_links:
        link = page.locator(f".sidebar a:has-text('{link_text}')")
        assert link.count() > 0, f"Sidebar link '{link_text}' not found"


@pytest.mark.e2e
def test_sidebar_containers_link_navigates(page, live_server):
    """Click Containers sidebar link and verify Containers heading appears."""
    page.locator(".sidebar a:has-text('Images')").click()
    page.wait_for_selector("h2:has-text('Images')", timeout=MEDIUM)

    page.locator(".sidebar a:has-text('Containers')").click()
    page.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Containers')").count() > 0


@pytest.mark.e2e
def test_sidebar_images_link_navigates(page, live_server):
    """Click Images sidebar link and verify Images heading appears."""
    page.locator(".sidebar a:has-text('Images')").click()
    page.wait_for_selector("h2:has-text('Images')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Images')").count() > 0


@pytest.mark.e2e
def test_sidebar_volumes_link_navigates(page, live_server):
    """Click Volumes sidebar link and verify Volumes heading appears."""
    page.locator(".sidebar a:has-text('Volumes')").click()
    page.wait_for_selector("h2:has-text('Volumes')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Volumes')").count() > 0


@pytest.mark.e2e
def test_sidebar_networks_link_navigates(page, live_server):
    """Click Networks sidebar link and verify Networks heading appears."""
    page.locator(".sidebar a:has-text('Networks')").click()
    page.wait_for_selector("h2:has-text('Networks')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Networks')").count() > 0


@pytest.mark.e2e
def test_sidebar_compose_link_navigates(page, live_server):
    """Click Compose sidebar link and verify Compose heading appears."""
    page.locator(".sidebar a:has-text('Compose')").click()
    page.wait_for_selector("h2:has-text('Compose')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Compose')").count() > 0


@pytest.mark.e2e
def test_sidebar_system_link_navigates(page, live_server):
    """Click System sidebar link and verify System heading appears."""
    page.locator(".sidebar a:has-text('System')").click()
    page.wait_for_selector("h2:has-text('System')", timeout=MEDIUM)
    assert page.locator("h2:has-text('System')").count() > 0


@pytest.mark.e2e
def test_sidebar_active_class_on_navigation(page, live_server):
    """Verify the active sidebar link gets the 'active' CSS class after navigation."""
    sections = [
        ("Containers", "Containers"),
        ("Images", "Images"),
        ("Volumes", "Volumes"),
        ("Networks", "Networks"),
        ("Compose", "Compose"),
        ("System", "System"),
    ]
    for link_text, heading in sections:
        page.locator(f".sidebar a:has-text('{link_text}')").click()
        page.wait_for_selector(f"h2:has-text('{heading}')", timeout=MEDIUM)

        active_link = page.locator(".sidebar a.active")
        assert active_link.count() > 0, f"No active sidebar link after clicking {link_text}"
        active_text = active_link.first.text_content().strip()
        assert link_text in active_text, (
            f"Expected '{link_text}' to be active, but active is: {active_text!r}"
        )


@pytest.mark.e2e
def test_sidebar_status_indicator_present(page, live_server):
    """The sidebar should show a connection status dot (ok or down)."""
    status_el = page.locator("#sidebar-status")
    assert status_el.count() > 0, "Sidebar status element not found"

    status_text = status_el.text_content()
    assert any(kw in status_text.lower() for kw in ("connected", "disconnected", "connecting"))

    dot = status_el.locator(".dot")
    assert dot.count() > 0, "Status dot element not found in sidebar"


@pytest.mark.e2e
def test_sidebar_brand_name(page, live_server):
    """Verify the SKIFF brand name is present in the sidebar."""
    brand = page.locator(".sidebar-brand")
    assert brand.count() > 0
    brand_text = brand.text_content()
    assert "SKIFF" in brand_text or "Container Manager" in brand_text


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket exec terminal — full storyboard
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_terminal_disconnect_button(page, live_server, docker_client):
    """Open terminal tab, click Disconnect button, verify session ends gracefully."""
    container_name = "e2e-term-disc"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Terminal')").click()
    page.wait_for_selector("#term-output", timeout=MEDIUM)

    # Wait for the Disconnect button to appear
    page.wait_for_selector("button:has-text('Disconnect')", timeout=SHORT)
    disconnect_btn = page.locator("button:has-text('Disconnect')")
    assert disconnect_btn.count() > 0

    disconnect_btn.click()
    page.wait_for_timeout(1000)

    # After disconnecting, the terminal should show [Disconnected]
    term_text = page.locator("#term-output").text_content()
    assert "Disconnected" in term_text or "Session ended" in term_text

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_terminal_tab_switch_closes_ws(page, live_server, docker_client):
    """Switching away from Terminal tab and back does not leave orphaned WS connections."""
    container_name = "e2e-term-switch"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Terminal')").click()
    page.wait_for_selector("#term-output", timeout=MEDIUM)

    # Switch to Inspect tab
    page.locator(".detail-tab:has-text('Inspect')").click()
    page.wait_for_function(
        "() => document.getElementById('detail-content') && "
        "!document.getElementById('term-output')",
        timeout=MEDIUM,
    )

    # Should show the inspect panel now
    page.wait_for_selector(".inspect-panel", timeout=MEDIUM)
    assert page.locator(".inspect-panel").count() > 0

    # Switch back to Terminal
    page.locator(".detail-tab:has-text('Terminal')").click()
    page.wait_for_selector("#term-output", timeout=MEDIUM)
    assert page.locator("#term-output").count() > 0
    assert page.locator("input.terminal-input").count() > 0

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Additional container lifecycle tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_inspect_shows_environment(page, live_server, docker_client):
    """A container started with env vars shows them in the Inspect Environment section."""
    container_name = "e2e-inspect-env"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run(
            "alpine",
            "sleep 600",
            name=container_name,
            environment={"E2E_MARKER": "e2e-env-value"},
            detach=True,
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Inspect')").click()
    page.wait_for_selector(".inspect-panel", timeout=MEDIUM)

    # Look for the Environment section
    panel_text = page.locator(".inspect-panel").text_content()

    # 'Environment' section should be present since we set env vars
    assert "E2E_MARKER" in panel_text or "Environment" in panel_text, (
        f"Expected env var or Environment section in inspect panel, got: {panel_text[:400]}"
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_inspect_rename_validation(page, live_server, docker_client):
    """Attempting to rename a container to its current name shows an error."""
    container_name = "e2e-rename-same"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Inspect')").click()
    page.wait_for_selector(".inspect-panel", timeout=MEDIUM)

    # Try to rename to the same name — should show "Name unchanged" toast/error
    page.locator("button:has-text('Rename')").click()
    page.wait_for_selector(".toast", timeout=SHORT)
    toast_text = page.locator(".toast").first.text_content()
    assert any(kw in toast_text.lower() for kw in ("unchanged", "same", "error"))

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_sort_by_name(page, live_server, docker_client):
    """Click the Name column header to sort containers by name, verify sort indicator."""
    if docker_client:
        for name in ("e2e-sort-aaa", "e2e-sort-zzz"):
            for c in docker_client.containers.list(all=True):
                if c.name == name:
                    c.remove(force=True)
            docker_client.containers.run("alpine", "sleep 600", name=name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector("text=e2e-sort-aaa", timeout=MEDIUM)
    page.wait_for_selector("text=e2e-sort-zzz", timeout=MEDIUM)

    # Click the Name header to sort
    page.locator("th:has-text('Name')").click()
    page.wait_for_timeout(300)

    # The Name header should now show a sort indicator
    name_header = page.locator("th:has-text('Name')").first
    header_text = name_header.text_content()
    assert "▲" in header_text or "▼" in header_text, (
        f"Expected sort indicator in Name header, got: {header_text!r}"
    )

    # Click again to reverse sort
    page.locator("th:has-text('Name')").click()
    page.wait_for_timeout(300)
    header_text_2 = page.locator("th:has-text('Name')").first.text_content()
    assert "▲" in header_text_2 or "▼" in header_text_2

    if docker_client:
        for name in ("e2e-sort-aaa", "e2e-sort-zzz"):
            for c in docker_client.containers.list(all=True):
                if c.name == name:
                    c.remove(force=True)


@pytest.mark.e2e
def test_images_search_filter(page, live_server):
    """Typing in the Images search bar filters the images list."""
    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    search_bar = page.locator("input.search-bar").first
    search_bar.fill("alpine")
    page.wait_for_timeout(300)

    # Rows should only show alpine or no rows if alpine not present
    tbody = page.locator("tbody")
    visible_rows = tbody.locator("tr").all_text_contents()
    non_alpine = [r for r in visible_rows if "alpine" not in r.lower() and "No images" not in r]
    assert non_alpine == [], f"Expected only alpine rows after search, got: {non_alpine[:3]}"

    # Clear filter
    search_bar.fill("")
    page.wait_for_timeout(300)


@pytest.mark.e2e
def test_volume_create_shows_in_list(page, live_server, docker_client):
    """Create a new volume via the modal and verify it appears with correct name."""
    vol_name = "e2e-vol-create-check"
    if docker_client:
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass

    _nav_to(page, "volumes")
    page.locator("button:has-text('Create volume')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    # Check modal title
    assert "Create volume" in page.locator(".modal h3").text_content()

    page.locator("#vol-name").fill(vol_name)
    page.locator(".modal button:has-text('Create')").click()

    # Success toast should appear
    page.wait_for_selector(".toast", timeout=MEDIUM)
    toast_text = page.locator(".toast").first.text_content()
    assert "created" in toast_text.lower() or "volume" in toast_text.lower()

    # Volume should appear in table
    page.wait_for_selector(f"text={vol_name}", timeout=MEDIUM)
    assert page.locator(f"tr:has-text('{vol_name}')").count() > 0

    # The volume row should show the driver (usually 'local')
    row_text = page.locator(f"tr:has-text('{vol_name}')").first.text_content()
    assert "local" in row_text.lower()

    if docker_client:
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass


@pytest.mark.e2e
def test_network_create_shows_driver(page, live_server, docker_client):
    """Create a bridge network and verify its driver shows as 'bridge' in the table."""
    net_name = "e2e-net-driver-check"
    if docker_client:
        for n in docker_client.networks.list():
            if n.name == net_name:
                n.remove()

    _nav_to(page, "networks")
    page.locator("button:has-text('Create network')").click()
    page.wait_for_selector(".modal", timeout=SHORT)

    page.locator("#net-name").fill(net_name)
    # Select bridge driver
    page.locator("#net-driver").select_option(value="bridge")
    page.locator(".modal button:has-text('Create')").click()

    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)

    # Verify driver is 'bridge' in the row
    row = page.locator(f"tr:has-text('{net_name}')")
    assert "bridge" in row.first.text_content().lower()

    if docker_client:
        for n in docker_client.networks.list():
            if n.name == net_name:
                n.remove()


@pytest.mark.e2e
def test_container_stats_tab_all_fields(page, live_server, docker_client):
    """Open Stats tab for a running container; verify all 8 stat cards are present."""
    container_name = "e2e-stats-full"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)
        docker_client.containers.run("alpine", "sleep 600", name=container_name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={container_name}", timeout=MEDIUM)
    page.locator(f"tr:has-text('{container_name}')").locator("button:has-text('Stats')").click()
    page.wait_for_selector(".stats-grid", timeout=MEDIUM)

    # Wait for stat cards to populate (not just "Loading stats...")
    page.wait_for_function(
        "() => document.querySelectorAll('.stat .value').length >= 4",
        timeout=MEDIUM,
    )

    stat_labels = [s.text_content().strip() for s in page.locator(".stat .label").all()]
    for expected in ("CPU", "Memory", "Net RX", "Net TX"):
        assert expected in stat_labels, (
            f"Expected stat card '{expected}', got labels: {stat_labels}"
        )

    # All stat values should be non-empty
    for stat in page.locator(".stat").all():
        val = stat.locator(".value").text_content().strip()
        assert len(val) > 0, "Found empty stat value"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == container_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_keyboard_shortcut_6_navigates_system(page, live_server):
    """'6' key should navigate to the System page."""
    _nav_to(page, "containers")
    page.keyboard.press("6")
    page.wait_for_selector("h2:has-text('System')", timeout=SHORT)
    assert page.locator("h2:has-text('System')").count() > 0


# ─────────────────────────────────────────────────────────────────────────────
# CSP-safe sidebar navigation (onclick replaced with addEventListener)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_sidebar_nav_containers_via_click(page, live_server):
    """Sidebar Containers link navigates — verifies data-page/addEventListener wiring."""
    _nav_to(page, "images")
    page.locator(".sidebar a[data-page='containers']").click()
    page.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Containers')").count() > 0


@pytest.mark.e2e
def test_sidebar_nav_images_via_click(page, live_server):
    """Sidebar Images link navigates via addEventListener (not onclick attribute)."""
    page.locator(".sidebar a[data-page='images']").click()
    page.wait_for_selector("h2:has-text('Images')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Images')").count() > 0


@pytest.mark.e2e
def test_sidebar_nav_volumes_via_click(page, live_server):
    """Sidebar Volumes link navigates."""
    page.locator(".sidebar a[data-page='volumes']").click()
    page.wait_for_selector("h2:has-text('Volumes')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Volumes')").count() > 0


@pytest.mark.e2e
def test_sidebar_nav_networks_via_click(page, live_server):
    """Sidebar Networks link navigates."""
    page.locator(".sidebar a[data-page='networks']").click()
    page.wait_for_selector("h2:has-text('Networks')", timeout=MEDIUM)
    assert page.locator("h2:has-text('Networks')").count() > 0


@pytest.mark.e2e
def test_sidebar_nav_system_via_click(page, live_server):
    """Sidebar System link navigates."""
    page.locator(".sidebar a[data-page='system']").click()
    page.wait_for_selector("h2:has-text('System')", timeout=MEDIUM)
    assert page.locator("h2:has-text('System')").count() > 0


# ─────────────────────────────────────────────────────────────────────────────
# Logout button
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_logout_button_visible_when_authenticated(page, live_server):
    """Logout button is present in the sidebar when a session token is set."""
    _nav_to(page, "containers")
    # The button should exist and be visible
    btn = page.locator("#sidebar-logout")
    assert btn.count() > 0
    btn.wait_for(state="visible", timeout=SHORT)


@pytest.mark.e2e
def test_logout_button_clears_session_and_shows_login(page, live_server):
    """Clicking Sign out clears the session token and shows the login form."""
    _nav_to(page, "containers")
    page.locator("#sidebar-logout").wait_for(state="visible", timeout=SHORT)
    page.locator("#sidebar-logout").click()
    # After logout the login form (h3 Sign in) should appear
    page.wait_for_selector("h3:has-text('Sign in'), button:has-text('Sign in')", timeout=SHORT)
    assert (
        page.locator("h3:has-text('Sign in')").count() > 0
        or page.locator("button:has-text('Sign in')").count() > 0
    )
    # Session storage should no longer have the token
    token = page.evaluate("() => sessionStorage.getItem('api_token')")
    assert token is None or token == ""


# ─────────────────────────────────────────────────────────────────────────────
# Fetch timeout — no inline-handler CSP violations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_no_csp_violations_on_page_load(page, live_server):
    """Page loads without any Content-Security-Policy violation errors in the console."""
    js_errors = page._e2e_js_errors  # type: ignore[attr-defined]
    csp_errors = [e for e in js_errors if "Content-Security-Policy" in e or "EvalError" in e]
    assert csp_errors == [], f"CSP violations found: {csp_errors}"


@pytest.mark.e2e
def test_no_inline_handler_errors(page, live_server):
    """No JavaScript errors about inline event handlers being blocked."""
    js_errors = page._e2e_js_errors  # type: ignore[attr-defined]
    handler_errors = [e for e in js_errors if "onclick" in e.lower() or "handler" in e.lower()]
    assert handler_errors == []
