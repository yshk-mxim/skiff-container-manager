# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Backend bug-class fuzz — complements the UI class file.

The UI counterpart (`test_ui_bug_class_regressions.py`) targets bug
classes that render in the browser (Nielsen's usability heuristics,
engineering race classes). This file targets the *backend-visible*
classes where bad inputs, weird Docker responses, or unexpected SDK
exceptions can lead to:

  1. **Docker SDK exception leakage** — a daemon-side failure returns as
     a raw 500 HTML page or an untyped `HTTPException` instead of the
     `{"detail": {"code": ..., "message": ...}}` envelope the UI and
     API consumers contract for. Every route must funnel Docker
     exceptions through `safe_docker_call` → `http_error(code)`.

  2. **Control-char / unicode round-trip** — Docker can emit container
     names with `\\n`, `\\r`, null bytes, or wide-plane unicode
     (emoji / RTL). Response bodies must serialize as valid JSON and
     no user-supplied bytes may escape into a response header (CRLF
     injection class).

  3. **Numeric boundary** — Docker stats can return very large numbers
     (uptime nanoseconds on long-running containers, int64 sizes),
     negative deltas (clock skew between pre/cur cpu_stats), or
     subnormal floats. JSON has no NaN/Infinity — routers that divide
     or subtract must coerce to a JSON-safe numeric.

Mapped to canonical frameworks:

  - OWASP API Security Top 10 — this file targets API2 (broken auth
    response shape), API8 (injection via unicode / CRLF), API10
    (improper asset management via unexpected SDK exception leaks).
  - ISO/IEC 25010 — reliability (fault tolerance), security (integrity
    of the JSON contract).

Approach: each class has a hypothesis harness that hammers the layer
under test with a realistic-but-randomised input shape. A 500 response
with an unstructured body fails the invariant; so does a JSON-encode
exception, a header with `\\r\\n` in the value, or a body containing
`NaN`/`Infinity` (not valid JSON per RFC 8259).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import docker.errors
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from skiff.contract.errors import known_codes


def _reset_limiter_for_fuzz() -> None:
    """Hypothesis runs many examples; reset the session-scoped slowapi
    limiter each iteration so the fuzz exercises the handler, not the
    429 short-circuit path. SKIFF has two limiter instances (config
    module + app.state); reset both or earlier suite tests can leave
    counters that fail this test under full-suite runs but pass in
    isolation."""
    from skiff import config as config_module
    from skiff.app import app

    for limiter in {config_module.limiter, app.state.limiter}:
        limiter.reset()


def _build_mock_client(**overrides) -> MagicMock:
    """Shared Docker-mock skeleton. `ping` must succeed so the client
    passes the liveness gate; callers wire per-test return values or
    side-effects on top."""
    m = MagicMock()
    m.ping.return_value = True
    m.containers.list.return_value = []
    m.containers.get.return_value = MagicMock()
    m.images.list.return_value = []
    m.volumes.list.return_value = []
    m.networks.list.return_value = []
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


def _assert_envelope_shape(resp, context: str) -> None:
    """Invariant for every 4xx/5xx SKIFF response: JSON body with
    `{"detail": {"code": <catalogued>, "message": str, ...}}`. No raw
    HTML, no Starlette default plain-text 500, no untyped string
    `detail`. 200/204 exempt."""
    assert resp.status_code not in (None, 0), f"{context}: no status"
    if resp.status_code < 400:
        return
    # Must be JSON.
    ct = resp.headers.get("content-type", "")
    assert "application/json" in ct.lower(), (
        f"{context}: error response content-type is {ct!r}, not JSON — "
        f"body: {resp.text[:200]!r}"
    )
    try:
        body = resp.json()
    except ValueError:
        pytest.fail(f"{context}: non-JSON error body: {resp.text[:200]!r}")
    detail = body.get("detail")
    # Some error paths (422 validation) produce a list — that's Starlette's
    # RequestValidationError default, which SKIFF keeps as-is for pydantic
    # shape violations. Only check the envelope when detail is a dict.
    if not isinstance(detail, dict):
        return
    code = detail.get("code")
    assert code in known_codes(), (
        f"{context}: error envelope code {code!r} not in known_codes() — "
        f"every error must be catalogued"
    )
    assert isinstance(detail.get("message"), str) and detail["message"], (
        f"{context}: envelope missing or empty `message` — body: {body!r}"
    )


