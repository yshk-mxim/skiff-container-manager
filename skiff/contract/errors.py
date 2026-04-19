# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Error-code catalogue.

Every 4xx/5xx response should carry a stable machine-readable `code` so the
client can react without parsing English. Human messages still travel in
`message`; the catalogue just pins the vocabulary.

Status quo (pre-migration): routers raise `HTTPException(400, "some string")`
with no code. The client switches on status_code alone, which is coarse.

Target: `raise http_error("container.name_taken", name=name)` → produces
`HTTPException(409, detail={"code": "container.name_taken",
"message": "name 'foo' is already in use", "help": <optional link>})`.

Migration is incremental: keep the old `HTTPException(400, "literal")`
paths working; route new code through `http_error()`. A lint can nudge
toward migration over time.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

# Request-scoped error code set by `http_error()`. The audit middleware
# reads this to distinguish `auth.reviewer_read_only` from the catch-all
# `auth.denied` so SIEM can separate reviewer noise from stolen-token
# attempts. contextvars is per-task (asyncio) AND per-thread, so this is
# safe under sync + async handlers.
_request_error_code: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skiff_request_error_code",
    default="",
)


def current_error_code() -> str:
    """Return the error code raised by the current request, or ""."""
    return _request_error_code.get()


@dataclass(frozen=True)
class _ErrorSpec:
    """Declaration of one error code.

    status:  HTTP status returned (4xx / 5xx).
    message: Human-readable default. Supports {placeholder} interpolation
             from the kwargs passed to http_error().
    help:    Optional URL or docs/ relative path the client can deep-link.
    """

    status: int
    message: str
    help: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue — every reachable error code lives here.
