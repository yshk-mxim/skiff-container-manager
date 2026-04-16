# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Container lifecycle routes and WebSocket log streaming / exec shell."""
from __future__ import annotations

import asyncio
import collections
import re
import threading
import time

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from skiff.auth import (
    AUTH,
    _check_session_age,
    _validate_ws_token_from_message,
    _validate_ws_origin,
    verify_csrf,
    ws_keepalive,
)
from skiff.config import (
    CONTAINER_RESTART_TIMEOUT,
    CONTAINER_STATS_TIMEOUT,
    CONTAINER_STOP_TIMEOUT,
    MAX_CONTAINER_CPU,
    MAX_CONTAINER_MEM,
    MAX_CONTAINERS,
    MAX_LOG_TAIL,
    MAX_PORT_MAPPINGS,
    MAX_RESTART_RETRIES,
    MAX_VOLUME_NAME_LENGTH,
    PRIVILEGED_PORT_THRESHOLD,
    RL_DEFAULT,
    RL_FAST,
    RL_SLOW,
    WS_EXEC_IDLE_TIMEOUT,
    WS_EXEC_RECV_TIMEOUT,
    WS_KEEPALIVE_INTERVAL,
    WS_KEEPALIVE_REVALIDATE_EVERY,
    WS_LOG_IDLE_TIMEOUT,
    WS_LOG_TAIL,
    WS_MAX_PER_IP,
    _cfg,
    _limit,
    limiter,
)
from skiff.docker_client import docker_client_dep, get_client
from skiff.validators import (
    CONTAINER_ID_RE,
    NETWORK_NAME_RE,
    _get_container,
    _redact_env,
    _validate_mount_target,
    safe_docker_call,
    validate_container_id,
    validate_container_name,
    validate_image_registry,
)
from starlette.websockets import WebSocket

log = structlog.get_logger(__name__)
router = APIRouter()

# ── Per-IP WebSocket connection rate limiting ──────────────
_ws_connections: dict[str, int] = collections.defaultdict(int)
_ws_lock = threading.Lock()


def _ws_acquire(ip: str) -> None:
    with _ws_lock:
        if _ws_connections[ip] >= WS_MAX_PER_IP:
            raise HTTPException(429, "Too many WebSocket connections from this IP")
        _ws_connections[ip] += 1


def _ws_release(ip: str) -> None:
    with _ws_lock:
        _ws_connections[ip] = max(0, _ws_connections[ip] - 1)


# ── Container routes ───────────────────────────────────────

@router.get("/api/containers", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_FAST))
def list_containers(request: Request, client=Depends(docker_client_dep)) -> list[dict]:
    """Return all containers (running and stopped)."""
    containers = safe_docker_call(client.containers.list, all=True)
    result = []
    for c in containers:
        try:
            image_name = c.image.tags[0] if c.image.tags else c.image.short_id
        except Exception:
            image_name = "unknown"
        result.append({
            "id": c.short_id,
            "name": c.name,
            "image": image_name,
            "status": c.status,
            "state": c.attrs.get("State", {}).get("Status", "unknown"),
            "health": c.attrs.get("State", {}).get("Health", {}).get("Status", "none")
            if isinstance(c.attrs.get("State", {}).get("Health"), dict) else "none",
            "ports": c.ports,
            "created": c.attrs.get("Created", ""),
        })
    return result


