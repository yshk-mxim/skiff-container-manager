# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""`secure_route` — one decorator bundles the four things every route needs.

Today a mutating route typically has:

    @router.post("/api/containers/{id}/start", dependencies=AUTH)
    @limiter.limit(_limit(RL_FAST))
    def start(...):
        verify_csrf(request)
        container = _get_container(...)
        container.start()
        log.info("container.started", id=container.short_id)
        return {"ok": True}

The four protection concerns (auth dep, rate-limit, CSRF, audit-log) are
preamble, not business logic. `secure_route` wraps them so the handler is:

    @router.post("/api/containers/{id}/start", dependencies=AUTH)
    @secure_route.mutate(RATE.WRITE, audit="container.started")
    def start(...):
        container = _get_container(...)
        container.start()
        return OkResponse(id=container.short_id)

Three variants:

  .mutate(rate, audit=..., audit_fields=...) — AUTH + rate-limit + CSRF +
         emit `audit` event on success if `audit` is truthy.
  .read(rate)                                — AUTH + rate-limit (no CSRF,
         no audit; read-only by definition).
  .public(rate)                              — rate-limit only. Used for
         /health, /ready, /api/docs.

Because FastAPI relies on the decorator order to compute the OpenAPI
schema AND slowapi needs `request: Request` in the signature to key the
limiter, this decorator wraps the handler in a way that preserves both:
it calls `limiter.limit(rate)(func)` first, then adds CSRF / audit
behaviour as an async wrapper.

Test invariants (enforced by tests/test_route_contract.py in F4):
  - Every `/api/*` mutating route has `.mutate`.
  - Every `/api/*` read route has `.read`.
  - Every route with `.mutate` references `verify_csrf` either directly
    or via this decorator (latter is preferred).
  - `audit` event names passed here must all appear in
    `skiff.contract.events.known_events()`.
"""
from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import Request

from skiff.auth import verify_csrf
from skiff.config import limiter
from skiff.contract.events import known_events

_log = structlog.get_logger(__name__)


def _emit_audit(event_name: str, fields: dict[str, Any]) -> None:
    """Emit an audit log entry, rejecting undeclared events in dev/test.

    We deliberately don't fail the request if the event is undeclared —
    that'd push contract drift into a user-visible error. Instead log a
    loud warning so a test scrub catches it.

    Note: `fields` is passed as a dict (not **kwargs) to avoid colliding
    with handler kwargs that happen to share a name (e.g. "name", "event").
    """
    if event_name and event_name not in known_events():
        _log.warning("audit.undeclared_event", undeclared=event_name)
    _log.info(event_name, **fields)


def _audit_from_response(
    audit: str | None,
    audit_fields_fn: Callable[..., dict[str, Any]] | None,
    *call_args: Any,
    **call_kwargs: Any,
) -> None:
    if not audit:
        return
    fields: dict[str, Any] = {}
    if audit_fields_fn is not None:
        # Intentionally broad: audit field extraction runs user-supplied
        # lambdas registered by every router. A misbehaving lambda must
        # NEVER fail the request — the mutation already succeeded when
        # we reach here. We log with the audit name so the offender is
        # easy to find.
        try:
            fields = audit_fields_fn(*call_args, **call_kwargs) or {}
        except Exception as exc:
            _log.warning("audit.field_extraction_failed", name=audit, error=str(exc))
    _emit_audit(audit, fields)


class _SecureRoute:
    """Namespace for the three variants. See module docstring."""

    def _wrap(
        self,
        rate: str,
        *,
        csrf: bool,
        audit: str | None,
        audit_fields: Callable[..., dict[str, Any]] | None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            # slowapi's limiter.limit needs the raw function so it can see its
            # signature (especially `request: Request`). We apply it last so
            # its wrapper is the outermost layer.
            if asyncio.iscoroutinefunction(func):
                @functools.wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    if csrf:
                        req = _find_request(args, kwargs)
                        if req is not None:
                            verify_csrf(req)
                    result = await func(*args, **kwargs)
                    _audit_from_response(audit, audit_fields, *args, **kwargs)
                    return result

                wrapped: Callable[..., Any] = async_wrapper
            else:
                @functools.wraps(func)
                def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                    if csrf:
                        req = _find_request(args, kwargs)
                        if req is not None:
                            verify_csrf(req)
                    result = func(*args, **kwargs)
                    _audit_from_response(audit, audit_fields, *args, **kwargs)
                    return result

                wrapped = sync_wrapper

            # Apply rate-limit as the outermost layer so slowapi keys on the
            # HTTP request before any business logic runs.
            limited = limiter.limit(rate)(wrapped)
            # Propagate the marker so route-contract tests can detect that
            # this handler carries CSRF / audit without introspecting the
            # closure. Written on BOTH layers so whichever the test inspects
            # first has a consistent signal.
            wrapped._skiff_secure = {  # type: ignore[attr-defined]
                "csrf": csrf, "audit": audit, "rate": rate,
            }
            limited._skiff_secure = wrapped._skiff_secure  # type: ignore[attr-defined]
            return limited

        return decorator

    def mutate(
        self,
        rate: str,
        *,
        audit: str | None = None,
        audit_fields: Callable[..., dict[str, Any]] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Bundle AUTH + CSRF + rate-limit + audit for a mutating route.

        Pass `audit` as a declared event name; optional `audit_fields` is a
        callable that receives the same (*args, **kwargs) as the handler
        and returns the structlog fields to emit.
        """
        return self._wrap(rate, csrf=True, audit=audit, audit_fields=audit_fields)

    def read(self, rate: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """AUTH + rate-limit. No CSRF (read-only). No audit by default."""
        return self._wrap(rate, csrf=False, audit=None, audit_fields=None)

    def public(self, rate: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Rate-limit only. For health probes and unauthenticated docs landing."""
        return self._wrap(rate, csrf=False, audit=None, audit_fields=None)


def _find_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    """Locate the FastAPI Request in the handler's call args.

    FastAPI always injects `request: Request` as a named argument when the
    parameter is annotated. We check kwargs first, then positional args.
    """
    req = kwargs.get("request")
    if isinstance(req, Request):
        return req
    for a in args:
        if isinstance(a, Request):
            return a
    return None


# Module-level singleton — `secure_route.mutate(...)`, `secure_route.read(...)`.
secure_route = _SecureRoute()


# Ensure runtime-used symbol is re-exported for type checkers who look at
# imports rather than module attrs.
__all__ = ["secure_route"]


# Keep a reference so `Awaitable` import isn't flagged unused when the file
# compiles in environments that strip type hints.
_: type[Awaitable[Any]] = Awaitable
