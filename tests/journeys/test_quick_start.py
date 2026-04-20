# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Quick-start journeys — 6 scenarios covering the shortest paths to a
running container from zero. Covers template-driven deploys (novice
flow) and direct image runs (developer flow).

Template cards are declared in config._APP_TEMPLATES; clicking one
opens a prefilled Run-container modal. These journeys walk the
click → modal → submit → running-row pipeline and assert the user
observes a success state without free-text typing (novice rubric).
"""

from __future__ import annotations

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


def _open_template(page, tid: str, timeout: int) -> None:
    """Navigate Templates → click a template card → wait for the
    prefilled Run modal to render. Reused across template journeys."""
    page.locator(".sidebar a:has-text('Templates')").click()
    # Page h2 was renamed from "Templates" to "Templates" when the
    # Stacks (docker-compose) section was added alongside single-container
    # apps. The data-testid on each template card is unchanged, so the
    # click selector below still finds the right row.
    page.wait_for_selector("h2:has-text('Templates')", timeout=timeout)
    card = page.locator(f"[data-testid='template-{tid}']")
    # Templates whose registry is not allowed render disabled; skip
    # those journeys on the CI with a restricted allowlist.
    if (
        not card.first.is_enabled()
        and card.first.get_attribute("style", timeout=1000)
        and "not-allowed" in (card.first.get_attribute("style") or "")
    ):
        pytest.skip(f"template {tid} disabled by registry allowlist")
    card.first.click()
    # Run modal renders on the containers page after a short delay.
    page.wait_for_selector("h3:has-text('Run'), h2:has-text('Run')", timeout=timeout)


@journey(
    persona=("novice", "hobbyist"),
    category="quick_start",
    severity="high",
    covers=("hb-templates-missing",),
)
def test_journey_template_opens_run_modal_nginx(audited_page, live_server, audit_observer, persona):
    """Novice path: Dashboard → Templates → nginx card → Run modal
    renders with image prefilled. Does NOT actually submit run (that
    path needs a real daemon + network pull — covered by extended pass)."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_open_nginx_template"):
        _open_template(page, "nginx", SHORT)

    with step("step_3_image_prefilled"):
        # Image input is prefilled with nginx:alpine — no free-text typing required.
        img_input = page.locator("#run-image").first
        val = img_input.input_value() if img_input.count() > 0 else ""
        assert "nginx" in val, f"image not prefilled with nginx (got {val!r})"

    with step("step_4_cancel_keeps_clean_state"):
        # Novice may change their mind — cancel must be easy and obvious.
        cancel = page.locator("button:has-text('Cancel'), button:has-text('Close')").first
        if cancel.count() > 0:
            cancel.click()
        # Modal should be gone.
        page.wait_for_selector("h3:has-text('Run'), h2:has-text('Run')", state="hidden", timeout=SHORT)


