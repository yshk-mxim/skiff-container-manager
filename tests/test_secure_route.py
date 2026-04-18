# SPDX-License-Identifier: MIT
"""Tests for `skiff.secure.secure_route` and `skiff.rate.RATE`.

Covers:
  - Rate tiers return scaled strings of the form "N/minute".
  - `.mutate` enforces CSRF on POST/DELETE — raises 403 when header absent.
  - `.mutate` emits the declared audit event on success.
  - `.mutate` warns when handed an undeclared event name (drift alert).
  - `.read` does NOT enforce CSRF.
  - `.public` does NOT enforce CSRF or AUTH (just rate-limit).
  - Async and sync handlers both supported.

Tests construct a minimal FastAPI app with the limiter state attached so
slowapi doesn't complain.
"""
from __future__ import annotations

from typing import Any

import pytest
import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from skiff.config import limiter
from skiff.rate import RATE
from skiff.secure import secure_route


@pytest.fixture
def log_capture():
    """Capture structlog events without touching the global config."""
    cap = structlog.testing.LogCapture()
    original_processors = structlog.get_config()["processors"]
    structlog.configure(processors=[cap])
    try:
        yield cap
    finally:
        structlog.configure(processors=original_processors)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    def _rl_handler(req: Any, exc: Any) -> None:
        raise HTTPException(429, "rate limited")
    app.add_exception_handler(RateLimitExceeded, _rl_handler)
    return app


class TestRateTiers:
    def test_all_tiers_have_minute_suffix(self) -> None:
        for tier in ("AUTH_SENSITIVE", "WRITE", "READ", "PUBLIC", "BURST"):
            value = getattr(RATE, tier)
            assert "/" in value, f"{tier}: {value!r}"
            count, _, period = value.partition("/")
            assert count.isdigit()
            assert period in {"minute", "second", "hour"}

    def test_tiers_are_ordered_sensibly(self) -> None:
        # Parse count out of each tier and verify the relative order.
        def _count(tier_value: str) -> int:
            return int(tier_value.split("/", maxsplit=1)[0])

        assert _count(RATE.AUTH_SENSITIVE) <= _count(RATE.WRITE)
        assert _count(RATE.WRITE) <= _count(RATE.READ)
        assert _count(RATE.READ) <= _count(RATE.PUBLIC)


class TestSecureRouteMutate:
    def test_mutate_requires_csrf_header(self) -> None:
        app = _make_app()
        r = APIRouter()

        @r.post("/things")
        @secure_route.mutate(RATE.WRITE)
        def create(request: Request) -> dict[str, Any]:
            return {"ok": True}

        app.include_router(r)
        client = TestClient(app)

        # No X-Requested-With — must 403
        resp = client.post("/things")
        assert resp.status_code == 403

        # With header — succeeds
        resp = client.post("/things", headers={"X-Requested-With": "ContainerManager"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_mutate_emits_audit_event_on_success(self, log_capture) -> None:
        app = _make_app()
        r = APIRouter()

        def _fields(request: Request, **kw: Any) -> dict[str, Any]:
            return {"id": "abc123"}

        @r.post("/things")
        @secure_route.mutate(RATE.WRITE, audit="container.started",
                             audit_fields=_fields)
        def create(request: Request) -> dict[str, Any]:
            return {"ok": True}

        app.include_router(r)
        client = TestClient(app)
        resp = client.post("/things", headers={"X-Requested-With": "ContainerManager"})
        assert resp.status_code == 200
        events = [e for e in log_capture.entries if e["event"] == "container.started"]
        assert len(events) == 1
        assert events[0].get("id") == "abc123"

    def test_mutate_warns_on_undeclared_event(self, log_capture) -> None:
        app = _make_app()
        r = APIRouter()

        @r.post("/things")
        @secure_route.mutate(RATE.WRITE, audit="totally.fake_event")
        def create(request: Request) -> dict[str, Any]:
            return {"ok": True}

        app.include_router(r)
        client = TestClient(app)
        resp = client.post("/things", headers={"X-Requested-With": "ContainerManager"})
        assert resp.status_code == 200
        drift_events = [e for e in log_capture.entries
                        if e["event"] == "audit.undeclared_event"]
        assert len(drift_events) == 1
        assert drift_events[0].get("undeclared") == "totally.fake_event"


class TestSecureRouteRead:
    def test_read_does_not_require_csrf(self) -> None:
        app = _make_app()
        r = APIRouter()

        @r.get("/things")
        @secure_route.read(RATE.READ)
        def list_things(request: Request) -> list[dict]:
            return [{"id": "a"}]

        app.include_router(r)
        client = TestClient(app)
        # No CSRF header, and GET shouldn't require it anyway — must 200
        resp = client.get("/things")
        assert resp.status_code == 200


class TestSecureRoutePublic:
    def test_public_does_not_require_csrf(self) -> None:
        app = _make_app()
        r = APIRouter()

        @r.get("/public")
        @secure_route.public(RATE.PUBLIC)
        def probe(request: Request) -> dict[str, Any]:
            return {"ok": True}

        app.include_router(r)
        client = TestClient(app)
        resp = client.get("/public")
        assert resp.status_code == 200


class TestSecureRouteAsync:
    def test_async_handler_supported(self) -> None:
        app = _make_app()
        r = APIRouter()

        @r.post("/async-thing")
        @secure_route.mutate(RATE.WRITE)
        async def create(request: Request) -> dict[str, Any]:
            return {"ok": True, "async": True}

        app.include_router(r)
        client = TestClient(app)
        resp = client.post("/async-thing", headers={"X-Requested-With": "ContainerManager"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "async": True}


class TestAuditFieldsCallableRobust:
    def test_audit_fields_raising_doesnt_break_handler(self, log_capture) -> None:
        app = _make_app()
        r = APIRouter()

        def _bad_fields(**kw: Any) -> dict[str, Any]:
            raise RuntimeError("introspection failed")

        @r.post("/things")
        @secure_route.mutate(RATE.WRITE, audit="container.started",
                             audit_fields=_bad_fields)
        def create(request: Request) -> dict[str, Any]:
            return {"ok": True}

        app.include_router(r)
        client = TestClient(app)
        resp = client.post("/things", headers={"X-Requested-With": "ContainerManager"})
        # Handler still returns success; audit extraction failure logged as warning
        assert resp.status_code == 200
        names = {e["event"] for e in log_capture.entries}
        assert "audit.field_extraction_failed" in names
        assert "container.started" in names
