# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Hypothesis-driven state-transition fuzz tests.

Three state machines drive SKIFF's runtime:
  1. PROFILE lifecycle (boot → optional enter-reviewer → reset-config).
  2. Undo queue lifecycle (enqueued → cancelled | fired | shutdown-flushed).
  3. _ws_acquire / _ws_release counter integrity across arbitrary
     interleavings.

Property-based tests exercise RANDOM sequences of valid operations
against these FSMs so we don't rely on manually-enumerated happy-paths
to catch liveness + safety invariants. A sequence that breaks an
invariant is automatically shrunk to a minimal repro by Hypothesis.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)

import skiff.config as config_module
from skiff.routers.containers_ws import (
    _active_exec_ws,
    _ws_acquire,
    _ws_connections,
    _ws_lock,
    _ws_release,
)
from skiff.secure import _reject_if_reviewer
from skiff.undo import UndoQueue

# ── PROFILE transitions ──────────────────────────────────────────────────────

_VALID_BOOT_PROFILES = ("dev", "sre", "homelab", "tutor", "ci", "reviewer")


@pytest.mark.unit
@given(boot_profile=st.sampled_from(_VALID_BOOT_PROFILES))
@settings(max_examples=50)
def test_reject_if_reviewer_holds_for_any_boot_profile_with_token(boot_profile):
    """Invariant: reviewer gate rejects iff PROFILE=reviewer AND token set.

    Valid boot profiles (every persona in the catalogue) must produce
    the expected gate behaviour:
      reviewer + token set      → raise 403 auth.reviewer_read_only
      reviewer + no token       → no raise (setup still open)
      any other profile         → no raise
    """
    with patch.object(config_module, "PROFILE", boot_profile):
        with patch.object(config_module._cfg, "api_token", "nontrivial-token"):
            if boot_profile == "reviewer":
                with pytest.raises(Exception) as exc:  # HTTPException
                    _reject_if_reviewer()
                assert exc.value.status_code == 403  # type: ignore[attr-defined]
                assert exc.value.detail["code"] == "auth.reviewer_read_only"  # type: ignore[attr-defined,index]
            else:
                _reject_if_reviewer()  # no raise

    # Empty-token branch: pre-setup; never raise regardless of profile.
    with patch.object(config_module, "PROFILE", boot_profile):
        with patch.object(config_module._cfg, "api_token", ""):
            _reject_if_reviewer()


# ── Undo state-machine fuzz ──────────────────────────────────────────────────


class UndoFSM(RuleBasedStateMachine):
    """Every ENQUEUE/CANCEL/FIRE sequence maintains these invariants:
    - depth() == len(pending enqueued, not cancelled, not fired)
    - fire_failures() increments ONLY on Exception that isn't NotFound
    - a cancelled token can never fire (no double-action)
    - _fire on a cancelled or missing token is a no-op (doesn't raise)
    """

    tokens = Bundle("tokens")

    @initialize()
    def setup(self) -> None:
        self.queue = UndoQueue()
        self.live: dict[str, dict] = {}  # token → {fn, fired, cancelled}

    @rule(target=tokens, kind=st.sampled_from(["container", "volume", "image"]))
    def enqueue(self, kind):
        fn = MagicMock()
        # Cancel the Timer immediately so every fire goes through our rule,
        # not the auto-schedule. This makes the FSM deterministic.
        tok = self.queue.enqueue(kind, "rid-x", fn)
        assume(tok is not None)
        with self.queue._lock:
            op = self.queue._ops[tok]
        op.timer.cancel()
        self.live[tok] = {"fn": fn, "fired": False, "cancelled": False}
        return tok

    @rule(tok=tokens)
    def cancel(self, tok):
        was_cancelled = self.live[tok]["cancelled"] or self.live[tok]["fired"]
        outcome = self.queue.cancel(tok)
        if was_cancelled:
            assert outcome is False
        else:
            assert outcome is True
            self.live[tok]["cancelled"] = True

    @rule(tok=tokens)
    def fire(self, tok):
        state = self.live[tok]
        # dev profile so the reviewer gate doesn't short-circuit the fire.
        with patch.object(config_module, "PROFILE", "dev"):
            self.queue._fire(tok)
        # A double-fire or fire-after-cancel must be a NO-OP on `fn`.
        if not state["cancelled"] and not state["fired"]:
            state["fn"].assert_called_once()
            state["fired"] = True
        # Still a no-op; we don't assert on call count because cancelled
        # before fire means fn was never called.
        elif state["cancelled"]:
            assert state["fn"].call_count == 0
        else:
            # already fired once — must stay at exactly one call
            assert state["fn"].call_count == 1

    @invariant()
    def depth_matches_live(self) -> None:
        live_pending = sum(1 for s in self.live.values() if not (s["cancelled"] or s["fired"]))
        assert self.queue.depth() == live_pending


