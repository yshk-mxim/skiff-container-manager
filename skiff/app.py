# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""
SKIFF Container Manager — FastAPI application entrypoint.

This module wires together the FastAPI app, middleware, rate limiter, and routers.
Implementation is split across focused submodules:
  skiff.config          — runtime config, constants, rate-limiter
  skiff.auth            — authentication, CSRF, session tracking, WebSocket auth
  skiff.logging_setup   — structured logging, audit log, ASGI middlewares
  skiff.docker_client   — Docker client singleton, SSH tunnel management
  skiff.validators      — input validation, Docker helpers, compose sandboxing
  skiff.routers.*       — route handlers grouped by resource type
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as _StarletteHTTPException

# ── logging_setup MUST be imported first to configure structlog
# before any other skiff module creates a logger. ──────────────
import skiff.logging_setup as _logging_setup
from skiff import config
from skiff.docker_client import invalidate_client, stop_tunnel
from skiff.logging_setup import (
    AuditLogMiddleware,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    StripForwardedHeadersMiddleware,
    _loop_lag_monitor,
    register_route_audit_events,
)
from skiff.routers import (
    audit,
    compose,
    containers,
    containers_ws,
    debug,
    health,
    images,
    networks,
    setup,
    system,
    undo_routes,
    volumes,
)

log = structlog.get_logger(__name__)


# ── Lifespan ───────────────────────────────────────────────
# Each startup task is a named helper: "run the sequence, log the event,
# start the monitor". lifespan is a thin composition — independent
# concerns live in independent functions.

_LOCAL_DOCKER_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEPENDENCY_PACKAGES = (
    "fastapi",
    "uvicorn",
    "docker",
    "structlog",
    "slowapi",
    "pyyaml",
    "python-multipart",
)


def _warn_missing_api_token() -> None:
    if not config._cfg.api_token:
        log.warning("security.no_api_token", msg="Running without auth — set API_TOKEN for production")


def _warn_setup_window_open() -> None:
    """Loudly announce that the first-run wizard is reachable.

    When `api_token` is unset and `from_env` is false, `POST /api/setup`
    is callable for `SETUP_WINDOW_SECS` after boot. Anyone who can reach
    `BIND_HOST:PORT` during that window can claim the instance with
    their own token. The warning names the bind, the duration, and the
    mitigation so a first-run operator on a shared host isn't surprised.

    Does not fire on fully-configured boots (token in env → `from_env`
    true → wizard is dead from boot-0, so the race doesn't exist).
    """
    if config._cfg.api_token or config._cfg.from_env:
        return
    log.warning(
        "security.setup_window_open",
        bind=config.BIND_HOST,
        port=_resolve_port_for_warning(),
        window_secs=config.SETUP_WINDOW_SECS,
        lockout_attempts=config.SETUP_MAX_ATTEMPTS,
        msg=(
            f"Setup wizard is reachable on {config.BIND_HOST}:{_resolve_port_for_warning()} "
            f"for {config.SETUP_WINDOW_SECS} seconds. Anyone who can reach this socket can claim "
            f"the instance with their own API token (rate-limited; {config.SETUP_MAX_ATTEMPTS} "
            f"failed attempts per IP → 5-minute lockout). "
            f"To skip the wizard entirely, set API_TOKEN in the environment before starting."
        ),
    )


def _resolve_port_for_warning() -> int | str:
    """Best-effort port for the setup-window warning — the server config
    does not own the port (uvicorn does), so we look at argv as a hint.
    Falls back to '?' when nothing obvious is on the CLI — the operator
    still sees the warning, just without the port echoed back."""
    import sys as _sys

    argv = _sys.argv
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return argv[i + 1]
        if arg.startswith("--port="):
            val = arg.split("=", 1)[1]
            try:
                return int(val)
            except ValueError:
                return val
    return "?"


def _warn_empty_api_token_env() -> None:
    if config._cfg.api_token_set_but_empty:
        log.warning(
            "security.empty_api_token_env",
            msg="API_TOKEN env var is set but empty — setup endpoint is OPEN. "
            "Set a non-empty token or unset the variable.",
        )


