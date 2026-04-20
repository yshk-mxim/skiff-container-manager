# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Docker client singleton, SSH tunnel management, and connection utilities.

Imports only from skiff.config to avoid circular imports.
structlog is configured by skiff.logging_setup before this module is used.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Self

import docker
import docker.errors
import requests
import requests.exceptions
import structlog
from requests.adapters import HTTPAdapter

from skiff import config
from skiff.contract.errors import http_error

log = structlog.get_logger(__name__)


def _close_fd_quiet(fd: int) -> None:
    """Best-effort os.close(); swallows the one error os.close can raise."""
    with contextlib.suppress(OSError):
        os.close(fd)


# ── Transient Docker errors ────────────────────────────────
# Narrow set: only errors that realistically mean "daemon is temporarily
# unreachable over the transport". Bare `OSError` is deliberately OUT —
# disk-full, EMFILE, etc. should surface as 500, not be downgraded to 503.
import urllib3.exceptions  # noqa: E402 — grouped with transient transports

DOCKER_TRANSIENT = (
    docker.errors.DockerException,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    socket.timeout,
    ConnectionError,
    TimeoutError,
    # Mid-request tunnel death (ssh killed during a stream) surfaces as
    # urllib3.exceptions.ProtocolError, not DockerException. Without it
    # in this tuple, `safe_docker_call` missed the retry path and the
    # error leaked as a plain 500 + traceback past SKIFF's middleware.
    urllib3.exceptions.ProtocolError,
)

# ── Docker client singleton ────────────────────────────────
# Note: _client_lock is held during SSH connect (~2-15s). With --workers 1,
# FastAPI runs sync endpoints in a threadpool. If SSH hangs, threads queue
# on the lock until backoff kicks in on subsequent attempts.
_client_lock = threading.Lock()
_client: docker.DockerClient | None = None
_client_failed_at: float = 0.0
_client_last_ping: float = 0.0


_TCP_KEEPALIVE_OPTIONS: tuple[tuple[str, str], ...] = (
    # (socket-constant-attribute-name, config-attribute-name)
    ("TCP_KEEPIDLE", "TCP_KEEPALIVE_IDLE"),
    ("TCP_KEEPINTVL", "TCP_KEEPALIVE_INTERVAL"),
    ("TCP_KEEPCNT", "TCP_KEEPALIVE_COUNT"),
)


def _tcp_keepalive_sockopts() -> list[tuple[int, int, int]]:
    """Return socket_options for a keepalive-tuned adapter; skip unknown constants."""
    opts: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for sock_const, cfg_attr in _TCP_KEEPALIVE_OPTIONS:
        const = getattr(socket, sock_const, None)
        if const is not None:
            opts.append((socket.IPPROTO_TCP, const, getattr(config, cfg_attr)))
    return opts


def _apply_tcp_keepalive(client: docker.DockerClient) -> None:
    """Mount a keepalive-tuned HTTPAdapter on the client. Best-effort.

    AttributeError/OSError on unusual transports or hostile kernels are
    swallowed — missing TCP keepalive is a tuning nicety, not a hard
    requirement. The client still works.
    """
    with contextlib.suppress(AttributeError, OSError):
        adapter = HTTPAdapter()
        adapter.poolmanager.connection_pool_kw["socket_options"] = _tcp_keepalive_sockopts()
        client.api.mount("http://", adapter)


def _build_client() -> docker.DockerClient:
    client = docker.DockerClient(
        base_url=config._cfg.docker_host,
        timeout=config.DOCKER_CLIENT_TIMEOUT,
        max_pool_size=config.DOCKER_POOL_SIZE,
    )
    # Unix socket doesn't need keepalive; TCP/HTTP transports do.
    if config._cfg.docker_host.startswith(("tcp://", "http://")):
        _apply_tcp_keepalive(client)
    client.ping()
    return client


def _cached_client_if_fresh() -> docker.DockerClient | None:
    """Phase 1: return the cached client iff it's within the ping-TTL.

    Takes the lock briefly (no I/O) to snapshot the cached client and the
    last-ping timestamp. Returns None when the cache is stale or absent —
    the caller moves to Phase 2.
    """
    with _client_lock:
        now = time.monotonic()
        if _client is None and (now - _client_failed_at) < config.DOCKER_BACKOFF:
            raise docker.errors.DockerException("Docker connection in backoff")
        if _client is not None and (now - _client_last_ping) < config.DOCKER_PING_TTL:
            return _client
        return None


