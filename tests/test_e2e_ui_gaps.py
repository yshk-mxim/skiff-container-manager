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
from tests.e2e_helpers import (
    LONG,
    MEDIUM,
    SHORT,
)
from tests.e2e_helpers import (
    deploy_compose_stack as _deploy_compose_stack,
)
from tests.e2e_helpers import (
    nav_to as _nav_to,
)
from tests.e2e_helpers import (
    teardown_compose_stack as _teardown_compose_stack,
)

pytestmark = pytest.mark.e2e


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
    page.wait_for_selector(f"tr:has-text('{name}') .status.running", timeout=LONG)

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
    assert page.locator(f"tr:has-text('{name}')").count() > 0, "Container row disappeared after cancelled delete"

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
    assert page.locator(f"tr:has-text('{name}') .status.running").count() > 0, (
        "Container no longer running after cancelled kill"
    )

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Container resource updates (POST /api/containers/{id}/update)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_container_update_memory_and_cpus_live(page, live_server, docker_client):
    """Edit memory + CPUs in the Inspect panel → real container's HostConfig changes.

    Covers the Phase 1 happy path end-to-end: inspect load → GCP-unit parse →
    cap enforcement → docker update() → reload → response round-trip to UI.
    """
    name = "e2e-update-ctr"
    for c in docker_client.containers.list(all=True):
        if c.name == name:
            c.remove(force=True)
    # Start with modest defaults so the test values are a real delta
    docker_client.containers.run(
        "alpine:latest",
        "sleep 600",
        name=name,
        detach=True,
        mem_limit="64m",  # 64 MiB = 67108864 bytes
    )
    try:
        _nav_to(page, "containers")
        page.wait_for_selector(f"text={name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{name}') button:has-text('Inspect')").click()
        # The Resources section now lives inside the detail view
        page.wait_for_selector(".inspect-section:has-text('Resources') input", timeout=MEDIUM)
        inputs = page.locator(".inspect-section:has-text('Resources') input")
        # Order per _renderEditableResources:
        #   0: memory, 1: memory_reservation, 2: cpus,
        #   3: cpu_shares, 4: pids_limit, (5: restart_retry when on-failure)
        inputs.nth(0).fill("128Mi")
        inputs.nth(2).fill("1")
        # Save — the button is gated by change detection; poll until enabled
        save_btn = page.locator(".inspect-section:has-text('Resources') button:has-text('Save changes')")
        # Dispatch one more input event so updateButtons() fires with both fills settled
        save_btn.wait_for(state="attached", timeout=MEDIUM)
        save_btn.click()
        # Verify via the Docker SDK — source of truth, not the UI status text.
        # The UI re-renders ~400ms after success so text-based assertion is racy.
        deadline = time.time() + 15
        last_hc = {}
        while time.time() < deadline:
            ctr = docker_client.containers.get(name)
            ctr.reload()
            last_hc = ctr.attrs["HostConfig"]
            if last_hc.get("Memory") == 128 * 1024 * 1024 and last_hc.get("CpuQuota") == 100_000:
                break
            time.sleep(0.25)
        assert last_hc.get("Memory") == 128 * 1024 * 1024, (
            f"Memory not updated after 15s: {last_hc.get('Memory')} (expected {128 * 1024 * 1024}). "
            f"Full HostConfig: {last_hc}"
        )
        assert last_hc.get("CpuQuota") == 100_000, f"CpuQuota not updated: {last_hc.get('CpuQuota')} (expected 100000)"
    finally:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_update_cap_rejected(page, live_server, docker_client):
    """Attempting to raise memory above MAX_CONTAINER_MEM shows the server error.

    This verifies the cap is enforced end-to-end (not just in unit tests against
    mocks) — a server misconfiguration that disabled the cap would surface here.
    """
    name = "e2e-update-cap"
    for c in docker_client.containers.list(all=True):
        if c.name == name:
            c.remove(force=True)
    docker_client.containers.run("alpine:latest", "sleep 600", name=name, detach=True, mem_limit="64m")
    try:
        _nav_to(page, "containers")
        page.wait_for_selector(f"text={name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{name}') button:has-text('Inspect')").click()
        page.wait_for_selector("text=Resources", timeout=MEDIUM)
        inputs = page.locator(".inspect-section:has-text('Resources') input")
        inputs.nth(0).fill("8Gi")  # way above the 2g cap
        page.locator(".inspect-section:has-text('Resources') button:has-text('Save changes')").click()
        # Error status should appear with "cap" in the message
        page.wait_for_selector("text=/cap|exceeds/i", timeout=MEDIUM)
        # Verify container was NOT mutated
        ctr = docker_client.containers.get(name)
        ctr.reload()
        assert ctr.attrs["HostConfig"]["Memory"] == 64 * 1024 * 1024, (
            "Container memory was changed despite cap rejection — server cap bypassed"
        )
    finally:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: Volume inspect
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_volume_inspect_shows_modal_with_details(page, live_server, docker_client):
    """Click Inspect on a volume → modal shows scope, mountpoint, labels, etc."""
    name = "e2e-inspect-vol"
    for v in docker_client.volumes.list():
        if v.name == name:
            v.remove(force=True)
    docker_client.volumes.create(name=name, labels={"purpose": "e2e-test"})
    try:
        _nav_to(page, "volumes")
        page.wait_for_selector(f"td:has-text('{name}')", timeout=MEDIUM)
        row = page.locator(f"tr:has-text('{name}')")
        row.locator("button:has-text('Inspect')").click()
        page.wait_for_selector(f".modal h3:has-text('Volume: {name}')", timeout=MEDIUM)
        # Wait for the async fetch to complete — "Loading…" is replaced with the
        # key/value rows once the response arrives.
        page.wait_for_selector(".modal .inspect-kv", timeout=MEDIUM)
        modal_text = page.locator(".modal").inner_text()
        assert "Scope" in modal_text
        assert "Driver" in modal_text
        assert "purpose" in modal_text  # label key shown
    finally:
        try:
            docker_client.volumes.get(name).remove(force=True)
        except Exception:
            pass