#
# Keys are dotted: <domain>.<short_code>. Domains match router package names.
# Adding a new error code:
#   1. Pick a stable dotted name.
#   2. Choose the narrowest HTTP status that fits.
#   3. Write a message template; keep it short and non-leaky.
#   4. Optionally point `help` at a docs/ page.
# ─────────────────────────────────────────────────────────────────────────────
_ERRORS: dict[str, _ErrorSpec] = {
    # ── Auth ──────────────────────────────────────────────────────────────
    "auth.missing_token": _ErrorSpec(401, "authentication required"),
    "auth.invalid_token": _ErrorSpec(401, "invalid token"),
    "auth.session_expired": _ErrorSpec(401, "session expired; please sign in again"),
    "auth.csrf_missing": _ErrorSpec(403, "missing X-Requested-With header"),
    "auth.csrf_invalid": _ErrorSpec(403, "invalid X-Requested-With header value"),
    "auth.rate_limited": _ErrorSpec(429, "too many requests"),
    "auth.setup_locked": _ErrorSpec(429, "setup endpoint is temporarily locked"),
    "auth.reviewer_read_only": _ErrorSpec(
        403,
        "reviewer profile is read-only; mutations are disabled",
        help="docs/dev/personas.md",
    ),
    # ── Setup ─────────────────────────────────────────────────────────────
    "setup.window_expired": _ErrorSpec(403, "setup window has expired; restart the server to re-enable"),
    "setup.already_configured": _ErrorSpec(409, "setup is already complete"),
    "validation.body_timeout": _ErrorSpec(
        408,
        "request body not received within the allowed window",
        help=(
            "Raise `BODY_READ_TIMEOUT_SECS` on the server OR have your "
            "client send the full body in one shot — the timeout is a "
            "slow-POST defence, not a per-operation budget."
        ),
    ),
    "validation.body_too_large": _ErrorSpec(
        413,
        "request body exceeds size cap",
        help="Lower the payload or raise `MAX_BODY_BYTES` server-side.",
    ),
    "setup.token_bad_charset": _ErrorSpec(
        400,
        "api_token contains characters HTTP Authorization can't carry",
        help=(
            "Use `openssl rand -hex 32` (or similar). Allowed: letters, "
            "digits, and `. _ ~ + / = -`. Unicode / bidi / control chars "
            "would travel in the HTTP header but can't be sent back on "
            "subsequent requests, silently locking the operator out."
        ),
    ),
    # ── Container ─────────────────────────────────────────────────────────
    "container.not_found": _ErrorSpec(404, "container {id} not found"),
    "container.name_taken": _ErrorSpec(409, "name '{name}' is already in use"),
    "container.bad_id": _ErrorSpec(400, "invalid container id"),
    "container.limit_reached": _ErrorSpec(400, "container limit ({limit}) reached"),
    "container.bad_signal": _ErrorSpec(400, "unsupported signal"),
    # ── Image ─────────────────────────────────────────────────────────────
    "image.not_found": _ErrorSpec(404, "image not found"),
    "image.bad_id": _ErrorSpec(400, "invalid image id"),
    "image.registry_blocked": _ErrorSpec(400, "registry '{registry}' is not in the allowlist"),
    "image.pull_failed": _ErrorSpec(400, "image pull failed"),
    "image.push_failed": _ErrorSpec(400, "image push failed"),
    "image.prune_failed": _ErrorSpec(400, "image prune failed"),
    # ── Volume ────────────────────────────────────────────────────────────
    "volume.not_found": _ErrorSpec(404, "volume not found"),
    "volume.bad_name": _ErrorSpec(400, "invalid volume name"),
    "volume.bad_driver": _ErrorSpec(400, "unsupported volume driver"),
    "volume.bad_labels": _ErrorSpec(400, "invalid volume labels"),
    "volume.bad_driver_opts": _ErrorSpec(400, "invalid volume driver options"),
    "volume.in_use": _ErrorSpec(409, "volume is in use"),
    # ── Network ───────────────────────────────────────────────────────────
    "network.not_found": _ErrorSpec(404, "network not found"),
    "network.bad_name": _ErrorSpec(400, "invalid network name"),
    "network.bad_driver": _ErrorSpec(400, "unsupported network driver"),
    "network.bad_labels": _ErrorSpec(400, "invalid network labels"),
    "network.bad_subnet": _ErrorSpec(400, "invalid subnet"),
    "network.bad_gateway": _ErrorSpec(400, "invalid gateway"),
    "network.builtin_protected": _ErrorSpec(400, "built-in network cannot be removed"),
    # ── Compose ───────────────────────────────────────────────────────────
    "compose.bad_yaml": _ErrorSpec(400, "invalid compose YAML"),
    "compose.too_large": _ErrorSpec(400, "compose file exceeds size limit"),
    "compose.forbidden_key": _ErrorSpec(400, "compose key '{key}' is not allowed"),
    "compose.deploy_failed": _ErrorSpec(400, "compose up failed"),
    "compose.teardown_failed": _ErrorSpec(400, "compose down failed"),
    "compose.not_found": _ErrorSpec(404, "compose project not found"),
    "compose.file_missing": _ErrorSpec(400, "no compose file uploaded and no existing file found for this project"),
    # ── System ────────────────────────────────────────────────────────────
    "system.docker_unreachable": _ErrorSpec(503, "container engine unreachable"),
    "system.tunnel_failed": _ErrorSpec(502, "tunnel did not come up"),
    "system.undo_not_found": _ErrorSpec(404, "undo token not found or expired"),
    "system.debug_disabled": _ErrorSpec(
        403,
        "debug endpoint disabled — set SKIFF_DEBUG_THREADS=1 on the server to enable",
    ),
    # ── Validation (catch-all) ────────────────────────────────────────────
    "validation.bad_input": _ErrorSpec(400, "invalid input"),
    "validation.path_traversal": _ErrorSpec(400, "path traversal attempt rejected"),
    "validation.bad_env": _ErrorSpec(400, "environment variable must be KEY=VALUE"),
    "validation.bad_restart_policy": _ErrorSpec(400, "unsupported restart policy"),
    "validation.bad_memory": _ErrorSpec(400, "invalid memory quantity"),
    "validation.bad_cpu": _ErrorSpec(400, "invalid cpu quantity"),
    # ── R4 additions — codes promoted from hand-written HTTPException strings
    # Prefer adding a code here over passing `message=` to http_error() so
    # the catalogue stays enumerable for SIEM / client-switch logic.
    "container.port_count_exceeds_cap": _ErrorSpec(400, "too many port mappings (max {limit})"),
    "container.port_host_privileged": _ErrorSpec(400, "host port {port} is privileged (< {threshold})"),
    "container.port_format": _ErrorSpec(400, "invalid port format"),
    "container.label_count_exceeds_cap": _ErrorSpec(400, "too many labels (max {limit})"),
    "container.label_bad": _ErrorSpec(400, "invalid label"),
    "container.command_too_long": _ErrorSpec(400, "command too long (max {limit} chars)"),
    "container.signal_bad": _ErrorSpec(400, "unsupported signal"),
    "container.volume_host_path_blocked": _ErrorSpec(400, "host path mounts are not allowed — use named volumes only"),
    "container.volume_format": _ErrorSpec(400, "invalid volume spec"),
    "container.update_no_fields": _ErrorSpec(400, "no updatable fields provided"),
    "container.memory_below_minimum": _ErrorSpec(400, "memory must be >= {minimum} bytes (Docker minimum)"),
    "container.memory_above_cap": _ErrorSpec(400, "memory exceeds cap of {cap}"),
    "container.memory_uncap_unsupported": _ErrorSpec(
        400,
        "Docker Engine does not support removing a memory cap on a running "
        "container; recreate the container with no memory limit instead.",
        help="docs/api-reference.md#post-apicontainersidupdate",
    ),
    "container.cpu_above_cap": _ErrorSpec(400, "cpus exceeds cap of {cap}"),
    "container.cpu_shares_bad": _ErrorSpec(400, "cpu_shares must be an integer in [2, 1024]"),
    "container.pids_limit_bad": _ErrorSpec(400, "pids_limit must be an integer in [1, {cap}]"),
    "container.restart_policy_shape": _ErrorSpec(400, "restart_policy must be an object"),
    "container.restart_retry_bad": _ErrorSpec(400, "MaximumRetryCount must be an integer in [0, {cap}]"),
    "setup.scheme_bad": _ErrorSpec(400, "docker_host must use unix://, tcp://, or npipe:// scheme"),
    "setup.tcp_host_bad": _ErrorSpec(400, "tcp:// docker_host must specify an IP address, not a hostname"),
    "setup.tcp_port_bad": _ErrorSpec(400, "tcp:// docker_host must include a valid port"),
    "setup.token_too_short": _ErrorSpec(400, "api_token must be at least {minimum} characters"),
    "setup.docker_host_required": _ErrorSpec(400, "docker_host is required"),
    "setup.ssh_target_bad": _ErrorSpec(400, "ssh_target must be user@host"),
    "setup.env_managed": _ErrorSpec(403, "server is configured via environment variables — setup endpoint disabled"),
    "setup.already_done": _ErrorSpec(403, "already configured"),
    "setup.probe_disabled": _ErrorSpec(403, "probe endpoint disabled after setup completes"),
    "auth.token_unchanged": _ErrorSpec(400, "new_token is identical to the current token"),
    "auth.env_managed": _ErrorSpec(
        403, "API_TOKEN is managed via environment variable — update .env and restart to rotate"
    ),
    "auth.reset_env_managed": _ErrorSpec(403, "cannot reset: server is configured via environment variables"),
    "ws.connections_exhausted": _ErrorSpec(429, "too many WebSocket connections from this IP"),
    "compose.project_dir_create_failed": _ErrorSpec(500, "failed to create project directory"),
    "compose.timeout": _ErrorSpec(504, "compose operation timed out"),
    "image.registry_search_failed": _ErrorSpec(502, "registry search failed"),
    "image.tag_fetch_failed": _ErrorSpec(502, "tag fetch failed"),
    "image.pull_timed_out": _ErrorSpec(504, "image pull timed out"),
    "image.push_timed_out": _ErrorSpec(504, "image push timed out"),
    "container.stats_timeout": _ErrorSpec(504, "stats call timed out"),
    "system.route_not_found": _ErrorSpec(
        404, "no route matches this path + method", help="Check `docs/api-reference.md` or GET /api/openapi.json."
    ),
    "system.method_not_allowed": _ErrorSpec(
        405,
        "this route does not accept that HTTP method",
        help="Check the `Allow` header on the response for accepted methods.",
    ),
    "tunnel.not_configured": _ErrorSpec(
        404, "no managed tunnel configured", help="The server has no stored SSH target. Run setup again via the wizard."
    ),
    "tunnel.manual_reconnect_required": _ErrorSpec(
        503,
        "Docker host is unreachable. The tunnel was not opened by SKIFF "
        "so it can't be re-opened server-side — re-run your `ssh -fNL …` "
        "command (or equivalent) to restore the socket.",
        help=(
            "SKIFF only auto-reconnects tunnels it opened itself (via the "
            "setup wizard). A manual `ssh -fNL` tunnel needs to be "
            "re-opened by the operator. The DOCKER_HOST socket path is "
            "included in the response so you can pass it back to ssh."
        ),
    ),
    "tunnel.already_connected": _ErrorSpec(
        409,
        "Docker host is already reachable — no reconnect needed",
        help=(
            "The socket is present and a Docker ping succeeded. "
            "The server-side Docker client was invalidated so the "
            "next request opens a fresh connection."
        ),
    ),
    # ── R4 final batch — auth / validators / docker_client migrations ─────
    "auth.not_configured": _ErrorSpec(503, "server not configured — set API_TOKEN before accessing this endpoint"),
    "container.conflict": _ErrorSpec(409, "container conflict (already started/stopped?)"),
    "container.op_failed": _ErrorSpec(400, "container operation failed"),
    "container.bad_name": _ErrorSpec(400, "invalid container name (alphanumeric, dots, hyphens, underscores)"),
    "validation.bad_project_name": _ErrorSpec(400, "invalid project name"),
    "validation.bad_image_name": _ErrorSpec(400, "invalid image name format"),
    "validation.bad_mount_target": _ErrorSpec(400, "volume mount target must be an absolute path"),
    "validation.mount_target_blocked": _ErrorSpec(400, "mount target {path!r} is not permitted"),
    "validation.bad_tmpfs_shape": _ErrorSpec(400, "tmpfs must be an object mapping paths to options"),
    "validation.tmpfs_too_many": _ErrorSpec(400, "too many tmpfs mounts (max {max_mounts})"),
    "validation.tmpfs_bad_path": _ErrorSpec(400, "invalid tmpfs path"),
    "validation.tmpfs_path_blocked": _ErrorSpec(400, "tmpfs on {path!r} is not permitted"),
    "validation.tmpfs_bad_options": _ErrorSpec(400, "invalid tmpfs options"),
    "validation.tmpfs_size_exceeds_cap": _ErrorSpec(
        400, "total tmpfs size {total_mb:.0f}MB exceeds cap ({max_total_mb}MB)"
    ),
    "compose.bad_services": _ErrorSpec(400, "invalid services section"),
    "compose.service_not_mapping": _ErrorSpec(400, "service '{svc_name}' must be a mapping"),
    "compose.service_forbidden_key": _ErrorSpec(
        400, "service '{svc_name}': '{key}' is not allowed for security reasons"
    ),
    "compose.service_bad_network_mode": _ErrorSpec(
        400, "service '{svc_name}': network_mode '{net_mode}' is not allowed"
    ),
    "compose.service_host_pid": _ErrorSpec(400, "service '{svc_name}': pid mode 'host' is not allowed"),
    "compose.service_bad_ipc": _ErrorSpec(400, "service '{svc_name}': ipc mode '{ipc_mode}' is not allowed"),
    "compose.service_host_volume": _ErrorSpec(400, "service '{svc_name}': host path mounts are not allowed"),
    # Generic 404 for safe_docker_call fallbacks where the caller hasn't
    # already mapped the resource type. Caller-specific codes
    # (container.not_found, volume.not_found, etc.) are preferred.
    "resource.not_found": _ErrorSpec(404, "resource not found"),
    "resource.in_use": _ErrorSpec(409, "resource is in use: {detail}"),
    # image.not_found / volume.not_found / network.not_found are
    # declared earlier in this file alongside the domain errors.
    "docker.sdk_error": _ErrorSpec(
        500,
        "Docker daemon returned {status}: {message}",
        help="docs/audit-events.md#docker-client-events",
    ),
    # `validation.body_too_large` + `validation.body_timeout` are
    # declared earlier in this file alongside the other request-shape
    # validation errors.
}