def _discard_stale_client(candidate: docker.DockerClient) -> None:
    """Close `candidate` and clear the module global if it's still current.

    Called after a failed ping. `close()` is best-effort — a stale client
    might already have a torn-down socket.
    """
    global _client
    with contextlib.suppress(docker.errors.DockerException):
        candidate.close()
    with _client_lock:
        if _client is candidate:
            _client = None


def _ping_or_discard(candidate: docker.DockerClient | None) -> docker.DockerClient | None:
    """Phase 2: ping the candidate; return it on success, None if it was discarded.

    Network I/O happens OUTSIDE the lock so the SSH round-trip doesn't
    block other threads. On a failed ping we close the stale client and
    null out the module global so Phase 3 builds fresh.
    """
    global _client_last_ping
    if candidate is None:
        return None
    try:
        candidate.ping()
    except (docker.errors.DockerException, OSError):
        log.warning("docker.client_stale", action="reconnecting")
        _discard_stale_client(candidate)
        return None
    with _client_lock:
        if _client is candidate:
            _client_last_ping = time.monotonic()
    return candidate


def _build_and_publish_client() -> docker.DockerClient:
    """Phase 3: build a fresh client and install it as the module singleton."""
    global _client, _client_failed_at, _client_last_ping
    try:
        new_client = _build_client()
    except (docker.errors.DockerException, OSError) as exc:
        with _client_lock:
            _client = None
            _client_failed_at = time.monotonic()
        log.error("docker.connection_failed", host=config._cfg.docker_host, error=str(exc))
        raise
    with _client_lock:
        if _client is None:
            _client = new_client
            _client_last_ping = time.monotonic()
            log.info("docker.connected", host=config._cfg.docker_host)
            return _client
        # Another thread installed a client while we were building — close
        # the duplicate quietly and return the winner.
        try:
            new_client.close()
        except docker.errors.DockerException:
            pass
        return _client


def get_client() -> docker.DockerClient:
    """Return a live Docker client, reconnecting if necessary.

    Three-phase pipeline — each phase is a named helper above:
      Phase 1 — cached_client_if_fresh(): lock-only in-memory cache check
      Phase 2 — ping_or_discard(candidate): lock-free network I/O
      Phase 3 — build_and_publish_client(): install a fresh singleton
    """
    fresh = _cached_client_if_fresh()
    if fresh is not None:
        return fresh
    with _client_lock:
        candidate = _client
    still_alive = _ping_or_discard(candidate)
    if still_alive is not None:
        return still_alive
    return _build_and_publish_client()


def invalidate_client() -> None:
    """Close and discard the current Docker client (forces reconnect on next call)."""
    global _client, _client_last_ping
    with _client_lock:
        _client_last_ping = 0.0
        if _client:
            try:
                _client.close()
            except docker.errors.DockerException:
                # Client may already be closed; discarding a stale
                # reference — close failure is not actionable.
                pass
        _client = None


def docker_client_dep() -> docker.DockerClient:
    """FastAPI dependency that returns a live Docker client or raises HTTP 503."""
    try:
        return get_client()
    except (docker.errors.DockerException, OSError) as exc:
        # Narrowed from Exception: DockerException covers SDK-level
        # failures; OSError covers unix-socket / network failures that
        # propagate from the low-level transport. Any other error means
        # something truly unexpected — surface it as a 500 instead of
        # misleading the user with "engine unreachable".
        raise http_error("system.docker_unreachable") from exc


# ── SSH Tunnel Management ──────────────────────────────────
# SSH target `user@host` shape. The first character of each side is
# constrained to an alphanumeric so a leading hyphen can't be smuggled
# past the regex — if the sanitised value ever leaked into an argv
# list, a `-oProxyCommand=…` style options-injection would be out of
# reach by construction.
_SSH_TARGET_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-zA-Z0-9][a-zA-Z0-9._-]*$",
)
_tunnel_lock = threading.Lock()
_tunnel_ctl_sock: str = ""
_tunnel_ssh_target: str = ""
_tunnel_socket_path: str = ""


class TunnelError(Exception):
    """SSH tunnel failure with a classified code for UI guidance.

    `.code` is the machine-readable failure class (`auth_failed`,
    `host_key_mismatch`, `timeout`, `no_ssh_binary`, `bad_ssh_target`,
    `socket_missing`, `other`); `.help_text` is a one-line hint the UI
    can surface verbatim. `_raise_tunnel_error` in
    `skiff.routers.setup` maps the code into the `system.tunnel_failed`
    HTTP error.
    """

    def __init__(self, msg: str, code: str = "other", help_text: str = "") -> None:
        super().__init__(msg)
        self.code = code
        self.help_text = help_text