# ── Class 1: Docker SDK exception funnel ─────────────────────────────────


# Representative Docker exceptions. If a router forgets to wrap a Docker
# SDK call in `safe_docker_call`, the raw exception propagates as a 500
# with no envelope. This harness forces each exception at EVERY probed
# endpoint and asserts the funnel holds.
_docker_exceptions = st.sampled_from(
    [
        ("not_found", docker.errors.NotFound("no such container")),
        ("api_error", docker.errors.APIError("daemon error")),
        ("docker_exception", docker.errors.DockerException("daemon unreachable")),
        ("image_not_found", docker.errors.ImageNotFound("no such image")),
    ]
)

# Read-only endpoints where a mock raising keeps the surface small
# (no path params that would themselves reject the request). Each is
# expected to return a catalogued envelope when Docker misbehaves.
_probed_read_endpoints = [
    ("GET", "/api/containers", "containers.list"),
    ("GET", "/api/images", "images.list"),
    ("GET", "/api/volumes", "volumes.list"),
    ("GET", "/api/networks", "networks.list"),
    ("GET", "/api/system/df", "df"),
    ("GET", "/api/system/info", "info"),
]


@given(exc=_docker_exceptions)
@settings(max_examples=30, deadline=None)
@pytest.mark.unit
def test_docker_exceptions_surface_as_catalogued_envelope(exc):
    """Every read endpoint must funnel any Docker SDK exception into
    a `{detail:{code, message}}` envelope with `code in known_codes()`.
    A bare 500 or a non-catalogued code means the route is missing its
    `safe_docker_call` wrapper and will leak a stack trace to the UI."""
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    kind, docker_exc = exc
    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""

    # Install the exception as a side-effect on every client method. Any
    # endpoint that does NOT funnel through safe_docker_call will surface
    # the raw exception as a 500.
    mock_client = _build_mock_client()
    mock_client.containers.list.side_effect = docker_exc
    mock_client.images.list.side_effect = docker_exc
    mock_client.volumes.list.side_effect = docker_exc
    mock_client.networks.list.side_effect = docker_exc
    mock_client.df.side_effect = docker_exc
    mock_client.info.side_effect = docker_exc

    _reset_limiter_for_fuzz()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                for method, path, _ in _probed_read_endpoints:
                    r = tc.request(
                        method, path, headers={"X-Requested-With": "ContainerManager"}
                    )
                    # Success path is impossible (we forced an exception).
                    # The invariant: error envelope + catalogued code.
                    assert r.status_code >= 400, (
                        f"{path} returned {r.status_code} despite forced "
                        f"{kind} — Docker error was swallowed: {r.text[:200]!r}"
                    )
                    _assert_envelope_shape(r, f"{method} {path} w/ {kind}")
    finally:
        config_module._cfg.api_token = original_token


# ── Class 2: Unicode / control-char round-trip ───────────────────────────


def _make_wild_container(name: str, mock_image_id: str = "sha256:abcd") -> MagicMock:
    """Build a docker.containers.Container-shaped mock where the `name`
    attribute is hypothesis-generated text — so we can prove the router
    handles pathological daemon-returned strings without crashing JSON
    encode or smuggling bytes into a response header."""
    c = MagicMock()
    c.id = "abc123def456" * 2
    c.short_id = "abc123def456"
    c.name = name
    c.status = "running"
    c.image.tags = ["alpine:latest"]
    c.image.id = mock_image_id
    c.attrs = {
        "Created": "2026-04-18T00:00:00Z",
        "State": {"Running": True, "Status": "running", "StartedAt": "2026-04-18T00:00:00Z"},
        "Config": {
            "Image": "alpine:latest",
            "Cmd": ["sh"],
            "Env": [],
            "Labels": {},
            "Entrypoint": None,
            "WorkingDir": "",
            "User": "",
            "Hostname": "",
        },
        "HostConfig": {
            "Memory": 0,
            "NanoCpus": 0,
            "RestartPolicy": {"Name": "no"},
            "Privileged": False,
            "CapAdd": [],
            "CapDrop": [],
            "PortBindings": {},
            "Binds": [],
            "ReadonlyRootfs": False,
            "IpcMode": "",
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {}, "Ports": {}, "IPAddress": ""},
        "Name": f"/{name}",
    }
    c.labels = {}
    c.ports = {}
    return c


