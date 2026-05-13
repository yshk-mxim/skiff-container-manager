# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Observe and operate the running server.

Holds the routes that report engine state (`/api/system/info`,
`/api/system/metrics`, `/api/system/df`) and the config / connect-snippets
endpoints used by the UI, plus the `/api/docs` landing page and the two
static handlers (`/` serves the SPA, `/LICENSE` serves the MIT text).
Setup, health, audit, debug, and undo each live in their own router
module.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import replace
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from skiff import config, docker_client, validators
from skiff.auth import AUTH  # decorator arg — direct import for readability
from skiff.contract.errors import http_error
from skiff.rate import RATE
from skiff.secure import secure_route
from skiff.validators import safe_docker_call

# Used by the config-knob PUT handler that lives above the rest of the
# module's structlog logger definition (line ~900). Same instance, hoisted
# so early routes can audit-log without forward references.
log = structlog.get_logger(__name__)

router = APIRouter()


# ── Config ─────────────────────────────────────────────────

_LOOPBACK_BINDS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _resolve_knob(knob_name: str, spec) -> Any:
    """Return the current value of an exposed knob.

    Four sources, in priority order:
      1. live _cfg.<attr> for wizard-managed knobs (api_token / docker_host)
      2. config.<NAME> module attribute — catches runtime edits through
         PUT /api/config/knobs/<name> that write to the module, so the
         viewer reflects the new value immediately.
      3. validator(env_value) when env is set and the knob has a validator
      4. spec.default otherwise

    Validator errors fall back to the default so a bad env entry can't
    500 /api/config.
    """
    attr = knob_name.lower()
    if hasattr(config._cfg, attr):
        return getattr(config._cfg, attr)
    # Uppercase module attribute is the canonical live value. Both the
    # import-time assignment AND runtime PUT edits go here, so this is
    # the most accurate read for almost every knob.
    if hasattr(config, knob_name):
        return getattr(config, knob_name)
    raw = os.environ.get(knob_name, spec.default)
    if raw is None or spec.validator is None:
        return raw
    try:
        return spec.validator(raw)
    except (ValueError, TypeError):
        return spec.default


# Knob grouping for the GUI viewer mirrors the section headers in
# `skiff/config.py` itself (`# ── Docker client ──`, `# ── WebSocket ──`,
# etc.) — see config.knob_section(). That's the single place to look
# when adding a knob, so the viewer follows suit automatically.

# Hide-from-GUI list. A knob can stay `expose=True` for CLI/SIEM scrapers
# while still being stripped from the Settings viewer when its presence
# would clutter the operator experience (e.g. a knob that's only
# interesting during a specific troubleshooting workflow). Empty today
# — the seam lets a future audit close a visibility concern without
# touching config_knob() call sites.
_KNOBS_HIDDEN_FROM_GUI: frozenset[str] = frozenset()


# Three-state editability taxonomy for the Settings viewer.
#
# Every exposed knob MUST land in exactly one set. A knob missing from
# all three renders as "LIFECYCLE" by default (restart required) — the
# safe, conservative choice — but tests enforce the explicit assignment
# so an addition is deliberate.
#
# LIVE        — the server reads the knob on every use (middleware,
#               per-request validator, per-new-WS-session). A PUT via
#               /api/config/knobs/<name> updates it immediately and
#               subsequent requests see the new value. Existing long-
#               lived state (already-open WS sessions, etc.) keeps the
#               old value — acceptable, documented in the tooltip.
#
# SECURITY    — changing at runtime would weaken a security control
#               (origin allowlist, rate-limit scale that was registered
#               at import, debug endpoint flag). GUI never edits these;
#               the operator must set the env var / TOML and restart.
#               This is POLICY — not a technical limitation.
#
# LIFECYCLE   — the server reads the knob ONCE at import (SDK client
#               construction, uvicorn bind, logging handler open). A
#               runtime edit would update the registry but have no
#               operational effect until restart. Rather than lie, the
#               viewer tells the operator exactly why.
_LIVE_EDITABLE: frozenset[str] = frozenset(
    {
        # Re-read per-request by middleware, validators, or query-param
        # checkers — editing updates the live behaviour immediately.
        "MAX_BODY_BYTES",
        "MAX_COMPOSE_SIZE",
        "MAX_LOG_TAIL",
        "MAX_AUDIT_LINES",
        "MAX_CONTAINERS",
        "MAX_PORT_MAPPINGS",
        "CONTAINER_CP_MAX_MB",
        "CONTAINER_LS_MAX_ENTRIES",
        "BODY_READ_TIMEOUT_SECS",
        # Session timeouts — /api/config serves the live value; the UI
        # picks it up on its next config poll.
        "SESSION_IDLE_SECS",
        "SESSION_ABS_TIMEOUT",
        # Undo queue — enqueue reads config.UNDO_DELAY_SECS per call.
        "UNDO_DELAY_SECS",
        "UNDO_QUEUE_MAX_DEPTH",
        # Compose + per-call budgets read per call.
        "COMPOSE_UP_TIMEOUT",
        "COMPOSE_DOWN_TIMEOUT",
        "COMPOSE_MAX_REPLICAS",
        "SHUTDOWN_FLUSH_TIMEOUT",
        "DF_TIMEOUT",
        # WebSocket — new sessions pick up new value; existing keep old.
        "WS_LOG_TAIL",
        "WS_KEEPALIVE_INTERVAL",
        "WS_LOG_IDLE_TIMEOUT",
        "WS_EXEC_IDLE_TIMEOUT",
        "WS_EXEC_RECV_TIMEOUT",
        "WS_TOKEN_TIMEOUT",
        "WS_MAX_PER_IP",
        "WS_AUTH_MAX_ATTEMPTS",
        "WS_AUTH_LOCKOUT_SECS",
        "WS_KEEPALIVE_REVALIDATE_EVERY",
        # Container op budgets — read per-call.
        "CONTAINER_STOP_TIMEOUT",
        "CONTAINER_RESTART_TIMEOUT",
        "CONTAINER_STATS_TIMEOUT",
        "IMAGE_PULL_TIMEOUT",
        # Audit log line cap is re-read; rotation size/backup count are
        # lifecycle (file handler constructed at import).
        # Docker Hub proxy timeouts — per-call.
        "REGISTRY_TIMEOUT",
        "REGISTRY_MAX_TAGS",
        "REGISTRY_DESC_MAX",
        "REGISTRY_SEARCH_PAGE_SIZE",
        # Setup wizard window — read on wizard probes.
        "SETUP_WINDOW_SECS",
        "SETUP_MAX_ATTEMPTS",
        "SETUP_LOCKOUT_SECS",
        # Docker probe + health.
        "PROBE_DOCKER_TIMEOUT",
        # Browser polling intervals — the JS re-reads these from /api/config
        # on every session refresh, so a runtime edit takes effect on the
        # next config poll (within a second for an active session).
        "CONTAINERS_POLL_MS",
        "DASHBOARD_POLL_MS",
        "EVENTS_POLL_MS",
        "FETCH_TIMEOUT_MS",
    }
)