# SSH error classification table — loaded from skiff/_config/ssh_errors.toml.
# Each entry is (needles, code, help_text); `needles` are lowercase
# substrings that must ALL appear in the lowercased stderr for the
# entry to match. Order matters — the first match wins.
# R2b: extracted from Python to TOML so operators can tune the help
# strings without editing code.
_TOML_SSH_ERRORS = config._load_toml("ssh_errors")


def _load_ssh_error_patterns() -> tuple[tuple[tuple[str, ...], str, str], ...]:
    """Build the pattern table from skiff/_config/ssh_errors.toml [[pattern]] entries.

    Each entry must supply `needles` (list of lowercase substrings — ANY match),
    `code` (stable UI contract), and optionally `help` (user-facing remediation).
    Malformed entries are skipped rather than aborting startup.
    """
    entries = _TOML_SSH_ERRORS["pattern"]
    return tuple(
        (tuple(e["needles"]), e["code"], e.get("help", ""))
        for e in entries
        if isinstance(e, dict) and "needles" in e and "code" in e
    )


_SSH_ERROR_PATTERNS = _load_ssh_error_patterns()


def _classify_ssh_stderr(stderr: str) -> tuple[str, str]:
    """Map raw SSH stderr to (code, user-facing help text).

    Codes are stable UI contract: `auth_failed`, `host_key_mismatch`, `unknown_host`,
    `connection_refused`, `timeout`, `no_key`, `other`.
    """
    s = stderr.lower()
    if "permission denied" in s and ("publickey" in s or "password" in s):
        return (
            "auth_failed",
            "Your SSH key isn't installed on the remote host. "
            "In your terminal, run `ssh-copy-id <target>`, then retry.",
        )
    for needles, code, help_text in _SSH_ERROR_PATTERNS:
        if needles and all(n in s for n in needles):
            return (code, help_text)
    return ("other", stderr[:200] or "SSH failed without output.")


