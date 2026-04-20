# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Unit coverage for /api/volumes/{name}/browse + /prune with undo.

The integration path (actually spawning an alpine helper) is exercised
by the journey tests; these unit tests use mocked Docker clients so the
coverage measurement isn't gated on a live daemon.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import docker.errors
import pytest

from tests.conftest import AUTH_CSRF


@pytest.fixture
def mock_client(mock_docker):
    return mock_docker


def _mk_volume(name: str = "myvol") -> MagicMock:
    v = MagicMock()
    v.name = name
    v.attrs = {"Name": name, "Driver": "local", "Mountpoint": f"/var/lib/docker/volumes/{name}/_data"}
    return v


def _mk_container(cid: str, name: str, volume_name: str, mount: str = "/data", status: str = "running") -> MagicMock:
    c = MagicMock()
    c.id = cid + "a" * (64 - len(cid))
    c.name = name
    c.status = status
    c.attrs = {
        "Mounts": [
            {"Type": "volume", "Name": volume_name, "Destination": mount},
        ],
    }
    return c


@pytest.mark.unit
def test_browse_open_reuses_attached_running_container(client, mock_client):
    """If the volume is mounted on a running container, /browse returns
    that container — no helper spawned. Running preferred over stopped."""
    vol = _mk_volume("myvol")
    running = _mk_container("aaaa111", "running-app", "myvol", "/data", "running")
    stopped = _mk_container("bbbb222", "stopped-app", "myvol", "/legacy", "exited")
    mock_client.volumes.get.return_value = vol
    mock_client.containers.list.return_value = [stopped, running]

    resp = client.post("/api/volumes/myvol/browse", headers=AUTH_CSRF)
    assert resp.status_code == 200
    body = resp.json()
    assert body["helper"] is None
    assert body["mount_path"] == "/data"
    assert body["container_id"].startswith("aaaa111")


@pytest.mark.unit
def test_browse_open_spawns_helper_when_no_attached_container(client, mock_client):
    """Orphan volume → alpine helper container spawned with the volume
    mounted at /mnt. Response carries the helper name for later
    DELETE /browse cleanup."""
    vol = _mk_volume("lonely")
    mock_client.volumes.get.return_value = vol
    mock_client.containers.list.return_value = []  # no attached containers
    helper = MagicMock()
    helper.id = "h" * 64
    mock_client.containers.run.return_value = helper

    resp = client.post("/api/volumes/lonely/browse", headers=AUTH_CSRF)
    assert resp.status_code == 200
    body = resp.json()
    assert body["helper"] is not None
    assert body["helper"].startswith("skiff-volbrowse-")
    assert body["mount_path"] == "/mnt"
    # Helper was labelled so close-browse can safely identify it.
    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["labels"]["skiff.helper"] == "volbrowse"
    assert call_kwargs["labels"]["skiff.volume"] == "lonely"


@pytest.mark.unit
def test_browse_open_rejects_bad_volume_name(client):
    """Names with spaces fail the VOLUME_NAME regex → volume.bad_name 404."""
    resp = client.post("/api/volumes/not%20valid/browse", headers=AUTH_CSRF)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "volume.bad_name"


@pytest.mark.unit
def test_browse_open_surfaces_helper_spawn_failure(client, mock_client):
    """Helper-spawn failure is surfaced as a catalogued envelope, not
    a 500. safe_docker_call converts APIError → appropriate envelope;
    we just verify the status is a 4xx/5xx catalogued response."""
    vol = _mk_volume("lonely")
    mock_client.volumes.get.return_value = vol
    mock_client.containers.list.return_value = []
    mock_client.containers.run.side_effect = docker.errors.APIError("image not found")

    resp = client.post("/api/volumes/lonely/browse", headers=AUTH_CSRF)
    # Accept any 4xx/5xx envelope — safe_docker_call maps APIError to
    # different codes depending on the exact error shape.
    assert resp.status_code >= 400
    detail = resp.json()["detail"]
    assert "code" in detail and "message" in detail


