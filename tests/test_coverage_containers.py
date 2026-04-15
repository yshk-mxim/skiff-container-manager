"""Tests for container endpoints missing coverage."""

from unittest.mock import MagicMock

import docker.errors

from tests.conftest import AUTH_CSRF, AUTH_HEADER


def _make_container(
    short_id="abc123def456",
    name="test-container",
    image_tag="us-docker.pkg.dev/p/r/img:latest",
    status="running",
    state_status="running",
):
    c = MagicMock()
    c.short_id = short_id
    c.id = short_id
    c.name = name
    c.image.tags = [image_tag]
    c.image.short_id = "sha256:abcdef"
    c.status = status
    c.ports = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
    c.labels = {}
    c.attrs = {
        "Id": short_id + "0" * (64 - len(short_id)),
        "Name": "/" + name,
        "Created": "2026-01-01T00:00:00Z",
        "State": {"Status": state_status, "Health": None, "ExitCode": 0},
        "Config": {
            "Image": image_tag,
            "Env": ["FOO=bar"],
            "Cmd": ["/bin/sh"],
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Labels": {},
            "Hostname": "myhost",
            "User": "",
            "Healthcheck": {},
        },
        "HostConfig": {
            "Memory": 2 * 1024**3,
            "CpuShares": 0,
            "RestartPolicy": {"Name": "no"},
            "ReadonlyRootfs": False,
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "NetworkSettings": {
            "Networks": {
                "bridge": {"IPAddress": "172.17.0.2", "Gateway": "172.17.0.1", "MacAddress": "02:42:ac:11:00:02"}
            },
            "Ports": {},
        },
        "Mounts": [],
        "RestartCount": 0,
        "Platform": "linux",
    }
    return c


# ── List containers ────────────────────────────────────────────────────────────

def test_list_containers_with_data(client, mock_docker):
    mock_docker.containers.list.return_value = [_make_container()]
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "abc123def456"
    assert data[0]["image"] == "us-docker.pkg.dev/p/r/img:latest"


def test_list_containers_image_fallback_to_short_id(client, mock_docker):
    c = _make_container()
    c.image.tags = []
    mock_docker.containers.list.return_value = [c]
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()[0]["image"] == "sha256:abcdef"


# ── Inspect ────────────────────────────────────────────────────────────────────

def test_inspect_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.get("/api/containers/abc123def456/inspect", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert "config" in data


# ── Logs ──────────────────────────────────────────────────────────────────────

def test_container_logs(client, mock_docker):
    c = _make_container()
    c.logs.return_value = b"2026-01-01T00:00:00Z log line\n"
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/logs", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert "logs" in resp.json()


def test_download_container_logs_plaintext(client, mock_docker):
    c = _make_container()
    c.logs.return_value = b"2026-01-01T00:00:00Z some log\n"
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/logs/download", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]


def test_download_container_logs_jsonl(client, mock_docker):
    c = _make_container()
    c.logs.return_value = b"2026-01-01T00:00:00Z message here\n"
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/logs/download.jsonl", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert ".jsonl" in resp.headers["content-disposition"]


def test_download_container_logs_jsonl_no_space(client, mock_docker):
    """Line without a space should still parse."""
    c = _make_container()
    c.logs.return_value = b"nospacehere\n"
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/logs/download.jsonl", headers=AUTH_HEADER)
    assert resp.status_code == 200


# ── Start/Stop/Restart/Pause/Unpause ──────────────────────────────────────────

def test_pause_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def456/pause", headers=AUTH_CSRF)
    assert resp.status_code == 200


def test_unpause_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def456/unpause", headers=AUTH_CSRF)
    assert resp.status_code == 200


# ── Kill ──────────────────────────────────────────────────────────────────────

def test_kill_container_sigterm(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def456/kill?signal=SIGTERM", headers=AUTH_CSRF)
    assert resp.status_code == 200


# ── Rename ────────────────────────────────────────────────────────────────────

def test_rename_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.post("/api/containers/abc123def456/rename?name=newname", headers=AUTH_CSRF)
    assert resp.status_code == 200


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_container(client, mock_docker):
    mock_docker.containers.get.return_value = _make_container()
    resp = client.delete("/api/containers/abc123def456", headers=AUTH_CSRF)
    assert resp.status_code == 200


# ── Stats ─────────────────────────────────────────────────────────────────────

def test_container_stats(client, mock_docker):
    c = _make_container()
    c.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 1000000},
            "system_cpu_usage": 5000000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 900000},
            "system_cpu_usage": 4000000,
        },
        "memory_stats": {"usage": 100 * 1024 * 1024, "limit": 2 * 1024**3},
        "networks": {"eth0": {"rx_bytes": 1024, "tx_bytes": 512}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 1024 * 1024},
                {"op": "Write", "value": 512 * 1024},
            ]
        },
    }
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/stats", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "cpu_percent" in data
    assert "memory_usage_mb" in data


# ── Top ───────────────────────────────────────────────────────────────────────

def test_container_top(client, mock_docker):
    c = _make_container()
    c.top.return_value = {"Titles": ["PID", "CMD"], "Processes": [["1", "/bin/sh"]]}
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/top", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["titles"] == ["PID", "CMD"]


def test_container_top_not_running(client, mock_docker):
    resp_mock = MagicMock()
    resp_mock.status_code = 409
    resp_mock.reason = "Conflict"
    err = docker.errors.APIError("not running", response=resp_mock, explanation="not running")
    c = _make_container()
    c.top.side_effect = err
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/top", headers=AUTH_HEADER)
    assert resp.status_code == 409


# ── Diff ─────────────────────────────────────────────────────────────────────

def test_container_diff(client, mock_docker):
    c = _make_container()
    c.diff.return_value = [{"Path": "/app/foo", "Kind": 1}]
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/diff", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["kind"] == "Added"


def test_container_diff_none(client, mock_docker):
    c = _make_container()
    c.diff.return_value = None
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/diff", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == []


# ── Run container ─────────────────────────────────────────────────────────────

def test_run_container_basic(client, mock_docker):
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == new_c.short_id


def test_run_container_with_all_options(client, mock_docker):
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest&name=mycontainer",
        headers=AUTH_CSRF,
        json={
            "ports": {"80/tcp": "8080"},
            "environment": ["FOO=bar"],
            "volumes": ["myvol:/data"],
            "labels": {"app": "test"},
            "restart_policy": "always",
            "network": "mynet",
        },
    )
    assert resp.status_code == 200


def test_run_container_blocked_registry(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=badregistry.io/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


def test_run_container_host_path_volume_rejected(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["/host/path:/data"]},
    )
    assert resp.status_code == 400


def test_run_container_blocked_mount_target(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["myvol:/etc"]},
    )
    assert resp.status_code == 400


def test_run_container_invalid_env(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"environment": ["123=bad"]},
    )
    assert resp.status_code == 400


def test_run_container_limit_reached(client, mock_docker):
    mock_docker.containers.list.return_value = [MagicMock()] * 50
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


def test_run_container_invalid_restart_policy(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"restart_policy": "bad"},
    )
    assert resp.status_code == 400


def test_run_container_invalid_network_name(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"network": "bad network!"},
    )
    assert resp.status_code == 400


def test_run_container_label_invalid_key(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"labels": {"bad label!": "val"}},
    )
    assert resp.status_code == 400


def test_run_container_volume_no_colon(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["invalidvolume"]},
    )
    assert resp.status_code == 400


def test_run_container_volume_invalid_name(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=us-docker.pkg.dev/p/r/img:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["invalid vol name!:/data"]},
    )
    assert resp.status_code == 400
