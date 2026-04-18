# SPDX-License-Identifier: MIT
"""Additional property tests using the shared strategies library.

These complement the existing test_fuzz.py / test_properties.py by
exercising:

  1. UndoQueue state-machine invariants (enqueue / cancel / fire).
  2. Memory + CPU parser round-trips using tests/strategies.py strategies.
  3. Compose YAML robustness using tests/strategies.py shared bodies.

Fold back into test_fuzz.py once the pattern stabilises.
"""

from __future__ import annotations

from threading import Event

import pytest
from fastapi import HTTPException
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from tests.strategies import (
    compose_yaml_body_st,
    container_id_st,
    cpu_quantity_st,
    memory_quantity_st,
    project_name_st,
)

pytestmark = pytest.mark.property


# ─────────────────────────────────────────────────────────────────────────────
# UndoQueue state machine
# ─────────────────────────────────────────────────────────────────────────────


class _UndoQueueMachine(RuleBasedStateMachine):
    """Random enqueue/cancel/fire_all_now sequences preserve invariants."""

    def __init__(self) -> None:
        super().__init__()
        from skiff.undo import UndoQueue

        self.q = UndoQueue(delay_secs=3600.0)  # "effectively never" auto-fire
        self.state: dict[str, dict] = {}

    @rule(kind=st.sampled_from(["container", "volume", "image", "network"]), rid=st.text(min_size=1, max_size=12))
    def enqueue(self, kind: str, rid: str) -> None:
        tok = self.q.enqueue(kind, rid, lambda: None)
        if tok is None:
            from skiff.undo import QUEUE_MAX_DEPTH

            assert self.q.depth() == QUEUE_MAX_DEPTH
            return
        assert tok not in self.state, "token collision"
        self.state[tok] = {"fired": False, "cancelled": False, "ran": False}

        def _fn() -> None:
            self.state[tok]["ran"] = True

        op = self.q._ops[tok]
        op.fn = _fn

    @precondition(lambda self: bool(self.state))
    @rule(data=st.data())
    def cancel_random(self, data) -> None:
        tok = data.draw(st.sampled_from(sorted(self.state)))
        was_pending = not self.state[tok]["cancelled"] and not self.state[tok]["fired"]
        result = self.q.cancel(tok)
        if was_pending:
            assert result is True
            self.state[tok]["cancelled"] = True
        else:
            assert result is False

    @rule()
    def fire_all_now(self) -> None:
        outstanding = {t for t, s in self.state.items() if not s["cancelled"] and not s["fired"]}
        self.q.fire_all_now()
        for t in outstanding:
            self.state[t]["fired"] = True
            assert self.state[t]["ran"], f"fire_all_now missed {t}"
        for t, s in self.state.items():
            if s["cancelled"]:
                assert not s["ran"], f"cancelled {t} had fn run"

    @invariant()
    def depth_consistent(self) -> None:
        outstanding = sum(1 for s in self.state.values() if not s["cancelled"] and not s["fired"])
        assert self.q.depth() == outstanding


TestUndoQueueStateMachine = _UndoQueueMachine.TestCase
TestUndoQueueStateMachine.settings = settings(
    max_examples=50,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def test_undo_timer_cancel_no_race() -> None:
    """A cancel before the timer fires must prevent the callback running."""
    from skiff.undo import UndoQueue

    q = UndoQueue(delay_secs=0.05)
    ran = Event()
    tok = q.enqueue("container", "x", ran.set)
    assert tok is not None
    assert q.cancel(tok) is True
    ran.wait(timeout=0.2)
    assert not ran.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# Parser invariants using shared strategies
# ─────────────────────────────────────────────────────────────────────────────


@given(q=memory_quantity_st())
@settings(max_examples=300, deadline=None)
def test_shared_memory_strategy_always_parses(q: str) -> None:
    """Any value our shared strategy generates must parse cleanly."""
    from skiff.validators import parse_memory_quantity

    result = parse_memory_quantity(q)
    assert isinstance(result, int)
    assert result >= 0


@given(q=cpu_quantity_st())
@settings(max_examples=300, deadline=None)
def test_shared_cpu_strategy_always_parses(q: str) -> None:
    from skiff.validators import parse_cpu_quantity

    result = parse_cpu_quantity(q)
    assert result >= 0


@given(n=st.integers(min_value=0, max_value=10**15))
@settings(max_examples=100, deadline=None)
def test_memory_int_identity(n: int) -> None:
    from skiff.validators import parse_memory_quantity

    assert parse_memory_quantity(n) == n


@given(
    n=st.integers(min_value=0, max_value=10**9),
    unit=st.sampled_from(
        [
            ("Ki", 1024),
            ("Mi", 1024**2),
            ("Gi", 1024**3),
            ("k", 1000),
            ("M", 1000**2),
            ("G", 1000**3),
        ]
    ),
)
@settings(max_examples=100, deadline=None)
def test_memory_multiplier_exact(n: int, unit: tuple[str, int]) -> None:
    from skiff.validators import parse_memory_quantity

    suffix, mult = unit
    assert parse_memory_quantity(f"{n}{suffix}") == n * mult


# ─────────────────────────────────────────────────────────────────────────────
# Compose YAML robustness using shared strategies
# ─────────────────────────────────────────────────────────────────────────────


@given(body=compose_yaml_body_st())
@settings(max_examples=30, deadline=None)
def test_shared_compose_body_never_crashes(body: bytes) -> None:
    from skiff.validators import validate_compose_file

    try:
        validate_compose_file(body)
    except HTTPException as exc:
        assert exc.status_code == 400
    except Exception as exc:
        pytest.fail(f"validator raised {type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# ID / name round-trip sanity
# ─────────────────────────────────────────────────────────────────────────────


@given(cid=container_id_st())
@settings(max_examples=200, deadline=None)
def test_container_id_strategy_always_passes_validator(cid: str) -> None:
    from skiff.validators import validate_container_id

    assert validate_container_id(cid) == cid


@given(pname=project_name_st())
@settings(max_examples=200, deadline=None)
def test_project_name_strategy_always_passes_validator(pname: str) -> None:
    from skiff.validators import validate_project_name

    assert validate_project_name(pname) == pname