_SECURITY_READONLY: frozenset[str] = frozenset(
    {
        # Origin / network exposure — runtime change would silently widen
        # attack surface or let an insider evade audits.
        "ALLOWED_ORIGINS",
        "ALLOWED_REGISTRIES",
        "TRUST_FORWARDED_HEADERS",
        "BIND_HOST",
        # DOCKER_HOST has a dedicated runtime mutation path through the
        # setup wizard (/api/setup). Editing via the generic knob endpoint
        # would bypass wizard validation (tunnel probe, SSH config).
        "DOCKER_HOST",
        "DOCKER_VM_HOST",
        # Debug endpoint flag — policy toggle.
        "SKIFF_DEBUG_THREADS",
        # Rate-limit scale — slowapi buckets register at import time, so a
        # runtime edit would update the display without actually re-bucketing.
        # Keeping it policy-locked avoids misleading the operator.
        "RATE_LIMIT_SCALE",
        # Profile switch has a dedicated one-way endpoint.
        "PROFILE",
    }
)

_LIFECYCLE_READONLY: frozenset[str] = frozenset(
    {
        # Audit logging handler constructed at import; runtime edit would
        # update the registry but leave the file rotator on old values.
        "AUDIT_LOG",
        "AUDIT_MAX_MB",
        "AUDIT_BACKUP_COUNT",
        # Filesystem paths resolved at import.
        "COMPOSE_DIR",
        "TUNNEL_DEFAULT_SOCKET",
        # Docker SDK client constructed at import with these params.
        "DOCKER_CLIENT_TIMEOUT",
        "DOCKER_POOL_SIZE",
        "DOCKER_PING_TTL",
        "DOCKER_BACKOFF",
        # TCP keepalive set at socket open.
        "TCP_KEEPALIVE_IDLE",
        "TCP_KEEPALIVE_INTERVAL",
        "TCP_KEEPALIVE_COUNT",
        # SSH tunnel params set at connection open.
        "TUNNEL_CONNECT_TIMEOUT",
        "TUNNEL_SOCKET_WAIT",
        "TUNNEL_SOCKET_POLL",
        "TUNNEL_SERVER_ALIVE_INTERVAL",
        "TUNNEL_SERVER_ALIVE_COUNT",
        "TUNNEL_STOP_TIMEOUT",
        # Uvicorn wiring — read by the boot script.
        "PORT",
        "UVICORN_WORKERS",
        "UVICORN_LOG_LEVEL",
    }
)


def _knob_edit_classification(name: str) -> tuple[str, str]:
    """Return `(status, reason)` for the GUI viewer.

    `status` is one of "live" / "security" / "lifecycle". `reason` is a
    one-line tooltip explaining why a non-live knob can't be edited
    here. Every exposed knob lands in exactly one of the three sets;
    tests/test_config_precedence.py::test_every_exposed_knob_has_edit_classification
    enforces the invariant.
    """
    if name in _LIVE_EDITABLE:
        return ("live", "Read on every use — edits apply immediately.")
    if name in _SECURITY_READONLY:
        return ("security", "Policy-locked — changes must go through an env/TOML edit + restart.")
    if name in _LIFECYCLE_READONLY:
        return (
            "lifecycle",
            "Read once at process start — changing here would not take effect until restart.",
        )
    # Unclassified: default to lifecycle (the conservative choice) and
    # let the test enforcing classification catch the omission.
    return ("lifecycle", "Unclassified — defaulting to restart-required.")


def _is_insecure_mode(bind: str) -> bool:
    """True when bound to a non-loopback interface with no API_TOKEN set."""
    return bind not in _LOOPBACK_BINDS and not config._cfg.api_token.strip()


@router.get("/api/config", dependencies=AUTH, tags=["auth"])
@secure_route.read(RATE.READ)
def get_config(request: Request):
    """Return non-secret server configuration for the UI.

    Derived from the `config_knob` registry — each knob with
    `expose=True, secret=False` contributes one field. Computed fields
    (profile / bind_host / rate_limit_scale / insecure_mode) are layered
    on top. `insecure_mode` is computed server-side so a compromised
    client can't silence the warning banner.
    """
    import skiff.config as _config_module

    bind = config.BIND_HOST or "127.0.0.1"
    resolved = {
        knob_name.lower(): _resolve_knob(knob_name, spec)
        for knob_name, spec in config.knobs().items()
        if spec.expose and not spec.secret
    }
    # The PORT knob captures the uvicorn default at import time, not the
    # port the server is currently bound to. Fix the field to reflect the
    # live port so scrapers that build "visit me" links from /api/config
    # get the right number.
    live_port = request.url.port
    if live_port is not None:
        resolved["port"] = int(live_port)
    # AUDIT_MAX_MB's validator stores bytes internally (value * 1024²) so
    # the raw lowercased field would be bytes under a name that says "MB".
    # Emit both: the MB integer the operator set AND the authoritative
    # byte count under a correctly-named field. Same pattern applies to
    # any other "display unit != storage unit" knob added later.
    if "audit_max_mb" in resolved:
        bytes_value = resolved["audit_max_mb"]
        resolved["audit_max_bytes"] = bytes_value
        try:
            resolved["audit_max_mb"] = int(bytes_value) // (1024 * 1024)
        except (TypeError, ValueError):
            pass  # keep raw value if non-int (shouldn't happen)
    # Expose this caller's own WS-auth-lockout remaining time so the UI can
    # paint a "WebSocket locked out" banner on page load without waiting for
    # the next WS handshake to fail. Reveals only the remaining countdown
    # for the caller's own IP; an attacker would already know they're
    # locked out from their failed attempts.
    from skiff import auth as _auth

    client_ip = request.client.host if request.client else ""
    ws_lockout = _auth.ws_lockout_remaining(client_ip)
    return {
        **resolved,
        "profile": config.PROFILE,
        "bind_host": bind,
        "rate_limit_scale": _config_module._RATE_SCALE,
        "insecure_mode": _is_insecure_mode(bind),
        "ws_auth_locked_remaining_secs": ws_lockout,
    }