TestUndoFSM = UndoFSM.TestCase
TestUndoFSM.settings = settings(
    max_examples=50,
    stateful_step_count=30,
    deadline=None,
)


# ── _ws_acquire / _ws_release counter integrity ──────────────────────────────


class WsCounterFSM(RuleBasedStateMachine):
    """Random acquire/release sequences must keep the per-IP counter
    bounded at [0, WS_MAX_PER_IP] for every IP seen, and the counter
    equals the number of un-matched acquire() calls that returned True."""

    @initialize()
    def setup(self) -> None:
        # Start with a clean counter map.
        with _ws_lock:
            _ws_connections.clear()
        self.shadow: dict[str, int] = {}

    @rule(ip=st.sampled_from(("10.0.0.1", "10.0.0.2", "::1")))
    def acquire(self, ip):
        expected = self.shadow.get(ip, 0)
        accepted = _ws_acquire(ip)
        if expected >= config_module.WS_MAX_PER_IP:
            assert accepted is False
        else:
            assert accepted is True
            self.shadow[ip] = expected + 1

    @rule(ip=st.sampled_from(("10.0.0.1", "10.0.0.2", "::1")))
    def release(self, ip):
        _ws_release(ip)
        # _ws_release floors at zero even for IPs that never acquired.
        self.shadow[ip] = max(0, self.shadow.get(ip, 0) - 1)

    @invariant()
    def counter_matches_shadow(self) -> None:
        with _ws_lock:
            live = dict(_ws_connections)
        for ip, count in live.items():
            assert count == self.shadow.get(ip, 0)

    @invariant()
    def counter_never_negative_or_above_cap(self) -> None:
        with _ws_lock:
            for ip, count in _ws_connections.items():
                assert 0 <= count <= config_module.WS_MAX_PER_IP, f"{ip} counter out of bounds: {count}"


TestWsCounterFSM = WsCounterFSM.TestCase
TestWsCounterFSM.settings = settings(
    max_examples=40,
    stateful_step_count=40,
    deadline=None,
)


# ── Active-exec-ws set integrity under random register/unregister ───────────


@pytest.mark.unit
@given(n=st.integers(min_value=1, max_value=12))
@settings(max_examples=30)
def test_try_register_and_unregister_are_inverse(n):
    """N registers followed by N unregisters should leave the set as it was."""
    from skiff.routers.containers_ws import (
        _try_register_exec_ws,
        _unregister_exec_ws,
    )

    with _ws_lock:
        baseline = set(_active_exec_ws)

    with patch.object(config_module, "PROFILE", "dev"):
        pairs = [(MagicMock(), f"cid-{i}") for i in range(n)]
        for ws, cid in pairs:
            assert _try_register_exec_ws(ws, cid) is True
        for ws, cid in pairs:
            _unregister_exec_ws(ws, cid)

    with _ws_lock:
        assert set(_active_exec_ws) == baseline


