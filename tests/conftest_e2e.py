# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""E2E test fixtures using Playwright and a live uvicorn server.

Configuration precedence (highest wins):

  1. Environment variables: E2E_DOCKER_HOST, E2E_SSH_TUNNEL,
     E2E_ALLOWED_REGISTRIES, E2E_PORT, E2E_TOKEN.
  2. `tests/e2e-config.json` (gitignored; see `tests/e2e-config.example.json`).
  3. Built-in defaults.

The JSON file is the stable home for target setup — SSH tunnel user@host,
Docker socket path, registry allowlist — so contributors can point the
suite at their own environment without editing test code or setting env
vars every invocation.

Keys (all optional):
  docker_host         → E2E_DOCKER_HOST (unix://… or tcp://…; default probes local sockets)
  ssh_tunnel          → E2E_SSH_TUNNEL  (user@host for session-scoped tunnel; "" disables)
  allowed_registries  → E2E_ALLOWED_REGISTRIES (comma-separated prefixes)
  port                → E2E_PORT (default 18080)
  token               → E2E_TOKEN (default e2e-test-token)
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time

import docker
import pytest
import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[assignment]  # e2e tests skipped when playwright not installed

# ── Configuration from JSON + environment ──────────────────────────────────
_CONFIG_PATH = pathlib.Path(__file__).parent / "e2e-config.json"
_CONFIG: dict[str, object] = {}
if _CONFIG_PATH.exists():
    try:
        _CONFIG = json.loads(_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        _CONFIG = {}


def _cfg(env_key: str, json_key: str, default: str) -> str:
    """Env var wins, then JSON config, then default."""
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val
    json_val = _CONFIG.get(json_key)
    return str(json_val) if json_val is not None else default


E2E_TOKEN = _cfg("E2E_TOKEN", "token", "e2e-test-token")
E2E_SSH_TUNNEL = _cfg("E2E_SSH_TUNNEL", "ssh_tunnel", "")  # "user@host" or ""
E2E_ALLOWED_REGISTRIES = _cfg("E2E_ALLOWED_REGISTRIES", "allowed_registries", "docker.io,ghcr.io")
E2E_PORT = int(_cfg("E2E_PORT", "port", "18080"))
BASE_URL = f"http://127.0.0.1:{E2E_PORT}"


def _discover_local_docker_host() -> str:
    """Probe the shipped docker_probe.toml paths for a live Unix socket.

    Returns the first reachable path as a `unix://...` URL. The tests
    need a real Docker daemon (fake SDK mocks don't test the UI end-to-
    end), and defaulting to /tmp/docker.sock meant every contributor on
    Colima / OrbStack / Docker Desktop saw /api/system/info → 503 and
    the System page abort partway through render.

    Shares its probe list with `skiff/routers/system.py`
    (skiff/_config/docker_probe.toml) so local dev and the setup wizard see
    the same runtimes.
    """
    from skiff.config import _TOML_DOCKER_PROBE

    for raw in _TOML_DOCKER_PROBE["paths"]:
        p = os.path.expanduser(raw)
        if os.path.exists(p):
            return f"unix://{p}"
    return "unix:///tmp/docker.sock"  # SSH-tunnel default — will fail visibly if Docker is absent


# E2E_DOCKER_HOST priority: explicit env var → discovered local socket.
# When E2E_SSH_TUNNEL is set, the caller is expected to also set
# E2E_DOCKER_HOST to the tunnel-side path (typically /tmp/docker.sock).
E2E_DOCKER_HOST = (
    os.environ.get("E2E_DOCKER_HOST") or str(_CONFIG.get("docker_host") or "") or _discover_local_docker_host()
)

# Socket path extracted from DOCKER_HOST for tunnel target and docker_client
_SOCKET_PATH = E2E_DOCKER_HOST.removeprefix("unix://") if E2E_DOCKER_HOST.startswith("unix://") else None


# ── SSH tunnel ──────────────────────────────────────────────────────────────


def _kill_stale_tunnels(target_host: str, socket_path: str | None) -> None:
    """Kill any orphaned SSH processes forwarding the same target/socket."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"ssh.*{target_host}"],
            capture_output=True,
            text=True,
            check=False,
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
        "-fNM",  # background + ControlMaster
        "-S",
        ctl_socket,  # control socket path
        "-o",
        "ControlPersist=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=6",
        "-L",
        f"{_SOCKET_PATH}:/var/run/docker.sock",
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


@pytest.fixture
def isolated_server():
    """Spawn a short-lived uvicorn on a unique port with custom env.

    Use for tests that need server-side knob overrides that the shared
    session-scoped `live_server` can't provide — e.g. `SETUP_WINDOW_SECS=3`
    for the setup-window expiry regression, or `WS_AUTH_LOCKOUT_SECS=5`
    for the WS lockout banner test. Yields `(base_url, proc)`; teardown
    kills the process.

    Usage::

        def test_x(page, isolated_server):
            url, _proc = isolated_server(env={"SETUP_WINDOW_SECS": "3",
                                              "API_TOKEN": ""})
            page.goto(url)
            ...
    """
    procs: list[subprocess.Popen] = []
    ports_used: list[int] = []

    def _spawn(env: dict[str, str], port: int | None = None) -> tuple[str, subprocess.Popen]:
        # Pick a port that doesn't collide with the session live_server.
        # Shift from E2E_PORT in 10-port increments so parallel test runs
        # don't clobber each other's isolated instances.
        chosen = port or (E2E_PORT + 10 + len(ports_used))
        full_env = {
            **os.environ,
            "ALLOWED_REGISTRIES": E2E_ALLOWED_REGISTRIES,
            "DOCKER_HOST": E2E_DOCKER_HOST,
            "AUDIT_LOG": f"/tmp/skiff-isolated-{chosen}.jsonl",
            "COMPOSE_DIR": f"/tmp/skiff-isolated-compose-{chosen}",
            "ALLOWED_ORIGINS": f"http://127.0.0.1:{chosen}",
            "RATE_LIMIT_SCALE": "100",
            **env,  # caller overrides win
        }
        proc = subprocess.Popen(
            [
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(chosen),
                "--no-proxy-headers",
                "--forwarded-allow-ips",
                "",
            ],
            env=full_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{chosen}"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = requests.get(f"{base}/health", timeout=1)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            proc.terminate()
            raise RuntimeError(f"isolated_server did not start within 10s on :{chosen}")
        procs.append(proc)
        ports_used.append(chosen)
        return base, proc

    yield _spawn

    # Teardown: kill every process the test spawned.
    for proc in procs:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


@pytest.fixture(scope="session")
def live_server(ssh_tunnel):
    """Start uvicorn on E2E_PORT, yield base URL, teardown after session."""
    # Kill any stale server holding the port from a previous interrupted run
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{E2E_PORT}"],
            capture_output=True,
            text=True,
            check=False,
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
        "COMPOSE_DIR": "/tmp/skiff-e2e-compose",  # writable; default /data/compose may be read-only
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
        raise RuntimeError(f"Live server did not start within 15s.\nstdout: {out[:500]}\nstderr: {err[:500]}")

    yield BASE_URL

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── Playwright browser (one process for the whole session) ──────────────────


@pytest.fixture(scope="session")
def browser(live_server):  # shadows playwright's own `browser` fixture name intentionally
    """Single headless Chromium process reused across all tests."""
    if sync_playwright is None:
        pytest.skip('playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium')

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


# ── Screenshot-on-failure hook ───────────────────────────────────────────────
# When an e2e test fails, dump the Playwright page's screenshot + console
# errors to `tests/e2e-artifacts/<testname>.png` for postmortem. Nothing
# writes on passing tests. The hook finds the `page` fixture instance on
# the test's call-time local namespace.
import pathlib

_E2E_ARTIFACT_DIR = pathlib.Path(__file__).parent / "e2e-artifacts"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    pg = None
    # Try Playwright `page` fixture first; fall back to any attribute named `page`.
    if hasattr(item, "funcargs"):
        pg = item.funcargs.get("page")
    if pg is None:
        return
    try:
        _E2E_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = item.nodeid.replace("/", "_").replace(":", "_").replace("::", "_")[:120]
        path = _E2E_ARTIFACT_DIR / f"{safe_name}.png"
        pg.screenshot(path=str(path), full_page=True)
        errors = getattr(pg, "_e2e_js_errors", [])
        if errors:
            (_E2E_ARTIFACT_DIR / f"{safe_name}.js-errors.txt").write_text("\n".join(errors))
    except Exception:
        pass
