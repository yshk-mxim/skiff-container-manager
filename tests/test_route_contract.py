# SPDX-License-Identifier: MIT
"""Route-contract invariants.

Walks the live `app.routes` and asserts the router-wide discipline:

  1. Every route has an explicit `tags=[...]` for OpenAPI grouping.
  2. Every `/api/*` route either has AUTH in its dependencies OR is on an
     explicit public allowlist (health probes, docs landing, setup).
  3. Every mutating `/api/*` route (POST/PUT/PATCH/DELETE) pulls in the
     CSRF check — either via the router-level AUTH dependency list, via
     `@secure_route.mutate`, or explicitly in the handler body.

Fails loudly when a new route is added that forgets one of these — the
goal is that the "oh I forgot @Depends(...)" bug is impossible to merge.

The allowlists below must be updated *deliberately* when a new public
or mutating-but-read-shaped endpoint is introduced.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from skiff.app import app
from skiff.auth import verify_auth, verify_auth_strict, verify_csrf
from skiff.routing_utils import iter_leaf_routes

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Allowlist: routes that are intentionally unauthenticated.
# Adding to this list is a security decision — each entry should be
# justified inline.
# ─────────────────────────────────────────────────────────────────────────────
_PUBLIC_ROUTES: frozenset[str] = frozenset(
    {
        "/health",
        "/ready",
        "/api/auth-required",  # probe used by the login page — returns a boolean
        "/api/setup",  # first-boot bootstrap — rate-limited + per-IP lockout
        "/api/setup-state",  # setup wizard state probe (no secrets returned)
        "/api/setup/probe-docker",  # wizard: detect local Docker — safe, read-only
        "/api/setup/tunnel",  # wizard: establish/teardown SSH tunnel; gated by
        # setup-window + per-IP lockout + token in body
        "/api/docs",  # CSP-safe OpenAPI landing — no data, just links
        # Public-by-design: browser iframe navigation cannot carry the
        # Bearer token from sessionStorage; the route serves only
        # boilerplate HTML. The real auth gate is `/ws/exec/{id}`
        # (AUTH frame). `frame-ancestors 'self'` + X-Frame-Options:
        # SAMEORIGIN keep cross-origin embedders out.
        "/api/terminal-frame/{container_id}",
        "/docs",  # FastAPI default (left enabled for non-CSP paths)
        "/openapi.json",
        "/",  # SPA shell
        "/{path:path}",  # catch-all SPA route
    }
)


# Routes whose mutating HTTP method is allowed without CSRF. Today this is
# empty — every mutating route enforces CSRF via secure_route.mutate or
# direct verify_csrf. Kept as a mechanism for future exceptions that must
# be reviewed explicitly.
_CSRF_EXEMPT_ROUTES: frozenset[str] = frozenset()


# Mutating methods that must carry CSRF protection when authenticated.
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _api_routes() -> list[APIRoute]:
    return [r for r in iter_leaf_routes(app.routes) if isinstance(r, APIRoute)]


def _ws_routes() -> list[WebSocketRoute]:
    return [r for r in iter_leaf_routes(app.routes) if isinstance(r, WebSocketRoute)]


def _route_has_auth_dep(route: APIRoute) -> bool:
    """Check whether the route (or its router) pulls in verify_auth / verify_auth_strict."""
    for dep in route.dependant.dependencies:
        call = dep.call
        if call in (verify_auth, verify_auth_strict):
            return True
    return False


def _route_has_csrf_dep(route: APIRoute) -> bool:
    """Check whether verify_csrf is referenced by the handler or a dependency."""
    for dep in route.dependant.dependencies:
        if dep.call is verify_csrf:
            return True
    # secure_route.mutate stamps `_skiff_secure = {csrf: True, ...}` on the
    # wrapped handler so this check doesn't rely on source introspection.
    marker = getattr(route.endpoint, "_skiff_secure", None)
    if marker and marker.get("csrf"):
        return True
    # Fallback: handler body calls verify_csrf(...) inline.
    try:
        import inspect

        if "verify_csrf" in inspect.getsource(route.endpoint):
            return True
    except (OSError, TypeError):
        pass
    return False


class TestRouteTags:
    """Every route should declare OpenAPI tags."""

    def test_every_api_route_has_tags(self) -> None:
        missing = [r.path for r in _api_routes() if r.path.startswith("/api/") and not (r.tags and len(r.tags) > 0)]
        assert not missing, "Routes missing OpenAPI tags — add tags=[...] in the @router decorator:\n  " + "\n  ".join(
            missing
        )


class TestRouteAuth:
    """Every /api/* route must be authenticated unless explicitly allowlisted."""

    def test_api_routes_require_auth_or_are_public(self) -> None:
        violations: list[str] = []
        for route in _api_routes():
            if not route.path.startswith("/api/"):
                continue
            if route.path in _PUBLIC_ROUTES:
                continue
            if not _route_has_auth_dep(route):
                violations.append(f"{route.path} [{','.join(sorted(route.methods or []))}]")
        assert not violations, "/api/* routes without AUTH that are not on the public allowlist:\n  " + "\n  ".join(
            violations
        )

    def test_public_allowlist_still_exists(self) -> None:
        """Guard against accidentally emptying the allowlist — we do expect some public routes."""
        seen_public = {r.path for r in _api_routes() if r.path in _PUBLIC_ROUTES}
        # At minimum, /health should be reachable without auth
        assert "/health" in seen_public


class TestMutatingCsrf:
    """Every mutating /api/* route must pull in CSRF protection."""

    def test_mutating_routes_have_csrf(self) -> None:
        violations: list[str] = []
        for route in _api_routes():
            if not route.path.startswith("/api/"):
                continue
            if route.path in _CSRF_EXEMPT_ROUTES:
                continue
            methods = {m.upper() for m in (route.methods or set())}
            if not (methods & _MUTATING_METHODS):
                continue
            if not _route_has_csrf_dep(route):
                violations.append(f"{route.path} [{','.join(sorted(methods & _MUTATING_METHODS))}]")
        assert not violations, (
            "Mutating /api/* routes without CSRF protection:\n  "
            + "\n  ".join(violations)
            + "\n\nFix: add Depends(verify_csrf) to router dependencies, "
            "switch to @secure_route.mutate, or explicitly exempt via "
            "_CSRF_EXEMPT_ROUTES with justification."
        )


class TestWebSocketRoutes:
    """WebSocket routes need their own auth path (first-message AUTH)."""

    def test_websocket_routes_exist(self) -> None:
        """Sanity check — there should be at least one WS route (logs + exec)."""
        assert len(_ws_routes()) >= 2, f"Expected ≥2 WebSocket routes (logs, exec); found {len(_ws_routes())}."


class TestCatchAllAllowlist:
    """The PUBLIC allowlist must not be silently broadening the attack surface."""

    def test_no_mutating_route_on_public_allowlist(self) -> None:
        """A route can be public (no auth) AND mutating only if it has either
        CSRF protection or is on _CSRF_EXEMPT_ROUTES (explicit opt-out with
        review). Otherwise it's almost certainly a security bug — a cross-
        origin POST could trigger state changes without any auth gate."""
        violations: list[str] = []
        for route in _api_routes():
            if route.path not in _PUBLIC_ROUTES:
                continue
            methods = {m.upper() for m in (route.methods or set())}
            mutating = methods & _MUTATING_METHODS
            if not mutating:
                continue
            if route.path in _CSRF_EXEMPT_ROUTES:
                continue  # explicit opt-out
            if _route_has_csrf_dep(route):
                continue  # CSRF enforced — safe
            violations.append(f"{route.path} [{','.join(sorted(mutating))}]")
        assert not violations, (
            "Public-allowlisted routes carrying mutating methods without "
            "CSRF protection (and not explicitly exempted):\n  " + "\n  ".join(violations)
        )
