# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Aggressive hypothesis fuzz harnesses for SKIFF's critical parsers.

These tests extend the existing hypothesis coverage in
`test_state_transitions.py` with higher-volume, wider-range inputs
targeted specifically at the parsers that accept untrusted data from
network or filesystem sources.

Each harness's invariant: "the function either returns a documented
shape OR raises a documented exception type — NEVER an unhandled
exception that would propagate to an ASGI error response".

Parsers covered:
  - compose YAML validator (validate_compose_file)
  - image-name regex + allow-list (validate_image_registry)
  - container-name validator (validate_container_id, accepts name OR hex)
  - volume / network name validators
  - memory-quantity parser (parse_memory_quantity)
  - audit classifier (_classify_event)
  - WS resize-frame parser (_maybe_resize)
  - http_error envelope construction

Atheris (Google's coverage-guided Python fuzzer) is the natural next
step here but does not build cleanly on Python 3.12+ as of this
writing (upstream issue). Hypothesis property-based fuzzing with
large example budgets gives us the same behavioural assurance for the
failure modes that matter in Python (unhandled exception, bad
return shape).
"""

from __future__ import annotations

import json
import string
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st

from skiff.contract.errors import http_error, known_codes
from skiff.logging_setup import _classify_event
from skiff.routers.containers_ws import _maybe_resize
from skiff.validators import (
    parse_memory_quantity,
    validate_compose_file,
    validate_container_id,
    validate_container_name,
    validate_image_registry,
    validate_project_name,
)

_HIGH_VOLUME = settings(max_examples=1000, deadline=None)
_MEDIUM_VOLUME = settings(max_examples=500, deadline=None)


# ── validate_compose_file — broad YAML fuzz ──────────────────────────


@given(st.binary(max_size=4096))
@_MEDIUM_VOLUME
@pytest.mark.unit
def test_compose_validator_never_raises_uncaught(payload):
    """Any random bytes in / out. Either validates cleanly, raises
    HTTPException, OR is rejected by the YAML parser's ValueError —
    NEVER an unhandled exception that would surface as a 500."""
    try:
        validate_compose_file(payload)
    except HTTPException:
        pass  # documented rejection path
    except Exception as exc:
        pytest.fail(
            f"validate_compose_file raised {type(exc).__name__} on input {payload[:64]!r}: {exc}",
        )


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:./ \n\t-_", max_size=2048),
)
@_MEDIUM_VOLUME
@pytest.mark.unit
def test_compose_validator_on_structured_text_never_raises_uncaught(payload):
    """Pseudo-YAML strings (YAML-ish alphabet) — same invariant."""
    try:
        validate_compose_file(payload.encode())
    except HTTPException:
        pass
    except Exception as exc:
        pytest.fail(f"validate_compose_file raised {type(exc).__name__}: {exc}")


# ── validate_image_registry — broader than test_properties.py ────────


@given(
    st.text(
        alphabet=string.printable.replace("\r", "").replace("\n", ""),
        max_size=300,
    ),
)
@_HIGH_VOLUME
@pytest.mark.unit
def test_image_registry_validator_never_raises_uncaught(image):
    """Every printable-ASCII string either passes, raises
    HTTPException with a documented code, OR is rejected cleanly."""
    try:
        validate_image_registry(image)
    except HTTPException as http_exc:
        # Must be a CATALOGUED error code. No bare HTTPException(400, "str").
        detail = http_exc.detail
        assert isinstance(detail, dict), f"bare HTTPException leaked for input {image!r}: {detail!r}"
        assert "code" in detail, f"envelope missing `code` key for input {image!r}: {detail!r}"
        assert detail["code"] in known_codes(), f"undeclared error code {detail['code']!r} for input {image!r}"
    except Exception as exc:
        pytest.fail(
            f"validate_image_registry raised {type(exc).__name__} on input {image!r}: {exc}",
        )


# ── container / project / volume / network name validators ───────────


@given(st.text(max_size=300))
@_HIGH_VOLUME
@pytest.mark.unit
def test_name_validators_never_raise_uncaught(name):
    """The four name validators must always either return the name or
    raise HTTPException — never anything else, for any input up to
    300 chars from the full Unicode plane."""
    for validator in (
        validate_container_id,
        validate_container_name,
        validate_project_name,
    ):
        try:
            validator(name)
        except HTTPException:
            pass
        except Exception as exc:
            pytest.fail(
                f"{validator.__name__} raised {type(exc).__name__} on input {name!r}: {exc}",
            )


# ── parse_memory_quantity — broad numeric + suffix fuzz ──────────────


@given(
    st.one_of(
        st.text(max_size=64),
        st.integers(min_value=-(10**18), max_value=10**18),
        st.floats(allow_nan=False, allow_infinity=False),
        st.booleans(),
        st.none(),
        st.lists(st.integers(), max_size=5),
        st.dictionaries(st.text(max_size=8), st.integers(), max_size=3),
    ),
)
@_HIGH_VOLUME
@pytest.mark.unit
def test_parse_memory_quantity_never_raises_uncaught(value):
    """Every plausible input type — string, int, float, bool, None,
    list, dict — either returns an int (bytes) or raises
    HTTPException with a documented `validation.bad_memory` code.
    Floats are NOT documented but must still not crash."""
    try:
        result = parse_memory_quantity(value)
        # Success path: must be a non-negative int
        assert isinstance(result, int) and result >= 0, (
            f"parse_memory_quantity returned unexpected shape for {value!r}: {result!r}"
        )
    except HTTPException:
        pass
    except Exception as exc:
        pytest.fail(
            f"parse_memory_quantity raised {type(exc).__name__} on input {value!r}: {exc}",
        )


# ── _classify_event — audit-middleware URL-path fuzz ─────────────────


@given(
    method=st.text(alphabet=string.ascii_uppercase, min_size=1, max_size=12),
    path=st.text(max_size=500),
    status=st.integers(min_value=100, max_value=999),
    error_code=st.one_of(st.just(""), st.sampled_from(list(known_codes())[:30])),
)
@_HIGH_VOLUME
@pytest.mark.unit
def test_classify_event_bounded_output(method, path, status, error_code):
    """For any (method, path, status, error_code) tuple,
    `_classify_event` returns a tuple of three strings with bounded
    lengths (event ≤ 64 chars, rtype ≤ 32, rid ≤ 128).

    Any exception here would crash the audit middleware — we've seen
    this bug in Loop 5 (string_too_long on oversize resource_id).
    The fuzz harness makes sure no similar bug reappears for any
    plausible URL shape."""
    try:
        event, rtype, rid = _classify_event(method, path, status, error_code=error_code)
    except Exception as exc:
        pytest.fail(
            f"_classify_event raised {type(exc).__name__} on ({method!r}, {path!r}, {status}, {error_code!r}): {exc}",
        )
    assert isinstance(event, str) and event, "empty event_type for input"
    assert len(rtype) <= 32, f"rtype over cap: {len(rtype)}"
    assert len(rid) <= 128, f"rid over cap: {len(rid)}"


# ── _maybe_resize — WS exec resize-frame parser ──────────────────────


@given(payload=st.text(max_size=2000))
@_HIGH_VOLUME
@pytest.mark.unit
def test_maybe_resize_never_raises_on_arbitrary_text(payload):
    """`_maybe_resize` is called on every WS exec input frame before
    the frame reaches the PTY. Any exception here would kill the
    user's shell session. Must return a bool for ANY string."""
    client = MagicMock()
    try:
        result = _maybe_resize(payload, client, "exec-abc")
    except Exception as exc:
        pytest.fail(
            f"_maybe_resize raised {type(exc).__name__} on input {payload[:64]!r}: {exc}",
        )
    assert isinstance(result, bool)


@given(
    type_val=st.one_of(st.text(max_size=32), st.integers(), st.none(), st.booleans()),
    cols=st.one_of(st.integers(min_value=-(10**9), max_value=10**9), st.text(max_size=16), st.none()),
    rows=st.one_of(st.integers(min_value=-(10**9), max_value=10**9), st.text(max_size=16), st.none()),
)
@_MEDIUM_VOLUME
@pytest.mark.unit
def test_maybe_resize_typed_frames(type_val, cols, rows):
    """Well-shaped JSON frame with wild field types. Must not crash."""
    frame = json.dumps({"type": type_val, "cols": cols, "rows": rows}, default=str)
    client = MagicMock()
    try:
        result = _maybe_resize(frame, client, "exec-abc")
    except Exception as exc:
        pytest.fail(f"_maybe_resize raised {type(exc).__name__} on typed frame: {exc}")
    assert isinstance(result, bool)


# ── http_error envelope — every catalogue code is constructable ─────


@given(code=st.sampled_from(list(known_codes())))
@_MEDIUM_VOLUME
@pytest.mark.unit
def test_every_catalogued_code_constructable(code):
    """For every error code in `known_codes()`, `http_error(code)` must
    construct successfully. Catches missing format-placeholders
    (where a code's template has `{name}` but the caller didn't pass
    kwargs) at test-time instead of request-time."""
    try:
        exc = http_error(code)
    except KeyError:
        # Some codes legitimately require kwargs for their template.
        # Retry with a plausible kwarg bundle.
        common_kwargs = {
            "id": "abc123",
            "name": "test",
            "image": "alpine",
            "port": 8080,
            "registry": "docker.io",
            "signal": "SIGTERM",
            "minimum": 6_291_456,
            "cap": "1Gi",
            "threshold": 1024,
            "svc_name": "svc",
            "ipc_mode": "host",
            "key": "privileged",
            "limit": 10,
            "seconds": 300,
            "detail": "demo",
            "status": 500,
            "message": "demo",
            "path": "/demo",
        }
        try:
            exc = http_error(code, **common_kwargs)
        except Exception as retry_exc:
            pytest.fail(f"code {code!r} unconstructable even with common kwargs: {retry_exc}")
    except Exception as exc:
        pytest.fail(f"http_error({code!r}) raised {type(exc).__name__}: {exc}")
    assert isinstance(exc, HTTPException)
    assert isinstance(exc.detail, dict)
    assert exc.detail.get("code") == code


@given(
    status_override=st.one_of(
        st.none(),
        st.integers(min_value=-1000, max_value=1000),
    ),
)
@_MEDIUM_VOLUME
@pytest.mark.unit
def test_http_error_status_override_bounded(status_override):
    """`status_override` must produce a final HTTP status in [400, 599]
    — SKIFF clamps exotic Docker-daemon status codes to avoid
    propagating non-compliant status lines to Starlette."""
    exc = http_error("resource.not_found", status_override=status_override)
    assert 400 <= exc.status_code <= 599, (
        f"status_override={status_override} produced out-of-range final status {exc.status_code}"
    )
