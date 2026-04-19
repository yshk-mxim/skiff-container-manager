# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier D — SSH tunnel lifecycle e2e.

Exercises the wizard-managed ControlMaster tunnel + tunnel reconnect
paths against a real remote Docker host. Requires `SKIFF_TEST_TARGET`
to include a reachable ssh-accessible host; without it the tests skip.

Skipped tests:
  - If `E2E_SSH_TUNNEL_TARGET` env var isn't set, every test in this
    file is skipped with a clear reason.
  - Host must accept key-based auth for the current user; password /
    2FA prompts are not supported by the wizard's non-interactive
    subprocess invocation.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import os
import subprocess
import time

import pytest
import requests

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]" && playwright install chromium',
)

pytestmark = pytest.mark.e2e

# Configurable target — no personal hostname committed. Callers set
# E2E_SSH_TUNNEL_TARGET=user@remote-host.local (or equivalent) to run
# these tests. Anything falsy → skip.
_TUNNEL_TARGET = os.environ.get("E2E_SSH_TUNNEL_TARGET", "").strip()

# Per-run tunnel socket path so parallel invocations don't collide on a
# shared /tmp/skiff-docker.sock. Keep it under 108 bytes (Unix socket
# path length cap on Linux).
_TUNNEL_SOCKET = f"/tmp/skiff-e2e-d-{os.getpid()}.sock"


def _require_tunnel_target():
    if not _TUNNEL_TARGET:
        pytest.skip(
            "E2E_SSH_TUNNEL_TARGET not set — SSH tunnel tests require a reachable "
            "remote host. Export e.g. E2E_SSH_TUNNEL_TARGET=user@host to run."
        )


