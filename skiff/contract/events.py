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
    "security.setup_window_open": _EventSpec(
        severity="warning",
        optional=("msg", "bind", "port", "window_secs", "lockout_attempts"),
        description=(
            "First-run setup wizard is reachable on BIND_HOST for "
            "SETUP_WINDOW_SECS after boot. Anyone with reach to that "
            "socket can claim the instance with their own token during "
            "the window (rate-limited; per-IP lockout). Set API_TOKEN "
            "in the environment to skip the wizard entirely."
        ),
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
    "audit.ws_auth_lockout": _EventSpec(
        severity="warning",
        required=("remote", "attempts", "lockout_secs"),
        description=(
            "WebSocket authentication tripped the per-IP brute-force "
            "lockout. Emitted on the exact attempt that crosses "
            "WS_AUTH_MAX_ATTEMPTS so SIEM alerts on activation, not "
            "on every failed attempt."
        ),
    ),
    # ── Audit-middleware-classified event_types ───────────────────────────
    # `_classify_event` in logging_setup.py emits these directly as the
    # `event_type` field of an `audit.api_access` line — they are NOT
    # round-tripped through the @secure_route.mutate audit= hook, so they
    # bypass the catalogue-drift guard in secure.py. Declaring them here
    # keeps the SIEM catalogue honest. (`auth.denied`, `api.request`,
    # `rate_limit.exceeded` are declared further down alongside the
    # handler-emitted counterparts so SIEM rule authors find them in
    # one place.)
    "auth.reviewer_denied": _EventSpec(
        severity="warning",
        optional=("remote", "path", "method"),
        description=(
            "Audit-middleware classification for a 403 whose envelope "
            "carries `auth.reviewer_read_only`. Separated from the "
            "generic `auth.denied` so SIEM can whitelist reviewer-mode "
            "noise while still alerting on stolen-token mutations."
        ),
    ),
    "image.list": _EventSpec(
        optional=("remote", "path"),
        description="Audit-middleware classification for GET /api/images.",
    ),
    "audit.log_read": _EventSpec(
        optional=("remote", "path"),
        description=(
            "Audit-middleware classification for GET /api/system/audit-log "
            "(and downloads). Distinct from the catch-all so SIEM rules "
            "can alert specifically on audit-tail exfil attempts."
        ),
    ),
    "container.logs_stream": _EventSpec(
        optional=("remote", "container"),
        description=(
            "Audit-middleware classification for WS upgrades to "
            "/ws/logs/{id}. The handler emits `audit.ws_logs` separately "
            "with the container id; this classification fires on the "
            "HTTP upgrade side."
        ),
    ),
    "container.exec_session": _EventSpec(
        optional=("remote", "container"),
        description=(
            "Audit-middleware classification for WS upgrades to "
            "/ws/exec/{id}. The handler emits `audit.ws_exec` separately "
            "with the container id; this classification fires on the "
            "HTTP upgrade side."
        ),
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
        required=("id",),
        optional=("reason",),
        description=(
            "Clone target already IS the replace_id — replace skipped. "
            "`id` is the new container's short id; `reason` explains why."
        ),
    ),
    "container.replaced": _EventSpec(
        required=("new_id", "old_id"),
        description="Clone replaced an older container in-place.",
    ),
    "container.exited_early": _EventSpec(
        severity="warning",
        required=("id", "name", "image", "exit_code"),
        description=(
            "A container exited within ~800 ms of `/run`. "
            "Response includes exit_code + tail of logs so the UI can "
            "surface the failure without a separate logs call."
        ),
    ),
    "container.replace_cleanup_failed": _EventSpec(
        severity="warning",
        required=("new_id", "old_id", "error"),
        description=(
            "Failed to remove the old container during a replace — "
            "manual cleanup needed. `new_id` is the successful clone; "
            "`old_id` is what we could not clean up."
        ),
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
    "container.cp_get": _EventSpec(
        required=("id", "path"),
        optional=("size_bytes",),
        description="Container file / directory streamed out via `docker cp`-equivalent.",
    ),
    "container.cp_get_truncated": _EventSpec(
        severity="warning",
        required=("id", "path", "cap_mb"),
        description=(
            "`docker cp`-out truncated at the configured size cap; raise CONTAINER_CP_MAX_MB or tar a smaller path."
        ),
    ),
    "container.cp_put": _EventSpec(
        required=("id", "path"),
        description="Tar archive uploaded into a container via POST /api/containers/{id}/files.",
    ),
    "container.cp_put_ok": _EventSpec(
        required=("id", "path", "size_bytes"),
        description="Container cp-put succeeded.",
    ),
    "container.committed": _EventSpec(
        required=("id", "repository", "tag"),
        description="Running container committed to a new local image.",
    ),
    "container.upload_ok": _EventSpec(
        required=("id", "path", "filename", "size_bytes"),
        description="Multipart file upload accepted into a container via /api/containers/{id}/upload.",
    ),
    "image.pruned": _EventSpec(
        description="Dangling / unused images pruned via /api/images/prune.",
    ),
    "system.events_failed": _EventSpec(
        severity="warning",
        optional=("error",),
        description="`docker events` poll returned an unexpected shape; returning best-effort partial.",
    ),
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
    "compose.started": _EventSpec(
        required=("project",),
        description="Compose stack `start`ed (containers resumed without re-create).",
    ),
    "compose.stopped": _EventSpec(
        required=("project",),
        description="Compose stack `stop`ed (containers halted, not removed).",
    ),
    "compose.pulled": _EventSpec(
        required=("project",),
        description="Compose stack images pulled (latest tags fetched).",
    ),
    "compose.scaled": _EventSpec(
        required=("project", "service", "replicas"),
        description="Compose service scaled to N replicas.",
    ),
    "compose.subcommand_failed": _EventSpec(
        severity="warning",
        required=("project", "subcommand", "stderr"),
        description="A compose subcommand (stop/start/pull/scale) returned non-zero.",
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
    "profile.switched": _EventSpec(
        required=("old", "new"),
        optional=("exec_sessions_closed",),
        description=(
            "Runtime PROFILE changed from `old` to `new` via "
            "POST /api/profile/enter-reviewer (one-way to reviewer). "
            "Emitted regardless of caller — the UI dropdown is one "
            "trigger, curl / CI / SIEM health checks are also in scope."
        ),
    ),
    "audit.ws_exec_terminated": _EventSpec(
        required=("container", "reason"),
        severity="warning",
        description=("A live exec WebSocket was force-closed by the server (e.g. profile switched to reviewer)."),
    ),
    "audit.extras_invalid": _EventSpec(
        optional=("path", "method"),
        severity="warning",
        description=(
            "Audit middleware could not build _AuditExtras for a request "
            "(e.g. over-long resource id). Line emitted without the extras."
        ),
    ),
    "undo.cancelled_by_reviewer": _EventSpec(
        required=("token_suffix", "kind", "id"),
        severity="warning",
        description=(
            "Undo timer fired after PROFILE transitioned to reviewer; "
            "the queued destructive op was skipped, not executed."
        ),
    ),
    "undo.shutdown_flush_timeout": _EventSpec(
        severity="error",
        required=("remaining", "timeout"),
        description=(
            "Lifespan shutdown hit SHUTDOWN_FLUSH_TIMEOUT while draining "
            "the undo queue. `remaining` ops stay in-memory and are lost "
            "when the process exits."
        ),
    ),
    "ws.detect_shell_timeout": _EventSpec(
        severity="warning",
        required=("container",),
        description=("The 5 s timeout on `which /bin/bash` fired; session opened with /bin/sh fallback."),
    ),
    "security.bind_non_loopback": _EventSpec(
        severity="warning",
        required=("bind_host", "msg"),
        description=("SKIFF bound to a non-loopback interface at startup. Emitted once per process boot."),
    ),
    "security.ci_profile_needs_token": _EventSpec(
        severity="warning",
        required=("msg",),
        description=(
            "PROFILE=ci booted without an API_TOKEN. The automation "
            "persona does not fit a wizard-driven first run; emit a "
            "loud warning so the operator fixes the env before a "
            "headless CI runner hits a setup wizard."
        ),
    ),
    "undo.fired_already_gone": _EventSpec(
        required=("token_suffix", "kind", "id"),
        description=(
            "Undo timer fired but the target resource was already "
            "absent (external delete or rebuild). Desired end-state "
            "reached; not counted as a failure."
        ),
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
            'typically filter on. `channel="audit"` distinguishes this '
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