def _warn_no_registry_allowlist() -> None:
    if not config._cfg.allowed_registries:
        log.warning(
            "security.no_registry_allowlist",
            msg=(
                "ALLOWED_REGISTRIES is empty — all image pulls will be "
                "REJECTED with image.registry_blocked. Set the env var "
                "to one or more registry hostnames (e.g. "
                "'docker.io,ghcr.io') to permit pulls from those origins."
            ),
        )


def _warn_unencrypted_docker_host() -> None:
    dh = config._cfg.docker_host or ""
    # Plain tcp:// and http:// to a remote host both cross the network
    # unencrypted. https:// (TLS) and unix:// (local socket or SSH tunnel)
    # are the safe shapes.
    if not dh.startswith(("http://", "tcp://")):
        return
    from urllib.parse import urlparse

    host = urlparse(dh).hostname or ""
    if host in _LOCAL_DOCKER_HOSTS:
        return
    log.warning(
        "security.docker_host_unencrypted",
        msg="DOCKER_HOST uses an unencrypted transport to a non-localhost address. "
        "Use a TLS-secured (https://) or SSH-tunnelled (unix://) connection instead.",
        docker_host=dh,
    )


def _warn_short_env_token() -> None:
    """If API_TOKEN came from the env and is shorter than MIN_TOKEN_LENGTH, warn.

    The setup-wizard path already enforces the 16-char minimum (see
    `skiff/routers/setup.py`), but `_cfg.from_env = True` bypasses the
    wizard entirely. An operator who exports `API_TOKEN=short` gets an
    unguarded start with a brute-forceable token — refuse to stay silent.
    """
    token = config._cfg.api_token or ""
    if not token or not config._cfg.from_env:
        return
    if len(token) < config.MIN_TOKEN_LENGTH:
        log.warning(
            "security.short_env_token",
            msg=(
                f"API_TOKEN from the environment is only {len(token)} chars; the "
                f"minimum enforced by the setup wizard is {config.MIN_TOKEN_LENGTH}. "
                "Generate a stronger token: `openssl rand -hex 32`."
            ),
        )


def _warn_proxy_headers_untrusted() -> None:
    """Warn loudly if uvicorn was launched with proxy_headers on but
    TRUST_FORWARDED_HEADERS is off.

    Uvicorn's CLI defaults `--proxy-headers true`, which rewrites
    `scope["client"]` from `X-Forwarded-For` BEFORE any ASGI middleware
    runs. When `TRUST_FORWARDED_HEADERS` is off SKIFF's code refuses to
    read forwarded headers directly (see `StripForwardedHeadersMiddleware`),
    but rate-limit keys + audit `remote` derive from `scope["client"]`
    which is already poisoned by the time we see it.

    The reliable fix is to either (a) run SKIFF via the `skiff` console
    script (which explicitly disables proxy_headers), or (b) pass
    `--no-proxy-headers --forwarded-allow-ips ""` on the uvicorn CLI.
    This warning surfaces the misconfiguration so the operator can't
    miss it in the startup log.
    """
    import sys as _sys

    if config.TRUST_FORWARDED_HEADERS:
        return  # operator has opted in; proxy_headers on is expected
    argv = " ".join(_sys.argv)
    if "--no-proxy-headers" in argv:
        return  # explicitly disabled
    if "skiff.app:_main" in argv or argv.endswith("skiff"):
        return  # running under our console script which disables proxy_headers
    # Can't reach uvicorn config from here; the argv heuristic is best-effort.
    log.warning(
        "security.proxy_headers_untrusted",
        msg=(
            "uvicorn may be running with --proxy-headers enabled (its CLI "
            "default) while TRUST_FORWARDED_HEADERS is off. If so, "
            "X-Forwarded-For can forge audit `remote` and rate-limit keys. "
            'Relaunch with `--no-proxy-headers --forwarded-allow-ips ""` '
            "or via the `skiff` console script, or set "
            "TRUST_FORWARDED_HEADERS=true when a trusted proxy fronts SKIFF."
        ),
    )