@router.get("/api/config/knobs", dependencies=AUTH, tags=["auth"])
@secure_route.read(RATE.READ)
def get_config_knobs(request: Request):
    """Return every exposed knob with metadata, for the GUI config viewer.

    Shape per entry:
      name        : "SESSION_IDLE_SECS"  (env-var / registry name)
      value       : live value, JSON-safe. `null` when `secret=True`.
      default     : fleet default (from TOML or inline). String or null.
      source      : "env" | "toml" | "default" | "unset" — where the
                    current value came from. Lets the operator tell at a
                    glance whether an override is in effect.
      doc         : human-readable description (same string that gen-
                    erates docs/config-knobs.md).
      category    : grouping label for the UI (Docker client, Session, …).
      secret      : true when the knob is marked secret (never show the
                    value).
      editable    : true when this knob is safely editable at runtime via
                    a future /api/config/knobs/<name> PUT. Empty today;
                    the viewer still shows the restart-required note.
      hidden      : true when the GUI should hide this knob from the
                    viewer even though it's exposed via /api/config (for
                    the minority of operator-only values).

    Non-exposed knobs (no `expose=True`) are NEVER returned by this
    endpoint — they are intentionally not part of the GUI surface. The
    ordered list is stable: sorted by category, then by name.
    """
    seen_categories: dict[str, list[dict]] = {}
    for knob_name, spec in config.knobs().items():
        if not spec.expose:
            continue
        cat = config.knob_section(knob_name)
        # Secrets never leave the server with a value. The browser has
        # a second-line defence (settings.js renders "(redacted)" even
        # if a future bug populates `value` for a secret), but the
        # authoritative protection is right here.
        value = None if spec.secret else _resolve_knob(knob_name, spec)
        status, reason = _knob_edit_classification(knob_name)
        # An env-sourced value is never editable via the generic PUT —
        # precedence rule: env always wins. Fold that into `edit_status`
        # so the UI renders "LIFECYCLE" (with an env-specific reason)
        # rather than offering a control that would 409 on submit.
        if spec.source == "env" and status == "live":
            status = "lifecycle"
            reason = (
                "Overridden by an environment variable. Env-var values are "
                "immutable at runtime — unset / change the env and restart."
            )
        entry = {
            "name": knob_name,
            "value": value,
            "default": spec.default,
            "source": spec.source,
            "doc": spec.doc,
            "category": cat,
            "secret": spec.secret,
            "edit_status": status,
            "edit_reason": reason,
            "hidden": knob_name in _KNOBS_HIDDEN_FROM_GUI,
        }
        seen_categories.setdefault(cat, []).append(entry)
    grouped = [
        {
            "category": cat,
            "knobs": sorted(seen_categories[cat], key=lambda k: k["name"]),
        }
        for cat in sorted(seen_categories)
    ]
    return {
        "groups": grouped,
        "counts": {
            "live": sum(1 for k in config.knobs() if k in _LIVE_EDITABLE),
            "security": sum(1 for k in config.knobs() if k in _SECURITY_READONLY),
            "lifecycle": sum(1 for k in config.knobs() if k in _LIFECYCLE_READONLY),
        },
    }


# ── Runtime knob edit (LIVE-editable subset only) ────────────────
#
# The viewer flags a small whitelist of knobs as runtime-editable — those
# whose value the server re-reads on every use, so a PUT here takes
# effect immediately for subsequent requests. Everything else either
# needs a restart (lifecycle) or is policy-locked (security) and rejects
# with a catalogued envelope.
#
# Security posture:
#  - Strict auth (same gate as audit log download).
#  - Env-sourced values refuse (env-wins precedence — consistency with
#    /api/setup's env_managed lock).
#  - Secrets refuse (defense-in-depth; secrets never carry expose=True
#    so they shouldn't reach this code path anyway).
#  - Every successful change is audit-logged with before/after values
#    under the `config.knob_updated` event.
#  - Value is validated through the knob's declared validator — an
#    invalid value returns the envelope the validator raises without
#    mutating any state.


