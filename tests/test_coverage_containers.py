# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for container endpoints missing coverage."""

from unittest.mock import MagicMock

import docker.errors

from tests.conftest import AUTH_CSRF, AUTH_HEADER
from tests.factories import make_container as _make_container

# ── List containers ────────────────────────────────────────────────────────────


def test_list_containers_with_data(client, mock_docker):
    mock_docker.containers.list.return_value = [_make_container()]
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "abc123def456"
    assert data[0]["image"] == "docker.io/library/nginx:latest"


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
    assert "mem_usage_mb" in data


# ── Stats on cgroup v2 (realistic response shape) ────────────────────────────


def test_container_stats_cgroup_v2_returns_non_zero_memory(client, mock_docker):
    """Reproduces the 1.0.1 bug: on cgroup v2 kernels Docker omits the
    `cache` key from `memory_stats.stats`. The old code did
    `usage - cache` via `.get("cache", 0)`, which returned None when
    the key was simply missing (default works) but the real bite came
    from the `usage` numerator ALSO being subject to null coercion on
    some drivers. This fixture is a verbatim cgroup v2 response from a
    running alpine container — keys and shape are what the daemon
    actually emits — and the handler must return a mem_usage_mb > 0."""
    c = _make_container()
    # Exact cgroup v2 shape — NO `cache` key in memory_stats.stats.
    c.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2_500_000},
            "system_cpu_usage": 10_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 2_000_000},
            "system_cpu_usage": 9_000_000,
        },
        "memory_stats": {
            "usage": 933_888,  # ~0.89 MiB real working-set memory
            "limit": 2 * 1024**3,
            "stats": {  # cgroup v2 keys — no "cache"
                "active_anon": 45_056,
                "anon": 389_120,
                "file": 544_768,
                "inactive_file": 413_696,
                "slab": 16_384,
            },
        },
        "networks": {"eth0": {"rx_bytes": 2_400, "tx_bytes": 1_200}},
        "blkio_stats": {"io_service_bytes_recursive": []},
    }
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/stats", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    # Pre-fix this returned 0.0 because `usage - cache` coerced None to
    # 0 via get-with-default, but the inactive_file fallback wasn't
    # used, so the working-set computation was wrong in a misleading way.
    # Post-fix: usage=933888, inactive_file=413696, working=520192 → ~0.5 MiB.
    assert data["mem_usage_mb"] > 0, (
        f"cgroup v2 memory dropped to {data['mem_usage_mb']}MB — the "
        "inactive_file fallback regressed and Stats will show 0 again"
    )
    assert data["mem_limit_mb"] == 2048.0


def test_container_stats_tolerates_all_null_fields(client, mock_docker):
    """Every nullable Docker response field set to None simultaneously —
    the endpoint MUST NOT crash. Covers the class of bug where Docker
    returns `null` for SizeRw / rx_bytes / tx_bytes / blkio values /
    memory usage on freshly-created or driver-specific containers."""
    c = _make_container()
    c.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 100},
            "system_cpu_usage": 1000,
            "online_cpus": 1,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": None, "limit": None, "stats": {}},
        "networks": {"eth0": {"rx_bytes": None, "tx_bytes": None}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": None},
                {"op": "Write", "value": None},
            ]
        },
    }
    mock_docker.containers.get.return_value = c
    resp = client.get("/api/containers/abc123def456/stats", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    # Each field must be a number (0 is fine), never null / not-returned.
    for field in ("mem_usage_mb", "mem_limit_mb", "net_rx_mb", "net_tx_mb", "blk_read_mb", "blk_write_mb"):
        assert isinstance(data[field], (int, float)), f"{field} must be numeric, got {data[field]!r}"


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
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == new_c.short_id


def test_run_container_with_all_options(client, mock_docker):
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest&name=mycontainer",
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
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["/host/path:/data"]},
    )
    assert resp.status_code == 400


def test_run_container_blocked_mount_target(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["myvol:/etc"]},
    )
    assert resp.status_code == 400


def test_run_container_invalid_env(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"environment": ["123=bad"]},
    )
    assert resp.status_code == 400


