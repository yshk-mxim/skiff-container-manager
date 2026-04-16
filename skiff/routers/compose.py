# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker Compose stack operations."""
from __future__ import annotations

import os
import re
import subprocess

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from skiff.auth import AUTH, verify_csrf
from skiff.config import COMPOSE_CMD, COMPOSE_DIR, RL_DEFAULT, _cfg, _limit, limiter
from skiff.docker_client import docker_client_dep
from skiff.validators import _sanitize_stderr, validate_compose_file, validate_project_name

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/api/compose/stacks", dependencies=AUTH, tags=["compose"])
@limiter.limit(_limit(RL_DEFAULT))
def list_compose_stacks(request: Request, client=Depends(docker_client_dep)):
    """List running compose stacks by inspecting container labels."""
    from skiff.validators import safe_docker_call
    containers = safe_docker_call(client.containers.list, all=True)
    stacks = {}
    for c in containers:
        project = (c.labels or {}).get("com.docker.compose.project", "")
        if not project:
            continue
        if project not in stacks:
            stacks[project] = {"name": project, "services": [], "status": "stopped"}
        service = (c.labels or {}).get("com.docker.compose.service", "")
        svc_state = c.attrs.get("State", {}).get("Status", "unknown")
        stacks[project]["services"].append({
            "name": service,
            "container_id": c.short_id,
            "status": c.status,
            "state": svc_state,
        })
        if svc_state == "running":
            stacks[project]["status"] = "running"
    return list(stacks.values())


@router.post("/api/compose/up", dependencies=AUTH, tags=["compose"])
@limiter.limit(_limit("5/minute"))
def compose_up(request: Request, file: UploadFile | None = None, project_name: str = "dev") -> dict:
    """Deploy a Compose stack (upload or redeploy an existing file)."""
    verify_csrf(request)
    validate_project_name(project_name)

    COMPOSE_DIR.mkdir(parents=True, exist_ok=True)
    # Two-stage path sanitisation — mirrors the pattern used in docker_client.py
    # which CodeQL accepts for py/path-injection:
    #
    # Stage 1: os.path.basename — the CodeQL-recognised path-injection sanitiser.
    #   Strips any directory-traversal components so the result is a flat name.
    # Stage 2: re.fullmatch — enforces the exact character allowlist on the
    #   already-safe basename, producing the final clean value via .group(0).
    _safe_name = os.path.basename(project_name)
    _m = re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}", _safe_name)
    if not _m:
        raise HTTPException(400, "Invalid project name")
    _compose_root = COMPOSE_DIR.resolve()
    project_dir = _compose_root / _m.group(0)
    if not str(project_dir).startswith(str(_compose_root) + os.sep):
        raise HTTPException(400, "Invalid project directory")
    project_dir.mkdir(exist_ok=True)
    compose_path = project_dir / "docker-compose.yml"

    if file and file.filename:
        content = file.file.read()
        parsed = validate_compose_file(content)
        # Audit: log compose structure with sensitive env values redacted
        def _env_keys(env) -> list[str]:
            """Extract env var names only (never values). Handles both dict and list forms."""
            if isinstance(env, dict):
                return list(env.keys())
            if isinstance(env, list):
                return [str(e).split("=", 1)[0] for e in env if isinstance(e, str)]
            return []
        services_summary = {
            svc: {"image": cfg.get("image", ""), "env_keys": _env_keys(cfg.get("environment"))}
            for svc, cfg in (parsed.get("services") or {}).items()
            if isinstance(cfg, dict)
        }
        log.info("compose.upload", project=project_name, services=services_summary)
        compose_path.write_bytes(content)
    elif not compose_path.exists():
        raise HTTPException(400, "No compose file uploaded and no existing file found for this project")
    else:
        # Re-validate existing file on restart (defense against manual tampering)
        validate_compose_file(compose_path.read_bytes())

    minimal_env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "DOCKER_HOST": _cfg.docker_host,
        "HOME": os.environ.get("HOME", "/root"),
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
    }
    minimal_env = {k: v for k, v in minimal_env.items() if v}
    try:
        result = subprocess.run(
            [*COMPOSE_CMD, "-f", str(compose_path), "-p", project_name, "up", "-d"],
            capture_output=True, text=True, env=minimal_env, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Compose up timed out (2 min limit)") from exc
    if result.returncode != 0:
        log.warning("compose.up_failed", project=project_name, stderr=result.stderr[:500])
        detail = _sanitize_stderr(result.stderr) if result.stderr \
            else "Compose deployment failed. Check compose file syntax and image availability."
        raise HTTPException(400, detail)
    log.info("compose.up", project=project_name)
    return {"ok": True, "output": result.stdout}


@router.post("/api/compose/down", dependencies=AUTH, tags=["compose"])
@limiter.limit(_limit("5/minute"))
def compose_down(request: Request, project_name: str = "dev") -> dict:
    """Tear down a running Compose stack."""
    verify_csrf(request)
    validate_project_name(project_name)
    minimal_env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "DOCKER_HOST": _cfg.docker_host,
        "HOME": os.environ.get("HOME", "/root"),
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
    }
    minimal_env = {k: v for k, v in minimal_env.items() if v}
    try:
        result = subprocess.run(
            [*COMPOSE_CMD, "-p", project_name, "down"],
            capture_output=True, text=True, env=minimal_env, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Compose down timed out") from exc
    if result.returncode != 0:
        log.warning("compose.down_failed", project=project_name, stderr=result.stderr[:500])
        detail = _sanitize_stderr(result.stderr) if result.stderr else "Compose teardown failed"
        raise HTTPException(400, detail)
    log.info("compose.down", project=project_name)
    return {"ok": True, "output": result.stdout}
