# SKIFF Container Manager

A lightweight web UI for Docker — works locally on your machine alongside any container runtime, or remotely over SSH. No agent, no daemon, no installation on the Docker host.

Registry allowlists, compose sandboxing, audit logging, WebSocket log streaming, and a browser UI that stores credentials in `sessionStorage` only (the browser drops them when the tab closes). MIT-licensed — free for personal and commercial use.

![SKIFF Container Manager — containers list](docs/screenshot.png)

![SKIFF Container Manager — live log streaming](docs/screenshot-logs.png)

![SKIFF Container Manager — exec terminal](docs/screenshot-exec.png)

---

## Why SKIFF?

SKIFF connects to any container daemon that speaks the Docker API — Docker Engine, Podman, Colima, OrbStack, Rancher Desktop, or a remote VM over SSH. It runs as a plain Python process: point it at a local socket for local container management, or at an SSH-tunnelled socket for a remote host.

Most container management UIs run as a container on the container host and reach the daemon by mounting `/var/run/docker.sock`. That works, but it has costs that matter on both laptops and cloud hosts:

- **`docker.sock` is root.** A container with socket access can start privileged containers, escape to the host filesystem, or terminate any workload. Security teams regularly flag this; the mitigation (a socket-proxy container) adds another thing to run and maintain.
- **Always-on management plane.** A server-based manager needs a dedicated process or VM running 24/7 — extra cost, another system to patch, a single point of failure.
- **No nested virtualisation needed.** Cloud workstations are VMs themselves. Running containers *inside* them requires nested virt, which not all SKUs support and many security policies prohibit.

SKIFF sidesteps all three: it runs as a plain Python process, the socket is forwarded over SSH (never mounted anywhere), and there is no idle management server.

