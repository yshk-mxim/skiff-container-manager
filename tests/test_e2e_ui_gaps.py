# SPDX-License-Identifier: MIT
"""Gap-filling e2e tests: UI interactions not covered by test_e2e_ui.py.

File is named test_e2e_ui_gaps.py (alphabetically after test_e2e_ui.py) so
pytest collects it in the same session but runs it after the main UI suite.
The tunnel-kill test is placed last to minimise blast radius if restoration
fails.
"""
from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import glob as _glob
import os
import subprocess
import time

import pytest
import requests

from tests.conftest_e2e import (
    _SOCKET_PATH,
    BASE_URL,
    E2E_SSH_TUNNEL,
    E2E_TOKEN,
    _docker_socket_alive,
)

pytestmark = pytest.mark.e2e

SHORT = 10_000
MEDIUM = 30_000
LONG = 90_000


def _nav_to(page, section: str) -> None:
    page.locator(f".sidebar a:has-text('{section.capitalize()}')").click()
    page.wait_for_selector(f"h2:has-text('{section.capitalize()}')", timeout=MEDIUM)


# ─────────────────────────────────────────────────────────────────────────────
# Containers — uncovered actions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_restart(page, live_server, docker_client):
    """Restart button on a running container keeps it running afterwards."""
    name = "e2e-restart-ctr"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)
        docker_client.containers.run("alpine:latest", "sleep 600", name=name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={name}", timeout=MEDIUM)
    row = page.locator(f"tr:has-text('{name}')")
    row.locator("button:has-text('Restart')").click()

    # Wait for the restart to complete and the container to be running again
    page.wait_for_selector(
        f"tr:has-text('{name}') .status.running", timeout=LONG
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_start_exits_immediately(page, live_server, docker_client):
    """Start a container whose command exits at once shows 'exited immediately' toast."""
    name = "e2e-exits-ctr"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)
        # Create (not run) so the container starts in 'created' state — Start button appears
        docker_client.containers.create("alpine:latest", command="/bin/false", name=name)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={name}", timeout=MEDIUM)
    row = page.locator(f"tr:has-text('{name}')")
    row.locator("button:has-text('Start')").click()

    # The app waits 600 ms, inspects the container, and shows an error toast
    page.wait_for_selector(".toast.error", timeout=LONG)
    toast_text = page.locator(".toast.error").first.text_content()
    assert "exited immediately" in toast_text, f"Unexpected toast: {toast_text!r}"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_delete_confirm_cancel(page, live_server, docker_client):
    """Cancelling the delete-container dialog leaves the container in the list."""
    name = "e2e-del-cancel-ctr"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)
        docker_client.containers.run("alpine:latest", "sleep 600", name=name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={name}", timeout=MEDIUM)

    # Stub window.confirm to always cancel
    page.evaluate("window.confirm = () => false")
    page.locator(f"tr:has-text('{name}') button:has-text('Delete')").click()

    page.wait_for_timeout(600)
    assert page.locator(f"tr:has-text('{name}')").count() > 0, \
        "Container row disappeared after cancelled delete"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_kill_confirm_cancel(page, live_server, docker_client):
    """Cancelling the kill-container dialog leaves the container running."""
    name = "e2e-kill-cancel-ctr"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)
        docker_client.containers.run("alpine:latest", "sleep 600", name=name, detach=True)

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={name}", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator(f"tr:has-text('{name}') button:has-text('Kill')").click()

    page.wait_for_timeout(600)
    assert page.locator(f"tr:has-text('{name}') .status.running").count() > 0, \
        "Container no longer running after cancelled kill"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Images — uncovered paths
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_image_pull_empty_name_error(page, live_server):
    """Submitting pull modal with empty name shows 'Image name is required' error toast."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    # Leave the input empty and click Pull
    page.locator(".modal-bg button:has-text('Pull')").click()

    page.wait_for_selector(".toast.error", timeout=SHORT)
    toast_text = page.locator(".toast.error").first.text_content()
    assert "required" in toast_text.lower(), f"Expected 'required' in toast, got: {toast_text!r}"

    # Modal must remain open
    assert page.locator(".modal-bg").count() > 0, "Pull modal closed unexpectedly"
    page.locator(".modal-bg button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_image_push_confirm_cancel(page, live_server, docker_client):
    """Cancelling the push confirm dialog does not trigger a push request."""
    _nav_to(page, "images")
    page.wait_for_selector("table", timeout=MEDIUM)

    inspect_btn = page.locator("button:has-text('Inspect')").first
    if inspect_btn.count() == 0:
        pytest.skip("No images available to open inspect modal")
    inspect_btn.click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    # Stub confirm to return false (cancel)
    page.evaluate("window.confirm = () => false")

    push_btn = page.locator(".modal-bg button[class*='btn']:has-text('Push')").first
    if push_btn.count() == 0:
        page.keyboard.press("Escape")
        pytest.skip("No Push button found in inspect modal")

    push_btn.click()
    page.wait_for_timeout(500)

    # No success toast should appear
    assert page.locator(".toast.success:has-text('Pushed')").count() == 0, \
        "Push ran despite confirm being cancelled"


@pytest.mark.e2e
def test_modal_cancel_pull(page, live_server):
    """Cancel button on pull modal closes it without pulling an image."""
    _nav_to(page, "images")
    page.locator("button:has-text('Pull image')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    page.locator(".modal-bg button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_modal_cancel_create_volume(page, live_server):
    """Cancel button on create-volume modal closes it without creating a volume."""
    _nav_to(page, "volumes")
    page.locator("button:has-text('Create volume')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    page.locator(".modal-bg button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


@pytest.mark.e2e
def test_modal_cancel_create_network(page, live_server):
    """Cancel button on create-network modal closes it without creating a network."""
    _nav_to(page, "networks")
    page.locator("button:has-text('Create network')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    page.locator(".modal-bg button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


# ─────────────────────────────────────────────────────────────────────────────
# Volumes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_volume_delete_confirm_cancel(page, live_server, docker_client):
    """Cancelling volume delete confirmation leaves the volume in the list."""
    vol_name = "e2e-del-cancel-vol"
    if docker_client:
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass
        docker_client.volumes.create(vol_name)

    _nav_to(page, "volumes")
    page.wait_for_selector(f"text={vol_name}", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator(f"tr:has-text('{vol_name}') button:has-text('Delete')").click()

    page.wait_for_timeout(600)
    assert page.locator(f"tr:has-text('{vol_name}')").count() > 0, \
        "Volume disappeared after cancelled delete"

    if docker_client:
        try:
            docker_client.volumes.get(vol_name).remove(force=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Compose — error and output paths
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_compose_deploy_invalid_yaml_shows_error(page, live_server):
    """Uploading an invalid compose file renders a red error output div."""
    _nav_to(page, "compose")

    page.locator("input[type='file']").set_input_files([{
        "name": "docker-compose.yml",
        "mimeType": "application/x-yaml",
        "buffer": b"this is: not: valid: compose: {{{",
    }])

    # The UI first renders a colourless "Deploying stack…" placeholder then replaces
    # it with the final (coloured) result div.  Wait until style.color is non-empty.
    page.wait_for_function(
        "() => { var el = document.querySelector('#compose-output .log-viewer'); "
        "return el && el.style.color !== ''; }",
        timeout=MEDIUM,
    )
    color = page.locator("#compose-output .log-viewer").evaluate("el => el.style.color")
    text = page.locator("#compose-output .log-viewer").text_content()
    # Red: rgb(248, 81, 73) or the hex equivalent
    assert "248" in color or "f85149" in color.replace("#", "").lower(), \
        f"Expected red error color, got: {color!r} (output: {text!r})"


@pytest.mark.e2e
def test_compose_output_shown_on_success(page, live_server):
    """Deploying a valid minimal compose file renders a green output div."""
    # Pre-clean any leftover dev stack (best-effort; ignore if nothing to clean)
    try:
        requests.post(
            f"{BASE_URL}/api/compose/down?project_name=dev",
            headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass

    _nav_to(page, "compose")

    yaml = b"services:\n  web:\n    image: alpine:latest\n    command: sleep 30\n"
    page.locator("input[type='file']").set_input_files([{
        "name": "docker-compose.yml",
        "mimeType": "application/x-yaml",
        "buffer": yaml,
    }])

    # Wait until style.color is non-empty (interim "Deploying…" placeholder has no colour)
    page.wait_for_function(
        "() => { var el = document.querySelector('#compose-output .log-viewer'); "
        "return el && el.style.color !== ''; }",
        timeout=LONG,
    )
    color = page.locator("#compose-output .log-viewer").evaluate("el => el.style.color")
    text = page.locator("#compose-output .log-viewer").text_content()
    # Green: rgb(63, 185, 80) or hex #3fb950
    assert "63" in color or "3fb950" in color.replace("#", "").lower(), \
        f"Expected green success color, got: {color!r} (output: {text!r})"

    # Tear down the stack after the test (best-effort)
    try:
        requests.post(
            f"{BASE_URL}/api/compose/down?project_name=dev",
            headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


@pytest.mark.e2e
def test_compose_project_name_field(page, live_server):
    """Changing the project name input sends that name in the deploy API request."""
    # Pre-clean (best-effort)
    try:
        requests.post(
            f"{BASE_URL}/api/compose/down?project_name=e2e-test-proj",
            headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass

    _nav_to(page, "compose")
    page.wait_for_selector("#compose-project", timeout=MEDIUM)
    assert page.locator("#compose-project").input_value() == "dev", \
        "Default project name should be 'dev'"

    page.locator("#compose-project").fill("e2e-test-proj")

    project_urls: list[str] = []

    def _capture(req):
        if "/compose/up" in req.url:
            project_urls.append(req.url)

    page.on("request", _capture)

    yaml = b"services:\n  app:\n    image: alpine:latest\n    command: sleep 10\n"
    page.locator("input[type='file']").set_input_files([{
        "name": "docker-compose.yml",
        "mimeType": "application/x-yaml",
        "buffer": yaml,
    }])

    page.wait_for_selector("#compose-output .log-viewer", timeout=LONG)

    assert any("e2e-test-proj" in u for u in project_urls), \
        f"Project name not found in captured requests: {project_urls}"

    # Tear down (best-effort)
    try:
        requests.post(
            f"{BASE_URL}/api/compose/down?project_name=e2e-test-proj",
            headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Networks — connect form submit
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_network_connect_form_submit(page, live_server, docker_client):
    """Network Connect modal: select a running container and submit → success toast."""
    net_name = "e2e-conn-submit-net"
    ctr_name = "e2e-conn-submit-ctr"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == ctr_name:
                c.remove(force=True)
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass
        docker_client.networks.create(net_name, driver="bridge")
        docker_client.containers.run("alpine:latest", "sleep 600", name=ctr_name, detach=True)

    _nav_to(page, "networks")
    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)

    page.locator(f"tr:has-text('{net_name}') button:has-text('Connect...')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    # <option> elements inside a <select> are hidden by default; use state="attached"
    page.wait_for_selector(
        f"#net-connect-container option:has-text('{ctr_name}')",
        timeout=MEDIUM,
        state="attached",
    )
    page.locator("#net-connect-container").select_option(label=f"{ctr_name} (running)")

    page.locator(".modal-bg button:has-text('Connect')").click()

    page.wait_for_selector(".toast:has-text('connected')", timeout=MEDIUM)

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == ctr_name:
                c.remove(force=True)
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Containers — sort direction toggle
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_sort_direction_toggle(page, live_server, docker_client):
    """Clicking the Name column header twice reverses the sort order."""
    names = ["e2e-sort-aaa", "e2e-sort-zzz"]
    if docker_client:
        for n in names:
            for c in docker_client.containers.list(all=True):
                if c.name == n:
                    c.remove(force=True)
        for n in names:
            docker_client.containers.run("alpine:latest", "sleep 600", name=n, detach=True)

    _nav_to(page, "containers")
    for n in names:
        page.wait_for_selector(f"text={n}", timeout=MEDIUM)

    # First click: record whatever order results (initial direction is unknown)
    page.locator("th:has-text('Name')").click()
    page.wait_for_timeout(300)
    rows1 = page.locator("tbody tr").all_text_contents()
    aaa1 = next((i for i, r in enumerate(rows1) if "e2e-sort-aaa" in r), -1)
    zzz1 = next((i for i, r in enumerate(rows1) if "e2e-sort-zzz" in r), -1)
    assert aaa1 >= 0 and zzz1 >= 0, "Both e2e-sort containers must be visible after first click"
    aaa_before_zzz_first = aaa1 < zzz1

    # Second click: order must be the exact opposite of after the first click
    page.locator("th:has-text('Name')").click()
    page.wait_for_timeout(300)
    rows2 = page.locator("tbody tr").all_text_contents()
    aaa2 = next((i for i, r in enumerate(rows2) if "e2e-sort-aaa" in r), -1)
    zzz2 = next((i for i, r in enumerate(rows2) if "e2e-sort-zzz" in r), -1)
    assert aaa2 >= 0 and zzz2 >= 0, "Both e2e-sort containers must be visible after second click"
    aaa_before_zzz_second = aaa2 < zzz2

    assert aaa_before_zzz_first != aaa_before_zzz_second, (
        f"Sort did not reverse: after 1st click aaa={aaa1} zzz={zzz1}; "
        f"after 2nd click aaa={aaa2} zzz={zzz2}"
    )

    if docker_client:
        for n in names:
            for c in docker_client.containers.list(all=True):
                if c.name == n:
                    c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Run modal — registry hint and image chips
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_run_modal_registry_hint_loads(page, live_server):
    """Run modal fetches /config and replaces 'Loading registry…' with real hint text."""
    _nav_to(page, "containers")
    page.locator("button:has-text('Run new container')").click()
    page.wait_for_selector(".modal-bg", timeout=SHORT)

    # Wait for the hint to stop saying "Loading…"
    hint = page.locator("#run-registry-hint")
    page.wait_for_function(
        "() => !document.getElementById('run-registry-hint')?.textContent.includes('Loading')",
        timeout=SHORT,
    )
    hint_text = hint.text_content()
    # Must mention either a registry name or "No registry restriction"
    assert hint_text and "Loading" not in hint_text, \
        f"Registry hint still loading: {hint_text!r}"

    page.locator(".modal-bg button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


# ─────────────────────────────────────────────────────────────────────────────
# Networks — built-in networks have no Connect or Delete buttons
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_builtin_network_no_action_buttons(page, live_server):
    """Built-in networks (bridge, host, none) expose no Connect or Delete buttons."""
    _nav_to(page, "networks")

    for builtin_name in ("bridge", "host", "none"):
        row = page.locator(f"tr:has-text('{builtin_name}')").first
        if row.count() == 0:
            continue
        assert row.locator("button:has-text('Connect...')").count() == 0, \
            f"'{builtin_name}' should not have Connect button"
        assert row.locator("button:has-text('Delete')").count() == 0, \
            f"'{builtin_name}' should not have Delete button"


# ─────────────────────────────────────────────────────────────────────────────
# System — prune confirm cancel
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_system_prune_confirm_cancel(page, live_server):
    """Cancelling the system-prune confirmation dialog makes no API call."""
    _nav_to(page, "system")
    page.wait_for_selector("button:has-text('Prune system')", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator("button:has-text('Prune system')").click()

    page.wait_for_timeout(600)
    # No success or info toast should fire
    assert page.locator(".toast:has-text('Pruned')").count() == 0, \
        "Prune toast appeared after cancelled confirmation"
    assert page.locator(".toast:has-text('Nothing')").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Volume prune confirm cancel
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_volume_prune_confirm_cancel(page, live_server):
    """Cancelling the volume-prune confirmation makes no API call."""
    _nav_to(page, "volumes")
    page.wait_for_selector("button:has-text('Prune unused')", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator("button:has-text('Prune unused')").click()

    page.wait_for_timeout(600)
    assert page.locator(".toast:has-text('Pruned')").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Network prune confirm cancel
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_network_prune_confirm_cancel(page, live_server):
    """Cancelling the network-prune confirmation makes no API call."""
    _nav_to(page, "networks")
    page.wait_for_selector("button:has-text('Prune unused')", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator("button:has-text('Prune unused')").click()

    page.wait_for_timeout(600)
    assert page.locator(".toast:has-text('Pruned')").count() == 0
    assert page.locator(".toast:has-text('No unused')").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Network delete confirm cancel
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_network_delete_confirm_cancel(page, live_server, docker_client):
    """Cancelling the network delete confirmation leaves the network in the list."""
    net_name = "e2e-del-cancel-net"
    if docker_client:
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass
        docker_client.networks.create(net_name, driver="bridge")

    _nav_to(page, "networks")
    page.wait_for_selector(f"text={net_name}", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator(f"tr:has-text('{net_name}') button:has-text('Delete')").click()

    page.wait_for_timeout(600)
    assert page.locator(f"tr:has-text('{net_name}')").count() > 0, \
        "Network disappeared after cancelled delete"

    if docker_client:
        for n in docker_client.networks.list():
            if n.name == net_name:
                try:
                    n.remove()
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Container health badge renders when container reports health status
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_container_health_badge(page, live_server, docker_client):
    """Container with a HEALTHCHECK shows a health badge in the status column."""
    name = "e2e-health-badge-ctr"
    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)
        # Use a healthcheck image (nginx has one built in, or we can specify)
        docker_client.containers.run(
            "alpine:latest",
            "sleep 600",
            name=name,
            detach=True,
            healthcheck={
                "test": ["CMD", "true"],
                "interval": 1_000_000_000,  # 1 second in nanoseconds
                "timeout":  3_000_000_000,
                "retries": 1,
            },
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={name}", timeout=MEDIUM)

    # Wait up to MEDIUM for the health badge to appear (healthcheck runs after ~1s)
    page.wait_for_selector(
        f"tr:has-text('{name}') .health-badge", timeout=MEDIUM
    )
    badge_text = page.locator(f"tr:has-text('{name}') .health-badge").first.text_content()
    assert badge_text in ("healthy", "unhealthy", "starting"), \
        f"Unexpected health badge text: {badge_text!r}"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Engine unreachable — tunnel builder form (LAST test: kills the SSH tunnel)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_engine_unreachable_shows_tunnel_instructions_impl(browser, live_server):
    """Kill SSH tunnel → containers page shows unreachable state + tunnel builder form.

    Placed last to minimise risk: if tunnel restoration fails, no subsequent
    tests depend on Docker in this session.
    """
    if not E2E_SSH_TUNNEL or not _SOCKET_PATH:
        pytest.skip("Requires E2E_SSH_TUNNEL and a unix:// DOCKER_HOST")

    ctl_socks = _glob.glob("/tmp/skiff-e2e-ssh-ctl-*.sock")
    if not ctl_socks:
        pytest.skip("SSH tunnel control socket not found — tunnel may not be active")
    ctl_sock = sorted(ctl_socks)[0]

    new_ctl = f"/tmp/skiff-e2e-ssh-ctl-{os.getpid()}-restored.sock"

    try:
        # ── Kill the tunnel ──────────────────────────────────────────────────
        subprocess.run(
            ["ssh", "-S", ctl_sock, "-O", "exit", E2E_SSH_TUNNEL],
            capture_output=True, check=False, timeout=5,
        )
        try:
            os.unlink(_SOCKET_PATH)  # remove dead socket so new tunnel can rebind
        except OSError:
            pass

        # ── Verify unreachable state in a fresh browser context ──────────────
        context = browser.new_context()
        pg = context.new_page()
        pg.set_default_navigation_timeout(5_000)
        pg.set_default_timeout(10_000)
        try:
            pg.goto(live_server, wait_until="domcontentloaded")
            pg.wait_for_selector("button:has-text('Sign in'), .sidebar", timeout=10_000)
            sign_in = pg.locator("button:has-text('Sign in')")
            if sign_in.count() > 0:
                pg.locator("input[type='password']").fill(E2E_TOKEN)
                sign_in.click()
                pg.wait_for_selector(".sidebar", timeout=10_000)

            # Navigate to containers — Docker is dead, the error form must appear
            pg.locator(".sidebar a:has-text('Containers')").click()
            pg.wait_for_selector("#tunnel-user", timeout=MEDIUM)

            assert pg.locator("#tunnel-host").count() > 0, "#tunnel-host input not rendered"
            assert pg.locator("#tunnel-cmd").count() > 0, "#tunnel-cmd pre not rendered"

            # Typing user/host must update the tunnel command
            pg.locator("#tunnel-user").fill("myuser")
            pg.locator("#tunnel-host").fill("myhost.local")
            pg.wait_for_timeout(200)
            cmd_text = pg.locator("#tunnel-cmd").text_content()
            assert "myuser" in cmd_text, f"user not in cmd: {cmd_text!r}"
            assert "myhost.local" in cmd_text, f"host not in cmd: {cmd_text!r}"
        finally:
            context.close()

    finally:
        # ── Restore the SSH tunnel ───────────────────────────────────────────
        restore = subprocess.run(
            [
                "ssh", "-fNM",
                "-S", new_ctl,
                "-o", "ControlPersist=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=6",
                "-L", f"{_SOCKET_PATH}:/var/run/docker.sock",
                E2E_SSH_TUNNEL,
            ],
            capture_output=True,
            timeout=25,
            check=False,
        )
        if restore.returncode == 0:
            deadline = time.time() + 15
            while time.time() < deadline:
                if _docker_socket_alive(_SOCKET_PATH):
                    break
                time.sleep(0.5)