def test_run_container_limit_reached(client, mock_docker):
    mock_docker.containers.list.return_value = [MagicMock()] * 50
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400


# ── tmpfs + read_only behaviour ────────────────────────────────────────────────


def test_run_container_readonly_default_mounts_tmpfs(client, mock_docker):
    """read_only=True (default) + tmpfs=None → DEFAULT_TMPFS is applied."""
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert kwargs["read_only"] is True
    assert "tmpfs" in kwargs
    assert "/tmp" in kwargs["tmpfs"]
    assert "/var/cache" in kwargs["tmpfs"]


def test_run_container_writable_rootfs_no_tmpfs(client, mock_docker):
    """read_only=False + tmpfs=None → no tmpfs kwarg passed to docker SDK."""
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"read_only": False},
    )
    assert resp.status_code == 200
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert kwargs["read_only"] is False
    assert "tmpfs" not in kwargs


def test_run_container_explicit_empty_tmpfs_overrides_default(client, mock_docker):
    """Explicit tmpfs={} with read_only=True → no tmpfs applied."""
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"tmpfs": {}},
    )
    assert resp.status_code == 200
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert "tmpfs" not in kwargs


def test_run_container_custom_tmpfs(client, mock_docker):
    new_c = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.run.return_value = new_c
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"tmpfs": {"/scratch": "rw,size=32m"}},
    )
    assert resp.status_code == 200
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert kwargs["tmpfs"] == {"/scratch": "rw,size=32m"}


def test_run_container_tmpfs_blocked_target_rejected(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"tmpfs": {"/etc": "rw"}},
    )
    assert resp.status_code == 400
    assert "not permitted" in resp.json()["detail"]["message"]


def test_run_container_tmpfs_bad_option_rejected(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"tmpfs": {"/tmp": "exec"}},  # `exec` not in allowlist (only `noexec`)
    )
    assert resp.status_code == 400


def test_run_container_tmpfs_size_cap(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        # 1 GB > MAX_TMPFS_SIZE_MB (512)
        json={"tmpfs": {"/tmp": "rw,size=1g"}},
    )
    assert resp.status_code == 400
    assert "exceeds cap" in resp.json()["detail"]["message"]


def test_run_container_tmpfs_path_traversal_rejected(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"tmpfs": {"/tmp/../etc": "rw"}},
    )
    assert resp.status_code == 400


# ── Container resource update: POST /api/containers/{id}/update ───────────────


from tests.factories import make_container_with_hc as _make_container_with_hc


def test_update_container_memory_gcp_unit(client, mock_docker):
    """memory='256Mi' is parsed to 268435456 bytes and passed as mem_limit."""
    c = _make_container_with_hc({"Memory": 0})
    mock_docker.containers.get.return_value = c

    def after_update(**kwargs):
        c.attrs["HostConfig"]["Memory"] = 268435456

    c.update.side_effect = after_update
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory": "256Mi"},
    )
    assert resp.status_code == 200, resp.text
    c.update.assert_called_once()
    assert c.update.call_args.kwargs["mem_limit"] == 256 * 1024 * 1024


def test_update_container_cpus_milli(client, mock_docker):
    """cpus='500m' = 0.5 CPU → cpu_quota=50000 with default period=100000."""
    c = _make_container_with_hc({"CpuQuota": 0, "CpuPeriod": 0})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"cpus": "500m"},
    )
    assert resp.status_code == 200
    kw = c.update.call_args.kwargs
    assert kw["cpu_period"] == 100_000
    assert kw["cpu_quota"] == 50_000


def test_update_container_restart_policy(client, mock_docker):
    c = _make_container_with_hc({"RestartPolicy": {"Name": "no"}})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"restart_policy": {"Name": "on-failure", "MaximumRetryCount": 3}},
    )
    assert resp.status_code == 200
    rp = c.update.call_args.kwargs["restart_policy"]
    assert rp == {"Name": "on-failure", "MaximumRetryCount": 3}


def test_update_container_pids_limit(client, mock_docker):
    c = _make_container_with_hc({"PidsLimit": 0})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"pids_limit": 100},
    )
    assert resp.status_code == 200
    assert c.update.call_args.kwargs["pids_limit"] == 100


