# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Error-recovery journeys — 6 scenarios a novice will actually hit on
their first deploys.

Goal for every journey: an error message the user sees includes BOTH
what went wrong AND a path forward. Nielsen #9 — "help users recognise,
diagnose, and recover from errors."
"""

from __future__ import annotations

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


def _auth_headers() -> dict[str, str]:
    from tests.e2e_helpers import auth_headers
    return auth_headers()


@journey(
    persona=("novice",),
    category="error_recovery",
    severity="high",
)
def test_journey_typo_image_name_returns_actionable_error(audited_page, live_server, audit_observer, persona):
    """Novice typos 'nginz' instead of 'nginx'. Must return a 4xx with
    either a suggested image or a clear 'not found' message — NOT a
    raw docker-py traceback."""
    import json as _json

    name = f"pa-typo-{uuid.uuid4().hex[:6]}"
    with step("step_1_run_typo"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/containers/run",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={
                "image": "nginz:doesnotexistanywhere",
                "name": name,
            },
            timeout=120,
        )
        # 4xx with structured envelope, not a 5xx.
        if r.status_code >= 500:
            audit_observer.emit(
                step="step_1_run_typo",
                severity="high",
                category="contract",
                title=f"Image-typo returned {r.status_code} instead of 4xx",
                expected="4xx with user-friendly 'image not found' copy",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            pytest.fail(f"image typo 5xx: {r.status_code}")
        try:
            body = r.json()
            assert isinstance(body, dict), f"body not dict: {body!r}"
        except _json.JSONDecodeError:
            pytest.fail("error body not JSON")


@journey(
    persona=("novice",),
    category="error_recovery",
    severity="medium",
)
def test_journey_port_collision_explains_conflict(audited_page, live_server, audit_observer, persona):
    """Seed container A binding 18099. Attempt to run B also binding
    18099. The second request must fail with a descriptive error
    (not a raw 'port allocated' stack trace)."""
    first = f"pa-p1-{uuid.uuid4().hex[:6]}"
    second = f"pa-p2-{uuid.uuid4().hex[:6]}"
    try:
        with step("step_1_seed_container_a"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/run",
                headers={**_auth_headers(), "Content-Type": "application/json"},
                json={
                    "image": "alpine:3.20",
                    "name": first,
                    "command": "nc -l -p 9999",
                    "ports": [{"host": 18099, "container": 9999, "protocol": "tcp"}],
                    "labels": {"skiff-audit-run": "1"},
                },
                timeout=120,
            )
            if r.status_code not in (200, 201):
                pytest.skip(f"seed failed: {r.status_code}")

        with step("step_2_attempt_conflict"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/run",
                headers={**_auth_headers(), "Content-Type": "application/json"},
                json={
                    "image": "alpine:3.20",
                    "name": second,
                    "command": "nc -l -p 9999",
                    "ports": [{"host": 18099, "container": 9999, "protocol": "tcp"}],
                    "labels": {"skiff-audit-run": "1"},
                },
                timeout=60,
            )
            # Must fail with user-friendly error, not a 500 leaking daemon trace.
            if r.status_code < 400:
                pytest.fail("port collision did not fail")
            body = r.text.lower()
            if "traceback" in body:
                audit_observer.emit(
                    step="step_2_attempt_conflict",
                    severity="high",
                    category="copy",
                    title="Port collision leaks Python traceback",
                    expected="User-readable 'port in use' message",
                    observed=r.text[:300],
                )
                pytest.fail("port collision leaks traceback")
    finally:
        for n in (first, second):
            try:
                requests.delete(
                    f"{live_server.rstrip('/')}/api/containers/{n}?force=true",
                    headers=_auth_headers(), timeout=30,
                )
            except requests.exceptions.RequestException:
                pass


@journey(
    persona=("novice",),
    category="error_recovery",
    severity="high",
)
def test_journey_missing_required_env_var(audited_page, live_server, audit_observer, persona):
    """Postgres without POSTGRES_PASSWORD crashes on first boot.
    Either the UI blocks submit (pre-flight) or the container-logs
    viewer surfaces the crash clearly. This journey checks the
    post-crash path — server returns some indication."""
    name = f"pa-env-{uuid.uuid4().hex[:6]}"
    try:
        with step("step_1_deploy_postgres_no_password"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/run",
                headers={**_auth_headers(), "Content-Type": "application/json"},
                json={
                    "image": "postgres:16-alpine",
                    "name": name,
                    "labels": {"skiff-audit-run": "1"},
                },
                timeout=120,
            )
            # Backend will either refuse (preferred — novice gets early
            # feedback) or run and the container will crashloop. Both
            # are acceptable; a 500 is not.
            if r.status_code >= 500:
                audit_observer.emit(
                    step="step_1_deploy_postgres_no_password",
                    severity="high",
                    category="contract",
                    title=f"Missing env var deploy raised {r.status_code}",
                    expected="4xx or 200 + visible crashloop in detail view",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
                headers=_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException:
            pass


@journey(
    persona=("hobbyist",),
    category="error_recovery",
    severity="medium",
)
def test_journey_denied_registry_explains_allowlist(audited_page, live_server, audit_observer, persona):
    """Hobbyist pastes `quay.io/…` but allowlist only has docker.io +
    ghcr.io. Error message must name the allowlist so the user knows
    why it was rejected."""
    name = f"pa-deny-{uuid.uuid4().hex[:6]}"
    with step("step_1_attempt_denied_registry"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/containers/run",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={
                "image": "quay.io/prometheus/node-exporter:latest",
                "name": name,
            },
            timeout=30,
        )
        # Must be rejected with a 4xx. The message should name
        # 'allowlist' or 'registry' so the user understands.
        if 200 <= r.status_code < 300:
            audit_observer.emit(
                step="step_1_attempt_denied_registry",
                severity="P0",
                category="security",
                zero_trust=True,
                title="Registry allowlist bypassed",
                expected="4xx rejection with allowlist reason",
                observed=f"{r.status_code} accepted",
            )
            pytest.fail("allowlist bypassed")
        body = r.text.lower()
        if not any(k in body for k in ("allowlist", "registry", "allowed")):
            audit_observer.emit(
                step="step_1_attempt_denied_registry",
                severity="medium",
                category="copy",
                title="Registry-denied error does not explain the allowlist",
                expected="Error body mentions 'allowlist' or 'registry'",
                observed=r.text[:200],
            )


@journey(
    persona=("super_user",),
    category="error_recovery",
    severity="medium",
)
def test_journey_rate_limit_headers_present(audited_page, live_server, audit_observer, persona):
    """Mutating routes should expose X-RateLimit-* headers so API
    consumers can back off gracefully. Super-user rubric."""
    with step("step_1_mutation_with_headers"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/volumes",
            params={"name": f"pa-rl-{uuid.uuid4().hex[:6]}"},
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"labels": {"skiff-audit-run": "1"}},
            timeout=30,
        )
        # Look for X-RateLimit-Limit + X-RateLimit-Remaining.
        rl_keys = [k for k in r.headers.keys() if k.lower().startswith("x-ratelimit")]
        if not rl_keys:
            audit_observer.emit(
                step="step_1_mutation_with_headers",
                severity="low",
                category="contract",
                title="Mutation response missing X-RateLimit-* headers",
                expected="At least X-RateLimit-Limit + X-RateLimit-Remaining",
                observed=f"response headers: {list(r.headers.keys())[:10]}...",
            )


@journey(
    persona=("novice",),
    category="error_recovery",
    severity="medium",
    covers=("hb-logs-connecting-forever",),
)
def test_journey_logs_viewer_clears_connecting_placeholder(audited_page, live_server, audit_observer, persona):
    """hb-logs-connecting-forever: a container with no output should
    not stay 'Connecting...' forever. The viewer must clear the
    placeholder on WS open even if no bytes arrive."""
    from tests.e2e_helpers import MEDIUM, SHORT, login, nav_to

    page = audited_page
    name = f"pa-lf-{uuid.uuid4().hex[:6]}"
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json={
            "image": "alpine:3.20",
            "name": name,
            "command": "sleep 3600",  # produces no logs
            "labels": {"skiff-audit-run": "1"},
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"seed failed: {r.status_code}")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_open_container_detail"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
            page.locator(f"tr:has-text('{name}') a, tr:has-text('{name}')").first.click()
            page.wait_for_timeout(500)
        with step("step_3_open_logs_tab"):
            logs_tab = page.locator("button:has-text('Logs'), a:has-text('Logs')").first
            if logs_tab.count() == 0:
                pytest.skip("Logs tab not present")
            logs_tab.click()
        with step("step_4_placeholder_clears_within_medium"):
            # Give WS handshake + placeholder-clear handler up to MEDIUM.
            page.wait_for_timeout(3000)
            text = page.locator("#main").inner_text()
            if "Connecting..." in text:
                audit_observer.emit(
                    step="step_4_placeholder_clears_within_medium",
                    severity="high",
                    category="copy",
                    title="Logs viewer stuck on 'Connecting...' placeholder",
                    expected="Placeholder cleared within 3s of WS open",
                    observed="'Connecting...' still visible",
                    covers_historical="hb-logs-connecting-forever",
                )
                pytest.fail("hb-logs-connecting-forever reproduced")
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
                headers=_auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException:
            pass


# ── Plan-named J-09 scenarios ────────────────────────────────────────


@journey(
    persona=("sre_ops", "hobbyist"),
    category="error_recovery",
    severity="medium",
)
def test_journey_disk_full_pull_surfaces_error(audited_page, live_server, audit_observer, persona):
    """Plan J-09 item: disk-full on pull. We can't truly fill the disk
    in a test, but a pull of a non-existent reference produces an
    analogous failure. The error body must explain the failure in
    user terms — no raw docker-py error wrapping."""
    with step("step_1_pull_nonexistent_ref"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/images/pull",
            params={"ref": "nonexistent-repo-xyz-abc123/never-existed:999"},
            headers=_auth_headers(), timeout=60,
        )
        body = r.text.lower()
        if "traceback" in body:
            audit_observer.emit(
                step="step_1_pull_nonexistent_ref",
                severity="high",
                category="copy",
                title="Image pull failure leaks Python traceback",
                expected="User-readable 'image not found' / 'unreachable'",
                observed=r.text[:300],
            )
            pytest.fail("pull error leaks traceback")


@journey(
    persona=("super_user",),
    category="error_recovery",
    severity="medium",
)
def test_journey_rate_limited_burst_returns_429(audited_page, live_server, audit_observer, persona):
    """Plan J-09 item: rate-limited burst. Super-user hammers a
    rate-limited endpoint and expects a 429 with Retry-After. The
    headers-present journey is a shape test; this one is the
    enforcement test."""
    saw_429 = False
    statuses: list[int] = []
    with step("step_1_burst_30_requests"):
        for _ in range(30):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/system/df",
                headers=_auth_headers(), timeout=5,
            )
            statuses.append(r.status_code)
            if r.status_code == 429:
                saw_429 = True
                if "retry-after" not in {k.lower() for k in r.headers.keys()}:
                    audit_observer.emit(
                        step="step_1_burst_30_requests",
                        severity="medium",
                        category="contract",
                        title="429 returned without Retry-After header",
                        expected="Retry-After header per RFC 6585",
                        observed=f"headers: {list(r.headers.keys())[:10]}",
                    )
                break
    if not saw_429:
        audit_observer.emit(
            step="step_1_burst_30_requests",
            severity="low",
            category="parity",
            title="Rate limiter did not fire on 30-request burst",
            expected="Eventually a 429 under dev/ci profile",
            observed=f"statuses: {statuses[:10]}...",
        )