def _stop_tunnel_locked() -> None:
    """Stop the managed SSH tunnel. Must be called with _tunnel_lock held."""
    global _tunnel_ctl_sock, _tunnel_ssh_target, _tunnel_socket_path
    if _tunnel_ctl_sock and _tunnel_ssh_target:
        try:
            subprocess.run(
                ["ssh", "-S", _tunnel_ctl_sock, "-O", "exit", _tunnel_ssh_target],
                capture_output=True,
                check=False,
                timeout=config.TUNNEL_STOP_TIMEOUT,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            # TimeoutExpired is a SubprocessError subclass. FileNotFoundError
            # fires if ssh isn't in PATH. OSError covers spawn / fork
            # issues on heavily-constrained containers. Any other error
            # during shutdown propagates so we notice it in logs.
            pass
        for path in (_tunnel_ctl_sock, _tunnel_socket_path):
            safe = _safe_tunnel_socket_path(path)
            if safe and Path(safe).exists():
                _try_unlink(safe)
    _tunnel_ctl_sock = ""
    _tunnel_ssh_target = ""
    _tunnel_socket_path = ""


def _try_unlink(path: str) -> None:
    """Best-effort Path.unlink — never raises. Used for transient file cleanup."""
    with contextlib.suppress(OSError):
        Path(path).unlink()


class _TunnelBuilder:
    """Build an SSH ControlMaster tunnel as a sequence of isolated steps.

    Instead of one 100-line handler mixing validation, temp-file lifecycle,
    subprocess invocation, socket-wait polling, and global-state update,
    each concern is a named method on this builder. The handler becomes:

        with _TunnelBuilder(ssh_target) as tb:
            tb.validate_target() \\
              .write_ssh_config() \\
              .invoke_ssh() \\
              .wait_for_socket() \\
              .commit()

    Each step returns `self`, so the pipeline is read top-to-bottom.
    Each step raises TunnelError (or ValueError for bad input) if it
    fails; the context manager's `__exit__` cleans up the temp config
    file unconditionally so a mid-pipeline failure doesn't leak files.

    Security properties preserved from the original implementation:
      - socket_path comes only from `config.TUNNEL_DEFAULT_SOCKET` — no
        user input reaches the subprocess argv.
      - ssh config is written via mkstemp (mode 0o600) and unlinked
        after the SSH call returns.
      - `skiff-tunnel-target` is a hard-coded Host alias; the SSH
        command line contains no user-controlled strings.
      - The tunnel lock is held across the full pipeline so concurrent
        callers serialize — no half-finished state observable.
    """

    _HOST_ALIAS = "skiff-tunnel-target"

    def __init__(self, ssh_target: str) -> None:
        self.ssh_target = ssh_target
        self.socket_path = str(Path(config.TUNNEL_DEFAULT_SOCKET).resolve())
        self.conf_path: str | None = None
        self.ctl_sock: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        # Always drop the temp ssh config, whether we succeeded or failed
        # mid-pipeline. The bound tunnel keeps running from the fd SSH
        # already opened on -fNM fork — it doesn't need the file anymore.
        if self.conf_path is not None:
            _try_unlink(self.conf_path)
            self.conf_path = None

    # ── Pipeline steps (each returns self) ──

    def validate_target(self) -> Self:
        """Reject malformed ssh_target. Raises TunnelError (caller wraps)."""
        if not _SSH_TARGET_RE.match(self.ssh_target):
            raise TunnelError(
                "Invalid ssh_target format (expected user@host)",
                "bad_ssh_target",
                "Use the form user@host (no scheme, no port here — add port via the advanced tunnel options).",
            )
        return self

    def write_ssh_config(self) -> Self:
        """Write a 0o600 SSH config containing ONLY the validated Host alias.

        mkstemp + os.write + os.close gives us the owner-only mode with
        no window for another process to open the file during the write.
        On any OSError the fd is closed and the partial file is unlinked
        before re-raising. A named file (rather than /dev/fd/N) is used
        because macOS devfs requires a live directory entry.
        """
        user, _, host = self.ssh_target.partition("@")
        fd, path = tempfile.mkstemp(suffix=".conf")
        self.conf_path = path
        payload = (f"Include ~/.ssh/config\nHost {self._HOST_ALIAS}\n  Hostname {host}\n  User {user}\n").encode()
        try:
            os.write(fd, payload)
        except OSError:
            # __exit__ only unlinks the path; the fd is our responsibility.
            _close_fd_quiet(fd)
            raise
        os.close(fd)
        return self

    def _build_ssh_cmd(self) -> list[str]:
        """Compose the SSH argv from TOML static flags + config-attr dynamic flags.

        The ControlMaster socket path uses a cryptographically-random
        suffix instead of the PID so an attacker on a shared host can't
        predict the path and pre-create a symlink at it. The socket is
        still in /tmp (honours `_safe_tunnel_socket_path`'s basename
        validator) but the attacker's prediction window is removed.
        """
        import secrets

        suffix = secrets.token_hex(8)  # 16 hex chars = 64 bits unguessable
        self.ctl_sock = f"/tmp/skiff-tunnel-ctl-{suffix}.sock"  # noqa: S108
        cmd = ["ssh", "-F", self.conf_path or "", "-fNM", "-S", self.ctl_sock]
        for name, value in config._TOML_SSH_TUNNEL["static"].items():
            cmd.extend(["-o", f"{name}={value}"])
        for name, attr in config._TOML_SSH_TUNNEL["dynamic"].items():
            cmd.extend(["-o", f"{name}={getattr(config, attr)}"])
        cmd.extend(
            [
                "-L",
                f"{self.socket_path}:/var/run/docker.sock",
                self._HOST_ALIAS,  # hard-coded alias, no user data in argv
            ]
        )
        return cmd

    def invoke_ssh(self) -> Self:
        """Run the SSH subprocess — caller MUST hold `_tunnel_lock`.

        The lock is acquired by `_start_tunnel` for the FULL pipeline
        (invoke → wait → commit) so two concurrent callers cannot
        spawn overlapping ssh processes. Without the whole-pipeline
        lock, caller B reads stale module globals (pre-A's ctl sock)
        during `_stop_tunnel_locked`, fails to kill A's new ssh, and
        leaves an orphan.
        """
        _stop_tunnel_locked()
        if Path(self.socket_path).exists():
            _try_unlink(self.socket_path)
        cmd = self._build_ssh_cmd()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=config.TUNNEL_CONNECT_TIMEOUT + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TunnelError(
                "SSH connection timed out",
                "timeout",
                "Connection timed out. Host unreachable or firewall blocking.",
            ) from exc
        except FileNotFoundError as exc:
            raise TunnelError(
                "ssh binary not found",
                "no_ssh_binary",
                "The `ssh` command is not installed on the SKIFF host.",
            ) from exc
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            code, help_text = _classify_ssh_stderr(stderr)
            raise TunnelError(
                f"SSH failed: {stderr[:200] or 'unknown error'}",
                code,
                help_text,
            )
        return self

    def wait_for_socket(self) -> Self:
        """Poll until the tunnel socket appears or TUNNEL_SOCKET_WAIT elapses."""
        deadline = time.monotonic() + config.TUNNEL_SOCKET_WAIT
        while time.monotonic() < deadline:
            if Path(self.socket_path).exists():
                return self
            time.sleep(config.TUNNEL_SOCKET_POLL)
        raise TunnelError(
            f"Tunnel socket did not appear at {self.socket_path}",
            "socket_missing",
            "SSH connected but the tunnel socket didn't open. Check the remote Docker daemon is running.",
        )

    def commit(self) -> Self:
        """Publish the new tunnel into module-global state and log success."""
        global _tunnel_ctl_sock, _tunnel_ssh_target, _tunnel_socket_path
        # invoke_ssh() sets ctl_sock before we get here; defend anyway so
        # a broken pipeline raises TunnelError rather than publishing None.
        if self.ctl_sock is None:
            raise TunnelError(
                "internal: commit() called before invoke_ssh()",
                "internal",
                "",
            )
        _tunnel_ctl_sock = self.ctl_sock
        _tunnel_ssh_target = self.ssh_target
        _tunnel_socket_path = self.socket_path
        log.info("tunnel.started", target=self.ssh_target, socket=self.socket_path)
        return self


def _start_tunnel(ssh_target: str) -> None:
    """Start an SSH ControlMaster tunnel. Raises ValueError / TunnelError on failure.

    Composed from `_TunnelBuilder` steps — each step is independently
    testable and the top-to-bottom sequence IS the handler's algorithm.
    The builder's `__exit__` cleans up the temp SSH config file even when
    a step midway through raises.

    The socket path is always the server-controlled
    `config.TUNNEL_DEFAULT_SOCKET` constant — no caller-provided path
    reaches the subprocess, eliminating command/path injection.
    """
    # Hold `_tunnel_lock` across the ENTIRE pipeline. Two concurrent
    # POSTs to /api/tunnel/reconnect would otherwise interleave:
    # caller A releases the lock before its `commit()`, caller B reads
    # stale module globals, fails to kill A's new ssh in
    # `_stop_tunnel_locked`, and A's ssh becomes an orphan holding the
    # -L socket binding. The cost of holding the lock across
    # wait_for_socket (up to TUNNEL_SOCKET_WAIT) is serialisation of
    # reconnects, which is the correct behaviour.
    with _tunnel_lock, _TunnelBuilder(ssh_target) as tb:
        (tb.validate_target().write_ssh_config().invoke_ssh().wait_for_socket().commit())


def stop_tunnel() -> None:
    """Stop the managed SSH tunnel (public, acquires lock)."""
    with _tunnel_lock:
        _stop_tunnel_locked()


def get_tunnel_socket_path() -> str:
    """Return the currently active tunnel socket path (empty if no tunnel active)."""
    return _tunnel_socket_path


def get_tunnel_ssh_target() -> str:
    """Return the SSH target of the last-started managed tunnel (empty if never started).

    Persists across tunnel drops so `/api/tunnel/reconnect` can re-open without the
    client needing to re-send the target — zero-trust: target is server-only state.
    """
    return _tunnel_ssh_target


# Shared between _stop_tunnel_locked, /api/setup-state, /api/tunnel/status.
# Returns a CodeQL-taint-safe path string or "" if the input isn't a well-formed
# socket basename. Rationale (same across all three callers):
#   - basename() strips any directory components — removes traversal surface.
#   - re.fullmatch() + group(0) is recognised by CodeQL as a sanitiser, breaking
#     py/path-injection taint propagation from the (possibly user-derived) input.
#   - Reconstructing from Path("/tmp").resolve() + basename avoids macOS's
#     /private/tmp canonicalisation mismatch (on macOS, /tmp → /private/tmp).
_TUNNEL_SOCK_BASENAME_RE = re.compile(r"[a-zA-Z0-9._\-]+\.sock")


def _safe_tunnel_socket_path(raw: str) -> str:
    """Return a sanitised path to a socket in /tmp, or "" if the basename is invalid."""
    if not raw:
        return ""
    bn = Path(raw).name
    match = _TUNNEL_SOCK_BASENAME_RE.fullmatch(bn) if bn else None
    return str(Path("/tmp").resolve() / match.group(0)) if match else ""  # noqa: S108
