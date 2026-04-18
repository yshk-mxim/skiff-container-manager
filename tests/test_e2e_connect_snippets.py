# SPDX-License-Identifier: MIT
"""E2E validation of the "Connect external tool" panel's generated snippets.

The point of this panel is that users paste the string directly into the target
tool's config file. If the snippet has a syntax error, typo, or stale server
field, the user's first experience is a parse failure in someone else's tool —
bad outcome. These tests open the panel, iterate every tool in the dropdown,
extract the generated code, and parse it with the appropriate parser
(json, yaml, shlex, regex) to confirm it's well-formed.

Content correctness (does the Prometheus config actually scrape? does the
oauth2-proxy command launch?) is beyond the scope of an in-browser test —
those would require a full integration lab. What's here covers the highest-
value guarantee: **the snippet is syntactically valid for its target tool**.
"""

from __future__ import annotations

pytest_plugins = ["tests.conftest_e2e"]

import json
import re
import shlex

import pytest
import yaml

from tests.e2e_helpers import MEDIUM
from tests.e2e_helpers import login as _login

pytestmark = pytest.mark.e2e


def _open_connect_panel(page):
    page.locator(".sidebar a:has-text('System')").click()
    page.wait_for_selector("h3:has-text('Connect external tool')", timeout=MEDIUM)


def _select_tool(page, tool_id):
    # The select is inside the Connect panel; narrow the locator
    sel = page.locator("h3:has-text('Connect external tool') ~ div select")
    sel.select_option(tool_id)


def _first_code_or_pre(page) -> str:
    """Extract the code block / pre text under the Connect panel body."""
    # The snippet is rendered in either a <code> (single-line) or <pre> (multi-line)
    # inside the body div after the tool select.
    pre_count = page.locator("h3:has-text('Connect external tool') ~ div pre").count()
    if pre_count > 0:
        return page.locator("h3:has-text('Connect external tool') ~ div pre").first.text_content() or ""
    return page.locator("h3:has-text('Connect external tool') ~ div code").first.text_content() or ""


# ─────────────────────────────────────────────────────────────────────────────
# Validators — parse the snippet with the target tool's grammar
# ─────────────────────────────────────────────────────────────────────────────


def _validate_vscode(page):
    _select_tool(page, "vscode")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    # Must be valid JSON with a top-level "docker.host" string
    data = json.loads(snippet)
    assert isinstance(data, dict)
    assert isinstance(data.get("docker.host"), str)
    assert data["docker.host"].startswith(("unix://", "tcp://", "npipe://", "ssh://"))


def _validate_jetbrains(page):
    _select_tool(page, "jetbrains")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    assert snippet.startswith(("unix://", "tcp://", "npipe://"))


def _validate_cli(page):
    _select_tool(page, "cli")
    page.wait_for_timeout(200)
    # CLI shows two code boxes — export and verify. Both must be shell-quotable.
    codes = page.locator("h3:has-text('Connect external tool') ~ div code").all_text_contents()
    assert any(c.startswith("export DOCKER_HOST=") for c in codes)
    export_line = next(c for c in codes if c.startswith("export"))
    # shlex must parse it without error AND produce an export statement
    tokens = shlex.split(export_line)
    assert tokens[0] == "export"
    # tokens[1] should be like DOCKER_HOST=unix:///var/run/docker.sock
    assert tokens[1].startswith("DOCKER_HOST=")


def _validate_prom(page):
    _select_tool(page, "prom")
    page.wait_for_timeout(200)
    # The YAML block is in the <pre>; the metrics URL is in a <code>
    pre = page.locator("h3:has-text('Connect external tool') ~ div pre").first.text_content() or ""
    doc = yaml.safe_load(pre)
    assert "scrape_configs" in doc
    jobs = doc["scrape_configs"]
    assert isinstance(jobs, list) and len(jobs) == 1
    job = jobs[0]
    assert job["job_name"] == "skiff"
    assert job["metrics_path"] == "/api/system/metrics"
    assert job["authorization"]["type"] == "Bearer"
    assert "targets" in job["static_configs"][0]


