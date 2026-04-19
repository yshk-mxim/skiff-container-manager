# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Audit + observability journeys — 5 scenarios covering the audit log
viewer, docker events stream, Prometheus scrape, and cross-surface
correlation.

These journeys assert the SRE rubric: a failing stack's root cause
must be diagnosable from SKIFF alone (audit + events + logs + stats)
without leaving the UI.
"""

from __future__ import annotations

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


@journey(
    persona=("sre_ops", "security_reviewer"),
    category="audit_observability",
    severity="high",
    covers=("hb-audit-silently-truncated",),
)
def test_journey_audit_log_page_discloses_cap(audited_page, live_server, audit_observer, persona):
    """Audit log page must expose its tail cap in the UI (either a
    selector or explicit copy). hb-audit-silently-truncated was the
    symptom of a silent `?tail=200` — the fix surfaces the control."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
    with step("step_2_navigate_to_system"):
        # Audit log typically lives under /system.
        page.locator(".sidebar a:has-text('System')").click()
        page.wait_for_selector("h2:has-text('System')", timeout=SHORT)
    with step("step_3_find_audit_cap_control"):
        # Look for a selector / input mentioning "tail" OR copy that
        # explicitly names the cap.
        cap_control_present = (
            page.locator("select:has-text('200'), select:has-text('100'), select:has-text('500')").count() > 0
            or page.locator("input[name*='tail' i]").count() > 0
            or page.locator("text=/showing (last|up to)/i").count() > 0
            or page.locator("text=/last \\d+ entries/i").count() > 0
        )
        if not cap_control_present:
            audit_observer.emit(
                step="step_3_find_audit_cap_control",
                severity="high",
                category="copy",
                title="Audit log page does not disclose its tail cap",
                expected="A selector or copy stating how many entries are shown",
                observed="No tail control or count copy found on System page",
                covers_historical="hb-audit-silently-truncated",
            )
            assert False, "audit log cap not disclosed"


@journey(
    persona=("sre_ops",),
    category="audit_observability",
    severity="high",
    covers=("hb-events-missing",),
)
def test_journey_events_endpoint_bounds_since_secs(audited_page, live_server, audit_observer, persona):
    """GET /api/system/events?since_secs=5 must return a JSON array
    bounded by the since_secs parameter. hb-events-missing class —
    docker events stream is a competitor-parity capability."""
    from tests.e2e_helpers import auth_headers

    with step("step_1_call_events_since_5"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/system/events",
            params={"since_secs": 5},
            headers=auth_headers(), timeout=30,
        )
        assert r.status_code == 200, f"events endpoint failed: {r.status_code}"
        body = r.json()
        assert isinstance(body, (list, dict)), f"unexpected shape: {type(body)}"

    with step("step_2_bound_rejects_oversized"):
        # since_secs must be bounded (e.g., ≤ 3600). If the route
        # accepts unbounded values it's a DoS vector.
        r = requests.get(
            f"{live_server.rstrip('/')}/api/system/events",
            params={"since_secs": 10**9},
            headers=auth_headers(), timeout=10,
        )
        if r.status_code == 200:
            audit_observer.emit(
                step="step_2_bound_rejects_oversized",
                severity="medium",
                category="security",
                title="events endpoint does not bound since_secs",
                expected="400/422 on since_secs above reasonable cap",
                observed=f"200 OK on since_secs=1e9",
            )


