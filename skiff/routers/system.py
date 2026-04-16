# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""System info, health, setup, audit, and frontend routes."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from skiff.auth import (
    AUTH,
    _invalidate_session_cache,
    verify_auth_strict,
    verify_csrf,
)
from skiff.config import (
    AUDIT_LOG_PATH,
    MAX_AUDIT_LINES,
    MIN_TOKEN_LENGTH,
    RL_DEFAULT,
    RL_FAST,
    RL_SLOW,
    SETUP_LOCKOUT_SECS,
    SETUP_MAX_ATTEMPTS,
    SETUP_WINDOW_SECS,
    TUNNEL_DEFAULT_SOCKET,
    APP_START_MONOTONIC,
    APP_START_WALL,
    _APP_VERSION,
    _INDEX_HTML,
    _LICENSE_FILE,
    _cfg,
    _limit,
    limiter,
)
from skiff.docker_client import (
    _SSH_TARGET_RE,
    _invalidate_client,
    _start_tunnel,
    _stop_tunnel,
    docker_client_dep,
    get_client,
    get_tunnel_socket_path,
)
from skiff.validators import safe_docker_call

log = structlog.get_logger(__name__)
router = APIRouter()

# ── Setup endpoint brute-force protection ────────────────────────────────────
# Maps client_ip → (failure_count, last_failure_monotonic)
_setup_failures: dict[str, tuple[int, float]] = {}
_setup_lock = threading.Lock()


def _setup_fail(client_ip: str, reason: str) -> None:
    """Increment per-IP failure counter and emit an audit log entry."""
    now = time.monotonic()
    with _setup_lock:
        count, _ = _setup_failures.get(client_ip, (0, now))
        _setup_failures[client_ip] = (count + 1, now)
    log.info("audit.setup_failed", remote=client_ip, reason=reason)


# ── Debug ──────────────────────────────────────────────────

@router.get("/debug/threads", dependencies=AUTH, tags=["system"])
async def debug_threads():
    """Return active thread stack traces for debugging. Requires authentication."""
    import traceback
    frames = sys._current_frames()
    result = {}
    for tid, frame in frames.items():
        tb = "".join(traceback.format_stack(frame))
        result[str(tid)] = tb
    return {"thread_count": len(frames), "threads": result}


# ── Auth Info (no auth needed) ─────────────────────────────

@router.get("/api/auth-required", tags=["auth"])
def auth_required():
    """Returns whether auth is required and frontend config. No secrets exposed."""
    return {"required": bool(_cfg.api_token)}


# ── Config ─────────────────────────────────────────────────

@router.get("/api/config", dependencies=AUTH, tags=["auth"])
@limiter.limit(_limit(RL_FAST))
def get_config(request: Request):
    """Return non-secret server configuration for the UI."""
    return {
        "allowed_registries": _cfg.allowed_registries,
        "docker_vm_host": _cfg.docker_vm_host,
        "docker_host": _cfg.docker_host,
    }


# ── Setup (only active when unconfigured) ─────────────────

@router.get("/api/setup-state", tags=["setup"])
def setup_state():
    """Returns configuration state for the setup wizard. No auth required."""
    if _cfg.api_token:
        # Minimal response when already configured — avoid leaking tunnel socket paths
        # to unauthenticated callers on a live server.
        return {"configured": True, "from_env": _cfg.from_env}
    tunnel_socket = get_tunnel_socket_path()
    return {
        "configured": False,
        "from_env": _cfg.from_env,
        "tunnel_active": bool(tunnel_socket and os.path.exists(tunnel_socket)),
        "tunnel_socket": tunnel_socket or TUNNEL_DEFAULT_SOCKET,
    }


