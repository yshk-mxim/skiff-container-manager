# API Reference

All endpoints require `Authorization: Bearer <token>` when `API_TOKEN` is configured.
Mutating endpoints (`POST`, `DELETE`) additionally require `X-Requested-With: ContainerManager`.

---

## Health

### `GET /health`
Liveness probe. Never checks Docker — always returns 200 to avoid restart loops.

**Response**
```json
{"status": "ok", "uptime_seconds": 123, "version": "1.0.1.dev0"}
```

### `GET /ready`
Readiness probe. Returns 503 if Docker is unreachable.

**Response (200)**
```json
{"status": "ready", "docker_version": "24.0.7", "containers_running": 3}
```

---

## Auth Info

### `GET /api/auth-required`
Returns whether authentication is required. No authentication needed — used by the UI login screen.

**Response**
```json
{"required": true}
```

### `GET /api/docs`
CSP-safe discoverability landing page for the OpenAPI spec. No
authentication — returns an HTML page with links to
`/api/openapi.json` (raw spec) and external-editor deep links
(opens a new tab). Rate-limited at the `PUBLIC` tier. Not exposed at
the FastAPI default `/docs` / `/redoc` paths because those pull assets
from a CDN, violating the strict `script-src 'self'` CSP.

### `GET /api/openapi.json`
FastAPI-generated OpenAPI 3.1 schema for every route. No
authentication — the route catalogue itself is not a secret; route
implementation details are guarded by per-route auth.

---

## Config

### `GET /api/config`
Returns non-secret server configuration for the UI. Requires authentication.

**Response**
```json
{
  "allowed_registries": ["docker.io", "ghcr.io"],
  "docker_vm_host": "docker-vm.example.com",
  "docker_host": "unix:///tmp/docker.sock"
}
```

---

## Registry

### `GET /api/registry/search`
Search Docker Hub for images. Proxied server-side to avoid browser CORS restrictions.

**Query params** — `q` (string, 1–100 chars, required)

**Response**
```json
{
  "results": [
    {
      "repo_name": "library/nginx",
      "short_description": "Official build of Nginx.",
      "pull_count": 1000000,
      "is_official": true
    }
  ]
}
```

Rate limit: see `/api/config.rate_limit_scale` and
`skiff/_config/rate.toml` — the `READ` tier applies to registry search/tags.

### `GET /api/registry/tags`
Fetch the 20 most recently updated tags for a Docker Hub image.

**Query params** — `image` (string, e.g. `nginx` or `library/nginx`, required)

**Response**
```json
{"image": "nginx", "tags": ["latest", "1.27", "1.26", "alpine", "..."]}
```

Rate limit: see `/api/config.rate_limit_scale` and
`skiff/_config/rate.toml` — the `READ` tier applies to registry search/tags.

---

## Containers

### `GET /api/containers`
List all containers (running and stopped).

**Response** — array of:
```json
{
  "id": "abc123",
  "name": "my-service",
  "image": "docker.io/library/nginx:latest",
  "status": "running",
  "state": "running",
  "health": "healthy",
  "ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
  "created": "2026-01-15T10:00:00Z"
}
```

### `POST /api/containers/run`
Run a new container.

**Query params** — `image` (required), `name` (optional)

**Body** (JSON, all optional):
```json
{
  "ports": {"8080/tcp": "8080"},
  "environment": ["KEY=value"],
  "command": "/bin/sh -c 'echo hello'",
  "volumes": ["my-volume:/data"],
  "restart_policy": "unless-stopped",
  "network": "my-network",
  "labels": {"app": "myapp"}
}
```

Constraints:
- Image must be from an allowed registry.
- Volumes must be named volumes (no host paths).
- `restart_policy`: `no`, `on-failure`, `unless-stopped`, or `always`.
- Max 50 labels; max container count is 50; memory capped at 2 GB; CPU capped at 2 cores.

**Response**
```json
{"id": "abc123", "name": "my-service", "status": "created"}
```

### `POST /api/containers/{id}/start`
Start a stopped container.

### `POST /api/containers/{id}/stop`
Stop a running container. The grace period before SIGKILL is
configured by `CONTAINER_STOP_TIMEOUT` (default 5 s; see
`docs/config-knobs.md`).

### `POST /api/containers/{id}/restart`
Restart a container. Grace period follows `CONTAINER_STOP_TIMEOUT`.

### `POST /api/containers/{id}/pause`
Pause a running container (SIGSTOP).