class _KnobUpdateBody(BaseModel):
    """PUT body — `value` is the new value as a STRING. Server parses it
    through the knob's declared validator so every knob gets the same
    input handling as its env var would have at boot."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=0, max_length=2048)


@router.put("/api/config/knobs/{knob_name}", dependencies=AUTH, tags=["auth"])
@secure_route.mutate(RATE.WRITE, audit="config.knob_updated")
def update_config_knob(
    request: Request,
    knob_name: str,
    body: _KnobUpdateBody,
):
    """Update a LIVE-editable knob in-memory for this process.

    Rejects with a specific envelope when the knob is unknown, secret,
    env-sourced, policy-locked, or lifecycle-only. Validator errors are
    surfaced unchanged so the user sees WHY their value was rejected.

    Persistence: ephemeral. The change survives until the next process
    restart, matching the setup-wizard layer. Operators who need the
    change across restarts edit the env var or defaults.toml.
    """
    # Same identifier grammar as env-var names — reject anything else up
    # front so a malformed URL can't slip through the registry lookup.
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", knob_name):
        raise http_error("validation.bad_input", message=f"invalid knob name: {knob_name!r}")
    spec = config.knobs().get(knob_name)
    if spec is None or not spec.expose:
        raise http_error("config.knob_not_found", name=knob_name)
    if spec.secret:
        raise http_error("config.knob_secret_locked", name=knob_name)
    if spec.source == "env":
        raise http_error("config.knob_env_sourced", name=knob_name)
    if knob_name in _SECURITY_READONLY:
        raise http_error("config.knob_security_locked", name=knob_name)
    if knob_name not in _LIVE_EDITABLE:
        raise http_error("config.knob_lifecycle_locked", name=knob_name)
    # Validate through the knob's declared validator. If the knob was
    # registered without one, write the raw string.
    raw = body.value
    if spec.validator is not None:
        try:
            new_value = spec.validator(raw)
        except (ValueError, TypeError) as exc:
            raise http_error(
                "validation.bad_input",
                message=f"invalid value for {knob_name}: {exc}",
            ) from exc
    else:
        new_value = raw
    # Capture the old value for the audit trail BEFORE mutating, so a
    # reviewer can reconstruct the change. Never audit a secret's value;
    # `spec.secret=False` here (rejected above), so it's fine.
    old_value = getattr(config, knob_name, None)
    # Mutate both the module attribute (what routes read via
    # `config.SESSION_IDLE_SECS`) AND the registry source-tracking so
    # subsequent /api/config responses reflect the change.
    setattr(config, knob_name, new_value)
    config._KNOBS[knob_name] = replace(spec, source="runtime")
    log.info(
        "config.knob_updated",
        name=knob_name,
        old=old_value if not spec.secret else "[redacted]",
        new=new_value if not spec.secret else "[redacted]",
    )
    return {"name": knob_name, "value": new_value, "source": "runtime"}


# ── Runtime profile switch ────────────────────────────────


# ── Runtime profile switch ────────────────────────────────
#
# Only one direction is exposed: any → reviewer. A reviewer cannot flip
# back out via the UI — that would defeat the audit posture where an
# admin hands a session to a reviewer with mutations locked off. Exiting
# reviewer mode requires a server restart (and therefore env access).
@router.post("/api/profile/enter-reviewer", dependencies=AUTH, tags=["auth"])
@secure_route.mutate(RATE.WRITE)
async def enter_reviewer_mode(request: Request) -> dict:
    """One-way runtime switch into reviewer (read-only) profile.

    Rate-limits and audit behaviour tied to the new PROFILE take effect
    immediately for the reviewer-mode gate; rate-limit *buckets* stay at
    their startup values (slowapi registers at import). Switching back
    requires a restart so a reviewer can't undo the lock.

    Also force-closes every active exec WebSocket so an insider cannot
    keep mutating container state through a shell opened before the
    switch. In-flight HTTP mutations already past the reviewer guard
    are left to complete (see `docs/dev/state_transitions.md`); only
    future requests see reviewer semantics.

    Audit is emitted inline so the pre-switch PROFILE value is captured
    in `old`; `secure_route`'s `audit_fields` hook runs after the
    handler returns, by which point PROFILE has already moved.
    """
    import structlog

    import skiff.config as _config_module
    from skiff.routers.containers_ws import close_active_exec_sessions

    old = _config_module.PROFILE
    # Flip PROFILE FIRST, then close sessions. `_try_register_exec_ws`
    # and `close_active_exec_sessions` both take `_ws_lock`; a handler
    # in its register-gap reads the post-flip PROFILE and aborts before
    # inserting, so the snapshot-and-close below cleans up every
    # registered session without a residual window. On the idempotent
    # branch, we still call `close_active_exec_sessions` — it is a
    # no-op when the set is already empty, but it closes the race
    # where a second "enter reviewer" concurrent with the first tries
    # to sweep sessions that slipped into the set after the first
    # snapshot.
    _config_module.PROFILE = "reviewer"
    closed = await close_active_exec_sessions(reason="reviewer_mode_entered")
    if old == "reviewer":
        # Idempotent — already in reviewer mode. Don't pollute the
        # audit log with a second "reviewer → reviewer" entry.
        return {"ok": True, "profile": "reviewer", "exec_sessions_closed": closed}
    structlog.get_logger(__name__).info(
        "profile.switched",
        old=old,
        new="reviewer",
        exec_sessions_closed=closed,
    )
    return {"ok": True, "profile": "reviewer", "exec_sessions_closed": closed}


# ── Connect snippets ──────────────────────────────────────


def _audit_log_glob() -> str:
    """Return the audit-log path so scrapers can tail it across a fleet.

    The connect-snippets TOML uses this as `{audit_log_glob}`. If the
    operator configured a fixed `AUDIT_LOG` path (the recommended
    production setup), that's returned verbatim. Otherwise we fall back
    to a platform-shaped per-user glob — illustrative for a local install.
    """
    configured = str(config.AUDIT_LOG_PATH)
    default_state = str(config._STATE_ROOT / "audit.jsonl")
    if configured != default_state:
        return configured
    if sys.platform == "darwin":
        return "/Users/*/Library/Application Support/skiff/audit.jsonl"
    if sys.platform.startswith("linux"):
        return "/home/*/.local/state/skiff/audit.jsonl"
    # Windows / BSD / other: emit the resolved path; fleet-level scraping
    # on non-mainstream OSes should configure AUDIT_LOG explicitly.
    return configured


def _render_snippet(template: str, ctx: dict[str, str]) -> str:
    """Substitute `{placeholder}` tokens in a connect-snippet template.

    Uses dict.get fallbacks so a typo in the TOML (e.g. `{dockerhost}`)
    leaves the literal braces in the rendered snippet rather than
    crashing with KeyError — operators reading the snippet will notice
    the unresolved placeholder and file a fix.
    """
    for key, val in ctx.items():
        template = template.replace("{" + key + "}", val)
    return template


@router.get("/api/connect-snippets", dependencies=AUTH, tags=["system"])
@secure_route.read(RATE.READ)
def connect_snippets(request: Request, tool: str | None = None) -> dict:
    """Return rendered per-tool snippets for the Connect-external-tool panel.

    The catalogue lives in `skiff/_config/connect_snippets.toml`; this endpoint
    interpolates live runtime values (docker_host, origin, scheme, etc.)
    into each block and returns the ready-to-render list. No user input
    reaches the TOML template; `request` supplies the scheme / host used
    by the Prometheus / Caddy / oauth2-proxy snippets.

    Pass `?tool=<id>` to return only that tool's block — useful for the
    Copy-snippet UX without shipping all 10 tools to the browser.
    """
    # Forwarded headers are only honoured when TRUST_FORWARDED_HEADERS is set,
    # otherwise any caller could override the origin shown in the snippets.
    if config.TRUST_FORWARDED_HEADERS:
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost:8080")
    else:
        scheme = request.url.scheme
        host = request.headers.get("host", "localhost:8080")
    origin = f"{scheme}://{host}"
    ctx = {
        "dockerHost": config._cfg.docker_host or "unix:///var/run/docker.sock",
        "scheme": scheme,
        "host": host,
        "origin": origin,
        "metricsUrl": f"{origin}/api/system/metrics",
        "audit_log_glob": _audit_log_glob(),
    }
    raw_tools = config._TOML_CONNECT_SNIPPETS.get("tool", [])
    if tool:
        raw_tools = [t for t in raw_tools if t.get("id") == tool]
    rendered: list[dict[str, Any]] = [
        {
            "id": t["id"],
            "label": t["label"],
            "hint": _render_snippet(t.get("hint", ""), ctx),
            "note": _render_snippet(t.get("note", ""), ctx),
            "blocks": [
                {
                    "kind": b.get("kind", "block"),
                    "filename": b.get("filename", ""),
                    "content": _render_snippet(b.get("content", ""), ctx),
                }
                for b in t.get("blocks", [])
            ],
        }
        for t in raw_tools
    ]
    return {"tools": rendered}


# ── OpenAPI docs landing (no auth, CSP-safe self-hosted Swagger UI) ──
# FastAPI's built-in /docs and /redoc pull assets from jsDelivr, which our
# strict `script-src 'self'` CSP blocks. We vendor Swagger UI 5.32.4 under
# skiff/static/swagger-ui/ and integrate it here — same-origin, no CDN,
# Try-it-out requests hit this instance directly so the operator can
# exercise routes without leaving the page.
#
# Swagger UI uses inline <style> attributes at runtime, so `style-src` keeps
# `'unsafe-inline'`. Scripts come from /static/ only.

_API_DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SKIFF API documentation</title>
<link rel="stylesheet" href="/static/swagger-ui/swagger-ui.css">
<style>
  body { margin: 0; padding: 0; background: #fafafa; }
  /* Hide Swagger's default topbar — we don't need the spec-URL input. */
  .swagger-ui .topbar { display: none; }
  /* SKIFF accent on the Authorize + Try-it-out surfaces. */
  .swagger-ui .btn.authorize,
  .swagger-ui .btn.authorize svg { color: #0f766e; border-color: #0f766e; }
  .swagger-ui .btn.authorize:hover { background: #0f766e; color: #fff; }
  .swagger-ui .btn.authorize:hover svg { fill: #fff; }
</style>
</head>
<body>
<div id="swagger-ui"></div>
<script src="/static/swagger-ui/swagger-ui-bundle.js"></script>
<script src="/static/swagger-ui/swagger-ui-standalone-preset.js"></script>
<script src="/static/core/docs.js"></script>
</body>
</html>
"""


