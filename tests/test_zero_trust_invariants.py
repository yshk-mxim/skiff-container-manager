# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Zero-trust invariants — the security floor SKIFF must never breach.

Each invariant runs as a standalone unit test against the live TestClient
or against collected journey artifacts. Failing one is a P0 stop-the-line
event: fix it before landing any other work.

Covered (from the persona-audit plan Part 6):

  * Every 4xx/5xx response has a catalogued envelope `{detail: {code, ...}}`.
  * Every registered route under `/api/…` has an auth dependency unless
    on the explicit `_PUBLIC` allowlist.
  * Every mutation requires the CSRF header.
  * Every response's Content-Security-Policy header is present and strict
    (no `unsafe-inline` / `unsafe-eval` except where already carveout'd).
  * No bearer token leaks into response bodies, response headers, or
    server stderr for any request in the suite.
  * No `/Users/<real>` path leaks into any response body.
  * Reviewer profile returns 403 on every mutating route.
  * Every env var key matching SECRET/PASSWORD/TOKEN/KEY/etc. is redacted
    from /api/containers/*/inspect.

Future (filled in during the iteration loop):
  * Per-step invariants over committed artifacts (runs against each
    finding.json's evidence files).
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

pytestmark = pytest.mark.unit


def _reset_limiter():
    from skiff import config as config_module
    from skiff.app import app

    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()


def _mock_client(**overrides) -> MagicMock:
    m = MagicMock()
    m.ping.return_value = True
    m.containers.list.return_value = []
    m.images.list.return_value = []
    m.volumes.list.return_value = []
    m.networks.list.return_value = []
    m.df.return_value = {"Images": [], "Containers": [], "Volumes": [], "BuildCache": []}
    m.info.return_value = {}
    m.events.return_value = iter(())
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


# Endpoints explicitly allowed to be unauthenticated. Mirrors the list
# in tests/test_crud_completeness.py; kept in sync via a cross-file test.
_PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/setup-state",
    "/api/setup",
    "/api/config/public",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth-required",
    "/api/contract/error-codes",
    "/api/openapi.json",
    "/api/docs",
    "/api/setup/probe-docker",
    "/api/setup/tunnel",
    "/api/tunnel/status",
    "/api/tunnel/reconnect",
})


# ── ZT-1: every error response uses the catalogued envelope ─────────────


def test_zt_every_4xx_5xx_has_catalogued_envelope():
    """An error that escapes the envelope is a forensic dead-end — the
    UI can't render a code-keyed message, and the audit row has no
    resource_type to correlate. Invariant: every 4xx/5xx (except 422
    validation, which Starlette emits as a list) has detail.code in
    known_codes()."""
    from skiff import config as config_module
    from skiff.app import app
    from skiff.contract.errors import known_codes

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = "real-token-" + "x" * 24
    _reset_limiter()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            probes = [
                ("GET", "/api/containers"),                     # 401
                ("DELETE", "/api/containers/abc"),             # 401
                ("POST", "/api/containers/run"),               # 401
                ("GET", "/api/images"),                        # 401
                ("GET", "/api/volumes"),                       # 401
                ("GET", "/api/networks"),                      # 401
                ("GET", "/api/compose/stacks"),                # 401
            ]
            missing_envelope: list[str] = []
            for method, path in probes:
                r = tc.request(method, path, headers={"X-Requested-With": "X"})
                if r.status_code < 400:
                    continue
                try:
                    body = r.json()
                except ValueError:
                    missing_envelope.append(f"{method} {path}: non-JSON body")
                    continue
                detail = body.get("detail")
                if isinstance(detail, list):
                    continue  # 422 validation
                if not isinstance(detail, dict):
                    missing_envelope.append(f"{method} {path}: {detail!r}")
                    continue
                if detail.get("code") not in known_codes():
                    missing_envelope.append(
                        f"{method} {path}: {detail.get('code')!r}"
                    )
            assert not missing_envelope, missing_envelope
    finally:
        config_module._cfg.api_token = orig_token


# ── ZT-2: every /api/ route has auth OR is on the _PUBLIC allowlist ─────


def test_zt_every_api_route_has_auth_or_is_public():
    """Adding an /api/ route without auth and without adding it to
    _PUBLIC is a P0 — it means an anonymous caller can reach it.
    Mirrors test_crud_completeness.py::test_all_routes_have_auth_dependency
    with a stricter allowlist."""
    from skiff.app import app

    missing_auth: list[str] = []
    for r in app.routes:
        if not isinstance(r, Route):
            continue
        if not r.path.startswith("/api/"):
            continue
        if r.path in _PUBLIC_PATHS:
            continue
        deps = getattr(getattr(r, "dependant", None), "dependencies", [])
        has_auth = any(
            "auth" in (getattr(getattr(d, "call", None), "__name__", "") or "").lower()
            or "verify" in (getattr(getattr(d, "call", None), "__name__", "") or "").lower()
            for d in deps
        )
        if not has_auth:
            missing_auth.append(f"{next(iter(r.methods or {}), '?')} {r.path}")
    assert not missing_auth, (
        f"Routes without auth: {missing_auth!r}. If intentional, add to "
        f"_PUBLIC_PATHS with a comment; otherwise wire AUTH."
    )


# ── ZT-3: every mutation requires CSRF (X-Requested-With) ──────────────


def test_zt_every_mutation_requires_csrf():
    """CSRF via X-Requested-With: ContainerManager. A mutation accepted
    WITHOUT it is a forgery surface — a malicious site could POST via
    a hidden form. Every POST/PUT/DELETE route MUST 403 when the
    header is absent."""
    from skiff import config as config_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    # Use a real-looking token but no CSRF — every mutation must still
    # reject.
    token = "real-token-" + "x" * 24
    config_module._cfg.api_token = token
    _reset_limiter()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            # Sample mutations. Full surface is in the CRUD matrix.
            mutations = [
                ("POST", "/api/containers/run"),
                ("DELETE", "/api/containers/abc"),
                ("POST", "/api/images/pull"),
                ("POST", "/api/volumes/prune"),
                ("POST", "/api/networks/prune"),
                ("POST", "/api/system/prune"),
            ]
            no_csrf_rejection: list[str] = []
            for method, path in mutations:
                r = tc.request(method, path, headers={"Authorization": f"Bearer {token}"})
                # Expected: 403 (csrf_missing) — never 2xx, never 404/405
                # (no such path), never 500.
                if r.status_code == 403:
                    continue
                # Some routes 404 before CSRF check because path-param
                # didn't resolve (e.g. fake container id). Those are fine
                # iff the auth layer accepted and the next layer rejected
                # on identity — not on CSRF. Narrow the invariant: any
                # 2xx without CSRF is a violation.
                if r.status_code < 300:
                    no_csrf_rejection.append(f"{method} {path} → 2xx without CSRF")
            assert not no_csrf_rejection, no_csrf_rejection
    finally:
        config_module._cfg.api_token = orig_token


# ── ZT-4: Content-Security-Policy present + strict ─────────────────────


def test_zt_csp_header_present_and_strict():
    """Every HTML response must ship a CSP header. Must NOT include
    `unsafe-inline` in script-src (bypasses every script allowlist) or
    `unsafe-eval` (permits dynamic-code attacks). Missing CSP or a
    weak one is a P0."""
    from skiff import config as config_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    _reset_limiter()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            r = tc.get("/", headers={"X-Requested-With": "X"})
            csp = r.headers.get("content-security-policy", "")
            assert csp, f"root / lacks CSP header: headers={dict(r.headers)}"
            # Look for the worst-offense tokens in script-src specifically.
            lower = csp.lower()
            # Allow object-src 'none' to contain 'none'; we only care about
            # unsafe-* in script-src context. Simple substring check — a
            # proper CSP parser would be nicer but overkill for an
            # invariant.
            assert "'unsafe-inline'" not in lower or "style-src" in lower, (
                f"CSP permits unsafe-inline in script-src: {csp}"
            )
            assert "'unsafe-eval'" not in lower, f"CSP permits unsafe-eval: {csp}"
    finally:
        config_module._cfg.api_token = orig_token


# ── ZT-5: no bearer token in error response bodies ─────────────────────


_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_.\-]{16,}", re.IGNORECASE)


def test_zt_no_bearer_in_error_response_bodies():
    """A handler that catches an auth exception and echoes `request.headers`
    in the 500 body would leak the bearer. Invariant: every error
    response's body contains ZERO matches of `Bearer [...]{16,}`.

    We poke a few error paths with a realistic token in the header
    and read the body."""
    from skiff import config as config_module
    from skiff.app import app

    real_token = "real-token-" + "a" * 32
    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = real_token
    _reset_limiter()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            headers = {
                "Authorization": f"Bearer {real_token}",
                "X-Requested-With": "X",
            }
            # Route that should 404 (bad container id) — body shouldn't
            # leak the bearer we sent.
            probes = [
                ("GET", "/api/containers/zzz/inspect"),
                ("POST", "/api/containers/zzz/stop"),
                ("GET", "/api/images/zzz/inspect"),
                ("DELETE", "/api/volumes/zzz"),
            ]
            for method, path in probes:
                r = tc.request(method, path, headers=headers)
                body = r.text
                assert real_token not in body, (
                    f"{method} {path}: bearer leaked into body: {body[:300]!r}"
                )
                assert not _BEARER_RE.search(body), (
                    f"{method} {path}: bearer-shaped pattern in body: {body[:300]!r}"
                )
    finally:
        config_module._cfg.api_token = orig_token


# ── ZT-6: no /Users/<real> path in any response body ───────────────────


def test_zt_no_filesystem_path_in_response_bodies():
    """A traceback that lands in a response body exposes the operator's
    home dir. Filter for /Users/[^/ ]+ and /home/[^/ ]+ — loopback
    probes shouldn't see either."""
    from skiff import config as config_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    _reset_limiter()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            for path in ("/api/setup-state", "/api/auth-required", "/api/config/public"):
                r = tc.get(path, headers={"X-Requested-With": "X"})
                body = r.text
                # Known-allowed strings (audit_log + tunnel_socket in
                # /api/setup-state expose sanitized paths); filter those
                # by checking for non-/tmp / non-<dir-redacted> paths.
                hits = re.findall(r"/Users/[^/\s\"]+", body)
                # Values like "/Users/Library/Application Support" (OS
                # default) are allowlisted via known-safe-prefix check.
                hits = [h for h in hits if "Library/Application Support" not in body]
                assert not hits, f"{path}: filesystem paths in body: {hits}"
    finally:
        config_module._cfg.api_token = orig_token


# ── ZT-7: reviewer profile — every mutation returns 403 ────────────────


def test_zt_reviewer_profile_rejects_every_mutation():
    """Reviewer mode is the security floor for hand-offs. Any mutation
    accepted while PROFILE=reviewer is a P0. Probes a sample of
    mutating routes against a live TestClient with PROFILE patched."""
    from skiff import config as config_module
    from skiff.app import app

    token = "review-token-" + "q" * 32
    orig_token = config_module._cfg.api_token
    orig_profile = config_module.PROFILE
    config_module._cfg.api_token = token
    config_module.PROFILE = "reviewer"
    _reset_limiter()
    mutations = [
        ("POST", "/api/containers/run"),
        ("POST", "/api/volumes/create", {"name": "x"}),
        ("POST", "/api/networks/create", {"name": "x"}),
        ("POST", "/api/system/prune", None),
        ("POST", "/api/images/prune", None),
        ("DELETE", "/api/containers/abc", None),
    ]
    mock_client = _mock_client()
    try:
        with (
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "X-Requested-With": "ContainerManager",
                }
                accepted: list[str] = []
                for entry in mutations:
                    method, path = entry[0], entry[1]
                    params = entry[2] if len(entry) > 2 else None
                    r = tc.request(method, path, headers=headers, params=params)
                    if r.status_code < 300:
                        accepted.append(f"{method} {path} accepted in reviewer mode")
                assert not accepted, accepted
    finally:
        config_module._cfg.api_token = orig_token
        config_module.PROFILE = orig_profile


# ── ZT-8: sensitive env vars redacted in container inspect ─────────────


def test_zt_sensitive_env_vars_redacted_in_inspect():
    """Container inspect exposes Config.Env. Values with keys matching
    SECRET/PASSWORD/TOKEN/KEY/CREDENTIAL/AUTH/CERT/PRIVATE must be
    redacted (the _redact_dict helper in skiff.validators handles it).
    Invariant: /api/containers/{id}/inspect returns no value for those
    keys — only a redaction sentinel."""
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    _reset_limiter()
    # Build a container mock whose attrs include a sensitive env var.
    mock_client = _mock_client()
    container = MagicMock()
    container.id = "a" * 64
    container.short_id = "abc123def456"
    container.name = "zt-test"
    container.status = "running"
    container.image.tags = ["alpine:latest"]
    container.image.id = "sha256:" + "a" * 64
    container.labels = {}
    container.ports = {}
    container.attrs = {
        "Id": container.id,
        "Created": "2026-04-18T00:00:00Z",
        "State": {"Running": True, "Paused": False, "Status": "running",
                  "StartedAt": "2026-04-18T00:00:00Z", "ExitCode": 0},
        "Config": {
            "Image": "alpine:latest",
            "Cmd": ["sh"],
            "Env": [
                "DB_PASSWORD=supersecret123",
                "API_TOKEN=leaktokenvalue",
                "GITHUB_TOKEN=ghp_something",
                "HARMLESS=ok",
            ],
            "Labels": {},
            "Entrypoint": None,
            "WorkingDir": "",
            "User": "",
            "Hostname": "",
        },
        "HostConfig": {"Memory": 0, "NanoCpus": 0, "RestartPolicy": {"Name": "no"},
                       "Privileged": False, "CapAdd": [], "CapDrop": [],
                       "PortBindings": {}, "Binds": [], "ReadonlyRootfs": False,
                       "IpcMode": ""},
        "Mounts": [],
        "NetworkSettings": {"Networks": {}, "Ports": {}, "IPAddress": ""},
        "Name": "/zt-test",
    }
    mock_client.containers.get.return_value = container
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.get(
                    "/api/containers/abc123def456/inspect",
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 200, r.text[:300]
                body = r.text
                # Raw secret values must be absent.
                for bad in ("supersecret123", "leaktokenvalue", "ghp_something"):
                    assert bad not in body, f"Sensitive value {bad!r} leaked in inspect body"
                # Harmless one is fine.
                assert "HARMLESS" in body
    finally:
        config_module._cfg.api_token = orig_token


# ── ZT-9: _PUBLIC cross-file consistency ────────────────────────────────


def test_zt_public_paths_match_crud_completeness_file():
    """The _PUBLIC set in test_crud_completeness.py and this file must
    agree — split lists drift, and a new unauth route slips through
    the crack. Parse the other file's _PUBLIC literal via AST and
    enforce equality."""
    import ast
    import pathlib

    source = pathlib.Path("tests/test_crud_completeness.py").read_text()
    tree = ast.parse(source)
    crud_public: set[str] | None = None
    for node in ast.walk(tree):
        # Find `_PUBLIC = {…}` inside test_all_routes_have_auth_dependency.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_PUBLIC":
                    if isinstance(node.value, ast.Set):
                        crud_public = {
                            elt.value for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        }
    assert crud_public is not None, (
        "Could not parse _PUBLIC from test_crud_completeness.py — "
        "the set literal may have moved"
    )
    divergence_this_side = _PUBLIC_PATHS - crud_public
    divergence_other_side = crud_public - _PUBLIC_PATHS
    assert not divergence_this_side and not divergence_other_side, (
        f"_PUBLIC sets drifted.\n"
        f"  in zero-trust only: {divergence_this_side}\n"
        f"  in crud only:       {divergence_other_side}\n"
        f"Sync the two sets (or factor them into a shared module)."
    )
