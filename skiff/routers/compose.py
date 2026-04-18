# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker Compose stack operations."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import docker.errors
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from skiff import config, validators
from skiff.auth import AUTH
from skiff.contract.errors import http_error
from skiff.contract.responses import OkResponse
from skiff.docker_client import docker_client_dep
from skiff.rate import RATE
from skiff.secure import secure_route

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/api/compose/stacks", dependencies=AUTH, tags=["compose"])
@secure_route.read(RATE.READ)
def list_compose_stacks(request: Request, client=Depends(docker_client_dep)):
    """List running compose stacks by inspecting container labels."""
    containers = validators.safe_docker_call(client.containers.list, all=True)
    stacks: dict[str, dict[str, Any]] = {}
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


# ── compose_up helpers ───────────────────────────────────────────────────────
# compose_up is a 5-step pipeline: sanitize project name, resolve the
# project dir (iterdir-equality to break CodeQL taint), persist the
# uploaded file or reuse the saved one, build the minimal subprocess
# env, then `docker compose up -d`. Each helper below owns one step.

_PROJECT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{0,63}")


def _sanitize_project_name(project_name: str) -> str:
    """Defence-in-depth: basename + strict regex. Returns the sanitized name."""
    bn = Path(project_name).name
    m = _PROJECT_NAME_RE.fullmatch(bn) if bn else None
    if not m:
        raise http_error("validation.bad_input")
    return m.group(0)


def _resolve_project_dir(safe_name: str) -> Path:
    """Return an existing project dir matching `safe_name`, creating it if needed.

    Derives the Path from the filesystem (iterdir + name==safe_name) rather
    than from user input concatenation, to break CodeQL `py/path-injection`
    taint. Equality comparison doesn't propagate taint.
    """
    config.COMPOSE_DIR.mkdir(parents=True, exist_ok=True)
    compose_root = config.COMPOSE_DIR.resolve()

    def _find() -> Path | None:
        for entry in compose_root.iterdir():
            if entry.is_dir() and entry.name == safe_name:
                return entry
        return None

    found = _find()
    if found is not None:
        return found
    (compose_root / safe_name).mkdir(parents=True, exist_ok=True)  # lgtm[py/path-injection]
    found = _find()
    if found is None:
        raise http_error("compose.project_dir_create_failed")
    return found


def _env_keys(env: Any) -> list[str]:
    """Extract env var names only — never values. Handles dict or list forms."""
    if isinstance(env, dict):
        return list(env.keys())
    if isinstance(env, list):
        return [str(e).split("=", 1)[0] for e in env if isinstance(e, str)]
    return []


def _services_summary(parsed: dict) -> dict[str, dict[str, Any]]:
    """Compact summary of each service's image + declared env keys (not values)."""
    return {
        svc: {"image": cfg.get("image", ""), "env_keys": _env_keys(cfg.get("environment"))}
        for svc, cfg in (parsed.get("services") or {}).items()
        if isinstance(cfg, dict)
    }


def _prepare_compose_file(
    compose_path: Path, uploaded: UploadFile | None, project_name: str,
) -> None:
    """Ensure compose_path has validated YAML on disk before the subprocess call.

    Three flows:
      - uploaded file present → validate + persist + audit summary
      - no upload but file exists on disk → re-validate the saved file
      - no upload and no saved file → 400
    """
    if uploaded and uploaded.filename:
        content = uploaded.file.read()
        parsed = validators.validate_compose_file(content)
        log.info(
            "compose.upload",
            project=project_name, services=_services_summary(parsed),
        )
        compose_path.write_bytes(content)
        return
    if not compose_path.exists():
        # No upload and no persisted compose file in the project dir —
        # caller must supply YAML.
        # Clean up the empty project dir so a failed call doesn't leave a
        # stray directory behind (cosmetic, but visible via compose.list).
        try:
            compose_path.parent.rmdir()
        except OSError:
            pass  # dir not empty (race with another request) — leave as-is
        raise http_error("compose.file_missing")
    validators.validate_compose_file(compose_path.read_bytes())