@router.get("/api/docs", include_in_schema=False, tags=["system"])
@secure_route.public(RATE.PUBLIC)
def api_docs_landing(request: Request) -> Response:
    """Discoverability landing page for the OpenAPI spec (CSP-safe).

    Rate-limited at the PUBLIC tier. The raw `/api/openapi.json` is
    FastAPI-managed (not defined in this file) and is similarly unauthed;
    see SECURITY.md for the explicit enumeration of endpoints exempt
    from bearer-token auth (health probes, auth-discovery, API spec
    discoverability — each is rate-limited).
    """
    return Response(
        content=_API_DOCS_HTML,
        media_type="text/html; charset=utf-8",
        # CSP for THIS response: strict `script-src 'self'` (no unsafe-inline —
        # the origin-stitcher script lives at /static/core/docs.js). No
        # `connect-src` beyond 'self' — the Swagger Editor / Petstore buttons
        # open editor.swagger.io in a NEW TAB via target="_blank"; this page
        # never fetches them.
        headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'",
        },
    )


# ── Terminal iframe (sandboxed xterm.js host) ──
# xterm.js writes element.style.X assignments during cell rendering — a
# pattern the main SPA's strict `style-src 'self'` (no 'unsafe-inline')
# CSP would block. We confine xterm to this iframe-served HTML, which
# carries its own route-scoped CSP that DOES allow 'unsafe-inline' for
# style-src. The container ID lives in the URL path; the iframe's JS
# pulls the AUTH token from sessionStorage (same-origin with the parent)
# and connects directly to `/ws/exec/{container_id}`. The parent SPA
# embeds this route via `<iframe src="/api/terminal-frame/{id}">` and
# communicates via postMessage (see terminal-frame.js for the protocol).
#
# `frame-ancestors 'self'` permits embedding only by the same-origin
# parent — third-party sites cannot iframe this terminal.

_TERMINAL_FRAME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SKIFF Terminal</title>
<link rel="stylesheet" href="/static/xterm/xterm.css">
<link rel="stylesheet" href="/static/terminal-frame.css">
</head>
<body>
<div id="term" aria-label="Container shell"></div>
<div id="status" role="status" aria-live="polite"></div>
<script src="/static/xterm/xterm.js"></script>
<script src="/static/xterm/addon-fit.js"></script>
<script src="/static/terminal-frame.js"></script>
</body>
</html>
"""


@router.get(
    "/api/terminal-frame/{container_id}",
    include_in_schema=False,
    tags=["system"],
)
@secure_route.public(RATE.PUBLIC)
def terminal_frame_page(request: Request, container_id: str) -> Response:
    """Serve the CSP-isolated HTML that hosts xterm.js for a container.

    The main SPA embeds this route in an iframe via
    ``<iframe src="/api/terminal-frame/{id}">``. The frame brings its
    own route-scoped CSP so xterm's inline-style writes survive while
    the parent document stays under strict `style-src 'self'`.

    **Public by design.** Bearer-token auth lives in sessionStorage on
    the parent SPA and CANNOT travel with a browser-initiated iframe
    navigation (sessionStorage is not a cookie). The HTML this route
    returns is pure boilerplate — xterm.js + addon-fit script tags + a
    stub div + the terminal-frame.js bootstrap. It exposes no Docker
    state and no privileged information; the container_id in the path
    is just a string the iframe's JS reads back. The real auth gate
    is `/ws/exec/{id}`, which the iframe connects to over WebSocket
    and authenticates via `AUTH <bearer-token>` as the first frame
    (same pattern the WS protocol already uses, see logging_setup.py).
    Pre-existing protections still apply:
      * `frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN`
        prevent cross-origin embedding, so a malicious site cannot
        iframe this route to phish operators.
      * `secure_route.public(RATE.PUBLIC)` rate-limits anonymous hits.
      * The container_id is regex-validated below — anything outside
        Docker's ID/name grammar → 400, never echoed into the HTML.
    """
    # Reject anything outside Docker's container-ID/name grammar so the
    # path never carries an exotic codepoint into the URL the iframe
    # exposes. Accept either short-ID (hex) or container-name shapes;
    # the actual auth + Docker lookup happens inside /ws/exec/{id}.
    if not (
        validators.CONTAINER_ID_RE.fullmatch(container_id)
        or validators.CONTAINER_NAME_RE.fullmatch(container_id)
    ):
        return Response(
            content="Invalid container id",
            media_type="text/plain; charset=utf-8",
            status_code=400,
        )
    return Response(
        content=_TERMINAL_FRAME_HTML,
        media_type="text/html; charset=utf-8",
        headers={
            # `default-src 'none'` denies-by-default; every category is
            # explicitly enumerated below.
            #  - script-src 'self' — xterm.js + addon-fit + terminal-frame.js
            #    all ship from /static/. No inline scripts, no CDNs.
            #  - style-src 'self' 'unsafe-inline' — xterm.js writes
            #    inline element.style during render; this exception is
            #    what justifies sandbox-via-iframe. Scoped to this
            #    response; the parent SPA keeps strict style-src 'self'.
            #  - connect-src 'self' — WebSocket back to /ws/exec/.
            #  - img-src 'self' data: — xterm's cursor cell uses data URIs.
            #  - frame-ancestors 'self' — embeddable only by same-origin.
            #  - form-action 'none' — no form posts from this page.
            #  - base-uri 'none' — defence against <base> injection.
            #  - object-src 'none' — no plugin embeds.
            "Content-Security-Policy": (
                "default-src 'none'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "frame-ancestors 'self'; "
                "form-action 'none'; "
                "base-uri 'none'; "
                "object-src 'none'"
            ),
            # Defence-in-depth on top of the in-CSP frame-ancestors
            # directive. Some older user-agents only honour XFO.
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


# ── System Info ────────────────────────────────────────────


@router.get("/api/system/info", dependencies=AUTH, tags=["system"])
@secure_route.read(RATE.READ)
def system_info(request: Request, client=Depends(docker_client.docker_client_dep)) -> dict:
    """Return Docker engine version, OS, hardware, and container counts."""
    info = safe_docker_call(client.info)
    ver = safe_docker_call(client.version)
    return {
        "docker_version": info.get("ServerVersion", ""),
        "api_version": ver.get("ApiVersion", ""),
        "os": info.get("OperatingSystem", ""),
        "os_type": info.get("OSType", ""),
        "architecture": info.get("Architecture", ""),
        "kernel": info.get("KernelVersion", ""),
        "cpus": info.get("NCPU", 0),
        "memory_gb": round(info.get("MemTotal", 0) / 1024 / 1024 / 1024, 1),
        "containers": info.get("Containers", 0),
        "containers_running": info.get("ContainersRunning", 0),
        "containers_paused": info.get("ContainersPaused", 0),
        "containers_stopped": info.get("ContainersStopped", 0),
        "images": info.get("Images", 0),
        "storage_driver": info.get("Driver", ""),
        "logging_driver": info.get("LoggingDriver", ""),
        "cgroup_driver": info.get("CgroupDriver", ""),
        "docker_root_dir": info.get("DockerRootDir", ""),
        "security_options": info.get("SecurityOptions", []),
        "registries": info.get("RegistryConfig", {}).get("IndexConfigs", {}),
    }


# ── Prometheus-format metrics ─────────────────────────────
# AUTH'd because metrics include container names + image paths (workload
# leak). Scrapers pass the bearer token like any other client.


def _escape_prom_label(value: str) -> str:
    """Escape a label value per Prometheus exposition format (§ Text format)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _docker_host_label(raw: str) -> str:
    """Derive a stable, non-leaking label value from a Docker host URL.

    The raw value (e.g. `unix:///tmp/skiff-docker.sock` or
    `tcp://10.0.1.23:2375`) reveals deployment topology to every scraper
    that reads the metrics endpoint. Across a fleet with a shared
    Prometheus this is multi-tenant leakage: every tenant's socket layout
    ends up in a shared datastore.
    We emit a stable short-hash of the raw value instead. Operators who
    need per-instance breakdown should set an external `instance` label
    at scrape time via Prometheus relabel config.
    """
    import hashlib

    if not raw:
        return "unset"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    return f"h_{digest[:12]}"


