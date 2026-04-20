# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for the R7 undo queue (skiff/undo.py) and the DELETE ?undo=1 flow."""

import time
from unittest.mock import MagicMock

import skiff.undo as undo_module
from skiff import config
from skiff.undo import UndoQueue
from tests.conftest import AUTH_CSRF

# ── UndoQueue unit tests ──────────────────────────────────────────────────────


def test_undo_queue_fires_after_delay():
    q = UndoQueue(delay_secs=0.05)
    marker = []
    token = q.enqueue("container", "abc", lambda: marker.append(1))
    assert token is not None
    assert q.depth() == 1
    time.sleep(0.15)
    assert marker == [1]
    assert q.depth() == 0


def test_undo_queue_cancel_prevents_fire():
    q = UndoQueue(delay_secs=0.1)
    marker = []
    token = q.enqueue("image", "img1", lambda: marker.append(1))
    assert q.cancel(token) is True
    time.sleep(0.15)
    assert marker == []
    assert q.depth() == 0


def test_undo_queue_cancel_unknown_token_returns_false():
    q = UndoQueue(delay_secs=0.05)
    assert q.cancel("nonexistent-token") is False


def test_undo_queue_cancel_is_idempotent():
    """Double-cancel returns False the second time — no double-free, no crash."""
    q = UndoQueue(delay_secs=0.2)
    token = q.enqueue("volume", "vol1", lambda: None)
    assert q.cancel(token) is True
    assert q.cancel(token) is False


def test_undo_queue_full_returns_none():
    q = UndoQueue(delay_secs=60)  # long enough that entries don't fire mid-test
    try:
        # Fill to capacity
        for i in range(config.UNDO_QUEUE_MAX_DEPTH):
            tok = q.enqueue("container", f"c{i}", lambda: None)
            assert tok is not None
        # Next enqueue is rejected
        assert q.enqueue("container", "overflow", lambda: None) is None
    finally:
        q.fire_all_now()  # cancel all timers so the test suite exits cleanly


def test_undo_queue_fire_exception_doesnt_propagate():
    """If the enqueued callable raises when fired, the error is logged but
    doesn't crash the process — the client already got 200."""
    q = UndoQueue(delay_secs=0.05)

    def boom():
        raise RuntimeError("simulated Docker SDK failure")

    q.enqueue("container", "c1", boom)
    time.sleep(0.15)
    # No crash; queue is empty
    assert q.depth() == 0


def test_undo_token_is_opaque_and_nonenumerable():
    """Tokens should be 22-char url-safe base64 — cannot be guessed."""
    q = UndoQueue(delay_secs=60)
    try:
        tokens = {q.enqueue("container", f"c{i}", lambda: None) for i in range(10)}
        # All unique
        assert len(tokens) == 10
        # All correct shape
        import re

        for t in tokens:
            assert re.fullmatch(r"[A-Za-z0-9_\-]{22}", t), f"bad token shape: {t!r}"
    finally:
        q.fire_all_now()


# ── /api/undo/{token} endpoint tests ──────────────────────────────────────────


def test_undo_endpoint_cancels(client, mock_docker, monkeypatch):
    """DELETE ?undo=1 returns undo_token; POSTing it cancels the fire."""
    # Use a real UndoQueue but with a long delay, so tests never race
    test_queue = UndoQueue(delay_secs=60)
    monkeypatch.setattr(undo_module, "_undo_queue", test_queue)

    c = MagicMock()
    c.short_id = "abc123def456"
    c.remove = MagicMock()
    mock_docker.containers.get.return_value = c

    resp = client.delete("/api/containers/abc123def456?undo=1", headers=AUTH_CSRF)
    assert resp.status_code == 200
    token = resp.json().get("undo_token")
    assert token
    assert test_queue.depth() == 1
    # remove() must NOT have fired synchronously
    c.remove.assert_not_called()

    # Cancel via the undo endpoint
    resp2 = client.post(f"/api/undo/{token}", headers=AUTH_CSRF)
    assert resp2.status_code == 200
    assert resp2.json() == {"ok": True, "cancelled": True}
    assert test_queue.depth() == 0
    # Still never fired
    c.remove.assert_not_called()
    test_queue.fire_all_now()


def test_undo_endpoint_requires_auth(client):
    resp = client.post("/api/undo/some-token")
    assert resp.status_code == 401


def test_undo_endpoint_requires_csrf(client, mock_docker):
    """POST /api/undo/{token} with Bearer but no X-Requested-With → 403."""
    resp = client.post("/api/undo/some-token", headers={"Authorization": AUTH_CSRF["Authorization"]})
    assert resp.status_code == 403


def test_undo_endpoint_rejects_bad_token_format(client):
    """Reject anything that isn't base64url-chars to keep garbage out of the queue map."""
    resp = client.post("/api/undo/has%20space", headers=AUTH_CSRF)
    assert resp.status_code == 400


def test_undo_endpoint_unknown_token_returns_cancelled_false(client):
    """An unknown token isn't an error — it's just a no-op. Idempotent semantics."""
    resp = client.post("/api/undo/abcdef1234567890ABCDEF", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cancelled": False}


def test_delete_without_undo_is_synchronous(client, mock_docker):
    """DELETE with explicit `undo=false` removes immediately, no undo_token.

    The default changed to `undo=true` so a misclick is recoverable; a
    script that wants the old hard-delete semantics opts in with
    `?undo=false`.
    """
    c = MagicMock()
    c.short_id = "abc123def456"
    c.remove = MagicMock()
    mock_docker.containers.get.return_value = c

    resp = client.delete("/api/containers/abc123def456?undo=false", headers=AUTH_CSRF)
    assert resp.status_code == 200
    assert "undo_token" not in resp.json()
    c.remove.assert_called_once()


def test_delete_with_undo_falls_back_if_queue_full(client, mock_docker, monkeypatch):
    """When the queue can't accept more entries, the delete proceeds
    synchronously — we never silently swallow a delete request."""
    test_queue = UndoQueue(delay_secs=60)
    # Fill to max
    for _ in range(config.UNDO_QUEUE_MAX_DEPTH):
        test_queue.enqueue("container", "x", lambda: None)
    monkeypatch.setattr(undo_module, "_undo_queue", test_queue)
    try:
        c = MagicMock()
        c.short_id = "abc123def456"
        c.remove = MagicMock()
        mock_docker.containers.get.return_value = c

        resp = client.delete("/api/containers/abc123def456?undo=1", headers=AUTH_CSRF)
        assert resp.status_code == 200
        assert "undo_token" not in resp.json()
        c.remove.assert_called_once()  # synchronous fallback ran
    finally:
        test_queue.fire_all_now()
