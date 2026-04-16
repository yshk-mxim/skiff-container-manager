# API Reference

All endpoints require `Authorization: Bearer <token>` when `API_TOKEN` is configured.  
Mutating endpoints (`POST`, `DELETE`) additionally require `X-Requested-With: ContainerManager`.

---

## Health

### `GET /health`
Liveness probe. Never checks Docker — always returns 200 to avoid restart loops.

**Response**
```json
{"status": "ok", "uptime_seconds": 123}
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

---

## Config

### `GET /api/config`
Returns non-secret server configuration for the UI. Requires authentication.

**Response**
```json
{
  "allowed_registries": ["docker.io", "ghcr.io"],
  "docker_vm_host": "docker-vm.internal",
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

Rate limit: 30/minute.

### `GET /api/registry/tags`
Fetch the 20 most recently updated tags for a Docker Hub image.

**Query params** — `image` (string, e.g. `nginx` or `library/nginx`, required)

**Response**
```json
{"image": "nginx", "tags": ["latest", "1.27", "1.26", "alpine", "..."]}
```

Rate limit: 30/minute.

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
Stop a running container (10 s grace period).

### `POST /api/containers/{id}/restart`
Restart a container (10 s grace period).

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

---

## WebSocket: Log Streaming

### `WS /ws/logs/{id}`
Stream container logs in real time.

**Handshake protocol:**
1. Client opens the WebSocket connection.
2. Client sends `AUTH <token>` as the first plain-text message.
3. Server acknowledges with `AUTH_OK` (or closes with `4003` on failure).
4. Server streams log lines as text frames.
5. Client sends a NUL byte (`\x00`) as a keepalive every 30 seconds to prevent idle timeout.

If `API_TOKEN` is unset on the server, the `AUTH` step is skipped.

**Close codes:**

| Code | Meaning |
|---|---|
| `4000` | Invalid container ID |
| `4003` | Auth failure (wrong token or session expired) |
| `4429` | Too many concurrent WebSocket connections from this IP |
| `1000` | Normal closure (logs ended or server shutdown) |

**Idle timeout:** The server closes the connection after 5 minutes of no new log output.

---

## WebSocket: Interactive Shell

### `WS /ws/exec/{id}`
Open an interactive shell inside a container.

**Handshake protocol:**
1. Client opens the WebSocket connection.
2. Client sends `AUTH <token>` as the first plain-text message.
3. Server acknowledges with `AUTH_OK` (or closes with `4003` on failure).
4. Stdin/stdout flow as plain text frames.
5. Client sends a NUL byte (`\x00`) as a keepalive every 30 seconds.

**Close codes:** same as log streaming above.

**Inactivity timeout:** Session closes after 10 minutes of no input.

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

---

## Compose

### `GET /api/compose/stacks`
List running Compose stacks (detected from container labels).

### `POST /api/compose/up`
Deploy a Compose stack. Upload a `docker-compose.yml` file, or re-deploy using the last uploaded file.

**Form params** — `file` (multipart, optional), `project_name` (default `dev`)

Compose files are validated before deployment; dangerous keys (`privileged`, `cap_add`, host mounts, etc.) are rejected.

### `POST /api/compose/down`
Tear down a Compose stack.

**Query params** — `project_name` (default `dev`)

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

**Query params** — `tail` (int, 1–1000, default 200)

**Response** — array of JSON objects, one per log event.

### `GET /api/system/audit-log/download`
Download the full audit log as a JSONL file (streamed).

**Response** — `Content-Type: application/x-ndjson`, `Content-Disposition: attachment; filename="audit.jsonl"`

---

## Error Responses

All errors return JSON:

```json
{"detail": "Error message"}
```

Common status codes and example `detail` strings:

| Code | Meaning | Example `detail` |
|---|---|---|
| 400 | Bad request (validation error, Docker API error) | `"Image not in allowed registries"` |
| 401 | Missing or invalid API token | `"Invalid or missing token"` |
| 403 | Missing `X-Requested-With` header or setup window expired | `"CSRF check failed"` |
| 404 | Container, image, volume, or network not found | `"Not found"` |
| 409 | Conflict (container already started/stopped) | `"Container already running"` |
| 422 | Malformed request body or query params | `"value is not a valid integer"` |
| 429 | Rate limit exceeded | `"Rate limit exceeded"` |
| 503 | Docker Engine unreachable | `"Container engine unavailable"` |
| 504 | Operation timed out | `"Operation timed out"` |