@router.get("/api/system/metrics", dependencies=AUTH, tags=["system"])
@secure_route.read(RATE.READ)
def system_metrics(request: Request, client=Depends(docker_client.docker_client_dep)) -> PlainTextResponse:
    """Prometheus text-format metrics snapshot.

    Deployments can wire this into Google Cloud Managed Service for Prometheus,
    vanilla Prometheus, or Datadog's Prometheus check. Gauges only — counters
    would need persistent state we don't keep. Memory, CPU, disk, and container
    counts are all derived from client.info()/df() so one HTTP call gets a
    consistent snapshot without multiple Docker round-trips drifting.
    """
    info = safe_docker_call(client.info)
    df = safe_docker_call(client.df)

    # Docker returns Size / SizeRw / UsageData.Size as `null` (not 0)
    # for unpopulated entries — build-cache with no materialised layer,
    # containers that haven't written past the image layer, etc.
    # `.get("Size", 0)` returns that explicit null, not the default,
    # so downstream sum() crashes or reports 0 depending on call path.
    # Coerce with `or 0` to treat both missing-key and null-value as 0.
    def _b(v):
        return v or 0

    image_bytes = sum(_b(i.get("Size")) for i in (df.get("Images") or []))
    container_bytes = sum(_b(c.get("SizeRw")) for c in (df.get("Containers") or []))
    volume_bytes = sum(_b((v.get("UsageData") or {}).get("Size")) for v in (df.get("Volumes") or []))
    build_cache_bytes = sum(_b(b.get("Size")) for b in (df.get("BuildCache") or []))

    uptime = int(time.time() - config.APP_START_WALL)
    # Hashed docker_host label — see `_docker_host_label` for why the raw
    # path is never emitted (multi-tenant scraper topology leak).
    docker_host_label = _escape_prom_label(_docker_host_label(config._cfg.docker_host or ""))

    lines = [
        "# HELP skiff_uptime_seconds How long the SKIFF process has been running.",
        "# TYPE skiff_uptime_seconds gauge",
        f"skiff_uptime_seconds {uptime}",
        "# HELP skiff_containers_total Total containers on the managed engine (running + stopped + paused).",
        "# TYPE skiff_containers_total gauge",
        f'skiff_containers_total{{docker_host="{docker_host_label}"}} {info.get("Containers", 0)}',
        "# HELP skiff_containers_running Currently running containers.",
        "# TYPE skiff_containers_running gauge",
        f'skiff_containers_running{{docker_host="{docker_host_label}"}} {info.get("ContainersRunning", 0)}',
        "# HELP skiff_containers_paused Paused containers.",
        "# TYPE skiff_containers_paused gauge",
        f'skiff_containers_paused{{docker_host="{docker_host_label}"}} {info.get("ContainersPaused", 0)}',
        "# HELP skiff_containers_stopped Stopped containers.",
        "# TYPE skiff_containers_stopped gauge",
        f'skiff_containers_stopped{{docker_host="{docker_host_label}"}} {info.get("ContainersStopped", 0)}',
        "# HELP skiff_images_total Total images on the managed engine.",
        "# TYPE skiff_images_total gauge",
        f'skiff_images_total{{docker_host="{docker_host_label}"}} {info.get("Images", 0)}',
        "# HELP skiff_engine_cpus Logical CPUs reported by the engine.",
        "# TYPE skiff_engine_cpus gauge",
        f"skiff_engine_cpus {info.get('NCPU', 0)}",
        "# HELP skiff_engine_memory_bytes Total RAM reported by the engine.",
        "# TYPE skiff_engine_memory_bytes gauge",
        f"skiff_engine_memory_bytes {info.get('MemTotal', 0)}",
        "# HELP skiff_disk_images_bytes Bytes consumed by pulled images.",
        "# TYPE skiff_disk_images_bytes gauge",
        f"skiff_disk_images_bytes {image_bytes}",
        "# HELP skiff_disk_containers_bytes Bytes consumed by container writable layers.",
        "# TYPE skiff_disk_containers_bytes gauge",
        f"skiff_disk_containers_bytes {container_bytes}",
        "# HELP skiff_disk_volumes_bytes Bytes consumed by named volumes.",
        "# TYPE skiff_disk_volumes_bytes gauge",
        f"skiff_disk_volumes_bytes {volume_bytes}",
        "# HELP skiff_disk_build_cache_bytes Bytes consumed by the BuildKit cache.",
        "# TYPE skiff_disk_build_cache_bytes gauge",
        f"skiff_disk_build_cache_bytes {build_cache_bytes}",
    ]
    body = "\n".join(lines) + "\n"
    # Per Prometheus exposition format, the text version 0.0.4 is still current
    # as of 2026 and is what Cloud Managed Prometheus / Datadog expect.
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/system/df", dependencies=AUTH, tags=["system"])
@secure_route.read(RATE.AUTH_SENSITIVE)  # df is expensive — rate-limit low
async def system_disk_usage(request: Request, client=Depends(docker_client.docker_client_dep)) -> dict:
    """Return disk usage breakdown for images, containers, volumes, and build cache."""
    # `client.df` walks every image / container / volume / build-cache
    # entry and can take tens of seconds on large hosts. Cap with
    # DF_TIMEOUT so a stuck daemon doesn't hold a request worker
    # indefinitely — the rate-limit tier (AUTH_SENSITIVE) is already low,
    # but without a per-call cap a single slow df pins the worker.
    import asyncio as _asyncio

    try:
        df = await _asyncio.wait_for(
            _asyncio.get_running_loop().run_in_executor(None, safe_docker_call, client.df),
            timeout=config.DF_TIMEOUT,
        )
    except TimeoutError as exc:
        raise http_error(
            "system.docker_unreachable",
            message=f"/api/system/df exceeded {config.DF_TIMEOUT}s",
        ) from exc
    images = df.get("Images") or []
    containers = df.get("Containers") or []
    volumes = df.get("Volumes") or []
    build_cache = df.get("BuildCache") or []

    # Docker returns SizeRw / Size as `null` (not 0) for entries it hasn't
    # computed — SizeRw is null on containers that haven't written to
    # their RW layer beyond the initial image, Size is null on build-cache
    # entries without a materialised layer. `.get("key", 0)` returns the
    # explicit null, not the default, so the downstream sum() would crash.
    # Coerce with `or 0` to handle both missing-key and null-value cases.
    def _b(v):
        return v or 0

    image_size = sum(_b(i.get("Size")) for i in images)
    image_reclaimable = sum(_b(i.get("Size")) for i in images if i.get("Containers", 0) == 0)
    container_size = sum(_b(c.get("SizeRw")) for c in containers)
    volume_size = sum(_b((v.get("UsageData") or {}).get("Size")) for v in volumes)
    volume_reclaimable = sum(
        _b((v.get("UsageData") or {}).get("Size"))
        for v in volumes
        if (v.get("UsageData") or {}).get("RefCount", 0) == 0
    )
    build_cache_size = sum(_b(b.get("Size")) for b in build_cache)
    build_cache_reclaimable = sum(_b(b.get("Size")) for b in build_cache if not b.get("InUse", False))
    total = image_size + container_size + volume_size + build_cache_size
    return {
        "images_mb": round(image_size / 1024 / 1024, 1),
        "images_reclaimable_mb": round(image_reclaimable / 1024 / 1024, 1),
        "images_count": len(images),
        "containers_mb": round(container_size / 1024 / 1024, 1),
        "containers_count": len(containers),
        "volumes_mb": round(volume_size / 1024 / 1024, 1),
        "volumes_reclaimable_mb": round(volume_reclaimable / 1024 / 1024, 1),
        "volumes_count": len(volumes),
        "build_cache_mb": round(build_cache_size / 1024 / 1024, 1),
        "build_cache_reclaimable_mb": round(build_cache_reclaimable / 1024 / 1024, 1),
        "total_mb": round(total / 1024 / 1024, 1),
    }


# ── Prune ──────────────────────────────────────────────────
# `log` is initialised at module top for use by the config-knob PUT handler
# above; same instance re-used here.


@router.get("/api/system/overview", dependencies=AUTH, tags=["system"])
@secure_route.read(RATE.READ)
def system_overview(request: Request, client=Depends(docker_client.docker_client_dep)) -> dict:
    """Aggregated counts + recent events — powers the Dashboard home page.

    One call returns everything the dashboard needs so the page loads in
    a single round-trip: per-state container counts, image count, volume
    count, network count, disk usage, and recent events. Every count is
    best-effort — a Docker failure on one sub-query degrades gracefully
    to `null` instead of failing the whole response."""
    import time

    def _safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    def _list_all_containers():
        return client.containers.list(all=True)

    containers = _safe(_list_all_containers, []) or []
    running = sum(1 for c in containers if c.status == "running")
    paused = sum(1 for c in containers if c.status == "paused")
    exited = sum(1 for c in containers if c.status in ("exited", "dead"))
    created = sum(1 for c in containers if c.status == "created")

    images = _safe(client.images.list, []) or []
    volumes = _safe(client.volumes.list, []) or []
    networks = _safe(client.networks.list, []) or []

    df = _safe(client.df, {}) or {}
    layers_mb = 0
    for img in df.get("Images") or []:
        layers_mb += (img.get("Size") or 0) / 1024 / 1024

    now = int(time.time())
    events: list[dict] = []
    try:
        gen = client.events(since=now - 300, until=now, decode=True)
        for evt in gen:
            events.append(
                {
                    "time": evt.get("time") or evt.get("timeNano", 0) // 10**9,
                    "type": evt.get("Type") or "",
                    "action": evt.get("Action") or "",
                    "actor_id": (evt.get("Actor") or {}).get("ID", "")[:12],
                    "actor_name": ((evt.get("Actor") or {}).get("Attributes") or {}).get("name", "")[:120],
                }
            )
            if len(events) >= 25:
                break
    except Exception:
        pass

    return {
        "containers": {
            "total": len(containers),
            "running": running,
            "paused": paused,
            "exited": exited,
            "created": created,
        },
        "images": {"total": len(images), "disk_mb": round(layers_mb, 1)},
        "volumes": {"total": len(volumes)},
        "networks": {"total": len(networks)},
        "recent_events": events,
    }


