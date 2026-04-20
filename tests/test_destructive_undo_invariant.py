# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Invariant: every destructive endpoint must accept an undo path OR
appear on the explicit "no-undo-needed" allowlist with a documented
reason. The allowlist is the single source of truth — a new destructive
route without either an undo gate or an allowlist entry fails the test.

The allowlist (`_UNDO_EXEMPTIONS`) is deliberately small and each entry
must name its reason so an auditor can review the exemptions at a
glance. Reasons in scope:
  - "reversible-via-side-effect": stop is undone by start, pause by
    unpause, etc.
  - "auth-gated-self-lock": rotate-token / reset-config, where the
    user is deliberately locking themselves out and an undo would let
    an attacker in the window reverse it.
  - "tunnel-config": setup tunnel endpoints already have strict gates
    and aren't destructive in the data sense.

Every other destructive mutation must expose `undo: bool` in its
signature AND the default must be `True` (except single-resource
DELETE endpoints where historical UX kept `undo=False` as default
because the user already confirmed — BUT they must still support
opting into undo).
"""

from __future__ import annotations

import inspect

import pytest

from app import app

# Routes that are REMOVE/PRUNE/TEAR-DOWN but do NOT need an undo path.
# Each entry maps (method, path) → human reason.
_UNDO_EXEMPTIONS = {
    # Reversible via side-effect: stop ↔ start, pause ↔ unpause.
    ("POST", "/api/containers/{container_id}/stop"): "reversible-via-start",
    ("POST", "/api/containers/{container_id}/pause"): "reversible-via-unpause",
    ("POST", "/api/containers/{container_id}/unpause"): "non-destructive",
    ("POST", "/api/containers/{container_id}/kill"): "reversible-via-start",
    ("POST", "/api/containers/{container_id}/restart"): "non-destructive",
    ("POST", "/api/compose/{project_name}/stop"): "reversible-via-start",
    ("POST", "/api/compose/{project_name}/start"): "non-destructive",
    # Self-lock auth ops — an undo window would let an attacker in the
    # grace period reverse the very action meant to lock them out.
    ("POST", "/api/auth/rotate-token"): "auth-gated-self-lock",
    ("POST", "/api/auth/reset-config"): "auth-gated-self-lock",
    ("POST", "/api/profile/enter-reviewer"): "auth-gated-self-lock-one-way",
    # Tunnel control plane — has its own config state machine; not
    # container-level destructive.
    ("DELETE", "/api/setup/tunnel"): "tunnel-control-plane",
    # Volume browse session cleanup — removes a helper we created for
    # this session; undo would just respawn the helper on next browse.
    ("DELETE", "/api/volumes/{volume_name}/browse"): "helper-session-cleanup",
    ("POST", "/api/tunnel/reconnect"): "non-destructive",
    # Setup wizard: one-shot, only reachable before configuration.
    ("POST", "/api/setup"): "bootstrap-only",
    ("POST", "/api/setup/tunnel"): "bootstrap-only",
    # Undo execution itself.
    ("POST", "/api/undo/{token}"): "meta-undo-execution",
    # Non-destructive mutations that happen to be POST.
    ("POST", "/api/containers/run"): "creative-non-destructive",
    ("POST", "/api/containers/{container_id}/rename"): "reversible-via-rename",
    ("POST", "/api/containers/{container_id}/update"): "in-place-no-data-loss",
    ("POST", "/api/containers/{container_id}/commit"): "creative-non-destructive",
    ("POST", "/api/containers/{container_id}/upload"): "user-initiated-overwrite",
    ("POST", "/api/containers/{container_id}/files"): "user-initiated-overwrite",
    ("POST", "/api/images/pull"): "creative-non-destructive",
    ("POST", "/api/images/{image_id}/tag"): "creative-non-destructive",
    ("POST", "/api/images/push"): "read-only-upload",
    ("POST", "/api/networks/create"): "creative-non-destructive",
    ("POST", "/api/networks/{network_id}/connect"): "reversible-via-disconnect",
    ("POST", "/api/networks/{network_id}/disconnect"): "reversible-via-connect",
    ("POST", "/api/volumes/create"): "creative-non-destructive",
    ("POST", "/api/compose/up"): "creative-non-destructive",
    ("POST", "/api/compose/{project_name}/pull"): "creative-non-destructive",
    ("POST", "/api/compose/{project_name}/scale"): "undo-opt-in-per-click",
}


def _destructive_routes():
    """Yield (method, path, endpoint_fn) for every route on the app
    that is a DELETE or a verb-named POST (prune/down/kill/etc)."""
    destructive_verbs = ("prune", "down", "kill", "delete", "remove", "stop")
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        for method in route.methods:
            if method == "DELETE":
                yield method, path, route.endpoint
            elif method == "POST":
                tail = path.rsplit("/", 1)[-1]
                if tail in destructive_verbs or path.endswith(("-cache", "/prune")):
                    yield method, path, route.endpoint


@pytest.mark.unit
def test_every_destructive_route_has_undo_or_exemption():
    """The contract: every destructive API route either accepts
    `undo: bool` in its signature OR is on the exemption list with a
    named reason. Adding a new destructive route without wiring either
    fails loudly here."""
    offenders = []
    for method, path, fn in _destructive_routes():
        if (method, path) in _UNDO_EXEMPTIONS:
            continue
        sig = inspect.signature(fn)
        if "undo" not in sig.parameters:
            offenders.append(f"{method} {path} (fn={fn.__name__})")
    assert not offenders, (
        "Destructive endpoints missing an `undo` parameter AND an entry "
        "in _UNDO_EXEMPTIONS:\n  "
        + "\n  ".join(offenders)
        + "\nEither wire the undo queue (see skiff/routers/system.py::system_prune "
        "for the canonical pattern) or add the (method, path) tuple to "
        "tests/test_destructive_undo_invariant.py::_UNDO_EXEMPTIONS with a "
        "one-word reason."
    )


@pytest.mark.unit
def test_no_destructive_undo_exemption_is_a_typo():
    """Exemption table should only reference real routes — a typo would
    silently let a destructive path through the `continue` guard."""
    real_routes = {(m, getattr(r, "path", "")) for r in app.routes if hasattr(r, "methods") for m in r.methods}
    bogus = [(m, p) for (m, p) in _UNDO_EXEMPTIONS if (m, p) not in real_routes]
    assert not bogus, f"Exemption list references non-existent routes: {bogus}"
