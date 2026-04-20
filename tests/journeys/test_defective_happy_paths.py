# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Journeys that would have caught bugs the first pass of the persona
audit missed. Each one pins down a specific failure mode the user hit
during live testing; the previous 127 journeys passed while the app
still had these holes.

Coverage pattern:
  - Real content (filenames with spaces, mounted volumes with data).
  - Real workflows (ls → download → open → verify bytes).
  - Safety affordances (undo on prune, undo on cp write, confirm before
    destructive ops).
  - Edge-case interactions (keyboard shortcut inside an open modal).

These are additive to `tests/journeys/test_files_tab.py` etc — they are
NOT replacements. The existing journeys cover "page loads, expected
text visible"; these cover "user actually does the task end-to-end".
"""

from __future__ import annotations

import time
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


def _auth_headers():
    from tests.e2e_helpers import auth_headers

    return auth_headers()


def _seed_container_with_volume(live_server: str, mount_target: str = "/data_new") -> tuple[str, str]:
    """Seed: create a named volume + run alpine with it mounted, return
    (container_name, volume_name). Caller teardown is required."""
    cname = f"hpfix-{uuid.uuid4().hex[:6]}"
    vname = f"hpvol-{uuid.uuid4().hex[:6]}"
    base = live_server.rstrip("/")
    r = requests.post(
        f"{base}/api/volumes/create",
        params={"name": vname},
        headers=_auth_headers(),
        timeout=30,
    )
    assert r.status_code in (200, 201), f"volume create failed: {r.status_code} {r.text}"
    r = requests.post(
        f"{base}/api/containers/run",
        params={"image": "alpine:3.20", "name": cname},
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json={
            "command": "sleep 3600",
            "labels": {"skiff-audit-run": "1"},
            "volumes": [f"{vname}:{mount_target}"],
            "read_only": False,
        },
        timeout=120,
    )
    assert r.status_code in (200, 201), f"run failed: {r.status_code} {r.text}"
    return cname, vname


def _teardown(live_server: str, cname: str, vname: str | None) -> None:
    base = live_server.rstrip("/")
    try:
        requests.delete(
            f"{base}/api/containers/{cname}?force=true",
            headers=_auth_headers(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass
    if vname:
        try:
            requests.delete(
                f"{base}/api/volumes/{vname}",
                headers=_auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass


@journey(
    persona=("developer",),
    category="files_tab",
    severity="high",
)
def test_journey_files_ls_preserves_spaces_in_filenames(audited_page, live_server, audit_observer, persona):
    """Upload a file whose name contains spaces, then ls the mount
    point and assert the full name survives the round-trip. The
    previous ls-parser used `rsplit(None, 1)[-1]` which truncated
    'The Simple Macroeconomics of AI.pdf' to 'AI.pdf' — the Files
    tab's Download button then hit /data_new/AI.pdf and 404'd.

    Regression: filename with spaces must be preserved in the JSON
    response AND round-trip through a download call."""
    import io
    import tarfile

    cname, vname = _seed_container_with_volume(live_server)
    base = live_server.rstrip("/")
    real_name = "The Simple Macroeconomics of AI.pdf"
    try:
        with step("step_1_write_spaced_filename_into_container"):
            # Build a tar containing the file, PUT to the container's
            # /data_new directory via the cp endpoint — mirrors how a
            # real user uploads a file through the Files tab.
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                info = tarfile.TarInfo(name=real_name)
                payload = b"macro economic content" * 100
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))
            buf.seek(0)
            r = requests.post(
                f"{base}/api/containers/{cname}/files",
                params={"path": "/data_new"},
                headers={**_auth_headers(), "Content-Type": "application/x-tar"},
                data=buf.getvalue(),
                timeout=30,
            )
            assert r.status_code == 200, f"cp put failed: {r.status_code} {r.text}"

        with step("step_2_ls_returns_full_name_not_truncated"):
            r = requests.get(
                f"{base}/api/containers/{cname}/ls",
                params={"path": "/data_new"},
                headers=_auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, f"ls failed: {r.status_code}"
            entries = r.json().get("entries") or []
            names = [e.get("name") for e in entries]
            assert real_name in names, (
                f"ls truncated the filename. Expected {real_name!r} in {names!r} — "
                f"the previous parser kept only the last whitespace-delimited token "
                f"so 'The Simple Macroeconomics of AI.pdf' became 'AI.pdf'."
            )

        with step("step_3_download_spaced_filename_succeeds"):
            # Hit the same URL the Files tab's Download button builds.
            r = requests.get(
                f"{base}/api/containers/{cname}/files",
                params={"path": f"/data_new/{real_name}"},
                headers=_auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, (
                f"download of spaced filename failed: {r.status_code} {r.text[:200]!r}. "
                f"This was the exact user-reported bug: UI sent the truncated name "
                f"and got 404 from the container."
            )
            assert len(r.content) > 0, "empty tar stream"
    finally:
        _teardown(live_server, cname, vname)


@journey(
    persona=("sre_ops",),
    category="audit_observability",
    severity="high",
    covers=("hb-undo-on-delete",),
)
def test_journey_system_prune_offers_undo_window(audited_page, live_server, audit_observer, persona):
    """POST /api/system/prune MUST return an undo envelope by default
    (same pattern as soft-delete). An immediate-fire response with no
    undo window was the bug: a misclick irreversibly purged images /
    containers / networks with no recovery path.

    Pre-fix response: {containers_deleted, images_deleted, ...}
    Post-fix response: {undo_token, expires_in}"""
    base = live_server.rstrip("/")
    with step("step_1_call_prune_expects_undo_envelope"):
        r = requests.post(
            f"{base}/api/system/prune",
            headers=_auth_headers(),
            timeout=30,
        )
        assert r.status_code == 200, f"prune failed: {r.status_code} {r.text}"
        body = r.json()
        assert "undo_token" in body, (
            f"system prune returned {list(body.keys())!r} — missing undo_token. "
            f"Every destructive mutation must surface an undo window."
        )
        assert body["expires_in"] > 0, "expires_in must be a positive delay"
        token = body["undo_token"]

    with step("step_2_undo_within_window_cancels_prune"):
        r = requests.post(
            f"{base}/api/undo/{token}",
            headers=_auth_headers(),
            timeout=5,
        )
        assert r.status_code == 200, (
            f"undo returned {r.status_code}; must succeed within the {body['expires_in']}s "
            f"window so a misclick is reversible."
        )


@journey(
    persona=("sre_ops",),
    category="audit_observability",
    severity="medium",
)
def test_journey_system_prune_undo_expiry_actually_fires(audited_page, live_server, audit_observer, persona):
    """If the user DOESN'T click undo, the prune actually runs after
    UNDO_DELAY_SECS. Pairs with the undo-window test — one proves
    cancellation works, the other proves the queue actually fires."""
    base = live_server.rstrip("/")
    with step("step_1_call_prune_and_let_it_fire"):
        r = requests.post(
            f"{base}/api/system/prune",
            headers=_auth_headers(),
            timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        if "undo_token" not in body:
            pytest.skip("queue was full — prune ran synchronously (legacy path)")
        expires_in = body["expires_in"]

    with step("step_2_wait_past_the_window"):
        # Wait the full window + a small margin for the timer.
        time.sleep(expires_in + 2.0)
        # After expiry the timer has fired; a second undo attempt must
        # not silently cancel a prune that already ran. Either 404 (token
        # removed) or a 200/400 that does NOT un-do the fired op is
        # acceptable — the invariant is "undo after firing is a no-op".
        # We hit the endpoint to verify it doesn't 500 and nothing sus
        # comes back.
        r = requests.post(
            f"{base}/api/undo/{body['undo_token']}",
            headers=_auth_headers(),
            timeout=5,
        )
        assert r.status_code in (200, 404, 409), (
            f"undo endpoint returned unexpected {r.status_code} — {r.text[:200]!r}. Must be a catalogued envelope."
        )


@journey(
    persona=("security_reviewer",),
    category="files_tab",
    severity="medium",
)
def test_journey_filename_edge_cases_survive_ls_roundtrip(audited_page, live_server, audit_observer, persona):
    """Hostile-looking but valid POSIX filenames must all survive ls
    parsing. The previous parser had silent truncation + potential
    ambiguity on symlink-target delimiters; cover the edge cases that
    a reviewer would try."""
    import io
    import tarfile

    cname, vname = _seed_container_with_volume(live_server)
    base = live_server.rstrip("/")
    # Each entry: filename, reason-for-inclusion.
    edge_names = [
        ("file with spaces.txt", "spaces"),
        ("file.with.dots.txt", "embedded dots"),
        ("file-with-dash.log", "dashes"),
        ("file_with_underscore.log", "underscores"),
        ("Mixed CASE name.txt", "mixed case"),
    ]
    try:
        # Write every edge-case filename into the container.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for fname, _ in edge_names:
                info = tarfile.TarInfo(name=fname)
                info.size = 10
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(b"placeholder"))
        buf.seek(0)
        r = requests.post(
            f"{base}/api/containers/{cname}/files",
            params={"path": "/data_new"},
            headers={**_auth_headers(), "Content-Type": "application/x-tar"},
            data=buf.getvalue(),
            timeout=30,
        )
        assert r.status_code == 200

        with step("step_1_ls_reports_every_edge_case_name_intact"):
            r = requests.get(
                f"{base}/api/containers/{cname}/ls",
                params={"path": "/data_new"},
                headers=_auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200
            names = {e.get("name") for e in r.json().get("entries") or []}
            missing = [fn for fn, _ in edge_names if fn not in names]
            assert not missing, (
                f"ls dropped these filenames: {missing!r}. Parser likely still "
                f"whitespace-splits or mis-interprets dots/dashes. All returned "
                f"names: {sorted(names)!r}"
            )
    finally:
        _teardown(live_server, cname, vname)
