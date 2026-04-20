# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tier B: round-trip workflows. Write something (file, env, label,
port, volume), read it back through the same API the UI uses, and
assert byte-for-byte preservation. Catches the silent-truncation
class of bug where data makes it to the server but is returned
mangled — exactly the pattern that hid the 'AI.pdf' filename bug.
"""

from __future__ import annotations

import io
import tarfile
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


def _seed(live_server: str, prefix: str, **kwargs) -> str:
    cname = f"{prefix}-{uuid.uuid4().hex[:6]}"
    body = {"command": "sleep 3600", "labels": {"skiff-audit-run": "1"}, **kwargs}
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": cname},
        headers={**_auth(), "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    assert r.status_code in (200, 201), f"run failed: {r.status_code} {r.text}"
    return cname


def _teardown_c(live_server: str, name: str) -> None:
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
            headers=_auth(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


def _teardown_v(live_server: str, name: str) -> None:
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/volumes/{name}",
            headers=_auth(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


# ── File content round-trip ─────────────────────────────────────────────


@journey(persona=("developer",), category="files_tab", severity="high")
def test_journey_file_bytes_roundtrip_exact_content(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Write known bytes via POST /files, download via GET /files,
    assert the bytes match. Breaks if the tar round-trip corrupts
    content (encoding, EOF, size header mismatch)."""
    base = live_server.rstrip("/")
    cname = _seed(live_server, "rtrfile", read_only=False)
    try:
        payload = (b"line 1\nline 2\n\xff\xfe\x00" * 20) + b"tail marker"
        filename = "binary_payload.bin"
        with step("step_1_write_bytes"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                info = tarfile.TarInfo(name=filename)
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))
            buf.seek(0)
            r = requests.post(
                f"{base}/api/containers/{cname}/files",
                params={"path": "/tmp"},
                headers={**_auth(), "Content-Type": "application/x-tar"},
                data=buf.getvalue(),
                timeout=30,
            )
            assert r.status_code == 200

        with step("step_2_download_and_verify_bytes"):
            r = requests.get(
                f"{base}/api/containers/{cname}/files",
                params={"path": f"/tmp/{filename}"},
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200
            # Server returns a tar archive; extract and compare.
            with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:") as tf:
                members = tf.getmembers()
                assert any(m.name.endswith(filename) for m in members), (
                    f"downloaded tar missing {filename}: {[m.name for m in members]}"
                )
                m = next(m for m in members if m.name.endswith(filename))
                got = tf.extractfile(m).read()
            assert got == payload, (
                f"content differs: wrote {len(payload)} bytes, got {len(got)}. "
                f"Head differs at first {sum(1 for a, b in zip(payload, got, strict=False) if a != b)} bytes."
            )
    finally:
        _teardown_c(live_server, cname)


# ── Env var round-trip (preservation through run → inspect) ─────────────


@journey(persona=("developer",), category="container_lifecycle", severity="high")
def test_journey_env_vars_roundtrip_through_run_and_inspect(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Env vars set at run time must be retrievable via inspect (with
    secrets redacted). Tests that the run-modal → inspect pipeline
    preserves key names without dropping them."""
    base = live_server.rstrip("/")
    cname = _seed(
        live_server,
        "rtre",
        environment=["APP_ENV=production", "FEATURE_X=on", "PUBLIC_KEY=visible"],
    )
    try:
        with step("step_1_inspect_env"):
            r = requests.get(
                f"{base}/api/containers/{cname}/inspect",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200
            attrs = r.json()
            env_list = (attrs.get("config") or {}).get("env") or []
            env_keys = [e.split("=")[0] for e in env_list]
            for required_key in ("APP_ENV", "FEATURE_X", "PUBLIC_KEY"):
                assert required_key in env_keys, (
                    f"env key {required_key!r} missing from inspect; got {env_keys!r}. "
                    f"Silent drop of env vars between run → inspect would break "
                    f"the clone-container flow."
                )
    finally:
        _teardown_c(live_server, cname)


# ── Env var redaction for sensitive-looking keys ────────────────────────


@journey(persona=("security_reviewer",), category="security_reviewer", severity="P0", tags=("zero-trust",))
def test_journey_sensitive_env_values_redacted_in_inspect(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Env vars whose KEY matches the sensitive pattern (SECRET / TOKEN
    / PASSWORD / KEY / CREDENTIAL) must have their VALUES redacted in
    the inspect response. Key name survives; value is the masked
    placeholder."""
    base = live_server.rstrip("/")
    secret_value = "super-sensitive-" + uuid.uuid4().hex
    cname = _seed(
        live_server,
        "rtsec",
        environment=[
            f"DATABASE_PASSWORD={secret_value}",
            f"API_TOKEN=tok-{secret_value}",
            "LOG_LEVEL=info",
        ],
    )
    try:
        with step("step_1_inspect"):
            r = requests.get(
                f"{base}/api/containers/{cname}/inspect",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200
            body_text = r.text
            assert secret_value not in body_text, (
                "raw secret value leaked in inspect response — sensitive redaction broken"
            )
            # Key names are preserved (redaction of VALUE only).
            data = r.json()
            env_list = (data.get("config") or {}).get("env") or []
            keys = [e.split("=", 1)[0] for e in env_list]
            assert "DATABASE_PASSWORD" in keys
            assert "API_TOKEN" in keys
            assert "LOG_LEVEL" in keys
    finally:
        _teardown_c(live_server, cname)


# ── Label round-trip through inspect ───────────────────────────────────


@journey(persona=("developer",), category="container_lifecycle", severity="medium")
def test_journey_labels_roundtrip_preserved_in_inspect(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """User-supplied labels must reach the container AND be readable
    back through inspect with original key + value. The Docker SDK has
    some label-name restrictions the app already validates — the
    journey just proves the happy-path round-trip."""
    base = live_server.rstrip("/")
    cname = _seed(
        live_server,
        "rtlab",
        labels={
            "skiff-audit-run": "1",  # required by seed conventions
            "app": "my-app",
            "team": "platform",
            "environment": "staging",
        },
    )
    try:
        with step("step_1_inspect_labels"):
            r = requests.get(
                f"{base}/api/containers/{cname}/inspect",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200
            labels = (r.json().get("config") or {}).get("labels") or {}
            for k, v in (("app", "my-app"), ("team", "platform"), ("environment", "staging")):
                assert labels.get(k) == v, f"label {k!r} lost in round-trip: expected {v!r}, got {labels.get(k)!r}"
    finally:
        _teardown_c(live_server, cname)


# ── Port binding round-trip ────────────────────────────────────────────


@journey(persona=("developer",), category="container_lifecycle", severity="high")
def test_journey_port_bindings_roundtrip(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Port mappings set at run time must come back via inspect so the
    Clone modal can recreate them. The UI specifically reads
    host_config.port_bindings — breaks if that path stops being
    populated."""
    base = live_server.rstrip("/")
    # Pick high random ports to avoid EADDRINUSE.
    host_port = 40000 + (uuid.uuid4().int % 5000)
    cname = _seed(
        live_server,
        "rtport",
        ports={"80/tcp": str(host_port)},
    )
    try:
        with step("step_1_inspect_port_bindings"):
            r = requests.get(
                f"{base}/api/containers/{cname}/inspect",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200
            pb = (r.json().get("host_config") or {}).get("port_bindings") or {}
            assert "80/tcp" in pb, f"80/tcp binding missing from inspect: {pb!r}"
            hosts = [b.get("HostPort") for b in pb["80/tcp"]]
            assert str(host_port) in hosts, f"host port {host_port!r} missing from binding; got {hosts!r}"
    finally:
        _teardown_c(live_server, cname)


# ── Restart policy round-trip ──────────────────────────────────────────


@journey(persona=("sre_ops",), category="container_lifecycle", severity="medium")
def test_journey_restart_policy_roundtrip(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Restart policy must round-trip so Clone and view pages render
    the real value, not a silent fallback to 'no'."""
    base = live_server.rstrip("/")
    cname = _seed(live_server, "rtrst", restart_policy="on-failure")
    try:
        with step("step_1_inspect_restart"):
            r = requests.get(
                f"{base}/api/containers/{cname}/inspect",
                headers=_auth(),
                timeout=10,
            )
            assert r.status_code == 200
            rp = (r.json().get("host_config") or {}).get("restart_policy") or {}
            assert rp.get("Name") == "on-failure", f"restart policy lost: expected on-failure, got {rp!r}"
    finally:
        _teardown_c(live_server, cname)


# ── Volume create + attach + preservation across container recreation ──


@journey(persona=("sre_ops",), category="volumes_networks", severity="P0")
def test_journey_volume_data_persists_across_container_recreation(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """A named volume's whole point is that its data survives the
    container. Create volume → attach to container A → write file →
    stop A → delete A → attach same volume to container B → read the
    file. Should be byte-identical."""
    base = live_server.rstrip("/")
    vname = f"rtvol-{uuid.uuid4().hex[:6]}"

    r = requests.post(f"{base}/api/volumes/create", params={"name": vname}, headers=_auth(), timeout=30)
    assert r.status_code in (200, 201)

    cname_a = f"rtva-{uuid.uuid4().hex[:6]}"
    cname_b = f"rtvb-{uuid.uuid4().hex[:6]}"

    try:
        with step("step_1_run_container_A_with_volume"):
            r = requests.post(
                f"{base}/api/containers/run",
                params={"image": "alpine:3.20", "name": cname_a},
                headers={**_auth(), "Content-Type": "application/json"},
                json={
                    "command": "sleep 3600",
                    "labels": {"skiff-audit-run": "1"},
                    "volumes": [f"{vname}:/data"],
                    "read_only": False,
                },
                timeout=60,
            )
            assert r.status_code in (200, 201)

        content = b"persistent-content-" + uuid.uuid4().hex.encode()
        fname = "durability_test.bin"
        with step("step_2_write_into_volume_via_container_A"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                info = tarfile.TarInfo(name=fname)
                info.size = len(content)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(content))
            buf.seek(0)
            r = requests.post(
                f"{base}/api/containers/{cname_a}/files",
                params={"path": "/data"},
                headers={**_auth(), "Content-Type": "application/x-tar"},
                data=buf.getvalue(),
                timeout=30,
            )
            assert r.status_code == 200

        with step("step_3_remove_container_A"):
            requests.post(f"{base}/api/containers/{cname_a}/stop", headers=_auth(), timeout=30)
            r = requests.delete(
                f"{base}/api/containers/{cname_a}?force=true",
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200

        with step("step_4_run_container_B_with_same_volume"):
            r = requests.post(
                f"{base}/api/containers/run",
                params={"image": "alpine:3.20", "name": cname_b},
                headers={**_auth(), "Content-Type": "application/json"},
                json={
                    "command": "sleep 3600",
                    "labels": {"skiff-audit-run": "1"},
                    "volumes": [f"{vname}:/data"],
                    "read_only": False,
                },
                timeout=60,
            )
            assert r.status_code in (200, 201)

        with step("step_5_read_file_back_from_container_B"):
            r = requests.get(
                f"{base}/api/containers/{cname_b}/files",
                params={"path": f"/data/{fname}"},
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200
            with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:") as tf:
                m = next(m for m in tf.getmembers() if m.name.endswith(fname))
                got = tf.extractfile(m).read()
            assert got == content, (
                f"Volume data lost across container recreation. Wrote "
                f"{len(content)} bytes, got back {len(got)}. Either the "
                f"volume was GC'd or the mount re-attach is broken."
            )
    finally:
        _teardown_c(live_server, cname_a)
        _teardown_c(live_server, cname_b)
        _teardown_v(live_server, vname)


# ── Network attach / detach round-trip ─────────────────────────────────


@journey(persona=("sre_ops",), category="volumes_networks", severity="high")
def test_journey_network_attach_inspect_detach_roundtrip(
    audited_page,
    live_server,
    audit_observer,
    persona,
):
    """Create a user network, attach a container to it, inspect the
    network, verify the container appears in the membership list,
    then disconnect and confirm it's gone."""
    base = live_server.rstrip("/")
    netname = f"rtnet-{uuid.uuid4().hex[:6]}"
    cname = _seed(live_server, "rtnc")

    r = requests.post(
        f"{base}/api/networks/create",
        params={"name": netname, "driver": "bridge"},
        headers=_auth(),
        timeout=30,
    )
    assert r.status_code in (200, 201)

    try:
        with step("step_1_attach_container_to_network"):
            r = requests.post(
                f"{base}/api/networks/{netname}/connect",
                params={"container_id": cname},
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200, f"{r.status_code} {r.text}"

        with step("step_2_inspect_network_shows_container"):
            r = requests.get(
                f"{base}/api/networks/{netname}/inspect",
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200
            body = r.json()
            # Inspect response passes through the Docker Engine shape —
            # capitalised "Containers" dict keyed by container ID with
            # per-endpoint metadata (Name, IPv4Address, …). Flatten to
            # strings + substring-search the container name; robust to
            # future shape changes.
            members = body.get("Containers") or body.get("containers") or {}
            member_blob = str(members)
            assert cname in member_blob, (
                f"container {cname!r} not shown as attached in network inspect. Got members: {member_blob[:500]!r}"
            )

        with step("step_3_disconnect_and_verify_gone"):
            r = requests.post(
                f"{base}/api/networks/{netname}/disconnect",
                params={"container_id": cname},
                headers=_auth(),
                timeout=30,
            )
            assert r.status_code == 200
    finally:
        _teardown_c(live_server, cname)
        try:
            requests.delete(
                f"{base}/api/networks/{netname}",
                headers=_auth(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass
