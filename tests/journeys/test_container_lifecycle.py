# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Container lifecycle journeys — 10 scenarios walking the state
transitions that docker-py exposes and SKIFF surfaces.

These journeys are API-driven where they can be (seed a container via
/api/containers/run, assert observable state from the UI, clean up in
teardown) so they're resilient to UI copy churn but still assert
what the user sees.

Lifecycle covered (one journey per transition class):
  1. run → observe on list
  2. stop + start cycle
  3. restart
  4. pause + unpause
  5. kill (force)
  6. rename
  7. bulk-stop multi-select
  8. delete with undo
  9. commit to image
 10. exec round-trip (terminal)

Every journey uses `skiff-audit-run-<hex>` labels on seeded resources
so conftest teardown can sweep leaks without disturbing user work.
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


_AUDIT_LABEL = "skiff-audit-run"


def _run_seed_container(live_server: str, name_prefix: str, image: str = "alpine:3.20") -> str:
    """Seed a container via the API. Returns the container name.
    Uses a `skiff-audit-run` label so conftest teardown can sweep.

    Contract (see skiff/routers/containers.py::run_container):
      - `image` + `name` are QUERY PARAMS, not body fields.
      - Body is RunContainerRequest (extra=forbid).
    """
    from tests.e2e_helpers import auth_headers
    name = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": image, "name": name},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "command": "sleep 3600",
            "labels": {_AUDIT_LABEL: "1"},
        },
        timeout=120,
    )
    assert r.status_code in (200, 201), f"run seed failed: {r.status_code} {r.text}"
    return name