def http_error(
    code: str,
    *,
    message: str | None = None,
    extra: dict[str, Any] | None = None,
    status_override: int | None = None,
    **kwargs: Any,
) -> HTTPException:
    """Produce an HTTPException from the catalogue.

    Examples:
        raise http_error("container.not_found", id="abc123")
        raise http_error("validation.bad_input", message="Invalid volume name 'xyz'")
        raise http_error("system.tunnel_failed", extra={"help": help_str}, message=str(exc))

    - `kwargs` fill `{placeholder}` slots in the catalogue's message
       template.
    - `message=` overrides the template entirely for one-off cases
       where a unique human-facing string matters but no distinct code
       is warranted. The catalogue-declared `code` is still returned so
       clients can switch on it.
    - `extra=` adds extra keys to the response body (e.g. `help` from a
       classified exception), merged after the standard {code, message,
       help} fields.

    Unknown codes fall through as 500s with the literal code — a typo
    is loud but doesn't take the server down.
    """
    spec = _ERRORS.get(code)
    if spec is None:
        return HTTPException(
            status_code=500,
            detail={"code": "internal.unknown_error_code", "message": f"unknown error code: {code}"},
        )
    if message is not None:
        final_message = message
    elif kwargs:
        final_message = spec.message.format(**kwargs)
    else:
        final_message = spec.message
    body: dict[str, Any] = {"code": code, "message": final_message}
    if spec.help:
        body["help"] = spec.help
    if extra:
        body.update(extra)
    # Park the code on a contextvar so the audit middleware can read it
    # when classifying the response (e.g. auth.reviewer_read_only → the
    # audit row gets `auth.reviewer_denied` instead of `auth.denied`).
    _request_error_code.set(code)
    status = status_override if status_override is not None else spec.status
    # Clamp to a valid HTTP error status range. `_raise_docker_api_error`
    # forwards the Docker daemon's status via `status_override`; a hostile
    # or exotic daemon returning e.g. 700 would otherwise reach Starlette
    # and propagate a non-compliant status line. Default to 500 on
    # out-of-range rather than masking the bug — the operator still sees
    # an error envelope, just with a sane wire status.
    if not (400 <= status <= 599):
        status = 500
    return HTTPException(status_code=status, detail=body)


def known_codes() -> frozenset[str]:
    """Introspection helper for tests."""
    return frozenset(_ERRORS)


def spec_for(code: str) -> _ErrorSpec | None:
    """Return the spec for a code, or None if unknown. Testing helper."""
    return _ERRORS.get(code)


__all__ = ["http_error", "known_codes", "spec_for"]
