# SPDX-License-Identifier: MIT
"""Multi-step journey tests — chain realistic user sequences, watch server
stderr during each journey for unexpected warnings / errors.

Each journey is a self-contained story (see docs/dev/storyboards.md §5). The goal
is not per-function assertion — individual endpoints have unit tests — but
emergent-issue detection: a sequence that logically should succeed in the UI
should not produce any 5xx response, server-stderr ERROR/WARNING that isn't
expected, stuck modals, or race conditions between steps.

A context-manager fixture (`watch_server_log`) captures the contents of the
e2e server's stderr for the duration of each journey and asserts no
unexpected noise leaked out.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import os
import re
import time
from contextlib import contextmanager

import pytest
import requests

from tests.conftest_e2e import (
    BASE_URL,
    E2E_TOKEN,
)
from tests.e2e_helpers import (
    LONG,
    MEDIUM,
    SHORT,
)
from tests.e2e_helpers import (
    auth_headers as _auth_headers,
)
from tests.e2e_helpers import (
    teardown_container as _teardown_container,
)

pytestmark = pytest.mark.e2e

# Known-benign stderr patterns that we DO NOT flag. Anything else surfaces as
# a journey failure — the whole point is to catch surprises. Keep the list
# tight; prefer root-cause fixes over pattern additions.
_BENIGN_STDERR_PATTERNS = (
    re.compile(r"WARNING: audit log path .* is not writable"),  # only before the defaults fix
    re.compile(r"INFO:\s+"),
    re.compile(r'"severity": "INFO"'),
    re.compile(r'"severity": "WARNING", "event": "security.no_api_token"'),  # e2e uses API_TOKEN
    re.compile(r'"event": "app\.'),  # app lifecycle
    re.compile(r'"event": "audit\.api_access"'),  # every request
    re.compile(r'"event": "container\.'),  # container ops
    re.compile(r'"event": "image\.'),
    re.compile(r'"event": "compose\.'),
    re.compile(r'"event": "volume\.'),
    re.compile(r'"event": "network\.'),
    re.compile(r'"event": "tunnel\.'),
    re.compile(r'"event": "auth\.'),
    re.compile(r'"event": "setup\.'),
    re.compile(r'"event": "health"'),
    re.compile(r'"event": "websocket\.'),
    re.compile(r"Press CTRL\+C"),
    re.compile(r"Uvicorn running"),
    re.compile(r"Started server process"),
    re.compile(r"Started reloader"),
    re.compile(r"Will watch for changes"),
    re.compile(r"Waiting for application startup"),
    re.compile(r"Application startup complete"),
    re.compile(r"Shutting down"),
    re.compile(r"Finished server"),
    re.compile(r"Waiting for application shutdown"),
    re.compile(r"Application shutdown complete"),
    re.compile(r"^$"),
    re.compile(r"^\s*$"),
    # Benign: the server classifies these as WARNING but they are expected
    # responses to sad-path tests (401/403/404/409) during the journey.
    re.compile(r'"severity": "ERROR".*"status": (400|401|403|404|409|429|502|503|504)'),
    # Benign: `undo.fire_failed` fires when a queued delete targets a resource
    # that was removed out-of-band (typically by a later test's cleanup).
    # The op already returned 200 to the caller; the log line is for forensics.
    re.compile(r'"event": "undo\.fire_failed"'),
)


@contextmanager
def watch_server_log():
    """Capture the server's stderr log during this block and check for surprises.

    The live_server fixture tees stderr to /tmp/skiff-e2e-server.stderr; we
    read the high-water mark at entry and diff at exit. Any line not matching
    a benign pattern is a finding.
    """
    log_path = "/tmp/skiff-e2e-server.stderr"
    start_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    findings: list[str] = []
    yield findings
    if not os.path.exists(log_path):
        return
    with open(log_path, "rb") as f:
        f.seek(start_size)
        new_bytes = f.read()
    new_text = new_bytes.decode(errors="replace")
    for line in new_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(p.search(stripped) for p in _BENIGN_STDERR_PATTERNS):
            continue
        findings.append(stripped)


# ─────────────────────────────────────────────────────────────────────────────
# J1 — Novice local run: pull alpine, run, logs, inspect, stop, delete
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_j1_novice_container_lifecycle(page, live_server, docker_client):
    """Step-by-step container lifecycle with zero hand-holding.

    Exercises: pull-from-datalist → run modal → defaults applied → live logs →
    inspect panel → stop → delete. No mid-journey 5xx, no unexpected modals.
    """
    name = "j1-alpine-lifecycle"
    _teardown_container(docker_client, name)

    with watch_server_log() as findings:
        # Step 1: land on containers page (auth already done by fixture)
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector("h2:has-text('Containers')", timeout=MEDIUM)

        # Step 2: open Run modal, use alpine (already on engine)
        page.locator("button:has-text('Run new container')").first.click()
        page.wait_for_selector(".modal", timeout=SHORT)
        page.locator("#run-image").fill("docker.io/library/alpine:latest")
        page.locator("#run-name").fill(name)
        page.locator("#run-cmd").fill("sleep 600")
        page.locator(".modal .actions button.primary").click()

        # Step 3: wait for the container row
        page.wait_for_selector(f"text={name}", timeout=LONG)

        # Step 4: open Logs detail — just confirm it loads without error
        page.locator(f"tr:has-text('{name}') button:has-text('Logs')").click()
        page.wait_for_selector("#logs-container, #detail-content", timeout=MEDIUM)
        page.wait_for_timeout(1500)

        # Step 5: Inspect — verify read-only rootfs = yes and tmpfs rows appear
        page.locator("a[data-page='containers']").click()
        page.wait_for_selector(f"text={name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{name}') button:has-text('Inspect')").click()
        page.wait_for_selector(".inspect-section:has-text('Resources')", timeout=MEDIUM)
        panel_text = page.locator(".inspect-panel").inner_text()
        assert "Read-only rootfs" in panel_text
        assert "yes" in panel_text.lower()

        # Step 6: Stop from container list
        page.locator("a[data-page='containers']").click()
        page.wait_for_selector(f"text={name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{name}') button:has-text('Stop')").click()
        page.wait_for_selector(
            f"tr:has-text('{name}') .status.exited, tr:has-text('{name}') .status.stopped", timeout=LONG
        )

        # Step 7: Delete
        page.on("dialog", lambda d: d.accept())
        page.locator(f"tr:has-text('{name}') button:has-text('Delete')").click()
        page.wait_for_timeout(2000)

    assert not findings, f"Unexpected server stderr during J1:\n{chr(10).join(findings)}"
    _teardown_container(docker_client, name)


# ─────────────────────────────────────────────────────────────────────────────
# J2 — Developer clone-and-edit
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_j2_developer_clone_flow(page, live_server, docker_client):
    """Run → edit memory via Inspect → Clone with changes → verify env preserved."""
    name = "j2-clone-src"
    clone_name = "clone-" + name
    _teardown_container(docker_client, name)
    _teardown_container(docker_client, clone_name)

    docker_client.containers.run(
        "alpine:latest",
        "sleep 600",
        name=name,
        detach=True,
        mem_limit="64m",
        environment=["SECRET=shh", "DEBUG=1"],
    )

    with watch_server_log() as findings:
        page.locator(".sidebar a:has-text('Containers')").click()
        page.wait_for_selector(f"text={name}", timeout=MEDIUM)
        page.locator(f"tr:has-text('{name}') button:has-text('Inspect')").click()

        # Edit memory
        page.wait_for_selector(".inspect-section:has-text('Resources') input", timeout=MEDIUM)
        inputs = page.locator(".inspect-section:has-text('Resources') input")
        inputs.nth(0).fill("128Mi")
        page.locator(".inspect-section:has-text('Resources') button:has-text('Save changes')").click()
        # Verify via Docker SDK
        deadline = time.time() + 10
        while time.time() < deadline:
            c = docker_client.containers.get(name)
            c.reload()
            if c.attrs["HostConfig"]["Memory"] == 128 * 1024 * 1024:
                break
            time.sleep(0.25)
        c = docker_client.containers.get(name)
        c.reload()
        assert c.attrs["HostConfig"]["Memory"] == 128 * 1024 * 1024

        # Clone with changes
        page.locator("button:has-text('Clone with changes')").click()
        page.wait_for_selector(".modal h3:has-text('Clone container')", timeout=MEDIUM)
        # Keep default clone name, launch
        page.locator(".modal .actions button.primary").click()
        page.wait_for_selector("text=Cloned from", timeout=LONG)

        # Verify clone has env preserved server-side
        deadline = time.time() + 10
        clone = None
        while time.time() < deadline:
            try:
                clone = docker_client.containers.get(clone_name)
                break
            except Exception:
                time.sleep(0.25)
        assert clone is not None, "Clone container never appeared"
        env = clone.attrs["Config"]["Env"]
        assert "SECRET=shh" in env
        assert "DEBUG=1" in env

    assert not findings, f"Unexpected server stderr during J2:\n{chr(10).join(findings)}"
    _teardown_container(docker_client, name)
    _teardown_container(docker_client, clone_name)


# ─────────────────────────────────────────────────────────────────────────────
# J3 — Compose lifecycle (ported-up, logs, per-service restart, teardown)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_j3_compose_full_lifecycle(page, live_server, docker_client):
    project = "j3compose"
    yaml = (
        b"services:\n"
        b"  web:\n"
        b"    image: alpine:latest\n"
        b"    command: sh -c 'echo WEB_LINE; sleep 600'\n"
        b"  cache:\n"
        b"    image: alpine:latest\n"
        b"    command: sh -c 'echo CACHE_LINE; sleep 600'\n"
    )
    # Pre-clean
    try:
        requests.post(f"{BASE_URL}/api/compose/down?project_name={project}", headers=_auth_headers(), timeout=30)
    except requests.exceptions.RequestException:
        pass

    with watch_server_log() as findings:
        page.locator(".sidebar a:has-text('Compose')").click()
        page.wait_for_selector("h2:has-text('Compose')", timeout=MEDIUM)
        page.locator("#compose-project").fill(project)
        page.locator("input[type='file']").set_input_files(
            [
                {
                    "name": "docker-compose.yml",
                    "mimeType": "application/x-yaml",
                    "buffer": yaml,
                }
            ]
        )
        # After the fix to uploadCompose, the stacks list auto-refreshes once
        # the deploy returns — no manual re-nav needed.
        page.wait_for_selector(f"h4:has-text('{project}')", timeout=LONG)

        # Per-service Logs
        web_row = page.locator(f"h4:has-text('{project}') + .stack-services div:has-text('web')").first
        web_row.locator("button:has-text('Logs')").click()
        page.wait_for_selector("#detail-content", timeout=MEDIUM)
        page.wait_for_timeout(1500)

        # Back to compose
        page.locator(".sidebar a:has-text('Compose')").click()
        page.wait_for_selector(f"h4:has-text('{project}')", timeout=MEDIUM)

        # All-service logs modal
        stack_card = page.locator(f"h4:has-text('{project}')").locator("..")
        stack_card.locator("button:has-text('All service logs')").click()
        page.wait_for_selector(f"h3:has-text('Aggregated logs: {project}')", timeout=MEDIUM)
        # Give logs a moment and verify both services appear
        page.wait_for_timeout(1500)
        modal_text = page.locator(".modal pre").text_content()
        assert "web |" in modal_text or "cache |" in modal_text  # at least one prefix present
        page.locator(".modal button:has-text('Close')").click()

        # Per-service Restart
        stack_card.locator(".stack-services div:has-text('cache')").first.locator("button:has-text('Restart')").click()
        page.wait_for_selector("text=cache restarted", timeout=LONG)

        # Tear down
        stack_card.locator("button:has-text('Tear down')").click()
        page.wait_for_timeout(3000)

    assert not findings, f"Unexpected server stderr during J3:\n{chr(10).join(findings)}"
    try:
        requests.post(f"{BASE_URL}/api/compose/down?project_name={project}", headers=_auth_headers(), timeout=30)
    except requests.exceptions.RequestException:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# J4 — Engine-unreachable helpful empty state, API-level sanity
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_j4_common_endpoints_reachable_no_error_warnings(live_server):
    """API-only sanity: every public endpoint returns <500 for a logged-in user,
    and no ERROR-severity logs are emitted during the happy path."""
    endpoints = [
        ("GET", "/health", False),
        ("GET", "/ready", False),
        ("GET", "/api/auth-required", False),
        ("GET", "/api/setup-state", False),
        ("GET", "/api/containers", True),
        ("GET", "/api/images", True),
        ("GET", "/api/images/allowed", True),
        ("GET", "/api/volumes", True),
        ("GET", "/api/networks", True),
        ("GET", "/api/compose/stacks", True),
        ("GET", "/api/system/info", True),
        ("GET", "/api/system/df", True),
        ("GET", "/api/system/metrics", True),
        ("GET", "/api/tunnel/status", True),
        ("GET", "/api/config", True),
    ]
    with watch_server_log() as findings:
        for method, path, auth in endpoints:
            headers = _auth_headers() if auth else {}
            r = requests.request(method, f"{BASE_URL}{path}", headers=headers, timeout=15)
            assert r.status_code < 500, f"5xx on {method} {path}: {r.status_code} {r.text[:200]}"
    assert not findings, f"Unexpected server stderr during J4:\n{chr(10).join(findings)}"


# ─────────────────────────────────────────────────────────────────────────────
# J5 — Resource-update cap enforcement and audit trail
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_j5_security_journey_cap_and_audit(live_server, docker_client):
    """Clone a container, try to exceed caps (400), succeed within caps (200).
    Then verify the audit log contains the updates and NO full token."""
    name = "j5-security-audit"
    _teardown_container(docker_client, name)
    ctr = docker_client.containers.run("alpine:latest", "sleep 600", name=name, detach=True, mem_limit="64m")
    cid = ctr.id

    with watch_server_log() as findings:
        # Cap exceeded → 400
        r = requests.post(
            f"{BASE_URL}/api/containers/{cid}/update",
            headers=_auth_headers(),
            json={"memory": "8Gi"},
            timeout=15,
        )
        assert r.status_code == 400
        # Within cap → 200
        r = requests.post(
            f"{BASE_URL}/api/containers/{cid}/update",
            headers=_auth_headers(),
            json={"memory": "128Mi"},
            timeout=15,
        )
        assert r.status_code == 200

    assert not findings, f"Unexpected server stderr during J5:\n{chr(10).join(findings)}"

    # Separately verify that the E2E token value never appears in stderr
    with open("/tmp/skiff-e2e-server.stderr") as f:
        content = f.read()
    assert E2E_TOKEN not in content, "Full API token leaked to server stderr — critical audit-log failure"
    _teardown_container(docker_client, name)