def test_update_container_memory_cap_enforced(client, mock_docker):
    """Memory above MAX_CONTAINER_MEM (2g = 2_000_000_000) is rejected."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory": "4Gi"},
    )
    assert resp.status_code == 400
    assert "cap" in resp.json()["detail"]["message"].lower()
    c.update.assert_not_called()


def test_update_container_cpus_cap_enforced(client, mock_docker):
    """CPUs above MAX_CONTAINER_CPU (2.0) is rejected."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"cpus": "4"},
    )
    assert resp.status_code == 400
    assert "cap" in resp.json()["detail"]["message"].lower()
    c.update.assert_not_called()


def test_update_container_invalid_restart_policy(client, mock_docker):
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"restart_policy": {"Name": "reboot-forever"}},
    )
    assert resp.status_code == 400
    c.update.assert_not_called()


def test_update_container_retry_count_cap(client, mock_docker):
    """MaximumRetryCount above MAX_RESTART_RETRIES (5) is rejected."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"restart_policy": {"Name": "on-failure", "MaximumRetryCount": 99}},
    )
    assert resp.status_code == 400
    c.update.assert_not_called()


def test_update_container_requires_body(client, mock_docker):
    """Empty body → 400 "No updatable fields provided"."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "container.update_no_fields"


def test_update_container_requires_csrf(client, mock_docker):
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    # Bearer but no X-Requested-With
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers={"Authorization": AUTH_CSRF["Authorization"]},
        json={"memory": "128Mi"},
    )
    assert resp.status_code == 403
    c.update.assert_not_called()


def test_update_container_cpu_shares_out_of_range(client, mock_docker):
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"cpu_shares": 9999},
    )
    assert resp.status_code == 400
    c.update.assert_not_called()


def test_update_container_memory_reservation(client, mock_docker):
    """memory_reservation is parsed and wired as mem_reservation kwarg."""
    c = _make_container_with_hc({"MemoryReservation": 0})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory_reservation": "128Mi"},
    )
    assert resp.status_code == 200
    assert c.update.call_args.kwargs["mem_reservation"] == 128 * 1024 * 1024


def test_update_container_memory_reservation_cap(client, mock_docker):
    """memory_reservation is capped by the same MAX_CONTAINER_MEM ceiling."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory_reservation": "4Gi"},
    )
    assert resp.status_code == 400
    c.update.assert_not_called()


def test_update_container_memory_zero_rejects(client, mock_docker):
    """memory=0 rejected with container.memory_uncap_unsupported.

    Docker Engine silently ignores `memory=0` on a RUNNING container
    (the cap stays unchanged and the API returns success). Loop-10 OSS
    caught the false-positive; the API now returns a 400 so scripted
    callers see the reality and recreate the container to remove the
    memory cap.
    """
    c = _make_container_with_hc({"Memory": 500_000_000})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory": 0},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "container.memory_uncap_unsupported"
    # The daemon's `update` must NOT have been called — we short-circuit
    # before issuing the no-op request.
    c.update.assert_not_called()


def test_update_container_memory_below_docker_minimum(client, mock_docker):
    """Docker rejects positive memory limits below 6 MiB — our check is client-side defence."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory": "1Mi"},
    )
    assert resp.status_code == 400
    c.update.assert_not_called()


def test_update_container_audit_log_captures_before_after(client, mock_docker, monkeypatch):
    """CRITICAL: audit log entry must carry before→after values for every changed field.

    Without this, a compromised account could silently downgrade a container's resource
    limits and there'd be no forensic trail. Patches the structlog logger so we can
    assert the exact kwargs passed — no indirection through caplog's stdlib filter.
    """
    import skiff.routers.containers as containers_module

    captured: list[dict] = []

    def _capture(event, **kwargs):
        captured.append({"event": event, **kwargs})

    monkeypatch.setattr(containers_module.log, "info", _capture)
    c = _make_container_with_hc({"Memory": 100_000_000})
    mock_docker.containers.get.return_value = c

    def after_update(**_kw):
        c.attrs["HostConfig"]["Memory"] = 268_435_456

    c.update.side_effect = after_update
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory": "256Mi"},
    )
    assert resp.status_code == 200
    # Exactly one container.updated event, with before→after for Memory
    update_events = [e for e in captured if e["event"] == "container.updated"]
    assert len(update_events) == 1
    changes = update_events[0]["changes"]
    assert "Memory" in changes
    assert changes["Memory"] == {"before": 100_000_000, "after": 268_435_456}