def _warn_non_loopback_bind() -> None:
    """Emit a startup warning when BIND_HOST is not a loopback address.

    Backs the SECURITY.md V13 ASVS claim that a non-loopback bind
    produces a startup warning. The UI already paints a banner via
    the `insecure_mode` flag in `/api/config`, but operators who
    only tail stderr won't see that — so mirror the signal here.
    The warning fires whether or not an API_TOKEN is set: a
    non-loopback bind is an operator choice that deserves an audible
    ping in the boot log every time.
    """
    bind = (config.BIND_HOST or "127.0.0.1").strip()
    # Literal loopback aliases — a bind to any of these is NOT a
    # network-exposed listener. 0.0.0.0 deliberately is NOT on this
    # list: binding to all interfaces includes non-loopback ones and
    # is exactly the case the warning exists for.
    if bind in {"127.0.0.1", "localhost", "::1"}:
        return
    log.warning(
        "security.bind_non_loopback",
        bind_host=bind,
        msg=(
            "SKIFF is bound to a non-loopback interface. Ensure a "
            "TLS-terminating reverse proxy fronts it (see "
            "docs/hardening/production.md §TLS termination), a tight "
            "firewall rule limits source IPs, and API_TOKEN is set "
            "(setting TRUST_FORWARDED_HEADERS=true is required so "
            "audit/ratelimit keys follow the real client)."
        ),
    )


def _warn_ci_profile_needs_token() -> None:
    """PROFILE=ci is the automation persona; it must boot with an
    API_TOKEN set. Without one, the setup wizard path would fire
    and no CI runner has a UI. Warn loudly so the operator fixes
    the env, and refuse to proceed silently into an unusable state.
    """
    if config.PROFILE != "ci":
        return
    if config._cfg.api_token.strip():
        return
    log.warning(
        "security.ci_profile_needs_token",
        msg=(
            "PROFILE=ci requires API_TOKEN to be set at boot. The "
            "automation persona does not fit a wizard-driven first run. "
            "Export API_TOKEN (>=16 chars, `openssl rand -hex 32`) or "
            "change PROFILE before deploying."
        ),
    )


def _shape_docker_host(raw: str) -> str:
    """Redact personal path segments from a docker_host for the startup banner.

    The startup banner goes to stdout and the audit log. Operators
    occasionally ship either to support channels; a raw value like
    `unix:///Users/<real-name>/.colima/default/docker.sock` would leak
    their local username and tooling choice. Reduce to scheme +
    socket basename so SIEM correlation still works (same input →
    same output) without the personal middle segments.
    """
    if raw.startswith("unix://"):
        from pathlib import PurePosixPath

        path = raw[len("unix://") :]
        basename = PurePosixPath(path).name or "(unnamed)"
        return f"unix://.../{basename}"
    return raw


def _log_startup_banner() -> None:
    # Include the resolved audit-log path's parent so operators can
    # grep it from startup logs without spelunking through
    # `skiff/config.py:_user_state_root`. The docker_host is shaped
    # to avoid leaking personal path segments when the log is shared.
    log.info(
        "app.started",
        docker_host=_shape_docker_host(config._cfg.docker_host or ""),
        registries=config._cfg.allowed_registries,
        bind=config.BIND_HOST,
        audit_log=str(config.AUDIT_LOG_PATH),
    )


def _log_dependency_versions() -> None:
    """Emit installed package versions for post-incident forensics. Best-effort.

    A missing dep or broken importlib.metadata must not block startup —
    the dict-comp already silently drops anything with no version.
    """
    import importlib.metadata as imeta

    try:
        versions = {pkg: imeta.version(pkg) for pkg in _DEPENDENCY_PACKAGES if imeta.version(pkg)}
    except (imeta.PackageNotFoundError, OSError):
        return
    log.info("app.dependency_versions", **versions)


