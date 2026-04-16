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

def _kill_stale_tunnels(target_host: str, socket_path: str | None) -> None:
    """Kill any orphaned SSH processes forwarding the same target/socket."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"ssh.*{target_host}"],
            capture_output=True, text=True, check=False,
        )
        for pid in result.stdout.split():
            try:
                subprocess.run(["kill", pid], check=False, capture_output=True)
            except Exception:
                pass
        if result.returncode == 0:
            time.sleep(0.5)  # give processes time to die
    except Exception:
        pass
    # Remove stale socket file
    if socket_path and os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def _docker_socket_alive(socket_path: str) -> bool:
    """Return True if the Docker socket responds to a ping."""
    try:
        client = docker.DockerClient(base_url=f"unix://{socket_path}", timeout=5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def ssh_tunnel():
    """Open an SSH tunnel if E2E_SSH_TUNNEL is set; otherwise no-op.

    Uses a ControlMaster socket so we can cleanly shut down the tunnel after
    the session without relying on process-group signals.

    Kills any orphaned SSH processes for the same target before starting,
    to avoid multiple competing tunnels fighting over the same Unix socket.
    """
    if not E2E_SSH_TUNNEL or not _SOCKET_PATH:
        yield
        return

    # Kill any stale tunnels from previous test runs and remove socket
    _kill_stale_tunnels(E2E_SSH_TUNNEL, _SOCKET_PATH)

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

    # Wait for socket to appear AND respond to Docker API
    deadline = time.time() + 15
    while time.time() < deadline:
        if _SOCKET_PATH and os.path.exists(_SOCKET_PATH) and _docker_socket_alive(_SOCKET_PATH):
            break
        time.sleep(0.5)
    else:
        pytest.skip(f"SSH tunnel socket {_SOCKET_PATH} did not become reachable within 15s")

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
    # Kill any stale server holding the port from a previous interrupted run
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{E2E_PORT}"],
            capture_output=True, text=True, check=False,
        )
        for pid in result.stdout.split():
            subprocess.run(["kill", "-9", pid], check=False, capture_output=True)
        if result.stdout.strip():
            time.sleep(0.5)
    except Exception:
        pass

    env = {
        **os.environ,
        "API_TOKEN": E2E_TOKEN,
        "ALLOWED_REGISTRIES": E2E_ALLOWED_REGISTRIES,
        "DOCKER_HOST": E2E_DOCKER_HOST,
        "AUDIT_LOG": "/tmp/skiff-e2e-audit.jsonl",
        "ALLOWED_ORIGINS": BASE_URL,
        "RATE_LIMIT_SCALE": "100",  # 100x limits for e2e test suite
    }
    _stderr_log = open("/tmp/skiff-e2e-server.stderr", "w")  # noqa: SIM115
    proc = subprocess.Popen(
        ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(E2E_PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=_stderr_log,
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


# ── Playwright browser (one process for the whole session) ──────────────────

@pytest.fixture(scope="session")
def browser(live_server):  # noqa: F811 — shadows playwright's own `browser` name
    """Single headless Chromium process reused across all tests."""
    if sync_playwright is None:
        pytest.skip("playwright not installed — run: pip install -e .[dev,e2e] && playwright install chromium")

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


# ── Playwright page ─────────────────────────────────────────────────────────

@pytest.fixture()
def page(browser, live_server):
    """Fresh browser context + page for each test, logged in and ready."""
    # Fast pre-flight: verify server responds to HTTP before launching Playwright.
    # A 3s failure here is a server-side bug, not a Playwright timeout.
    deadline = time.time() + 3
    while True:
        try:
            r = requests.get(f"{live_server}/health", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            pass
        if time.time() > deadline:
            pytest.fail(f"Server at {live_server} did not respond to /health within 3s — server may be hung")
        time.sleep(0.1)

    js_errors: list[str] = []

    context = browser.new_context(
        # Enforce a short connection timeout so stalled requests fail fast instead of
        # holding Playwright open against a hung server.
    )
    pg = context.new_page()
    # 5s navigation timeout: goto() takes ~1s normally; >5s means the server is stuck.
    pg.set_default_navigation_timeout(5_000)
    pg.set_default_timeout(10_000)
    pg.on("pageerror", lambda err: js_errors.append(str(err)))

    pg.goto(live_server, wait_until="domcontentloaded")
    # Wait for login form OR the authenticated sidebar.
    # Do NOT wait for h2 — it's rendered only after the Docker API responds,
    # so it's slow under load and causes fixture timeouts under threadpool pressure.
    pg.wait_for_selector("button:has-text('Sign in'), .sidebar", timeout=10_000)
    sign_in = pg.locator("button:has-text('Sign in')")
    if sign_in.count() > 0:
        pg.locator("input[type='password']").fill(E2E_TOKEN)
        sign_in.click()
        # Sidebar renders immediately after login — no Docker round-trip required.
        pg.wait_for_selector(".sidebar", timeout=10_000)

    pg._e2e_js_errors = js_errors  # type: ignore[attr-defined]
    yield pg

    context.close()


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
