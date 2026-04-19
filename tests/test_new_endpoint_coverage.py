# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Coverage for the endpoints added this session — Dashboard overview,
Docker events, App templates, compose stop/start/pull/scale/download,
and image prune.

Each is mocked against the Docker SDK so we can fire them in a fast
unit tier and pull the suite's per-module coverage back above the
94% gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import docker.errors
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _reset_limiter():
    from skiff import config as config_module
    from skiff.app import app

    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()


def _mock_client(**overrides) -> MagicMock:
    m = MagicMock()
    m.ping.return_value = True
    m.containers.list.return_value = []
    m.images.list.return_value = []
    m.volumes.list.return_value = []
    m.networks.list.return_value = []
    m.df.return_value = {"Images": [], "Containers": [], "Volumes": [], "BuildCache": []}
    m.info.return_value = {}
    m.events.return_value = iter(())
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


def _invoke(mock_client: MagicMock, method: str, path: str, **kw):
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    _reset_limiter()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                return tc.request(
                    method,
                    path,
                    headers={"X-Requested-With": "ContainerManager"},
                    **kw,
                )
    finally:
        config_module._cfg.api_token = orig_token


# ── /api/system/overview ─────────────────────────────────────────────────


def test_overview_aggregates_counts_and_events():
    """Dashboard overview: combines per-state counts, totals, df, events
    into a single JSON. Empty-daemon path — every count should be 0."""
    r = _invoke(_mock_client(), "GET", "/api/system/overview")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["containers"] == {"total": 0, "running": 0, "paused": 0, "exited": 0, "created": 0}
    assert body["images"]["total"] == 0
    assert body["volumes"]["total"] == 0
    assert body["networks"]["total"] == 0
    assert body["recent_events"] == []