def test_update_container_requires_auth(client, mock_docker):
    """No Bearer → 401, no container mutation."""
    c = _make_container_with_hc({})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers={"X-Requested-With": "ContainerManager"},
        json={"memory": "128Mi"},
    )
    assert resp.status_code == 401
    c.update.assert_not_called()


def test_update_container_ignores_unknown_fields(client, mock_docker):
    """Unknown body keys must not reach Docker kwargs — FastAPI strips them, but assert it."""
    c = _make_container_with_hc({"Memory": 0})
    mock_docker.containers.get.return_value = c
    resp = client.post(
        "/api/containers/abc123def456/update",
        headers=AUTH_CSRF,
        json={"memory": "128Mi", "privileged": True, "network": "attacker-net"},
    )
    assert resp.status_code == 200
    kw = c.update.call_args.kwargs
    # Only mem_limit should have been passed — privileged/network must not leak through
    assert "privileged" not in kw
    assert "network" not in kw
    assert kw == {"mem_limit": 128 * 1024 * 1024}


# ── Phase 2: Clone-to-recreate (inherit_from / replace_id on /containers/run) ─


def _source_container(env_list, container_id="abcd1234deadbeef"):
    """Helper: build a mock source container for inherit_from tests."""
    src = MagicMock()
    src.id = container_id
    src.short_id = container_id[:12]
    src.name = "source"
    src.attrs = {"Config": {"Env": env_list}, "HostConfig": {}}
    return src


def test_run_container_inherit_from_preserves_env(client, mock_docker):
    """Env from the source is inherited into the new container without crossing the UI."""
    src = _source_container(["SECRET=s3cret", "PATH=/bin", "REGION=us-east"])
    new = _make_container()
    mock_docker.containers.list.return_value = []
    # get() is called once for the source lookup
    mock_docker.containers.get.return_value = src
    mock_docker.containers.run.return_value = new
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"inherit_from": "abcd1234deadbeef"},
    )
    assert resp.status_code == 200, resp.text
    # The SDK's run was called with environment=<inherited list>, unchanged
    env_passed = mock_docker.containers.run.call_args.kwargs.get("environment")
    assert env_passed == ["SECRET=s3cret", "PATH=/bin", "REGION=us-east"]


def test_run_container_inherit_from_override_takes_precedence(client, mock_docker):
    """environment entries with the same key as an inherited env replace, not duplicate."""
    src = _source_container(["SECRET=old", "KEEP=yes"])
    new = _make_container()
    mock_docker.containers.list.return_value = []
    mock_docker.containers.get.return_value = src
    mock_docker.containers.run.return_value = new
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"inherit_from": "abcd1234deadbeef", "environment": ["SECRET=new", "EXTRA=1"]},
    )
    assert resp.status_code == 200
    env = mock_docker.containers.run.call_args.kwargs["environment"]
    # Inherited KEEP=yes preserved; SECRET replaced; EXTRA added
    assert "KEEP=yes" in env
    assert "SECRET=new" in env
    assert "SECRET=old" not in env
    assert "EXTRA=1" in env


def test_run_container_inherit_from_invalid_id_rejected(client, mock_docker):
    """Malformed inherit_from must be rejected BEFORE any Docker call."""
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"inherit_from": "not-a-hex-id!"},
    )
    assert resp.status_code == 400
    mock_docker.containers.get.assert_not_called()
    mock_docker.containers.run.assert_not_called()


def test_run_container_inherit_from_missing_container(client, mock_docker):
    """inherit_from pointing at a removed container surfaces 404 from _get_container."""
    import docker.errors

    mock_docker.containers.list.return_value = []
    mock_docker.containers.get.side_effect = docker.errors.NotFound("not found")
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"inherit_from": "cafebabe5678"},
    )
    assert resp.status_code == 404
    mock_docker.containers.run.assert_not_called()