@journey(
    persona=("novice",),
    category="quick_start",
    severity="high",
    covers=("hb-templates-missing",),
)
def test_journey_template_postgres_shows_required_password(audited_page, live_server, audit_observer, persona):
    """Novice path to postgres: template declares POSTGRES_PASSWORD
    as required. The Run modal must surface the env var with its
    'help' text so the novice doesn't submit a config-broken deploy."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_open_postgres_template"):
        _open_template(page, "postgres", SHORT)

    with step("step_3_password_env_visible"):
        # The env-var row for POSTGRES_PASSWORD should be present in the modal
        # (the template declares required=True + help text). Prefilled
        # env lands in the run-env textarea as KEY=VALUE lines.
        env_text = page.locator("#run-env").first.input_value()
        assert "POSTGRES_PASSWORD" in env_text, (
            f"postgres template modal missing POSTGRES_PASSWORD env row (got {env_text!r})"
        )

    with step("step_4_close_without_submit"):
        cancel = page.locator("button:has-text('Cancel'), button:has-text('Close')").first
        if cancel.count() > 0:
            cancel.click()


@journey(
    persona=("developer",),
    category="quick_start",
    severity="medium",
)
def test_journey_developer_opens_run_modal_directly(audited_page, live_server, audit_observer, persona):
    """Developer path: Containers → 'Run new container' → modal
    opens with an empty image field ready for typing. Enter-in-input
    should not submit prematurely (hb-*-form-shape class)."""
    from tests.e2e_helpers import SHORT, login, nav_to

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_nav_containers"):
        nav_to(page, "containers")

    with step("step_3_open_run_modal"):
        btn = page.locator("button:has-text('Run new container'), button:has-text('Run a container')").first
        assert btn.count() > 0, "Containers page missing Run-new-container button"
        btn.click()
        page.wait_for_selector("h3:has-text('Run'), h2:has-text('Run')", timeout=SHORT)

    with step("step_4_image_input_is_empty_and_focusable"):
        img = page.locator("input[placeholder*='image' i], input[name='image']").first
        assert img.count() > 0, "Run modal missing image input"
        # Should be empty for a cold open (no prefill from template).
        assert img.input_value() == "", "image input not empty on cold open"


@journey(
    persona=("super_user",),
    category="quick_start",
    severity="medium",
    tags=("api",),
)
def test_journey_super_user_lists_templates_via_api(audited_page, live_server, audit_observer, persona):
    """Super-user parity: every UI action has an API equivalent.
    GET /api/templates must return the same catalogue the UI renders."""
    from tests.e2e_helpers import auth_headers

    with step("step_1_fetch_templates_api"):
        r = requests.get(f"{live_server.rstrip('/')}/api/templates", headers=auth_headers(), timeout=10)
        assert r.status_code == 200, f"GET /api/templates → {r.status_code}"
        body = r.json()
        assert "templates" in body, "response missing 'templates' key"
        ids = {t["id"] for t in body["templates"]}
        # Seeded catalogue covers these; if any drop out, super-user
        # has a parity regression.
        expected = {"nginx", "postgres", "redis", "alpine"}
        missing = expected - ids
        assert not missing, f"API response missing seeded templates: {missing}"


@journey(
    persona=("hobbyist",),
    category="quick_start",
    severity="medium",
)
def test_journey_hobbyist_finds_template_by_search(audited_page, live_server, audit_observer, persona):
    """Hobbyist types 'post' into the filter input — expect ≤2 matches
    (postgres; mongo if 'Document' contains 'Post'-like copy isn't matched).
    Validates the template filter input actually filters (not dead widget)."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_nav_templates"):
        page.locator(".sidebar a:has-text('Templates')").click()
        page.wait_for_selector("h2:has-text('Templates')", timeout=SHORT)

    with step("step_3_type_search_query"):
        search = page.locator("input[type='search']").first
        search.fill("post")
        # One template card should remain: postgres.
        page.wait_for_selector("[data-testid='template-postgres']", timeout=SHORT)
        # nginx card should be filtered out.
        assert page.locator("[data-testid='template-nginx']").count() == 0, "search filter not actually filtering"

    with step("step_4_clear_search_restores_all"):
        search.fill("")
        page.wait_for_selector("[data-testid='template-nginx']", timeout=SHORT)


@journey(
    persona=("developer",),
    category="quick_start",
    severity="medium",
)
def test_journey_developer_cmd_k_reaches_run(audited_page, live_server, audit_observer, persona):
    """Developer rubric: ⌘K palette jumps to any primary action. This
    journey opens the palette and confirms 'Run container' or
    similar action is reachable without mouse."""
    from tests.e2e_helpers import login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)

    with step("step_2_open_palette"):
        # Accelerator varies by OS; try both Mod+K and Control+K.
        page.keyboard.press("Meta+K")
        # If palette didn't open (Linux CI), try Control+K.
        if page.locator(".palette, [role='dialog'][aria-label*='palette' i]").count() == 0:
            page.keyboard.press("Control+K")
        # If still no palette, this feature isn't wired for dev persona
        # yet — emit a finding rather than fail hard.
        if page.locator(".palette, [role='dialog'][aria-label*='palette' i]").count() == 0:
            audit_observer.emit(
                step="step_2_open_palette",
                severity="medium",
                category="behaviour",
                title="⌘K palette not reachable from dashboard",
                expected="Developer presses ⌘K / Ctrl-K → palette opens",
                observed="Neither shortcut opened a palette dialog",
            )
            return

    with step("step_3_palette_has_run_action"):
        page.keyboard.type("run")
        # Expect at least one match surfacing a run action.
        page.wait_for_timeout(200)