@journey(
    persona=("sre_ops", "super_user"),
    category="audit_observability",
    severity="medium",
)
def test_journey_prometheus_metrics_scrape(audited_page, live_server, audit_observer, persona):
    """Prometheus must be able to scrape /metrics and get a non-empty
    text response in the exposition format. SRE rubric: metrics go
    straight into a dashboard without SKIFF intervention."""
    from tests.e2e_helpers import auth_headers

    with step("step_1_scrape_metrics"):
        r = requests.get(
            f"{live_server.rstrip('/')}/metrics",
            headers=auth_headers(), timeout=10,
        )
        # Either 200 with exposition-format body, or a 404 if metrics
        # are gated behind a separate port (emit a finding).
        if r.status_code == 404:
            audit_observer.emit(
                step="step_1_scrape_metrics",
                severity="medium",
                category="parity",
                title="/metrics returns 404 — Prometheus parity gap",
                expected="200 OK with exposition-format text body",
                observed="404 Not Found",
            )
            return
        assert r.status_code == 200, f"/metrics failed: {r.status_code}"
        body = r.text
        # Exposition format starts with '# HELP' or '# TYPE' lines.
        assert "# HELP" in body or "# TYPE" in body, (
            f"/metrics body not in exposition format: {body[:200]!r}"
        )


