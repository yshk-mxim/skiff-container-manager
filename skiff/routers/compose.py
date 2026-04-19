# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker Compose stack operations."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
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

# Per-project mutex for the compose_up / compose_down pipeline.
# Two concurrent POST /api/compose/up calls to the SAME project_name
# would otherwise race at the filesystem layer: each read
# `was_created = _find_project_dir(...) is None` BEFORE the other's
# `_resolve_project_dir` created the directory, and if one failed,
# both would attempt `rmtree` on the shared dir. Rate limits narrow
# this, but a per-project lock gives correct serialisation even if an
# operator double-submits the form. Keyed by `safe_name` so only
# collisions on the same project serialise; different projects run
# in parallel.
_project_locks_guard = threading.Lock()
_project_locks: dict[str, threading.Lock] = {}


def _lock_for_project(safe_name: str) -> threading.Lock:
    with _project_locks_guard:
        lock = _project_locks.get(safe_name)
        if lock is None:
            lock = threading.Lock()
            _project_locks[safe_name] = lock
        return lock


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
        stacks[project]["services"].append(
            {
                "name": service,
                "container_id": c.short_id,
                "status": c.status,
                "state": svc_state,
            }
        )
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


def _find_project_dir(safe_name: str) -> Path | None:
    """Return the existing project dir for `safe_name`, or None.

    Read-only helper. Safe to use from compose_down (we don't want a
    `down` on a never-deployed project to silently create an empty
    project dir). Derives the Path from the filesystem (iterdir +
    name==safe_name) to break CodeQL `py/path-injection` taint.
    """
    if not config.COMPOSE_DIR.exists():
        return None
    compose_root = config.COMPOSE_DIR.resolve()
    for entry in compose_root.iterdir():
        if entry.is_dir() and entry.name == safe_name:
            return entry
    return None


def _resolve_project_dir(safe_name: str) -> Path:
    """Return the project dir for `safe_name`, creating it if needed.

    Used by the `up` code path, which WANTS to create the dir on first
    deploy. `compose_down` should call `_find_project_dir` instead.
    """
    config.COMPOSE_DIR.mkdir(parents=True, exist_ok=True)
    found = _find_project_dir(safe_name)
    if found is not None:
        return found
    compose_root = config.COMPOSE_DIR.resolve()
    (compose_root / safe_name).mkdir(parents=True, exist_ok=True)  # lgtm[py/path-injection]
    found = _find_project_dir(safe_name)
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
    compose_path: Path,
    uploaded: UploadFile | None,
    project_name: str,
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
            project=project_name,
            services=_services_summary(parsed),
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
            capture_output=True,
            text=True,
            env=_compose_subprocess_env(),
            timeout=config.COMPOSE_UP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise http_error("compose.timeout", message="Compose up timed out (2 min limit)") from exc