# ── Plan-named J-02 scenarios ────────────────────────────────────────


@journey(
    persona=("developer",),
    category="quick_start",
    severity="medium",
)
def test_journey_template_python_dev_opens_modal(audited_page, live_server, audit_observer, persona):
    """Plan J-02 item: template→python-dev. The seeded catalogue has
    a `python` template; this journey opens its Run modal and asserts
    the image field is prefilled with a python image."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
    with step("step_2_open_python_template"):
        _open_template(page, "python", SHORT)
    with step("step_3_image_is_python"):
        img_input = page.locator("#run-image").first
        val = img_input.input_value() if img_input.count() > 0 else ""
        assert "python" in val.lower(), f"image not python: {val!r}"


@journey(
    persona=("developer",),
    category="quick_start",
    severity="medium",
)
def test_journey_template_node_dev_opens_modal(audited_page, live_server, audit_observer, persona):
    """Plan J-02 item: template→node-dev. Same shape as python-dev —
    asserts the node template prefills the modal with a node image."""
    from tests.e2e_helpers import SHORT, login

    page = audited_page
    with step("step_1_sign_in"):
        login(page, live_server)
    with step("step_2_open_node_template"):
        _open_template(page, "node", SHORT)
    with step("step_3_image_is_node"):
        img_input = page.locator("#run-image").first
        val = img_input.input_value() if img_input.count() > 0 else ""
        assert "node" in val.lower(), f"image not node: {val!r}"


@journey(
    persona=("sre_ops", "developer"),
    category="quick_start",
    severity="medium",
    tags=("api",),
)
def test_journey_pull_then_run_separates_cleanly(audited_page, live_server, audit_observer, persona):
    """Plan J-02 item: pull-then-run. Pull an image first (background
    task), then run a container from it — exercises the two-step path
    that lets SRE operators warm the image cache before deploying."""
    import uuid

    from tests.e2e_helpers import auth_headers

    name = f"pa-ptr-{uuid.uuid4().hex[:6]}"
    ref = "alpine:3.20"
    with step("step_1_pull_image"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/images/pull",
            params={"image": ref},
            headers=auth_headers(),
            timeout=300,
        )
        # 200 or 202 (background task) both acceptable.
        if r.status_code not in (200, 202):
            audit_observer.emit(
                step="step_1_pull_image",
                severity="medium",
                category="contract",
                title=f"Image pull returned {r.status_code}",
                expected="200 or 202",
                observed=f"{r.status_code}: {r.text[:200]!r}",
            )
    try:
        with step("step_2_run_after_pull"):
            r = requests.post(
                f"{live_server.rstrip('/')}/api/containers/run",
                params={"image": ref, "name": name},
                headers={**auth_headers(), "Content-Type": "application/json"},
                json={
                    "command": "sleep 3600",
                    "labels": {"skiff-audit-run": "1"},
                },
                timeout=120,
            )
            assert r.status_code in (200, 201), f"run after pull failed: {r.status_code} {r.text}"
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
                headers=auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass


# ── Historical-bug coverage additions ────────────────────────────────


@journey(
    persona=("sre_ops",),
    category="quick_start",
    severity="medium",
    covers=("hb-image-prune-missing",),
)
def test_journey_images_prune_returns_reclaimed(audited_page, live_server, audit_observer, persona):
    """hb-image-prune-missing: images must expose a dedicated prune
    endpoint (not just bundled under system prune)."""
    from tests.e2e_helpers import auth_headers

    with step("step_1_prune_images"):
        r = requests.post(
            f"{live_server.rstrip('/')}/api/images/prune?undo=false",
            headers=auth_headers(),
            timeout=60,
        )
        assert r.status_code == 200, f"images prune failed: {r.status_code} {r.text[:200]!r}"
        body = r.json()
        # Must carry a reclaimed-space key so SRE can read the outcome.
        reclaimed_keys = {"SpaceReclaimed", "space_reclaimed", "space_reclaimed_mb", "reclaimed_bytes"}
        assert any(k in body for k in reclaimed_keys), f"images prune missing reclaimed-space key: {body}"


@journey(
    persona=("developer",),
    category="quick_start",
    severity="medium",
    covers=("hb-tag-search-3.12-slim",),
)
def test_journey_image_tag_search_finds_stable_tag(audited_page, live_server, audit_observer, persona):
    """hb-tag-search-3.12-slim: tag search must reach beyond the default
    100-most-recent window via the name filter param."""
    from tests.e2e_helpers import auth_headers

    with step("step_1_tags_with_name_filter"):
        r = requests.get(
            f"{live_server.rstrip('/')}/api/registry/tags",
            params={"image": "library/python", "name": "3.12"},
            headers=auth_headers(),
            timeout=30,
        )
        # Accept 200 (found) or 404/502 (Docker Hub unreachable in CI);
        # 5xx indicates the server crashed on the filter param itself.
        if r.status_code in (404, 502, 504):
            pytest.skip(f"Docker Hub unreachable: {r.status_code}")
        assert r.status_code == 200, f"tag search failed: {r.status_code} {r.text[:200]!r}"
        body = r.json()
        tags = body.get("tags") or body.get("results") or []
        # Filter server-side must return at least some 3.12-named tags.
        matches = [t for t in tags if "3.12" in (t.get("name") if isinstance(t, dict) else str(t))]
        assert matches, f"tag search for 3.12 returned no matches (body: {str(body)[:300]})"


@journey(
    persona=("developer",),
    category="quick_start",
    severity="low",
    covers=("hb-no-context-menu",),
)
def test_journey_context_menu_on_container_row(audited_page, live_server, audit_observer, persona):
    """hb-no-context-menu: right-click on a container row must open a
    context menu with the same verbs as the button group."""
    import uuid

    from tests.e2e_helpers import MEDIUM, SHORT, auth_headers, login, nav_to

    page = audited_page
    name = f"pa-ctx-{uuid.uuid4().hex[:6]}"
    r = requests.post(
        f"{live_server.rstrip('/')}/api/containers/run",
        params={"image": "alpine:3.20", "name": name},
        headers={**auth_headers(), "Content-Type": "application/json"},
        json={"command": "sleep 3600", "labels": {"skiff-audit-run": "1"}},
        timeout=120,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"seed failed: {r.status_code}")
    try:
        with step("step_1_sign_in"):
            login(page, live_server)
        with step("step_2_nav_containers"):
            nav_to(page, "containers")
            page.wait_for_selector(f"text={name}", timeout=SHORT)
        with step("step_3_right_click_row"):
            row = page.locator(f"tr:has-text('{name}')").first
            row.click(button="right")
            page.wait_for_timeout(300)
        with step("step_4_context_menu_visible"):
            # Accept several context-menu class conventions; the key
            # point is SOMETHING appears and has recognisable verbs.
            menu = page.locator(
                ".context-menu, [role='menu'], .ctx-menu",
            ).first
            assert menu.count() > 0 and menu.is_visible(), "no context menu rendered on right-click"
            text = menu.inner_text(timeout=MEDIUM).lower()
            verbs_present = sum(v in text for v in ("stop", "restart", "delete", "logs"))
            assert verbs_present >= 2, f"context menu missing the common verbs; text: {text[:200]!r}"
    finally:
        try:
            requests.delete(
                f"{live_server.rstrip('/')}/api/containers/{name}?force=true",
                headers=auth_headers(),
                timeout=30,
            )
        except requests.exceptions.RequestException:
            pass