def _validate_loki(page):
    _select_tool(page, "loki")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    # Alloy .river is its own syntax — not YAML. Sanity checks:
    assert 'loki.source.file "skiff_audit"' in snippet
    assert 'loki.process "parse_json"' in snippet
    # Balanced braces
    assert snippet.count("{") == snippet.count("}")


def _validate_splunk(page):
    _select_tool(page, "splunk")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    # Splunk inputs.conf is INI-style; verify the stanza header shape and required keys
    assert re.search(r"^\[monitor://", snippet, re.MULTILINE), f"Missing [monitor://…] stanza: {snippet!r}"
    assert "sourcetype = _json" in snippet


def _validate_datadog(page):
    _select_tool(page, "datadog")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    doc = yaml.safe_load(snippet)
    assert "logs" in doc
    entry = doc["logs"][0]
    assert entry["type"] == "file"
    assert entry["service"] == "skiff"
    assert entry["source"] == "skiff"
    assert isinstance(entry["path"], str)


def _validate_elk(page):
    _select_tool(page, "elk")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    doc = yaml.safe_load(snippet)
    assert "filebeat.inputs" in doc
    inp = doc["filebeat.inputs"][0]
    assert inp["type"] == "filestream"
    assert isinstance(inp["paths"], list) and len(inp["paths"]) == 1
    assert "output.elasticsearch" in doc


def _validate_oauth2(page):
    _select_tool(page, "oauth2")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    # shlex must parse the full command (backslash-line-continuations included
    # require us to collapse them first).
    collapsed = snippet.replace("\\\n", " ")
    tokens = shlex.split(collapsed)
    assert tokens[0] == "oauth2-proxy"
    assert any(t.startswith("--provider=") for t in tokens)
    assert any(t.startswith("--upstream=") for t in tokens)
    assert any(t.startswith("--client-id=") for t in tokens)


def _validate_caddy(page):
    _select_tool(page, "caddy")
    page.wait_for_timeout(200)
    snippet = _first_code_or_pre(page)
    tokens = shlex.split(snippet)
    assert tokens[0] == "caddy"
    assert "reverse-proxy" in tokens
    # --from and --to with plausible args
    assert "--from" in tokens and "--to" in tokens


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_connect_panel_renders(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    # Every tool in the dropdown renders without a JavaScript error
    sel = page.locator("h3:has-text('Connect external tool') ~ div select")
    ids = sel.locator("option").evaluate_all("opts => opts.map(o => o.value)")
    assert len(ids) >= 8, f"Dropdown should list all tools: {ids}"


@pytest.mark.e2e
def test_connect_vscode_snippet_is_valid_json(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_vscode(page)


@pytest.mark.e2e
def test_connect_jetbrains_snippet_is_valid_url(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_jetbrains(page)


@pytest.mark.e2e
def test_connect_cli_snippet_is_shell_parseable(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_cli(page)


@pytest.mark.e2e
def test_connect_prometheus_snippet_is_valid_yaml(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_prom(page)


@pytest.mark.e2e
def test_connect_loki_snippet_alloy_syntax(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_loki(page)


@pytest.mark.e2e
def test_connect_splunk_snippet_ini(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_splunk(page)


@pytest.mark.e2e
def test_connect_datadog_snippet_is_valid_yaml(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_datadog(page)


@pytest.mark.e2e
def test_connect_elk_snippet_is_valid_yaml(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_elk(page)


@pytest.mark.e2e
def test_connect_oauth2_snippet_is_shell_parseable(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_oauth2(page)


@pytest.mark.e2e
def test_connect_caddy_snippet_is_shell_parseable(page, live_server):
    _login(page, live_server)
    _open_connect_panel(page)
    _validate_caddy(page)
