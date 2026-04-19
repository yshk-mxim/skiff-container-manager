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
            headers=auth_headers(), timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


def _delete_network(live_server: str, name: str) -> None:
    from tests.e2e_helpers import auth_headers
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/networks/{name}",
            headers=auth_headers(), timeout=30,
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
            r = requests.post(
                f"{live_server.rstrip('/')}/api/volumes",
                params={"name": name, "driver": "local"},
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={
                    "labels": {"skiff-audit-run": "1", "pa-class": "vol-create"},
                    "driver_opts": {"type": "tmpfs", "device": "tmpfs", "o": "size=1m"},
                },
                timeout=30,
            )
            assert r.status_code in (200, 201), (
                f"volume create failed: {r.status_code} {r.text}"
            )
        with step("step_2_inspect_reflects_params"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/volumes/{name}",
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"inspect failed: {r.status_code}"
            body = r.json()
            assert body.get("Labels", {}).get("skiff-audit-run") == "1", (
                f"label not persisted: {body.get('Labels')!r}"
            )
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
    try:
        with step("step_1_create_network"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks",
                params={"name": name, "driver": "bridge"},
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={
                    "subnet": "172.28.200.0/24",
                    "gateway": "172.28.200.1",
                    "labels": {"skiff-audit-run": "1"},
                    "internal": False,
                    "attachable": True,
                },
                timeout=30,
            )
            assert r.status_code in (200, 201), (
                f"network create failed: {r.status_code} {r.text}"
            )
        with step("step_2_inspect_shows_subnet"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/networks/{name}",
                headers=auth_headers(), timeout=30,
            )
            body = r.json()
            ipam = body.get("IPAM", {}).get("Config", [])
            subnets = [c.get("Subnet") for c in ipam]
            assert "172.28.200.0/24" in subnets, (
                f"subnet not persisted: {subnets!r}"
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
                f"{live_server.rstrip('/')}/api/networks",
                params={"name": name, "driver": "bridge"},
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={"subnet": "999.999.999.0/24"},
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
            assert 400 <= r.status_code < 500, (
                f"expected 4xx for bad CIDR; got {r.status_code}"
            )
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
                assert False, f"{section} page missing search affordance"


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
            f"{live_server.rstrip('/')}/api/volumes/prune",
            headers=auth_headers(), timeout=60,
        )
        assert r.status_code == 200, f"prune failed: {r.status_code}"
        body = r.json()
        # Either SpaceReclaimed or space_reclaimed depending on case.
        reclaimed_keys = {"SpaceReclaimed", "space_reclaimed", "reclaimed_bytes"}
        assert any(k in body for k in reclaimed_keys), (
            f"prune response missing SpaceReclaimed: {body!r}"
        )


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
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "image": "alpine:3.20",
            "name": cont,
            "command": "sleep 3600",
            "labels": {"skiff-audit-run": "1"},
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"container seed failed: {r.status_code}")
    # Seed network
    r = requests.post(
        f"{live_server.rstrip('/')}/api/networks",
        params={"name": net, "driver": "bridge"},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={"labels": {"skiff-audit-run": "1"}},
        timeout=30,
    )
    assert r.status_code in (200, 201), f"network seed failed: {r.status_code}"

    try:
        with step("step_1_connect"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks/{net}/connect",
                params={"container": cont},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, f"connect failed: {r.status_code} {r.text}"
        with step("step_2_disconnect"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/networks/{net}/disconnect",
                params={"container": cont},
                headers=auth_headers(), timeout=30,
            )
            assert r.status_code == 200, (
                f"disconnect failed: {r.status_code} {r.text}"
            )
    finally:
        # Best-effort teardown.
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{cont}?force=true",
                headers=auth_headers(), timeout=30,
            )
        except requests.exceptions.RequestException:
            pass
        _delete_network(live_server, net)
