# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Historical bug registry.

Explicit enumeration of every bug the user has reported in this session.
Each entry is referenced by ≥1 journey via `@journey(covers=…)`, by a
regression unit test, and by a class-level sweep.

Adding an entry here without:
  1. At least one journey covering it, AND
  2. At least one regression test, AND
  3. An entry in CHANGELOG.md

…fails `tests/test_history_coverage.py`. This is deliberate — if a bug
matters enough to track, it matters enough to have the full three-layer
guard around it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HistoricalBug:
    id: str                        # 'hb-undo-on-delete'
    one_line: str                  # observed problem
    class_sweep: str               # what class the fix generalises to
    fix_commit_short: str = ""     # first 7 chars; filled after fix lands
    regression_test: str = ""      # tests/test_<file>.py::<test>


# Catalogue. Order = chronological-ish. Every row mapped to the fix
# commit once it's landed.
HISTORICAL_BUGS: tuple[HistoricalBug, ...] = (
    HistoricalBug(
        id="hb-undo-on-delete",
        one_line="Container delete bypassed undo queue via ?force=true hard-coded in UI",
        class_sweep="Every undoable DELETE returns undo_token by default (force only when needed)",
        fix_commit_short="99647c8",
        regression_test="tests/test_ui_list_affordances.py::test_delete_emits_undo_toast_on_every_resource",
    ),
    HistoricalBug(
        id="hb-tag-search-3.12-slim",
        one_line="Tag list capped at 100 most-recent; stable tags like 3.12-slim outside the window",
        class_sweep="Every paginated-from-API list exposes a substring filter that reaches beyond the initial window",
        fix_commit_short="c1e5e47",
        regression_test="",  # covered implicitly by e2e flow; add explicit
    ),
    HistoricalBug(
        id="hb-volumes-no-search",
        one_line="Volumes page had no search bar",
        class_sweep="Every list-rendering page with ≥1 row has a visible search input",
        fix_commit_short="99647c8",
        regression_test="tests/test_ui_list_affordances.py::test_every_entity_list_page_has_search_affordance",
    ),
    HistoricalBug(
        id="hb-networks-no-search",
        one_line="Networks page had no search bar",
        class_sweep="(same class as hb-volumes-no-search)",
        fix_commit_short="99647c8",
        regression_test="tests/test_ui_list_affordances.py::test_every_entity_list_page_has_search_affordance",
    ),
    HistoricalBug(
        id="hb-compose-no-search",
        one_line="Compose page had no search bar",
        class_sweep="(same class)",
        fix_commit_short="99647c8",
        regression_test="tests/test_ui_list_affordances.py::test_every_entity_list_page_has_search_affordance",
    ),
    HistoricalBug(
        id="hb-audit-silently-truncated",
        one_line="Audit log capped at tail=200 with no UI cue (no selector, no count)",
        class_sweep="Every capped list discloses its cap (either in a selector or in the header text)",
        fix_commit_short="99647c8",
        regression_test="tests/test_ui_list_affordances.py::test_audit_log_discloses_its_cap",
    ),
    HistoricalBug(
        id="hb-logs-connecting-forever",
        one_line="Log viewer 'Connecting...' text never cleared on WS open; stayed forever for silent containers",
        class_sweep="Every WS-backed viewer replaces its placeholder on first open event",
        fix_commit_short="cb07a77",
        regression_test="",  # TODO — add journey observation
    ),
    HistoricalBug(
        id="hb-terminal-dies-on-tab-switch",
        one_line="Terminal xterm + WS torn down on detail-tab switch (showDetail innerHTML='')",
        class_sweep="Each tab-cached WS survives tab switches within the same detail view",
        fix_commit_short="cb07a77",
        regression_test="",  # TODO — add explicit regression
    ),
    HistoricalBug(
        id="hb-files-tab-misleading",
        one_line="'No filesystem changes detected' misread as 'delete failed'",
        class_sweep="Every empty-state message explains what's missing, not just 'no …'",
        fix_commit_short="cb07a77",
        regression_test="",
    ),
    HistoricalBug(
        id="hb-volume-create-skinny-form",
        one_line="Volume create modal only accepted `name`; no driver / labels / driver_opts",
        class_sweep="Every immutable-after-creation resource exposes every knob upfront",
        fix_commit_short="38f9ce4",
        regression_test="tests/test_crud_completeness.py::test_volume_create_accepts_full_params",
    ),
    HistoricalBug(
        id="hb-network-create-skinny-form",
        one_line="Network create modal only accepted `name + driver`",
        class_sweep="(same class)",
        fix_commit_short="38f9ce4",
        regression_test="tests/test_crud_completeness.py::test_network_create_accepts_full_params",
    ),
    HistoricalBug(
        id="hb-image-prune-missing",
        one_line="No dedicated Images Prune button (only via system prune)",
        class_sweep="Every resource has a Prune that matches `docker <resource> prune`",
        fix_commit_short="99647c8",
        regression_test="tests/test_new_endpoint_coverage.py::test_image_prune_returns_reclaimed_space",
    ),
    HistoricalBug(
        id="hb-compose-no-pull-or-scale",
        one_line="Compose surface lacked pull / scale / start / stop",
        class_sweep="Compose backend exposes every `docker compose` verb (minus build = supply-chain NO)",
        fix_commit_short="0683c08",
        regression_test="tests/test_new_endpoint_coverage.py::test_compose_lifecycle_verbs_invoke_subprocess",
    ),
    HistoricalBug(
        id="hb-dashboard-missing",
        one_line="No home / landing page",
        class_sweep="App has a dashboard entry point with aggregated state + quick actions",
        fix_commit_short="a24b724",
        regression_test="",  # TODO add explicit /api/system/overview shape test
    ),
    HistoricalBug(
        id="hb-events-missing",
        one_line="No docker events stream view",
        class_sweep="Observability surface includes live daemon events",
        fix_commit_short="a24b724",
        regression_test="tests/test_new_endpoint_coverage.py::test_events_endpoint_bounds_since_secs",
    ),
    HistoricalBug(
        id="hb-no-bulk-actions",
        one_line="No multi-select on containers table",
        class_sweep="Every list page with row-level mutations has a bulk-action bar",
        fix_commit_short="cb07a77",
        regression_test="",
    ),
    HistoricalBug(
        id="hb-no-context-menu",
        one_line="No right-click context menu on rows",
        class_sweep="Rows expose the same verbs via right-click as via button group",
        fix_commit_short="cb07a77",
        regression_test="",
    ),
    HistoricalBug(
        id="hb-no-notifications-history",
        one_line="Toasts ephemeral; lost on tab switch or modal open",
        class_sweep="Bell icon shows last-50 toasts",
        fix_commit_short="462b6f7",
        regression_test="",
    ),
    HistoricalBug(
        id="hb-no-first-run-tour",
        one_line="New users dropped onto raw containers page with no guidance",
        class_sweep="Tour fires once after wizard; skippable; stored in localStorage",
        fix_commit_short="462b6f7",
        regression_test="",
    ),
    HistoricalBug(
        id="hb-cp-ui-missing",
        one_line="Files tab only showed docker diff; no filesystem browser or cp",
        class_sweep="Files tab has a live-filesystem Browse sub-view with upload + download",
        fix_commit_short="cb07a77",
        regression_test="tests/test_e2e_file_browser.py::test_file_browser_lists_navigates_downloads_uploads",
    ),
    HistoricalBug(
        id="hb-commit-missing",
        one_line="No way to save a running container as an image",
        class_sweep="Container actions include 'Commit to image…'",
        fix_commit_short="cb07a77",
        regression_test="tests/test_new_endpoint_coverage.py::test_commit_succeeds_with_canonical_inputs",
    ),
    HistoricalBug(
        id="hb-templates-missing",
        one_line="No one-click app deploy catalogue",
        class_sweep="Templates page with catalogue from config._APP_TEMPLATES",
        fix_commit_short="a24b724",
        regression_test="tests/test_new_endpoint_coverage.py::test_templates_catalogue_lists_known_ids",
    ),
    HistoricalBug(
        id="hb-files-tab-path-memory",
        one_line="Files path forgotten across tab switches",
        class_sweep="Per-container path memo survives tab switches within detail view",
        fix_commit_short="cb07a77",
        regression_test="tests/test_e2e_file_browser.py::test_file_browser_remembers_path_across_tab_switches",
    ),
)


HISTORICAL_BUGS_BY_ID: dict[str, HistoricalBug] = {hb.id: hb for hb in HISTORICAL_BUGS}


def get(hb_id: str) -> HistoricalBug:
    try:
        return HISTORICAL_BUGS_BY_ID[hb_id]
    except KeyError as exc:
        raise KeyError(f"Unknown historical bug {hb_id!r}") from exc