@router.post("/api/containers/run", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_SLOW))
def run_container(
    request: Request,
    image: str,
    name: str | None = None,
    ports: dict[str, str] | None = Body(default=None),
    environment: list[str] | None = Body(default=None),
    command: str | None = Body(default=None),
    volumes: list[str] | None = Body(default=None),
    restart_policy: str | None = Body(default=None),
    network: str | None = Body(default=None),
    labels: dict[str, str] | None = Body(default=None),
    read_only: bool = Body(default=True),
    client=Depends(docker_client_dep),
) -> dict:
    """Create and start a new container from a validated registry image."""
    verify_csrf(request)
    validate_image_registry(image)
    validate_container_name(name)

    if ports:
        if len(ports) > MAX_PORT_MAPPINGS:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(400, f"Too many port mappings (max {MAX_PORT_MAPPINGS})")
        for cport, hport in ports.items():
            if not re.match(r"^\d{1,5}(/tcp|/udp)?$", str(cport)):
                from fastapi import HTTPException  # noqa: PLC0415
                raise HTTPException(400, f"Invalid container port format: {str(cport)[:20]}")
            raw_hp = hport
            if isinstance(raw_hp, (list, tuple)) and len(raw_hp) == 2:
                raw_hp = raw_hp[1]
            if raw_hp is not None:
                from fastapi import HTTPException  # noqa: PLC0415
                try:
                    hp = int(str(raw_hp).split(":")[-1])
                except (ValueError, TypeError):
                    raise HTTPException(400, f"Invalid host port: {str(hport)[:20]}") from None
                if hp < PRIVILEGED_PORT_THRESHOLD:
                    raise HTTPException(400, f"Host port {hp} is privileged (<{PRIVILEGED_PORT_THRESHOLD})")

    from fastapi import HTTPException  # noqa: PLC0415
    if environment:
        for env in environment:
            if "=" not in env or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*=", env):
                raise HTTPException(400, f"Invalid environment variable format: {env[:50]}. Use KEY=VALUE.")

    volume_binds = {}
    if volumes:
        for vol in volumes:
            if ":" not in vol:
                raise HTTPException(400, f"Invalid volume format: {vol[:50]}. Use name:/path.")
            parts = vol.split(":", 2)
            vol_name, mount_path = parts[0], parts[1]
            _validate_mount_target(mount_path)
            if vol_name.startswith(("/", "~", "..", "$")):
                raise HTTPException(400, "Host path mounts are not allowed — use named volumes only.")
            if not re.match(rf"^[a-zA-Z0-9][a-zA-Z0-9_.-]{{0,{MAX_VOLUME_NAME_LENGTH}}}$", vol_name):
                raise HTTPException(400, f"Invalid volume name: {vol_name[:50]}")
            mode = parts[2] if len(parts) > 2 and parts[2] in ("ro", "rw") else "rw"
            volume_binds[vol_name] = {"bind": mount_path, "mode": mode}

    valid_restart = {
        "no": {},
        "on-failure": {"Name": "on-failure", "MaximumRetryCount": MAX_RESTART_RETRIES},
        "unless-stopped": {"Name": "unless-stopped"},
        "always": {"Name": "always"},
    }
    rp = valid_restart.get(restart_policy or "no")
    if rp is None:
        raise HTTPException(400, "Invalid restart policy")

    if network and not NETWORK_NAME_RE.match(network):
        raise HTTPException(400, "Invalid network name")

    if labels:
        if len(labels) > 50:
            raise HTTPException(400, "Too many labels (max 50)")
        for lk, lv in labels.items():
            if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$", lk):
                raise HTTPException(400, f"Invalid label key: {lk[:50]}")
            if len(str(lv)) > 4096:
                raise HTTPException(400, f"Label value too long for key: {lk[:50]} (max 4096 chars)")

    existing = len(client.containers.list(all=True))
    if existing >= MAX_CONTAINERS:
        raise HTTPException(400, f"Container limit ({MAX_CONTAINERS}) reached")

    run_kwargs = dict(
        name=name,
        ports=ports,
        environment=environment,
        detach=True,
        mem_limit=MAX_CONTAINER_MEM,
        nano_cpus=int(MAX_CONTAINER_CPU * 1e9),
        security_opt=["no-new-privileges:true"],
        read_only=read_only,
    )
    if command:
        if len(command) > 4096:
            raise HTTPException(400, "Command too long (max 4096 chars)")
        run_kwargs["command"] = command
    if volume_binds:
        run_kwargs["volumes"] = volume_binds
    if restart_policy and restart_policy != "no":
        run_kwargs["restart_policy"] = rp
    if network:
        run_kwargs["network"] = network
    if labels:
        run_kwargs["labels"] = labels

    container = safe_docker_call(client.containers.run, image, **run_kwargs)
    log.info("container.created", id=container.short_id, name=container.name, image=image)
    return {"id": container.short_id, "name": container.name, "status": container.status}


@router.post("/api/containers/{container_id}/start", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def start_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Start a stopped container."""
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.start)
    log.info("container.started", id=container_id)
    return {"ok": True}


@router.post("/api/containers/{container_id}/stop", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def stop_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Stop a running container gracefully (SIGTERM, then SIGKILL after timeout)."""
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.stop, timeout=CONTAINER_STOP_TIMEOUT)
    log.info("container.stopped", id=container_id)
    return {"ok": True}


