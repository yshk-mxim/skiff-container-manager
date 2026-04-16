# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Structured logging, audit log handler, and ASGI middleware.

Must be imported by skiff.app BEFORE any other skiff module so that
structlog is configured before any logger is created.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import re
import sys
import time

import structlog
from starlette.datastructures import MutableHeaders

from skiff.auth import _constant_time_compare
from skiff.config import (
    _CSP,
    _GCP_LOG_NAME,
    _GCP_PROJECT,
    _PERMISSIONS_POLICY,
    AUDIT_BACKUP_COUNT,
    AUDIT_LOG_PATH,
    AUDIT_MAX_BYTES,
    HSTS_HEADER,
    TOKEN_AUDIT_SUFFIX_LEN,
    _cfg,
)

# ── Audit log file handler ─────────────────────────────────

def _make_audit_handler() -> logging.handlers.RotatingFileHandler | None:
    """Return a RotatingFileHandler for the audit log, or None if the path is not writable."""
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            AUDIT_LOG_PATH,
            maxBytes=AUDIT_MAX_BYTES,
            backupCount=AUDIT_BACKUP_COUNT,
            encoding="utf-8",
        )
        return handler
    except OSError as exc:
        print(  # noqa: T201
            f"WARNING: audit log path {AUDIT_LOG_PATH} is not writable ({exc}). Audit log disabled.",
            flush=True,
        )
        return None


_audit_handler = _make_audit_handler()


# ── Optional GCP Cloud Logging sink ───────────────────────
# Activated when GOOGLE_CLOUD_PROJECT env var is set and google-cloud-logging is installed.
# Dual-writes every structured log entry to Cloud Logging alongside local file + stdout.
_gcp_logger = None
if _GCP_PROJECT:
    try:
        import google.cloud.logging as _gcl  # type: ignore[import]
        _gcp_client = _gcl.Client(project=_GCP_PROJECT)
        _gcp_logger = _gcp_client.logger(_GCP_LOG_NAME)
        print(  # noqa: T201
            f"INFO: GCP Cloud Logging sink active → project={_GCP_PROJECT} log={_GCP_LOG_NAME}",
            flush=True,
        )
    except ImportError:
        pass
    except Exception as _gcp_exc:
        print(f"WARNING: GCP Cloud Logging init failed: {_gcp_exc}", flush=True)  # noqa: T201


# ── structlog processors ───────────────────────────────────

def _level_to_severity(logger, method_name, event_dict):
    """Map Python log levels to Cloud Logging severity field."""
    level = event_dict.pop("level", method_name)
    severity_map = {
        "debug": "DEBUG", "info": "INFO", "warning": "WARNING",
        "error": "ERROR", "critical": "CRITICAL",
    }
    event_dict["severity"] = severity_map.get(level, "DEFAULT")
    return event_dict


def _audit_file_sink(_, __, event_dict):
    """Write every log line to the rotating audit JSONL file and optionally GCP Cloud Logging."""
    if _audit_handler is not None:
        line = json.dumps(event_dict) + "\n"
        record = logging.makeLogRecord({"msg": line})
        try:
            _audit_handler.emit(record)
        except OSError:
            pass
    if _gcp_logger is not None:
        try:
            severity = event_dict.get("severity", "DEFAULT")
            _gcp_logger.log_struct(event_dict, severity=severity)
        except Exception:
            pass
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _level_to_severity,
        structlog.processors.TimeStamper(fmt="iso"),
        _audit_file_sink,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(sys.stderr),
)

log = structlog.get_logger(__name__)


# ── Loop lag monitor ───────────────────────────────────────


async def _loop_lag_monitor() -> None:
    """Background task: warn to stderr when event loop is blocked > 300ms."""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        lag = time.monotonic() - t0 - 0.1
        if lag > 0.3:
            import traceback
            frames = sys._current_frames()
            frame_info = [
                f"Thread {tid}:\n{''.join(traceback.format_stack(frame))}"
                for tid, frame in frames.items()
            ]
            print(  # noqa: T201 — last-resort diagnostic; structlog itself may be blocked
                f"[LOOP_LAG] event loop blocked {lag*1000:.0f}ms\n" + "\n".join(frame_info),
                file=sys.stderr, flush=True,
            )