_STARTUP_WARNINGS = (
    _warn_missing_api_token,
    _warn_setup_window_open,
    _warn_empty_api_token_env,
    _warn_no_registry_allowlist,
    _warn_unencrypted_docker_host,
    _warn_short_env_token,
    _warn_proxy_headers_untrusted,
    _warn_non_loopback_bind,
    _warn_ci_profile_needs_token,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _logging_setup.install_access_log_scrubber()
    for warn in _STARTUP_WARNINGS:
        warn()
    _log_startup_banner()
    _log_dependency_versions()
    monitor = asyncio.create_task(_loop_lag_monitor(), name="loop-lag-monitor")
    yield
    monitor.cancel()
    # Fire any queued undo operations synchronously before shutdown so a
    # clean SIGTERM doesn't silently drop pending destructive rollbacks.
    # Cap total flush time so a slow Docker daemon cannot hold the process
    # past systemd/k8s's SIGKILL grace window (~30 s default). On timeout,
    # surviving ops stay in the in-memory queue and are lost — but that is
    # strictly better than the whole process getting SIGKILL'd mid-flush.
    from skiff.undo import get_queue

    queue = get_queue()
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, queue.fire_all_now),
            timeout=config.SHUTDOWN_FLUSH_TIMEOUT,
        )
    except TimeoutError:
        log.error(
            "undo.shutdown_flush_timeout",
            remaining=queue.depth(),
            timeout=config.SHUTDOWN_FLUSH_TIMEOUT,
        )
    log.info("app.shutdown")
    stop_tunnel()
    invalidate_client()


# ── FastAPI app ────────────────────────────────────────────

app = FastAPI(
    title=config.APP_TITLE,
    version=config._APP_VERSION,
    # Default /docs (Swagger UI) and /redoc (ReDoc) pull their assets from a CDN,
    # which our strict CSP (`script-src 'self'`) blocks. We serve a CSP-safe,
    # self-hosted Swagger UI at /api/docs instead (see routers/system.py) with
    # assets vendored under skiff/static/swagger-ui/.
    docs_url=None,
    redoc_url=None,
    openapi_url=config.OPENAPI_URL,
    description=config.APP_DESCRIPTION,
    openapi_tags=[dict(t) for t in config.OPENAPI_TAGS],
    lifespan=lifespan,
)


def _custom_openapi():
    """Inject the `bearerAuth` security scheme into the generated spec.

    FastAPI only auto-declares security schemes for routes that wire an
    explicit `Security(HTTPBearer())` dep; SKIFF uses a plain `AUTH`
    dependency so the schema ends up without `components.securitySchemes`.
    Without this, Swagger UI's "Authorize" button never appears and
    `Try it out` requests ship no auth header.

    Caches the generated schema — FastAPI expects `openapi()` to be
    idempotent and memoised, matching its default behaviour.
    """
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=[dict(t) for t in config.OPENAPI_TAGS],
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "API token issued via the setup wizard or the `API_TOKEN` env var. "
            "Click Authorize, paste the token, and every `Try it out` request "
            "will carry `Authorization: Bearer <token>`. Mutating requests "
            "additionally need the `X-Requested-With: ContainerManager` CSRF "
            "header, which Swagger UI sends when the spec declares it."
        ),
    }
    # Apply bearerAuth to every path that isn't explicitly public-catalogued.
    # The explicit list here matches SECURITY.md's unauthenticated-endpoints
    # section; anything else gets the security requirement so the UI shows
    # the auth-required padlock and issues the header.
    public_paths = {
        "/health",
        "/ready",
        "/api/auth-required",
        "/api/setup-state",
        "/api/setup",
        "/api/setup/probe-docker",
        "/api/setup/tunnel",
        "/api/docs",
        "/api/openapi.json",
    }
    for path, ops in schema.get("paths", {}).items():
        if path in public_paths:
            continue
        for op in ops.values():
            if isinstance(op, dict):
                op.setdefault("security", [{"bearerAuth": []}])
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

