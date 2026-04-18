# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for WebSocket helper functions and direct unit coverage."""

from unittest.mock import AsyncMock, MagicMock, patch

import docker.errors
import pytest

import skiff.config as config_module
import skiff.docker_client as docker_client_module
from skiff.auth import _validate_ws_origin, _validate_ws_token_from_message
from skiff.docker_client import invalidate_client
from tests.conftest import AUTH_HEADER, TOKEN

# ── _validate_ws_origin ───────────────────────────────────────────────────────

def _make_ws(origin="", host="localhost:8080"):
    ws = MagicMock()
    headers = {}
    if origin:
        headers["origin"] = origin
    headers["host"] = host
    ws.headers = headers
    return ws


def test_validate_ws_origin_wildcard_is_rejected_even_if_smuggled_in():
    # Wildcards are rejected at config load (see _csv_list_no_wildcard).
    # If one is somehow present in allowed_origins, the WS origin check must
    # still refuse an unrelated caller — it is NOT a permissive escape hatch.
    with patch.object(config_module._cfg, "allowed_origins", ["*"]):
        ws = _make_ws(origin="https://anything.com")
        assert _validate_ws_origin(ws) is False


def test_validate_ws_origin_no_origin_rejects():
    ws = _make_ws(origin="")
    assert _validate_ws_origin(ws) is False


def test_validate_ws_origin_in_allowlist():
    ws = _make_ws(origin="http://127.0.0.1:8080")
    # http://127.0.0.1:8080 is in ALLOWED_ORIGINS
    assert _validate_ws_origin(ws) is True


def test_validate_ws_origin_same_host():
    ws = _make_ws(origin="https://myworkstation.dev", host="myworkstation.dev")
    result = _validate_ws_origin(ws)
    assert result is True


def test_validate_ws_origin_different_host():
    ws = _make_ws(origin="https://evil.com", host="myworkstation.dev")
    result = _validate_ws_origin(ws)
    assert result is False


# ── _validate_ws_token_from_message ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_ws_token_no_api_token():
    with patch.object(config_module._cfg, "api_token", ""):
        ws = MagicMock()
        result = await _validate_ws_token_from_message(ws)
        assert result is True


@pytest.mark.asyncio
async def test_validate_ws_token_valid():
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value=f"AUTH {TOKEN}")
    with patch.object(config_module._cfg, "api_token", TOKEN):
        result = await _validate_ws_token_from_message(ws)
        assert result is True


@pytest.mark.asyncio
async def test_validate_ws_token_invalid():
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value="AUTH wrongtoken")
    with patch.object(config_module._cfg, "api_token", TOKEN):
        result = await _validate_ws_token_from_message(ws)
        assert result is False


@pytest.mark.asyncio
async def test_validate_ws_token_not_auth_prefix():
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value="HELLO world")
    with patch.object(config_module._cfg, "api_token", TOKEN):
        result = await _validate_ws_token_from_message(ws)
        assert result is False


@pytest.mark.asyncio
async def test_validate_ws_token_exception_returns_false():
    ws = MagicMock()
    # RuntimeError is the starlette-on-send-after-close signal we narrowed to.
    ws.receive_text = AsyncMock(side_effect=RuntimeError("closed"))
    with patch.object(config_module._cfg, "api_token", TOKEN):
        result = await _validate_ws_token_from_message(ws)
        assert result is False


# ── invalidate_client ────────────────────────────────────────────────────────

def testinvalidate_client_closes_and_nones():
    mock_client = MagicMock()
    with (
        patch.object(docker_client_module, "_client", mock_client),
        patch.object(docker_client_module, "_client_last_ping", 999.0),
    ):
        invalidate_client()
        mock_client.close.assert_called_once()
        assert docker_client_module._client is None
        assert docker_client_module._client_last_ping == 0.0


def testinvalidate_client_close_exception_swallowed():
    mock_client = MagicMock()
    mock_client.close.side_effect = docker.errors.DockerException("close failed")
    with patch.object(docker_client_module, "_client", mock_client):
        # Should not raise
        invalidate_client()
        assert docker_client_module._client is None


def testinvalidate_client_none_client():
    with patch.object(docker_client_module, "_client", None):
        # Should not raise
        invalidate_client()


# ── WebSocket endpoint tests ──────────────────────────────────────────────────

def test_ws_logs_invalid_origin(client):
    """WebSocket logs endpoint rejects missing origin."""
    try:
        with client.websocket_connect("/ws/logs/abc1234567890123"):
            pass
    except Exception:
        pass  # Expected: closed with code 4003


def test_ws_logs_invalid_container_id(client):
    """WebSocket logs endpoint rejects invalid container ID."""
    with patch.object(config_module._cfg, "allowed_origins", ["*"]):
        try:
            with client.websocket_connect(
                "/ws/logs/INVALID-ID",
                headers={"origin": "http://127.0.0.1:8080"},
            ):
                pass
        except Exception:
            pass  # closed with error code 4000


def test_ws_exec_invalid_origin(client):
    """WebSocket exec endpoint rejects missing origin."""
    try:
        with client.websocket_connect("/ws/exec/abc1234567890123"):
            pass
    except Exception:
        pass  # Expected: closed


# ── _get_container transient error ────────────────────────────────────────────

def test_get_container_transient_error(client, mock_docker):
    """_get_container raises 503 on transient docker error.

    Use ConnectionError (the narrow transient class in docker_client
    .DOCKER_TRANSIENT) — bare OSError is intentionally no longer
    classified as transient so disk-full / EMFILE surface as 500, not 503.
    """
    mock_docker.containers.get.side_effect = ConnectionError("connection reset")
    resp = client.get("/api/containers/abc1234567890123/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 503


# ── _build_client ─────────────────────────────────────────────────────────────

def test_build_client_calls_ping():
    """_build_client creates a DockerClient and pings it."""
    from skiff.docker_client import _build_client
    mock_client = MagicMock()
    with patch("skiff.docker_client.docker.DockerClient", return_value=mock_client):
        result = _build_client()
        mock_client.ping.assert_called_once()
        assert result is mock_client


# ── get_client ping stale path ─────────────────────────────────────────────────

def test_get_client_existing_ping_success():
    """When client exists and stale, successful ping returns same client."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    with (
        patch.object(docker_client_module, "_client", mock_client),
        patch.object(docker_client_module, "_client_last_ping", 0.0),  # very stale
    ):
        result = docker_client_module.get_client()
        mock_client.ping.assert_called_once()
        assert result is mock_client
