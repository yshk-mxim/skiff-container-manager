# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Health / readiness / auth-discovery endpoints.

Separated from `routers.system` so the liveness probe path is tiny and
has zero incidental dependencies — a container orchestrator hitting
`/health` shouldn't pull in the whole `system_metrics` / `connect_snippets`
surface just to resolve a route.

  GET /health           liveness — never touches Docker
  GET /ready            readiness — returns 503 if Docker engine unreachable
  GET /api/auth-required bool flag used by the pre-login UI
"""

from __future__ import annotations

import time

import docker.errors
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from skiff import config, docker_client
from skiff.rate import RATE
from skiff.secure import secure_route

router = APIRouter()


@router.get("/health", tags=["health"])
async def health():
    """Liveness — never checks Docker to avoid restart loops."""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - config.APP_START_WALL),
        "version": config._APP_VERSION,
    }


# /ready hits the Docker API. Rate-limit per client IP so an outside caller
# can't turn the probe into an unbounded-Docker-calls amplifier. /health
# and /api/auth-required stay unlimited — they're pure in-memory responses
# that orchestrators hit at high frequency.
@router.get("/ready", tags=["health"])
@secure_route.public(RATE.PUBLIC)
def ready(request: Request):
    """Readiness — returns 503 if Docker is unreachable."""
    try:
        client = docker_client.get_client()
        info = client.info()
        return {
            "status": "ready",
            "docker_version": info.get("ServerVersion", "unknown"),
            "containers_running": info.get("ContainersRunning", 0),
        }
    except (docker.errors.DockerException, OSError):
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "Docker engine unreachable"},
        )


@router.get("/api/auth-required", tags=["auth"])
@secure_route.public(RATE.PUBLIC)
def auth_required(request: Request):
    """Returns whether auth is required. No secrets exposed — callable pre-login.

    Rate-limited at the PUBLIC tier so an outside caller can't turn
    repeated polls into a cheap discovery loop.
    """
    return {"required": bool(config._cfg.api_token)}