### `POST /api/containers/{id}/unpause`
Unpause a paused container.

### `POST /api/containers/{id}/kill`
Send a signal to a container.

**Query params** — `signal`: `SIGKILL` (default), `SIGTERM`, `SIGINT`, or `SIGHUP`.

### `POST /api/containers/{id}/rename`
Rename a container.

**Query params** — `name`

### `DELETE /api/containers/{id}`
Remove a container.

**Query params** — `force` (bool, default `false`)

### `GET /api/containers/{id}/logs`
Retrieve container logs.

**Query params** — `tail` (int, 1–5000, default 200)

**Response**
```json
{"logs": "2026-01-15T10:00:00Z stdout log line\n..."}
```

### `GET /api/containers/{id}/logs/download`
Download logs as a plain-text `.txt` file.

**Query params** — `tail` (int, 1–5000, default 5000), `since` (ISO 8601 or Unix ts, optional), `until` (optional)

### `GET /api/containers/{id}/logs/download.jsonl`
Download logs as a JSONL file — one JSON object per line with `timestamp` and `message` fields.

**Query params** — same as above

### `GET /api/containers/{id}/inspect`
Full container details: state, config, mounts, network, ports, health check.

### `GET /api/containers/{id}/stats`
Real-time resource usage snapshot.

**Response**
```json
{
  "cpu_percent": 1.23,
  "mem_usage_mb": 128.5,
  "mem_limit_mb": 2048.0,
  "mem_percent": 6.27,
  "net_rx_mb": 0.01,
  "net_tx_mb": 0.00,
  "blk_read_mb": 0.00,
  "blk_write_mb": 0.01
}
```

### `GET /api/containers/{id}/top`
List processes running inside a container (`docker top`).

### `GET /api/containers/{id}/diff`
Show filesystem changes in a container since it was created.

### `POST /api/containers/{id}/update`
Adjust a running container's resource limits (`memory`, `cpus`,
`restart_policy`). Values are capped server-side at `MAX_CONTAINER_MEM`
/ `MAX_CONTAINER_CPU`; the audit log records before/after per field
so operators can spot unexpected tuning.

**Body** (JSON) — any subset of `memory`, `cpus`, `restart_policy`.

**Response** — `OkResponse` with `id` and the applied changes.

---

## WebSocket: Log Streaming

### `WS /ws/logs/{id}`
Stream container logs in real time.

**Handshake protocol:**
1. Client opens the WebSocket connection. Do NOT pass the bearer token as a
   query parameter (`?token=…`) — SKIFF rejects such upgrades with close
   code `4008`, and the URL would otherwise be logged by any HTTP proxy
   in front.
2. Client sends `AUTH <token>` as the first plain-text message.
3. On success the server starts streaming log lines (no acknowledgement
   frame is sent — the first log line IS the acknowledgement). On failure
   the server closes with `4003`.
4. Server sends a single NUL byte (`\x00`) every 30 seconds as a
   server-to-client keepalive so the client can distinguish a healthy
   idle channel from a hung proxy.

If `API_TOKEN` is unset on the server, the `AUTH` step is skipped.

**Close codes:**

| Code | Meaning |
|---|---|
| `4000` | Invalid container ID (path validation failed) |
| `4003` | Auth failure — wrong token, expired session, blocked origin, or token rotated server-side |
| `4008` | Policy violation — token passed as `?token=…` query parameter, or payload exceeded the 64 KiB single-message cap |
| `1000` | Normal closure (logs ended or server shutdown) |

**Idle timeout:** the server closes the connection after
`WS_LOG_IDLE_TIMEOUT` seconds of no new log output (see
[`docs/config-knobs.md`](config-knobs.md) for the current default
and how to tune it). Any client should treat the disconnect as a
normal idle close and reconnect when the user returns.

---

## WebSocket: Interactive Shell

### `WS /ws/exec/{id}`
Open an interactive shell inside a container.

**Handshake protocol:**
1. Client opens the WebSocket connection (no `?token=` query — see log
   streaming above for the reasoning).
2. Client sends `AUTH <token>` as the first plain-text message.
3. On success the server starts piping stdin/stdout; the first byte is the
   shell's prompt. On failure the server closes with `4003`.
4. Server sends a NUL byte keepalive every 30 seconds.

**Close codes:** same table as log streaming above.

