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
import sys
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from skiff import config, docker_client
from skiff.auth import AUTH  # decorator arg — direct import for readability
from skiff.contract.errors import http_error
from skiff.rate import RATE
from skiff.secure import secure_route
from skiff.validators import safe_docker_call

router = APIRouter()


# ── Config ─────────────────────────────────────────────────

_LOOPBACK_BINDS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _resolve_knob(knob_name: str, spec) -> Any:
    """Return the current value of an exposed knob.

    Three sources, in priority order:
      1. live _cfg.<attr> for mutable knobs (setup-wizard edits visible)
      2. validator(env_value) when env is set and the knob has a validator
      3. spec.default otherwise

    Validator errors fall back to the default so a bad env entry can't
    500 /api/config.
    """
    attr = knob_name.lower()
    if hasattr(config._cfg, attr):
        return getattr(config._cfg, attr)
    raw = os.environ.get(knob_name, spec.default)
    if raw is None or spec.validator is None:
        return raw
    try:
        return spec.validator(raw)
    except (ValueError, TypeError):
        return spec.default


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

import structlog  # noqa: E402 — log is only used by the prune handlers below

log = structlog.get_logger(__name__)


@router.post("/api/system/prune", dependencies=AUTH, tags=["system"])
@secure_route.mutate(RATE.BURST)  # audit emitted inline (needs computed counts)
def system_prune(request: Request, client=Depends(docker_client.docker_client_dep)) -> dict:
    """Remove all stopped containers, dangling images, and unused networks."""
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


@router.post("/api/system/prune-build-cache", dependencies=AUTH, tags=["system"])
@secure_route.mutate(RATE.BURST)  # audit emitted inline (needs computed size)
def prune_build_cache(request: Request, client=Depends(docker_client.docker_client_dep)) -> dict:
    """Clear Docker build cache and return the amount of space reclaimed."""
    result = safe_docker_call(client.api.prune_builds)
    space = result.get("SpaceReclaimed", 0)
    log.info("build_cache.pruned", space_mb=round(space / 1024 / 1024, 1))
    return {"space_reclaimed_mb": round(space / 1024 / 1024, 1)}


# ── Frontend (SPA + MIT license) ──────────────────────────


@router.get("/", include_in_schema=False)
async def index() -> Response:
    """Serve the SPA frontend."""
    return Response(content=config._INDEX_HTML, media_type="text/html")


@router.get("/LICENSE", include_in_schema=False)
def license_file() -> FileResponse:
    """Serve the MIT LICENSE file."""
    return FileResponse(config._LICENSE_FILE, media_type="text/plain")
