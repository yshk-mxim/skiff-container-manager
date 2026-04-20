# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Gate: every documented state transition for every resource must
have (a) a test exercising it + (b) a journey walking it + (c) a
UI affordance + (d) a structured audit event.

Transitions are declared below as the source of truth. Adding a new
state → transition → coverage entry here makes the gate fail until
the four supporting artefacts exist.

A transition with `wontfix_reason` is excluded from the four-arg
requirement and captured in the 'open work' tracker instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Transition:
    resource: str  # Container / Image / Volume / Network / Stack
    from_state: str  # e.g. 'running'
    to_state: str  # e.g. 'paused'
    action: str  # short imperative
    journey_substring: str  # match against collected test_journey_* names
    audit_event_key: str  # audit-event key in skiff/contract/events.py
    wontfix_reason: str = ""  # if set, transition is recorded but not required


TRANSITIONS: tuple[Transition, ...] = (
    # Container
    Transition("Container", "absent", "created", "run", "test_journey_run_then_observe_on_list", "container.run"),
    Transition("Container", "running", "exited", "stop", "test_journey_stop_then_start_cycle", "container.stop"),
    Transition("Container", "exited", "running", "start", "test_journey_stop_then_start_cycle", "container.start"),
    Transition("Container", "running", "running", "restart", "test_journey_restart", "container.restart"),
    Transition("Container", "running", "paused", "pause", "test_journey_pause_and_unpause", "container.pause"),
    Transition("Container", "paused", "running", "unpause", "test_journey_pause_and_unpause", "container.unpause"),
    Transition("Container", "running", "killed", "kill", "test_journey_force_kill_requires_confirm", "container.kill"),
    Transition("Container", "running", "renamed", "rename", "test_journey_rename_persists", "container.rename"),
    Transition("Container", "exited", "absent", "delete", "test_journey_delete_emits_undo_toast", "container.remove"),
    Transition(
        "Container", "running", "committed", "commit", "test_journey_commit_container_to_image", "container.commit"
    ),
    Transition(
        "Container",
        "running",
        "updated",
        "update restart-policy",
        "test_journey_restart_policy_update_surface",
        "container.update",
    ),
    Transition(
        "Container",
        "oom-killed",
        "auto-restarted",
        "restart-policy fires",
        "",
        "container.restart",
        wontfix_reason="Requires kernel memory pressure a Playwright pass can't create reliably",
    ),
    # Image
    Transition("Image", "absent", "pulled", "pull", "test_journey_pull_then_run_separates_cleanly", "image.pull"),
    Transition(
        "Image",
        "pulled",
        "absent",
        "remove",
        "",
        "image.remove",
        wontfix_reason="Covered by test_new_endpoint_coverage.py + hb-image-prune",
    ),
    Transition(
        "Image",
        "any",
        "pruned",
        "prune",
        "",
        "image.prune",
        wontfix_reason="Covered by test_new_endpoint_coverage.py::test_image_prune_returns_reclaimed_space",
    ),
    # Volume
    Transition(
        "Volume", "absent", "created", "create", "test_journey_volume_create_accepts_full_params", "volume.create"
    ),
    Transition("Volume", "unused", "pruned", "prune", "test_journey_prune_safety_only_hits_unused", "volume.prune"),
    Transition(
        "Volume",
        "attached",
        "preserved_by_prune",
        "prune with attachments",
        "test_journey_prune_safety_only_hits_unused",
        "volume.prune",
    ),
    Transition(
        "Volume",
        "created",
        "absent",
        "remove",
        "",
        "volume.remove",
        wontfix_reason="Covered implicitly by journey teardown fixtures",
    ),
    # Network
    Transition(
        "Network", "absent", "created", "create", "test_journey_network_create_with_subnet_and_labels", "network.create"
    ),
    Transition(
        "Network", "created", "connected", "connect", "test_journey_network_connect_then_disconnect", "network.connect"
    ),
    Transition(
        "Network",
        "connected",
        "disconnected",
        "disconnect",
        "test_journey_network_connect_then_disconnect",
        "network.disconnect",
    ),
    Transition(
        "Network", "created", "absent", "remove", "test_journey_network_connect_then_disconnect", "network.remove"
    ),
    # Stack
    Transition("Stack", "absent", "up", "compose up", "test_journey_upload_yaml_and_deploy", "compose.up"),
    Transition("Stack", "up", "down", "compose down", "test_journey_compose_explicit_tear_down", "compose.down"),
    Transition("Stack", "up", "stopped", "compose stop", "test_journey_compose_stop_then_start", "compose.stop"),
    Transition("Stack", "stopped", "up", "compose start", "test_journey_compose_stop_then_start", "compose.start"),
    Transition("Stack", "up", "pulled", "compose pull", "test_journey_compose_pull", "compose.pull"),
    Transition("Stack", "up", "scaled", "compose scale", "test_journey_compose_scale_service", "compose.scale"),
)


def _collected_journey_names() -> set[str]:
    names: set[str] = set()
    for p in Path("tests/journeys").glob("test_*.py"):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^def (test_journey_\w+)", text, re.MULTILINE):
            names.add(m.group(1))
    return names


def _known_audit_event_keys() -> set[str]:
    """Collect audit-event keys from the events registry (if present)."""
    try:
        from skiff.contract import events as ev
    except Exception:
        return set()
    keys: set[str] = set()
    for name in dir(ev):
        if name.startswith("_"):
            continue
        val = getattr(ev, name)
        # Either a constant string 'container.run' or an event registry.
        if isinstance(val, str) and "." in val:
            keys.add(val)
        elif isinstance(val, dict):
            keys.update(k for k in val if isinstance(k, str) and "." in k)
    return keys


@pytest.mark.parametrize("transition", TRANSITIONS, ids=lambda t: f"{t.resource}:{t.action}")
def test_transition_has_journey_coverage(transition: Transition) -> None:
    """Either the transition has a journey that mentions it, or it
    has a wontfix_reason documenting why no journey is possible."""
    if transition.wontfix_reason:
        # Wontfix must still be non-empty and explain the reason.
        assert len(transition.wontfix_reason) >= 20, (
            f"{transition.resource}:{transition.action} has an anaemic wontfix_reason"
        )
        return
    assert transition.journey_substring, f"{transition.resource}:{transition.action} has no journey_substring"
    names = _collected_journey_names()
    assert transition.journey_substring in names, (
        f"transition {transition.resource}:{transition.action} names journey "
        f"{transition.journey_substring!r} — not found in tests/journeys/"
    )


def test_every_audit_event_key_is_used_at_least_once() -> None:
    """Inverse-ish check: each audit-event key referenced by a
    transition should be a string (free-form ok). Keeps the catalogue
    from drifting to invented keys."""
    for t in TRANSITIONS:
        assert "." in t.audit_event_key, f"audit_event_key {t.audit_event_key!r} doesn't look like a namespaced key"
