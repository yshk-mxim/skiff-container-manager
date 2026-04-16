"""Shared fixtures for SKIFF Container Manager tests."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import skiff.docker_client as docker_client_module
from app import app

TOKEN = "test-secret-token"


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi rate limiter storage before each test to prevent cross-test pollution."""
    import app as _app_module
    _app_module.limiter.reset()
    yield
    _app_module.limiter.reset()
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
CSRF_HEADER = {"X-Requested-With": "ContainerManager"}
AUTH_CSRF = {**AUTH_HEADER, **CSRF_HEADER}


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
    original_token = app_module._cfg.api_token
    app_module._cfg.api_token = TOKEN
    with (
        patch.object(docker_client_module, "_client", mock_docker),
        patch.object(docker_client_module, "_client_last_ping", float("inf")),
        patch("skiff.docker_client.get_client", return_value=mock_docker),
    ):
        with TestClient(app, raise_server_exceptions=True) as tc:
            yield tc
    app_module._cfg.api_token = original_token


@pytest.fixture()
def noauth_client() -> Generator[TestClient, None, None]:
    """TestClient with auth disabled (api_token='')."""
    original_token = app_module._cfg.api_token
    app_module._cfg.api_token = ""
    with TestClient(app, raise_server_exceptions=True) as tc:
        yield tc
    app_module._cfg.api_token = original_token