@router.post("/api/compose/up", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.AUTH_SENSITIVE,
    audit="compose.up",
    audit_fields=lambda request, file=None, project_name="dev", **kw: (  # noqa: ARG005
        {"project": project_name}
    ),
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
    # Serialise concurrent compose_up calls for the SAME project so two
    # accidental double-submits don't race at the filesystem layer
    # (see `_lock_for_project` docstring).
    with _lock_for_project(safe_name):
        # Track whether THIS request created the project dir so we can
        # clean it up on validation failure. Otherwise a rejected upload
        # (privileged / host mount / network_mode:host) leaves an empty
        # directory on disk that appears as a phantom stack in the UI.
        was_created = _find_project_dir(safe_name) is None
        project_dir = _resolve_project_dir(safe_name)
        compose_path = project_dir / "docker-compose.yml"
        try:
            _prepare_compose_file(compose_path, file, project_name)
            result = _run_compose_up(compose_path, project_name)
        except Exception:
            # Only tear down the dir we created in this request. If a
            # previous deploy left a working stack here, don't disturb
            # it on a validation failure of the NEW upload.
            if was_created:
                shutil.rmtree(project_dir, ignore_errors=True)
            raise
        if result.returncode != 0:
            log.warning("compose.up_failed", project=project_name, stderr=result.stderr[:500])
            detail = (
                validators._sanitize_stderr(result.stderr)
                if result.stderr
                else "Compose deployment failed. Check compose file syntax and image availability."
            )
            # Compose itself refused the file (e.g. image not found).
            # Same cleanup rule: only remove the dir if we just created it.
            if was_created:
                shutil.rmtree(project_dir, ignore_errors=True)
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
        containers = [c for c in containers if (c.labels or {}).get("com.docker.compose.service") == service_name]
    if not containers:
        raise http_error("compose.not_found")
    return containers


@router.get("/api/compose/{project_name}/download", dependencies=AUTH, tags=["compose"])
@secure_route.read(RATE.READ)
def compose_download_yaml(request: Request, project_name: str):
    """Serve the stored `docker-compose.yml` for a deployed project.

    Useful for backups or for pasting into another SKIFF / CI pipeline.
    Returns 404 if the project has never been deployed here."""
    from fastapi.responses import FileResponse

    validators.validate_project_name(project_name)
    safe_name = _sanitize_project_name(project_name)
    project_dir = _find_project_dir(safe_name)
    if project_dir is None:
        raise http_error("compose.not_found", project=project_name)
    compose_path = project_dir / "docker-compose.yml"
    if not compose_path.exists():
        raise http_error("compose.file_missing")
    # Stream from disk — the file has passed every policy check at deploy
    # time (size, forbidden keys, registry allowlist), so downloading it
    # back out is safe regardless of caller trust.
    return FileResponse(
        path=str(compose_path),
        media_type="application/x-yaml",
        filename=f"{project_name}.docker-compose.yml",
    )


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
    dependencies=AUTH,
    tags=["compose"],
)
@secure_route.mutate(
    RATE.WRITE,
    audit="compose.service_restarted",
    audit_fields=lambda request, project_name, service_name, **kw: (  # noqa: ARG005
        {"project": project_name, "service": service_name}
    ),
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
    RATE.AUTH_SENSITIVE,
    audit="compose.down",
    audit_fields=lambda request, project_name="dev", **kw: {"project": project_name},  # noqa: ARG005
)
def compose_down(request: Request, project_name: str = "dev") -> OkResponse:
    """Tear down a running Compose stack."""
    validators.validate_project_name(project_name)
    safe_name = _sanitize_project_name(project_name)
    project_dir = _find_project_dir(safe_name)
    if project_dir is None:
        # No deploy ever happened for this name — return 404 instead of
        # creating an empty project dir just to `docker compose down`
        # against a nonexistent stack (which would fail + leave a dir).
        raise http_error("compose.not_found", project=project_name)
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
            capture_output=True,
            text=True,
            env=_compose_subprocess_env(),
            timeout=config.COMPOSE_DOWN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise http_error("compose.timeout", message="Compose down timed out") from exc
    if result.returncode != 0:
        log.warning("compose.down_failed", project=project_name, stderr=result.stderr[:500])
        detail = validators._sanitize_stderr(result.stderr) if result.stderr else "Compose teardown failed"
        raise http_error("compose.deploy_failed", message=detail)
    # Successful teardown: remove the project dir so `GET /api/compose/stacks`
    # doesn't list a phantom project that has no running containers and no
    # way for the operator to know whether cleanup needs a manual rm -rf.
    shutil.rmtree(project_dir, ignore_errors=True)
    return OkResponse(output=str(result.stdout) if result.stdout is not None else None)


# ── Stack-level lifecycle: stop / start / pull / scale / validate ─────────
#
# All share a common shape: find the project dir, run `docker compose -p
# <project> -f <file> <subcommand>`, surface stderr as a catalogued envelope
# on failure. The helpers below let each endpoint stay ~10 lines.