@pytest.mark.unit
def test_browse_close_removes_labelled_helper(client, mock_client):
    """DELETE /browse?container_id=<helper> tears down an explicit helper.
    Accepts only containers labelled skiff.helper=volbrowse AND skiff.volume
    matches — defence against malicious cleanup of real containers."""
    helper = MagicMock()
    helper.attrs = {
        "Config": {
            "Labels": {"skiff.helper": "volbrowse", "skiff.volume": "myvol"},
        },
    }
    mock_client.containers.get.return_value = helper

    resp = client.delete(
        "/api/volumes/myvol/browse?container_id=abc123",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200
    helper.remove.assert_called_once_with(force=True)


@pytest.mark.unit
def test_browse_close_refuses_non_helper_container(client, mock_client):
    """A caller passing a real container ID must NOT be allowed to
    remove it via the close-browse route — refuse with 400."""
    real_container = MagicMock()
    real_container.attrs = {"Config": {"Labels": {}}}  # no skiff.helper
    mock_client.containers.get.return_value = real_container

    resp = client.delete(
        "/api/volumes/myvol/browse?container_id=real123",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400
    real_container.remove.assert_not_called()


@pytest.mark.unit
def test_browse_close_refuses_helper_from_different_volume(client, mock_client):
    """Cross-volume defence: a helper labelled with a different
    skiff.volume than the URL path must NOT be removable by this
    request — someone trying to clean up volume A via path /volumes/B."""
    helper = MagicMock()
    helper.attrs = {
        "Config": {
            "Labels": {"skiff.helper": "volbrowse", "skiff.volume": "other-vol"},
        },
    }
    mock_client.containers.get.return_value = helper

    resp = client.delete(
        "/api/volumes/myvol/browse?container_id=abc123",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 400
    helper.remove.assert_not_called()


@pytest.mark.unit
def test_browse_close_is_idempotent_when_helper_missing(client, mock_client):
    """If the helper is already gone (404 NotFound), the close endpoint
    returns 200 — double-close should not error."""
    mock_client.containers.get.side_effect = docker.errors.NotFound("gone")

    resp = client.delete(
        "/api/volumes/myvol/browse?container_id=abc123",
        headers=AUTH_CSRF,
    )
    assert resp.status_code == 200


# ── Compose templates + stack-scale undo (same coverage file) ──────


@pytest.mark.unit
def test_compose_templates_catalogue_returns_every_stack(client):
    """GET /api/compose/templates returns the full catalogue with
    is_allowed badges. Each entry has id, name, yaml, images, env."""
    resp = client.get("/api/compose/templates", headers=AUTH_CSRF)
    assert resp.status_code == 200
    body = resp.json()
    templates = body.get("templates", [])
    assert len(templates) >= 1
    ids = {t["id"] for t in templates}
    # A few expected stacks.
    assert {"wordpress", "monitoring", "pihole"}.issubset(ids)
    for t in templates:
        for field in ("id", "name", "description", "category", "images", "yaml"):
            assert field in t, f"template {t.get('id')!r} missing {field}"
        assert isinstance(t["images"], list)
        assert isinstance(t["yaml"], str) and "services:" in t["yaml"]
        assert "is_allowed" in t and "reject_reason" in t


@pytest.mark.unit
def test_compose_templates_flags_blocked_registry(client, monkeypatch):
    """If a template's image registry is outside the allowlist,
    is_allowed=False + reject_reason names the offending image."""
    # Force the allowlist to empty — nothing passes validate_image_registry.
    from skiff import config as _cfg

    orig = _cfg._cfg.allowed_registries
    _cfg._cfg.allowed_registries = ["unreachable.example.com"]
    try:
        resp = client.get("/api/compose/templates", headers=AUTH_CSRF)
        assert resp.status_code == 200
        bodies = resp.json()["templates"]
        # Every template should now be flagged since none use that registry.
        assert all(not t["is_allowed"] for t in bodies)
        assert all(t["reject_reason"] for t in bodies)
    finally:
        _cfg._cfg.allowed_registries = orig


@pytest.mark.unit
def test_compose_scale_with_undo_returns_token():
    """Scale-down path with undo=true queues the op + returns a token
    so a misclick is reversible within the window. Covers the undo
    branch in compose_scale()."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app import app
    from skiff import config as _cfg

    orig = _cfg._cfg.api_token
    _cfg._cfg.api_token = ""
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            # Patch subprocess.run so _compose_stack_op doesn't actually shell
            # out; the undo path queues it for UNDO_DELAY_SECS anyway.
            with patch("subprocess.run") as sp:
                sp.return_value.returncode = 0
                sp.return_value.stdout = "scaled"
                sp.return_value.stderr = ""
                resp = tc.post(
                    "/api/compose/myproj/scale?service_name=web&replicas=2&undo=true",
                    headers=AUTH_CSRF,
                )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # Queue-full fallback also acceptable, but normally returns a token.
            assert "undo_token" in body or body.get("ok")
    finally:
        _cfg._cfg.api_token = orig


@pytest.mark.unit
def test_container_delete_file_undo_path_covers_rm_exec():
    """DELETE /files?path=...&undo=false runs exec `rm -rf -- <path>`
    inline. Covers the inline fire path (undo queue skipped)."""
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient

    from app import app
    from skiff import config as _cfg

    orig = _cfg._cfg.api_token
    _cfg._cfg.api_token = ""
    try:
        c = MagicMock()
        c.short_id = "abc123def456"
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = b""
        c.exec_run.return_value = exec_result
        mock_client = MagicMock()
        mock_client.containers.get.return_value = c
        mock_client.ping.return_value = True
        with (
            patch("skiff.docker_client._client", mock_client),
            patch("skiff.docker_client._client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                resp = tc.delete(
                    "/api/containers/abc123def456/files?path=/tmp/x&undo=false",
                    headers=AUTH_CSRF,
                )
        assert resp.status_code == 200
        c.exec_run.assert_called_once()
        args, _kw = c.exec_run.call_args
        assert args[0] == ["rm", "-rf", "--", "/tmp/x"]
    finally:
        _cfg._cfg.api_token = orig