**Inactivity timeout:** Session closes after `WS_EXEC_IDLE_TIMEOUT`
seconds of no input (see [`docs/config-knobs.md`](config-knobs.md)).

---

## Images

### `GET /api/images`
List all local images.

### `GET /api/images/allowed`
List images whose tags match an allowed registry.

### `POST /api/images/pull`
Pull an image from an allowed registry (5-minute timeout).

**Query params** — `image`

### `POST /api/images/push`
Push a tagged image to an allowed registry (10-minute timeout).

**Query params** — `image`

### `POST /api/images/{id}/tag`
Tag an image.

**Query params** — `repository`, `tag` (default `latest`)

### `DELETE /api/images/{id}`
Remove an image.

**Query params** — `force` (bool, default `false`)

### `GET /api/images/{id}/inspect`
Full image details: layers, config, history.

---

## Volumes

### `GET /api/volumes`
List all volumes with in-use status.

### `POST /api/volumes/create`
Create a named volume.

**Query params** — `name`

### `DELETE /api/volumes/{name}`
Remove a volume.

**Query params** — `force` (bool, default `false`)

### `POST /api/volumes/prune`
Remove all unused volumes.

**Response**
```json
{"deleted": ["vol1", "vol2"], "space_reclaimed_mb": 1024.0}
```

---

## Networks

### `GET /api/networks`
List all networks with connected containers.

### `POST /api/networks/create`
Create a network.

**Query params** — `name`, `driver` (`bridge`, `overlay`, `macvlan`, `none`; default `bridge`)

### `DELETE /api/networks/{id}`
Remove a network. Default networks (`bridge`, `host`, `none`) cannot be removed.

### `POST /api/networks/{id}/connect`
Connect a container to a network.

**Query params** — `container_id`

### `POST /api/networks/{id}/disconnect`
Disconnect a container from a network.

**Query params** — `container_id`

### `POST /api/networks/prune`
Remove all unused networks.

### `GET /api/networks/{id}/inspect`
Full Docker network inspect payload — options, labels, attached
containers, driver-specific metadata. Parity with
`/api/volumes/{name}/inspect`.

---

## Compose

### `GET /api/compose/stacks`
List running Compose stacks (detected from container labels).

### `POST /api/compose/up`
Deploy a Compose stack. Upload a `docker-compose.yml` file, or re-deploy using the last uploaded file.

**Query params** — `project_name` (default `dev`)
**Form params** — `file` (multipart, optional)

Compose files are validated before deployment; dangerous keys (`privileged`, `cap_add`, host mounts, etc.) are rejected.

### `POST /api/compose/down`
Tear down a Compose stack.

**Query params** — `project_name` (default `dev`)

### `GET /api/compose/{project}/logs`
Tail-aggregated logs for all services in a Compose stack. Equivalent
to `docker compose logs --tail=N`; rate-limited at the `READ` tier.

**Query params** — `project` (path), `tail` (int, default 200)

### `POST /api/compose/{project}/services/{service}/restart`
Restart every container belonging to a single service in a Compose
stack. Per-service granularity — does NOT re-evaluate the compose
file the way `docker compose restart` would. Returns the list of
restarted short ids.

---

## Profile

### `POST /api/profile/enter-reviewer`
One-way runtime switch into the read-only reviewer profile. Flips
`config.PROFILE = "reviewer"` under the WS lock, force-closes every
active exec WebSocket (`audit.ws_exec_terminated`), and emits a
`profile.switched` audit record. Exiting reviewer mode requires
either `/api/auth/reset-config` (which also restores PROFILE to
the boot value) or a server restart.

**Response** — `{ok: true, profile: "reviewer", exec_sessions_closed: <int>}`

---

## Volumes (inspect)

### `GET /api/volumes/{name}/inspect`
Full volume details: driver, mountpoint, usage bytes, referencing
containers. Audited as a read; no rate-limit surprises.

**Response** (truncated):
```json
{
  "name": "my-volume",
  "driver": "local",
  "mountpoint": "/var/lib/docker/volumes/my-volume/_data",
  "usage_bytes": 1048576,
  "ref_count": 1,
  "containers": ["abc123"]
}
```

---

## Debug (maintainer-only)

### `GET /debug/threads`
Dump every live Python thread's stack frame. **Disabled by default**;
enable per-process with `SKIFF_DEBUG_THREADS=1` — intended for an
operator diagnosing a hang. When the flag is off, returns 403
`system.debug_disabled` so a SIEM rule can flag an operator turning
it on inadvertently.

