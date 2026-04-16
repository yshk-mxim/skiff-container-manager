# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Playwright e2e tests for the first-run setup wizard.

These tests start a server with no API_TOKEN so the wizard is shown.
The tunnel connect test requires SSH access; it is skipped if unavailable.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest
import requests

pytest_plugins = ["tests.conftest_e2e"]

pytest.importorskip("playwright", reason="playwright not installed — run: pip install -e .[dev,e2e] && playwright install chromium")

from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.e2e

E2E_SETUP_PORT = int(os.environ.get("E2E_SETUP_PORT", "18081"))
SETUP_BASE = f"http://127.0.0.1:{E2E_SETUP_PORT}"
E2E_SSH_TUNNEL_TARGET = os.environ.get("E2E_SSH_TUNNEL_TARGET", "")  # user@host for tunnel test


@pytest.fixture(scope="module")
def unconfigured_server():
    """Start a SKIFF server with no API_TOKEN so the setup wizard is shown."""
    env = {
        **os.environ,
        "API_TOKEN": "",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "AUDIT_LOG": "/tmp/skiff-e2e-setup-audit.jsonl",
        "ALLOWED_ORIGINS": SETUP_BASE,
        "RATE_LIMIT_SCALE": "100",
    }
    proc = subprocess.Popen(
        ["uvicorn", "skiff.app:app", "--host", "127.0.0.1", "--port", str(E2E_SETUP_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r = requests.get(f"{SETUP_BASE}/health", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        proc.terminate()
        proc.wait()
        raise RuntimeError("Unconfigured server did not start within 15s")
    yield SETUP_BASE
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def setup_page(unconfigured_server):
    """Headless Chromium page pointed at the unconfigured server."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        pg.goto(unconfigured_server)
        pg.wait_for_selector("text=SKIFF Container Manager", timeout=10_000)
        yield pg
        ctx.close()
        browser.close()


# ── Wizard visibility ──────────────────────────────────────────────────────

def test_setup_wizard_shown_when_unconfigured(setup_page):
    """Wizard appears when server has no API_TOKEN."""
    assert setup_page.locator("text=First-run setup").count() > 0


def test_setup_wizard_has_ssh_and_local_tabs(setup_page):
    assert setup_page.locator("button:has-text('SSH Tunnel')").count() > 0
    assert setup_page.locator("button:has-text('Local / Custom')").count() > 0


def test_setup_wizard_local_tab_switches_panel(setup_page):
    setup_page.locator("button:has-text('Local / Custom')").click()
    setup_page.locator("#sw-host-custom").wait_for(state="visible", timeout=3_000)
    assert not setup_page.locator("#sw-ssh-target").is_visible()


def test_setup_wizard_ssh_tab_restores(setup_page):
    setup_page.locator("button:has-text('Local / Custom')").click()
    setup_page.locator("#sw-host-custom").wait_for(state="visible", timeout=3_000)
    setup_page.locator("button:has-text('SSH Tunnel')").click()
    setup_page.locator("#sw-ssh-target").wait_for(state="visible", timeout=3_000)


# ── Token generation ───────────────────────────────────────────────────────

def test_generate_token_fills_input(setup_page):
    setup_page.locator("button:has-text('Generate')").click()
    token = setup_page.locator("#sw-token").input_value()
    assert len(token) >= 16


def test_generate_token_is_different_each_time(setup_page):
    setup_page.locator("button:has-text('Generate')").click()
    t1 = setup_page.locator("#sw-token").input_value()
    setup_page.locator("button:has-text('Generate')").click()
    t2 = setup_page.locator("#sw-token").input_value()
    assert t1 != t2


# ── Validation ─────────────────────────────────────────────────────────────

def test_submit_without_token_shows_error(setup_page):
    setup_page.locator("button:has-text('Continue (session only)')").click()
    setup_page.wait_for_timeout(500)
    err = setup_page.locator("#sw-error")
    assert err.is_visible()
    assert "token" in err.inner_text().lower()


def test_submit_with_short_token_shows_error(setup_page):
    # Make input editable, then fill with a short token
    setup_page.evaluate("document.getElementById('sw-token').removeAttribute('readonly')")
    setup_page.locator("#sw-token").fill("tooshort")
    setup_page.locator("button:has-text('Continue (session only)')").click()
    setup_page.wait_for_timeout(500)
    assert setup_page.locator("#sw-error").is_visible()


# ── SSH tunnel connect ─────────────────────────────────────────────────────

def test_tunnel_connect_invalid_target_shows_error(setup_page):
    """Entering a bad SSH target (missing @) shows a validation error status."""
    setup_page.locator("#sw-ssh-target").fill("notvalid")
    setup_page.locator("button:has-text('Connect')").click()
    # Wait for fetch to complete — status changes from 'Connecting…' to error
    setup_page.wait_for_function(
        "() => !document.getElementById('sw-tunnel-status').textContent.includes('Connecting')",
        timeout=10_000,
    )
    status_text = setup_page.locator("#sw-tunnel-status").inner_text()
    assert "✗" in status_text or "must be" in status_text


@pytest.mark.skipif(not E2E_SSH_TUNNEL_TARGET, reason="E2E_SSH_TUNNEL_TARGET not set")
def test_tunnel_connect_real_ssh(setup_page):
    """Full tunnel connect test — requires E2E_SSH_TUNNEL_TARGET=user@host."""
    setup_page.locator("#sw-ssh-target").fill(E2E_SSH_TUNNEL_TARGET)
    setup_page.locator("button:has-text('Connect')").click()
    setup_page.wait_for_timeout(20_000)
    status = setup_page.locator("#sw-tunnel-status")
    assert "✓" in status.inner_text()
    host_val = setup_page.evaluate("document.getElementById('sw-host').value")
    assert host_val.startswith("unix://")


# ── Session-only flow (runs last — configures the server in session memory) ─

def test_session_only_setup_completes(setup_page, unconfigured_server):
    """Complete setup via session-only path and verify main app loads."""
    # Switch to local tab to avoid needing real Docker
    setup_page.locator("button:has-text('Local / Custom')").click()
    setup_page.locator("#sw-host-custom").wait_for(state="visible", timeout=3_000)
    setup_page.locator("#sw-host-custom").fill("unix:///var/run/docker.sock")
    setup_page.locator("button:has-text('Generate')").click()
    setup_page.locator("button:has-text('Continue (session only)')").click()
    # After setup, page reloads — should show main app or Docker-unreachable state
    setup_page.wait_for_selector("h2, h3", timeout=15_000)
    # Wizard should be gone
    assert setup_page.locator("text=First-run setup").count() == 0