@router.post("/api/setup", tags=["setup"])
@limiter.limit(_limit(RL_SLOW))
def do_setup(
    request: Request,
    docker_host: str = Body(...),
    api_token: str = Body(...),
    allowed_registries: str = Body(default=""),
):
    """Configure the server in-memory. Only callable when unconfigured and not from env."""
    verify_csrf(request)
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _setup_lock:
        if client_ip in _setup_failures:
            count, last_t = _setup_failures[client_ip]
            if count >= SETUP_MAX_ATTEMPTS and (now - last_t) < SETUP_LOCKOUT_SECS:
                log.warning("audit.setup_lockout", remote=client_ip, remaining=SETUP_LOCKOUT_SECS - (now - last_t))
                raise HTTPException(429, "Too many failed setup attempts — try again later")
            if (now - last_t) >= SETUP_LOCKOUT_SECS:
                del _setup_failures[client_ip]
    if time.monotonic() - APP_START_MONOTONIC > SETUP_WINDOW_SECS:
        raise HTTPException(403, "Setup window has closed — restart the server to reconfigure")
    if _cfg.from_env:
        raise HTTPException(403, "Server is configured via environment variables — setup endpoint disabled")
    if _cfg.api_token:
        raise HTTPException(403, "Already configured")
    if not api_token or len(api_token) < MIN_TOKEN_LENGTH:
        _setup_fail(client_ip, "token_too_short")
        raise HTTPException(400, f"api_token must be at least {MIN_TOKEN_LENGTH} characters")
    if not docker_host:
        _setup_fail(client_ip, "missing_docker_host")
        raise HTTPException(400, "docker_host is required")
    _dh = docker_host.strip()
    _parsed = urlparse(_dh)
    if _parsed.scheme not in ("unix", "tcp", "npipe"):
        _setup_fail(client_ip, "bad_docker_host_scheme")
        raise HTTPException(400, "docker_host must use unix://, tcp://, or npipe:// scheme")
    if _parsed.scheme == "tcp":
        import ipaddress
        _host = _parsed.hostname or ""
        try:
            ipaddress.ip_address(_host)
        except ValueError:
            _setup_fail(client_ip, "bad_docker_host_address")
            raise HTTPException(400, "tcp:// docker_host must specify an IP address, not a hostname") from None
        if not (1 <= (_parsed.port or 0) <= 65535):
            _setup_fail(client_ip, "bad_docker_host_port")
            raise HTTPException(400, "tcp:// docker_host must include a valid port")
    # Clear failure counter on successful validation
    with _setup_lock:
        _setup_failures.pop(client_ip, None)
    _cfg.api_token = api_token.strip()
    _cfg.docker_host = _dh
    _cfg.allowed_registries = [r.strip() for r in allowed_registries.split(",") if r.strip()]
    _invalidate_client()
    _invalidate_session_cache()
    log.info("setup.configured", docker_host=_cfg.docker_host, registries=_cfg.allowed_registries)
    return {"ok": True}


@router.post("/api/setup/tunnel", tags=["setup"])
@limiter.limit(_limit(RL_SLOW))
def start_tunnel(
    request: Request,
    ssh_target: str = Body(...),
    socket_path: str = Body(default=TUNNEL_DEFAULT_SOCKET),
):
    """Start an SSH ControlMaster tunnel to the Docker VM. Setup-only endpoint."""
    verify_csrf(request)
    if _cfg.from_env:
        raise HTTPException(403, "Setup endpoints disabled when configured via environment")
    if _cfg.api_token:
        raise HTTPException(403, "Already configured")
    if not _SSH_TARGET_RE.match(ssh_target):
        raise HTTPException(400, "ssh_target must be user@host")
    _sp_resolved = Path(socket_path).resolve()
    # /tmp is a symlink to /private/tmp on macOS — resolve both for canonical comparison
    _tmp_resolved = Path("/tmp").resolve()  # noqa: S108
    if not _sp_resolved.is_relative_to(_tmp_resolved):
        raise HTTPException(400, "socket_path must resolve to a path under /tmp/")
    try:
        _start_tunnel(ssh_target, socket_path)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "socket_path": socket_path, "docker_host": f"unix://{socket_path}"}


@router.delete("/api/setup/tunnel", tags=["setup"])
@limiter.limit(_limit(RL_SLOW))
def stop_tunnel_endpoint(request: Request):
    """Stop the managed SSH tunnel."""
    verify_csrf(request)
    if _cfg.from_env:
        raise HTTPException(403, "Setup endpoints disabled when configured via environment")
    _stop_tunnel()
    return {"ok": True}


# ── Health Endpoints (no auth) ─────────────────────────────

@router.get("/health", tags=["health"])
async def health():
    """Liveness — never checks Docker to avoid restart loops."""
    return {"status": "ok", "uptime_seconds": int(time.time() - APP_START_WALL), "version": _APP_VERSION}


@router.get("/ready", tags=["health"])
def ready():
    """Readiness — returns 503 if Docker is unreachable."""
    try:
        client = get_client()
        info = client.info()
        return {
            "status": "ready",
            "docker_version": info.get("ServerVersion", "unknown"),
            "containers_running": info.get("ContainersRunning", 0),
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(exc)})


