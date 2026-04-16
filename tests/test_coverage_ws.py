# SPDX-License-Identifier: MIT
"""WebSocket handler tests to reach 100% coverage on containers.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app as app_module
from skiff.routers.containers import exec_shell, run_container, stream_logs

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ws(disconnect_on_receive: bool = True, yield_before_disconnect: bool = False) -> AsyncMock:
    """Minimal WebSocket double for async tests.

    yield_before_disconnect=True makes receive_text sleep briefly (1 ms) before
    raising, giving asyncio.create_task() coroutines — which use run_in_executor
    internally — enough event-loop iterations to complete before the outer loop
    exits.
    """
    ws = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    ws.headers = {"origin": "http://127.0.0.1:8080", "host": "127.0.0.1:8080"}
    if disconnect_on_receive:
        if yield_before_disconnect:
            async def _sleep_then_raise():
                await asyncio.sleep(0.002)  # 2 ms — enough for thread-pool MagicMock calls
                raise RuntimeError("client disconnect")
            ws.receive_text = _sleep_then_raise
        else:
            ws.receive_text = AsyncMock(side_effect=RuntimeError("client disconnect"))
    return ws


# ── stream_logs WebSocket ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_logs_idle_timeout_message():
    """TimeoutError in read_logs sends idle-timeout message and exits the read loop."""
    ws = _make_ws(disconnect_on_receive=False)
    # receive_text sleeps briefly so that the read_logs task gets at least one
    # full event-loop cycle (including the thread-pool executor round-trip) before
    # the outer loop sees the disconnect.
    async def _sleep_then_raise():
        await asyncio.sleep(0.002)
        raise RuntimeError("client disconnect")
    ws.receive_text = _sleep_then_raise

    mock_container = MagicMock()
    mock_container.logs.return_value = MagicMock()
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=mock_client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
        # Force wait_for inside read_logs to raise TimeoutError immediately
        patch("skiff.routers.containers.asyncio.wait_for", side_effect=TimeoutError()),
    ):
        mock_re.match.return_value = MagicMock()
        await stream_logs(ws, "abc1234567890123")

    ws.send_text.assert_any_call("\n[Idle timeout — no new logs for 5 minutes]\n")


@pytest.mark.asyncio
async def test_stream_logs_gen_close_exception_swallowed():
    """RuntimeError from gen.close() in stream_logs finally is swallowed."""
    ws = _make_ws()
    mock_gen = MagicMock()
    # next(gen, None) returns None → read_logs breaks immediately
    mock_gen.__next__ = MagicMock(side_effect=StopIteration)
    mock_gen.close = MagicMock(side_effect=RuntimeError("close failed"))

    mock_container = MagicMock()
    mock_container.logs.return_value = mock_gen
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=mock_client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await stream_logs(ws, "abc1234567890123")  # must not raise


@pytest.mark.asyncio
async def test_stream_logs_ws_close_exception_swallowed():
    """Exception from websocket.close() in the outer finally of stream_logs is swallowed."""
    ws = _make_ws()
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))

    mock_gen = MagicMock()
    mock_gen.__next__ = MagicMock(side_effect=StopIteration)
    mock_container = MagicMock()
    mock_container.logs.return_value = mock_gen
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=mock_client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await stream_logs(ws, "abc1234567890123")  # must not raise


# ── exec_shell WebSocket ──────────────────────────────────────────────────────


def _make_exec_mock_client(bash_exit_code: int = 0, recv_side_effect=None):
    """Build a mock Docker client wired for exec_shell tests."""
    sock_mock = MagicMock()
    sock_mock._sock = MagicMock()
    if recv_side_effect is not None:
        sock_mock._sock.recv.side_effect = recv_side_effect
    else:
        # Default: return some data once, then return b"" to break read loop
        sock_mock._sock.recv.side_effect = [b"shell output\n", b""]

    container = MagicMock()
    container.exec_run.return_value = (bash_exit_code, b"")
    container.id = "abc123id"

    client = MagicMock()
    client.containers.get.return_value = container
    client.api.exec_create.return_value = {"Id": "execid123"}
    client.api.exec_start.return_value = sock_mock
    return client, sock_mock


@pytest.mark.asyncio
async def test_exec_shell_bash_detection_exception():
    """Exception during exec_run('which bash') is swallowed; falls back to /bin/sh."""
    ws = _make_ws()
    client, _sock = _make_exec_mock_client()
    client.containers.get.return_value.exec_run.side_effect = RuntimeError("exec failed")

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await exec_shell(ws, "abc1234567890123")

    # exec_create was called (handler proceeded with /bin/sh fallback)
    client.api.exec_create.assert_called_once()
    call_args = client.api.exec_create.call_args
    assert call_args[0][1] == "/bin/sh"


@pytest.mark.asyncio
async def test_exec_shell_read_output_data_sent():
    """Data received from exec socket is forwarded to the WebSocket client."""
    ws = _make_ws(yield_before_disconnect=True)
    client, _ = _make_exec_mock_client(recv_side_effect=[b"hello\n", b""])

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await exec_shell(ws, "abc1234567890123")

    ws.send_text.assert_any_call("hello\n")


@pytest.mark.asyncio
async def test_exec_shell_read_output_idle_timeout():
    """TimeoutError with idle time exceeding WS_EXEC_IDLE_TIMEOUT sends timeout message."""
    ws = _make_ws(yield_before_disconnect=True)
    client, _sock = _make_exec_mock_client()
    # Make recv always raise TimeoutError so the idle check triggers
    _sock._sock.recv.side_effect = TimeoutError()
    _sock._sock.settimeout = MagicMock()
    _sock._sock.setblocking = MagicMock()

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
        # Set idle timeout to 0 so the very first TimeoutError triggers the message
        patch("skiff.routers.containers.WS_EXEC_IDLE_TIMEOUT", 0),
    ):
        mock_re.match.return_value = MagicMock()
        await exec_shell(ws, "abc1234567890123")

    ws.send_text.assert_any_call("\r\n[Session idle timeout — 10 minutes]\r\n")


@pytest.mark.asyncio
async def test_exec_shell_read_output_exception_breaks():
    """Non-Timeout exception from socket recv causes read_output to break."""
    ws = _make_ws(yield_before_disconnect=True)
    client, _sock = _make_exec_mock_client()
    _sock._sock.recv.side_effect = ConnectionResetError("peer reset")

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await exec_shell(ws, "abc1234567890123")  # must not raise


@pytest.mark.asyncio
async def test_exec_shell_large_input_closes_4008():
    """Input message >65536 bytes causes ws.close(code=4008) and exits the send loop."""
    ws = _make_ws(disconnect_on_receive=False)
    large_payload = "x" * 65537
    ws.receive_text = AsyncMock(side_effect=[large_payload, Exception("done")])

    client, _sock = _make_exec_mock_client(recv_side_effect=[b""])

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await exec_shell(ws, "abc1234567890123")

    ws.close.assert_any_call(code=4008)


@pytest.mark.asyncio
async def test_exec_shell_ws_close_exception_swallowed():
    """Exception from ws.close() in exec_shell outer finally is swallowed."""
    ws = _make_ws()
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))

    client, _sock = _make_exec_mock_client(recv_side_effect=[b""])

    with (
        patch("skiff.routers.containers._validate_ws_origin", return_value=True),
        patch(
            "skiff.routers.containers._validate_ws_token_from_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("skiff.routers.containers.get_client", return_value=client),
        patch("skiff.routers.containers.CONTAINER_ID_RE") as mock_re,
        patch("skiff.routers.containers.ws_keepalive", new_callable=AsyncMock),
    ):
        mock_re.match.return_value = MagicMock()
        await exec_shell(ws, "abc1234567890123")  # must not raise


# ── run_container: port value as tuple (line 136-137 defensive branch) ───────


def test_run_container_port_as_tuple(mock_docker):
    """Port value as (host_ip, host_port) tuple covers the isinstance list/tuple branch."""
    from fastapi import Request

    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.headers = {"X-Requested-With": "ContainerManager"}

    mock_container = MagicMock()
    mock_container.short_id = "abc123"
    mock_container.name = "test-container"
    mock_container.status = "running"
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = mock_container

    # Pass a port value as a 2-tuple — this is the (host_ip, host_port) format
    # that some Docker SDK wrappers return; the defensive branch at line 136 handles it.
    # All Body-annotated parameters must be passed explicitly (not left at their
    # Body(default=None) defaults) when calling the function directly.
    with patch.object(app_module._cfg, "allowed_registries", ["docker.io"]):
        result = run_container(
            request=mock_request,
            image="docker.io/library/nginx:latest",
            ports={"8080/tcp": ("127.0.0.1", "8080")},  # tuple value triggers line 136-137
            environment=None,
            command=None,
            volumes=None,
            restart_policy=None,
            network=None,
            labels=None,
            read_only=True,
            client=mock_docker,
        )

    assert result["id"] == "abc123"
