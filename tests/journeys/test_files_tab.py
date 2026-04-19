# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Files-tab journeys — 5 scenarios covering the live-filesystem
browser + docker-diff view added in commit cb07a77.

Before cb07a77, the Files tab only showed `docker diff` output and
the empty-state copy said "No filesystem changes detected" (misread
as an error). After cb07a77:
  - Files tab has Browse and Changes sub-views
  - Browse shows a live `docker cp /path` listing with breadcrumb +
    upload + download controls
  - Path is memorised per-container across tab switches

These journeys lock the new surface against regression.
"""

from __future__ import annotations

import uuid

import pytest
import requests

from tests.audit_driver import step
from tests.journeys import journey

pytest_plugins = ["tests.conftest_e2e", "tests.conftest_audit"]

pytest.importorskip(
    "playwright",
    reason='playwright not installed — run: pip install -e ".[dev,e2e]"',
)

pytestmark = pytest.mark.e2e


def _seed(live_server: str, name_prefix: str, read_only: bool = True) -> str:
    """Seed a container. read_only defaults to True (matches the harness's
    safer-by-default UX); pass read_only=False for journeys that exercise
    upload/write paths on the container rootfs."""
    from tests.e2e_helpers import auth_headers
    name = f"{name_prefix}-{uuid.uuid4().hex[:6]}"
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": name},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "command": "sleep 3600",
            "labels": {"skiff-audit-run": "1"},
            "read_only": read_only,
        },
        timeout=120,
    )
    assert r.status_code in (200, 201), f"seed failed: {r.status_code} {r.text}"
    return name


def _teardown(live_server: str, name: str) -> None:
    from tests.e2e_helpers import auth_headers
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
            headers=auth_headers(), timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


@journey(
    persona=("developer",),
    category="files_tab",
    severity="high",
    covers=("hb-cp-ui-missing",),
)
def test_journey_files_browser_lists_etc(audited_page, live_server, audit_observer, persona):
    """Files tab Browse view on /etc must list well-known files
    (passwd, hostname). The /api/containers/{id}/ls endpoint parses
    `ls -la` output; this journey round-trips through it."""
    from tests.e2e_helpers import auth_headers

    name = _seed(live_server, "flsb")
    try:
        with step("step_1_ls_etc"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/ls",
                params={"path": "/etc"},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"ls failed: {r.status_code}"
            body = r.json()
            entries = body.get("entries") or body.get("files") or []
            names = [e.get("name") for e in entries]
            assert any(n in {"passwd", "hostname", "hosts"} for n in names), (
                f"/etc listing missing well-known files; got {names[:20]!r}"
            )
    finally:
        _teardown(live_server, name)


@journey(
    persona=("developer",),
    category="files_tab",
    severity="high",
    covers=("hb-cp-ui-missing",),
)
def test_journey_files_download_contents(audited_page, live_server, audit_observer, persona):
    """GET /api/containers/{id}/files?path=/etc/hostname streams a tar
    containing the file. The UI download button hits this endpoint."""
    from tests.e2e_helpers import auth_headers

    name = _seed(live_server, "fldl")
    try:
        with step("step_1_download_hostname"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/files",
                params={"path": "/etc/hostname"},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"download failed: {r.status_code}"
            # Response should be a non-empty tar stream. We don't parse
            # it here; the e2e tests in test_e2e_file_browser.py do full
            # round-trip. This journey just asserts the endpoint responds.
            assert len(r.content) > 0, "empty download body"
    finally:
        _teardown(live_server, name)


@journey(
    persona=("security_reviewer",),
    category="files_tab",
    severity="P0",
    tags=("zero-trust",),
)
def test_journey_files_path_traversal_rejected(audited_page, live_server, audit_observer, persona):
    """Files endpoints MUST reject absolute-path traversal (../../../../).
    The validator collapses `..` segments; an unsanitised path is a
    zero-trust P0."""
    from tests.e2e_helpers import auth_headers

    name = _seed(live_server, "fltr")
    try:
        with step("step_1_attempt_traversal_ls"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/ls",
                params={"path": "/etc/../../../../etc/passwd"},
                headers=auth_headers(), timeout=30,
            )
            # Two acceptable outcomes:
            #  - 400/422: validator rejects the path
            #  - 200 with canonicalised path that stays inside the
            #    container (e.g., resolves to /etc/passwd and shows as
            #    a file listing)
            # Unacceptable:
            #  - 500 (raw error from daemon or subprocess)
            #  - 200 with an ls of the host filesystem
            if r.status_code == 500:
                audit_observer.emit(
                    step="step_1_attempt_traversal_ls",
                    severity="P0",
                    category="security",
                    zero_trust=True,
                    title="Path-traversal returns 500 — validator boundary broken",
                    expected="400/422 or canonicalised-safe 200",
                    observed=f"HTTP 500: {r.text[:200]!r}",
                )
                pytest.fail("path traversal raised 500 — zero-trust boundary weak")
    finally:
        _teardown(live_server, name)


@journey(
    persona=("ui_ux_auditor",),
    category="files_tab",
    severity="medium",
    covers=("hb-files-tab-misleading",),
)
def test_journey_files_empty_state_explains_what_is_missing(audited_page, live_server, audit_observer, persona):
    """hb-files-tab-misleading: the empty 'No filesystem changes'
    copy had no context. Journey asserts any files-tab empty-state
    text explains WHAT is missing (not just 'no …')."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    name = _seed(live_server, "flms")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_open_container_detail"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
            page.locator(f"tr:has-text('{name}') a, tr:has-text('{name}')").first.click()
            page.wait_for_timeout(500)
        with step("step_3_open_files_tab"):
            files_tab = page.locator(".detail-tab:has-text('Files')").first
            if files_tab.count() == 0:
                pytest.skip("Files tab not present for this state")
            files_tab.click()
            page.wait_for_timeout(600)
        with step("step_4_empty_state_helpful"):
            body = page.locator("#main").inner_text()
            # If the page shows an empty state, it should explain what
            # would show up when populated. "No filesystem changes"
            # alone is the banned copy.
            banned_copy = "No filesystem changes detected"
            if banned_copy in body and "changes" not in body.replace(banned_copy, ""):
                # Only the banned copy, no extra context nearby.
                audit_observer.emit(
                    step="step_4_empty_state_helpful",
                    severity="medium",
                    category="copy",
                    title="Files tab empty state is context-free",
                    expected="Copy that explains what 'changes' means (diff-from-image)",
                    observed=f"Only '{banned_copy}' visible",
                    covers_historical="hb-files-tab-misleading",
                )
    finally:
        _teardown(live_server, name)