@router.post("/api/containers/{container_id}/restart", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def restart_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Restart a container."""
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.restart, timeout=CONTAINER_RESTART_TIMEOUT)
    log.info("container.restarted", id=container_id)
    return {"ok": True}


@router.post("/api/containers/{container_id}/pause", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def pause_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Pause (freeze) all processes in a running container."""
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.pause)
    log.info("container.paused", id=container_id)
    return {"ok": True}


@router.post("/api/containers/{container_id}/unpause", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def unpause_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Resume a paused container."""
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.unpause)
    log.info("container.unpaused", id=container_id)
    return {"ok": True}


@router.post("/api/containers/{container_id}/kill", dependencies=AUTH, tags=["containers"])
@limiter.limit("20/minute")
def kill_container(
    request: Request, container_id: str, signal: str = "SIGKILL", client=Depends(docker_client_dep)
) -> dict:
    """Send a signal to a container (default SIGKILL)."""
    from fastapi import HTTPException  # noqa: PLC0415
    verify_csrf(request)
    if signal not in ("SIGKILL", "SIGTERM", "SIGINT", "SIGHUP"):
        raise HTTPException(400, "Invalid signal")
    container = _get_container(client, container_id)
    safe_docker_call(container.kill, signal=signal)
    log.info("container.killed", id=container_id, signal=signal)
    return {"ok": True}


@router.post("/api/containers/{container_id}/rename", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_SLOW))
def rename_container(request: Request, container_id: str, name: str, client=Depends(docker_client_dep)) -> dict:
    """Rename a container."""
    verify_csrf(request)
    validate_container_name(name)
    container = _get_container(client, container_id)
    safe_docker_call(container.rename, name)
    log.info("container.renamed", id=container_id, new_name=name)
    return {"ok": True}


@router.delete("/api/containers/{container_id}", dependencies=AUTH, tags=["containers"])
@limiter.limit("20/minute")
def delete_container(
    request: Request, container_id: str, force: bool = False, client=Depends(docker_client_dep)
) -> dict:
    """Remove a container permanently."""
    verify_csrf(request)
    container = _get_container(client, container_id)
    safe_docker_call(container.remove, force=force)
    log.info("container.deleted", id=container_id, force=force)
    return {"ok": True}


@router.get("/api/containers/{container_id}/logs", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def container_logs(
    request: Request,
    container_id: str,
    tail: int = Query(default=200, le=MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
) -> dict:
    """Fetch container log lines with optional time-range filtering."""
    container = _get_container(client, container_id)
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    logs = safe_docker_call(container.logs, **kwargs)
    return {"logs": logs.decode(errors="replace")}


@router.get("/api/containers/{container_id}/logs/download", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_SLOW))
def download_container_logs(
    request: Request,
    container_id: str,
    tail: int = Query(default=5000, le=MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
):
    """Download container logs as plain text. Auth via Authorization header."""
    container = _get_container(client, container_id)
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    logs = safe_docker_call(container.logs, **kwargs)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', container.name)
    return PlainTextResponse(
        content=logs.decode(errors="replace"),
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-logs.txt"'},
    )


@router.get("/api/containers/{container_id}/logs/download.jsonl", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_SLOW))
def download_container_logs_jsonl(
    request: Request,
    container_id: str,
    tail: int = Query(default=5000, le=MAX_LOG_TAIL, ge=1),
    since: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    until: str = Query(default="", description="ISO 8601 datetime or Unix timestamp"),
    client=Depends(docker_client_dep),
):
    """Download container logs as JSONL (one JSON object per line with timestamp + message)."""
    import json  # noqa: PLC0415
    container = _get_container(client, container_id)
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until
    logs = safe_docker_call(container.logs, **kwargs)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', container.name)
    lines = []
    for line in logs.decode(errors="replace").splitlines():
        if " " in line:
            ts, _, msg = line.partition(" ")
        else:
            ts, msg = "", line
        lines.append(json.dumps({"timestamp": ts, "message": msg}))
    return PlainTextResponse(
        content="\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-logs.jsonl"'},
    )


@router.get("/api/containers/{container_id}/inspect", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def inspect_container(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Return detailed container metadata (config, state, mounts, network, health)."""
    container = _get_container(client, container_id)
    attrs = container.attrs
    return {
        "id": attrs["Id"][:12],
        "name": attrs["Name"].lstrip("/"),
        "image": attrs["Config"]["Image"],
        "created": attrs["Created"],
        "state": attrs["State"],
        "restart_count": attrs.get("RestartCount", 0),
        "platform": attrs.get("Platform", ""),
        "config": {
            "env": _redact_env(attrs["Config"].get("Env", [])),
            "cmd": attrs["Config"].get("Cmd"),
            "entrypoint": attrs["Config"].get("Entrypoint"),
            "labels": attrs["Config"].get("Labels", {}),
            "exposed_ports": list(attrs["Config"].get("ExposedPorts", {}).keys()),
            "working_dir": attrs["Config"].get("WorkingDir", ""),
            "user": attrs["Config"].get("User", ""),
        },
        "host_config": {
            "port_bindings": attrs.get("HostConfig", {}).get("PortBindings", {}),
            "restart_policy": attrs.get("HostConfig", {}).get("RestartPolicy", {}),
            "binds": attrs.get("HostConfig", {}).get("Binds", []),
            "memory": attrs.get("HostConfig", {}).get("Memory", 0),
            "cpu_quota": attrs.get("HostConfig", {}).get("CpuQuota", 0),
        },
        "network": {
            net: {
                "ip_address": info.get("IPAddress", ""),
                "gateway": info.get("Gateway", ""),
                "mac_address": info.get("MacAddress", ""),
            }
            for net, info in attrs.get("NetworkSettings", {}).get("Networks", {}).items()
        },
        "mounts": [
            {
                "type": m.get("Type", ""),
                "name": m.get("Name", ""),
                "source": m.get("Source", ""),
                "destination": m.get("Destination", ""),
                "mode": m.get("Mode", ""),
                "rw": m.get("RW", True),
            }
            for m in attrs.get("Mounts", [])
        ],
    }


@router.get("/api/containers/{container_id}/stats", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
async def container_stats(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """Return real-time CPU, memory, network, and disk I/O stats."""
    from fastapi import HTTPException  # noqa: PLC0415
    import asyncio  # noqa: PLC0415
    container = _get_container(client, container_id)
    try:
        loop = asyncio.get_running_loop()
        raw = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: container.stats(stream=False)),
            timeout=CONTAINER_STATS_TIMEOUT,
        )
    except TimeoutError as exc:
        raise HTTPException(504, "Stats call timed out") from exc
    # CPU delta calculation
    cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    sys_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw["precpu_stats"].get("system_cpu_usage", 0)
    num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
    cpu_pct = (cpu_delta / sys_delta * num_cpus * 100.0) if sys_delta > 0 else 0.0

    mem = raw.get("memory_stats", {})
    mem_usage = mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)
    mem_limit = mem.get("limit", 0)
    mem_pct = (mem_usage / mem_limit * 100.0) if mem_limit > 0 else 0.0

    nets = raw.get("networks", {})
    net_rx = sum(v.get("rx_bytes", 0) for v in nets.values())
    net_tx = sum(v.get("tx_bytes", 0) for v in nets.values())

    bio = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    blk_r = sum(b.get("value", 0) for b in bio if b.get("op") == "read")
    blk_w = sum(b.get("value", 0) for b in bio if b.get("op") == "write")

    return {
        "cpu_percent": round(cpu_pct, 2),
        "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
        "mem_percent": round(mem_pct, 2),
        "net_rx_mb": round(net_rx / 1024 / 1024, 3),
        "net_tx_mb": round(net_tx / 1024 / 1024, 3),
        "blk_read_mb": round(blk_r / 1024 / 1024, 3),
        "blk_write_mb": round(blk_w / 1024 / 1024, 3),
    }


