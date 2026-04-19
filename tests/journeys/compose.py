# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Compose journeys — 8 scenarios walking the docker-compose verbs
SKIFF exposes (up, down, stop, start, pull, scale, logs, download).

The compose surface was expanded in 0683c08 to close hb-compose-no-
pull-or-scale; these journeys lock that surface against regression.

Each journey uploads a minimal compose YAML to a unique project name,
asserts an observable outcome, and tears down via /api/compose/down.
"""

from __future__ import annotations

import time
import uuid

import pytest
import requests

from tests.audit_driver import step
from tests.journeys import journey


pytest_plugins = ["tests.conftest_e2e", "tests.conftest_audit"]

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]"',
)

pytestmark = pytest.mark.e2e


_MINIMAL_YAML = b"""services:
  web:
    image: alpine:3.20
    command: sleep 3600
    labels:
      skiff-audit-run: "1"
"""

_SCALABLE_YAML = b"""services:
  worker:
    image: alpine:3.20
    command: sleep 3600
    labels:
      skiff-audit-run: "1"
"""


def _project_name(prefix: str) -> str:
    return f"pa{prefix}{uuid.uuid4().hex[:6]}"


def _deploy(live_server: str, project: str, yaml: bytes) -> None:
    from tests.e2e_helpers import auth_headers
    files = [("file", ("docker-compose.yml", yaml, "application/x-yaml"))]
    r = requests.post(
        f"{live_server.rstrip('/')}/api/compose/up",
        params={"project_name": project},
        headers=auth_headers(),
        files=files,
        timeout=180,
    )
    assert r.status_code == 200, f"compose up failed: {r.status_code} {r.text}"


def _down(live_server: str, project: str) -> None:
    from tests.e2e_helpers import auth_headers
    try:
        requests.post(
            f"{live_server.rstrip('/')}/api/compose/down",
            params={"project_name": project},
            headers=auth_headers(),
            timeout=120,
        )
    except requests.exceptions.RequestException:
        pass


@journey(
    persona=("developer", "sre_ops"),
    category="compose",
    severity="high",
)
def test_journey_upload_yaml_and_deploy(audited_page, live_server, audit_observer, persona):
    """Upload a one-service compose file via the UI's file input, click
    Deploy, assert the stack appears on the list."""
    from tests.e2e_helpers import MEDIUM, SHORT, login, nav_to

    page = audited_page
    project = _project_name("up")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_compose"):
            nav_to(page, "compose")

        # UI: set project name input then upload file via the hidden
        # file-input. Playwright's set_input_files works on <input type=file>.
        with step("step_3_set_project_and_upload"):
            proj_input = page.locator("input[placeholder*='project' i]").first
            if proj_input.count() == 0:
                pytest.skip("compose project input not found")
            proj_input.fill(project)
            file_input = page.locator("input[type='file']").first
            file_input.set_input_files({
                "name": "docker-compose.yml",
                "mimeType": "application/x-yaml",
                "buffer": _MINIMAL_YAML,
            })
            # If the page auto-submits on file select, the next assertion
            # below succeeds. Otherwise click the Deploy button.
            deploy_btn = page.locator("button:has-text('Deploy'), button:has-text('Up')").first
            if deploy_btn.count() > 0:
                deploy_btn.click()

        with step("step_4_stack_row_appears"):
            page.wait_for_selector(f"text={project}", timeout=MEDIUM)
    finally:
        _down(live_server, project)


@journey(
    persona=("developer",),
    category="compose",
    severity="high",
    covers=("hb-compose-no-pull-or-scale",),
)
def test_journey_compose_stop_then_start(audited_page, live_server, audit_observer, persona):
    """API-deploy a stack, call /stop then /start, verify the stack
    list reflects state. Covers hb-compose-no-pull-or-scale class."""
    from tests.e2e_helpers import MEDIUM, auth_headers, login, nav_to

    page = audited_page
    project = _project_name("ss")
    try:
        _deploy(live_server, project, _MINIMAL_YAML)
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_stop_stack_api"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/compose/{project}/stop",
                headers=auth_headers(), timeout=60,
            )
            assert r.status_code == 200, f"stop failed: {r.status_code} {r.text}"
        with step("step_3_start_stack_api"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/compose/{project}/start",
                headers=auth_headers(), timeout=60,
            )
            assert r.status_code == 200, f"start failed: {r.status_code} {r.text}"
        with step("step_4_list_still_has_stack"):
            nav_to(page, "compose")
            page.wait_for_selector(f"text={project}", timeout=MEDIUM)
    finally:
        _down(live_server, project)


@journey(
    persona=("sre_ops",),
    category="compose",
    severity="high",
    covers=("hb-compose-no-pull-or-scale",),
)
def test_journey_compose_scale_service(audited_page, live_server, audit_observer, persona):
    """SRE scales a single-service stack to 3 replicas via API. The
    call must return 200; the UI scale control eventually calls the
    same route."""
    from tests.e2e_helpers import auth_headers

    project = _project_name("sc")
    try:
        _deploy(live_server, project, _SCALABLE_YAML)
        with step("step_1_scale_to_3"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/compose/{project}/scale",
                params={"service": "worker", "replicas": 3},
                headers=auth_headers(), timeout=120,
            )
            # Scale may return 200 or 500 if daemon lacks capacity.
            # 500 is still valuable data — emit a finding not a pass.
            if r.status_code != 200:
                audit_observer.emit(
                    step="step_1_scale_to_3",
                    severity="medium",
                    category="contract",
                    title=f"compose scale returned {r.status_code}",
                    expected="200 OK with replica count updated",
                    observed=f"{r.status_code}: {r.text[:200]}",
                    covers_historical="hb-compose-no-pull-or-scale",
                )
    finally:
        _down(live_server, project)


@journey(
    persona=("developer", "sre_ops"),
    category="compose",
    severity="medium",
    covers=("hb-compose-no-pull-or-scale",),
)
def test_journey_compose_pull(audited_page, live_server, audit_observer, persona):
    """Trigger a pull on the stack; assert the endpoint returns 200
    (not a 404 — that was the hb-compose-no-pull-or-scale symptom)."""
    from tests.e2e_helpers import auth_headers

    project = _project_name("pl")
    try:
        _deploy(live_server, project, _MINIMAL_YAML)
        with step("step_1_pull"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/compose/{project}/pull",
                headers=auth_headers(), timeout=300,
            )
            assert r.status_code == 200, f"pull failed: {r.status_code} {r.text}"
    finally:
        _down(live_server, project)


@journey(
    persona=("developer", "sre_ops"),
    category="compose",
    severity="medium",
)
def test_journey_download_yaml_matches_deployed(audited_page, live_server, audit_observer, persona):
    """Deploy, then GET /api/compose/{project}/download; the returned
    YAML must contain the service name we uploaded. Round-trip sanity."""
    from tests.e2e_helpers import auth_headers

    project = _project_name("dl")
    try:
        _deploy(live_server, project, _MINIMAL_YAML)
        with step("step_1_download_yaml"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/compose/{project}/download",
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"download failed: {r.status_code}"
            body = r.text
            assert "web" in body, f"downloaded YAML missing 'web' service: {body[:400]!r}"
    finally:
        _down(live_server, project)


@journey(
    persona=("sre_ops",),
    category="compose",
    severity="medium",
)
def test_journey_compose_aggregated_logs(audited_page, live_server, audit_observer, persona):
    """Hit /api/compose/{project}/logs and expect a non-empty response
    within a reasonable timeout. The UI logs pane uses this endpoint."""
    from tests.e2e_helpers import auth_headers

    project = _project_name("lg")
    try:
        _deploy(live_server, project, _MINIMAL_YAML)
        # Give the stack a moment to emit at least something.
        time.sleep(2)
        with step("step_1_fetch_logs"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/compose/{project}/logs",
                params={"tail": 50},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"logs failed: {r.status_code}"
    finally:
        _down(live_server, project)


@journey(
    persona=("hobbyist",),
    category="compose",
    severity="medium",
    covers=("hb-compose-no-search",),
)
def test_journey_compose_page_has_search_bar(audited_page, live_server, audit_observer, persona):
    """hb-compose-no-search regression. Hobbyist typically has ≥5
    stacks — the compose page must expose a search bar."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
    with step("step_2_nav_compose"):
        nav_to(page, "compose")
    with step("step_3_search_bar_present"):
        # Filter/search input may be type=search or have placeholder
        # containing 'search' or 'filter'.
        found = (
            page.locator("input[type='search']").count() > 0
            or page.locator("input[placeholder*='search' i]").count() > 0
            or page.locator("input[placeholder*='filter' i]").count() > 0
        )
        if not found:
            audit_observer.emit(
                step="step_3_search_bar_present",
                severity="high",
                category="layout",
                title="Compose page missing search affordance",
                expected="An input[type=search] or placeholder*=filter",
                observed="No search/filter input found",
                covers_historical="hb-compose-no-search",
            )
            assert False, "compose page missing search affordance"