# ── Rate limiting + validation error envelope ──────────────
app.state.limiter = config.limiter


def _rate_limit_envelope_handler(request: Request, exc: RateLimitExceeded):
    """Shape slowapi's 429 response into the documented error envelope.

    slowapi's default handler emits a raw `{"error":"Rate limit exceeded: <spec>"}`.
    Every SIEM / retry client keyed on `detail.code` (see `docs/errors.md`) would
    miss a 429 otherwise. We wrap into the same shape every other error uses.
    """
    # `exc.limit` is a slowapi Limit object whose repr is the useless
    # `<slowapi.wrappers.Limit object at 0x…>`. The inner `.limit` (a
    # `limits.RateLimitItem`) has a proper `__str__` producing the
    # human-readable spec — use that for the user-facing message.
    limit_obj = getattr(exc, "limit", None)
    inner = getattr(limit_obj, "limit", None) if limit_obj is not None else None
    limit_text = str(inner) if inner is not None else ""
    message = f"too many requests ({limit_text})" if limit_text else "too many requests"
    response = JSONResponse(
        status_code=429,
        content={"detail": {"code": "auth.rate_limited", "message": message}},
    )
    # Propagate the Retry-After / rate-limit metadata headers slowapi normally sets.
    if hasattr(request.state, "view_rate_limit"):
        response = request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)
    return response


def _request_validation_envelope_handler(request: Request, exc):
    """Shape FastAPI's 422 into the documented envelope.

    Default body is a list of dicts (`[{type, loc, msg, input}, ...]`).
    We collapse that into one structured entry so clients keying on
    `detail.code` (see `docs/errors.md`) get a uniform shape across every
    4xx / 5xx the server produces.
    """
    if not isinstance(exc, RequestValidationError):
        # Defensive — FastAPI only dispatches RequestValidationError here.
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "validation.bad_input", "message": "invalid input"}},
        )
    # Summarise the first error location for the `message` field — enough
    # for a client to understand, not enough to leak stack-trace internals.
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    base_msg = first.get("msg", "invalid input")
    msg = f"{loc}: {base_msg}" if loc and base_msg else (base_msg or "invalid input")
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "validation.bad_input", "message": msg}},
    )


app.add_exception_handler(RateLimitExceeded, _rate_limit_envelope_handler)
app.add_exception_handler(RequestValidationError, _request_validation_envelope_handler)


# FastAPI's default 404 payload is `{"detail": "Not Found"}` — a bare
# string, not the `{code, message, help?}` envelope the rest of SKIFF
# emits. Clients keyed on `detail.code` would silently ignore 404s.
# Wrap the star-catch-all so every path-not-found speaks the same shape.
async def _not_found_envelope(request: Request, exc):
    """Return the documented error envelope on 404 / 405 / similar bare-string HTTPExceptions.

    Any HTTPException our own code raises via `http_error(…)` already
    carries a dict `detail` — those pass through unchanged. Starlette /
    FastAPI fallback handlers emit bare-string `detail`s for 404 (route
    not found) and 405 (method not allowed); reshape both into the
    envelope so clients keyed on `detail.code` don't silently miss them.
    """
    status = getattr(exc, "status_code", 500)
    if not isinstance(exc, _StarletteHTTPException) or not isinstance(exc.detail, str):
        return JSONResponse(status_code=status, content={"detail": exc.detail})
    bare_map = {
        404: (
            "system.route_not_found",
            "no route matches this path + method",
            "Verify the URL against `docs/api-reference.md` or `GET /api/openapi.json`.",
        ),
        405: (
            "system.method_not_allowed",
            "this route does not accept that HTTP method",
            "Check the `Allow` header on the response for the methods this path accepts.",
        ),
    }
    if status in bare_map:
        code, msg, help_ = bare_map[status]
        return JSONResponse(
            status_code=status,
            content={"detail": {"code": code, "message": msg, "help": help_}},
        )
    return JSONResponse(status_code=status, content={"detail": exc.detail})