---

## System

### `GET /api/system/info`
Docker Engine info: version, OS, CPUs, memory, container/image counts, storage driver.

### `GET /api/system/df`
Disk usage by images, containers, volumes, and build cache.

### `POST /api/system/prune`
Prune stopped containers, dangling images, and unused networks.

### `POST /api/system/prune-build-cache`
Prune the Docker build cache.

### `GET /api/system/audit-log`
Return the last N lines of the structured audit log.

**Query params** — `tail` (int, 1–`MAX_AUDIT_LINES` [default 2000], default 200)

**Response** — array of JSON objects, one per log event.

### `GET /api/system/audit-log/download`
Download the full audit log as a JSONL file (streamed).

**Response** — `Content-Type: application/x-ndjson`, `Content-Disposition: attachment; filename="audit.jsonl"`

### `GET /api/system/metrics`
Prometheus text-format metrics snapshot. Authenticated — scrapers
must present a valid Bearer token. Labels use a hashed `docker_host`
so topology doesn't leak across a shared scraper.

---

## Setup wizard + auth lifecycle

These routes are live on any running instance; the wizard ones are
reachable only while `api_token` is unset (or within the 5-minute
setup window after boot). Full bodies + examples live in the
auto-generated `docs/features/setup.generated.md`.

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /api/setup-state` | public (loopback discloses socket path) | Wizard presence check |
| `GET /api/setup/probe-docker` | public (wizard-only) | Probe the common local Docker sockets |
| `POST /api/setup` | public (wizard-only) | Commit `docker_host` + `api_token` from the wizard |
| `POST /api/setup/tunnel` | public (wizard-only) | Open a wizard-managed SSH ControlMaster tunnel |
| `DELETE /api/setup/tunnel` | public (wizard-only) | Stop the wizard-managed tunnel |
| `GET /api/tunnel/status` | AUTH | Report tunnel reachability; honest for manual tunnels too |
| `POST /api/tunnel/reconnect` | AUTH | Wizard-managed: reopen; manual: return envelope pointing at the socket path |
| `POST /api/auth/rotate-token` | AUTH | Swap `API_TOKEN` in memory; force-closes active WebSockets |
| `POST /api/auth/reset-config` | AUTH | Clear in-memory state; reopen the 5-min setup window |

## Undo queue

| Method + path | Auth | Purpose |
|---|---|---|
| `POST /api/undo/{token}` | AUTH | Cancel a pending destructive op inside its window |

## Connect-snippets

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /api/connect-snippets` | AUTH | Render per-tool snippets from `skiff/_config/connect_snippets.toml`. Optional `?tool=<id>` returns a single tool's block. |

---

## Error Responses

Every 4xx / 5xx response uses the same envelope:

```json
{"detail": {"code": "container.name_taken", "message": "container name is already in use", "help": "try a different --name"}}
```

- `code` — stable machine-readable identifier from the
  [`docs/errors.md`](errors.md) catalogue. SIEM rules key on this.
- `message` — short human string safe to display in a toast.
- `help` *(optional)* — one-sentence remediation hint when the server
  can provide one (e.g. tunnel failure → "check ssh-agent is running").

Common status codes:

| Code | Meaning | Example envelope `detail.code` |
|---|---|---|
| 400 | Input validation failed | `validation.bad_input`, `image.registry_blocked` |
| 401 | Missing or invalid bearer token | `auth.missing_token`, `auth.invalid_token`, `auth.session_expired` |
| 403 | CSRF or setup-window check failed | `auth.csrf_missing`, `auth.csrf_invalid`, `setup.window_expired` |
| 404 | Resource not found | `container.not_found`, `image.not_found`, `volume.not_found`, `network.not_found` |
| 409 | Conflict | `container.name_taken`, `container.conflict`, `auth.token_unchanged`, `tunnel.already_connected` |
| 422 | Malformed request body or query params | `validation.bad_input` |
| 429 | Rate limit exceeded | `auth.rate_limited`, `auth.setup_locked` |
| 503 | Container engine unreachable | `system.docker_unreachable`, `system.tunnel_failed` |
| 504 | Operation timed out | `compose.timeout`, `image.pull_timed_out`, `image.push_timed_out`, `container.stats_timeout` |

See [`docs/errors.md`](errors.md) for the authoritative code catalogue
(auto-generated from `skiff/contract/errors.py`).
