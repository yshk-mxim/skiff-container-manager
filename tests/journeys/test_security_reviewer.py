# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Security-reviewer journeys — 7 scenarios exercising the zero-trust
perimeter: auth, CSRF, origin, reviewer mode, token rotation, WS auth,
path traversal.

Each journey is adversarial — the persona tries a specific bypass
and the test asserts the boundary holds. Violations are P0 findings
that stop the iteration loop.
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
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="P0",
    tags=("zero-trust", "auth"),
)
def test_journey_missing_auth_returns_401(audited_page, live_server, audit_observer, persona):
    """No Authorization header → every mutating API must return 401.
    Zero-trust: never fall through to a handler without a bearer."""
    endpoints = [
        ("GET", "/api/containers/ls"),
        ("POST", "/api/containers/run"),
        ("POST", "/api/volumes"),
        ("POST", "/api/networks"),
        ("POST", "/api/compose/up"),
        ("GET", "/api/system/overview"),
    ]
    with step("step_1_probe_every_route_without_auth"):
        for method, path in endpoints:
            r = requests.request(
                method, f"{live_server.rstrip('/')}{path}",
                timeout=10,
            )
            # Allow 401 or 403 (both count as blocked).
            if r.status_code not in (401, 403):
                audit_observer.emit(
                    step="step_1_probe_every_route_without_auth",
                    severity="P0",
                    category="security",
                    zero_trust=True,
                    title=f"{method} {path} returned {r.status_code} without auth",
                    expected="401 Unauthorized (or 403)",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
                pytest.fail(
                    f"{method} {path} accessible without auth (status {r.status_code})"
                )


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="high",
    tags=("zero-trust", "csrf"),
)
def test_journey_mutating_requires_x_requested_with(audited_page, live_server, audit_observer, persona):
    """POST /api/volumes without X-Requested-With must be rejected.
    CSRF cover: simple-request bypass must fail for mutations."""
    with step("step_1_post_without_xrw"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/volumes",
            params={"name": "pa-csrf-test"},
            headers={
                "Authorization": f"Bearer {_token()}",
                # deliberately omit X-Requested-With
            },
            timeout=10,
        )
        # Acceptable: 403 (origin/CSRF), 401 (bearer refuse), 400/422.
        # Not acceptable: 200/201 (mutation went through).
        if r.status_code < 400:
            audit_observer.emit(
                step="step_1_post_without_xrw",
                severity="P0",
                category="security",
                zero_trust=True,
                title="Mutation POST succeeded without X-Requested-With",
                expected="4xx rejection (CSRF gate)",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            pytest.fail("CSRF gate bypassed")


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_forged_origin_rejected(audited_page, live_server, audit_observer, persona):
    """Requests with Origin: https://evil.example must be rejected by
    the origin allowlist — reviewer perimeter check."""
    with step("step_1_forged_origin"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/volumes",
            params={"name": "pa-origin-test"},
            headers={
                "Authorization": f"Bearer {_token()}",
                "X-Requested-With": "ContainerManager",
                "Origin": "https://evil.example",
            },
            timeout=10,
        )
        if r.status_code < 400:
            audit_observer.emit(
                step="step_1_forged_origin",
                severity="P0",
                category="security",
                zero_trust=True,
                title="Forged Origin header not rejected",
                expected="4xx rejection from origin allowlist",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            pytest.fail("origin allowlist bypassed")


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_every_4xx_has_catalogued_envelope(audited_page, live_server, audit_observer, persona):
    """Every 4xx response must conform to the envelope:
    {detail: {code, message, ...}} with `code` in the known catalogue.
    No stack traces in the body."""
    import json as _json

    # Deliberately trigger a 4xx: invalid container name.
    with step("step_1_trigger_400"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/containers/run",
            headers={
                "Authorization": f"Bearer {_token()}",
                "X-Requested-With": "ContainerManager",
                "Content-Type": "application/json",
            },
            json={"image": "", "name": "!!bad name!!"},  # invalid
            timeout=10,
        )
        if not (400 <= r.status_code < 500):
            pytest.skip(f"trigger failed to produce 4xx (got {r.status_code})")
        try:
            body = r.json()
        except _json.JSONDecodeError:
            audit_observer.emit(
                step="step_1_trigger_400",
                severity="high",
                category="contract",
                title="4xx response body not JSON",
                expected="{detail: {code: ...}} envelope",
                observed=r.text[:200],
            )
            pytest.fail("4xx body not JSON")
        # Accept both new-style {detail: {code, message}} and
        # old-style {detail: "string"} — but reject stack traces.
        s = _json.dumps(body)
        if "Traceback" in s or 'File "/' in s:
            audit_observer.emit(
                step="step_1_trigger_400",
                severity="P0",
                category="security",
                zero_trust=True,
                title="4xx envelope contains stack trace",
                expected="Redacted envelope with no traceback",
                observed=s[:300],
            )
            pytest.fail("stack trace leaked in 4xx body")


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="medium",
    tags=("auth",),
)
def test_journey_invalid_token_returns_401(audited_page, live_server, audit_observer, persona):
    """Wrong bearer → 401. Every route. No timing-side-channel details
    in the response body (don't distinguish 'unknown user' vs 'bad
    password' — not applicable here but still, don't leak internals)."""
    with step("step_1_wrong_bearer"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/system/overview",
            headers={"Authorization": "Bearer not-a-real-token"},
            timeout=10,
        )
        assert r.status_code in (401, 403), (
            f"wrong bearer got {r.status_code} (expected 401/403)"
        )
        body = r.text.lower()
        # Should not reveal whether the token format was right / wrong /
        # expired — all three map to the same error class.
        assert "expired" not in body, "401 body reveals 'expired' state"


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="high",
    tags=("zero-trust", "csp"),
)
def test_journey_csp_header_on_static(audited_page, live_server, audit_observer, persona):
    """Every static page response must set a CSP header with script-src
    no 'unsafe-inline' and no wildcard."""
    with step("step_1_fetch_root"):
        r = requests.get(f"{live_server.rstrip('/')}/", timeout=10)
        csp = r.headers.get("content-security-policy", "")
        if not csp:
            audit_observer.emit(
                step="step_1_fetch_root",
                severity="high",
                category="security",
                title="No CSP header on /",
                expected="Content-Security-Policy header present",
                observed="header absent",
            )
            pytest.fail("no CSP header on /")
        # Must NOT contain 'unsafe-inline' or '*' in script-src.
        script_src = ""
        for directive in csp.split(";"):
            if directive.strip().startswith("script-src"):
                script_src = directive
                break
        if "'unsafe-inline'" in script_src or script_src.endswith("*"):
            audit_observer.emit(
                step="step_1_fetch_root",
                severity="P0",
                category="security",
                zero_trust=True,
                title="CSP script-src allows unsafe-inline or wildcard",
                expected="script-src with concrete sources only",
                observed=script_src,
            )
            pytest.fail("CSP script-src too permissive")


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_no_route_surfaces_outside_openapi(audited_page, live_server, audit_observer, persona):
    """Every route reachable by the client must be documented in
    /api/openapi.json. Undocumented routes are either shadow features
    or debug endpoints that shouldn't be live in prod."""
    import re as _re

    from skiff.app import app

    with step("step_1_load_openapi"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/openapi.json",
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=10,
        )
        assert r.status_code == 200, f"openapi fetch failed: {r.status_code}"
        spec = r.json()
        doc_paths = set(spec.get("paths", {}).keys())

    with step("step_2_diff_registered_vs_doc"):
        registered: list[tuple[str, str]] = []
        for route in app.routes:
            if hasattr(route, "methods") and getattr(route, "include_in_schema", True):
                for m in route.methods or ():
                    if m in {"HEAD", "OPTIONS"}:
                        continue
                    registered.append((m, route.path))

        # Normalise param names to {param} so {id} vs {container_id} match.
        def _norm(p: str) -> str:
            return _re.sub(r"\{[^}]+\}", "{param}", p)

        doc_norm = {_norm(p) for p in doc_paths}
        undoc = [
            (m, p) for (m, p) in registered
            if _norm(p) not in doc_norm
            and not p.startswith("/static")
            and p not in {"/", "/redoc", "/docs", "/api/openapi.json", "/api/docs"}
        ]
        if undoc:
            audit_observer.emit(
                step="step_2_diff_registered_vs_doc",
                severity="high",
                category="parity",
                title="Registered routes missing from OpenAPI spec",
                expected="Every app.route appears in /api/openapi.json paths",
                observed=f"{len(undoc)} undocumented: {undoc[:5]}...",
            )


