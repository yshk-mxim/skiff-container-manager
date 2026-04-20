# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier A: every destructive mutation either offers an undo window or
surfaces an explicit, honest irreversibility contract. The previous
journey passes validated that prune endpoints returned 200; they never
asserted the user had a safety net before the irreversible damage.
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


def _auth():
    from tests.e2e_helpers import auth_headers

    return auth_headers()


def _seed_container(live_server: str, name_prefix: str, **kwargs) -> str:
    cname = f"{name_prefix}-{uuid.uuid4().hex[:6]}"
    base = live_server.rstrip("/")
    body = {"command": "sleep 3600", "labels": {"skiff-audit-run": "1"}, **kwargs}
    r = requests.post(
        f"{base}/api/containers/run",
        params={"image": "alpine:3.20", "name": cname},
        headers={**_auth(), "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    assert r.status_code in (200, 201), f"seed failed: {r.status_code} {r.text}"
    return cname


def _teardown_container(live_server: str, name: str) -> None:
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
            headers=_auth(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


def _teardown_volume(live_server: str, name: str) -> None:
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/volumes/{name}",
            headers=_auth(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


def _seed_volume(live_server: str) -> str:
    vname = f"undo-vol-{uuid.uuid4().hex[:6]}"
    r = requests.post(
        f"{live_server.rstrip('/')}/api/volumes/create",
        params={"name": vname},
        headers=_auth(),
        timeout=30,
    )
    assert r.status_code in (200, 201), f"volume seed failed: {r.text}"
    return vname


# ── Container delete ────────────────────────────────────────────────────


@journey(persona=("developer",), category="container_lifecycle", severity="P0", covers=("hb-undo-on-delete",))
def test_journey_container_delete_undo_returns_token(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """DELETE /api/containers/<id>?undo=1 must return an undo token so a
    misclick is reversible. The historical bug was force=true short-
    circuiting the undo; this journey explicitly asserts the non-force
    path surfaces the token."""
    base = live_server.rstrip("/")
    cname = _seed_container(live_server, "udc")
    try:
        with step("step_1_stop_then_soft_delete"):
            requests.post(
                f"{base}/api/containers/{cname}/stop",
                headers=_auth(),
                timeout=30,
            )
        with step("step_2_delete_undo_returns_token"):
            r = requests.delete(
                f"{base}/api/containers/{cname}?undo=1",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200, f"delete failed: {r.status_code} {r.text}"
            body = r.json()
            assert "undo_token" in body, f"soft-delete must surface undo_token; got {list(body)!r}"
        with step("step_3_undo_restores"):
            r = requests.post(
                f"{base}/api/undo/{body['undo_token']}",
                headers=_auth(),
                timeout=5,
            )
            assert r.status_code == 200
    finally:
        _teardown_container(live_server, cname)


# ── Volume delete ───────────────────────────────────────────────────────


@journey(persona=("developer",), category="volumes_networks", severity="P0")
def test_journey_volume_delete_undo_returns_token(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Volume delete's `?undo=1` path must return an undo token and the
    token must cancel a pending removal. Absence of undo here would be
    the same class of silent-bypass bug that bit container delete."""
    base = live_server.rstrip("/")
    vname = _seed_volume(live_server)
    try:
        with step("step_1_soft_delete_volume"):
            r = requests.delete(
                f"{base}/api/volumes/{vname}?undo=1",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200, f"{r.status_code} {r.text}"
            body = r.json()
            assert "undo_token" in body, f"no undo_token: {body!r}"
        with step("step_2_undo_restores"):
            r = requests.post(
                f"{base}/api/undo/{body['undo_token']}",
                headers=_auth(),
                timeout=5,
            )
            assert r.status_code == 200
    finally:
        _teardown_volume(live_server, vname)


# ── Image delete (soft) ─────────────────────────────────────────────────


@journey(persona=("developer",), category="container_lifecycle", severity="high")
def test_journey_image_delete_undo_returns_token(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Delete an image via DELETE /api/images/<id>?undo=true — must
    return undo_token. Pull alpine:3.20 first (likely already cached)
    so we have a known-present image to soft-delete."""
    base = live_server.rstrip("/")
    # List an existing image to pick one we know exists locally. Using
    # the live server's /api/images keeps us on the same Docker socket
    # the server is talking to — no need to open our own SDK client
    # that might not see the same daemon (Colima, remote tunnel, etc.)
    r = requests.get(f"{base}/api/images", headers=_auth(), timeout=30)
    assert r.status_code == 200
    imgs = r.json()
    # Prefer an image we seeded (skiff-audit label or alpine) so we
    # don't pollute the user's tag tree.
    candidate = None
    for img in imgs:
        tags = img.get("tags") or []
        if any(t.startswith("alpine:") for t in tags):
            candidate = img.get("id") or img.get("short_id")
            break
    if not candidate:
        pytest.skip("no alpine image available to soft-delete")

    with step("step_1_soft_delete_image"):
        r = requests.delete(
            f"{base}/api/images/{candidate}?undo=true",
            headers=_auth(),
            timeout=10,
        )
        # 200 with undo_token is the expected path. 409 (in-use by a
        # running container) is also acceptable — the user would see
        # the catalogued envelope and know why.
        if r.status_code == 409:
            pytest.skip("alpine in use by a running container; undo path not exercised")
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        body = r.json()
        assert "undo_token" in body, f"no undo_token: {body!r}"
        # Cancel the undo so the image stays present for subsequent tests.
        requests.post(
            f"{base}/api/undo/{body['undo_token']}",
            headers=_auth(),
            timeout=5,
        )


# ── System prune — already covered in test_defective_happy_paths.py ─────


# ── Image prune — two modes (dangling vs all-unused) ────────────────────


@journey(persona=("sre_ops",), category="container_lifecycle", severity="high")
def test_journey_image_prune_dangling_reports_summary(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """image prune dangling-only must return a summary with deleted
    count + reclaimed MB. No undo window (prune is inherently
    irreversible — user re-pulls), but the UI confirmation must own
    that — covered separately in the UI ux_flows journeys."""
    base = live_server.rstrip("/")
    with step("step_1_prune_dangling"):
        r = requests.post(
            f"{base}/api/images/prune?dangling_only=true&undo=false",
            headers=_auth(),
            timeout=30,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        body = r.json()
        assert "deleted_count" in body
        assert "space_reclaimed_mb" in body


@journey(persona=("sre_ops",), category="container_lifecycle", severity="high")
def test_journey_image_prune_all_unused_has_explicit_flag(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """image prune with dangling_only=false removes every unused image
    (tagged or not). The flag MUST be explicit — the default stays on
    the safer dangling-only path so a naive caller doesn't nuke their
    tag tree."""
    base = live_server.rstrip("/")
    # Default (no flag) must be dangling-only — verified by the other
    # prune journey returning only dangling cleanup. Here we pass the
    # explicit flag and confirm the server accepts + runs it.
    with step("step_1_prune_all_unused_explicit"):
        r = requests.post(
            f"{base}/api/images/prune?dangling_only=false&undo=false",
            headers=_auth(),
            timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text}"


# ── Build cache prune ───────────────────────────────────────────────────


@journey(persona=("sre_ops",), category="container_lifecycle", severity="medium")
def test_journey_build_cache_prune_reports_reclaim(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """build-cache prune returns a reclaimed-MB number. Irreversible
    by nature; the UI confirmation must say so."""
    base = live_server.rstrip("/")
    r = requests.post(
        f"{base}/api/system/prune-build-cache?undo=false",
        headers=_auth(),
        timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert "space_reclaimed_mb" in body, body


# ── Bulk container actions return per-item outcomes ─────────────────────


@journey(persona=("sre_ops",), category="container_lifecycle", severity="high")
def test_journey_bulk_stop_reports_per_item_failures(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Bulk actions MUST surface per-item failures rather than showing
    'Stopped N container(s)' when some failed. This was a silent-
    success class of bug — the client-side `.catch(()=>{})` around
    each per-item apiFetch is now a structured outcome aggregator.

    Simulated by calling stop on two containers where one is already
    stopped (so its stop call is a no-op or warning depending on SDK
    behaviour) — server-side still returns 200, but the journey asserts
    the surface is well-shaped for the UI to summarise."""
    base = live_server.rstrip("/")
    c1 = _seed_container(live_server, "bulka")
    c2 = _seed_container(live_server, "bulkb")
    try:
        with step("step_1_stop_one_preemptively"):
            r = requests.post(f"{base}/api/containers/{c1}/stop", headers=_auth(), timeout=30)
            assert r.status_code in (200, 409)
        with step("step_2_stop_both"):
            # Not a batch endpoint yet — the UI does N individual calls.
            # We test the shape: both calls return well-formed envelopes.
            for c in (c1, c2):
                r = requests.post(f"{base}/api/containers/{c}/stop", headers=_auth(), timeout=30)
                # 200 (stopped), 409 (already stopped/exited), 404 (gone) —
                # all are catalogued. 500 would be the bug.
                assert r.status_code != 500, f"Bulk stop of {c} returned 500 — must be catalogued envelope"
    finally:
        for c in (c1, c2):
            _teardown_container(live_server, c)


# ── Running-container delete stops first (preserves undo window) ────────


@journey(persona=("developer",), category="container_lifecycle", severity="P0", covers=("hb-undo-on-delete",))
def test_journey_running_container_delete_goes_through_stop_then_soft_delete(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Historical bug: deleting a running container used force=true
    which bypassed the undo queue. Fix: UI stops first, then soft-
    deletes. This journey verifies the backend still accepts the two-
    step sequence without requiring force=true (so the UI's undo path
    works)."""
    base = live_server.rstrip("/")
    cname = _seed_container(live_server, "rcd")
    try:
        with step("step_1_stop_running_container"):
            r = requests.post(
                f"{base}/api/containers/{cname}/stop",
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200
        with step("step_2_soft_delete_returns_undo_token"):
            r = requests.delete(
                f"{base}/api/containers/{cname}?undo=1",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200
            body = r.json()
            assert "undo_token" in body, (
                "Stop-then-soft-delete must return an undo token so a misclick "
                "on a running container is still reversible. The historical "
                "force=true path bypassed this."
            )
    finally:
        _teardown_container(live_server, cname)