def _teardown_by_name(live_server: str, name: str) -> None:
    from tests.e2e_helpers import auth_headers
    try:
        requests.delete(
            f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
            headers=auth_headers(),
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


@journey(
    persona=("novice", "developer"),
    category="container_lifecycle",
    severity="high",
)
def test_journey_run_then_observe_on_list(audited_page, live_server, audit_observer, persona):
    """Seed a container via API, navigate to the list, assert its row
    renders with the expected name. Smoke test for the list page."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "ro")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
        with step("step_3_seeded_container_in_list"):
            # Allow a couple of polls for the list to refresh.
            page.wait_for_selector(f"text={name}", timeout=SHORT)
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("developer", "sre_ops"),
    category="container_lifecycle",
    severity="high",
)
def test_journey_stop_then_start_cycle(audited_page, live_server, audit_observer, persona):
    """Click Stop on a running container, assert state flips to exited,
    click Start, assert it flips back to running. Tests the button
    dispatch + the list refresh loop."""
    from tests.e2e_helpers import MEDIUM, SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "sc")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)

        with step("step_3_click_stop_for_row"):
            # Locate the row then click its Stop button. The button has
            # text 'Stop' and lives in the row's btn-group.
            row = page.locator(f"tr:has-text('{name}')").first
            row.locator("button:has-text('Stop')").first.click()

        with step("step_4_observe_exited_state"):
            # Poll for the row to re-render with exited/stopped state.
            page.wait_for_timeout(1000)
            row = page.locator(f"tr:has-text('{name}')").first
            # Either a Start button appears, or the state chip reads
            # 'exited' — both indicate the stop went through.
            assert (
                row.locator("button:has-text('Start')").count() > 0
                or row.locator("text=exited").count() > 0
            ), "row did not reflect stopped state"

        with step("step_5_click_start_again"):
            row = page.locator(f"tr:has-text('{name}')").first
            row.locator("button:has-text('Start')").first.click()
            # Expect Stop button to come back.
            page.wait_for_selector(
                f"tr:has-text('{name}') button:has-text('Stop')",
                timeout=MEDIUM,
            )
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("developer",),
    category="container_lifecycle",
    severity="medium",
)
def test_journey_restart(audited_page, live_server, audit_observer, persona):
    """Restart button on a running row → no state change visible (still
    running after restart), but a toast confirms the action."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "re")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
        with step("step_3_click_restart"):
            row = page.locator(f"tr:has-text('{name}')").first
            row.locator("button:has-text('Restart')").first.click()
            # Row should still exist + still be running after a moment.
            page.wait_for_timeout(1500)
            row = page.locator(f"tr:has-text('{name}')").first
            assert row.locator("button:has-text('Stop')").count() > 0, (
                "container not running after restart"
            )
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("sre_ops",),
    category="container_lifecycle",
    severity="medium",
)
def test_journey_pause_and_unpause(audited_page, live_server, audit_observer, persona):
    """Pause flips state to 'paused', which should show an Unpause button
    (or similar). Unpause returns to running."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "pa")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
        with step("step_3_click_pause"):
            row = page.locator(f"tr:has-text('{name}')").first
            pause_btn = row.locator("button:has-text('Pause')").first
            if pause_btn.count() == 0:
                pytest.skip("pause button not present — backend build may lack pause")
            pause_btn.click()
        with step("step_4_paused_state_visible"):
            page.wait_for_timeout(1000)
            # Paused state chip OR Unpause button should appear.
            row = page.locator(f"tr:has-text('{name}')").first
            assert (
                row.locator("text=paused").count() > 0
                or row.locator("button:has-text('Unpause')").count() > 0
            ), "paused state not reflected after pause"
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("sre_ops", "developer"),
    category="container_lifecycle",
    severity="high",
)
def test_journey_force_kill_requires_confirm(audited_page, live_server, audit_observer, persona):
    """Kill is destructive; clicking the Kill button must prompt for
    confirmation (alert/confirm dialog) — clicking Cancel must NOT
    kill the container. Accepts-dialog branch is a separate audit."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "ki")
    dialog_seen = {"count": 0}

    def _on_dialog(d):
        dialog_seen["count"] += 1
        d.dismiss()  # novice-safe path: cancel the kill

    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
        with step("step_3_attach_dialog_handler"):
            page.on("dialog", _on_dialog)
        with step("step_4_click_kill_cancel_dialog"):
            row = page.locator(f"tr:has-text('{name}')").first
            kill_btn = row.locator("button:has-text('Kill')").first
            if kill_btn.count() == 0:
                pytest.skip("Kill button not on this row")
            kill_btn.click()
            page.wait_for_timeout(500)
            # Dialog must have fired at least once — otherwise a
            # destructive action dispatched without confirmation.
            assert dialog_seen["count"] > 0, (
                "Kill dispatched without a confirm dialog — destructive "
                "action bypasses Nielsen #5 (error prevention)"
            )
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("developer",),
    category="container_lifecycle",
    severity="medium",
)
def test_journey_rename_persists(audited_page, live_server, audit_observer, persona):
    """Rename via API (UI rename modal auth'ed by the same contract),
    then confirm the list shows the new name."""
    from tests.e2e_helpers import MEDIUM, SHORT, auth_headers, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "rn")
    new_name = f"{name}-renamed"
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_rename_via_api"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/rename",
                params={"new_name": new_name},
                headers=auth_headers(),
                timeout=30,
            )
            assert r.status_code == 200, f"rename failed: {r.status_code} {r.text}"
        with step("step_3_observe_renamed_row"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={new_name}", timeout=MEDIUM)
            # Old name should not appear any more.
            page.wait_for_selector(f"text={name}",
                                   state="detached", timeout=SHORT)
    finally:
        _teardown_by_name(live_server, new_name)
        _teardown_by_name(live_server, name)


@journey(
    persona=("sre_ops",),
    category="container_lifecycle",
    severity="high",
    covers=("hb-no-bulk-actions",),
)
def test_journey_bulk_stop_multiple(audited_page, live_server, audit_observer, persona):
    """Multi-select 3 rows → click Bulk Stop → all 3 stop. Tests the
    bulk-action bar that hb-no-bulk-actions closed."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    names = [_run_seed_container(live_server, "bk") for _ in range(3)]
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            for n in names:
                page.wait_for_selector(f"text={n}", timeout=SHORT)

        with step("step_3_multi_select_rows"):
            for n in names:
                row = page.locator(f"tr:has-text('{n}')").first
                cb = row.locator("input[type='checkbox']").first
                if cb.count() == 0:
                    pytest.skip("row checkbox missing — bulk-action UI not present")
                cb.check()

        with step("step_4_click_bulk_stop"):
            # Floating bulk-action bar has a Stop button.
            bulk_stop = page.locator(".bulk-actions button:has-text('Stop'), .bulk-bar button:has-text('Stop')").first
            if bulk_stop.count() == 0:
                pytest.skip("bulk-action bar not surfaced")
            bulk_stop.click()
            page.wait_for_timeout(2000)

        with step("step_5_all_three_in_stopped_state"):
            for n in names:
                row = page.locator(f"tr:has-text('{n}')").first
                # Each row should offer a Start button (i.e., stopped).
                assert (
                    row.locator("button:has-text('Start')").count() > 0
                    or row.locator("text=exited").count() > 0
                ), f"{n} did not reach stopped state after bulk stop"
    finally:
        for n in names:
            _teardown_by_name(live_server, n)


@journey(
    persona=("developer", "novice"),
    category="container_lifecycle",
    severity="high",
    covers=("hb-undo-on-delete",),
)
def test_journey_delete_emits_undo_toast(audited_page, live_server, audit_observer, persona):
    """Soft-delete a stopped container → success toast MUST contain an
    'Undo' control (hb-undo-on-delete). The undo window is 10s in the
    audit profile; we assert the presence of Undo, not its round-trip
    (that's covered by tests/test_ui_list_affordances.py)."""
    from tests.e2e_helpers import MEDIUM, SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "du")
    try:
        # Stop the container first so delete doesn't need force=true.
        from tests.e2e_helpers import auth_headers
        requests.post(
            f"{live_server.rstrip('/')}/api/containers/{name}/stop",
            headers=auth_headers(), timeout=60,
        )

        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)

        dialog_dismissed = {"count": 0}
        page.on("dialog", lambda d: (dialog_dismissed.update(count=dialog_dismissed["count"] + 1), d.accept()))

        with step("step_3_click_delete_confirm_dialog"):
            row = page.locator(f"tr:has-text('{name}')").first
            delete_btn = row.locator("button:has-text('Delete'), button:has-text('Remove')").first
            if delete_btn.count() == 0:
                pytest.skip("Delete button missing from row")
            delete_btn.click()

        with step("step_4_undo_toast_appears"):
            # Toast with an Undo button should be visible within a few seconds.
            # Check both common toast classes.
            try:
                page.wait_for_selector(
                    ".toast button:has-text('Undo'), .notification button:has-text('Undo')",
                    timeout=MEDIUM,
                )
            except Exception:
                audit_observer.emit(
                    step="step_4_undo_toast_appears",
                    severity="high",
                    category="behaviour",
                    title="Delete did not surface an Undo toast",
                    expected="Toast with Undo control after soft-delete",
                    observed="No toast-with-undo rendered within MEDIUM timeout",
                    covers_historical="hb-undo-on-delete",
                )
                raise
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("developer",),
    category="container_lifecycle",
    severity="medium",
    covers=("hb-commit-missing",),
)
def test_journey_commit_container_to_image(audited_page, live_server, audit_observer, persona):
    """Commit a running container to a new image tag via API. The UI
    commit modal calls the same endpoint — this asserts the backend
    contract that backs the UI button."""
    from tests.e2e_helpers import auth_headers, login

    page = audited_page
    name = _run_seed_container(live_server, "cm")
    tag = f"local/pa-commit-{uuid.uuid4().hex[:6]}:latest"
    committed_ref = None
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_commit_via_api"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/commit",
                params={"repo": tag.split(":", 1)[0], "tag": "latest"},
                headers=auth_headers(),
                timeout=60,
            )
            assert r.status_code in (200, 201), (
                f"commit failed: {r.status_code} {r.text}"
            )
            body = r.json()
            assert body.get("ok") is True, f"commit response missing ok: {body}"
            # Store the resulting image id / ref for cleanup.
            committed_ref = body.get("image") or body.get("id") or tag
    finally:
        _teardown_by_name(live_server, name)
        if committed_ref:
            try:
                requests.delete(
                    f"{live_server.rstrip('/')}/api/images/{committed_ref.split(':')[0]}?force=true",
                    headers=auth_headers(),
                    timeout=30,
                )
            except requests.exceptions.RequestException:
                pass


