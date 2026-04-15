"""Tests for docker client singleton logic and safe_docker_call."""

import time
from unittest.mock import MagicMock, patch

import docker.errors
import pytest
import requests.exceptions
from fastapi import HTTPException

import app as app_module
from app import (
    _ws_acquire,
    _ws_release,
    docker_client_dep,
    get_client,
    safe_docker_call,
)

# ── get_client: backoff when _client is None ──────────────────────────────────

def test_get_client_in_backoff_raises():
    """When client is None and failed recently, raises DockerException (backoff)."""
    with (
        patch.object(app_module, "_client", None),
        patch.object(app_module, "_client_failed_at", time.monotonic()),  # failed just now
    ):
        with pytest.raises(docker.errors.DockerException):
            get_client()


def test_get_client_builds_when_none_and_backoff_expired():
    """When client is None and backoff has expired, builds a new client."""
    mock_new = MagicMock()
    mock_new.ping.return_value = True
    with (
        patch.object(app_module, "_client", None),
        patch.object(app_module, "_client_failed_at", 0.0),
        patch.object(app_module, "_client_last_ping", 0.0),
        patch("app._build_client", return_value=mock_new),
    ):
        result = get_client()
        assert result is mock_new


def test_get_client_build_failure_sets_failed_at():
    """When _build_client raises, _client_failed_at is updated."""
    with (
        patch.object(app_module, "_client", None),
        patch.object(app_module, "_client_failed_at", 0.0),
        patch.object(app_module, "_client_last_ping", 0.0),
        patch("app._build_client", side_effect=Exception("connection refused")),
    ):
        with pytest.raises(Exception, match="connection refused"):
            get_client()
        assert app_module._client is None


def test_get_client_ping_ttl_skips_ping():
    """When _client exists and last ping is within TTL, skip ping and return client."""
    mock_client = MagicMock()
    with (
        patch.object(app_module, "_client", mock_client),
        patch.object(app_module, "_client_last_ping", time.monotonic()),  # just now
    ):
        result = get_client()
        assert result is mock_client
        mock_client.ping.assert_not_called()


def test_get_client_ping_stale_invalidates():
    """When _client exists but ping fails, client is invalidated."""
    mock_client = MagicMock()
    mock_client.ping.side_effect = Exception("timeout")
    mock_new = MagicMock()
    mock_new.ping.return_value = True

    with (
        patch.object(app_module, "_client", mock_client),
        patch.object(app_module, "_client_last_ping", 0.0),  # very stale
        patch.object(app_module, "_client_failed_at", 0.0),
        patch("app._build_client", return_value=mock_new),
    ):
        result = get_client()
        assert result is mock_new


# ── docker_client_dep ─────────────────────────────────────────────────────────

def test_docker_client_dep_raises_503_on_failure():
    """docker_client_dep converts exceptions to 503."""
    with patch("app.get_client", side_effect=Exception("no docker")):
        with pytest.raises(HTTPException) as exc_info:
            docker_client_dep()
        assert exc_info.value.status_code == 503


def test_docker_client_dep_returns_client():
    """docker_client_dep returns client on success."""
    mock = MagicMock()
    with patch("app.get_client", return_value=mock):
        result = docker_client_dep()
        assert result is mock


# ── safe_docker_call ──────────────────────────────────────────────────────────

def test_safe_docker_call_success():
    fn = MagicMock(return_value="result")
    assert safe_docker_call(fn) == "result"


def test_safe_docker_call_not_found_raises_404():
    fn = MagicMock(side_effect=docker.errors.NotFound("missing"))
    with pytest.raises(HTTPException) as exc_info:
        safe_docker_call(fn)
    assert exc_info.value.status_code == 404


def test_safe_docker_call_api_error_409():
    resp_mock = MagicMock()
    resp_mock.status_code = 409
    resp_mock.reason = "Conflict"
    err = docker.errors.APIError("conflict", response=resp_mock, explanation="already started")
    fn = MagicMock(side_effect=err)
    with pytest.raises(HTTPException) as exc_info:
        safe_docker_call(fn)
    assert exc_info.value.status_code == 409


def test_safe_docker_call_api_error_other():
    resp_mock = MagicMock()
    resp_mock.status_code = 400
    resp_mock.reason = "Bad Request"
    err = docker.errors.APIError("bad request", response=resp_mock, explanation="some error")
    fn = MagicMock(side_effect=err)
    with pytest.raises(HTTPException) as exc_info:
        safe_docker_call(fn)
    assert exc_info.value.status_code == 400


def test_safe_docker_call_connection_error_retries_then_503():
    fn = MagicMock(side_effect=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(HTTPException) as exc_info:
        safe_docker_call(fn)
    assert exc_info.value.status_code == 503
    assert fn.call_count == 2  # retried once


def test_safe_docker_call_timeout_error_retries_then_503():
    fn = MagicMock(side_effect=requests.exceptions.Timeout("timed out"))
    with pytest.raises(HTTPException) as exc_info:
        safe_docker_call(fn)
    assert exc_info.value.status_code == 503


# ── _ws_acquire / _ws_release ─────────────────────────────────────────────────

def test_ws_acquire_and_release():
    ip = "10.0.0.99"
    # reset
    from app import _ws_connections
    _ws_connections[ip] = 0

    _ws_acquire(ip)
    assert _ws_connections[ip] == 1
    _ws_release(ip)
    assert _ws_connections[ip] == 0


def test_ws_acquire_too_many_raises_429():
    ip = "10.0.0.88"
    from skiff.app import WS_MAX_PER_IP, _ws_connections
    _ws_connections[ip] = WS_MAX_PER_IP

    with pytest.raises(HTTPException) as exc_info:
        _ws_acquire(ip)
    assert exc_info.value.status_code == 429
    _ws_connections[ip] = 0


def test_ws_release_floor_at_zero():
    ip = "10.0.0.77"
    from app import _ws_connections
    _ws_connections[ip] = 0
    _ws_release(ip)
    assert _ws_connections[ip] == 0


# ── _audit_file_sink OSError silently swallowed ────────────────────────────────

def test_audit_file_sink_oserror_swallowed():
    from app import _audit_file_sink
    with patch("builtins.open", side_effect=OSError("disk full")):
        # Should not raise
        result = _audit_file_sink(None, None, {"event": "test", "severity": "INFO"})
        assert result is not None