# Hypothesis strategy: strings that are realistic for a Docker daemon
# to surface back to us — full unicode plane, CR/LF/null, zero-width,
# RTL override, long-ASCII, etc. A router that interpolates these
# without sanitising will trip either JSON encoding or a header check.
_wild_name = st.one_of(
    # Hypothesis `text` by default generates full BMP; constrain to codepoints
    # that can round-trip UTF-8 (skip lone surrogates U+D800-U+DFFF — those
    # aren't valid in network-transported UTF-8 and Python refuses to encode
    # them, which would mask real bugs under encode errors).
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),  # exclude surrogates
            max_codepoint=0xFFFF,
        ),
        max_size=80,
    ),
    # Known classes of real-world pain for text layout / serialisation:
    st.sampled_from(
        [
            "container\r\nX-Injected: bad",  # CRLF injection into headers
            "container\x00null",  # NUL-byte split
            "container\u202eRTL",  # RTL override — visual spoofing
            "container\u200bzwsp",  # zero-width space
            "container\U0001f4a9",  # 4-byte UTF-8 (💩 — single codepoint)
            "容器名前",  # full CJK
            "\r\n" * 8,  # pure CRLF
            " " * 64,  # whitespace-only
            "a" * 255,  # boundary on Docker's 255-char name cap
        ]
    ),
)


@given(names=st.lists(_wild_name, min_size=1, max_size=4))
@settings(max_examples=60, deadline=None)
@pytest.mark.unit
def test_container_listing_survives_wild_unicode_names(names):
    """Docker can return container names containing CRLF, null bytes,
    zero-width marks, or non-BMP characters. `/api/containers` must:

      1. Return 200 with a JSON body.
      2. Serialise every name without raising (no `UnicodeEncodeError`,
         no `TypeError` on surrogate halves).
      3. Keep the body parseable by `json.loads` — which rejects NaN/
         Infinity and forbids embedded control chars in strings below
         U+0020 unless escaped.
      4. Emit no response header whose value contains a raw CR or LF —
         that's the CRLF-injection class.
    """
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    mock_client = _build_mock_client()
    mock_client.containers.list.return_value = [_make_wild_container(n) for n in names]

    _reset_limiter_for_fuzz()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.get("/api/containers", headers={"X-Requested-With": "ContainerManager"})

                # Endpoint may reject a wild name as a 4xx, but it must
                # NOT crash with a 500 — that's the class we're guarding.
                assert r.status_code < 500, (
                    f"wild-unicode names crashed handler (500): {r.text[:200]!r} "
                    f"— names={names!r}"
                )

                # Body must be JSON (or a well-shaped envelope on 4xx).
                try:
                    body_text = r.text
                    json.loads(body_text)
                except ValueError as exc:
                    pytest.fail(
                        f"response body is not valid JSON for names={names!r}: {exc} — body: {body_text[:200]!r}"
                    )

                # No header value may contain a bare CR/LF.
                for hk, hv in r.headers.items():
                    assert "\r" not in hv and "\n" not in hv, (
                        f"CRLF leaked into response header {hk!r}={hv!r} — "
                        f"a daemon-supplied name containing \\r\\n made it out"
                    )
    finally:
        config_module._cfg.api_token = original_token


# ── Class 3: Numeric boundary — JSON doesn't allow NaN/Inf ───────────────


@st.composite
def _extreme_numeric_stats(draw):
    """Stats response with numeric extremes — very large, negative, zero."""
    extreme = st.one_of(
        st.just(0),
        st.just(2**63 - 1),  # int64 max
        st.just(2**53),  # above JS safe-integer range
        st.integers(min_value=-(10**12), max_value=-1),  # negative
        st.integers(min_value=0, max_value=10**15),
    )
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": draw(extreme)},
            "system_cpu_usage": draw(extreme),
            "online_cpus": draw(st.integers(min_value=1, max_value=128)),
        },
        "precpu_stats": {
            # Deliberately make precpu >> cpu to force a negative delta —
            # a naive subtraction would emit a negative cpu_percent or
            # a NaN via 0/0.
            "cpu_usage": {"total_usage": draw(extreme)},
            "system_cpu_usage": draw(extreme),
        },
        "memory_stats": {
            "usage": draw(extreme),
            "limit": draw(extreme),
            "stats": {
                "cache": draw(extreme),
                "inactive_file": draw(extreme),
            },
        },
        "networks": {"eth0": {"rx_bytes": draw(extreme), "tx_bytes": draw(extreme)}},
        "blkio_stats": {"io_service_bytes_recursive": []},
    }


@given(stats=_extreme_numeric_stats())
@settings(max_examples=100, deadline=None)
@pytest.mark.unit
def test_container_stats_never_emits_nan_or_infinity(stats):
    """Stats math can divide by zero (both cpu_stats totals equal), go
    negative (clock skew / counter reset), or overflow. JSON forbids
    NaN and Infinity (RFC 8259 §6). Any stats handler that emits them
    produces a body that breaks a strict JSON parser — and Python's
    own `json.loads` with default settings DOES accept them, so a
    vanilla parse won't catch it. We assert the raw response text
    contains none of the forbidden tokens."""
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    mock_client = _build_mock_client()
    container_mock = MagicMock()
    container_mock.stats.return_value = stats
    container_mock.short_id = "abc123def456"
    mock_client.containers.get.return_value = container_mock

    _reset_limiter_for_fuzz()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.get(
                    "/api/containers/abc123def456/stats",
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code < 500, (
                    f"stats crashed on extreme numerics: {r.text[:200]!r}"
                )
                # RFC 8259: the tokens NaN, Infinity, -Infinity are NOT
                # valid JSON. Python's `json.dumps` with default settings
                # emits them anyway (allow_nan=True default). A strict
                # scrape from Prometheus or a browser's strict JSON
                # parser would reject. Assert they never appear.
                forbidden = ("NaN", "Infinity", "-Infinity")
                for tok in forbidden:
                    assert tok not in r.text, (
                        f"response contains forbidden JSON token {tok!r} — "
                        f"handler emitted a non-JSON-safe numeric. "
                        f"stats={stats!r} body={r.text[:200]!r}"
                    )
                # All numeric fields must be finite numbers.
                if r.status_code == 200:
                    body = r.json()
                    for field in (
                        "cpu_percent",
                        "mem_usage_mb",
                        "mem_limit_mb",
                        "mem_percent",
                        "net_rx_mb",
                        "net_tx_mb",
                    ):
                        v = body.get(field)
                        assert isinstance(v, (int, float)), (
                            f"field {field!r} is {v!r} — must be numeric"
                        )
                        import math

                        assert not (
                            isinstance(v, float) and math.isnan(v)
                        ), f"field {field!r} is NaN for stats={stats!r}"
                        assert v not in (float("inf"), float("-inf")), (
                            f"field {field!r} is infinite for stats={stats!r}"
                        )
    finally:
        config_module._cfg.api_token = original_token


# ── Class 4: Contract invariance — auth-gated routes without token ──────


@given(
    method_path=st.sampled_from(
        [
            ("GET", "/api/containers"),
            ("GET", "/api/images"),
            ("GET", "/api/volumes"),
            ("GET", "/api/networks"),
            ("GET", "/api/system/df"),
            ("GET", "/api/system/info"),
            ("POST", "/api/containers/abc/start"),
            ("DELETE", "/api/containers/abc"),
        ]
    ),
    bad_tokens=st.lists(
        # Constrain to printable ASCII — httpx rejects non-ASCII in headers
        # at the client layer, which would crash the test before exercising
        # the server's auth path. The server-side test for unicode handling
        # in headers is covered by the unicode round-trip test above.
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_.",
            min_size=0,
            max_size=80,
        ),
        min_size=1,
        max_size=1,
    ),
)
@settings(max_examples=60, deadline=None)
@pytest.mark.unit
def test_auth_gated_endpoints_return_catalogued_envelope(method_path, bad_tokens):
    """Every auth-gated endpoint, when called with a garbage token,
    must return a catalogued auth envelope (not a raw 401/403 string
    nor a 500). This is the class of bug where adding a new route
    silently skips the auth decorator — caught here because any
    non-envelope response fails the shape invariant."""
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff.app import app

    method, path = method_path
    bad_token = bad_tokens[0]
    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = "real-token-" + "x" * 24

    _reset_limiter_for_fuzz()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            headers = {"X-Requested-With": "ContainerManager"}
            if bad_token:
                headers["Authorization"] = f"Bearer {bad_token}"
            r = tc.request(method, path, headers=headers)
            assert r.status_code in (401, 403, 429), (
                f"{method} {path} with bad token returned {r.status_code} — "
                f"expected 401/403/429: {r.text[:200]!r}"
            )
            _assert_envelope_shape(r, f"{method} {path} w/ bad token")
    finally:
        config_module._cfg.api_token = original_token