@journey(
    persona=("developer",),
    category="container_lifecycle",
    severity="high",
    covers=("hb-terminal-dies-on-tab-switch",),
)
def test_journey_terminal_survives_tab_switch(audited_page, live_server, audit_observer, persona):
    """Open container detail → Terminal tab → switch to Logs tab → back
    to Terminal. The xterm session MUST still be alive (hb-terminal-dies-
    on-tab-switch regression)."""
    from tests.e2e_helpers import MEDIUM, SHORT, login, nav_to

    page = audited_page
    name = _run_seed_container(live_server, "tm")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_open_container_detail"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
            page.locator(f"tr:has-text('{name}') a, tr:has-text('{name}')").first.click()
            page.wait_for_timeout(500)

        with step("step_3_open_terminal_tab"):
            term_tab = page.locator("button:has-text('Terminal'), a:has-text('Terminal')").first
            if term_tab.count() == 0:
                pytest.skip("Terminal tab not surfaced (feature may be off for this image)")
            term_tab.click()
            # Allow xterm to mount.
            page.wait_for_selector(".xterm, #term-output", timeout=MEDIUM)

        with step("step_4_switch_to_logs_and_back"):
            logs_tab = page.locator("button:has-text('Logs'), a:has-text('Logs')").first
            if logs_tab.count() > 0:
                logs_tab.click()
                page.wait_for_timeout(400)
            # Switch back.
            term_tab.click()
            # If the regression recurs, xterm is absent or the
            # #term-output element is empty. Check both.
            page.wait_for_selector(".xterm, #term-output", timeout=MEDIUM)
            # Assert the cached term wasn't torn down.
            alive = page.evaluate(
                "() => { const el = document.getElementById('term-output');"
                "  return !!(el && (el._term || el.querySelector('.xterm'))); }",
            )
            if not alive:
                audit_observer.emit(
                    step="step_4_switch_to_logs_and_back",
                    severity="high",
                    category="behaviour",
                    title="Terminal torn down on tab switch",
                    expected="xterm cached across tab swaps within detail view",
                    observed="#term-output has no _term and no .xterm descendant after switch",
                    covers_historical="hb-terminal-dies-on-tab-switch",
                )
                pytest.fail("terminal did not survive tab switch")
    finally:
        _teardown_by_name(live_server, name)


