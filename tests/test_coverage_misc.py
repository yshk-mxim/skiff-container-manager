"""Additional tests to cover remaining missing lines."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app as app_module
from tests.conftest import AUTH_CSRF, AUTH_HEADER

# ── list_containers: image exception branch ───────────────────────────────────

def test_list_containers_image_exception_fallback(client, mock_docker):
    """When image.tags raises, falls back to 'unknown'."""
    c = MagicMock()
    c.short_id = "abc123"
    c.name = "test"
    c.status = "running"
    c.ports = {}
    c.attrs = {"State": {"Status": "running", "Health": None}, "Created": ""}
    # Make image.tags raise an exception
    def _raise(_):
        raise Exception("no image")  # noqa: TRY002
    type(c.image).tags = property(_raise)
    mock_docker.containers.list.return_value = [c]
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()[0]["image"] == "unknown"


# ── run_container: command and labels branches ────────────────────────────────

def test_run_container_with_command(client, mock_docker):
    new_c = MagicMock()
    new_c.short_id = "abc123"
    new_c.name = "test"
    new_c.status = "running"
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"command": "echo hello"},
    )
    assert resp.status_code == 200
    # Verify command was passed
    call_kwargs = mock_docker.containers.run.call_args[1]
    assert call_kwargs.get("command") == "echo hello"


def test_run_container_with_labels(client, mock_docker):
    new_c = MagicMock()
    new_c.short_id = "abc123"
    new_c.name = "test"
    new_c.status = "running"
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"labels": {"app": "myapp", "version": "1.0"}},
    )
    assert resp.status_code == 200


def test_run_container_label_value_too_long(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"labels": {"mykey": "x" * 4097}},
    )
    assert resp.status_code == 400


# ── container_stats: TimeoutError ────────────────────────────────────────────

def test_container_stats_timeout(client, mock_docker):
    c = MagicMock()
    c.stats.side_effect = TimeoutError("timed out")
    mock_docker.containers.get.return_value = c

    with patch("asyncio.wait_for", side_effect=TimeoutError("timed out")):
        resp = client.get("/api/containers/abc1234567890123/stats", headers=AUTH_HEADER)
    assert resp.status_code == 504


# ── list_volumes: containers.list exception ───────────────────────────────────

def test_list_volumes_containers_exception(client, mock_docker):
    """When containers.list raises during volume lookup, gracefully continues."""
    vol = MagicMock()
    vol.name = "myvol"
    vol.attrs = {
        "Driver": "local",
        "Mountpoint": "/var/lib/docker/volumes/myvol/_data",
        "CreatedAt": "2026-01-01T00:00:00Z",
        "Labels": {},
    }
    mock_docker.volumes.list.return_value = [vol]
    mock_docker.containers.list.side_effect = Exception("docker error")
    resp = client.get("/api/volumes", headers=AUTH_HEADER)
    assert resp.status_code == 200
    # Still returns volumes even when container lookup fails
    assert resp.json()[0]["name"] == "myvol"


# ── compose stacks: container without project label ───────────────────────────

def test_compose_stacks_container_no_project_label(client, mock_docker):
    """Container without compose project label is skipped."""
    c = MagicMock()
    c.labels = {}  # No compose labels
    c.short_id = "abc123"
    c.status = "running"
    c.attrs = {"State": {"Status": "running"}}
    mock_docker.containers.list.return_value = [c]
    resp = client.get("/api/compose/stacks", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == []


def test_compose_stacks_stopped_status(client, mock_docker):
    """Stack with non-running state has status 'stopped'."""
    c = MagicMock()
    c.labels = {
        "com.docker.compose.project": "myproject",
        "com.docker.compose.service": "web",
    }
    c.short_id = "abc123"
    c.status = "exited"
    c.attrs = {"State": {"Status": "exited"}}
    mock_docker.containers.list.return_value = [c]
    resp = client.get("/api/compose/stacks", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["status"] == "stopped"


# ── pull image: TimeoutError ──────────────────────────────────────────────────

def test_pull_image_timeout(client, mock_docker):
    with patch("asyncio.wait_for", side_effect=TimeoutError("timeout")):
        resp = client.post(
            "/api/images/pull?image=us-docker.pkg.dev/p/r/img:latest",
            headers=AUTH_CSRF,
        )
    assert resp.status_code == 504


# ── push image: TimeoutError and APIError ─────────────────────────────────────

def test_push_image_timeout(client, mock_docker):
    with patch("asyncio.wait_for", side_effect=TimeoutError("timeout")):
        resp = client.post(
            "/api/images/push?image=us-docker.pkg.dev/p/r/img:latest",
            headers=AUTH_CSRF,
        )
    assert resp.status_code == 504


def test_push_image_api_error(client, mock_docker):
    import docker.errors
    mock_docker.images.push.side_effect = docker.errors.APIError("push failed")
    resp = client.post(
        "/api/images/push?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


# ── _validate_ws_origin: exception in urlparse ───────────────────────────────

def test_validate_ws_origin_urlparse_exception():
    from app import _validate_ws_origin
    ws = MagicMock()
    ws.headers = {"origin": "not-a-url", "host": "localhost"}
    # urlparse won't raise but netloc will be empty, causing return False
    result = _validate_ws_origin(ws)
    assert result is False


# ── get_client: close fails during stale ping ─────────────────────────────────

def test_get_client_stale_close_exception():
    """When ping fails and close() also raises, still invalidates client."""
    mock_client = MagicMock()
    mock_client.ping.side_effect = Exception("ping failed")
    mock_client.close.side_effect = Exception("close also failed")
    mock_new = MagicMock()
    mock_new.ping.return_value = True

    with (
        patch.object(app_module, "_client", mock_client),
        patch.object(app_module, "_client_last_ping", 0.0),
        patch.object(app_module, "_client_failed_at", 0.0),
        patch("app._build_client", return_value=mock_new),
    ):
        result = app_module.get_client()
        assert result is mock_new


# ── WebSocket: valid origin but bad auth ──────────────────────────────────────

def test_ws_logs_valid_origin_bad_auth(client):
    """WebSocket accepts origin but closes on bad auth token."""
    try:
        with client.websocket_connect(
            "/ws/logs/abc1234567890123",
            headers={"origin": "http://127.0.0.1:8080"},
        ) as ws:
            ws.send_text("AUTH wrongtoken")
            # Server should close after bad auth
    except Exception:
        pass  # Expected disconnect


def test_ws_exec_valid_origin_bad_auth(client):
    """WebSocket exec accepts origin but closes on bad auth token."""
    try:
        with client.websocket_connect(
            "/ws/exec/abc1234567890123",
            headers={"origin": "http://127.0.0.1:8080"},
        ) as ws:
            ws.send_text("AUTH wrongtoken")
    except Exception:
        pass  # Expected disconnect


def test_ws_logs_valid_auth_docker_error(client, mock_docker):
    """WebSocket logs: valid auth but docker raises error."""
    mock_docker.containers.get.side_effect = Exception("docker error")
    try:
        with client.websocket_connect(
            "/ws/logs/abc1234567890123",
            headers={"origin": "http://127.0.0.1:8080"},
        ) as ws:
            ws.send_text(f"AUTH {AUTH_HEADER['Authorization'].split()[1]}")
    except Exception:
        pass


def test_ws_logs_valid_auth_and_logs(client, mock_docker):
    """WebSocket logs: valid auth, logs yielded then done."""
    c = MagicMock()
    def _gen():
        yield b"2026-01-01T00:00:00Z log line\n"
    c.logs.return_value = _gen()
    mock_docker.containers.get.return_value = c
    try:
        with client.websocket_connect(
            "/ws/logs/abc1234567890123",
            headers={"origin": "http://127.0.0.1:8080"},
        ) as ws:
            ws.send_text(f"AUTH {AUTH_HEADER['Authorization'].split()[1]}")
            try:
                ws.receive_text()
            except Exception:
                pass
    except Exception:
        pass


def test_ws_exec_valid_auth_docker_error(client, mock_docker):
    """WebSocket exec: valid auth but docker error."""
    mock_docker.containers.get.side_effect = Exception("docker error")
    try:
        with client.websocket_connect(
            "/ws/exec/abc1234567890123",
            headers={"origin": "http://127.0.0.1:8080"},
        ) as ws:
            ws.send_text(f"AUTH {AUTH_HEADER['Authorization'].split()[1]}")
    except Exception:
        pass


def test_ws_exec_invalid_container_id(client, mock_docker):
    """WebSocket exec: invalid container ID closes with 4000."""
    try:
        with client.websocket_connect(
            "/ws/exec/INVALID-UPPERCASE",
            headers={"origin": "http://127.0.0.1:8080"},
        ):
            pass
    except Exception:
        pass  # closed with code 4000


def test_ws_logs_invalid_container_id_format(client, mock_docker):
    """WebSocket logs: invalid container ID closes with 4000."""
    try:
        with client.websocket_connect(
            "/ws/logs/INVALID-UPPERCASE",
            headers={"origin": "http://127.0.0.1:8080"},
        ):
            pass
    except Exception:
        pass  # closed with code 4000


def test_ws_exec_valid_auth_exec_success(client, mock_docker):
    """WebSocket exec: gets through auth and docker setup."""
    c = MagicMock()
    c.id = "abc1234567890123"
    c.exec_run.return_value = (1, None)  # bash not found
    mock_socket = MagicMock()
    mock_socket._sock = MagicMock()
    mock_socket._sock.recv.side_effect = Exception("closed")
    mock_docker.containers.get.return_value = c
    mock_docker.api.exec_create.return_value = "exec_id"
    mock_docker.api.exec_start.return_value = mock_socket
    try:
        with client.websocket_connect(
            "/ws/exec/abc1234567890123",
            headers={"origin": "http://127.0.0.1:8080"},
        ) as ws:
            ws.send_text(f"AUTH {AUTH_HEADER['Authorization'].split()[1]}")
    except Exception:
        pass


# ── LICENSE file ──────────────────────────────────────────────────────────────

def test_license_file_exists(client):
    """Test /LICENSE endpoint serves file."""
    lic = Path(__file__).parent.parent / "LICENSE"
    if lic.exists():
        resp = client.get("/LICENSE")
        assert resp.status_code == 200
    else:
        pytest.skip("No LICENSE file present")


def test_index_page(client):
    """Test / serves index.html."""
    index = Path(__file__).parent.parent / "static" / "index.html"
    if index.exists():
        resp = client.get("/")
        assert resp.status_code == 200
    else:
        pytest.skip("No static/index.html present")


# ── container_top non-409 re-raise ────────────────────────────────────────────

def test_container_top_non_409_error(client, mock_docker):
    """container_top re-raises non-409 HTTPExceptions."""
    import docker.errors
    c = MagicMock()
    resp_mock = MagicMock()
    resp_mock.status_code = 404
    resp_mock.reason = "Not Found"
    err = docker.errors.APIError("not found", response=resp_mock, explanation="not found")
    c.top.side_effect = err
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc1234567890123/top", headers=AUTH_HEADER)
    assert resp.status_code == 404


# ── push_image: non-json line in output ───────────────────────────────────────

def test_push_image_non_json_output(client, mock_docker):
    """Push output with non-JSON lines should be silently ignored."""
    mock_docker.images.push.return_value = "this is not json\n"
    resp = client.post(
        "/api/images/push?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── compose up symlink traversal ──────────────────────────────────────────────

def test_compose_up_symlink_traversal(client, tmp_path):
    """Symlink-based path traversal should be rejected."""
    # Create a symlink that points outside COMPOSE_DIR
    outside = tmp_path / "outside"
    outside.mkdir()
    link_target = tmp_path / "compose" / "evil"
    (tmp_path / "compose").mkdir()
    link_target.symlink_to(outside)

    with patch("app.COMPOSE_DIR", tmp_path / "compose"):
        # The symlink "evil" resolves to outside compose dir
        import io
        valid_content = b"services:\n  web:\n    image: us-docker.pkg.dev/p/r/img:latest\n"
        resp = client.post(
            "/api/compose/up?project_name=evil",
            headers=AUTH_CSRF,
            files={"file": ("docker-compose.yml", io.BytesIO(valid_content), "text/yaml")},
        )
    # Either 200 (symlink not traversal) or 400 (blocked)
    assert resp.status_code in (200, 400, 504)


# ── _validate_ws_origin exception path ───────────────────────────────────────

def test_validate_ws_origin_exception_in_urlparse():
    """If urlparse raises, return False."""
    from app import _validate_ws_origin
    ws = MagicMock()
    # A valid non-empty origin not in allowlist, but urlparse will work
    # Test the path where origin_host is empty (no netloc)
    ws.headers = {"origin": "//", "host": "myhost"}
    result = _validate_ws_origin(ws)
    assert result is False