app.add_exception_handler(_StarletteHTTPException, _not_found_envelope)

# ── Middleware ─────────────────────────────────────────────
#
# ASGI stacks last-added-first — each `add_middleware` call wraps the
# previous layer, so the LAST line below is the outermost layer that
# runs first on the request path.
#
# Desired order (outermost → innermost):
#   StripForwardedHeaders — drops X-Forwarded-* unless trust-flag on.
#   CORSMiddleware        — adds CORS response headers on EVERY
#                           response (including 413 / 4xx from inner
#                           short-circuits) so a browser cross-origin
#                           caller can read the body instead of seeing
#                           an opaque CORS-blocked error.
#   SecurityHeaders       — injects CSP / HSTS / X-Content-Type etc.
#                           on EVERY response for the same reason.
#   BodySizeLimit         — rejects oversize bodies before auth/audit.
#   AuditLog              — structured audit on successful dispatch.
#   Routes                — innermost.
#
# Therefore add_middleware order is innermost-first:
app.add_middleware(AuditLogMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
# SecurityHeaders wraps BodySizeLimit so a 413 short-circuit still
# emits CSP / Permissions-Policy / X-Content-Type-Options etc.
app.add_middleware(SecurityHeadersMiddleware)
# CORS wraps SecurityHeaders so a browser cross-origin caller can read
# 413 / 4xx bodies instead of an opaque-failure error. Prior ordering
# had CORS innermost, so BodySizeLimit's 413 short-circuit shipped
# without the `Access-Control-Allow-Origin` header and the browser
# discarded the body.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config._cfg.allowed_origins,
    allow_methods=list(config.CORS_ALLOW_METHODS),
    allow_headers=list(config.CORS_ALLOW_HEADERS),
)
# StripForwardedHeaders runs outermost so nothing downstream — not
# CORS, not SlowAPI, not AuditLog, not the security-headers layer's
# HSTS flip — reads an X-Forwarded-* header the operator hasn't
# explicitly trusted. Uvicorn's CLI default is `proxy_headers=True`,
# which would otherwise rewrite `scope["client"]` from the forged
# header before any SKIFF code runs; this middleware + `_main()`'s
# `proxy_headers=False` are belt-and-suspenders.
app.add_middleware(StripForwardedHeadersMiddleware)

# ── Routers ────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(audit.router)
app.include_router(debug.router)
app.include_router(undo_routes.router)
app.include_router(system.router)
app.include_router(setup.router)
app.include_router(containers.router)
app.include_router(containers_ws.router)
app.include_router(images.router)
app.include_router(volumes.router)
app.include_router(networks.router)
app.include_router(compose.router)

# Overlay decorator-derived audit events on _AUDIT_EVENT_MAP at
# module-load time rather than in lifespan, so classification is
# correct for TestClient instances that bypass lifespan.
register_route_audit_events(app)

# ── Static files ───────────────────────────────────────────
app.mount("/static", StaticFiles(directory=config._STATIC_DIR), name="static")


def _main():
    """Entrypoint for `pip install` / `skiff` CLI command.

    `proxy_headers` is gated on TRUST_FORWARDED_HEADERS. When unset
    (default), uvicorn ignores X-Forwarded-* from upstream so the audit
    `remote` field and rate-limit bucket key reflect the actual TCP peer,
    not an attacker-controlled header. When set, we trust the proxy to
    have sanitised them already (same contract SKIFF uses for
    X-Forwarded-User in the audit log).
    """
    import uvicorn

    uvicorn.run(
        "skiff.app:app",
        host=config.BIND_HOST,
        port=config.APP_PORT,
        workers=config.UVICORN_WORKERS,
        log_level=config.UVICORN_LOG_LEVEL,
        proxy_headers=config.TRUST_FORWARDED_HEADERS,
        forwarded_allow_ips="*" if config.TRUST_FORWARDED_HEADERS else "",
    )


if __name__ == "__main__":
    _main()