def test_overview_counts_by_state():
    """Per-state container breakdown must sum correctly."""

    def _c(status):
        m = MagicMock()
        m.status = status
        return m

    mc = _mock_client()
    mc.containers.list.return_value = [
        _c("running"),
        _c("running"),
        _c("exited"),
        _c("paused"),
        _c("created"),
    ]
    r = _invoke(mc, "GET", "/api/system/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["containers"]["total"] == 5
    assert body["containers"]["running"] == 2
    assert body["containers"]["exited"] == 1
    assert body["containers"]["paused"] == 1
    assert body["containers"]["created"] == 1


def test_overview_degrades_when_engine_partially_errors():
    """A failure on one sub-query (e.g. df) must not fail the whole
    response — aggregation uses `_safe` with a default-empty shape."""
    mc = _mock_client()
    mc.df.side_effect = docker.errors.APIError("daemon snapshot failed")
    r = _invoke(mc, "GET", "/api/system/overview")
    assert r.status_code == 200
    body = r.json()
    # Even with df failing, counts come from other sub-queries.
    assert "containers" in body


# ── /api/system/events ───────────────────────────────────────────────────


def test_events_endpoint_bounds_since_secs():
    """since_secs must be clamped to [1, 3600]. Passing a huge value is
    not an error — just clamped. Same for limit."""
    r = _invoke(_mock_client(), "GET", "/api/system/events?since_secs=99999&limit=99999")
    assert r.status_code == 200
    body = r.json()
    assert body["since_secs"] <= 3600
    assert isinstance(body["events"], list)


def test_events_endpoint_handles_generator_errors():
    """If `client.events` raises mid-iteration, handler must log the
    warning event and return best-effort (partial) events."""
    mc = _mock_client()
    mc.events.side_effect = RuntimeError("docker daemon event stream broken")
    r = _invoke(mc, "GET", "/api/system/events")
    assert r.status_code == 200, r.text[:200]
    assert r.json()["events"] == []


# ── /api/templates ───────────────────────────────────────────────────────


def test_templates_catalogue_lists_known_ids():
    """The template catalogue should include the canonical stack of
    nginx, postgres, redis. Each entry carries an `is_allowed` flag."""
    r = _invoke(_mock_client(), "GET", "/api/templates")
    assert r.status_code == 200
    body = r.json()
    ids = {t["id"] for t in body["templates"]}
    assert {"nginx", "postgres", "redis", "mysql", "mongo", "python", "node", "alpine"} <= ids


def test_templates_entries_carry_required_fields():
    """Frontend expects every entry to have: id, name, description, image,
    category, ports, env, volumes, command, is_allowed."""
    r = _invoke(_mock_client(), "GET", "/api/templates")
    for t in r.json()["templates"]:
        for k in ("id", "name", "description", "image", "category", "ports", "env", "volumes", "command", "is_allowed"):
            assert k in t, f"template {t.get('id')!r} missing {k!r}"


# ── /api/images/prune ────────────────────────────────────────────────────


def test_image_prune_returns_reclaimed_space():
    """Prune response reshapes docker-py's output into MB reclaimed +
    deleted count."""
    mc = _mock_client()
    mc.images.prune.return_value = {
        "ImagesDeleted": [{"Deleted": "sha256:abc"}] * 3,
        "SpaceReclaimed": 5 * 1024 * 1024,  # 5 MB
    }
    r = _invoke(mc, "POST", "/api/images/prune")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["deleted_count"] == 3
    assert body["space_reclaimed_mb"] == 5.0


def test_image_prune_envelope_on_api_error():
    """Docker's APIError → catalogued `image.prune_failed`."""
    mc = _mock_client()
    mc.images.prune.side_effect = docker.errors.APIError("prune explosion")
    r = _invoke(mc, "POST", "/api/images/prune")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "image.prune_failed"


def test_image_prune_all_flag_changes_filter():
    """`dangling_only=false` must pass `{dangling: false}` to docker-py."""
    mc = _mock_client()
    mc.images.prune.return_value = {"ImagesDeleted": [], "SpaceReclaimed": 0}
    _invoke(mc, "POST", "/api/images/prune?dangling_only=false")
    mc.images.prune.assert_called_with(filters={"dangling": False})


# ── Compose stop / start / pull / scale / download ──────────────────────


def _fake_subprocess_ok():
    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    return _R()


def _fake_subprocess_fail(stderr="boom"):
    class _R:
        returncode = 1
        stdout = ""

    _R.stderr = stderr
    return _R()


@pytest.mark.parametrize("verb", ["stop", "start", "pull"])
def test_compose_lifecycle_verbs_invoke_subprocess(verb):
    """Each verb must invoke `docker compose <verb>` under a project dir
    that exists on disk. We patch `_find_project_dir` to simulate a
    deployed stack and `subprocess.run` to capture the call args."""
    from pathlib import Path

    with (
        patch("skiff.routers.compose._find_project_dir", return_value=Path("/tmp/skiff-fake-proj")),
        patch("skiff.routers.compose.subprocess.run", return_value=_fake_subprocess_ok()) as sp,
    ):
        r = _invoke(_mock_client(), "POST", f"/api/compose/demo/{verb}")
        assert r.status_code == 200, r.text[:200]
        # The subprocess call argv must contain the verb.
        argv = sp.call_args.args[0]
        assert verb in argv


def test_compose_scale_clamps_replicas():
    """`replicas` past the cap → 400 envelope, never a silent runaway."""
    r = _invoke(_mock_client(), "POST", "/api/compose/demo/scale?service_name=web&replicas=99999")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "validation.bad_input"


def test_compose_scale_rejects_bad_service_name():
    """Service name goes through SERVICE_NAME_RE — shell metachars rejected."""
    r = _invoke(_mock_client(), "POST", "/api/compose/demo/scale?service_name=bad;cmd&replicas=1")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "validation.bad_input"


def test_compose_download_404_when_project_unknown():
    """Requesting a project that has never been deployed → 404 envelope."""
    with patch("skiff.routers.compose._find_project_dir", return_value=None):
        r = _invoke(_mock_client(), "GET", "/api/compose/never-deployed/download")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "compose.not_found"


def test_compose_lifecycle_surface_envelope_on_subprocess_fail():
    """Non-zero exit from `docker compose` → compose.deploy_failed."""
    from pathlib import Path

    with (
        patch("skiff.routers.compose._find_project_dir", return_value=Path("/tmp/skiff-fake-proj")),
        patch("skiff.routers.compose.subprocess.run", return_value=_fake_subprocess_fail()),
    ):
        r = _invoke(_mock_client(), "POST", "/api/compose/demo/stop")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "compose.deploy_failed"


# ── Container commit — error envelope paths ─────────────────────────────


def test_commit_rejects_uppercase_repository():
    """Commit repo must pass COMMIT_REPO_RE (lowercase). Envelope, not 500."""
    r = _invoke(_mock_client(), "POST", "/api/containers/abc123def456/commit?repository=UPPER")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "validation.bad_image_name"


def test_commit_rejects_bad_tag():
    """Tag grammar rejects leading-dash + whitespace."""
    r = _invoke(_mock_client(), "POST", "/api/containers/abc123def456/commit?repository=local/a&tag=-bad")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "validation.bad_image_name"


def test_commit_succeeds_with_canonical_inputs():
    """Happy path — returns OkResponse with image_id/repository/tag."""
    mc = _mock_client()
    ctr = MagicMock()
    img = MagicMock()
    img.short_id = "sha123"
    ctr.commit.return_value = img
    mc.containers.get.return_value = ctr
    r = _invoke(mc, "POST", "/api/containers/abc123def456/commit?repository=local/app&tag=v1")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["image_id"] == "sha123"
    assert body["repository"] == "local/app"
    assert body["tag"] == "v1"
