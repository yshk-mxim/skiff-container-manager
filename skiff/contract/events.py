# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Audit event catalogue.

Every `log.info("<event.name>", ...)` used for audit purposes should be
declared here. The catalogue serves three roles:

1. A SIEM integrator has one file to parse for field shapes.
2. A future `emit_audit(name, **fields)` helper can validate names
   against this catalogue and reject typos at emit time.
3. A CI test walks `skiff/**/*.py`, extracts `log.*("<name>")`
   literals, and asserts every audit-tagged event is declared.

This is a *catalogue*, not a schema validator — the required-fields set
is a hint, not enforced by default. When we ship `emit_audit()` later,
it'll enforce required fields via this table.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _EventSpec:
    """Declaration of one audit event.

    severity: "info" | "warning" | "error". Lines the current call site's
              intent, not the HTTP status (a 400 can still be an INFO-level
              audit event because it's a routine validation refusal).
    required: structlog keys the caller MUST pass.
    optional: structlog keys the caller MAY pass. Extras not in either set
              are accepted but flagged by the CI test.
    description: one-line doc for SIEM rule authors.
    """

    severity: str = "info"
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    description: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue — every declared audit event the server emits.
#
# Keys follow <domain>.<past_tense_verb>. Add a new event:
#   1. Pick a stable dotted name with a past-tense verb.
#   2. List required fields (callers must pass).
#   3. List optional fields (callers may pass).
#   4. Write a one-line description aimed at someone writing detection rules.
# ─────────────────────────────────────────────────────────────────────────────
_EVENTS: dict[str, _EventSpec] = {
    # ── Application lifecycle ─────────────────────────────────────────────
    "app.dependency_versions": _EventSpec(
        severity="info",
        required=(),
        optional=("skiff", "fastapi", "docker", "structlog", "slowapi", "pydantic"),
        description="Installed versions of direct dependencies at startup (supply-chain forensics).",
    ),
    "app.started": _EventSpec(
        optional=("version", "docker_host", "bind", "profile"),
        description="FastAPI app finished startup.",
    ),
    "audit.undeclared_event": _EventSpec(
        severity="warning",
        required=("undeclared",),
        description="secure_route saw an audit name not in the catalogue; drift alert.",
    ),
    "audit.field_extraction_failed": _EventSpec(
        severity="warning",
        required=("name", "error"),
        description="secure_route's audit_fields callable raised; fired with empty fields.",
    ),
    "app.shutdown": _EventSpec(
        description="Process shutting down cleanly.",
    ),
    "security.no_api_token": _EventSpec(
        severity="warning",
        optional=("msg",),
        description="Server started without API_TOKEN — no auth enforced.",
    ),
    "security.empty_api_token_env": _EventSpec(
        severity="warning",
        optional=("msg",),
        description="API_TOKEN env var is present but empty — treated as unset.",
    ),
    "security.no_registry_allowlist": _EventSpec(
        severity="warning",
        optional=("msg",),
        description="ALLOWED_REGISTRIES is empty — every registry is implicitly allowed.",
    ),
    "security.docker_host_unencrypted": _EventSpec(
        severity="warning",
        required=("host",),
        description="DOCKER_HOST points at an HTTP URL off-localhost — traffic is unencrypted.",
    ),
    # ── Auth ──────────────────────────────────────────────────────────────
    "auth.token_rotated": _EventSpec(
        required=("old_suffix", "new_suffix"),
        description="Operator rotated the API token. Old suffix retained for audit correlation.",
    ),
    "auth.config_reset": _EventSpec(
        required=("old_suffix",),
        description="Operator reset SKIFF's runtime config (wipes tunnel + token).",
    ),
    "auth.reset_tunnel_cleanup_failed": _EventSpec(
        severity="warning",
        required=("error",),
        description="Tunnel teardown during config-reset raised; state may be partial.",
    ),
    # ── Setup (first-boot flow) ───────────────────────────────────────────
    "audit.setup_failed": _EventSpec(
        required=("remote", "reason"),
        description="A /api/setup attempt was rejected (bad token, locked, after-window).",
    ),
    "audit.setup_lockout": _EventSpec(
        severity="warning",
        required=("remote", "remaining"),
        description="Setup endpoint tripped the per-IP lockout threshold.",
    ),
    "setup.configured": _EventSpec(
        required=("docker_host", "registries"),
        description="First-boot setup completed successfully.",
    ),
    # ── Container lifecycle ───────────────────────────────────────────────
    "container.created": _EventSpec(
        required=("id", "name", "image"),
        optional=("memory", "cpus", "ports", "readonly_rootfs", "inherit_from"),
        description=(
            "New container created via /api/containers/run. "
            "`inherit_from` carries the source container ID when the "
            "caller asked to copy env vars from an existing container."
        ),
    ),
    # Audit-layer events emitted by AuditLogMiddleware from the
    # @secure_route.mutate(audit=...) decorator. Handlers also emit
    # named structured log lines (container.started etc.) — those are
    # separate from these, which fire on every /api/* request hit.
    "container.run": _EventSpec(
        optional=("image", "name", "user", "resource_type", "resource_id", "token_suffix"),
        description="Middleware audit of POST /api/containers/run.",
    ),
    "container.removed": _EventSpec(
        optional=("user", "resource_type", "resource_id", "token_suffix"),
        description="Middleware audit of DELETE /api/containers/{id}.",
    ),
    "container.started": _EventSpec(required=("id",), description="Container started."),
    "container.stopped": _EventSpec(required=("id",), description="Container stopped."),
    "container.restarted": _EventSpec(required=("id",), description="Container restarted."),
    "container.paused": _EventSpec(required=("id",), description="Container paused."),
    "container.unpaused": _EventSpec(required=("id",), description="Container unpaused."),
    "container.killed": _EventSpec(
        required=("id", "signal"),
        description="Container killed with explicit signal (not default SIGKILL).",
    ),
    "container.renamed": _EventSpec(
        required=("id", "new_name"),
        description="Container renamed.",
    ),
    "container.delete_queued": _EventSpec(
        required=("id", "force", "token_suffix"),
        description="Destructive delete queued under the undo window.",
    ),
    "container.deleted": _EventSpec(
        required=("id", "force"),
        description="Container deleted (either direct or after undo window).",
    ),
    "container.updated": _EventSpec(
        required=("id", "name", "changes"),
        description="Container resource limits updated in place.",
    ),
    "container.replace_noop": _EventSpec(
        severity="warning",
        required=("id", "replace_id"),
        description="Clone target differs from replace_id; replace skipped.",
    ),
    "container.replaced": _EventSpec(
        required=("new_id", "old_id"),
        description="Clone replaced an older container in-place.",
    ),
    "container.replace_cleanup_failed": _EventSpec(
        severity="warning",
        required=("replace_id", "error"),
        description="Failed to remove the old container during a replace — manual cleanup needed.",
    ),
    # ── Image ─────────────────────────────────────────────────────────────
    "image.pulled": _EventSpec(required=("image",), description="Image pulled from a registry."),
    "image.tagged": _EventSpec(
        required=("id", "repository", "tag"),
        description="Image tag operation succeeded.",
    ),
    "image.pushed": _EventSpec(required=("image",), description="Image pushed to a registry."),
    "image.delete_queued": _EventSpec(
        required=("id", "token_suffix"),
        description="Image deletion queued under the undo window.",
    ),
    "image.deleted": _EventSpec(required=("id",), description="Image deleted."),
    # ── Volume ────────────────────────────────────────────────────────────
    "volume.created": _EventSpec(required=("name",), description="Volume created."),
    "volume.delete_queued": _EventSpec(
        required=("name", "token_suffix"),
        description="Volume deletion queued under the undo window.",
    ),
    "volume.deleted": _EventSpec(required=("name",), description="Volume deleted."),
    "volumes.pruned": _EventSpec(required=("count",), description="Unused volumes pruned."),
    # ── Network ───────────────────────────────────────────────────────────
    "network.created": _EventSpec(
        required=("name", "driver"),
        description="Network created.",
    ),
    "network.deleted": _EventSpec(required=("id",), description="Network deleted."),
    "network.connect": _EventSpec(
        required=("network", "container"),
        description="Container attached to a network.",
    ),
    "network.disconnect": _EventSpec(
        required=("network", "container"),
        description="Container detached from a network.",
    ),
    "networks.pruned": _EventSpec(required=("count",), description="Unused networks pruned."),
    # ── Compose ───────────────────────────────────────────────────────────
    "compose.upload": _EventSpec(
        required=("project", "services"),
        description="Compose YAML accepted; stack about to deploy.",
    ),
    "compose.up": _EventSpec(required=("project",), description="Compose stack deployed."),
    "compose.up_failed": _EventSpec(
        severity="warning",
        required=("project", "stderr"),
        description="compose up returned non-zero; stderr truncated at 500 chars.",
    ),
    "compose.down": _EventSpec(required=("project",), description="Compose stack torn down."),
    "compose.down_failed": _EventSpec(
        severity="warning",
        required=("project", "stderr"),
        description="compose down returned non-zero.",
    ),
    "compose.service_restarted": _EventSpec(
        required=("project", "service"),
        optional=("container_id",),
        description="Single compose service restarted.",
    ),
    "compose.service_logs_failed": _EventSpec(
        severity="warning",
        required=("project", "service", "error"),
        description="Per-service log fetch failed; aggregate view fell back.",
    ),
    # ── Undo queue ────────────────────────────────────────────────────────
    "undo.enqueued": _EventSpec(
        required=("token_suffix", "kind", "id", "expires_in"),
        description="Destructive op deferred under the undo window.",
    ),
    "undo.fired": _EventSpec(
        required=("token_suffix", "kind", "id"),
        description="Undo window elapsed; op executed.",
    ),
    "undo.fired_on_shutdown": _EventSpec(
        required=("token_suffix", "kind", "id"),
        description=(
            "Server shutdown (SIGTERM, lifespan exit) flushed a pending "
            "undo op before the window would have elapsed naturally. "
            "Distinguishable from `undo.fired` so an incident reviewer "
            "can tell scheduled fires from shutdown-flush fires."
        ),
    ),
    "undo.cancelled": _EventSpec(
        required=("token_suffix", "kind", "id"),
        description="Operator cancelled the pending op before the window elapsed.",
    ),
    "undo.fire_failed": _EventSpec(
        severity="error",
        required=("token_suffix", "kind", "id", "error"),
        description="Op fired but raised; forensics needed — caller already got 200.",
    ),
    "undo.queue_full": _EventSpec(
        severity="warning",
        required=("depth", "kind"),
        description="Undo queue at cap; new deletions run synchronously.",
    ),
    # ── System ────────────────────────────────────────────────────────────
    "system.pruned": _EventSpec(
        optional=("containers", "images", "networks", "volumes", "space_mb"),
        description="Docker system prune completed.",
    ),
    "build_cache.pruned": _EventSpec(
        required=("space_mb",),
        description="Docker build cache pruned.",
    ),
    # ── Tunnel ────────────────────────────────────────────────────────────
    "tunnel.started": _EventSpec(
        required=("target", "socket"),
        description="SSH tunnel established.",
    ),
    "tunnel.reconnected": _EventSpec(
        required=("socket",),
        description="SSH tunnel re-established after being dropped.",
    ),
    # ── Docker client ─────────────────────────────────────────────────────
    "docker.connected": _EventSpec(
        required=("host",),
        description="Docker SDK client reconnected.",
    ),
    "docker.client_stale": _EventSpec(
        severity="warning",
        required=("action",),
        description="Ping failed; client marked for reconnect.",
    ),
    "docker.connection_failed": _EventSpec(
        severity="error",
        required=("host", "error"),
        description="Docker SDK could not connect on startup.",
    ),
    "docker.transient_error": _EventSpec(
        severity="warning",
        required=("error",),
        optional=("action",),
        description="Transient Docker SDK error absorbed by safe_docker_call.",
    ),
    # ── WebSocket audit ───────────────────────────────────────────────────
    "audit.ws_logs": _EventSpec(
        required=("container", "remote"),
        description="Log stream WS opened.",
    ),
    "audit.ws_exec": _EventSpec(
        required=("container", "remote"),
        description="Exec WS opened.",
    ),
    "audit.ws_exec_input": _EventSpec(
        required=("container", "remote", "bytes"),
        description=(
            "Exec WS received client input. The emitter logs byte-count "
            "ONLY — command content is intentionally NOT captured, so pasted "
            "credentials (`export TOKEN=…`, sudo prompts, etc.) never land "
            "in the audit log."
        ),
    ),
    "audit.ws_exec_input_oversize": _EventSpec(
        severity="warning",
        required=("container", "remote", "bytes", "limit"),
        description=(
            "Exec WS client sent a single input message larger than "
            "`_EXEC_MAX_INPUT_BYTES`. The socket is closed with "
            "code 4008 (policy-violation). Useful for SIEM rules that "
            "want to flag malformed / oversized paste activity."
        ),
    ),
    "audit.ws_handshake_failed": _EventSpec(
        severity="warning",
        required=("reason", "remote"),
        optional=("container",),
        description=(
            "WebSocket upgrade rejected during the handshake. `reason` is "
            "one of: `token_in_query` (bearer smuggled via ?token=…), "
            "`origin_denied` (Origin not in allowlist), `bad_container_id` "
            "(path validation failed), `auth_failed` (AUTH message absent "
            "or wrong token). Pair with HTTP `auth.denied` for a complete "
            "denied-access signal."
        ),
    ),
    "audit.ws_exec_disconnect": _EventSpec(
        required=("container", "remote"),
        description="Exec WS closed by client.",
    ),
    "ws.logs_error": _EventSpec(
        severity="warning",
        required=("container", "error"),
        description="Log stream WS raised; connection closed.",
    ),
    "ws.exec_error": _EventSpec(
        severity="warning",
        required=("container", "error"),
        description="Exec WS raised; connection closed.",
    ),
    # ── Generic HTTP audit (middleware-emitted on every request) ──────────
    "audit.api_access": _EventSpec(
        required=("channel", "event_type", "method", "path", "status", "remote", "auth"),
        optional=("token_suffix", "user", "resource_type", "resource_id"),
        description=(
            "One line per completed HTTP request, emitted by "
            "AuditLogMiddleware. `event_type` carries the classified action "
            "name (api.request, container.started, compose.up, image.pulled, "
            "rate_limit.exceeded, auth.denied, …) and is what SIEM rules "
            "typically filter on. `channel=\"audit\"` distinguishes this "
            "from debug/info lines from the same structlog configuration."
        ),
    ),
    "api.request": _EventSpec(
        required=("method", "path", "status"),
        optional=("remote", "auth"),
        description=(
            "Generic classified event_type attached to `audit.api_access` "
            "lines for any HTTP request that doesn't match a domain-specific "
            "action pattern. Not emitted as a top-level `event`; appears as "
            "`event_type` on `audit.api_access` lines."
        ),
    ),
    "rate_limit.exceeded": _EventSpec(
        severity="warning",
        required=("method", "path", "status"),
        optional=("remote", "auth"),
        description=(
            "event_type carried on `audit.api_access` lines when slowapi "
            "refused the request with 429. SIEM rules can count per `remote` "
            "to detect brute-force or scraping patterns."
        ),
    ),
    "auth.denied": _EventSpec(
        severity="warning",
        required=("method", "path", "status"),
        optional=("remote", "auth"),
        description=(
            "event_type carried on `audit.api_access` lines when a request "
            "failed the bearer-token check. Pair with "
            "`audit.ws_auth_lockout` for WS-specific brute-force signal."
        ),
    ),
    # ── Startup hardening warnings ────────────────────────────────────────
    "security.short_env_token": _EventSpec(
        severity="warning",
        required=("msg",),
        description=(
            "API_TOKEN from the environment is shorter than the setup "
            "wizard's 16-character minimum. Emitted once at startup so "
            "operators see weak-token deployments in their boot logs."
        ),
    ),
    "security.proxy_headers_untrusted": _EventSpec(
        severity="warning",
        required=("msg",),
        description=(
            "Startup heuristic detected that uvicorn may be running with "
            "--proxy-headers enabled while TRUST_FORWARDED_HEADERS is off. "
            "In that configuration X-Forwarded-For can forge audit `remote` "
            "and rate-limit keys. Surface so the operator fixes the launch "
            "recipe."
        ),
    ),
    # ── Tunnel lifecycle (authenticated) ─────────────────────────────────
    "tunnel.reconnect_noop": _EventSpec(
        required=("socket", "managed"),
        description=(
            "Operator hit Reconnect on a manual-tunnel deployment whose "
            "socket is still reachable. No SSH work was done — the Docker "
            "client was invalidated so the next API call refreshes state."
        ),
    ),
    "tunnel.manual_reconnect_required": _EventSpec(
        severity="info",
        required=("socket", "managed"),
        description=(
            "Operator hit Reconnect but the tunnel was not wizard-managed "
            "and the socket is down. SKIFF cannot re-open a tunnel it did "
            "not open itself (it never learned the SSH target). The client "
            "response includes the socket path so the operator can re-run "
            "`ssh -fNL <socket>:...`."
        ),
    ),
}


def known_events() -> frozenset[str]:
    """Return every event name declared in the catalogue."""
    return frozenset(_EVENTS)


def spec_for(name: str) -> _EventSpec | None:
    """Look up an event spec, or None if undeclared."""
    return _EVENTS.get(name)


def required_fields(name: str) -> frozenset[str]:
    """Required field set for an event. Empty for undeclared events."""
    spec = _EVENTS.get(name)
    return frozenset(spec.required) if spec else frozenset()


__all__ = ["known_events", "required_fields", "spec_for"]
