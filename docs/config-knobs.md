# Configuration knob catalogue

GENERATED FROM `skiff/config.py` + `skiff/_config/defaults.toml`. Run
`python tools/gen_catalogues.py` to regenerate; CI `--check`
fails if this file drifts.

Every tunable the server reads from the environment or TOML
defaults is registered via `config_knob(...)`. The table lists
every knob with its default, type, and one-line doc. See
[`docs/configuration.md`](configuration.md) for how to override.

| Env var | Default | Validator | Exposed? | Secret? | Doc |
|---|---|---|---|---|---|
| `ALLOWED_ORIGINS` | `http://127.0.0.1:8080` | _csv_list_no_wildcard | yes | no | Comma-separated browser origin allowlist. Must exactly match the UI origin. |
| `ALLOWED_REGISTRIES` | `docker.io,ghcr.io` | _csv_list | yes | no | Comma-separated image-registry allowlist. Pull/push reject images outside this list. |
| `API_TOKEN` | `` |  | no | yes | Shared bearer token for every authenticated request. Empty ⇒ no auth (localhost-only). |
| `AUDIT_BACKUP_COUNT` | `5` | int | yes | no | Number of rotated audit log files to keep. Higher = longer retention, more disk. |
| `AUDIT_LOG` | `$HOME/Library/Application Support/skiff/audit…` |  | yes | no | Path to the rotating JSON-lines audit log. Set to /dev/null to disable file logging. |
| `AUDIT_MAX_MB` | `10` | lambda | yes | no | Audit log rotation file size in MiB. Multiplied by AUDIT_BACKUP_COUNT for total retention. |
| `BIND_HOST` | `127.0.0.1` |  | yes | no | Interface uvicorn binds to. Use 127.0.0.1 for local; override only with an explicit front proxy. |
| `BODY_READ_TIMEOUT_SECS` | `30` | int | yes | no | Per-chunk timeout (seconds) for reading a request body. A client that drips one byte every 5 s would otherwise hold an ASGI worker open for minutes; at this timeout the middleware responds 408 `validation.body_timeout` instead. Set higher for very slow uploads over WAN links; lower for tighter slow-POST defence. |
| `COMPOSE_DIR` | `$HOME/Library/Application Support/skiff/compose` |  | yes | no | Directory where uploaded docker-compose.yml files are stored (one subdir per project). |
| `COMPOSE_DOWN_TIMEOUT` | `60` | int | yes | no | Seconds for `docker compose down`. |
| `COMPOSE_MAX_REPLICAS` | `10` | int | yes | no | Max replicas per service via /scale. |
| `COMPOSE_UP_TIMEOUT` | `120` | int | yes | no | Seconds for `docker compose up -d`. |
| `CONTAINERS_POLL_MS` | `5000` | int | yes | no | Containers list auto-refresh interval (ms). |
| `CONTAINER_CP_MAX_MB` | `64` | int | yes | no | Max MB for /api/containers/{id}/files get/put (cp). |
| `CONTAINER_LS_MAX_ENTRIES` | `2000` | int | yes | no | Max dir entries returned by /api/containers/{id}/ls. |
| `CONTAINER_RESTART_TIMEOUT` | `10` | int | yes | no | Seconds for restart. |
| `CONTAINER_STATS_TIMEOUT` | `10.0` | float | yes | no | Seconds for stats call. |
| `CONTAINER_STOP_TIMEOUT` | `5` | int | yes | no | Seconds for graceful stop before kill. |
| `DASHBOARD_POLL_MS` | `8000` | int | yes | no | Dashboard counters/events refresh interval (ms). |
| `DF_TIMEOUT` | `30` | int | yes | no | Max seconds for a single `/api/system/df` Docker SDK call on large hosts. |
| `DOCKER_BACKOFF` | `5` | int | yes | no | Seconds to wait after a failed connection before retrying. |
| `DOCKER_CLIENT_TIMEOUT` | `15` | int | yes | no | Seconds per Docker SDK HTTP request. |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` |  | yes | no | Docker daemon socket or TCP URL. Override for remote hosts or Colima. |
| `DOCKER_PING_TTL` | `3` | int | yes | no | Skip ping if last success < this (seconds). |
| `DOCKER_POOL_SIZE` | `5` | int | yes | no | urllib3 connection pool size. |
| `DOCKER_VM_HOST` | `` | _validate_hostname | yes | no | Optional hostname shown in audit log lines when Docker is remote (display only). |
| `EVENTS_POLL_MS` | `5000` | int | yes | no | System Events tail auto-refresh interval (ms). |
| `FETCH_TIMEOUT_MS` | `30000` | int | yes | no | Browser-side fetch() abort timeout in ms (long ops override per-call). |
| `GCP_LOG_NAME` | `skiff-audit` |  | no | no | Cloud Logging log name used when GOOGLE_CLOUD_PROJECT is set. |
| `GOOGLE_CLOUD_PROJECT` | `` |  | no | no | Google Cloud project ID. When set, audit log events are also mirrored to Cloud Logging. |
| `IMAGE_PULL_TIMEOUT` | `300.0` | float | yes | no | Seconds for image pull (network slow). |
| `MAX_AUDIT_LINES` | `2000` | int | yes | no | Max lines in a single /audit-log response. |
| `MAX_BODY_BYTES` | `16777216` | _validate | yes | no | Maximum request body size in bytes (min 1024). 413 returned if exceeded. Rejected at boot below 1 KiB so a zero / negative value can't make every mutation 413. |
| `MAX_COMPOSE_SIZE` | `2097152` | int | yes | no | Max compose file upload size in bytes. |
| `MAX_CONTAINERS` | `50` | int | yes | no | Max containers the UI enumerates. |
| `MAX_LOG_TAIL` | `5000` | int | yes | no | Max lines in a single /logs response. |
| `MAX_PORT_MAPPINGS` | `10` | int | yes | no | Max published ports per `docker run`. |
| `PORT` | `8080` | int | yes | no | TCP port uvicorn listens on. Override via PORT env var. |
| `PROBE_DOCKER_TIMEOUT` | `2` | int | yes | no | Seconds for the wizard's ping probe. |
| `PROFILE` | `` | _apply_profile | yes | no | Persona preset that seeds sensible defaults for RATE_LIMIT_SCALE and related knobs. One of: ci, dev, homelab, reviewer, sre, tutor. Explicit env wins. |
| `RATE_LIMIT_SCALE` | `1` | _rate_scale_validator | yes | no | Multiplier applied to every rate-limit spec. 1=default, 100=CI (effectively uncapped). |
| `REGISTRY_DESC_MAX` | `200` | int | yes | no | Max chars of registry description echoed back. |
| `REGISTRY_MAX_TAGS` | `100` | int | yes | no | Tags per /api/registry/tags call. |
| `REGISTRY_SEARCH_PAGE_SIZE` | `10` | int | yes | no | Results per /api/registry/search call. |
| `REGISTRY_TIMEOUT` | `8` | int | yes | no | Seconds for Docker Hub API requests. |
| `SESSION_ABS_TIMEOUT` | `28800` | _validate | yes | no | Server-side absolute session lifetime, seconds. The client reads this via /api/config so a deployment tightening the window doesn't require a JS edit. Rejected at boot below 60 s so a zero / negative value can't lock the operator out of their own setup wizard. |
| `SESSION_IDLE_SECS` | `900` | _validate | yes | no | Server-side + client-side idle-timeout window in seconds. Read by app.js from /api/config at boot; server enforces via `_check_session_age`. Rejected at boot below 30 s to prevent an unusable instance. |
| `SETUP_LOCKOUT_SECS` | `300` | int | yes | no | Seconds to lock out an IP after max failed setup attempts. |
| `SETUP_MAX_ATTEMPTS` | `3` | int | yes | no | POST /api/setup failures before IP lockout. |
| `SETUP_WINDOW_SECS` | `300` | int | yes | no | Wizard reachable for N seconds post-boot. |
| `SHUTDOWN_FLUSH_TIMEOUT` | `20` | int | yes | no | Max seconds the lifespan shutdown will spend draining the undo queue. |
| `SKIFF_DEBUG_THREADS` | `0` | lambda | yes | no | Enable /debug/threads endpoint (AUTH-gated). Default disabled because thread stacks can contain sensitive local-variable reprs. |
| `TCP_KEEPALIVE_COUNT` | `3` | int | yes | no | Probes before declaring the socket dead. |
| `TCP_KEEPALIVE_IDLE` | `60` | int | yes | no | Seconds before first keepalive probe. |
| `TCP_KEEPALIVE_INTERVAL` | `10` | int | yes | no | Seconds between keepalive probes. |
| `TRUST_FORWARDED_HEADERS` | `false` | lambda | yes | no | When set, honour X-Forwarded-Proto / X-Forwarded-Host / X-Forwarded-User from the front proxy. Leave false unless SKIFF is behind a trusted reverse proxy (Caddy, nginx, oauth2-proxy) — otherwise the headers are caller-controlled. |
| `TUNNEL_CONNECT_TIMEOUT` | `15` | int | yes | no | Seconds for SSH to establish. |
| `TUNNEL_DEFAULT_SOCKET` | `/tmp/skiff-docker.sock` |  | yes | no | Default local Unix socket path for the SSH tunnel. |
| `TUNNEL_SERVER_ALIVE_COUNT` | `3` | int | yes | no | SSH ServerAliveCountMax. |
| `TUNNEL_SERVER_ALIVE_INTERVAL` | `30` | int | yes | no | SSH ServerAliveInterval. |
| `TUNNEL_SOCKET_POLL` | `0.3` | float | yes | no | Seconds between socket existence polls. |
| `TUNNEL_SOCKET_WAIT` | `10` | int | yes | no | Seconds to wait for tunnel socket to appear. |
| `TUNNEL_STOP_TIMEOUT` | `5` | int | yes | no | Seconds for `ssh -O exit` teardown subprocess. |
| `UNDO_DELAY_SECS` | `5.0` | float | yes | no | Grace period in seconds before an undo-queued destructive op fires. |
| `UNDO_QUEUE_MAX_DEPTH` | `64` | int | yes | no | Max pending undo ops; new enqueues past the cap run synchronously. |
| `UVICORN_LOG_LEVEL` | `warning` |  | yes | no | uvicorn log level (critical\|error\|warning\|info\|debug\|trace). |
| `UVICORN_WORKERS` | `1` | int | yes | no | uvicorn worker count. Must be 1 for the module-level Docker client singleton. |
| `WS_AUTH_LOCKOUT_SECS` | `300` | int | yes | no | Seconds to lock out an IP after max failed WS auth attempts. |
| `WS_AUTH_MAX_ATTEMPTS` | `3` | int | yes | no | Failed WS auth attempts before IP lockout. |
| `WS_EXEC_IDLE_TIMEOUT` | `600` | int | yes | no | Close exec session after N seconds of inactivity. |
| `WS_EXEC_RECV_TIMEOUT` | `0.5` | float | yes | no | Exec socket recv timeout (seconds). |
| `WS_KEEPALIVE_INTERVAL` | `15` | int | yes | no | Seconds between WebSocket ping frames. |
| `WS_KEEPALIVE_REVALIDATE_EVERY` | `1` | int | yes | no | Revalidate session age every N keepalive ticks. |
| `WS_LOG_IDLE_TIMEOUT` | `30` | int | yes | no | Close log stream after N seconds of silence. |
| `WS_LOG_TAIL` | `50` | int | yes | no | Initial tail lines for log streams. |
| `WS_MAX_PER_IP` | `5` | int | yes | no | Max concurrent WS connections per IP. |
| `WS_TOKEN_TIMEOUT` | `5.0` | float | yes | no | Seconds to wait for the first-message AUTH token. |