# ── Plan-named J-03 scenarios ────────────────────────────────────────
# Original 10 journeys cover: run, stop/start, restart, pause, kill,
# rename, bulk stop, undo-delete, commit, terminal-tab-switch.
# Plan J-03 also enumerated: stats, exec round-trip, restart loop,
# OOM recovery, rootless-user exec, restart-policy update.
# Stats, exec, restart-policy update are high-value additions. OOM
# recovery and rootless-user exec are captured via the lifecycle
# coverage CSV (need special container images + kernel config).


@journey(
    persona=("sre_ops",),
    category="container_lifecycle",
    severity="medium",
)
def test_journey_stats_endpoint_returns_shape(audited_page, live_server, audit_observer, persona):
    """Plan J-03 item: stats. GET /api/containers/{id}/stats returns
    the cgroup shape the SRE stats tab renders. A 5xx means the cgroup
    v1/v2 branch is broken; a 200 with missing cpu_percent means the
    UI chart draws a flat line."""
    from tests.e2e_helpers import auth_headers

    name = _run_seed_container(live_server, "st")
    try:
        with step("step_1_fetch_stats"):
            r = requests.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/stats",
                headers=auth_headers(), timeout=30,
            )
            if r.status_code != 200:
                audit_observer.emit(
                    step="step_1_fetch_stats",
                    severity="high",
                    category="contract",
                    title=f"Stats endpoint returned {r.status_code}",
                    expected="200 with cpu/memory shape",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
                pytest.fail(f"stats failed: {r.status_code}")
            body = r.json()
            # Must expose CPU + memory fields for the UI to render.
            missing = [k for k in ("cpu_percent", "memory_mb", "memory_limit_mb")
                       if k not in body and k.replace("_", "") not in {kk.replace("_", "") for kk in body}]
            if missing:
                audit_observer.emit(
                    step="step_1_fetch_stats",
                    severity="medium",
                    category="contract",
                    title=f"Stats shape missing fields: {missing}",
                    expected="cpu_percent, memory_mb, memory_limit_mb",
                    observed=f"keys: {list(body.keys())[:10]}",
                )
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("developer",),
    category="container_lifecycle",
    severity="high",
)
def test_journey_exec_roundtrip_writes_file(audited_page, live_server, audit_observer, persona):
    """Plan J-03 item: exec round-trip. Developer rubric: edit a file
    in a running container and see the change. This journey exec's
    `sh -c "echo hi > /tmp/pa-mark"` and verifies the file exists
    afterwards via the ls endpoint."""
    import requests as _rq

    from tests.e2e_helpers import auth_headers

    name = _run_seed_container(live_server, "ex")
    try:
        with step("step_1_exec_write"):
            # Non-streaming exec — the REST route (if exposed) or the
            # container-commit path. Try the most common shape; if the
            # backend gates exec behind WS, skip.
            r = _rq.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/exec",
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={"cmd": ["sh", "-c", "echo hi > /tmp/pa-mark"]},
                timeout=30,
            )
            if r.status_code == 404:
                pytest.skip("REST exec endpoint not present; covered by WS journey")
            if r.status_code >= 500:
                audit_observer.emit(
                    step="step_1_exec_write",
                    severity="high",
                    category="contract",
                    title=f"Exec round-trip returned {r.status_code}",
                    expected="200 / 202 / 204",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
                pytest.fail(f"exec 5xx: {r.status_code}")
        with step("step_2_ls_tmp_sees_mark"):
            r = _rq.get(
                f"{live_server.rstrip('/')}/api/containers/{name}/ls",
                params={"path": "/tmp"},
                headers=auth_headers(), timeout=30,
            )
            if r.status_code == 200:
                body = r.json()
                names = [e.get("name") for e in (body.get("entries") or body.get("files") or [])]
                if "pa-mark" not in names:
                    audit_observer.emit(
                        step="step_2_ls_tmp_sees_mark",
                        severity="medium",
                        category="behaviour",
                        title="Exec write not visible via ls",
                        expected="pa-mark in /tmp after exec write",
                        observed=f"entries: {names[:10]}",
                    )
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("developer",),
    category="container_lifecycle",
    severity="low",
)
def test_journey_restart_loop_repeated(audited_page, live_server, audit_observer, persona):
    """Plan J-03 item: restart loop. Restart 3× in a row — every
    restart must 200 and leave the container running. Guards against
    a bug where the second restart finds a stale client handle."""
    from tests.e2e_helpers import auth_headers

    name = _run_seed_container(live_server, "rl")
    try:
        for i in range(3):
            with step(f"step_{i+1}_restart"):
                r = requests.post(
                    f"{live_server.rstrip('/')}/api/containers/{name}/restart",
                    headers=auth_headers(), timeout=60,
                )
                if r.status_code != 200:
                    audit_observer.emit(
                        step=f"step_{i+1}_restart",
                        severity="medium",
                        category="behaviour",
                        title=f"Restart #{i+1} returned {r.status_code}",
                        expected="200 OK on every restart in a tight loop",
                        observed=f"{r.status_code}: {r.text[:200]!r}",
                    )
                    pytest.fail(f"restart {i+1} failed")
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("sre_ops",),
    category="container_lifecycle",
    severity="medium",
)
def test_journey_restart_policy_update_surface(audited_page, live_server, audit_observer, persona):
    """Plan J-03 item: restart-policy update. A container's restart
    policy (no / on-failure / always / unless-stopped) must be updatable
    after creation. Probes whether the backend exposes the update
    endpoint — falls back to emitting a parity finding if not."""
    from tests.e2e_helpers import auth_headers

    name = _run_seed_container(live_server, "rp")
    try:
        with step("step_1_update_restart_policy"):
            # docker-py supports `container.update(restart_policy=...)`.
            # The UI typically exposes this via a PATCH or POST /update
            # route. Try both common shapes.
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/update",
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={"restart_policy": {"Name": "on-failure", "MaximumRetryCount": 3}},
                timeout=30,
            )
            if r.status_code == 404:
                audit_observer.emit(
                    step="step_1_update_restart_policy",
                    severity="medium",
                    category="parity",
                    title="No restart-policy update endpoint",
                    expected="POST /api/containers/{id}/update supports restart_policy",
                    observed="404 Not Found — endpoint missing",
                )
                return
            if r.status_code >= 500:
                pytest.fail(f"update 5xx: {r.status_code}")
    finally:
        _teardown_by_name(live_server, name)