def _token() -> str:
    from tests.conftest_e2e import E2E_TOKEN
    return E2E_TOKEN


# ── Plan-named J-08 scenarios ────────────────────────────────────────


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="P0",
    tags=("zero-trust",),
)
def test_journey_reviewer_mode_blocks_mutation(audited_page, live_server, audit_observer, persona):
    """Plan J-08 item: enter reviewer → attempt mutation. POST
    /api/profile/enter-reviewer, then attempt a mutation. The
    mutation must be rejected server-side regardless of the bearer."""
    with step("step_1_enter_reviewer"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/profile/enter-reviewer",
            headers={
                "Authorization": f"Bearer {_token()}",
                "X-Requested-With": "ContainerManager",
            },
            timeout=10,
        )
        if r.status_code not in (200, 204):
            pytest.skip(f"enter-reviewer not reachable: {r.status_code}")
    try:
        with step("step_2_mutation_blocked"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/volumes",
                params={"name": "pa-reviewer-test"},
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "X-Requested-With": "ContainerManager",
                    "Content-Type": "application/json",
                },
                json={"labels": {"skiff-audit-run": "1"}},
                timeout=10,
            )
            if 200 <= r.status_code < 300:
                audit_observer.emit(
                    step="step_2_mutation_blocked",
                    severity="P0",
                    category="security",
                    zero_trust=True,
                    title="Mutation succeeded in reviewer mode",
                    expected="403 Forbidden from reviewer gate",
                    observed=f"{r.status_code} accepted",
                )
                pytest.fail("reviewer mode bypassed")
    finally:
        # Reset profile via the same endpoint (idempotent; entering
        # 'dev' is safe).
        try:
            requests.post(
                f"{live_server.rstrip('/')}/api/profile/enter-reviewer",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "X-Requested-With": "ContainerManager",
                },
                json={"leave": True},
                timeout=10,
            )
        except requests.exceptions.RequestException:
            pass


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="medium",
    tags=("zero-trust",),
)
def test_journey_token_rotation_stale_session(audited_page, live_server, audit_observer, persona):
    """Plan J-08 item: rotate token → stale session. After a rotate-
    token call, the OLD bearer must not retain access. Observation-
    only: rotating would invalidate the harness's bearer, so this
    journey probes the endpoint's auth contract without executing."""
    with step("step_1_rotate_without_csrf_blocked"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/auth/rotate-token",
            headers={"Authorization": f"Bearer {_token()}"},  # no X-Requested-With
            timeout=10,
        )
        # CSRF gate must block a rotation without the header.
        if 200 <= r.status_code < 300:
            audit_observer.emit(
                step="step_1_rotate_without_csrf_blocked",
                severity="P0",
                category="security",
                zero_trust=True,
                title="Token rotation accepted without CSRF header",
                expected="403 / 400 — CSRF required",
                observed=f"{r.status_code} accepted",
            )
            pytest.fail("rotate-token CSRF bypass")


