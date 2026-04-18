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
    return {
        **resolved,
        "profile": config.PROFILE,
        "bind_host": bind,
        "rate_limit_scale": _config_module._RATE_SCALE,
        "insecure_mode": _is_insecure_mode(bind),
    }


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


# ── OpenAPI docs landing (no auth, CSP-safe) ──────────────
# The default FastAPI Swagger UI / ReDoc routes are disabled because they
# pull assets from a CDN, violating our strict CSP. This landing page
# offers the raw spec + deep links into editor.swagger.io (opens in a
# new tab — no connect-src cost to this page). A tiny static script at
# /static/core/docs.js stitches in window.location at view time.

_API_DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SKIFF API documentation</title>
<style>
  :root { --accent: #0d9488; --muted: #64748b; color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 720px; margin: 48px auto; padding: 0 24px; line-height: 1.6; }
  h1 { font-size: 24px; margin: 0 0 8px; }
  p { color: var(--muted); margin: 0 0 16px; }
  code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-family: ui-monospace, monospace; }
  @media (prefers-color-scheme: dark) { code { background: #1b2236; color: #e2e8f0; } }
  a.btn { display: inline-block; padding: 10px 16px; margin: 4px 8px 4px 0;
          background: var(--accent); color: #fff; border-radius: 6px;
          text-decoration: none; font-weight: 500; }
  a.btn.secondary { background: transparent; color: var(--accent);
                    border: 1px solid var(--accent); }
  .section { margin: 24px 0; padding: 16px; border: 1px solid #e5e7eb;
             border-radius: 8px; }
  @media (prefers-color-scheme: dark) {
    .section { border-color: #26324a; }
    body { background: #0b1220; color: #e2e8f0; }
  }
</style>
</head>
<body>
<h1>SKIFF API documentation</h1>
<p>OpenAPI 3.1 schema for every endpoint this server exposes, with request/response
shapes, rate limits, and required authentication. The spec is the source of truth;
pick the viewer that suits your workflow.</p>

<div class="section">
  <strong>Raw spec</strong>
  <p>Machine-readable JSON. Suitable for code generation
  (<code>openapi-generator</code>, <code>oapi-codegen</code>, etc.)
  and for importing into any OpenAPI-aware tool.</p>
  <a class="btn" href="/api/openapi.json">Download openapi.json</a>
  <a class="btn secondary" href="/api/openapi.json" target="_blank">Open in browser</a>
</div>

<div class="section">
  <strong>Interactive explorer</strong>
  <p>Our Content Security Policy blocks the default Swagger UI (CDN assets).
  Open the spec in Swagger Editor instead — paste your server URL
  in the top bar to make "Try it out" requests hit this instance.</p>
  <a class="btn" href="https://editor.swagger.io/?url="
     id="editor-link" target="_blank">Open in Swagger Editor</a>
  <a class="btn secondary" href="https://petstore.swagger.io/?url="
     id="petstore-link" target="_blank">Open in Swagger UI</a>
</div>

<div class="section">
  <strong>Quick curl</strong>
  <pre><code>curl -H "Authorization: Bearer $TOKEN" \\
  -H "X-Requested-With: ContainerManager" \\
  <span id="origin"></span>/api/containers</code></pre>
</div>

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
            "Content-Security-Policy":
                "default-src 'self'; script-src 'self'; "
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
    return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


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
    image_bytes = sum(i.get("Size", 0) for i in (df.get("Images") or []))
    container_bytes = sum(c.get("SizeRw", 0) for c in (df.get("Containers") or []))
    volume_bytes = sum((v.get("UsageData") or {}).get("Size", 0) for v in (df.get("Volumes") or []))
    build_cache_bytes = sum(b.get("Size", 0) for b in (df.get("BuildCache") or []))

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
        f"skiff_containers_total{{docker_host=\"{docker_host_label}\"}} {info.get('Containers', 0)}",
        "# HELP skiff_containers_running Currently running containers.",
        "# TYPE skiff_containers_running gauge",
        f"skiff_containers_running{{docker_host=\"{docker_host_label}\"}} {info.get('ContainersRunning', 0)}",
        "# HELP skiff_containers_paused Paused containers.",
        "# TYPE skiff_containers_paused gauge",
        f"skiff_containers_paused{{docker_host=\"{docker_host_label}\"}} {info.get('ContainersPaused', 0)}",
        "# HELP skiff_containers_stopped Stopped containers.",
        "# TYPE skiff_containers_stopped gauge",
        f"skiff_containers_stopped{{docker_host=\"{docker_host_label}\"}} {info.get('ContainersStopped', 0)}",
        "# HELP skiff_images_total Total images on the managed engine.",
        "# TYPE skiff_images_total gauge",
        f"skiff_images_total{{docker_host=\"{docker_host_label}\"}} {info.get('Images', 0)}",
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
def system_disk_usage(request: Request, client=Depends(docker_client.docker_client_dep)) -> dict:
    """Return disk usage breakdown for images, containers, volumes, and build cache."""
    df = safe_docker_call(client.df)
    images = df.get("Images") or []
    containers = df.get("Containers") or []
    volumes = df.get("Volumes") or []
    build_cache = df.get("BuildCache") or []
    image_size = sum(i.get("Size", 0) for i in images)
    image_reclaimable = sum(i.get("Size", 0) for i in images if i.get("Containers", 0) == 0)
    container_size = sum(c.get("SizeRw", 0) for c in containers)
    volume_size = sum(v.get("UsageData", {}).get("Size", 0) for v in volumes)
    volume_reclaimable = sum(
        v.get("UsageData", {}).get("Size", 0) for v in volumes if v.get("UsageData", {}).get("RefCount", 0) == 0
    )
    build_cache_size = sum(b.get("Size", 0) for b in build_cache)
    build_cache_reclaimable = sum(b.get("Size", 0) for b in build_cache if not b.get("InUse", False))
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
            (containers.get("SpaceReclaimed", 0) + images.get("SpaceReclaimed", 0)) / 1024 / 1024, 1,
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