# ── _maybe_resize input fuzz ────────────────────────────────────────────────


@pytest.mark.unit
@given(payload=st.text(max_size=200))
@settings(max_examples=200)
def test_maybe_resize_never_raises(payload):
    """No string input should ever raise out of _maybe_resize.

    Hypothesis sweeps arbitrary text to shake out any crash path —
    truncated JSON, lone braces, null bytes, Unicode mess, etc.
    The function must either consume (True) or pass through (False).
    """
    from skiff.routers.containers_ws import _maybe_resize

    client = MagicMock()
    client.api.exec_resize.side_effect = None
    try:
        result = _maybe_resize(payload, client, "exec-abc")
    except Exception as exc:
        pytest.fail(f"_maybe_resize raised on input {payload!r}: {exc}")
    assert isinstance(result, bool)


@pytest.mark.unit
@given(
    cols=st.integers(min_value=-(10**9), max_value=10**9),
    rows=st.integers(min_value=-(10**9), max_value=10**9),
)
@settings(max_examples=100)
def test_maybe_resize_out_of_bounds_never_reaches_docker(cols, rows):
    """Only cols and rows both in [4, 1024] reach exec_resize."""
    import json as _json

    from skiff.routers.containers_ws import _maybe_resize

    client = MagicMock()
    payload = _json.dumps({"type": "resize", "cols": cols, "rows": rows})
    _maybe_resize(payload, client, "exec-abc")
    if 4 <= cols <= 1024 and 4 <= rows <= 1024:
        client.api.exec_resize.assert_called_once_with(
            "exec-abc",
            height=rows,
            width=cols,
        )
    else:
        client.api.exec_resize.assert_not_called()


# ── _classify_event fuzz: status / path invariants ──────────────────────────


@pytest.mark.unit
@given(
    method=st.sampled_from(("GET", "POST", "PUT", "DELETE", "PATCH")),
    path=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_.",
        min_size=1,
        max_size=150,
    ),
    status=st.integers(min_value=100, max_value=599),
)
@settings(max_examples=200)
def test_classify_event_never_returns_over_caps(method, path, status):
    """resource_type ≤ 32, resource_id ≤ 128 for every possible input."""
    from skiff.logging_setup import _classify_event

    event, rtype, rid = _classify_event(method, path, status)
    assert isinstance(event, str) and event
    assert len(rtype) <= 32
    assert len(rid) <= 128


@pytest.mark.unit
@given(
    status=st.sampled_from((401, 403)),
    error_code=st.sampled_from(
        ("", "auth.missing_token", "auth.invalid_token", "auth.reviewer_read_only", "some.other"),
    ),
)
@settings(max_examples=50)
def test_classify_event_403_reviewer_vs_generic(status, error_code):
    """Precedence: reviewer_read_only → auth.reviewer_denied; other 403/401 → auth.denied."""
    from skiff.logging_setup import _classify_event

    event, _, _ = _classify_event(
        "POST",
        "/api/containers/run",
        status=status,
        error_code=error_code,
    )
    if status == 403 and error_code == "auth.reviewer_read_only":
        assert event == "auth.reviewer_denied"
    else:
        assert event == "auth.denied"


# ── Error envelope contract under fuzzed codes + kwargs ─────────────────────


@pytest.mark.unit
@given(
    extra=st.dictionaries(
        st.text(min_size=1, max_size=8, alphabet="abcde_"),
        st.one_of(st.integers(), st.text(max_size=20)),
        max_size=3,
    ),
)
@settings(max_examples=50)
def test_http_error_envelope_shape_is_always_code_message(extra):
    """Every HTTPException built via http_error has `detail = {code, message, ...}`."""
    from skiff.contract.errors import http_error

    exc = http_error("resource.not_found", extra=extra)
    assert isinstance(exc.detail, dict)
    assert "code" in exc.detail
    assert "message" in exc.detail
    for k in extra:
        assert k in exc.detail