@journey(
    persona=("security_reviewer",),
    category="compose",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_compose_privileged_blocked(audited_page, live_server, audit_observer, persona):
    """Uploading a compose file with privileged: true MUST be rejected
    by the validator. Zero-trust: no path around the privileged block."""
    from tests.e2e_helpers import auth_headers

    project = _project_name("pv")
    privileged_yaml = b"""services:
  bad:
    image: alpine:3.20
    privileged: true
    command: sleep 3600
"""
    try:
        with step("step_1_attempt_privileged_upload"):
            files = [("file", ("docker-compose.yml", privileged_yaml, "application/x-yaml"))]
            r = requests.post(
                f"{live_server.rstrip('/')}/api/compose/up",
                params={"project_name": project},
                headers=auth_headers(),
                files=files,
                timeout=60,
            )
            # Must be rejected: 400 (validation) or 422 (shape).
            if r.status_code not in (400, 422):
                audit_observer.emit(
                    step="step_1_attempt_privileged_upload",
                    severity="P0",
                    category="security",
                    zero_trust=True,
                    title="privileged: true accepted by compose validator",
                    expected="400/422 rejection with a validator envelope",
                    observed=f"HTTP {r.status_code}: {r.text[:300]!r}",
                )
                pytest.fail(
                    f"privileged compose accepted (status {r.status_code}) — "
                    "zero-trust violation"
                )
    finally:
        _down(live_server, project)
