# SPDX-License-Identifier: MIT
"""Property + unit tests that close the last coverage gaps in critical modules.

Targets: auth._ws_tick, secure._find_request_arg, validators._volume_source,
and the undo cancel/fire race + fire_all_now error-suppression path. Each
uses Hypothesis where the function branches, so the test covers every
path instead of cherry-picking example inputs.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import Request
from hypothesis import given, settings
from hypothesis import strategies as st

import skiff.auth as auth_module
import skiff.secure as secure_module
import skiff.undo as undo_module
import skiff.validators as validators_module

pytestmark = pytest.mark.unit


# ── auth._ws_tick ────────────────────────────────────────────────────────────


@given(ticks=st.integers(min_value=0, max_value=100))
def test_ws_tick_before_revalidate(ticks: int) -> None:
    """When ticks+1 < WS_KEEPALIVE_REVALIDATE_EVERY → (ticks+1, False).

    Covers the fall-through return at auth.py:217 (the common case of an
    active WS session that hasn't accumulated enough ticks to trigger a
    session-age re-check yet).
    """
    from skiff import config

    if ticks + 1 >= config.WS_KEEPALIVE_REVALIDATE_EVERY:
        # Outside our coverage target — the other branch returns (0, True).
        return
    next_ticks, revalidate = auth_module._ws_tick(ticks)
    assert next_ticks == ticks + 1
    assert revalidate is False


@given(ticks=st.integers(min_value=0, max_value=100))
def test_ws_tick_at_or_past_revalidate(ticks: int) -> None:
    """At or past the revalidate threshold → counter resets to 0, flag is True."""
    from skiff import config

    if ticks + 1 < config.WS_KEEPALIVE_REVALIDATE_EVERY:
        return
    next_ticks, revalidate = auth_module._ws_tick(ticks)
    assert next_ticks == 0
    assert revalidate is True


# ── secure._find_request_arg ─────────────────────────────────────────────────


def _fake_request() -> Request:
    """Build a Request that passes isinstance() without a live ASGI scope."""
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/", "query_string": b""}
    return Request(scope)


def test_find_request_in_kwargs() -> None:
    req = _fake_request()
    assert secure_module._find_request((1, 2), {"request": req}) is req


def test_find_request_as_positional() -> None:
    """Fallback path — request comes via *args instead of kwargs (lines 190-193)."""
    req = _fake_request()
    result = secure_module._find_request((1, req, "x"), {})
    assert result is req


def test_find_request_absent_returns_none() -> None:
    """No Request anywhere in args/kwargs → None (defensive; caller handles)."""
    assert secure_module._find_request((1, 2), {}) is None
    assert secure_module._find_request((), {"foo": "bar"}) is None


@given(other_args=st.lists(st.one_of(st.integers(), st.text(max_size=8), st.none()), max_size=5))
def test_find_request_positional_property(other_args: list) -> None:
    """Property: a Request in *args is found regardless of its position."""
    req = _fake_request()
    for insert_at in range(len(other_args) + 1):
        args = (*other_args[:insert_at], req, *other_args[insert_at:])
        assert secure_module._find_request(args, {}) is req


# ── validators._volume_source ────────────────────────────────────────────────


@given(s=st.text(max_size=50))
def test_volume_source_str_passthrough(s: str) -> None:
    assert validators_module._volume_source(s) == s


@given(
    src=st.text(max_size=30),
    extra=st.dictionaries(st.text(min_size=1, max_size=5), st.text(max_size=5), max_size=3),
)
def test_volume_source_dict_extracts_source(src: str, extra: dict) -> None:
    vol = {**extra, "source": src}
    assert validators_module._volume_source(vol) == src


def test_volume_source_dict_missing_source_returns_empty() -> None:
    assert validators_module._volume_source({"target": "/data"}) == ""
    assert validators_module._volume_source({"source": None}) == ""


@given(n=st.integers(min_value=-100, max_value=100))
def test_volume_source_non_str_non_dict_fallback(n: int) -> None:
    """Line 525: anything other than str/dict goes through str() coercion.

    Compose spec usually gives strings, but dict-less entries can be
    ints (port numbers parsed oddly) or arbitrary types from a broken
    YAML parse. The fallback prevents a TypeError in downstream
    host-path detection.
    """
    assert validators_module._volume_source(n) == str(n)


# ── undo._fire cancel-race + fire_all_now exception swallowing ──────────────


def test_undo_fire_after_cancel_is_noop() -> None:
    """Line 96: timer fires AFTER cancel() removed the op from the queue.

    Simulate the race: enqueue, then cancel, then call _fire with the
    same token. Must return silently, not KeyError. The real code path
    is rare but real — Timer + cancel have a ms-wide race window.
    """
    q = undo_module.UndoQueue(delay_secs=3600.0)
    called = threading.Event()
    token = q.enqueue("container", "abc", called.set)
    assert token is not None
    q.cancel(token)
    q._fire(token)
    assert not called.is_set()


def test_undo_fire_all_now_swallows_individual_errors() -> None:
    """Lines 139-140: fire_all_now keeps going when one op's fn raises.

    Enqueue N ops where one raises. After fire_all_now, every OTHER op
    must have run. We use a delay long enough that the timers don't
    auto-fire in the test window.
    """
    q = undo_module.UndoQueue(delay_secs=3600.0)
    ran: list[str] = []

    def ok(name: str) -> None:
        ran.append(name)

    def boom() -> None:
        raise RuntimeError("fn failed")

    q.enqueue("container", "a", ok, "a")
    q.enqueue("container", "b", boom)
    q.enqueue("container", "c", ok, "c")
    q.fire_all_now()
    assert "a" in ran
    assert "c" in ran
    assert q.depth() == 0


# ── auth._ws_tick Hypothesis-settles the 2^1 branch space ────────────────────


@given(ticks=st.integers(min_value=0, max_value=1_000_000))
@settings(max_examples=200)
def test_ws_tick_invariants(ticks: int) -> None:
    """Both branches satisfy: output ticks ∈ [0, threshold) and the
    revalidate flag is True iff the counter hit the threshold."""
    from skiff import config

    out_ticks, revalidate = auth_module._ws_tick(ticks)
    assert 0 <= out_ticks < config.WS_KEEPALIVE_REVALIDATE_EVERY
    assert revalidate == (ticks + 1 >= config.WS_KEEPALIVE_REVALIDATE_EVERY)
