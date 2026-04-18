# SPDX-License-Identifier: MIT
"""Tests for the `skiff.contract` package.

Three concerns:
  1. Catalogues are internally consistent (http_error produces expected
     shapes; events/metrics have sane fields).
  2. Every audit event actually emitted by router code is declared in
     `skiff.contract.events` — drift alert.
  3. Pydantic envelope models serialise/validate as expected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from skiff.contract.errors import http_error, known_codes
from skiff.contract.errors import spec_for as error_spec
from skiff.contract.events import known_events, required_fields
from skiff.contract.events import spec_for as event_spec
from skiff.contract.metrics import known_metrics
from skiff.contract.metrics import spec_for as metric_spec
from skiff.contract.responses import ErrorResponse, OkResponse, UndoableResponse

# ─────────────────────────────────────────────────────────────────────────────
# Response envelopes
# ─────────────────────────────────────────────────────────────────────────────


class TestResponses:
    def test_ok_response_default(self) -> None:
        r = OkResponse()
        assert r.ok is True
        assert r.model_dump(exclude_none=True) == {"ok": True}

    def test_ok_response_with_id(self) -> None:
        r = OkResponse(id="abc123", name="foo")
        assert r.model_dump(exclude_none=True) == {"ok": True, "id": "abc123", "name": "foo"}

    def test_ok_response_ok_is_literal_true(self) -> None:
        with pytest.raises(ValidationError):
            OkResponse(ok=False)  # type: ignore[arg-type]

    def test_ok_response_rejects_extra(self) -> None:
        with pytest.raises(ValidationError):
            OkResponse(unknown_field="x")  # type: ignore[call-arg]

    def test_undoable_response(self) -> None:
        r = UndoableResponse(undo_token="xyz", expires_in=5.0)
        d = r.model_dump()
        assert d == {"ok": True, "undo_token": "xyz", "expires_in": 5.0}

    def test_undoable_response_requires_fields(self) -> None:
        with pytest.raises(ValidationError):
            UndoableResponse()  # type: ignore[call-arg]

    def test_error_response_wraps_detail(self) -> None:
        r = ErrorResponse(detail={"code": "x.y", "message": "oops"})
        assert r.detail["code"] == "x.y"


# ─────────────────────────────────────────────────────────────────────────────
# Error catalogue
# ─────────────────────────────────────────────────────────────────────────────


class TestErrors:
    def test_known_codes_non_empty(self) -> None:
        assert len(known_codes()) > 10

    def test_http_error_produces_catalogue_status(self) -> None:
        exc = http_error("container.not_found", id="abc")
        assert exc.status_code == 404
        assert exc.detail["code"] == "container.not_found"
        assert "abc" in exc.detail["message"]

    def test_http_error_unknown_code_falls_through_500(self) -> None:
        exc = http_error("totally.bogus")
        assert exc.status_code == 500
        assert exc.detail["code"] == "internal.unknown_error_code"

    def test_all_codes_are_dotted(self) -> None:
        for code in known_codes():
            assert "." in code, f"error code {code!r} missing domain.<short> form"
            domain, _, short = code.partition(".")
            assert domain and short, f"bad code shape: {code}"

    def test_all_codes_have_4xx_or_5xx_status(self) -> None:
        for code in known_codes():
            spec = error_spec(code)
            assert spec is not None
            assert 400 <= spec.status < 600, f"{code} has non-4xx/5xx status {spec.status}"

    def test_error_message_placeholders_fill_correctly(self) -> None:
        # A sampling of templates from the catalogue
        exc = http_error("image.registry_blocked", registry="evil.com")
        assert "evil.com" in exc.detail["message"]
        exc = http_error("container.limit_reached", limit=50)
        assert "50" in exc.detail["message"]


# ─────────────────────────────────────────────────────────────────────────────
# Audit event catalogue drift test
# ─────────────────────────────────────────────────────────────────────────────

# Regex matches any `log.info("<name>", ...)`, `log.warning("<name>", ...)`,
# `log.error("<name>", ...)` call in skiff source. Names must be dotted
# lowercase to count.
# Matches `<anything>.info("name", ...)` / `.warning(...)` / `.error(...)`.
# Covers `log.info("x")`, `structlog.get_logger(__name__).info("x")`,
# and other local logger variants.
_LOG_CALL_RE = re.compile(r'\.(?:info|warning|error)\(\s*"([a-z][a-z0-9_.]+)"')

# Also pick up `@secure_route.mutate(..., audit="<name>")` — secure_route
# emits the audit event automatically, so the drift check must know about it.
_SECURE_AUDIT_RE = re.compile(r'audit\s*=\s*"([a-z][a-z0-9_.]+)"')


def _emitted_event_names() -> set[str]:
    """Scan skiff/ for every log.*("name") literal and secure_route(audit="...")
    declaration. No AST — fast + robust."""
    names: set[str] = set()
    skiff_dir = Path(__file__).resolve().parent.parent / "skiff"
    for py_file in skiff_dir.rglob("*.py"):
        # Skip the contract package itself (it declares events; doesn't emit)
        if "contract" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8")
        names.update(_LOG_CALL_RE.findall(text))
        names.update(_SECURE_AUDIT_RE.findall(text))
    return names


class TestEventCatalogue:
    def test_known_events_non_empty(self) -> None:
        assert len(known_events()) > 20

    def test_every_emitted_event_is_declared(self) -> None:
        """Drift guard: every `log.*("name", ...)` in skiff source must appear
        in the declared catalogue. When this fails, either:
          - declare the new event in skiff/contract/events.py, or
          - rename the log call to match an existing declaration.
        """
        emitted = _emitted_event_names()
        declared = known_events()
        undeclared = emitted - declared
        assert not undeclared, (
            "Audit events emitted in code but not declared in "
            "skiff/contract/events.py:\n  " + "\n  ".join(sorted(undeclared))
        )

    def test_no_catalogued_event_is_unused(self) -> None:
        """Reverse drift: the catalogue shouldn't contain dead entries.

        Some entries are legitimately unused today (reserved for a route
        that's about to land, or shared names emitted via helpers not yet
        written). Track those in an allowlist so removal stays deliberate.
        """
        emitted = _emitted_event_names()
        declared = known_events()
        reserved: frozenset[str] = frozenset(
            {
                # Emitted via `getattr(log, level_name)(event, ...)` in the
                # middleware — the AST scanner in this test only picks up
                # literal `log.info(...)` / `log.warning(...)` call sites.
                "audit.api_access",
                # Emitted as a secondary `event_type` on `audit.api_access`
                # lines (from the middleware classifier) rather than as the
                # top-level structlog event name. Catalogued so SIEM rules
                # have an authoritative reference.
                "api.request",
                "rate_limit.exceeded",
                "auth.denied",
                "auth.reviewer_denied",
                "image.list",
                "audit.log_read",
                "container.logs_stream",
                "container.exec_session",
            }
        )
        unused = declared - emitted - reserved
        assert not unused, "Catalogue declares events never emitted:\n  " + "\n  ".join(sorted(unused))

    def test_all_event_names_are_dotted(self) -> None:
        for name in known_events():
            assert "." in name, f"event {name!r} missing domain.<verb> form"

    def test_required_fields_returns_frozenset(self) -> None:
        # Sanity: for a well-known event with required fields
        req = required_fields("container.created")
        assert "id" in req
        assert "name" in req

    def test_required_fields_for_unknown_event(self) -> None:
        assert required_fields("no.such.event") == frozenset()

    def test_all_severities_recognised(self) -> None:
        for name in known_events():
            spec = event_spec(name)
            assert spec is not None
            assert spec.severity in {"info", "warning", "error"}


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_known_metrics_non_empty(self) -> None:
        assert len(known_metrics()) >= 4

    def test_metric_names_start_with_skiff(self) -> None:
        for name in known_metrics():
            assert name.startswith("skiff_"), f"metric {name!r} not prefixed with skiff_"

    def test_metric_kinds_are_valid(self) -> None:
        for name in known_metrics():
            spec = metric_spec(name)
            assert spec is not None
            assert spec.kind in {"counter", "gauge", "histogram"}