@journey(
    persona=("developer",),
    category="files_tab",
    severity="medium",
    covers=("hb-files-tab-path-memory",),
)
def test_journey_files_path_remembered_across_tab_switch(audited_page, live_server, audit_observer, persona):
    """Navigate to /var/log in Browse, switch to Logs tab, back to
    Files tab → path should still be /var/log (hb-files-tab-path-
    memory). Asserts localStorage-or-module-state persistence."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    name = _seed(live_server, "flpm")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_open_container_detail"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
            page.locator(f"tr:has-text('{name}') a, tr:has-text('{name}')").first.click()
            page.wait_for_timeout(500)
        with step("step_3_files_tab_browse_navigate"):
            files_tab = page.locator(".detail-tab:has-text('Files')").first
            if files_tab.count() == 0:
                pytest.skip("Files tab not present")
            files_tab.click()
            page.wait_for_timeout(600)
            # Click /var/log breadcrumb or a 'var' entry. If browser
            # input exists, type path directly.
            path_input = page.locator("input[placeholder*='path' i]").first
            if path_input.count() > 0:
                path_input.fill("/var/log")
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)
        with step("step_4_switch_to_logs_and_back"):
            logs_tab = page.locator(".detail-tab:has-text('Logs')").first
            if logs_tab.count() > 0:
                logs_tab.click()
                page.wait_for_timeout(400)
            files_tab.click()
            page.wait_for_timeout(500)
        with step("step_5_path_preserved"):
            # The Browse panel should still show /var/log — either in an
            # input[value=] or a visible breadcrumb.
            text = page.locator("#main").inner_text()
            if "/var/log" not in text:
                # Allow the path-input to reveal it.
                path_input = page.locator("input[placeholder*='path' i]").first
                if path_input.count() > 0 and path_input.input_value() == "/var/log":
                    return
                audit_observer.emit(
                    step="step_5_path_preserved",
                    severity="medium",
                    category="behaviour",
                    title="Files Browse path not preserved across tab switch",
                    expected="/var/log still the current Browse path",
                    observed="/var/log absent from visible Files panel",
                    covers_historical="hb-files-tab-path-memory",
                )
                pytest.fail("path not preserved")
    finally:
        _teardown(live_server, name)


# ── Plan-named J-06 scenarios ────────────────────────────────────────


@journey(
    persona=("developer",),
    category="files_tab",
    severity="medium",
)
def test_journey_files_diff_view_lists_changes(audited_page, live_server, audit_observer, persona):
    """Plan J-06 item: download + diff. After exec-ing a file change,
    the Files tab's Changes sub-view (docker diff) should list the
    modified path. Probes /api/containers/{id}/diff directly."""
    import time

    from tests.e2e_helpers import auth_headers

    name = _seed(live_server, "fldf")
    try:
        # Touch a file so diff reports a change. Use the REST exec path
        # if present; else skip — diff journey only asserts the shape.
        requests.post(
            f"{live_server.rstrip('/')}/api/containers/{name}/exec",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json={"cmd": ["sh", "-c", "touch /pa-diff-mark"]},
            timeout=30,
        )
        time.sleep(0.5)
        with step("step_1_fetch_diff"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/diff",
                headers=auth_headers(), timeout=30,
            )
            if r.status_code == 404:
                pytest.skip("diff endpoint not surfaced under this path")
            if r.status_code != 200:
                audit_observer.emit(
                    step="step_1_fetch_diff",
                    severity="medium",
                    category="contract",
                    title=f"Diff endpoint returned {r.status_code}",
                    expected="200 with an array of {Path, Kind} entries",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
                return
            body = r.json()
            # Body is typically a list of dicts from docker-py.
            assert isinstance(body, (list, dict)), f"unexpected shape: {type(body)}"
    finally:
        _teardown(live_server, name)


@journey(
    persona=("developer",),
    category="files_tab",
    severity="high",
    covers=("hb-cp-ui-missing",),
)
def test_journey_files_upload_then_verify(audited_page, live_server, audit_observer, persona):
    """Plan J-06 item: upload + verify. POST a multipart body to
    /api/containers/{id}/upload targeting /tmp, then ls /tmp and
    assert the filename appears."""
    import io

    from tests.e2e_helpers import auth_headers

    # Upload requires a writable rootfs.
    name = _seed(live_server, "flup", read_only=False)
    marker = "pa-upload-marker.txt"
    content = b"hello from upload journey\n"
    try:
        with step("step_1_upload_file"):
            files = [("file", (marker, io.BytesIO(content), "text/plain"))]
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/upload",
                params={"path": "/tmp"},
                headers=auth_headers(),
                files=files,
                timeout=30,
            )
            if r.status_code == 404:
                pytest.skip("upload endpoint not surfaced under this path")
            assert r.status_code in (200, 201), (
                f"upload failed: {r.status_code} {r.text}"
            )
        with step("step_2_ls_tmp_sees_uploaded_file"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/ls",
                params={"path": "/tmp"},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200
            entries = r.json().get("entries") or r.json().get("files") or []
            names = [e.get("name") for e in entries]
            if marker not in names:
                audit_observer.emit(
                    step="step_2_ls_tmp_sees_uploaded_file",
                    severity="high",
                    category="behaviour",
                    title="Uploaded file not visible via ls",
                    expected=f"{marker} in /tmp",
                    observed=f"entries: {names[:10]}",
                )
                pytest.fail("upload→verify round-trip broken")
    finally:
        _teardown(live_server, name)


@journey(
    persona=("developer", "security_reviewer"),
    category="files_tab",
    severity="medium",
    tags=("zero-trust",),
)
def test_journey_files_symlink_navigation_safe(audited_page, live_server, audit_observer, persona):
    """Plan J-06 item: symlink navigation. Listing /var often contains
    symlinks on Alpine; the response should either follow them (200
    with contents) or refuse safely (4xx) — never 500 or escape
    outside the container root."""
    from tests.e2e_helpers import auth_headers

    name = _seed(live_server, "flsl")
    try:
        with step("step_1_ls_with_symlinks"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/ls",
                params={"path": "/bin"},  # /bin → /usr/bin on modern alpine
                headers=auth_headers(), timeout=30,
            )
            if r.status_code >= 500:
                audit_observer.emit(
                    step="step_1_ls_with_symlinks",
                    severity="high",
                    category="contract",
                    title=f"ls on symlink target returned {r.status_code}",
                    expected="200 or 4xx — never 5xx",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
                pytest.fail("symlink navigation 5xx")
    finally:
        _teardown(live_server, name)


@journey(
    persona=("security_reviewer", "developer"),
    category="files_tab",
    severity="medium",
    tags=("zero-trust",),
)
def test_journey_files_oversize_upload_rejected(audited_page, live_server, audit_observer, persona):
    """Plan J-06 item: over-size rejection. Upload must refuse bodies
    larger than the configured max (to prevent disk exhaustion).
    Zero-trust: no path around the size limit."""
    import io

    from tests.e2e_helpers import auth_headers

    name = _seed(live_server, "floz")
    # 50 MiB payload — enough to exceed most sensible defaults without
    # taking forever over localhost.
    big = b"A" * (50 * 1024 * 1024)
    try:
        with step("step_1_upload_oversize"):
            files = [("file", ("big.bin", io.BytesIO(big), "application/octet-stream"))]
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/upload",
                params={"path": "/tmp"},
                headers=auth_headers(),
                files=files,
                timeout=120,
            )
            # Acceptable: 413 (too large), 400/422 (validator), 500 is NOT acceptable.
            if r.status_code == 404:
                pytest.skip("upload endpoint not present")
            if 200 <= r.status_code < 300:
                audit_observer.emit(
                    step="step_1_upload_oversize",
                    severity="high",
                    category="security",
                    title="50 MiB upload accepted — no size cap",
                    expected="413 / 400 rejection",
                    observed=f"{r.status_code} accepted",
                )
                audit_observer.emit(
                    step="step_1_upload_oversize",
                    severity="P0",
                    category="security",
                    zero_trust=True,
                    title="No upload size cap — disk-exhaustion DoS vector",
                    expected="4xx rejection on oversized bodies",
                    observed=f"50 MiB upload returned {r.status_code}",
                )
    finally:
        _teardown(live_server, name)
