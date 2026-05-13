# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Structured logging, audit log handler, and ASGI middleware.

Must be imported by skiff.app BEFORE any other skiff module so that
structlog is configured before any logger is created.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import re
import sys
import time

import pydantic
import structlog
from starlette.datastructures import MutableHeaders

from skiff import config
from skiff.auth import constant_time_compare

# ── Audit log file handler ─────────────────────────────────


class _TightRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that chmods each log file to 0600.

    The audit log contains token suffixes, client IPs, and full structured
    event payloads; a 0644 default (inherited from process umask) lets any
    other local user read every auth failure, every container action, every
    rotation timing signal. We fix the mode on both the initial open and on
    every doRollover so rotated files are equally tight.
    """

    def _fix_mode(self, path: str | None = None) -> None:
        """chmod the given path (or self.baseFilename) to 0600, tolerating absence."""
        from pathlib import Path

        target = Path(path if path is not None else self.baseFilename)
        try:
            target.chmod(0o600)
        except OSError:
            pass

    def _open(self):  # type: ignore[override]
        fh = super()._open()
        self._fix_mode()
        return fh

    def doRollover(self) -> None:  # noqa: N802 — RotatingFileHandler API uses camelCase
        super().doRollover()
        self._fix_mode()
        from pathlib import Path

        base = Path(self.baseFilename)
        for p in base.parent.glob(f"{base.name}.*"):
            self._fix_mode(str(p))


def _make_audit_handler() -> logging.handlers.RotatingFileHandler | None:
    """Return a RotatingFileHandler for the audit log, or None if the path is not writable."""
    try:
        config.AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = _TightRotatingFileHandler(
            config.AUDIT_LOG_PATH,
            maxBytes=config.AUDIT_MAX_BYTES,
            backupCount=config.AUDIT_BACKUP_COUNT,
            encoding="utf-8",
        )
        return handler
    except OSError as exc:
        print(  # noqa: T201
            f"WARNING: audit log path {config.AUDIT_LOG_PATH} is not writable ({exc}). Audit log disabled.",
            flush=True,
        )
        return None


# ── uvicorn access-log filter: strip `token=` from URL path before emit ─────
# Clients that follow a `?token=…` WebSocket convention would otherwise
# leak the bearer into stderr / journald / syslog. The access log runs
# BEFORE SKIFF's own WS handler rejects such URLs, so we scrub at the
# logging layer as defence-in-depth (the route handler below also hard-
# rejects `token=` query params on WS upgrades).
_TOKEN_QS_RE = re.compile(r"([?&](?:token|api_token|bearer)=)[^\s&\"']+", re.IGNORECASE)


class _RedactQueryTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            return True
        if "token=" in msg.lower() or "api_token=" in msg.lower() or "bearer=" in msg.lower():
            # Rewrite both args-formatted and already-rendered forms.
            if record.args:
                record.args = tuple(
                    _TOKEN_QS_RE.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a for a in record.args
                )
            record.msg = _TOKEN_QS_RE.sub(r"\1[REDACTED]", str(record.msg))
        return True


def install_access_log_scrubber() -> None:
    """Attach the token-scrubber filter to uvicorn's access and error loggers.

    Called once from the app lifespan. Idempotent — attaching the same
    filter instance twice is harmless.
    """
    f = _RedactQueryTokenFilter()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        if not any(isinstance(x, _RedactQueryTokenFilter) for x in logger.filters):
            logger.addFilter(f)


_audit_handler = _make_audit_handler()


# ── Optional GCP Cloud Logging sink ───────────────────────
# Activated when GOOGLE_CLOUD_PROJECT env var is set and google-cloud-logging is installed.
# Dual-writes every structured log entry to Cloud Logging alongside local file + stdout.
_gcp_logger = None
if config._GCP_PROJECT:
    try:
        import google.cloud.logging as _gcl  # type: ignore[import]

        _gcp_client = _gcl.Client(project=config._GCP_PROJECT)
        _gcp_logger = _gcp_client.logger(config._GCP_LOG_NAME)
        print(  # noqa: T201
            f"INFO: GCP Cloud Logging sink active → project={config._GCP_PROJECT} log={config._GCP_LOG_NAME}",
            flush=True,
        )
    except ImportError:
        pass
    # Intentionally broad: google-cloud-logging has dozens of failure modes
    # (auth, network, quota, malformed project ID) and we MUST fall through
    # to the file-only sink rather than crash startup. The warning line is
    # the operator's signal to investigate.
    except Exception as _gcp_exc:
        print(f"WARNING: GCP Cloud Logging init failed: {_gcp_exc}", flush=True)  # noqa: T201


# ── structlog processors ───────────────────────────────────


def _level_to_severity(logger, method_name, event_dict):
    """Map Python log levels to Cloud Logging severity field."""
    level = event_dict.pop("level", method_name)
    severity_map = {
        "debug": "DEBUG",
        "info": "INFO",
        "warning": "WARNING",
        "error": "ERROR",
        "critical": "CRITICAL",
    }
    event_dict["severity"] = severity_map.get(level, "DEFAULT")
    return event_dict


def _audit_file_sink(_, __, event_dict):
    """Write every log line to the rotating audit JSONL file and optionally GCP Cloud Logging."""
    if _audit_handler is not None:
        # `RotatingFileHandler.emit()` appends its own terminator via the
        # Formatter. Adding our own `\n` here produced a blank line
        # between every record, which broke NDJSON parsers (Filebeat,
        # Fluent Bit, `jq -c`). One JSON object per line, no padding.
        line = json.dumps(event_dict)
        record = logging.makeLogRecord({"msg": line})
        try:
            _audit_handler.emit(record)
        except OSError:
            pass
    if _gcp_logger is not None:
        # Intentionally broad: runs in every log call site. A transient
        # Cloud Logging failure (quota, network blip, auth token refresh
        # race) must NOT block local file-sink writes or propagate into
        # request handlers. The local JSONL is authoritative; Cloud
        # Logging is a mirror.
        with contextlib.suppress(Exception):
            severity = event_dict.get("severity", "DEFAULT")
            _gcp_logger.log_struct(event_dict, severity=severity)
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


def _emit_loop_lag_warning(lag: float) -> None:
    """Dump every thread's stack to stderr. Separate so it can be unit-tested
    without the infinite monitor loop around it."""
    import traceback

    frames = sys._current_frames()
    frame_info = [f"Thread {tid}:\n{''.join(traceback.format_stack(frame))}" for tid, frame in frames.items()]
    print(  # noqa: T201 — last-resort diagnostic; structlog itself may be blocked
        f"[LOOP_LAG] event loop blocked {lag * 1000:.0f}ms\n" + "\n".join(frame_info),
        file=sys.stderr,
        flush=True,
    )


async def _loop_lag_monitor() -> None:
    """Background task: warn to stderr when event loop is blocked > 300ms."""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        lag = time.monotonic() - t0 - 0.1
        if lag > 0.3:
            _emit_loop_lag_warning(lag)


# ── Audit event classification ─────────────────────────────
# Map for routes `register_route_audit_events(app)` can't derive
# a name for — WebSockets bypass @secure_route, and a handful of reads
# deserve a named event_type. Every HTTP mutating route now carries
# `@secure_route.mutate(audit=...)`; the decorator-derived entries get
# prepended at startup, so there are no container/image/volume/network
# placeholder rows here. If a new WS or unusual GET route needs
# classification, add the row with the most-specific prefix first.
_AUDIT_EVENT_MAP: list[tuple[str, str, str]] = [
    # WebSockets — bypass @secure_route.mutate, so no decorator to read.
    ("GET", "/ws/logs/", "container.logs_stream"),
    ("GET", "/ws/exec/", "container.exec_session"),
    # Reads that deserve a named event_type (rather than the catch-all).
    ("GET", "/api/images", "image.list"),
    ("GET", "/api/system/audit-log", "audit.log_read"),
    # Mutations whose handlers emit audit inline (to carry computed
    # counts) instead of via the decorator. The handler already emits
    # `system.pruned` / `build_cache.pruned` with detail; this entry
    # just fixes the audit.api_access `event_type` on the same request
    # so SIEM can alert on either shape uniformly.
    ("POST", "/api/system/prune-build-cache", "build_cache.pruned"),
    ("POST", "/api/system/prune", "system.pruned"),
    # Catch-all for anything else under /api/.
    ("*", "/api/", "api.request"),
]

# Regex-compiled table of decorator-derived (method, path-template, label).
# Populated at app-load time by `register_route_audit_events(app)`. FastAPI
# path templates like `/api/containers/{id}/start` are converted to
# `^/api/containers/[^/]+/start$` so an actual request path matches.
_AUDIT_EVENT_REGEXES: list[tuple[str, re.Pattern[str], str]] = []


def _path_template_to_regex(template: str) -> re.Pattern[str]:
    """Convert FastAPI path template to an anchored regex.

    `{name}` → `[^/]+`. Any other regex metacharacters in the literal
    segments are escaped.
    """
    parts = re.split(r"\{[^/]+?\}", template)
    escaped = "[^/]+".join(re.escape(p) for p in parts)
    return re.compile(f"^{escaped}$")


def register_route_audit_events(app) -> None:
    """Derive per-route audit event names from `@secure_route.mutate(audit=...)`.

    Walks every registered FastAPI route, reads the `_skiff_secure["audit"]`
    marker attached by the decorator, and prepends compiled
    (method, path-pattern, event_name) entries to `_AUDIT_EVENT_REGEXES`
    so the request-classification middleware uses the same name the
    handler declared. The static fallback entries in `_AUDIT_EVENT_MAP`
    still catch WS + reads that bypass `@secure_route.mutate`.
    """
    derived: list[tuple[str, re.Pattern[str], str]] = []
    for route in getattr(app, "routes", []):
        endpoint = getattr(route, "endpoint", None)
        marker = getattr(endpoint, "_skiff_secure", None)
        if not marker or not marker.get("audit"):
            continue
        path = getattr(route, "path", "")
        if not path:
            continue
        pattern = _path_template_to_regex(path)
        methods = getattr(route, "methods", set()) or {"*"}
        derived.extend((method, pattern, marker["audit"]) for method in methods)
    # Longer path templates first so `/api/containers/{id}/start` beats
    # the shorter `/api/containers/run` pattern when both would match.
    derived.sort(key=lambda t: -len(t[1].pattern))
    _AUDIT_EVENT_REGEXES[:] = derived


_RESOURCE_PATH_RE = re.compile(r"/api/(?P<rtype>containers|images|volumes|networks|compose)/(?P<rid>[^/]+)")

# URL verbs that the positional regex misclassifies as `rid`. Without
# this filter `/api/containers/run` produces `resource_id="run"` and
# the System page's Resource column renders "container run" for every
# new-container action. Keep every other `rid` (short ids, names) —
# "container ab12cd34" is the useful case.
_URL_VERB_BLOCKLIST: frozenset[str] = frozenset(
    {
        "run",
        "create",
        "prune",
        "up",
        "down",
        "stacks",
        "allowed",
        "list",
        "pull",
        "push",
        "search",
        "tags",
    }
)


def _classify_event(method: str, path: str, status: int, error_code: str = "") -> tuple[str, str, str]:
    """Return (event_type, resource_type, resource_id) for an API request.

    Precedence:
      1. 403 + `auth.reviewer_read_only` → auth.reviewer_denied
      2. 401/403 → auth.denied
      3. 429     → rate_limit.exceeded
      4. Decorator-derived regex table (exact FastAPI route match)
      5. Static _AUDIT_EVENT_MAP (WS + reads + catch-all)
    """
    if status == 403 and error_code == "auth.reviewer_read_only":
        # Distinguish "reviewer profile tried to mutate" from the generic
        # auth.denied bucket so SIEM can whitelist reviewer noise while
        # still alerting on stolen-token mutation attempts.
        return "auth.reviewer_denied", "", ""
    if status in {401, 403}:
        return "auth.denied", "", ""
    if status == 429:
        return "rate_limit.exceeded", "", ""
    event_type = _match_audit_event(method, path)
    m2 = _RESOURCE_PATH_RE.match(path)
    resource_type = m2.group("rtype").rstrip("s") if m2 else ""
    resource_id = m2.group("rid") if m2 else ""
    if resource_id in _URL_VERB_BLOCKLIST:
        resource_id = ""
    # Truncate at the classifier boundary so the audit middleware's
    # Pydantic model never sees an over-long value. A caller who hits a
    # path like `/api/images/<128+ chars>/remove` would otherwise crash
    # the middleware with string_too_long, drop the audit line, and
    # leak a 500 to the client. Limits are kept a hair under the
    # _AuditExtras field caps (32 / 128) to leave headroom for any
    # future truncation indicator.
    resource_type = resource_type[:32]
    resource_id = resource_id[:128]
    return event_type, resource_type, resource_id


def _match_audit_event(method: str, path: str) -> str:
    """Pick the event label for (method, path). Decorator regexes win."""
    for m, pattern, label in _AUDIT_EVENT_REGEXES:
        if (m in ("*", method)) and pattern.match(path):
            return label
    for m, prefix, label in _AUDIT_EVENT_MAP:
        if (m in ("*", method)) and path.startswith(prefix):
            return label
    return "api.request"


# ── ASGI Middlewares ───────────────────────────────────────


def _log_level_for_status(status_code: int) -> str:
    """Pick a log severity from an HTTP status. Mirrors the audit contract."""
    if status_code in (401, 403, 429):
        return "warning"
    if status_code >= 500:
        return "error"
    return "info"


def _describe_auth(auth_header: str, status_code: int = 0) -> tuple[str, str]:
    """Classify an Authorization header. Returns (hint, token_suffix).

      - ""                → ("", "")
      - wrong bearer      → ("invalid", "")
      - no server token   → ("", "") — request behaves as unauthenticated
      - correct bearer    → ("authenticated", last-N-chars of token)

    Token-rotation race: the rotate-token route swaps `_cfg.api_token`
    BEFORE the middleware runs, so a middleware compare against the
    inbound header's old token would return "invalid" even though
    `verify_auth` had already approved the request. When the handler
    returned 2xx, trust that verdict — the dependency chain already
    validated the old token.
    """
    if not auth_header.startswith("Bearer ") or not config._cfg.api_token:
        return ("", "")
    provided = auth_header[7:]
    suffix_len = config.TOKEN_AUDIT_SUFFIX_LEN
    if constant_time_compare(provided, config._cfg.api_token):
        suffix = provided[-suffix_len:] if len(provided) >= suffix_len else provided
        return ("authenticated", suffix)
    # Compare failed but the route succeeded — this is the rotate-token
    # path where the server token changed mid-request. Mark as
    # authenticated (the dependency already passed) and log a truncated
    # suffix from the original bearer so audit correlation still works.
    if 200 <= status_code < 300:
        suffix = provided[-suffix_len:] if len(provided) >= suffix_len else ""
        return ("authenticated", suffix)
    return ("invalid", "")


class _AuditExtras(pydantic.BaseModel):
    """Optional fields for an `audit.api_access` log line.

    Pydantic model (not a plain dataclass) so we get validation + defence
    in depth for free — the audit log is a security-sensitive sink and
    letting a caller slip a non-string (int, object, control chars) into
    it would corrupt SIEM parsers and, worse, could smuggle data past
    field-based search filters.

    Validation contract:
      - Every field is `str`; any other input raises.
      - Length cap per field prevents the audit-log-line-too-long bug.
      - Non-printable ASCII stripped (newlines / NUL would break JSONL).
      - Empty strings drop out of `to_kwargs()` so the log call emits
        only the fields that had a value.
    """

    model_config = pydantic.ConfigDict(str_strip_whitespace=True, extra="forbid")

    token_suffix: str = pydantic.Field(default="", max_length=64)
    user: str = pydantic.Field(default="", max_length=128)
    resource_type: str = pydantic.Field(default="", max_length=32)
    resource_id: str = pydantic.Field(default="", max_length=128)

    @pydantic.field_validator("token_suffix", "user", "resource_type", "resource_id")
    @classmethod
    def _strip_control_chars(cls, v: str) -> str:
        # Printable ASCII only — audit-log JSONL parsers choke on \n / NUL
        # and a control char in the token suffix would break SIEM search.
        return re.sub(r"[^\x20-\x7E]", "", v)

    def to_kwargs(self) -> dict[str, str]:
        """Return only the non-empty fields, for **unpack into log.*()."""
        return {k: v for k, v in self.model_dump().items() if v}


def _emit_api_access_audit(scope: dict, status_code: int, error_code: str = "") -> None:
    """Emit one `audit.api_access` line for the request that just completed.

    `error_code` is extracted from the response envelope's
    `detail.code` by the middleware; falls back to the request-scoped
    contextvar (which works in unit tests and sync-only call sites
    but not across the anyio boundary). The middleware is the
    authoritative source.
    """
    headers = dict(scope.get("headers", []))
    auth_header = headers.get(b"authorization", b"").decode(errors="replace")
    token_hint, token_suffix = _describe_auth(auth_header, status_code)
    # Only trust X-Forwarded-User when the operator has declared a front proxy.
    # Otherwise any direct caller could forge the audit attribution.
    if config.TRUST_FORWARDED_HEADERS:
        raw_user = headers.get(b"x-forwarded-user", b"").decode(errors="replace")
        forwarded_user = re.sub(r"[^\x20-\x7E]", "", raw_user)[:128]
    else:
        forwarded_user = ""

    path = scope.get("path", "")
    method = scope.get("method", "")
    client = scope.get("client")
    if not error_code:
        # Fallback for any non-middleware call site. The middleware
        # passes the authoritative code from the serialized response.
        from skiff.contract.errors import current_error_code

        error_code = current_error_code()
    event_type, rtype, rid = _classify_event(
        method,
        path,
        status_code,
        error_code=error_code,
    )
    try:
        extras = _AuditExtras(
            token_suffix=token_suffix,
            user=forwarded_user,
            resource_type=rtype,
            resource_id=rid,
        )
    except pydantic.ValidationError:
        # Defensive fallback: a future classifier change could miss a
        # field cap or introduce a non-string. The audit middleware
        # must NEVER propagate an exception into the response path —
        # that would drop audit AND return a 500. Log the validation
        # failure as a warning, emit the audit line with empty extras,
        # and keep the user's response intact.
        log.warning("audit.extras_invalid", path=path[:128], method=method)
        extras = _AuditExtras()
    # Two audit channels coexist:
    #   `event=audit.api_access`  — this line, emitted by the middleware on
    #       every request. Always carries method/path/status/remote/auth.
    #       Secondary `event_type` (`api.request`, `container.started`,
    #       `compose.up`, …) is the classified action name used by the
    #       SIEM cookbook in `docs/hardening/production.md`.
    #   `event=<domain>.<verb>`   — emitted by route handlers when they
    #       carry action-specific detail (id, name, signal, changes). See
    #       `docs/audit-events.md` for the catalogue. These live alongside
    #       the middleware line and are authoritative for the action.
    # An alert should key on either `event == "container.started"` (domain
    # emit) OR on the middleware pattern `event == "audit.api_access" AND
    # event_type == "container.started"` — both forms match all mutations.
    getattr(log, _log_level_for_status(status_code))(
        "audit.api_access",
        channel="audit",
        event_type=event_type,
        method=method,
        path=path,
        status=status_code,
        remote=f"{client[0]}" if client else "unknown",
        auth=token_hint or ("none" if not auth_header else "present"),
        **extras.to_kwargs(),
    )


class AuditLogMiddleware:
    """Log all authenticated API requests for governance compliance.

    Pure ASGI middleware (no BaseHTTPMiddleware) to avoid the known Starlette deadlock
    when many clients disconnect simultaneously during the http.response.start phase.

    For 4xx/5xx responses, the middleware peeks at the response body to
    extract the envelope's `code` field. `http_error()` sets a
    `contextvars.ContextVar` but that doesn't survive the anyio task
    boundary between FastAPI's ExceptionMiddleware and our outer
    wrapper; reading the serialized body is the bulletproof path —
    the response was built by FastAPI, so whatever it shipped is
    exactly what the client sees.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        status_code = 500
        body_chunks: list[bytes] = []
        body_overflow = False
        body_peek_limit = 4096  # more than enough for an envelope

        def _maybe_capture_body(message: dict) -> None:
            """Append response-body bytes to `body_chunks` until the
            cap is reached. Extracted to keep `auditing_send` flat."""
            nonlocal body_overflow
            if body_overflow or message["type"] != "http.response.body":
                return
            if status_code < 400:
                return
            chunk = message.get("body", b"") or b""
            total = sum(len(c) for c in body_chunks) + len(chunk)
            if total > body_peek_limit:
                body_overflow = True
                return
            body_chunks.append(chunk)

        async def auditing_send(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            else:
                _maybe_capture_body(message)
            await send(message)

        try:
            await self.app(scope, receive, auditing_send)
        finally:
            error_code = ""
            if status_code >= 400 and body_chunks:
                error_code = _extract_envelope_code(b"".join(body_chunks))
            _emit_api_access_audit(scope, status_code, error_code=error_code)


def _extract_envelope_code(body: bytes) -> str:
    """Return `detail.code` from a JSON error envelope, or ""."""
    import json as _json

    try:
        payload = _json.loads(body or b"{}")
    except ValueError:
        return ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code", "")
        return code if isinstance(code, str) else ""
    return ""


class StripForwardedHeadersMiddleware:
    """Strip `X-Forwarded-*` headers from the ASGI scope when not trusted.

    Defence in depth. `skiff.app._main()` starts uvicorn with
    `proxy_headers=config.TRUST_FORWARDED_HEADERS`, so a CLI-launched
    uvicorn (as the README / CONTRIBUTING / deployment.md recipes use)
    would pick up the library default of `proxy_headers=True` and trust
    these headers regardless of our intent. This middleware enforces the
    policy at the application layer — no matter how the ASGI server was
    invoked, the downstream middleware / handlers never see a
    `x-forwarded-*` header the operator did not explicitly opt into.

    Effect when `TRUST_FORWARDED_HEADERS` is false (the default):
      - X-Forwarded-For        — dropped so rate-limit keys and audit
                                 `remote` reflect the real TCP peer.
      - X-Forwarded-Proto      — dropped so HSTS only emits on real TLS.
      - X-Forwarded-Host       — dropped so snippet rendering can't be
                                 redirected to an attacker-controlled host.
      - X-Forwarded-User       — dropped so audit attribution can't be forged.

    When `TRUST_FORWARDED_HEADERS` is true, the middleware is a no-op
    and the downstream code reads the headers normally (assuming the
    operator has placed a trusted proxy in front that sanitises them).
    """

    # Headers dropped when the flag is off. Lowercased because ASGI
    # header tuples are bytes-level lowercase by convention.
    _STRIPPED = frozenset(
        [
            b"x-forwarded-for",
            b"x-forwarded-proto",
            b"x-forwarded-host",
            b"x-forwarded-user",
            b"x-forwarded-port",
            b"x-forwarded-prefix",
            b"forwarded",
        ]
    )

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") or config.TRUST_FORWARDED_HEADERS:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        filtered = [(k, v) for (k, v) in headers if k.lower() not in self._STRIPPED]
        if len(filtered) != len(headers):
            scope = dict(scope)
            scope["headers"] = filtered
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Reject requests whose Content-Length exceeds config.MAX_BODY_BYTES.

    Early defence: if a client honestly declares a payload larger than
    the cap, refuse with 413 before reading a single byte of the body.

    Per-endpoint validators (the compose file size check in
    skiff.validators.validate_compose_file, for instance) still cap
    specific routes at their own tighter limit. This middleware is the
    cheap-and-loud outer guard so a malicious client can't chew memory
    by declaring a 10 GB body.

    Chunked / transfer-encoded bodies without Content-Length pass
    through here; they hit the per-route validator which reads the
    body into memory with its own cap. We deliberately do NOT wrap
    `receive` — that creates an interleaving hazard with downstream
    send, since the guard has to compete with the app emitting its
    own response.

    OWASP Top 10:2025: A02 Security Misconfiguration (default-deny
    oversize body) + A05 Injection (caps attacker's payload size).
    """

    def __init__(self, app) -> None:
        self.app = app

    @staticmethod
    def _parse_content_length(raw: bytes | None) -> int | None:
        if raw is None:
            return None
        try:
            return int(raw.decode())
        except (ValueError, UnicodeDecodeError):
            # Malformed header — let starlette reject it naturally.
            return None

    @staticmethod
    async def _send_413(send, declared: int | None = None) -> None:
        # Tell the user the ACTUAL limit — "exceeds size cap" was
        # effectively a black box. Include both the declared body size
        # (so "1.0 MB exceeds 512 KiB cap" is self-explanatory) and the
        # knob name so an operator knows what to raise.
        cap = config.MAX_BODY_BYTES

        def _fmt(n: int) -> str:
            if n >= 1024 * 1024:
                return f"{n / (1024 * 1024):.1f} MiB"
            if n >= 1024:
                return f"{n / 1024:.1f} KiB"
            return f"{n} B"

        if declared is not None:
            msg = (
                f"request body {_fmt(declared)} exceeds server cap of {_fmt(cap)}. "
                f"Raise MAX_BODY_BYTES on the server to lift this limit."
            )
        else:
            msg = (
                f"request body exceeds server cap of {_fmt(cap)}. "
                f"Raise MAX_BODY_BYTES on the server to lift this limit."
            )
        payload = (
            b'{"detail":{"code":"validation.body_too_large","message":"'
            + msg.encode("utf-8").replace(b'"', b'\\"')
            + b'"}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    async def _send_408(send) -> None:
        body = (
            b'{"detail":{"code":"validation.body_timeout",'
            b'"message":"request body not received within the allowed window"}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 408,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        declared = self._parse_content_length(headers.get(b"content-length"))
        if declared is not None and declared > config.MAX_BODY_BYTES:
            await self._send_413(send, declared=declared)
            return

        # Slow-POST defence: wrap `receive` so a client that drips the
        # body one byte at a time for minutes ties up at most
        # `BODY_READ_TIMEOUT_SECS` of wall time. On timeout we respond
        # 408 via the error envelope rather than hanging until the
        # connection is finally closed. Downstream middleware / the
        # route handler still sees `receive` — we're just enforcing a
        # time budget per chunk.
        timeout = config.BODY_READ_TIMEOUT_SECS
        timed_out = False

        async def _wrapped_receive():
            nonlocal timed_out
            try:
                return await asyncio.wait_for(receive(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                # Synthesise an empty end-of-body chunk so the downstream
                # parser unwinds cleanly. Pair with the 408 response the
                # caller emits below.
                return {"type": "http.disconnect"}

        async def _guarded_send(message):
            if timed_out:
                # Body-read timed out: refuse to emit the app's response.
                # We'll emit our own 408 after self.app returns.
                return
            await send(message)

        await self.app(scope, _wrapped_receive, _guarded_send)
        if timed_out:
            await self._send_408(send)


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
        # X-Forwarded-Proto is honoured only when an operator has marked
        # a front proxy as trusted. Without the gate, any direct caller
        # could flip HSTS emission on an unencrypted connection.
        if config.TRUST_FORWARDED_HEADERS:
            forwarded_proto = req_headers.get(b"x-forwarded-proto", b"").decode()
            scheme = forwarded_proto or scope.get("scheme", "http")
        else:
            scheme = scope.get("scheme", "http")

        async def security_send(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                # Route handlers may set their own `X-Frame-Options` (e.g.
                # the iframe-hosting `/api/terminal-frame/{id}` sets
                # `SAMEORIGIN` so it can be embedded by the same-origin
                # SPA). Only fall back to the strict global default when
                # the handler hasn't already chosen.
                if "X-Frame-Options" not in headers:
                    headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                # Likewise for CSP — route handlers that need a different
                # policy (Swagger UI's inline styles, xterm's inline
                # styles inside the terminal iframe) set the header
                # themselves; the middleware then leaves it alone instead
                # of clobbering the route-scoped relaxation back to the
                # strict global default.
                if "Content-Security-Policy" not in headers:
                    headers["Content-Security-Policy"] = config._CSP
                headers["Permissions-Policy"] = config._PERMISSIONS_POLICY
                if scheme == "https":
                    headers["Strict-Transport-Security"] = config.HSTS_HEADER
            await send(message)

        await self.app(scope, receive, security_send)
