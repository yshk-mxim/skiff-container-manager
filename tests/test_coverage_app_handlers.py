# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Coverage for the exception-handler shims in skiff/app.py.

The handlers are security-surface contracts: every SIEM / retry client
keyed on `detail.code` (see `docs/errors.md`) depends on the envelope
shape being consistent across 4xx / 5xx. These tests exercise the
handler functions directly to verify the documented shape — without
spinning up a live rate limiter or crafting a real RequestValidationError.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from skiff.app import (
    _not_found_envelope,
    _rate_limit_envelope_handler,
    _request_validation_envelope_handler,
)


def _run(coro):
    """Run a coroutine to completion for sync unit tests."""
    return asyncio.run(coro)


def _fake_request() -> MagicMock:
    req = MagicMock()
    req.state = MagicMock(spec=[])  # no view_rate_limit attribute
    req.app.state.limiter = MagicMock()
    return req


@pytest.mark.unit
def test_rate_limit_handler_envelope_with_limit_spec():
    """A slowapi-shaped exception must surface the documented envelope,
    with the human-readable limit spec in `message`."""

    class FakeInnerLimit:
        def __str__(self) -> str:
            return "20 per 1 minute"

    class FakeLimit:
        limit = FakeInnerLimit()

    exc = MagicMock()
    exc.limit = FakeLimit()
    req = _fake_request()

    resp = _rate_limit_envelope_handler(req, exc)
    assert resp.status_code == 429
    body = resp.body.decode()
    assert "auth.rate_limited" in body
    assert "20 per 1 minute" in body


@pytest.mark.unit
def test_rate_limit_handler_envelope_without_limit_spec():
    """No accessible limit spec → generic message, envelope still valid."""
    exc = MagicMock()
    exc.limit = None
    req = _fake_request()

    resp = _rate_limit_envelope_handler(req, exc)
    assert resp.status_code == 429
    body = resp.body.decode()
    assert "auth.rate_limited" in body
    assert "too many requests" in body


@pytest.mark.unit
def test_validation_handler_defensive_fallback_on_wrong_exc_type():
    """If something other than RequestValidationError is dispatched here,
    the handler must still return the documented 422 envelope instead of
    propagating or crashing."""
    req = _fake_request()
    resp = _request_validation_envelope_handler(req, ValueError("not a validation error"))
    assert resp.status_code == 422
    body = resp.body.decode()
    assert "validation.bad_input" in body
    assert "invalid input" in body


@pytest.mark.unit
def test_not_found_envelope_405_method_not_allowed():
    """A bare-string 405 from Starlette must be reshaped into the
    documented envelope (code=system.method_not_allowed)."""
    req = _fake_request()
    exc = StarletteHTTPException(status_code=405, detail="Method Not Allowed")
    resp = _run(_not_found_envelope(req, exc))
    assert resp.status_code == 405
    body = resp.body.decode()
    assert "system.method_not_allowed" in body


@pytest.mark.unit
def test_not_found_envelope_passthrough_on_dict_detail():
    """If our own code raised `http_error(...)` with a dict `detail`, the
    envelope must flow through UNCHANGED — no re-wrapping, no code loss."""
    req = _fake_request()
    exc = HTTPException(status_code=409, detail={"code": "resource.conflict", "message": "busy"})
    resp = _run(_not_found_envelope(req, exc))
    assert resp.status_code == 409
    body = resp.body.decode()
    assert "resource.conflict" in body
    assert "busy" in body


@pytest.mark.unit
def test_not_found_envelope_unmapped_status_passthrough():
    """HTTPException with a bare-string detail at an unmapped status
    (e.g. 503) must fall through the bare_map and serialise the original
    detail untouched — don't invent a code for statuses we haven't
    catalogued."""
    req = _fake_request()
    exc = StarletteHTTPException(status_code=503, detail="maintenance window")
    resp = _run(_not_found_envelope(req, exc))
    assert resp.status_code == 503
    body = resp.body.decode()
    assert "maintenance window" in body
    # No invented code sneaks in for unmapped statuses.
    assert "system.method_not_allowed" not in body
    assert "system.route_not_found" not in body