@router.get("/api/containers/{container_id}/top", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def container_top(request: Request, container_id: str, client=Depends(docker_client_dep)) -> dict:
    """List processes running inside a container (like docker top)."""
    container = _get_container(client, container_id)
    result = safe_docker_call(container.top)
    return {"titles": result.get("Titles", []), "processes": result.get("Processes", [])}


@router.get("/api/containers/{container_id}/diff", dependencies=AUTH, tags=["containers"])
@limiter.limit(_limit(RL_DEFAULT))
def container_diff(request: Request, container_id: str, client=Depends(docker_client_dep)) -> list[dict]:
    """Show filesystem changes in a container's writable layer since it was created."""
    container = _get_container(client, container_id)
    changes = safe_docker_call(container.diff) or []
    kind_map = {0: "modified", 1: "added", 2: "deleted"}
    return [{"path": c.get("Path", ""), "kind": kind_map.get(c.get("Kind", 0), "unknown")} for c in changes]


# ── WebSocket: log streaming ───────────────────────────────

@router.websocket("/ws/logs/{container_id}")
async def stream_logs(websocket: WebSocket, container_id: str):
    """Stream container logs in real time over WebSocket."""
    if not _validate_ws_origin(websocket):
        await websocket.close(code=4003)
        return
    if not CONTAINER_ID_RE.match(container_id):
        await websocket.close(code=4000)
        return
    await websocket.accept()
    if not await _validate_ws_token_from_message(websocket):
        await websocket.close(code=4003)
        return
    ip = websocket.client.host if websocket.client else "unknown"
    _ws_acquire(ip)
    log.info("audit.ws_logs", container=container_id, remote=ip)
    try:
        loop = asyncio.get_running_loop()
        client = await loop.run_in_executor(None, get_client)
        container = await loop.run_in_executor(None, client.containers.get, container_id)
        gen = container.logs(stream=True, follow=True, tail=WS_LOG_TAIL, timestamps=True)

        async def read_logs():
            while True:
                try:
                    line = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: next(gen, None)),
                        timeout=WS_LOG_IDLE_TIMEOUT,
                    )
                    if line is None:
                        break
                    await websocket.send_text(line.decode(errors="replace"))
                except TimeoutError:
                    await websocket.send_text("\n[Idle timeout — no new logs for 5 minutes]\n")
                    break
                except StopIteration:
                    break

        read_task = asyncio.create_task(read_logs())
        keepalive_task = asyncio.create_task(ws_keepalive(websocket))
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            pass
        finally:
            read_task.cancel()
            keepalive_task.cancel()
            try:
                gen.close()
            except Exception:
                pass
    except Exception as exc:
        log.warning("ws.logs_error", container=container_id, error=str(exc))
    finally:
        _ws_release(ip)
        try:
            await websocket.close()
        except Exception:
            pass