def test_run_container_replace_id_removes_source_on_success(client, mock_docker):
    """replace_id causes the source container to be force-removed AFTER the new one starts."""
    src = MagicMock()
    src.id = "abcd1234deadbeef"
    src.short_id = "sourcehex123"
    src.name = "source"
    src.attrs = {"Config": {"Env": []}, "HostConfig": {}}
    new = _make_container()
    new.id = "fedcba9876543210"
    mock_docker.containers.list.return_value = []
    mock_docker.containers.get.return_value = src
    mock_docker.containers.run.return_value = new
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"replace_id": "abcd1234deadbeef"},
    )
    assert resp.status_code == 200
    assert resp.json().get("replaced_old") is True
    src.remove.assert_called_once_with(force=True)


def test_run_container_replace_id_invalid_rejected(client, mock_docker):
    """Malformed replace_id rejected up-front — no partial state."""
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"replace_id": "../../etc/passwd"},
    )
    assert resp.status_code == 400
    mock_docker.containers.run.assert_not_called()


def test_run_container_replace_cleanup_failure_does_not_fail_request(client, mock_docker):
    """If remove() fails AFTER new container starts, response is 200 with replaced_old=False."""
    src = MagicMock()
    src.id = "abcd1234deadbeef"
    src.short_id = "sourcehex123"
    src.name = "source"
    src.attrs = {"Config": {"Env": []}, "HostConfig": {}}
    src.remove.side_effect = docker.errors.DockerException("simulated removal failure")
    new = _make_container()
    new.id = "fedcba9876543210"
    mock_docker.containers.list.return_value = []
    mock_docker.containers.get.return_value = src
    mock_docker.containers.run.return_value = new
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"replace_id": "abcd1234deadbeef"},
    )
    # New container was created — this is 200 even though cleanup failed
    assert resp.status_code == 200
    assert resp.json().get("replaced_old") is False


def test_run_container_replace_id_equal_to_new_is_noop(client, mock_docker):
    """Defence-in-depth: if replace_id somehow matches the new container's own ID, don't delete it."""
    src = MagicMock()
    src.id = "0123456789abcdef"
    src.short_id = "samehex123456"
    src.name = "shared"
    src.attrs = {"Config": {"Env": []}, "HostConfig": {}}
    new = MagicMock()
    new.id = "0123456789abcdef"  # same id → don't remove
    new.short_id = "samehex123456"
    new.name = "shared"
    new.status = "running"
    mock_docker.containers.list.return_value = []
    mock_docker.containers.get.return_value = src
    mock_docker.containers.run.return_value = new
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"replace_id": "0123456789abcdef"},
    )
    assert resp.status_code == 200
    src.remove.assert_not_called()


def test_run_container_inherit_fails_before_replace_cleanup(client, mock_docker):
    """If inherit_from fetch fails, we never reach the replace_id cleanup path.

    This guards against a sequence where replace runs even though the clone didn't.
    """
    import docker.errors

    mock_docker.containers.list.return_value = []
    mock_docker.containers.get.side_effect = docker.errors.NotFound("inherit source gone")
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"inherit_from": "abcd1234deadbeef", "replace_id": "abcd1234deadbeef"},
    )
    assert resp.status_code == 404
    # Run was never called, so no cleanup on the source
    mock_docker.containers.run.assert_not_called()


def test_run_container_invalid_restart_policy(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"restart_policy": "bad"},
    )
    assert resp.status_code == 400


def test_run_container_invalid_network_name(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"network": "bad network!"},
    )
    assert resp.status_code == 400


def test_run_container_label_invalid_key(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"labels": {"bad label!": "val"}},
    )
    assert resp.status_code == 400


def test_run_container_volume_no_colon(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["invalidvolume"]},
    )
    assert resp.status_code == 400


def test_run_container_volume_invalid_name(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.post(
        "/api/containers/run?image=docker.io/library/nginx:latest",
        headers=AUTH_CSRF,
        json={"volumes": ["invalid vol name!:/data"]},
    )
    assert resp.status_code == 400