# ── System Info ────────────────────────────────────────────

@router.get("/api/system/info", dependencies=AUTH, tags=["system"])
@limiter.limit(_limit(RL_SLOW))
def system_info(request: Request, client=Depends(docker_client_dep)) -> dict:
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


@router.get("/api/system/df", dependencies=AUTH, tags=["system"])
@limiter.limit(_limit("5/minute"))
def system_disk_usage(request: Request, client=Depends(docker_client_dep)) -> dict:
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


@router.post("/api/system/prune", dependencies=AUTH, tags=["system"])
@limiter.limit("2/minute")
def system_prune(request: Request, client=Depends(docker_client_dep)) -> dict:
    """Remove all stopped containers, dangling images, and unused networks."""
    verify_csrf(request)
    containers = safe_docker_call(client.containers.prune)
    images = safe_docker_call(client.images.prune)
    networks = safe_docker_call(client.networks.prune)
    log.info("system.pruned",
             containers=len(containers.get("ContainersDeleted") or []),
             images=len(images.get("ImagesDeleted") or []),
             networks=len(networks.get("NetworksDeleted") or []))
    return {
        "containers_deleted": len(containers.get("ContainersDeleted") or []),
        "images_deleted": len(images.get("ImagesDeleted") or []),
        "networks_deleted": len(networks.get("NetworksDeleted") or []),
        "space_reclaimed_mb": round(
            (containers.get("SpaceReclaimed", 0) + images.get("SpaceReclaimed", 0)) / 1024 / 1024, 1,
        ),
    }


@router.post("/api/system/prune-build-cache", dependencies=AUTH, tags=["system"])
@limiter.limit("2/minute")
def prune_build_cache(request: Request, client=Depends(docker_client_dep)) -> dict:
    """Clear Docker build cache and return the amount of space reclaimed."""
    verify_csrf(request)
    result = safe_docker_call(client.api.prune_builds)
    space = result.get("SpaceReclaimed", 0)
    log.info("build_cache.pruned", space_mb=round(space / 1024 / 1024, 1))
    return {"space_reclaimed_mb": round(space / 1024 / 1024, 1)}


# ── Audit Log ──────────────────────────────────────────────

@router.get("/api/system/audit-log", dependencies=[Depends(verify_auth_strict)], tags=["audit"])
@limiter.limit("5/minute")
def get_audit_log(request: Request, tail: int = Query(default=200, le=MAX_AUDIT_LINES, ge=1)):
    """Return the last N lines of the app audit log, read efficiently without loading the full file."""
    if not AUDIT_LOG_PATH.exists():
        return []
    # Read only the last chunk of the file to avoid loading hundreds of MB into memory.
    # Assumes average line length of ~300 bytes; read 2x that budget to be safe.
    chunk_size = tail * 600
    raw_lines: list[str] = []
    try:
        with AUDIT_LOG_PATH.open("rb") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            seek_to = max(0, file_size - chunk_size)
            fh.seek(seek_to)
            chunk = fh.read()
        raw_lines = chunk.decode("utf-8", errors="replace").splitlines()
        if seek_to > 0:
            raw_lines = raw_lines[1:]  # discard potentially partial first line
    except OSError:
        return []
    result = []
    for raw_line in raw_lines[-tail:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            result.append(json.loads(stripped))
        except json.JSONDecodeError:
            result.append({"raw": stripped})
    return result


@router.get("/api/system/audit-log/download", dependencies=[Depends(verify_auth_strict)], tags=["audit"])
@limiter.limit("2/minute")
def download_audit_log(request: Request):
    """Download the full audit log as a JSONL file (streamed to avoid memory spikes)."""
    if not AUDIT_LOG_PATH.exists():
        return PlainTextResponse("", media_type="application/x-ndjson",
                                 headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'})
    return FileResponse(
        path=str(AUDIT_LOG_PATH),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'},
    )


# ── Frontend ───────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def index() -> Response:
    """Serve the SPA frontend."""
    return Response(content=_INDEX_HTML, media_type="text/html")


@router.get("/LICENSE", include_in_schema=False)
def license_file() -> FileResponse:
    """Serve the MIT LICENSE file."""
    return FileResponse(_LICENSE_FILE, media_type="text/plain")