def _compose_stack_op(
    project_name: str,
    subcommand: list[str],
    timeout: int,
    *,
    require_existing_dir: bool = True,
) -> OkResponse:
    """Run a `docker compose -p <name> -f <file> <subcommand>` invocation.

    Shared with: stop, start, pull, scale, and the stack-level validate.
    Raises `compose.not_found` when the project has never been deployed
    (no project dir on disk) and `require_existing_dir=True`."""
    validators.validate_project_name(project_name)
    safe_name = _sanitize_project_name(project_name)
    project_dir = _find_project_dir(safe_name)
    if project_dir is None and require_existing_dir:
        raise http_error("compose.not_found", project=project_name)
    compose_path = project_dir / "docker-compose.yml" if project_dir else None
    cmd = [*config.COMPOSE_CMD, "-p", project_name]
    if compose_path and compose_path.exists():
        cmd.extend(["-f", str(compose_path)])
    cmd.extend(subcommand)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_compose_subprocess_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise http_error("compose.timeout", message=f"{subcommand[0]} timed out") from exc
    if result.returncode != 0:
        log.warning(
            "compose.subcommand_failed",
            project=project_name,
            subcommand=subcommand[0],
            stderr=result.stderr[:500],
        )
        detail = validators._sanitize_stderr(result.stderr) if result.stderr else f"{subcommand[0]} failed"
        raise http_error("compose.deploy_failed", message=detail)
    return OkResponse(output=str(result.stdout) if result.stdout is not None else None)


@router.post("/api/compose/{project_name}/stop", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.WRITE,
    audit="compose.stopped",
    audit_fields=lambda request, project_name, **kw: {"project": project_name},  # noqa: ARG005
)
def compose_stop(request: Request, project_name: str) -> OkResponse:
    """Stop every service container in a stack without removing them.

    Paired with `/start`. Keeps the containers so a later `/start` brings
    them back with the same state — useful for temporarily halting a dev
    stack without losing per-container state."""
    return _compose_stack_op(project_name, ["stop"], config.COMPOSE_DOWN_TIMEOUT)


@router.post("/api/compose/{project_name}/start", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.WRITE,
    audit="compose.started",
    audit_fields=lambda request, project_name, **kw: {"project": project_name},  # noqa: ARG005
)
def compose_start(request: Request, project_name: str) -> OkResponse:
    """Bring a stopped stack back up. Paired with `/stop`."""
    return _compose_stack_op(project_name, ["start"], config.COMPOSE_DOWN_TIMEOUT)


@router.post("/api/compose/{project_name}/pull", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.WRITE,
    audit="compose.pulled",
    audit_fields=lambda request, project_name, **kw: {"project": project_name},  # noqa: ARG005
)
def compose_pull(request: Request, project_name: str) -> OkResponse:
    """Pull the latest versions of every service image in a stack.

    Runs `docker compose pull` — updates image refs without redeploying.
    A subsequent `/up` picks up the new tags."""
    return _compose_stack_op(project_name, ["pull"], config.IMAGE_PULL_TIMEOUT * 2)


@router.post("/api/compose/{project_name}/scale", dependencies=AUTH, tags=["compose"])
@secure_route.mutate(
    RATE.WRITE,
    audit="compose.scaled",
    audit_fields=lambda request, project_name, service_name, replicas, **kw: (  # noqa: ARG005
        {"project": project_name, "service": service_name, "replicas": replicas}
    ),
)
def compose_scale(
    request: Request,
    project_name: str,
    service_name: str,
    replicas: int,
) -> OkResponse:
    """Scale a service to N replicas.

    `replicas` is bounded by `COMPOSE_MAX_REPLICAS` (host policy) to
    prevent a runaway ask from exhausting the engine. Docker compose
    itself will reject N=0 on some versions — we allow it here for
    "stop without removing the service definition"."""
    if not validators.SERVICE_NAME_RE.fullmatch(service_name):
        raise http_error("validation.bad_input")
    max_replicas = config.COMPOSE_MAX_REPLICAS
    if replicas < 0 or replicas > max_replicas:
        raise http_error(
            "validation.bad_input",
            message=f"replicas must be between 0 and {max_replicas}",
        )
    spec = f"{service_name}={replicas}"
    return _compose_stack_op(
        project_name,
        ["up", "-d", "--scale", spec, "--no-recreate"],
        config.COMPOSE_UP_TIMEOUT,
    )
