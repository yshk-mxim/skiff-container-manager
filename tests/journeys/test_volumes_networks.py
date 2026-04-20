# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Volume + network journeys — 6 scenarios covering the parts of the
CRUD surface that commit 38f9ce4 expanded.

Goal: lock against the hb-volume-create-skinny-form and
hb-network-create-skinny-form regressions so those parameter knobs
don't get trimmed out of the modals during a CSS refactor.
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


def _name(prefix: str) -> str:
    return f"pa-{prefix}-{uuid.uuid4().hex[:8]}"


def _delete_volume(live_server: str, name: str) -> None:
    from tests.e2e_helpers import auth_headers

    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/volumes/{name}",
            headers=auth_headers(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


def _delete_network(live_server: str, name: str) -> None:
    from tests.e2e_helpers import auth_headers

    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/networks/{name}",
            headers=auth_headers(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


@journey(
    persona=("developer",),
    category="volumes_networks",
    severity="high",
    covers=("hb-volume-create-skinny-form",),
)
def test_journey_volume_create_accepts_full_params(audited_page, live_server, audit_observer, persona):
    """Create a volume via API with driver + labels + driver_opts. The
    API contract is what the UI modal exercises — if this shape goes
    red, the modal is broken by definition."""
    from tests.e2e_helpers import auth_headers

    name = _name("vol")
    try:
        with step("step_1_create_full_params"):
            # Route takes labels/driver_opts as key=value,key=value strings
            # (parsed by _parse_kv_list in skiff/routers/volumes.py).
            r = requests.post(
                f"{live_server.rstrip('/')}/api/volumes/create",
                params={
                    "name": name,
                    "driver": "local",
                    "labels": "skiff-audit-run=1,pa-class=vol-create",
                    "driver_opts": "type=tmpfs,device=tmpfs,o=size=1m",
                },
                headers=auth_headers(),
                timeout=30,
            )
            # Full-suite runs can saturate the WRITE rate-limit window
            # across the 100-journey sweep. The live server is a separate
            # process so the in-process conftest reset doesn't reach it.
            # Treat a 403 here as a harness flake (not a finding).
            assert r.status_code in (200, 201), f"volume create failed: {r.status_code} {r.text}"
        with step("step_2_inspect_reflects_params"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/volumes/{name}/inspect",
                headers=auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, f"inspect failed: {r.status_code}"
            body = r.json()
            # Response uses lowercase 'labels' (not Docker's PascalCase).
            labels = body.get("labels") or body.get("Labels") or {}
            assert labels.get("skiff-audit-run") == "1", f"label not persisted: {labels!r}"
    finally:
        _delete_volume(live_server, name)


@journey(
    persona=("sre_ops",),
    category="volumes_networks",
    severity="high",
    covers=("hb-network-create-skinny-form",),
)
def test_journey_network_create_with_subnet_and_labels(audited_page, live_server, audit_observer, persona):
    """Create a network with subnet + gateway + labels via API. Round-
    trips through the POST /api/networks body shape expanded in 38f9ce4."""
    from tests.e2e_helpers import auth_headers

    name = _name("net")
    # Randomise subnet octet to avoid collisions with prior runs.
    # Not cryptographic — just a non-colliding test fixture.
    import random

    octet = random.randint(100, 250)  # noqa: S311 — test fixture, not crypto
    subnet = f"172.28.{octet}.0/24"
    gateway = f"172.28.{octet}.1"
    try:
        with step("step_1_create_network"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks/create",
                params={
                    "name": name,
                    "driver": "bridge",
                    "subnet": subnet,
                    "gateway": gateway,
                    "labels": "skiff-audit-run=1",
                    "attachable": "true",
                },
                headers=auth_headers(),
                timeout=30,
            )
            if r.status_code == 403 and "overlaps" in r.text.lower():
                pytest.skip(f"subnet {subnet} overlaps (daemon state) — retry later")
            assert r.status_code in (200, 201), f"network create failed: {r.status_code} {r.text}"
        with step("step_2_inspect_shows_subnet"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/networks/{name}/inspect",
                headers=auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, f"network inspect failed: {r.status_code}"
            body = r.json()
            import json as _json

            body_str = _json.dumps(body)
            assert subnet in body_str, (
                f"subnet {subnet!r} not visible in inspect payload (keys: {list(body.keys())[:10]}): {body_str[:400]}"
            )
    finally:
        _delete_network(live_server, name)


@journey(
    persona=("security_reviewer",),
    category="volumes_networks",
    severity="high",
    tags=("zero-trust",),
)
def test_journey_network_bad_subnet_rejected(audited_page, live_server, audit_observer, persona):
    """Invalid CIDR must be rejected before reaching the daemon.
    Defense-in-depth: the validator catches this, not Docker."""
    from tests.e2e_helpers import auth_headers

    name = _name("badnet")
    try:
        with step("step_1_invalid_cidr"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks/create",
                params={
                    "name": name,
                    "driver": "bridge",
                    "subnet": "999.999.999.0/24",
                },
                headers=auth_headers(),
                timeout=30,
            )
            # Must be rejected (4xx). A 5xx would mean we're passing
            # garbage to the daemon.
            if r.status_code >= 500:
                audit_observer.emit(
                    step="step_1_invalid_cidr",
                    severity="P0",
                    category="security",
                    zero_trust=True,
                    title="Invalid CIDR reaches the daemon (5xx from backend)",
                    expected="400/422 rejection from the SKIFF validator",
                    observed=f"HTTP {r.status_code}: {r.text[:200]!r}",
                )
                pytest.fail("bad CIDR not caught at the validator boundary")
            assert 400 <= r.status_code < 500, f"expected 4xx for bad CIDR; got {r.status_code}"
    finally:
        _delete_network(live_server, name)


@journey(
    persona=("hobbyist", "ui_ux_auditor"),
    category="volumes_networks",
    severity="medium",
    covers=("hb-volumes-no-search", "hb-networks-no-search"),
)
def test_journey_volumes_and_networks_pages_have_search(audited_page, live_server, audit_observer, persona):
    """Both pages render a search/filter input (hb-volumes-no-search +
    hb-networks-no-search)."""
    from tests.e2e_helpers import login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    for section in ("volumes", "networks"):
        with step(f"step_2_check_search_on_{section}"):
            nav_to(page, section)
            present = (
                page.locator("input[type='search']").count() > 0
                or page.locator("input[placeholder*='search' i]").count() > 0
                or page.locator("input[placeholder*='filter' i]").count() > 0
            )
            if not present:
                hb_id = f"hb-{section}-no-search"
                audit_observer.emit(
                    step=f"step_2_check_search_on_{section}",
                    severity="high",
                    category="layout",
                    title=f"{section.capitalize()} page missing search affordance",
                    expected="An input[type=search] or placeholder*=filter",
                    observed="No search/filter input found",
                    covers_historical=hb_id,
                )
                pytest.fail(f"{section} page missing search affordance")


@journey(
    persona=("sre_ops",),
    category="volumes_networks",
    severity="medium",
)
def test_journey_volume_prune_returns_reclaimed(audited_page, live_server, audit_observer, persona):
    """Hitting /api/volumes/prune returns a dict with SpaceReclaimed
    (docker SDK shape). Even if 0 bytes, the key must be present."""
    from tests.e2e_helpers import auth_headers

    with step("step_1_prune_volumes"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/volumes/prune?undo=false",
            headers=auth_headers(),
            timeout=60,
        )
        assert r.status_code == 200, f"prune failed: {r.status_code}"
        body = r.json()
        # Either SpaceReclaimed or space_reclaimed depending on case.
        reclaimed_keys = {"SpaceReclaimed", "space_reclaimed", "space_reclaimed_mb", "reclaimed_bytes"}
        assert any(k in body for k in reclaimed_keys), f"prune response missing SpaceReclaimed: {body!r}"


@journey(
    persona=("developer",),
    category="volumes_networks",
    severity="medium",
)
def test_journey_network_connect_then_disconnect(audited_page, live_server, audit_observer, persona):
    """Create a container + network, connect, disconnect. Tests the
    /connect /disconnect endpoints the UI Detail view uses."""
    from tests.e2e_helpers import auth_headers

    net = _name("connet")
    cont = _name("concont")
    # Seed container
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": cont},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "command": "sleep 3600",
            "labels": {"skiff-audit-run": "1"},
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"container seed failed: {r.status_code}")
    # Seed network
    r = requests.post(
        f"{live_server.rstrip('/')}/api/networks/create",
        params={
            "name": net,
            "driver": "bridge",
            "labels": "skiff-audit-run=1",
        },
        headers=auth_headers(),
        timeout=30,
    )
    assert r.status_code in (200, 201), f"network seed failed: {r.status_code}"

    try:
        with step("step_1_connect"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks/{net}/connect",
                params={"container_id": cont},
                headers=auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, f"connect failed: {r.status_code} {r.text}"
        with step("step_2_disconnect"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks/{net}/disconnect",
                params={"container_id": cont},
                headers=auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, f"disconnect failed: {r.status_code} {r.text}"
    finally:
        # Best-effort teardown.
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{cont}?force=true",
                headers=auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass
        _delete_network(live_server, net)


# ── Plan-named J-05 scenarios ────────────────────────────────────────


@journey(
    persona=("sre_ops",),
    category="volumes_networks",
    severity="medium",
)
def test_journey_volume_backup_via_cp(audited_page, live_server, audit_observer, persona):
    """Plan J-05 item: backup via cp. Attach a volume to a container,
    put a marker file inside, then docker-cp the file back out via
    /api/containers/{id}/files?path=/vol/marker. Exercises the SRE
    rubric where volumes need a user-triggerable backup path."""
    from tests.e2e_helpers import auth_headers

    vol = _name("bvol")
    cont = _name("bcont")
    # Seed volume.
    r = requests.post(
        f"{live_server.rstrip('/')}/api/volumes/create",
        params={
            "name": vol,
            "driver": "local",
            "labels": "skiff-audit-run=1",
        },
        headers=auth_headers(),
        timeout=30,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"volume seed failed: {r.status_code}")
    # Seed container with the volume mounted + marker written at boot.
    # volumes field takes list of 'name:/path[:ro|rw]' strings.
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": cont},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "command": 'sh -c "echo marker > /vol/pa-marker && sleep 3600"',
            "volumes": [f"{vol}:/vol"],
            "labels": {"skiff-audit-run": "1"},
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        _delete_volume(live_server, vol)
        pytest.skip(f"container seed failed: {r.status_code}")
    try:
        import time

        time.sleep(1)
        with step("step_1_cp_out_marker"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{cont}/files",
                params={"path": "/vol/pa-marker"},
                headers=auth_headers(),
                timeout=30,
            )
            if r.status_code != 200:
                audit_observer.emit(
                    step="step_1_cp_out_marker",
                    severity="medium",
                    category="behaviour",
                    title=f"Volume backup via cp returned {r.status_code}",
                    expected="200 with tar stream containing marker",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{cont}?force=true",
                headers=auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass
        _delete_volume(live_server, vol)


@journey(
    persona=("sre_ops",),
    category="volumes_networks",
    severity="low",
)
def test_journey_volume_nfs_driver_surface(audited_page, live_server, audit_observer, persona):
    """Plan J-05 item: NFS driver path. Create a volume with
    driver=local + o=addr=…,nfsvers=4 style options. We don't actually
    mount an NFS share — test probes that the backend accepts the
    create payload (fails cleanly if the NFS server is unreachable,
    not with a 500 traceback)."""
    from tests.e2e_helpers import auth_headers

    name = _name("nfsvol")
    try:
        with step("step_1_create_nfs_style_volume"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/volumes/create",
                params={
                    "name": name,
                    "driver": "local",
                    "driver_opts": "type=nfs,o=addr=127.0.0.1 nfsvers=4,device=:/export/test",
                    "labels": "skiff-audit-run=1",
                },
                headers=auth_headers(),
                timeout=30,
            )
            # Acceptable: 2xx (accepted — mount happens lazily), OR
            # 4xx (validator rejected, e.g. NFS-only allowlist off).
            # 5xx is a broken-shape bug — the request body should be
            # syntactically valid regardless of daemon state.
            assert r.status_code < 500, f"NFS-style driver_opts raised 5xx: {r.status_code} {r.text[:200]!r}"
            assert r.status_code in (200, 201, 400, 422, 403), (
                f"unexpected NFS-create status {r.status_code}: {r.text[:200]!r}"
            )
            # If accepted, inspect must round-trip the driver_opts.
            if r.status_code in (200, 201):
                r2 = requests.get(
                    f"{live_server.rstrip('/')}/api/volumes/{name}/inspect",
                    headers=auth_headers(),
                    timeout=30,
                )
                assert r2.status_code == 200, f"NFS volume inspect failed: {r2.status_code}"
                import json as _json

                body_str = _json.dumps(r2.json())
                assert "nfs" in body_str.lower(), f"NFS driver_opts not persisted in inspect: {body_str[:300]}"
    finally:
        _delete_volume(live_server, name)


@journey(
    persona=("sre_ops", "developer"),
    category="volumes_networks",
    severity="medium",
)
def test_journey_prune_safety_only_hits_unused(audited_page, live_server, audit_observer, persona):
    """Plan J-05 item: prune safety. Create one attached volume and
    one dangling volume. Prune must remove the dangling one ONLY —
    never the attached one."""
    from tests.e2e_helpers import auth_headers

    attached = _name("attv")
    dangling = _name("danv")
    cont = _name("attc")

    def _mk_vol(vname):
        return requests.post(
            f"{live_server.rstrip('/')}/api/volumes/create",
            params={
                "name": vname,
                "driver": "local",
                "labels": "skiff-audit-run=1",
            },
            headers=auth_headers(),
            timeout=30,
        )

    if _mk_vol(attached).status_code not in (200, 201) or _mk_vol(dangling).status_code not in (200, 201):
        pytest.skip("volume seed failed")

    # Attach the first to a running container.
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": cont},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "command": "sleep 3600",
            "volumes": [f"{attached}:/vol"],
            "labels": {"skiff-audit-run": "1"},
        },
        timeout=60,
    )
    if r.status_code not in (200, 201):
        _delete_volume(live_server, attached)
        _delete_volume(live_server, dangling)
        pytest.skip(f"container seed failed: {r.status_code}")

    try:
        with step("step_1_prune"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/volumes/prune?undo=false",
                headers=auth_headers(),
                timeout=60,
            )
            assert r.status_code == 200, f"prune failed: {r.status_code}"
        with step("step_2_attached_still_exists"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/volumes/{attached}/inspect",
                headers=auth_headers(),
                timeout=30,
            )
            if r.status_code != 200:
                audit_observer.emit(
                    step="step_2_attached_still_exists",
                    severity="P0",
                    category="behaviour",
                    title="Attached volume destroyed by prune",
                    expected="Attached volume survives prune",
                    observed=f"inspect after prune: {r.status_code}",
                )
                pytest.fail("attached volume pruned — data loss")
        with step("step_3_dangling_may_be_gone"):
            # Dangling volume SHOULD be gone (prune reclaimed it);
            # emit a finding if it's still there, but don't hard-fail
            # — some runtimes don't consider it orphaned if the
            # allocation TTL hasn't elapsed.
            r = requests.get(
                f"{live_server.rstrip('/')}/api/volumes/{dangling}/inspect",
                headers=auth_headers(),
                timeout=30,
            )
            if r.status_code == 200:
                audit_observer.emit(
                    step="step_3_dangling_may_be_gone",
                    severity="low",
                    category="behaviour",
                    title="Dangling volume not reclaimed by prune",
                    expected="Unused volume removed by prune",
                    observed="inspect still returned 200 — not pruned",
                )
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{cont}?force=true",
                headers=auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass
        _delete_volume(live_server, attached)
        _delete_volume(live_server, dangling)
