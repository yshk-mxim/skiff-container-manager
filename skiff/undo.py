# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Short-window undo queue for destructive operations.

A DELETE endpoint opting into `?undo=true` enqueues the Docker-SDK
call here and returns an undo token; a `threading.Timer` fires the
call after `config.UNDO_DELAY_SECS`. `POST /api/undo/<token>` cancels
before the timer fires. The queue is in-memory only — a crash during
the grace window loses the pending op, which is the fail-safe outcome
(the resource stays).

Security + correctness invariants:
- Tokens are `secrets.token_urlsafe(16)` (22 chars, unguessable).
- Timer callbacks invoke the stored fn OUTSIDE the lock, so a slow
  Docker call never blocks other queue operations.
- Cancel is idempotent: a second cancel returns False.
- Enqueues past `config.UNDO_QUEUE_MAX_DEPTH` are refused at the
  caller, which falls back to synchronous execution — fail-closed,
  never silently dropped.
"""
from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from skiff import config

log = structlog.get_logger(__name__)


@dataclass
class _PendingOp:
    token: str
    kind: str                # "container" | "image" | "volume"
    resource_id: str         # short id / name for audit log
    fn: Callable[..., Any]   # the Docker SDK call to perform on firing
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    timer: threading.Timer | None = None
    # True once the timer has fired and the op has run (or raised).
    fired: bool = False


class UndoQueue:
    """Token-keyed queue of delayed operations. One instance per process."""

    def __init__(self, delay_secs: float | None = None) -> None:
        delay_secs = config.UNDO_DELAY_SECS if delay_secs is None else delay_secs
        self._delay = delay_secs
        self._lock = threading.Lock()
        self._ops: dict[str, _PendingOp] = {}
        self._fire_failures: int = 0

    def enqueue(
        self, kind: str, resource_id: str,
        fn: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> str | None:
        """Schedule `fn(*args, **kwargs)` to run after the undo window elapses.

        Returns the undo_token on success, or None if the queue is full (caller
        should fall back to synchronous execution). Tokens are cryptographically
        random; cannot be guessed or enumerated.
        """
        token = secrets.token_urlsafe(16)
        with self._lock:
            if len(self._ops) >= config.UNDO_QUEUE_MAX_DEPTH:
                log.warning("undo.queue_full", depth=len(self._ops), kind=kind)
                return None
            op = _PendingOp(token=token, kind=kind, resource_id=resource_id,
                            fn=fn, args=args, kwargs=kwargs)
            t = threading.Timer(self._delay, self._fire, args=(token,))
            t.daemon = True
            op.timer = t
            self._ops[token] = op
        t.start()
        log.info("undo.enqueued", token_suffix=token[-6:], kind=kind, id=resource_id,
                 expires_in=self._delay)
        return token

    def _fire(self, token: str) -> None:
        """Timer callback. Removes the entry, then runs the callable outside the lock."""
        with self._lock:
            op = self._ops.pop(token, None)
        if op is None:
            return  # cancelled between timer schedule and fire
        op.fired = True
        # Intentionally broad: this runs on a Timer thread after the client
        # has already received 200. Any unhandled exception would crash the
        # thread and leave the undo op silently dropped with no audit trail.
        # We log with enough context for forensics; operators can re-run
        # manually from the audit log.
        try:
            op.fn(*op.args, **op.kwargs)
            log.info("undo.fired", token_suffix=token[-6:], kind=op.kind, id=op.resource_id)
        except Exception as exc:
            # A failed fire leaves the resource in an unknown state. Bump a
            # process-local counter so the health endpoint can surface the
            # failure count without having to tail the audit log.
            with self._lock:
                self._fire_failures += 1
            log.error("undo.fire_failed", token_suffix=token[-6:], kind=op.kind,
                      id=op.resource_id, error=str(exc))

    def cancel(self, token: str) -> bool:
        """Cancel a pending op. Returns True if there was something to cancel."""
        with self._lock:
            op = self._ops.pop(token, None)
        if op is None:
            return False
        if op.timer is not None:
            op.timer.cancel()
        log.info("undo.cancelled", token_suffix=token[-6:], kind=op.kind, id=op.resource_id)
        return True

    def depth(self) -> int:
        """Current number of pending operations. Exposed for rate-limit guards + tests."""
        with self._lock:
            return len(self._ops)

    def fire_failures(self) -> int:
        """Count of undo ops whose scheduled action raised on fire.

        Surfaced by `/ready` so operators monitoring readiness probes see a
        non-zero value when a destructive rollback failed after the client
        already received 200.
        """
        with self._lock:
            return self._fire_failures

    def fire_all_now(self) -> None:
        """Test / shutdown helper: cancel every Timer and run every pending op immediately.

        Emits one `undo.fired_on_shutdown` audit entry per op so an
        incident reviewer can tell a clean-shutdown flush apart from a
        scheduled-timer fire — SIGTERM-flushed destructive rollbacks
        would otherwise be invisible in the audit log.
        """
        with self._lock:
            pending = list(self._ops.values())
            self._ops.clear()
        for op in pending:
            if op.timer is not None:
                op.timer.cancel()
            # Intentionally broad: test/shutdown helper. One failing op must
            # not block the rest. Errors go to `undo.fire_failed` at the
            # natural catch site.
            try:
                op.fn(*op.args, **op.kwargs)
                log.info(
                    "undo.fired_on_shutdown",
                    token_suffix=op.token[-6:], kind=op.kind, id=op.resource_id,
                )
            except Exception as exc:
                with self._lock:
                    self._fire_failures += 1
                log.error(
                    "undo.fire_failed",
                    token_suffix=op.token[-6:], kind=op.kind,
                    id=op.resource_id, error=str(exc),
                )


# Module-level singleton. One queue per process.
_undo_queue = UndoQueue()


def get_queue() -> UndoQueue:
    return _undo_queue