# ── WebSocket: interactive exec shell ─────────────────────

@router.websocket("/ws/exec/{container_id}")
async def exec_shell(websocket: WebSocket, container_id: str):
    """Open an interactive shell in a container over WebSocket."""
    if not _validate_ws_origin(websocket):
        await websocket.close(code=4003)
        return
    if not CONTAINER_ID_RE.match(container_id):
        await websocket.close(code=4000)
        return
    await websocket.accept()
    if not await _validate_ws_token_from_message(websocket):
        await websocket.close(code=4003)
        return
    ip = websocket.client.host if websocket.client else "unknown"
    _ws_acquire(ip)
    log.info("audit.ws_exec", container=container_id, remote=ip)
    try:
        loop = asyncio.get_running_loop()
        client = await loop.run_in_executor(None, get_client)
        container = await loop.run_in_executor(None, client.containers.get, container_id)
        shell = "/bin/sh"
        try:
            exit_code, _ = container.exec_run("which /bin/bash", demux=True)
            if exit_code == 0:
                shell = "/bin/bash"
        except Exception:
            pass
        exec_id = client.api.exec_create(container.id, shell, stdin=True, tty=True, stdout=True, stderr=True)
        sock = client.api.exec_start(exec_id, socket=True, tty=True)
        sock._sock.setblocking(True)
        sock._sock.settimeout(WS_EXEC_RECV_TIMEOUT)

        async def read_output():
            idle_since = time.monotonic()
            while True:
                try:
                    data = await loop.run_in_executor(None, sock._sock.recv, 4096)
                    if not data:
                        break
                    idle_since = time.monotonic()
                    await websocket.send_text(data.decode(errors="replace"))
                except TimeoutError:
                    if time.monotonic() - idle_since > WS_EXEC_IDLE_TIMEOUT:
                        await websocket.send_text("\r\n[Session idle timeout — 10 minutes]\r\n")
                        break
                    continue
                except Exception:
                    break

        read_task = asyncio.create_task(read_output())
        keepalive_task = asyncio.create_task(ws_keepalive(websocket))
        try:
            while True:
                data = await websocket.receive_text()
                if len(data) > 65536:
                    await websocket.close(code=4008)
                    break
                log.info("audit.ws_exec_input", container=container_id, remote=ip, cmd_preview=data[:120])
                await loop.run_in_executor(None, sock._sock.sendall, data.encode())
        except Exception:
            pass
        finally:
            read_task.cancel()
            keepalive_task.cancel()
            sock.close()
            log.info("audit.ws_exec_disconnect", container=container_id, remote=ip)
    except Exception as exc:
        log.warning("ws.exec_error", container=container_id, error=str(exc))
    finally:
        _ws_release(ip)
        try:
            await websocket.close()
        except Exception:
            pass