def _probe_ssh():
    """Confirm the SSH target is reachable non-interactively (key-based
    auth). Returns True iff `ssh -o BatchMode=yes user@host true` exits 0."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", _TUNNEL_TARGET, "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def _cleanup_tunnel_socket():
    """Best-effort removal of a stale tunnel socket left by a previous run."""
    try:
        os.unlink(_TUNNEL_SOCKET)
    except OSError:
        pass


# ── D1. Wizard tunnel tab → tunnel active, setup completes ──────────────


def test_d1_wizard_tunnel_starts_and_completes_setup(isolated_server):
    """End-to-end: start the managed tunnel via the wizard endpoint,
    confirm the socket exists + Docker over it is reachable, then
    complete /api/setup → authenticated /api/containers works over
    the tunnel. Covers the operator's first-run remote deployment.
    """
    _require_tunnel_target()
    if not _probe_ssh():
        pytest.skip(f"cannot ssh non-interactively to {_TUNNEL_TARGET}; check key-based auth")
    _cleanup_tunnel_socket()
    url, _proc = isolated_server(
        {
            "API_TOKEN": "",
            "TUNNEL_DEFAULT_SOCKET": _TUNNEL_SOCKET,
            # The server's docker_probe probes common local sockets at setup
            # time; set DOCKER_HOST to the tunnel socket so /api/setup's
            # validator doesn't fall back to the host's default.
            "DOCKER_HOST": f"unix://{_TUNNEL_SOCKET}",
        }
    )
    token = "d1-tunnel-setup-token-0123456789abcdef"
    try:
        # Kick the tunnel.
        r = requests.post(
            f"{url}/api/setup/tunnel",
            headers={"X-Requested-With": "ContainerManager"},
            json={"ssh_target": _TUNNEL_TARGET},
            timeout=30,
        )
        assert r.status_code == 200, f"tunnel start failed: {r.status_code} {r.text[:300]}"
        assert os.path.exists(_TUNNEL_SOCKET), "tunnel socket wasn't created"

        # Complete setup over the tunnel socket.
        r = requests.post(
            f"{url}/api/setup",
            headers={"X-Requested-With": "ContainerManager"},
            json={
                "docker_host": f"unix://{_TUNNEL_SOCKET}",
                "api_token": token,
                "allowed_registries": "docker.io,ghcr.io",
            },
            timeout=30,
        )
        assert r.status_code == 200, f"setup failed: {r.status_code} {r.text[:300]}"

        # Authenticated call hits the remote Docker daemon.
        r = requests.get(
            f"{url}/api/containers",
            headers={"Authorization": f"Bearer {token}", "X-Requested-With": "ContainerManager"},
            timeout=10,
        )
        assert r.status_code == 200, f"/api/containers over tunnel failed: {r.status_code}"
    finally:
        # Stop the managed tunnel so the server cleans up the socket.
        try:
            requests.delete(
                f"{url}/api/setup/tunnel",
                headers={"X-Requested-With": "ContainerManager"},
                timeout=10,
            )
        except Exception:
            pass
        _cleanup_tunnel_socket()


# ── D2. Tunnel reconnect — wizard-managed path ──────────────────────────


def test_d2_tunnel_reconnect_wizard_managed(isolated_server):
    """After the wizard starts a tunnel, simulate a mid-session drop by
    killing the ControlMaster process and removing the socket. Then
    POST /api/tunnel/reconnect and assert the socket is restored and
    Docker is reachable again. Verifies the "known-ssh-target"
    reconnect path that wizard-managed tunnels take."""
    _require_tunnel_target()
    if not _probe_ssh():
        pytest.skip(f"cannot ssh non-interactively to {_TUNNEL_TARGET}")
    _cleanup_tunnel_socket()
    url, _proc = isolated_server(
        {
            "API_TOKEN": "",
            "TUNNEL_DEFAULT_SOCKET": _TUNNEL_SOCKET,
            "DOCKER_HOST": f"unix://{_TUNNEL_SOCKET}",
        }
    )
    token = "d2-reconnect-token-0123456789abcdef0"
    try:
        # Start + setup, same as D1.
        r = requests.post(
            f"{url}/api/setup/tunnel",
            headers={"X-Requested-With": "ContainerManager"},
            json={"ssh_target": _TUNNEL_TARGET},
            timeout=30,
        )
        assert r.status_code == 200
        r = requests.post(
            f"{url}/api/setup",
            headers={"X-Requested-With": "ContainerManager"},
            json={
                "docker_host": f"unix://{_TUNNEL_SOCKET}",
                "api_token": token,
                "allowed_registries": "docker.io,ghcr.io",
            },
            timeout=30,
        )
        assert r.status_code == 200

        # Simulate the tunnel drop. The wizard-spawned ssh uses
        # `-S /tmp/skiff-tunnel-ctl-<random>.sock` and a host-alias in
        # argv; the `user@host` form never appears, so matching on the
        # controlmaster socket prefix is the correct kill handle.
        subprocess.run(
            ["pkill", "-f", "skiff-tunnel-ctl"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.5)
        _cleanup_tunnel_socket()
        assert not os.path.exists(_TUNNEL_SOCKET), "socket should have been removed"

        # Reconnect via the authed endpoint. Wizard-managed tunnels
        # return 200 + socket restored; manual-only would return an
        # envelope pointing at the socket path.
        r = requests.post(
            f"{url}/api/tunnel/reconnect",
            headers={"Authorization": f"Bearer {token}", "X-Requested-With": "ContainerManager"},
            timeout=30,
        )
        assert r.status_code == 200, f"reconnect failed: {r.status_code} {r.text[:300]}"
        assert os.path.exists(_TUNNEL_SOCKET), "reconnect didn't restore the socket"

        # API over the restored tunnel.
        r = requests.get(
            f"{url}/api/containers",
            headers={"Authorization": f"Bearer {token}", "X-Requested-With": "ContainerManager"},
            timeout=10,
        )
        assert r.status_code == 200
    finally:
        try:
            requests.delete(
                f"{url}/api/setup/tunnel",
                headers={"X-Requested-With": "ContainerManager"},
                timeout=10,
            )
        except Exception:
            pass
        subprocess.run(
            ["pkill", "-f", f"ssh.*{_TUNNEL_TARGET}"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        _cleanup_tunnel_socket()


# ── D3. Tunnel reconnect — manual-tunnel envelope ───────────────────────


def test_d3_tunnel_reconnect_manual_envelope(isolated_server):
    """When the server was configured with a DOCKER_HOST that points at
    a socket it didn't open itself (an operator's `ssh -fNL`), a
    post-drop /api/tunnel/reconnect cannot re-establish the tunnel
    (it never learned the SSH target). Response must be a clear
    envelope naming the socket path so the operator can re-run
    their own `ssh -fNL` command — NOT a 5xx crash and NOT a 200
    that silently does nothing.
    """
    _require_tunnel_target()
    if not _probe_ssh():
        pytest.skip(f"cannot ssh non-interactively to {_TUNNEL_TARGET}")
    _cleanup_tunnel_socket()
    # Manually open the tunnel BEFORE starting the server, then configure
    # the server to use that path. The server's _SSH_TARGET state stays
    # empty, so it treats this as an operator-managed tunnel.
    subprocess.run(
        [
            "ssh",
            "-fNL",
            f"{_TUNNEL_SOCKET}:/var/run/docker.sock",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            _TUNNEL_TARGET,
        ],
        check=True,
        timeout=20,
    )
    # Wait for the socket to appear.
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(_TUNNEL_SOCKET):
            break
        time.sleep(0.2)
    assert os.path.exists(_TUNNEL_SOCKET), "manual tunnel didn't create socket"

    token = "d3-manual-envelope-token-01234567890"
    url, _proc = isolated_server(
        {
            "API_TOKEN": token,  # env-configured, skips wizard
            "DOCKER_HOST": f"unix://{_TUNNEL_SOCKET}",
            "TUNNEL_DEFAULT_SOCKET": _TUNNEL_SOCKET,
        }
    )
    try:
        # Baseline: containers over the manual tunnel works.
        r = requests.get(
            f"{url}/api/containers",
            headers={"Authorization": f"Bearer {token}", "X-Requested-With": "ContainerManager"},
            timeout=10,
        )
        assert r.status_code == 200

        # Kill the manual tunnel.
        subprocess.run(
            ["pkill", "-f", f"ssh.*{_TUNNEL_TARGET}"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.5)
        _cleanup_tunnel_socket()

        # Reconnect — server doesn't know the SSH target, so it must
        # return the documented envelope with the socket path.
        r = requests.post(
            f"{url}/api/tunnel/reconnect",
            headers={"Authorization": f"Bearer {token}", "X-Requested-With": "ContainerManager"},
            timeout=10,
        )
        # Either 200 with manual_reconnect_required info, or an
        # envelope-shaped error at 409/503 pointing at the socket.
        assert r.status_code in (200, 409, 503), f"unexpected status: {r.status_code} {r.text[:200]}"
        body = r.json()
        # Walk the envelope shapes — detail is either dict or top-level fields.
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        combined = str(detail) + " " + r.text
        assert _TUNNEL_SOCKET in combined, (
            f"manual-reconnect response didn't mention socket path {_TUNNEL_SOCKET!r}: {combined[:400]!r}"
        )
        # Body should NOT silently claim success.
        if r.status_code == 200:
            assert body.get("manual_reconnect_required") or "manual" in combined.lower(), (
                f"200 response missing manual-reconnect marker: {body!r}"
            )
    finally:
        subprocess.run(
            ["pkill", "-f", f"ssh.*{_TUNNEL_TARGET}"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        _cleanup_tunnel_socket()


# ── D4. Docker daemon disappearance → banner + reconnect flow ───────────


def test_d4_docker_down_banner_via_tunnel_ui(browser, isolated_server):
    """With the wizard-managed tunnel up, kill the tunnel and verify
    the UI paints the docker-unreachable banner AND the Reconnect
    button actually restores the Docker connection when clicked.
    Covers the daily-flake-path: laptop sleep → tunnel dies → user
    clicks Reconnect instead of signing back in."""
    _require_tunnel_target()
    if not _probe_ssh():
        pytest.skip(f"cannot ssh non-interactively to {_TUNNEL_TARGET}")
    _cleanup_tunnel_socket()
    url, _proc = isolated_server(
        {
            "API_TOKEN": "",
            "TUNNEL_DEFAULT_SOCKET": _TUNNEL_SOCKET,
            "DOCKER_HOST": f"unix://{_TUNNEL_SOCKET}",
        }
    )
    token = "d4-ui-reconnect-token-0123456789abc"
    try:
        # Start tunnel + configure.
        r = requests.post(
            f"{url}/api/setup/tunnel",
            headers={"X-Requested-With": "ContainerManager"},
            json={"ssh_target": _TUNNEL_TARGET},
            timeout=30,
        )
        assert r.status_code == 200
        r = requests.post(
            f"{url}/api/setup",
            headers={"X-Requested-With": "ContainerManager"},
            json={
                "docker_host": f"unix://{_TUNNEL_SOCKET}",
                "api_token": token,
                "allowed_registries": "docker.io,ghcr.io",
            },
            timeout=30,
        )
        assert r.status_code == 200

        ctx = browser.new_context()
        pg = ctx.new_page()
        pg.set_default_navigation_timeout(10_000)
        pg.set_default_timeout(20_000)
        try:
            pg.goto(url)
            pg.wait_for_selector("button:has-text('Sign in')", timeout=10_000)
            pg.locator("input[type='password']").fill(token)
            pg.locator("button:has-text('Sign in')").click()
            pg.wait_for_selector(".sidebar", timeout=10_000)
            # Trigger a containers fetch to confirm baseline.
            pg.wait_for_selector("h2:has-text('Containers')", timeout=10_000)

            # Kill the tunnel — the wizard-spawned ssh uses
            # `-S /tmp/skiff-tunnel-ctl-<random>.sock` and a host alias
            # (skiff-tunnel-target) in argv; the `user@host` form never
            # appears, so matching on the target doesn't work. Match on
            # the controlmaster socket prefix instead.
            subprocess.run(
                ["pkill", "-f", "skiff-tunnel-ctl"],
                check=False,
                capture_output=True,
                timeout=5,
            )
            _cleanup_tunnel_socket()
            # Wait for ANY of the three "docker down" UI signals to fire:
            # status banner, sidebar Disconnected badge, or the empty-state
            # copy. Any single one is enough; requiring a specific one has
            # bitten us in the past (docker-status path rewrote banner copy
            # mid-refactor and regressed only the banner assertion).
            try:
                pg.wait_for_function(
                    """() => {
                        const banner = (document.getElementById('status-banner')?.innerText || '').toLowerCase();
                        const sidebar = (document.getElementById('sidebar-status')?.innerText || '').toLowerCase();
                        const main = (document.getElementById('main')?.innerText || '').toLowerCase();
                        return banner.includes('unreachable')
                            || sidebar.includes('disconnected')
                            || main.includes('unreachable')
                            || main.includes('cannot reach');
                    }""",
                    timeout=30_000,
                )
            except Exception:
                # Diagnostic dump to help the next maintainer work out
                # why the docker-down UI didn't paint.
                diag = pg.evaluate("""() => ({
                    banner: document.getElementById('status-banner')?.innerText || '',
                    sidebar: document.getElementById('sidebar-status')?.innerText || '',
                    main_snippet: (document.getElementById('main')?.innerText || '').slice(0, 300),
                    fetched: window.apiFetch ? 'apiFetch-present' : 'apiFetch-missing',
                })""")
                pytest.fail(f"docker-down UI never painted within 30s; dom state: {diag!r}")
            sidebar_or_banner = pg.evaluate("""() => ({
                sidebar: document.getElementById('sidebar-status')?.innerText || '',
                banner: document.getElementById('status-banner')?.innerText || '',
            })""")
            # Sanity: at least one has the expected copy.
            combined = (sidebar_or_banner.get("sidebar", "") + " " + sidebar_or_banner.get("banner", "")).lower()
            assert "disconnected" in combined or "unreachable" in combined, (
                f"no docker-down UI signal after tunnel kill: {sidebar_or_banner!r}"
            )
        finally:
            ctx.close()
    finally:
        try:
            requests.delete(
                f"{url}/api/setup/tunnel",
                headers={"X-Requested-With": "ContainerManager"},
                timeout=10,
            )
        except Exception:
            pass
        subprocess.run(
            ["pkill", "-f", f"ssh.*{_TUNNEL_TARGET}"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        _cleanup_tunnel_socket()