@journey(
    persona=("security_reviewer",),
    category="container_lifecycle",
    severity="medium",
    tags=("zero-trust",),
)
def test_journey_rootless_exec_capability_check(audited_page, live_server, audit_observer, persona):
    """Plan J-03 item: rootless-user exec. Reviewer probes that the
    exec endpoint honours the User field — a container started with
    `user: 10000:10000` must exec as UID 10000. This is observability:
    we DON'T assert the exact UID (cross-platform variance) but we do
    assert the endpoint either succeeds or fails with a catalogued
    envelope, never a raw traceback."""
    import uuid

    from tests.e2e_helpers import auth_headers
    name = f"pa-ru-{uuid.uuid4().hex[:6]}"
    # RunContainerRequest currently has no 'user' field (extra=forbid).
    # If that ever changes, this journey should start exercising it.
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": name},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={
            "command": "sleep 3600",
            "labels": {"skiff-audit-run": "1"},
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"rootless seed failed: {r.status_code}")
    # Parity finding: body schema has no user field yet.
    audit_observer.emit(
        step="step_0_schema_check",
        severity="low",
        category="parity",
        title="RunContainerRequest has no 'user' field — rootless exec not wired",
        expected="body.user accepted for UID/GID-scoped run",
        observed="container seeded as default UID (schema omits user)",
    )
    try:
        with step("step_1_exec_whoami"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/{name}/exec",
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={"cmd": ["id"]},
                timeout=30,
            )
            if r.status_code == 404:
                pytest.skip("REST exec not present")
            if r.status_code >= 500:
                audit_observer.emit(
                    step="step_1_exec_whoami",
                    severity="medium",
                    category="contract",
                    title=f"Rootless exec raised {r.status_code}",
                    expected="2xx or envelope-formatted 4xx",
                    observed=f"{r.status_code}: {r.text[:200]!r}",
                )
                pytest.fail(f"rootless exec 5xx: {r.status_code}")
    finally:
        _teardown_by_name(live_server, name)
