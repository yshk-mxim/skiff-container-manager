# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker client singleton, SSH tunnel management, and connection utilities.

Imports only from skiff.config to avoid circular imports.
structlog is configured by skiff.logging_setup before this module is used.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import docker
import docker.errors
import requests
import requests.exceptions
import structlog
from fastapi import HTTPException
from requests.adapters import HTTPAdapter

from skiff.config import (
    DOCKER_BACKOFF,
    DOCKER_CLIENT_TIMEOUT,
    DOCKER_PING_TTL,
    DOCKER_POOL_SIZE,
    TCP_KEEPALIVE_COUNT,
    TCP_KEEPALIVE_IDLE,
    TCP_KEEPALIVE_INTERVAL,
    TUNNEL_CONNECT_TIMEOUT,
    TUNNEL_SERVER_ALIVE_COUNT,
    TUNNEL_SERVER_ALIVE_INTERVAL,
    TUNNEL_SOCKET_POLL,
    TUNNEL_SOCKET_WAIT,
    _cfg,
)

log = structlog.get_logger(__name__)

# ── Transient Docker errors ────────────────────────────────
DOCKER_TRANSIENT = (
    docker.errors.DockerException,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    socket.timeout,
    OSError,
)

# ── Docker client singleton ────────────────────────────────
# Note: _client_lock is held during SSH connect (~2-15s). With --workers 1,
# FastAPI runs sync endpoints in a threadpool. If SSH hangs, threads queue
# on the lock until backoff kicks in on subsequent attempts.
_client_lock = threading.Lock()
_client: docker.DockerClient | None = None
_client_failed_at: float = 0.0
_client_last_ping: float = 0.0


def _build_client() -> docker.DockerClient:
    client = docker.DockerClient(
        base_url=_cfg.docker_host,
        timeout=DOCKER_CLIENT_TIMEOUT,
        max_pool_size=DOCKER_POOL_SIZE,
    )
    # TCP keepalive only for TCP-based Docker hosts — Unix socket doesn't need it.
    if _cfg.docker_host.startswith("tcp://") or _cfg.docker_host.startswith("http://"):
        try:
            _ka_opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
            if hasattr(socket, "TCP_KEEPIDLE"):
                _ka_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE))
            if hasattr(socket, "TCP_KEEPINTVL"):
                _ka_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL))
            if hasattr(socket, "TCP_KEEPCNT"):
                _ka_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT))
            _adapter = HTTPAdapter()
            _adapter.poolmanager.connection_pool_kw["socket_options"] = _ka_opts
            client.api.mount("http://", _adapter)
        except Exception:
            pass
    client.ping()
    return client


def get_client() -> docker.DockerClient:
    """Return a live Docker client, reconnecting if necessary."""
    global _client, _client_failed_at, _client_last_ping
    # Phase 1: fast in-memory check under the lock (no I/O).
    with _client_lock:
        now = time.monotonic()
        if _client is None and (now - _client_failed_at) < DOCKER_BACKOFF:
            raise docker.errors.DockerException("Docker connection in backoff")
        if _client is not None and (now - _client_last_ping) < DOCKER_PING_TTL:
            return _client
        candidate = _client

    # Phase 2: network I/O (ping or connect) — done WITHOUT holding the lock so
    # other threads are not blocked during the SSH round-trip.
    if candidate is not None:
        try:
            candidate.ping()
            with _client_lock:
                if _client is candidate:
                    _client_last_ping = time.monotonic()
            return candidate
        except Exception:
            log.warning("docker.client_stale", action="reconnecting")
            try:
                candidate.close()
            except Exception:
                pass
            with _client_lock:
                if _client is candidate:
                    _client = None

    # Phase 3: build a fresh client.
    try:
        new_client = _build_client()
    except Exception as exc:
        with _client_lock:
            _client = None
            _client_failed_at = time.monotonic()
        log.error("docker.connection_failed", host=_cfg.docker_host, error=str(exc))
        raise
    with _client_lock:
        if _client is None:
            _client = new_client
            _client_last_ping = time.monotonic()
            log.info("docker.connected", host=_cfg.docker_host)
        else:
            try:
                new_client.close()
            except Exception:
                pass
        return _client


def _invalidate_client() -> None:
    """Close and discard the current Docker client (forces reconnect on next call)."""
    global _client, _client_last_ping
    with _client_lock:
        _client_last_ping = 0.0
        if _client:
            try:
                _client.close()
            except Exception:
                pass
        _client = None


def docker_client_dep() -> docker.DockerClient:
    """FastAPI dependency that returns a live Docker client or raises HTTP 503."""
    try:
        return get_client()
    except Exception as exc:
        raise HTTPException(503, "Container engine unreachable") from exc


# ── SSH Tunnel Management ──────────────────────────────────
_SSH_TARGET_RE = re.compile(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$")
_tunnel_lock = threading.Lock()
_tunnel_ctl_sock: str = ""
_tunnel_ssh_target: str = ""
_tunnel_socket_path: str = ""


def _stop_tunnel_locked() -> None:
    """Stop the managed SSH tunnel. Must be called with _tunnel_lock held."""
    global _tunnel_ctl_sock, _tunnel_ssh_target, _tunnel_socket_path
    if _tunnel_ctl_sock and _tunnel_ssh_target:
        try:
            subprocess.run(
                ["ssh", "-S", _tunnel_ctl_sock, "-O", "exit", _tunnel_ssh_target],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except Exception:
            pass
        for path in (_tunnel_ctl_sock, _tunnel_socket_path):
            try:
                if path and os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
    _tunnel_ctl_sock = ""
    _tunnel_ssh_target = ""
    _tunnel_socket_path = ""


def _start_tunnel(ssh_target: str, socket_path: str) -> None:
    """Start an SSH ControlMaster tunnel. Raises ValueError on failure."""
    global _tunnel_ctl_sock, _tunnel_ssh_target, _tunnel_socket_path
    with _tunnel_lock:
        _stop_tunnel_locked()
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        ctl_sock = f"/tmp/skiff-tunnel-ctl-{os.getpid()}.sock"  # noqa: S108
        cmd = [
            "ssh", "-fNM",
            "-S", ctl_sock,
            "-o", "ControlPersist=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={TUNNEL_CONNECT_TIMEOUT}",
            "-o", f"ServerAliveInterval={TUNNEL_SERVER_ALIVE_INTERVAL}",
            "-o", f"ServerAliveCountMax={TUNNEL_SERVER_ALIVE_COUNT}",
            "-L", f"{socket_path}:/var/run/docker.sock",
            ssh_target,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=TUNNEL_CONNECT_TIMEOUT + 5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("SSH connection timed out") from exc
        except FileNotFoundError as exc:
            raise ValueError("ssh binary not found") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise ValueError(f"SSH failed: {stderr[:200] or 'unknown error'}")
        deadline = time.monotonic() + TUNNEL_SOCKET_WAIT
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                break
            time.sleep(TUNNEL_SOCKET_POLL)
        else:
            raise ValueError(f"Tunnel socket did not appear at {socket_path}")
        _tunnel_ctl_sock = ctl_sock
        _tunnel_ssh_target = ssh_target
        _tunnel_socket_path = socket_path
        log.info("tunnel.started", target=ssh_target, socket=socket_path)


def _stop_tunnel() -> None:
    """Stop the managed SSH tunnel (public, acquires lock)."""
    with _tunnel_lock:
        _stop_tunnel_locked()


def get_tunnel_socket_path() -> str:
    """Return the currently active tunnel socket path (empty if no tunnel active)."""
    return _tunnel_socket_path