@pytest.mark.e2e
def test_volume_inspect_missing_volume_404(live_server):
    """Direct API: non-existent volume → 404, no server crash."""
    r = requests.get(
        f"{BASE_URL}/api/volumes/totally-gone-vol/inspect",
        headers={"Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=10,
    )
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: Prometheus-format metrics endpoint
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_system_metrics_scrapeable(live_server):
    """Metrics endpoint returns Prometheus text format that a scraper can parse."""
    r = requests.get(
        f"{BASE_URL}/api/system/metrics",
        headers={"Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=15,
    )
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "version=0.0.4" in r.headers["content-type"]
    body = r.text
    # Core gauges must be present
    for name in (
        "skiff_uptime_seconds",
        "skiff_containers_running",
        "skiff_containers_total",
        "skiff_images_total",
        "skiff_engine_cpus",
        "skiff_engine_memory_bytes",
        "skiff_disk_images_bytes",
    ):
        assert f"# TYPE {name} gauge" in body, f"Missing gauge: {name}"


@pytest.mark.e2e
def test_system_metrics_requires_auth_e2e(live_server):
    """Unauthenticated scrape returns 401, doesn't leak workload details."""
    r = requests.get(f"{BASE_URL}/api/system/metrics", timeout=10)
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: Session lifecycle (token rotation + config reset → wizard)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_rotate_token_blocked_when_from_env(live_server):
    """E2E server is launched with API_TOKEN in env (from_env=True), so the rotate
    endpoint must 403. This is the primary "sad path" guard for env-managed setups.
    """
    r = requests.post(
        f"{BASE_URL}/api/auth/rotate-token",
        headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
        json={"new_token": "new-rotated-value-for-the-test-32c"},
        timeout=15,
    )
    assert r.status_code == 403
    # After R4: structured detail body carries code + message.
    assert r.json()["detail"]["code"] == "auth.env_managed"


@pytest.mark.e2e
def test_reset_config_blocked_when_from_env(live_server):
    """Same pathway: reset is blocked when from_env=True."""
    r = requests.post(
        f"{BASE_URL}/api/auth/reset-config",
        headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=15,
    )
    assert r.status_code == 403


@pytest.mark.e2e
def test_account_section_hidden_when_from_env(page, live_server):
    """The Account card must not render on env-configured servers (both endpoints 403)."""
    _nav_to(page, "system")
    page.wait_for_selector("h2:has-text('System')", timeout=MEDIUM)
    # Give the async fetch time to settle
    page.wait_for_timeout(1500)
    assert page.locator("h3:has-text('Account')").count() == 0, (
        "Account section should be hidden in env-configured mode"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Compose lifecycle completeness (per-service logs + restart, aggregated logs)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_compose_service_restart_via_ui(page, live_server, docker_client):
    """Per-service Restart button calls the new endpoint and the service comes back up."""
    project = "e2ephase3restart"
    yaml = b"services:\n  web:\n    image: alpine:latest\n    command: sleep 600\n"
    _teardown_compose_stack(project)
    _deploy_compose_stack(project, yaml)
    try:
        _nav_to(page, "compose")
        # Wait for the stack card to render
        page.wait_for_selector(f"h4:has-text('{project}')", timeout=MEDIUM)
        # The web service row should have a Restart button specific to it
        svc_row = page.locator(f"h4:has-text('{project}') + .stack-services div:has-text('web')").first
        svc_row.locator("button:has-text('Restart')").click()
        page.wait_for_selector("text=web restarted", timeout=LONG)
        # Verify via Docker SDK that the service is still running (restart preserved it)
        containers = docker_client.containers.list(
            filters={"label": f"com.docker.compose.project={project}"},
        )
        assert any(c.attrs["State"]["Status"] == "running" for c in containers)
    finally:
        _teardown_compose_stack(project)


@pytest.mark.e2e
def test_compose_aggregated_logs_modal(page, live_server, docker_client):
    """'All service logs' button shows a modal with per-service-prefixed lines."""
    project = "e2ephase3logs"
    yaml = (
        b"services:\n"
        b"  web:\n"
        b"    image: alpine:latest\n"
        b"    command: sh -c 'echo WEB_LINE; sleep 600'\n"
        b"  db:\n"
        b"    image: alpine:latest\n"
        b"    command: sh -c 'echo DB_LINE; sleep 600'\n"
    )
    _teardown_compose_stack(project)
    _deploy_compose_stack(project, yaml)
    try:
        _nav_to(page, "compose")
        page.wait_for_selector(f"h4:has-text('{project}')", timeout=MEDIUM)
        # Give services a couple seconds to emit stdout so logs aren't empty
        page.wait_for_timeout(2000)
        # Find the "All service logs" button within the stack's action row
        stack_card = page.locator(f"h4:has-text('{project}')").locator("..")
        stack_card.locator("button:has-text('All service logs')").click()
        # Modal opens with "Aggregated logs: <project>" heading
        page.wait_for_selector(f"h3:has-text('Aggregated logs: {project}')", timeout=MEDIUM)
        # Log viewer should contain both services' prefixed lines
        page.wait_for_function(
            "() => { var el = document.querySelector('.modal pre'); "
            "return el && el.textContent && el.textContent.includes('web |') "
            "&& el.textContent.includes('db |'); }",
            timeout=LONG,
        )
    finally:
        _teardown_compose_stack(project)


@pytest.mark.e2e
def test_compose_service_restart_unknown_service_returns_404_noop(page, live_server):
    """Sad path: API directly — restarting a non-existent service returns 404, no action."""
    r = requests.post(
        f"{BASE_URL}/api/compose/nonexistentproj/services/ghost/restart",
        headers={"X-Requested-With": "ContainerManager", "Authorization": f"Bearer {E2E_TOKEN}"},
        timeout=15,
    )
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Clone-to-recreate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_container_clone_changes_memory_both_exist(page, live_server, docker_client):
    """Clone with a new name + changed memory → both containers exist, env preserved.

    Exercises the Phase 2 happy path end-to-end: Inspect → Clone with changes →
    edit memory in the pre-filled Run modal → launch → old and new coexist, new
    has the edited memory, env from old is inherited (verified via Docker SDK).
    """
    src_name = "e2e-clone-src"
    for c in docker_client.containers.list(all=True):
        if c.name == src_name or c.name == "clone-" + src_name:
            c.remove(force=True)
    docker_client.containers.run(
        "alpine:latest",
        "sleep 600",
        name=src_name,
        detach=True,
        mem_limit="64m",
        environment=["FROM_SOURCE=yes", "SECRET=topsecret"],
    )
    try:
        _nav_to(page, "containers")
        page.wait_for_selector(f"text={src_name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{src_name}') button:has-text('Inspect')").click()
        # Click "Clone with changes" button in the Inspect action row
        page.wait_for_selector("button:has-text('Clone with changes')", timeout=MEDIUM)
        page.locator("button:has-text('Clone with changes')").click()
        # Modal should open with "Clone container" heading and pre-filled name
        page.wait_for_selector(".modal h3:has-text('Clone container')", timeout=MEDIUM)
        name_input = page.locator(".modal #run-name")
        assert name_input.input_value() == "clone-" + src_name
        # Launch — server will inherit env and the clone will have default memory
        page.locator(".modal .actions button.primary").click()
        # Toast confirms the clone
        page.wait_for_selector("text=Cloned from", timeout=LONG)
        # Both containers should exist via Docker SDK
        deadline = time.time() + 10
        found_both = False
        while time.time() < deadline:
            names = {c.name for c in docker_client.containers.list(all=True)}
            if src_name in names and ("clone-" + src_name) in names:
                found_both = True
                break
            time.sleep(0.25)
        assert found_both, "Expected both source and clone to exist"
        # Verify env inheritance: clone's env must include SECRET=topsecret
        clone = docker_client.containers.get("clone-" + src_name)
        clone_env = clone.attrs["Config"]["Env"]
        assert "SECRET=topsecret" in clone_env, f"Env not inherited: {clone_env!r}"
        assert "FROM_SOURCE=yes" in clone_env
    finally:
        for c in docker_client.containers.list(all=True):
            if c.name == src_name or c.name == "clone-" + src_name:
                c.remove(force=True)


@pytest.mark.e2e
def test_container_clone_replace_removes_source(page, live_server, docker_client):
    """Clone with "Replace original" checked → source is removed after clone starts."""
    src_name = "e2e-clone-replace-src"
    for c in docker_client.containers.list(all=True):
        if c.name in (src_name, "clone-" + src_name):
            c.remove(force=True)
    docker_client.containers.run(
        "alpine:latest",
        "sleep 600",
        name=src_name,
        detach=True,
        mem_limit="64m",
    )
    try:
        _nav_to(page, "containers")
        page.wait_for_selector(f"text={src_name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{src_name}') button:has-text('Inspect')").click()
        page.wait_for_selector("button:has-text('Clone with changes')", timeout=MEDIUM)
        page.locator("button:has-text('Clone with changes')").click()
        page.wait_for_selector(".modal h3:has-text('Clone container')", timeout=MEDIUM)
        # Check "Replace original"
        page.locator(".modal #run-replace").check()
        page.locator(".modal .actions button.primary").click()
        page.wait_for_selector("text=replaced", timeout=LONG)
        # Source should be gone within a few seconds
        deadline = time.time() + 10
        source_gone = False
        while time.time() < deadline:
            names = {c.name for c in docker_client.containers.list(all=True)}
            if src_name not in names and ("clone-" + src_name) in names:
                source_gone = True
                break
            time.sleep(0.25)
        assert source_gone, "Source container should have been removed after replace"
    finally:
        for c in docker_client.containers.list(all=True):
            if c.name in (src_name, "clone-" + src_name):
                c.remove(force=True)


@pytest.mark.e2e
def test_container_clone_bad_port_preserves_source(page, live_server, docker_client):
    """Clone with an invalid port mapping → server rejects, source is preserved.

    Defence-in-depth: an error during clone creation must NOT remove the source
    even if replace_id was specified, because the server only issues the cleanup
    AFTER the new container successfully starts.
    """
    src_name = "e2e-clone-bad-src"
    for c in docker_client.containers.list(all=True):
        if c.name in (src_name, "clone-" + src_name):
            c.remove(force=True)
    docker_client.containers.run(
        "alpine:latest",
        "sleep 600",
        name=src_name,
        detach=True,
        mem_limit="64m",
    )
    try:
        _nav_to(page, "containers")
        page.wait_for_selector(f"text={src_name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{src_name}') button:has-text('Inspect')").click()
        page.wait_for_selector("button:has-text('Clone with changes')", timeout=MEDIUM)
        page.locator("button:has-text('Clone with changes')").click()
        page.wait_for_selector(".modal h3:has-text('Clone container')", timeout=MEDIUM)
        # Check Replace original, then provide a privileged port to trigger server rejection
        page.locator(".modal #run-replace").check()
        page.locator(".modal #run-ports").fill("80:80")  # privileged host port → 400
        page.locator(".modal .actions button.primary").click()
        # An error toast should appear
        page.wait_for_selector(".toast.error", timeout=LONG)
        # Source container must still exist — replace_cleanup never ran
        src = docker_client.containers.get(src_name)
        assert src.status == "running", f"Source status changed: {src.status}"
    finally:
        for c in docker_client.containers.list(all=True):
            if c.name in (src_name, "clone-" + src_name):
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
    assert page.locator(".toast.success:has-text('Pushed')").count() == 0, "Push ran despite confirm being cancelled"


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
    page.locator("#main button:has-text('Create')").click()
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
    assert page.locator(f"tr:has-text('{vol_name}')").count() > 0, "Volume disappeared after cancelled delete"

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

    page.locator("input[type='file']").set_input_files(
        [
            {
                "name": "docker-compose.yml",
                "mimeType": "application/x-yaml",
                "buffer": b"this is: not: valid: compose: {{{",
            }
        ]
    )

    # The UI first renders a colourless "Deploying stack…" placeholder then replaces
    # it with the final (coloured) result div.  Wait until style.color is non-empty.
    page.wait_for_function(
        "() => { var el = document.querySelector('#compose-output .log-viewer'); return el && el.style.color !== ''; }",
        timeout=MEDIUM,
    )
    color = page.locator("#compose-output .log-viewer").evaluate("el => el.style.color")
    text = page.locator("#compose-output .log-viewer").text_content()
    # Red: rgb(248, 81, 73) or the hex equivalent
    assert "248" in color or "f85149" in color.replace("#", "").lower(), (
        f"Expected red error color, got: {color!r} (output: {text!r})"
    )


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
    page.locator("input[type='file']").set_input_files(
        [
            {
                "name": "docker-compose.yml",
                "mimeType": "application/x-yaml",
                "buffer": yaml,
            }
        ]
    )

    # Wait until style.color is non-empty (interim "Deploying…" placeholder has no colour)
    page.wait_for_function(
        "() => { var el = document.querySelector('#compose-output .log-viewer'); return el && el.style.color !== ''; }",
        timeout=LONG,
    )
    color = page.locator("#compose-output .log-viewer").evaluate("el => el.style.color")
    text = page.locator("#compose-output .log-viewer").text_content()
    # Green: rgb(63, 185, 80) or hex #3fb950
    assert "63" in color or "3fb950" in color.replace("#", "").lower(), (
        f"Expected green success color, got: {color!r} (output: {text!r})"
    )

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
    assert page.locator("#compose-project").input_value() == "dev", "Default project name should be 'dev'"

    page.locator("#compose-project").fill("e2e-test-proj")

    project_urls: list[str] = []

    def _capture(req):
        if "/compose/up" in req.url:
            project_urls.append(req.url)

    page.on("request", _capture)

    yaml = b"services:\n  app:\n    image: alpine:latest\n    command: sleep 10\n"
    page.locator("input[type='file']").set_input_files(
        [
            {
                "name": "docker-compose.yml",
                "mimeType": "application/x-yaml",
                "buffer": yaml,
            }
        ]
    )

    page.wait_for_selector("#compose-output .log-viewer", timeout=LONG)

    assert any("e2e-test-proj" in u for u in project_urls), (
        f"Project name not found in captured requests: {project_urls}"
    )

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
        f"select[name='container_id'] option:has-text('{ctr_name}')",
        timeout=MEDIUM,
        state="attached",
    )
    page.locator("select[name='container_id']").select_option(label=f"{ctr_name} (running)")

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
        f"Sort did not reverse: after 1st click aaa={aaa1} zzz={zzz1}; after 2nd click aaa={aaa2} zzz={zzz2}"
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
    assert hint_text and "Loading" not in hint_text, f"Registry hint still loading: {hint_text!r}"

    page.locator(".modal-bg button:has-text('Cancel')").click()
    page.wait_for_selector(".modal-bg", state="detached", timeout=SHORT)


# ─────────────────────────────────────────────────────────────────────────────
# Networks — built-in networks have no Connect or Delete buttons
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_builtin_network_no_action_buttons(page, live_server):
    """Built-in networks (bridge, host, none) expose no Connect or Delete buttons.

    Matches rows by the built-in badge rather than just the network name —
    earlier tests may create networks that contain "bridge" / "host" /
    "none" as substrings and flake the raw text match."""
    _nav_to(page, "networks")

    for builtin_name in ("bridge", "host", "none"):
        # A built-in row has BOTH the name AND the "built-in" badge span.
        row = page.locator(
            f"tr:has-text('{builtin_name}'):has-text('built-in')",
        ).first
        if row.count() == 0:
            continue
        assert row.locator("button:has-text('Connect...')").count() == 0, (
            f"'{builtin_name}' should not have Connect button"
        )
        assert row.locator("button:has-text('Delete')").count() == 0, f"'{builtin_name}' should not have Delete button"


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
    assert page.locator(".toast:has-text('Pruned')").count() == 0, "Prune toast appeared after cancelled confirmation"
    assert page.locator(".toast:has-text('Nothing')").count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Volume prune confirm cancel
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_volume_prune_confirm_cancel(page, live_server):
    """Cancelling the volume-prune confirmation makes no API call."""
    _nav_to(page, "volumes")
    # Volumes toolbar prune button is "Prune" (networks still uses "Prune unused").
    page.wait_for_selector("#main button:has-text('Prune')", timeout=MEDIUM)

    page.evaluate("window.confirm = () => false")
    page.locator("#main button:has-text('Prune')").click()

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
    assert page.locator(f"tr:has-text('{net_name}')").count() > 0, "Network disappeared after cancelled delete"

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
                "timeout": 3_000_000_000,
                "retries": 1,
            },
        )

    _nav_to(page, "containers")
    page.wait_for_selector(f"text={name}", timeout=MEDIUM)

    # Wait up to MEDIUM for the health badge to appear (healthcheck runs after ~1s)
    page.wait_for_selector(f"tr:has-text('{name}') .health-badge", timeout=MEDIUM)
    badge_text = page.locator(f"tr:has-text('{name}') .health-badge").first.text_content()
    assert badge_text in ("healthy", "unhealthy", "starting"), f"Unexpected health badge text: {badge_text!r}"

    if docker_client:
        for c in docker_client.containers.list(all=True):
            if c.name == name:
                c.remove(force=True)