@journey(
    persona=("sre_ops",),
    category="audit_observability",
    severity="medium",
)
def test_journey_audit_log_download_is_jsonl(audited_page, live_server, audit_observer, persona):
    """SRE needs a downloadable audit trail for post-incident review.
    GET /api/system/audit-log?download=1 must return JSONL content-
    type and a body where each line parses to JSON."""
    import json as _json

    from tests.e2e_helpers import auth_headers

    # First ensure audit has at least one entry by hitting any mutating
    # route; an auth'd GET to /api/system/overview is harmless and
    # emits audit events.
    try:
        requests.get(
            f"{live_server.rstrip('/')}/api/system/overview",
            headers=auth_headers(), timeout=10,
        )
    except requests.exceptions.RequestException:
        pass

    with step("step_1_download_audit_jsonl"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/system/audit-log",
            params={"download": 1},
            headers=auth_headers(), timeout=30,
        )
        # 200 with JSONL or 200 with JSON — accept both, but every
        # non-blank line must parse.
        if r.status_code != 200:
            audit_observer.emit(
                step="step_1_download_audit_jsonl",
                severity="medium",
                category="behaviour",
                title="Audit download endpoint returns non-200",
                expected="200 OK with parseable entries",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            return
        body = r.text.strip()
        if not body:
            return  # empty log is acceptable on a fresh server
        # Try parsing as JSON array first, fallback to JSONL.
        try:
            _json.loads(body)
        except ValueError:
            for line in body.splitlines():
                if not line.strip():
                    continue
                try:
                    _json.loads(line)
                except ValueError as exc:
                    audit_observer.emit(
                        step="step_1_download_audit_jsonl",
                        severity="medium",
                        category="contract",
                        title="Audit log line not valid JSON",
                        expected="Each JSONL line parseable via json.loads",
                        observed=f"line failed: {line[:80]!r} ({exc})",
                    )
                    raise


@journey(
    persona=("security_reviewer",),
    category="audit_observability",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_audit_never_leaks_bearer(audited_page, live_server, audit_observer, persona):
    """Audit log MUST never contain raw bearer tokens, even when a
    client sends them. The redactor in skiff/logging_setup.py and
    skiff/validators.py::_redact_dict must strip any bearer/token
    before the entry hits disk."""
    from tests.e2e_helpers import E2E_TOKEN, auth_headers

    # Deliberately include the token in a URL query — this exercises
    # the logging redactor even if the token comes through in an
    # unexpected header or body field.
    with step("step_1_emit_audit_with_token_in_url"):
        requests.get(
            f"{live_server.rstrip('/')}/api/system/overview?api_token=shouldnotleak",
            headers=auth_headers(), timeout=10,
        )

    with step("step_2_read_audit_log"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/system/audit-log",
            params={"tail": 50},
            headers=auth_headers(), timeout=30,
        )
        assert r.status_code == 200, f"audit read failed: {r.status_code}"
        body = r.text
        # Zero-trust: the real bearer must not appear in plaintext, and
        # neither should the fake token we passed in the URL.
        leaked = []
        if E2E_TOKEN in body and len(E2E_TOKEN) >= 16:
            leaked.append("E2E_TOKEN")
        if "shouldnotleak" in body:
            leaked.append("api_token query param")
        if leaked:
            audit_observer.emit(
                step="step_2_read_audit_log",
                severity="P0",
                category="security",
                zero_trust=True,
                title=f"Audit log contains leaked credential markers: {leaked}",
                expected="Redactor strips bearer + api_token before write",
                observed=f"Markers appear in plaintext: {leaked}",
            )
            pytest.fail(f"credential leaked into audit: {leaked}")


# ── Plan-named J-07 scenarios ────────────────────────────────────────


@journey(
    persona=("sre_ops",),
    category="audit_observability",
    severity="medium",
)
def test_journey_events_stream_captures_container_lifecycle(audited_page, live_server, audit_observer, persona):
    """Plan J-07 item: events stream during deploy. Run a container,
    then poll /api/system/events with since_secs=30 and confirm the
    create/start event shows up. SRE rubric: live feed of daemon events
    is visible in SKIFF during a deploy."""
    import time
    import uuid

    from tests.e2e_helpers import auth_headers

    name = f"pa-ev-{uuid.uuid4().hex[:6]}"
    with step("step_1_deploy_container"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/containers/run",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json={
                "image": "alpine:3.20",
                "name": name,
                "command": "sleep 3600",
                "labels": {"skiff-audit-run": "1"},
            },
            timeout=120,
        )
        if r.status_code not in (200, 201):
            pytest.skip(f"deploy failed: {r.status_code}")
    try:
        time.sleep(1)
        with step("step_2_events_contain_deploy"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/system/events",
                params={"since_secs": 30},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"events failed: {r.status_code}"
            body = r.text
            # The event stream should mention the container name or
            # at least a `create` or `start` action. If empty, emit a
            # finding so the SRE rubric tracks parity.
            if name not in body and "create" not in body.lower() and "start" not in body.lower():
                audit_observer.emit(
                    step="step_2_events_contain_deploy",
                    severity="medium",
                    category="parity",
                    title="Events stream did not capture deploy action",
                    expected=f"Event mentioning {name} or 'create'/'start'",
                    observed=f"body prefix: {body[:300]!r}",
                )
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
                headers=auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException:
            pass


@journey(
    persona=("sre_ops",),
    category="audit_observability",
    severity="medium",
)
def test_journey_stderr_audit_ui_correlation(audited_page, live_server, audit_observer, persona):
    """Plan J-07 item: correlate stderr→audit→UI. A 4xx on a mutating
    endpoint should produce (a) a stderr/log line, (b) an audit event,
    (c) an error envelope the UI can render. This journey triggers
    such a 4xx and checks the audit side is populated."""
    import time

    from tests.e2e_helpers import auth_headers

    with step("step_1_trigger_known_4xx"):
        # Invalid container name → 4xx envelope + audit line.
        r = requests.post(
            f"{live_server.rstrip('/')}/api/containers/run",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json={"image": "", "name": "!!bad!!"},
            timeout=10,
        )
        if not (400 <= r.status_code < 500):
            pytest.skip(f"couldn't provoke a 4xx (got {r.status_code})")
    time.sleep(0.5)
    with step("step_2_audit_has_failure_row"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/system/audit-log",
            params={"tail": 20},
            headers=auth_headers(), timeout=30,
        )
        if r.status_code != 200:
            pytest.skip(f"audit read failed: {r.status_code}")
        body = r.text
        # There should be SOMETHING about a failed container create
        # in the recent audit — either a 'failed' entry or a '4xx' tag.
        if "fail" not in body.lower() and "denied" not in body.lower() and "invalid" not in body.lower():
            audit_observer.emit(
                step="step_2_audit_has_failure_row",
                severity="low",
                category="parity",
                title="4xx on container create did not produce a visible audit row",
                expected="Audit tail contains a failure / invalid / denied marker",
                observed=f"audit tail: {body[-300:]!r}",
            )
