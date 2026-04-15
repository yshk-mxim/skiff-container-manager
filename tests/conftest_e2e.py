"""E2E test fixtures using Playwright and a live uvicorn server.

Configuration via environment variables (all optional — defaults work for local dev):

  E2E_DOCKER_HOST        Unix socket or TCP URL for the Docker daemon.
                         Default: unix:///tmp/docker.sock
                         Examples:
                           unix:///var/run/docker.sock   (local Docker Engine)
                           unix:///tmp/docker.sock       (SSH tunnel)
                           tcp://192.168.1.10:2375       (remote TCP)

  E2E_SSH_TUNNEL         If set to "user@host", the fixture opens an SSH tunnel
                         before starting the server and closes it after.
                         E.g.: E2E_SSH_TUNNEL=dev@my-docker-vm
                         When using a GCP VM: E2E_SSH_TUNNEL=user@<GCP_VM_IP>

  E2E_ALLOWED_REGISTRIES Comma-separated registry prefixes to allow.
                         Default: docker.io,ghcr.io

  E2E_PORT               Port for the test server. Default: 18080.

  E2E_TOKEN              Bearer token for the test server. Default: e2e-test-token.

Example — run against a GCP VM:
  E2E_SSH_TUNNEL=dev@10.0.0.5 \\
  E2E_DOCKER_HOST=unix:///tmp/docker.sock \\
  E2E_ALLOWED_REGISTRIES=us-docker.pkg.dev/my-project/ \\
  pytest -m e2e tests/
"""

from __future__ import annotations

import os
import subprocess
import time

import docker
import pytest
import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[assignment]  # e2e tests skipped when playwright not installed

# ── Configuration from environment ─────────────────────────────────────────
E2E_TOKEN = os.environ.get("E2E_TOKEN", "e2e-test-token")
E2E_DOCKER_HOST = os.environ.get("E2E_DOCKER_HOST", "unix:///tmp/docker.sock")
E2E_SSH_TUNNEL = os.environ.get("E2E_SSH_TUNNEL", "")  # "user@host" or ""
E2E_ALLOWED_REGISTRIES = os.environ.get("E2E_ALLOWED_REGISTRIES", "docker.io,ghcr.io")
E2E_PORT = int(os.environ.get("E2E_PORT", "18080"))
BASE_URL = f"http://127.0.0.1:{E2E_PORT}"

# Socket path extracted from DOCKER_HOST for tunnel target and docker_client
_SOCKET_PATH = (
    E2E_DOCKER_HOST.removeprefix("unix://")
    if E2E_DOCKER_HOST.startswith("unix://")
    else None
)


# ── SSH tunnel ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def ssh_tunnel():
    """Open an SSH tunnel if E2E_SSH_TUNNEL is set; otherwise no-op.

    Uses a ControlMaster socket so we can cleanly shut down the tunnel after
    the session without relying on process-group signals.
    """
    if not E2E_SSH_TUNNEL or not _SOCKET_PATH:
        yield
        return

    # Remove stale socket if present
    if os.path.exists(_SOCKET_PATH):
        os.unlink(_SOCKET_PATH)

    ctl_socket = f"/tmp/skiff-e2e-ssh-ctl-{os.getpid()}.sock"
    cmd = [
        "ssh",
        "-fNM",                          # background + ControlMaster
        "-S", ctl_socket,               # control socket path
        "-o", "ControlPersist=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        "-L", f"{_SOCKET_PATH}:/var/run/docker.sock",
        E2E_SSH_TUNNEL,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=20)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"Could not open SSH tunnel to {E2E_SSH_TUNNEL}: {exc}")

    # Wait for socket to appear
    deadline = time.time() + 10
    while time.time() < deadline:
        if _SOCKET_PATH and os.path.exists(_SOCKET_PATH):
            break
        time.sleep(0.3)
    else:
        pytest.skip(f"SSH tunnel socket {_SOCKET_PATH} did not appear within 10s")

    yield

    # Close tunnel via ControlMaster
    subprocess.run(
        ["ssh", "-S", ctl_socket, "-O", "exit", E2E_SSH_TUNNEL],
        capture_output=True,
        check=False,
    )
    for f in (ctl_socket, _SOCKET_PATH):
        try:
            os.unlink(f)
        except OSError:
            pass


# ── Live server ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def live_server(ssh_tunnel):
    """Start uvicorn on E2E_PORT, yield base URL, teardown after session."""
    env = {
        **os.environ,
        "API_TOKEN": E2E_TOKEN,
        "ALLOWED_REGISTRIES": E2E_ALLOWED_REGISTRIES,
        "DOCKER_HOST": E2E_DOCKER_HOST,
        "AUDIT_LOG": "/tmp/skiff-e2e-audit.jsonl",
        "ALLOWED_ORIGINS": BASE_URL,
        "RATE_LIMIT_SCALE": "100",  # 100x limits for e2e test suite
    }
    proc = subprocess.Popen(
        ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(E2E_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Poll /health until it responds (up to 15s)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        proc.terminate()
        proc.wait()
        out, err = proc.communicate()
        raise RuntimeError(
            f"Live server did not start within 15s.\nstdout: {out[:500]}\nstderr: {err[:500]}"
        )

    yield BASE_URL

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── Playwright page ─────────────────────────────────────────────────────────

@pytest.fixture()
def page(live_server):
    """Headless Chromium page, logged in and ready at the containers view."""
    if sync_playwright is None:
        pytest.skip("playwright not installed — run: pip install -e .[dev,e2e] && playwright install chromium")

    js_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        pg = context.new_page()
        pg.on("pageerror", lambda err: js_errors.append(str(err)))

        pg.goto(live_server)
        # Wait for the page to render — either login form or main content
        pg.wait_for_selector("button:has-text('Sign in'), h2, h3", timeout=15_000)
        sign_in = pg.locator("button:has-text('Sign in')")
        if sign_in.count() > 0:
            pg.locator("input[type='password']").fill(E2E_TOKEN)
            sign_in.click()
            # After login, wait for main content (h2) or Docker-unreachable state (h3)
            pg.wait_for_selector("h2, h3", timeout=15_000)

        pg._e2e_js_errors = js_errors  # type: ignore[attr-defined]
        yield pg

        context.close()
        browser.close()


# ── Docker client for setup/teardown ────────────────────────────────────────

@pytest.fixture(scope="session")
def docker_client(ssh_tunnel):
    """Direct Docker SDK client; cleans up e2e- resources after the session."""
    try:
        client = docker.DockerClient(base_url=E2E_DOCKER_HOST, timeout=15)
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker not reachable at {E2E_DOCKER_HOST}: {exc}")
        return

    yield client

    # Teardown: remove all e2e- prefixed resources
    for c in client.containers.list(all=True):
        if c.name.startswith("e2e-"):
            try:
                c.remove(force=True)
            except Exception:
                pass
    for v in client.volumes.list():
        if v.name.startswith("e2e-"):
            try:
                v.remove(force=True)
            except Exception:
                pass
    for n in client.networks.list():
        if n.name.startswith("e2e-"):
            try:
                n.remove()
            except Exception:
                pass
    client.close()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end tests requiring a live server and a Docker daemon",
    )