# ── Audit event classification ─────────────────────────────
# Maps (HTTP method, path prefix) → semantic event_type label for SIEM ingestion.
# More-specific prefixes must appear before less-specific ones.
_AUDIT_EVENT_MAP: list[tuple[str, str, str]] = [
    # Containers — more-specific entries MUST come before less-specific ones
    ("POST",   "/api/containers/run",     "container.run"),
    ("POST",   "/api/containers/",        "container.action"),
    ("DELETE", "/api/containers/",        "container.removed"),
    # Logs / exec WebSocket
    ("GET",    "/ws/logs/",               "container.logs_stream"),
    ("GET",    "/ws/exec/",               "container.exec_session"),
    # Images
    ("POST",   "/api/images/pull",        "image.pull"),
    ("POST",   "/api/images/push",        "image.push"),
    ("POST",   "/api/images/tag",         "image.tag"),
    ("DELETE", "/api/images/",            "image.removed"),
    ("GET",    "/api/images",             "image.list"),
    # Volumes
    ("POST",   "/api/volumes",            "volume.created"),
    ("DELETE", "/api/volumes/",           "volume.removed"),
    ("POST",   "/api/volumes/prune",      "volume.pruned"),
    # Networks
    ("POST",   "/api/networks",           "network.created"),
    ("DELETE", "/api/networks/",          "network.removed"),
    ("POST",   "/api/networks/prune",     "network.pruned"),
    # Compose
    ("POST",   "/api/compose/up",         "compose.deployed"),
    ("DELETE", "/api/compose/",           "compose.torn_down"),
    # System
    ("POST",   "/api/system/prune",       "system.pruned"),
    ("GET",    "/api/system/audit-log",   "audit.log_read"),
    # Setup
    ("POST",   "/api/setup/tunnel",       "setup.tunnel_start"),
    ("DELETE", "/api/setup/tunnel",       "setup.tunnel_stop"),
    ("POST",   "/api/setup",              "setup.configured"),
    # Catch-all
    ("*",      "/api/",                   "api.request"),
]

_RESOURCE_PATH_RE = re.compile(
    r"/api/(?P<rtype>containers|images|volumes|networks|compose)/(?P<rid>[^/]+)"
)


def _classify_event(method: str, path: str, status: int) -> tuple[str, str, str]:
    """Return (event_type, resource_type, resource_id) for an API request."""
    if status in {401, 403}:
        return "auth.denied", "", ""
    if status == 429:
        return "rate_limit.exceeded", "", ""
    for m, prefix, label in _AUDIT_EVENT_MAP:
        if (m in ("*", method)) and path.startswith(prefix):
            event_type = label
            break
    else:
        event_type = "api.request"
    m2 = _RESOURCE_PATH_RE.match(path)
    resource_type = m2.group("rtype").rstrip("s") if m2 else ""
    resource_id = m2.group("rid") if m2 else ""
    return event_type, resource_type, resource_id


# ── ASGI Middlewares ───────────────────────────────────────

class AuditLogMiddleware:
    """Log all authenticated API requests for governance compliance.

    Pure ASGI middleware (no BaseHTTPMiddleware) to avoid the known Starlette deadlock
    when many clients disconnect simultaneously during the http.response.start phase.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        status_code = 500
        request_headers = dict(scope.get("headers", []))

        async def auditing_send(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, auditing_send)
        finally:
            auth_header = request_headers.get(b"authorization", b"").decode(errors="replace")
            token_hint = ""
            token_suffix = ""
            if auth_header.startswith("Bearer ") and _cfg.api_token:
                provided = auth_header[7:]
                if _constant_time_compare(provided, _cfg.api_token):
                    token_hint = "authenticated"
                    token_suffix = (
                        provided[-TOKEN_AUDIT_SUFFIX_LEN:]
                        if len(provided) >= TOKEN_AUDIT_SUFFIX_LEN
                        else provided
                    )
                else:
                    token_hint = "invalid"

            _raw_user = request_headers.get(b"x-forwarded-user", b"").decode(errors="replace")
            forwarded_user = re.sub(r"[^\x20-\x7E]", "", _raw_user)[:128]

            path = scope.get("path", "")
            method = scope.get("method", "")
            client = scope.get("client")
            remote = f"{client[0]}" if client else "unknown"

            event_type, resource_type, resource_id = _classify_event(method, path, status_code)

            level = "warning" if status_code in (401, 403, 429) else (
                "error" if status_code >= 500 else "info"
            )
            extra: dict = {}
            if token_suffix:
                extra["token_suffix"] = token_suffix
            if forwarded_user:
                extra["user"] = forwarded_user
            if resource_type:
                extra["resource_type"] = resource_type
            if resource_id:
                extra["resource_id"] = resource_id

            getattr(log, level)(
                "audit.api_access",
                event_type=event_type,
                method=method,
                path=path,
                status=status_code,
                remote=remote,
                auth=token_hint or ("none" if not auth_header else "present"),
                **extra,
            )


class SecurityHeadersMiddleware:
    """Inject security headers on every HTTP response.

    Pure ASGI middleware to avoid BaseHTTPMiddleware deadlocks on concurrent disconnects.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        req_headers = dict(scope.get("headers", []))
        scheme = req_headers.get(b"x-forwarded-proto", b"").decode() or scope.get("scheme", "http")

        async def security_send(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Content-Security-Policy"] = _CSP
                headers["Permissions-Policy"] = _PERMISSIONS_POLICY
                if scheme == "https":
                    headers["Strict-Transport-Security"] = HSTS_HEADER
            await send(message)

        await self.app(scope, receive, security_send)