| Concern | Desktop GUI tools | Server-based manager | SKIFF |
|---|---|---|---|
| Cost | Free (personal) or per-seat commercial | Dedicated management VM / container | Free, MIT |
| API access | GUI only | Yes | Yes — full REST API + audit log |
| `docker.sock` exposure | Mounted locally | Mounted into a privileged container | Same socket SKIFF's operator can reach — local OR SSH-forwarded — never privileged, never bind-mounted into another container |
| Always-on cost | Must be open | Dedicated management VM required | Start when needed; stop when done |
| Registry controls | None built-in | Varies | Allowlist enforced server-side |
| Agent on container host | Not applicable | Required for remote hosts | None — SSH tunnel is the transport (ControlMaster when SKIFF's wizard opens it; any `ssh -fNL` works otherwise) |
| Per-user RBAC | None | Supported (some tools) | Single token by default; front it with an [SSO proxy](docs/hardening/production.md#5-sso-via-identity-proxy-optional-multi-user) for per-user identity |

### Zero-trust-compatible deployment

SKIFF is designed to slot into a zero-trust deployment without itself being a trust anchor. There is no persistent management process on the container host: access is established per-session over SSH, and the authentication layer is whatever your organisation already runs in front of SKIFF — IAP, BeyondCorp, SSH certificates, or any OIDC-aware proxy (see [`docs/hardening/production.md`](docs/hardening/production.md)). SKIFF enforces its own guardrails at the API layer — registry allowlist, compose sandboxing, structured audit log — independent of who is connected.

This sits between two other common patterns: tools that run on the Docker host and therefore require `docker.sock` access (effectively root) plus a persistent management VM; and editor plugins that give developers full daemon access with no guardrails. SKIFF is a full browser UI with the registry controls, compose sandboxing, and audit trail that neither approach provides — without adding its own long-lived privileged process.

Known gaps (see [`SECURITY.md`](SECURITY.md) for the full list) include a shared bearer token (no built-in per-user invalidation without an SSO proxy) and a mutable registry allowlist during runtime configuration.

---

## Features

- **Container lifecycle** — list, run, start, stop, restart, pause, unpause, kill, rename, remove
- **Logs** — tail, search, download, stream via WebSocket; filter by `since`/`until` timestamp
- **Shell access** — interactive exec terminal via WebSocket
- **Images** — list, pull, push (approved registries only), tag, remove, inspect
- **Volumes** — create, delete, prune (named volumes only)
- **Networks** — create, delete, connect/disconnect containers, prune
- **Compose** — deploy and tear down stacks; sandbox-validated before execution
- **System** — container engine info, disk usage, prune all / build cache
- **Security** — Bearer token auth, CSRF protection, registry allowlist, rate limiting, security headers, audit logging

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.12+ | |
| Docker CLI or equivalent | 24+ | For compose commands |
| Docker Engine or equivalent | 24+ | Local or remote |
| pip / venv | any | `run.sh` creates a venv automatically |

---

## Quick start

> **First-run setup window — know before you boot.** If `API_TOKEN` is unset at startup, SKIFF opens a **5-minute first-run wizard** reachable on `BIND_HOST:PORT` (default `127.0.0.1:8080`). Anyone who can reach that socket during the window can claim the instance with their own token. Fine on a single-user workstation (only you can reach localhost). On a shared / remote host, **pre-set `API_TOKEN` in the environment before starting** — the wizard stays closed and the server logs `from_env=true`. See [SECURITY.md § First-run setup window](SECURITY.md#first-run-setup-window-wizard-race) and [`docs/hardening/production.md § setup-window`](docs/hardening/production.md#setup-window) for the full model and mitigations.

### Local use (Colima / OrbStack / Rancher Desktop / Docker Engine)

```bash
git clone https://github.com/yshk-mxim/skiff-container-manager skiff
cd skiff
cp .env.example .env   # DOCKER_HOST is pre-filled; leave API_TOKEN empty only when binding to 127.0.0.1
./run.sh
# Opens http://127.0.0.1:8080
```

`run.sh` creates a `.venv/` virtual environment and installs runtime dependencies automatically. This is the one-command path for **running** SKIFF. If you intend to **develop** SKIFF (run tests, contribute), follow [`CONTRIBUTING.md`](CONTRIBUTING.md) instead — it uses `pip install -e ".[dev]"` so test deps are included.

### Platform socket paths

| Runtime | `DOCKER_HOST` value |
|---|---|
| Mac/Win default socket | `unix:///var/run/docker.sock` |
| Colima | `unix://$HOME/.colima/default/docker.sock` |
| OrbStack | `unix:///var/run/docker.sock` |
| Rancher Desktop | `unix://$HOME/.rd/docker.sock` |
| Linux Docker Engine | `unix:///var/run/docker.sock` |
| Remote via SSH | `unix:///tmp/docker.sock` (after tunnel — see [Remote deployment](#remote-deployment)) |

### Alternative: install from git without cloning

```bash
pip install git+https://github.com/yshk-mxim/skiff-container-manager

export API_TOKEN="$(openssl rand -hex 32)"
export DOCKER_HOST="unix:///var/run/docker.sock"
skiff
# Opens http://127.0.0.1:8080
```

### Remote deployment

For SSH tunnel setup (remote VMs, GCP Cloud Workstations, EC2, bare metal):

```bash
# Forward the remote socket
ssh -fNL /tmp/docker.sock:/var/run/docker.sock user@docker-host

export API_TOKEN="$(openssl rand -hex 32)"
export DOCKER_HOST="unix:///tmp/docker.sock"
./run.sh
```

See [docs/deployment.md](docs/deployment.md) for full remote setup, systemd configuration, and environment variable reference.

### Insecure dev mode (localhost only)

If you just want to click around the UI on your own machine and don't care about auth:

```bash
API_TOKEN="" uvicorn skiff.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

Do **not** use this with `BIND_HOST` other than `127.0.0.1`. Anyone who can reach the socket can use the UI.

---

## Configuration

Copy `.env.example` to `.env` and edit. All values can also be set as environment variables directly.

| Variable | Default | Description |
|---|---|---|
| `API_TOKEN` | _(none)_ | Bearer token for API auth. Leave unset only for local dev. |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Socket path for the container daemon. Recognised by any Docker-API-compatible runtime (Docker Engine, Podman, Colima, etc.) — not Docker-specific. Set to your local socket or SSH-tunnelled socket path. |
| `ALLOWED_REGISTRIES` | `docker.io,ghcr.io` | Comma-separated registry prefixes. Images outside these are rejected. Leave empty to allow all registries (dev only). |
| `ALLOWED_ORIGINS` | `http://127.0.0.1:8080` | Comma-separated CORS origins. |
| `BIND_HOST` | `127.0.0.1` | Address uvicorn listens on. |
| `PORT` | `8080` | Port uvicorn listens on (run.sh only). |
| `DOCKER_VM_HOST` | _(empty)_ | Hostname shown for container port links in the UI. |
| `COMPOSE_DIR` | per-user state dir (see hardening §6) | Directory where uploaded compose YAML files are stored on the SKIFF server. Default is writable without root (e.g. `~/Library/Application Support/skiff/compose/` on macOS). |
| `AUDIT_LOG` | per-user state dir (see hardening §6) | Audit log path. Default is writable without root (e.g. `~/Library/Application Support/skiff/audit.jsonl` on macOS, `~/.local/state/skiff/audit.jsonl` on Linux). Override in production with a fixed path such as `/var/log/skiff-audit.jsonl`. |
| `AUDIT_MAX_MB` | `10` | Max size per audit log file in MB. Increase for longer retention (e.g. `200` for ~1-year at moderate traffic). |
| `AUDIT_BACKUP_COUNT` | `5` | Number of rotated audit log files to keep. Total retention ≈ `AUDIT_MAX_MB × AUDIT_BACKUP_COUNT`. |
| `GOOGLE_CLOUD_PROJECT` | _(empty)_ | GCP project ID. When set (and `google-cloud-logging` installed via `pip install 'skiff-container-manager[gcp]'`), all audit log entries are dual-written to GCP Cloud Logging. |
| `GCP_LOG_NAME` | `skiff-audit` | Cloud Logging log name (only used when `GOOGLE_CLOUD_PROJECT` is set). |
| `RATE_LIMIT_SCALE` | `1` | Multiply all rate limits by this factor (e.g. `100` for CI / load tests). Must be between 1 and 100. |
| `PROFILE` | _(empty)_ | One of `homelab`, `dev`, `sre`, `reviewer`, `tutor`, `ci`. Applies a documented bundle of sensible defaults for each persona (see [docs/hardening/production.md §14](docs/hardening/production.md#14-profile-presets)). Explicit env vars always win over the preset. |

---

## API

See [docs/api-reference.md](docs/api-reference.md) for the full endpoint reference.

| Prefix | Resource |
|---|---|
| `GET /health` | Liveness probe (no auth) |
| `GET /ready` | Readiness — returns 503 if container daemon unreachable (no auth) |
| `GET /api/auth-required` | Auth config for the UI (no auth) |
| `/api/containers/...` | Container lifecycle, logs, inspect, stats |
| `/api/images/...` | Image operations |
| `/api/volumes/...` | Volume operations |
| `/api/networks/...` | Network operations |
| `/api/compose/...` | Compose stacks |
| `/api/registry/...` | Docker Hub search and tag lookup |
| `/api/system/...` | System info, prune, and audit log |
| `/api/config` | Server config returned to the UI |
| `WS /ws/logs/{id}` | Stream container logs |
| `WS /ws/exec/{id}` | Interactive shell |

All `/api/` endpoints require `Authorization: Bearer <token>`.
Mutating endpoints (`POST`, `DELETE`) also require `X-Requested-With: ContainerManager`.

---

## Deployment (systemd)

A systemd unit file is provided at `docs/skiff.service`. To install:

```bash
sudo cp docs/skiff.service /etc/systemd/system/skiff@.service
sudo systemctl daemon-reload
sudo systemctl enable --now skiff@$USER
sudo journalctl -u skiff@$USER -f
```

The template is per-instance (`skiff@<name>.service`); each instance
reads `/opt/skiff/<name>.env`. Enable as `systemctl enable --now
skiff@prod.service`, `systemctl enable --now skiff@staging.service`,
etc. Each env file sets `PORT`, `DOCKER_HOST`, `API_TOKEN`, and any
other per-instance overrides.

---

## Development

```bash
git clone https://github.com/yshk-mxim/skiff-container-manager skiff
cd skiff
python3 -m venv .venv && source .venv/bin/activate

# Unit tests — no container daemon required
pip install -e ".[dev]"
make test-unit

# Run with hot-reload
cp .env.example .env   # set API_TOKEN="" for no-auth dev mode
API_TOKEN="" uvicorn skiff.app:app --reload --host 127.0.0.1 --port 8080 --no-proxy-headers
```

```bash
make lint        # ruff check
make format      # ruff format + auto-fix
make test-unit   # fast unit tests (no container daemon required)
make test-e2e    # Playwright e2e (requires pip install -e .[dev,e2e] && playwright install chromium)
make coverage    # coverage report
make security    # ruff --select S security scan
make ci          # full CI: lint + lint-antipatterns + lint-js + lint-md + lint-asvs + lint-notice + security + docs-check + coverage
```

---

## Security

See [SECURITY.md](SECURITY.md) for the security model, design trade-offs, and vulnerability reporting process. See [docs/hardening/production.md](docs/hardening/production.md) for the operator deployment and hardening guide (TLS, token rotation, registry scoping, SSO, audit log retention, supply chain).

---

## License

MIT — see [LICENSE](LICENSE).
