# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Shared fixtures for SKIFF Container Manager tests."""

import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module  # noqa: F401 — kept so test-side monkeypatches that patch `app_module.X` still resolve via the root shim during the anti-pattern cleanup
import skiff.config as config_module
import skiff.docker_client as docker_client_module
from app import app

TOKEN = "test-secret-token"


# ── R12: per-module pytest markers (auto-tagged by filename) ─────────────────
# `pytest -m containers` runs every test from test_coverage_containers.py,
# test_containers_*, etc. The mapping is derived from filename stems using
# the prefix table below.
#
# The rule: every file test_<category>_<rest>.py or test_coverage_<category>.py
# gets the marker `<category>`. Files that don't match the pattern stay untagged
# (no surprise auto-tagging) and are still reachable via `-m unit`/`-m e2e`.
#
# Register the markers at collection time via pytest_configure so pytest's
# strict-marker mode doesn't reject them. The markers table is exposed to
# tooling (CI `pytest --markers`, docs generators).
_MODULE_MARKERS = (
    "containers",
    "compose",
    "images",
    "volumes",
    "networks",
    "system",
    "docker_client",
    "websocket",
    "ws",
    "registry",
    "setup",
    "audit",
    "middleware",
    "undo",
)


def pytest_configure(config):
    """Register per-module markers (R12) + reuse existing unit/e2e namespace."""
    for name in _MODULE_MARKERS:
        config.addinivalue_line(
            "markers",
            f"{name}: tests targeting the {name} module (auto-tagged by filename)",
        )


def pytest_collection_modifyitems(config, items):
    """Auto-apply a module marker when a test's filename matches the table.

    Keeps the markers table in ONE place (conftest) and lets reviewers
    run focused subsets without hand-decorating every test.
    """
    for item in items:
        stem = Path(str(item.fspath)).stem  # test_coverage_containers → test_coverage_containers
        # Strip test_ prefix and any coverage_ subsection marker
        bare = re.sub(r"^test_(coverage_)?", "", stem)
        for mod in _MODULE_MARKERS:
            # Match when the stem STARTS with the module name (so
            # test_coverage_containers, test_containers_e2e both tag).
            if bare == mod or bare.startswith(mod + "_"):
                item.add_marker(getattr(pytest.mark, mod))
                break


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset per-test-polluting guards before each test:

    - slowapi rate limiter storage (both the current module-level limiter
      AND the limiter attached to `app.state.limiter` — tests that reload
      skiff.config create a new config.limiter while the app still holds
      the old one, so we clear both to be safe)
    - setup endpoint per-IP brute-force lockout (_setup_failures)
    - WebSocket auth per-IP lockout (_ws_auth_failures)
    """
    import skiff.auth as _auth
    import skiff.routers.setup as _setup

    for limiter in {config_module.limiter, app.state.limiter}:
        limiter.reset()
    _setup._setup_failures.clear()
    _auth._ws_auth_failures.clear()
    yield
    for limiter in {config_module.limiter, app.state.limiter}:
        limiter.reset()
    _setup._setup_failures.clear()
    _auth._ws_auth_failures.clear()


AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
CSRF_HEADER = {"X-Requested-With": "ContainerManager"}
AUTH_CSRF = {**AUTH_HEADER, **CSRF_HEADER}

# ── Target-aware helpers ────────────────────────────────────────────────────
# Let the test suite run against mock (default), a live local Docker
# daemon, or an SSH-tunnelled remote daemon by reading
# SKIFF_TEST_TARGET. Each target has its own Docker host + whether the
# mock fixture should short-circuit.
#
# Values:
#   "mock"   — default; tests use the MagicMock Docker client.
#   "local"  — tests requiring a real daemon use the host's Docker
#              (Colima / Docker Desktop / Linux socket).
#   "remote" — tests requiring a real daemon use an SSH-tunnelled
#              socket the caller has brought up externally.
#   "gcp"    — reserved for future GCE-backed daemon.
#
# Tests that need live Docker MUST request the `live_docker_host`
# fixture; the fixture skips when SKIFF_TEST_TARGET=mock.
import os as _os


def test_target() -> str:
    """Return the active test target, defaulting to mock."""
    return _os.environ.get("SKIFF_TEST_TARGET", "mock").strip().lower()


_VALID_LIVE_TARGETS = frozenset({"local", "remote", "gcp"})


@pytest.fixture()
def live_docker_host() -> str:
    """Skip unless a live-docker target is configured, then return its URL.

    Mock tests never reach this fixture; it's only requested by the
    opt-in integration tests that want a real daemon. Target + host
    are env-driven so the same test matrix works across workstations
    and CI without any hostname embedded in the repo:

      SKIFF_TEST_TARGET      — `mock` (default) | `local` | `remote` | `gcp`
      SKIFF_TEST_DOCKER_HOST — Docker host URL when target ≠ mock;
                               `local` falls back to `DOCKER_HOST` env
                               var then `unix:///var/run/docker.sock`.
    """
    target = test_target()
    if target == "mock":
        pytest.skip("live-docker test; set SKIFF_TEST_TARGET=local|remote|gcp to run")
    if target not in _VALID_LIVE_TARGETS:
        pytest.skip(f"unknown SKIFF_TEST_TARGET={target!r}; expected one of {sorted(_VALID_LIVE_TARGETS)}")
    host = _os.environ.get("SKIFF_TEST_DOCKER_HOST", "").strip()
    if host:
        return host
    # `local` has a sensible fallback — DOCKER_HOST or the kernel
    # socket. Remote / GCP require explicit configuration so the
    # caller doesn't silently hit the wrong daemon.
    if target == "local":
        return _os.environ.get("DOCKER_HOST", "").strip() or "unix:///var/run/docker.sock"
    pytest.skip(f"target {target!r} needs SKIFF_TEST_DOCKER_HOST set")


@pytest.fixture()
def mock_docker() -> MagicMock:
    """Return a MagicMock configured as a Docker client."""
    client = MagicMock()
    client.ping.return_value = True
    client.info.return_value = {
        "ServerVersion": "24.0.7",
        "ContainersRunning": 2,
        "Containers": 5,
        "ContainersPaused": 0,
        "ContainersStopped": 3,
        "Images": 10,
        "NCPU": 4,
        "MemTotal": 8 * 1024**3,
        "OperatingSystem": "Ubuntu 22.04",
        "OSType": "linux",
        "Architecture": "x86_64",
        "KernelVersion": "5.15.0",
        "Driver": "overlay2",
        "LoggingDriver": "json-file",
        "CgroupDriver": "systemd",
        "DockerRootDir": "/var/lib/docker",
        "SecurityOptions": [],
        "RegistryConfig": {"IndexConfigs": {}},
        "ApiVersion": "1.43",
    }
    return client


@pytest.fixture()
def client(mock_docker: MagicMock) -> Generator[TestClient, None, None]:
    """TestClient with auth enabled and Docker client mocked."""
    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = TOKEN
    with (
        patch.object(docker_client_module, "_client", mock_docker),
        patch.object(docker_client_module, "_client_last_ping", float("inf")),
        patch("skiff.docker_client.get_client", return_value=mock_docker),
    ):
        with TestClient(app, raise_server_exceptions=True) as tc:
            yield tc
    config_module._cfg.api_token = original_token


@pytest.fixture()
def noauth_client() -> Generator[TestClient, None, None]:
    """TestClient with auth disabled (api_token='')."""
    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    with TestClient(app, raise_server_exceptions=True) as tc:
        yield tc
    config_module._cfg.api_token = original_token