@router.get("/api/system/events", dependencies=AUTH, tags=["system"])
@secure_route.read(RATE.READ)
def system_events(
    request: Request,
    since_secs: int = 60,
    limit: int = 200,
    client=Depends(docker_client.docker_client_dep),
) -> dict:
    """Return recent Docker engine events.

    Calls `docker events --since <now-since_secs>s --until <now>` via the
    SDK's `client.events(since=..., until=..., decode=True)` generator and
    drains it with a hard cap (`limit`, bounded at 500). Polling-based
    rather than a streaming WS because:

      - one HTTP GET is easier for a simple live viewer (re-poll every Ns)
      - no WS cleanup / reconnect logic — matches the audit-log pattern
      - every event is small (<1 KB); 200 events = ~50 KB response
    """
    import time

    since_secs = max(1, min(int(since_secs), 3600))
    limit = max(1, min(int(limit), 500))
    now = int(time.time())
    events: list[dict] = []
    try:
        # `client.events` returns a generator; with `until` set it terminates
        # once events are drained instead of streaming forever.
        gen = client.events(since=now - since_secs, until=now, decode=True)
        for evt in gen:
            events.append(
                {
                    "time": evt.get("time") or evt.get("timeNano", 0) // 10**9,
                    "type": evt.get("Type") or "",
                    "action": evt.get("Action") or "",
                    "actor_id": (evt.get("Actor") or {}).get("ID", "")[:12],
                    "actor_attributes": {
                        # Limit labels to short common keys — the raw
                        # Attributes dict can contain very long image
                        # digests. Keep the useful ones for an operator:
                        k: str(v)[:120]
                        for k, v in ((evt.get("Actor") or {}).get("Attributes") or {}).items()
                        if k in {"name", "image", "exitCode", "signal", "container"}
                    },
                }
            )
            if len(events) >= limit:
                break
    except Exception as exc:
        # Docker may return events in an unexpected shape on some hosts;
        # don't fail the whole request — return what we've got.
        log.warning("system.events_failed", error=str(exc))
    return {"since_secs": since_secs, "count": len(events), "events": events}


def _do_system_prune(client) -> dict:
    """Run the three Docker prune calls + return counts + reclaimed bytes.

    Pulled out so both the immediate-fire path and the undo-queued path
    run the exact same sequence.
    """
    containers = safe_docker_call(client.containers.prune)
    images = safe_docker_call(client.images.prune)
    networks = safe_docker_call(client.networks.prune)
    log.info(
        "system.pruned",
        containers=len(containers.get("ContainersDeleted") or []),
        images=len(images.get("ImagesDeleted") or []),
        networks=len(networks.get("NetworksDeleted") or []),
    )
    return {
        "containers_deleted": len(containers.get("ContainersDeleted") or []),
        "images_deleted": len(images.get("ImagesDeleted") or []),
        "networks_deleted": len(networks.get("NetworksDeleted") or []),
        "space_reclaimed_mb": round(
            (containers.get("SpaceReclaimed", 0) + images.get("SpaceReclaimed", 0)) / 1024 / 1024,
            1,
        ),
    }


@router.post("/api/system/prune", dependencies=AUTH, tags=["system"])
@secure_route.mutate(RATE.BURST)  # audit emitted inline (needs computed counts)
def system_prune(
    request: Request,
    undo: bool = True,
    client=Depends(docker_client.docker_client_dep),
):
    """Remove all stopped containers, dangling images, and unused networks.

    With `undo=true` (default) the three prune calls are queued for
    `UNDO_DELAY_SECS` seconds; the response carries an `undo_token` the
    caller can POST to cancel. Parallels every other destructive op in
    the app — a mistaken click on "Prune system" is no longer instantly
    irreversible. Scripts that want immediate execution can pass
    `undo=false` to preserve the legacy shape.
    """
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "system",
            "prune",
            _do_system_prune,
            client,
        )
        if token is not None:
            from skiff.contract.responses import UndoableResponse

            return UndoableResponse(
                undo_token=token,
                expires_in=config.UNDO_DELAY_SECS,
            )
        # Queue full — fall back to synchronous so the user's click still works.
    return _do_system_prune(client)


def _do_prune_build_cache(client) -> dict:
    result = safe_docker_call(client.api.prune_builds)
    space = result.get("SpaceReclaimed", 0)
    log.info("build_cache.pruned", space_mb=round(space / 1024 / 1024, 1))
    return {"space_reclaimed_mb": round(space / 1024 / 1024, 1)}


@router.post("/api/system/prune-build-cache", dependencies=AUTH, tags=["system"])
@secure_route.mutate(RATE.BURST)
def prune_build_cache(
    request: Request,
    undo: bool = True,
    client=Depends(docker_client.docker_client_dep),
):
    """Clear the Docker build cache. Default queues the op so a misclick
    is reversible within the undo window; `undo=false` fires now. Cache
    rebuilds automatically so the data-loss risk is low — but an ongoing
    build could still hit the "cache miss" wall unnecessarily. Safer by
    default, consistent with every other prune."""
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "system",
            "prune:build_cache",
            _do_prune_build_cache,
            client,
        )
        if token is not None:
            from skiff.contract.responses import UndoableResponse

            return UndoableResponse(
                undo_token=token,
                expires_in=config.UNDO_DELAY_SECS,
            )
    return _do_prune_build_cache(client)


# ── Frontend (SPA + MIT license) ──────────────────────────


@router.get("/", include_in_schema=False)
async def index() -> Response:
    """Serve the SPA frontend."""
    return Response(content=config._INDEX_HTML, media_type="text/html")


@router.get("/LICENSE", include_in_schema=False)
def license_file() -> FileResponse:
    """Serve the MIT LICENSE file."""
    return FileResponse(config._LICENSE_FILE, media_type="text/plain")