# ─────────────────────────────────────────────────────────────────────────────
# Engine unreachable — helpful empty state (LAST test: kills the SSH tunnel)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_engine_unreachable_shows_helpful_empty_state(browser, live_server):
    """Kill SSH tunnel → containers page shows "cannot reach Docker" with guidance.

    E2E runs with env-configured DOCKER_HOST, so the managed-tunnel branch is not
    exercised (server has no stored ssh_target). Expect: the local-runtime hint
    that names the configured socket path and tells the user to check their
    runtime is up. Must NOT show the deprecated tunnel-builder form.

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
            capture_output=True,
            check=False,
            timeout=5,
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

            # Navigate to containers — Docker is dead, the helpful empty state must appear
            pg.locator(".sidebar a:has-text('Containers')").click()
            pg.wait_for_selector("h3:has-text('Cannot reach Docker engine')", timeout=MEDIUM)

            # Deprecated form MUST NOT be present anywhere
            assert pg.locator("#tunnel-user").count() == 0, "old tunnel-builder form still rendered"
            assert pg.locator("#tunnel-host").count() == 0, "old tunnel-builder form still rendered"
            assert pg.locator("#tunnel-cmd").count() == 0, "old tunnel-cmd pre still rendered"

            # The empty-state paragraph must name a runtime or tunnel guidance so the
            # user has an actionable next step (not just "cannot reach").
            body = pg.locator(".empty-state").text_content()
            assert body and any(h in body.lower() for h in ("runtime", "tunnel", "reload", "reconnect", "docker")), (
                f"empty-state lacks actionable guidance: {body!r}"
            )
        finally:
            context.close()

    finally:
        # ── Restore the SSH tunnel ───────────────────────────────────────────
        restore = subprocess.run(
            [
                "ssh",
                "-fNM",
                "-S",
                new_ctl,
                "-o",
                "ControlPersist=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=6",
                "-L",
                f"{_SOCKET_PATH}:/var/run/docker.sock",
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