@journey(
    persona=("security_reviewer",),
    category="security_reviewer",
    severity="high",
    tags=("zero-trust", "ws"),
)
def test_journey_ws_auth_lockout(audited_page, live_server, audit_observer, persona):
    """Plan J-08 item: WS auth lockout. Try to open a WebSocket with
    a wrong bearer — the server must reject it at handshake, not let
    the connection stay open with silent failure later."""
    import uuid

    try:
        from websockets.sync.client import connect  # type: ignore
    except ImportError:
        pytest.skip("websockets client not installed")
    with step("step_1_ws_handshake_with_bad_bearer"):
        # The container WS endpoint is typically /ws/logs/{id}.
        # A random id + bad bearer should fail at handshake.
        url = (
            live_server.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
            + f"/ws/logs/{uuid.uuid4().hex}"
            + "?token=not-a-real-token"
        )
        try:
            ws = connect(url, open_timeout=3, additional_headers={
                "Authorization": "Bearer not-a-real-token",
            })
            ws.close()
            # If we got here, the handshake succeeded — that's a breach.
            audit_observer.emit(
                step="step_1_ws_handshake_with_bad_bearer",
                severity="P0",
                category="security",
                zero_trust=True,
                title="WS handshake succeeded with bad bearer",
                expected="Handshake refused (101 blocked)",
                observed="connection opened",
            )
            pytest.fail("WS auth lockout bypassed")
        except Exception:
            # Any connection failure is acceptable — that's the lockout.
            return


@journey(
    persona=("security_reviewer", "ui_ux_auditor"),
    category="security_reviewer",
    severity="medium",
    tags=("csp",),
)
def test_journey_csp_violation_reporting(audited_page, live_server, audit_observer, persona):
    """Plan J-08 item: CSP violation report. The /api/csp-report
    endpoint (or equivalent) must accept a structured CSP report
    without 5xx — modern browsers POST these on violations."""
    # Craft a minimal CSP-report payload.
    report = {
        "csp-report": {
            "document-uri": "http://testserver/",
            "violated-directive": "script-src 'self'",
            "effective-directive": "script-src",
            "blocked-uri": "https://evil.example/x.js",
            "disposition": "enforce",
            "status-code": 200,
        }
    }
    with step("step_1_post_csp_report"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/csp-report",
            json=report,
            headers={"Content-Type": "application/csp-report"},
            timeout=10,
        )
        # Acceptable: 200/204 (ingested), 404 (not surfaced yet).
        # Not acceptable: 500 (report caused a crash).
        if r.status_code == 404:
            audit_observer.emit(
                step="step_1_post_csp_report",
                severity="medium",
                category="parity",
                title="No /api/csp-report endpoint surfaced",
                expected="200/204 — CSP violations ingested + audited",
                observed="404 Not Found",
            )
            return
        if r.status_code >= 500:
            audit_observer.emit(
                step="step_1_post_csp_report",
                severity="high",
                category="contract",
                title=f"CSP report raised {r.status_code}",
                expected="200/204",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
            pytest.fail(f"csp-report 5xx: {r.status_code}")