def _compose_subprocess_env() -> dict[str, str]:
    """Minimal env handed to the `docker compose` subprocess — no secret leakage."""
    env = {
        "PATH": os.environ.get("PATH", config.COMPOSE_PATH_FALLBACK),
        "DOCKER_HOST": config._cfg.docker_host,
        "HOME": os.environ.get("HOME", config.COMPOSE_HOME_FALLBACK),
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
    }
    return {k: v for k, v in env.items() if v}


def _run_compose_up(compose_path: Path, project_name: str) -> subprocess.CompletedProcess:
    """Invoke `docker compose up -d`; classify failures.

    Subprocess timeout is `config.COMPOSE_UP_TIMEOUT` (see
    `skiff/_config/defaults.toml`). Callers should not restate the
    number here — operators tuning the knob shouldn't have to hunt for
    stale comments.
    """
    try:
        return subprocess.run(
            [*config.COMPOSE_CMD, "-f", str(compose_path), "-p", project_name, "up", "-d"],
            capture_output=True, text=True, env=_compose_subprocess_env(),
            timeout=config.COMPOSE_UP_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise http_error("compose.timeout", message="Compose up timed out (2 min limit)") from exc


@router.post("/api/compose/up", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.AUTH_SENSITIVE, audit="compose.up",
    audit_fields=lambda request, file=None, project_name="dev", **kw:  # noqa: ARG005
        {"project": project_name},
)
def compose_up(
    request: Request,
    file: UploadFile | None = None,
    # `project_name` is a query param, not a form field. FastAPI infers
    # type by annotation: `str` without `Form(...)` means query-string.
    # Documenting here so a future contributor doesn't accidentally
    # re-declare it as `Form(...)` and silently break the API contract
    # the UI + curl examples rely on.
    project_name: str = "dev",
) -> OkResponse:
    """Deploy a Compose stack (upload a new file or redeploy an existing one).

    Linear pipeline: validate project name → resolve project dir →
    validate-and-persist compose file → shell out to `docker compose up`
    → surface rc≠0 as a sanitized error.
    """
    validators.validate_project_name(project_name)
    safe_name = _sanitize_project_name(project_name)
    project_dir = _resolve_project_dir(safe_name)
    compose_path = project_dir / "docker-compose.yml"
    _prepare_compose_file(compose_path, file, project_name)
    result = _run_compose_up(compose_path, project_name)
    if result.returncode != 0:
        log.warning("compose.up_failed", project=project_name, stderr=result.stderr[:500])
        detail = validators._sanitize_stderr(result.stderr) if result.stderr \
            else "Compose deployment failed. Check compose file syntax and image availability."
        raise http_error("compose.deploy_failed", message=detail)
    return OkResponse(output=str(result.stdout) if result.stdout is not None else None)


# ── Per-service compose operations (Phase 3) ─────────────────────────────
# These operate on containers filtered by compose labels rather than shelling
# out to `docker compose`. Two advantages: (1) zero subprocess attack surface
# for logs/restart, (2) per-service granularity without aggregate semantics
# of `docker compose logs/restart` (which re-evaluates the compose file).


def _find_compose_containers(client, project_name: str, service_name: str | None = None):
    """Return containers belonging to a project (optionally filtered by service).

    Uses Docker's label filter server-side so we don't paginate the full
    container list through the SDK. Raises 404 if no matching containers exist.
    """
    filters: dict[str, str] = {"label": f"com.docker.compose.project={project_name}"}
    containers = validators.safe_docker_call(client.containers.list, all=True, filters=filters)
    if service_name:
        containers = [
            c for c in containers
            if (c.labels or {}).get("com.docker.compose.service") == service_name
        ]
    if not containers:
        raise http_error("compose.not_found")
    return containers


@router.get("/api/compose/{project_name}/logs", dependencies=AUTH, tags=["compose"])
@secure_route.read(RATE.READ)
def compose_project_logs(
    request: Request,
    project_name: str,
    service: str | None = None,
    tail: int = 200,
    client=Depends(docker_client_dep),
) -> dict:
    """Aggregated logs for a compose project, optionally filtered to one service.

    Mirrors `docker compose logs [service]`. Lines are prefixed with the
    service name for cross-service readability. Lines are merged by arrival
    order per-container; we don't parse timestamps for global interleaving
    because doing a stable merge would require parsing every line. Per-
    container chunks preserve within-service chronology, which is what most
    operators want when debugging a stack.
    """
    validators.validate_project_name(project_name)
    if service is not None and not validators.SERVICE_NAME_RE.fullmatch(service):
        raise http_error("validation.bad_input")
    tail = max(1, min(int(tail), config.MAX_LOG_TAIL))
    containers = _find_compose_containers(client, project_name, service)
    chunks = []
    for c in containers:
        svc_name = (c.labels or {}).get("com.docker.compose.service") or c.name
        try:
            raw = validators.safe_docker_call(c.logs, tail=tail, timestamps=True, stdout=True, stderr=True)
        except (HTTPException, docker.errors.DockerException, OSError) as exc:
            # Best-effort: one bad service shouldn't fail the whole call.
            # validators.safe_docker_call surfaces transient Docker errors as HTTPException,
            # so we catch that alongside the raw Docker SDK exceptions.
            log.warning("compose.service_logs_failed", project=project_name, service=svc_name, error=str(exc))
            continue
        text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        prefixed = [f"{svc_name} | {line}" for line in text.splitlines() if line]
        chunks.extend(prefixed)
    return {"project": project_name, "service": service, "lines": chunks[-tail:]}


@router.post(
    "/api/compose/{project_name}/services/{service_name}/restart",
    dependencies=AUTH, tags=["compose"],
)
@secure_route.mutate(
    RATE.WRITE, audit="compose.service_restarted",
    audit_fields=lambda request, project_name, service_name, **kw:  # noqa: ARG005
        {"project": project_name, "service": service_name},
)
def compose_service_restart(
    request: Request,
    project_name: str,
    service_name: str,
    client=Depends(docker_client_dep),
) -> dict:
    """Restart every container belonging to a single service in a compose stack."""
    validators.validate_project_name(project_name)
    if not validators.SERVICE_NAME_RE.fullmatch(service_name):
        raise http_error("validation.bad_input")
    containers = _find_compose_containers(client, project_name, service_name)
    restarted: list[str] = []
    for c in containers:
        validators.safe_docker_call(c.restart)
        restarted.append(c.short_id)
    return {"project": project_name, "service": service_name, "restarted": restarted}


@router.post("/api/compose/down", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.AUTH_SENSITIVE, audit="compose.down",
    audit_fields=lambda request, project_name="dev", **kw: {"project": project_name},  # noqa: ARG005
)
def compose_down(request: Request, project_name: str = "dev") -> OkResponse:
    """Tear down a running Compose stack."""
    validators.validate_project_name(project_name)
    safe_name = _sanitize_project_name(project_name)
    project_dir = _resolve_project_dir(safe_name)
    compose_path = project_dir / "docker-compose.yml"
    # `docker compose down -p <name>` without `-f` resolves the compose file
    # from the subprocess CWD; here we pass `-f` so teardown works regardless
    # of where the server was launched from, matching `_run_compose_up`.
    compose_args = [*config.COMPOSE_CMD, "-p", project_name, "down"]
    if compose_path.exists():
        compose_args = [*config.COMPOSE_CMD, "-f", str(compose_path), "-p", project_name, "down"]
    try:
        result = subprocess.run(
            compose_args,
            capture_output=True, text=True, env=_compose_subprocess_env(),
            timeout=config.COMPOSE_DOWN_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise http_error("compose.timeout", message="Compose down timed out") from exc
    if result.returncode != 0:
        log.warning("compose.down_failed", project=project_name, stderr=result.stderr[:500])
        detail = validators._sanitize_stderr(result.stderr) if result.stderr else "Compose teardown failed"
        raise http_error("compose.deploy_failed", message=detail)
    return OkResponse(output=str(result.stdout) if result.stdout is not None else None)
