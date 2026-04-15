# SKIFF Container Manager

A cloud-native container manager with a web UI. Built for teams running Docker on remote VMs — GCP Cloud Workstations, EC2, bare metal — accessible securely over an SSH tunnel from anywhere with a browser.

![SKIFF Container Manager UI](docs/screenshot.png)

## Features

- **Container lifecycle** — list, run, start, stop, restart, pause, unpause, kill, rename, remove
- **Logs** — tail, search, download, stream via WebSocket; filter by `since`/`until` timestamp
- **Shell access** — interactive exec terminal via WebSocket
- **Images** — list, pull, push (approved registries only), tag, remove, inspect
- **Volumes** — create, delete, prune (named volumes only)
- **Networks** — create, delete, connect/disconnect containers, prune
- **Compose** — deploy and tear down stacks; sandbox-validated before execution
- **System** — Docker engine info, disk usage, prune all / build cache
- **Security** — Bearer token auth, CSRF protection, registry allowlist, rate limiting, security headers, audit logging

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| Docker CLI | 24+ | For compose commands |
| Docker Engine | 24+ | Local or remote |
| pip / venv | any | `run.sh` creates a venv automatically |

---

## Quick start

### Option A — clone and run (recommended)

```bash
git clone https://github.com/yshk-mxim/skiff-container-manager skiff
cd skiff

cp .env.example .env
# Edit .env — set API_TOKEN and DOCKER_HOST at minimum

./run.sh
# Opens http://127.0.0.1:8080
```

`run.sh` creates a `.venv/` virtual environment and installs all dependencies automatically.

### Option B — pip install from git

```bash
pip install git+https://github.com/yshk-mxim/skiff-container-manager

# Configure via environment variables or a .env file in the working directory
export API_TOKEN="$(openssl rand -hex 32)"
export DOCKER_HOST="unix:///var/run/docker.sock"

skiff
# Opens http://127.0.0.1:8080
```

---

## Platform setup

### Linux (local Docker Engine)

```bash
# Docker Engine is at /var/run/docker.sock by default
cp .env.example .env
# Edit .env: set API_TOKEN, leave DOCKER_HOST as unix:///var/run/docker.sock
./run.sh
```

### macOS (Docker Desktop)

```bash
cp .env.example .env
# Edit .env: set API_TOKEN
# Docker Desktop exposes the socket at /var/run/docker.sock automatically
./run.sh
```

### Windows (WSL2)

`dockerd` does not start automatically in WSL2. Start it first:

```bash
# In WSL2 terminal
sudo dockerd &>/tmp/dockerd.log &

# Wait a few seconds, then:
cp .env.example .env
# Edit .env: set API_TOKEN, DOCKER_HOST=unix:///var/run/docker.sock
./run.sh
```

To start `dockerd` automatically, enable systemd in WSL2 via `/etc/wsl.conf`:
```ini
[boot]
systemd=true
```
Then `sudo systemctl enable docker`.

### Remote Docker host (SSH tunnel)

```bash
# Open the tunnel first (once per session)
ssh -fNL /tmp/docker.sock:/var/run/docker.sock user@docker-host

# Configure and start
cp .env.example .env
# Edit .env:
#   API_TOKEN=<your-token>
#   DOCKER_HOST=unix:///tmp/docker.sock
./run.sh
```

### GCP Cloud Workstation

See [docs/deployment.md](docs/deployment.md) for the full GCP setup guide.

```bash
# On the Docker Engine VM:
gcloud auth configure-docker us-docker.pkg.dev

# On the Cloud Workstation:
ssh -fNL /tmp/docker.sock:/var/run/docker.sock dev@<DOCKER_VM_IP>

cp .env.example .env
# Edit .env:
#   API_TOKEN=<your-token>
#   DOCKER_HOST=unix:///tmp/docker.sock
#   ALLOWED_REGISTRIES=us-docker.pkg.dev/my-project/
./run.sh
```

---

## How it connects to Docker

The app talks to Docker via a Unix socket. Set `DOCKER_HOST` to point at the right socket:

| Scenario | DOCKER_HOST |
|---|---|
| Local Docker Engine (Linux / macOS Desktop) | `unix:///var/run/docker.sock` |
| SSH tunnel to remote host | `unix:///tmp/docker.sock` |
| TCP (insecure, LAN only) | `tcp://192.168.1.10:2375` |

---

## Configuration

Copy `.env.example` to `.env` and edit. All values can also be set as environment variables directly.

| Variable | Default | Description |
|---|---|---|
| `API_TOKEN` | _(none)_ | Bearer token for API auth. Leave unset only for local dev. |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker socket path (local or SSH-tunnelled). |
| `ALLOWED_REGISTRIES` | `us-docker.pkg.dev/` | Comma-separated registry prefixes. Images outside these are rejected. |
| `ALLOWED_ORIGINS` | `http://127.0.0.1:8080` | Comma-separated CORS origins. |
| `BIND_HOST` | `127.0.0.1` | Address uvicorn listens on. |
| `PORT` | `8080` | Port uvicorn listens on (run.sh only). |
| `DOCKER_VM_HOST` | _(empty)_ | Hostname shown for container port links in the UI. |
| `COMPOSE_DIR` | `/data/compose` | Directory where compose files are stored on the server. |
| `AUDIT_LOG` | `/var/log/skiff-audit.jsonl` | Audit log path. Set to a writable path (e.g. `./audit.jsonl`). |
| `RATE_LIMIT_SCALE` | `1` | Multiply all rate limits by this factor (e.g. `100` for CI / load tests). |

---

## Deployment (systemd)

A systemd unit file is provided at `docs/skiff.service`. To install:

```bash
sudo cp docs/skiff.service /etc/systemd/system/skiff@.service
sudo systemctl daemon-reload
sudo systemctl enable --now skiff@$USER
sudo journalctl -u skiff@$USER -f
```

The service reads configuration from `/opt/skiff/.env`.

---

## API

See [docs/api-reference.md](docs/api-reference.md) for the full endpoint reference.

| Prefix | Resource |
|---|---|
| `GET /health` | Liveness probe (no auth) |
| `GET /ready` | Readiness — returns 503 if Docker unreachable (no auth) |
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

## Why SKIFF instead of a container management server?

Most Docker management UIs are designed around a different assumption: they run as a container *on* the Docker host itself and reach the daemon through the Unix socket mounted into that container. That works well for always-on infrastructure, but it carries a few architectural costs that matter in cloud workstation environments.

**The `docker.sock` risk.** Mounting `/var/run/docker.sock` into a container is equivalent to granting root access to the host — the container can start new privileged containers, escape to the host filesystem, or terminate any running workload. Security teams regularly flag this pattern; the standard mitigation is adding a separate socket-proxy container to filter which API calls are allowed, which adds another moving part to maintain.

**The always-on management plane.** A server-based manager needs a VM dedicated to running the management UI, even when no one is actively using it — extra compute cost, another system to patch, a single point of failure, and (the real irony) a VM that exists purely to manage your other VMs. This cost is invisible when your workloads already run 24/7, but it's wasteful for on-demand or dev workloads.

**The nested-virtualisation problem on cloud workstations.** Cloud workstations (GCP Cloud Workstations, AWS WorkSpaces, Azure Dev Box, etc.) are VMs themselves. Running Docker inside them requires nested virtualisation, which not all SKUs support, adds a kernel-sharing exposure layer, and can conflict with corporate security policies that prohibit nested virt.

**How SKIFF handles these differently:**

| Concern | Server-based manager | SKIFF |
|---|---|---|
| `docker.sock` exposure | Socket mounted into a privileged container on the host | Socket forwarded over SSH — never mounted anywhere |
| Always-on cost | Dedicated management VM required | Runs as a Python process on your workstation; start it when you need it |
| Nested virtualisation | Required if running on a cloud workstation | Not required — SKIFF is not a container |
| Agent on Docker host | Required for remote hosts | None — SSH ControlMaster is the transport |
| Multiple hosts | Supported (some tools) | One Docker host per instance |
| RBAC | Supported (some tools) | Single bearer token — all-or-nothing access |

**The right tool for the right situation.** If you manage many Docker hosts, need per-user RBAC, or run Swarm/Kubernetes, a server-based manager is probably the better fit. SKIFF is designed for teams where developers have cloud workstations and Docker runs on a separate VM in the same VPC — the SSH tunnel is the security boundary, there is no idle management server, and the manager runs only when someone is actively using it.

---

## Security

- **Registry allowlist** — `ALLOWED_REGISTRIES` restricts which images can be pulled, pushed, or run.
- **Compose sandboxing** — compose files are validated before execution; `privileged`, host path mounts, `cap_add`, `devices`, `build`, and unapproved registries are rejected.
- **Volume sandboxing** — only named volumes are permitted; host path mounts are rejected.
- **WebSocket auth** — tokens sent as the first WebSocket message, not query parameters.
- **Rate limiting** — all endpoints rate-limited via slowapi.
- **Security headers** — CSP, X-Frame-Options, HSTS, Referrer-Policy, Permissions-Policy.
- **Audit logging** — every API request logged with method, path, status, IP, and auth status.

---

## Development

```bash
git clone https://github.com/yshk-mxim/skiff-container-manager skiff
cd skiff
python3 -m venv .venv && source .venv/bin/activate

# Unit tests — no Docker daemon required
pip install -e .[dev]
make test-unit

# Run with hot-reload
cp .env.example .env   # set API_TOKEN="" for no-auth dev mode
API_TOKEN="" uvicorn skiff.app:app --reload --host 127.0.0.1 --port 8080
```

```bash
make lint        # ruff check
make format      # ruff format + auto-fix
make test-unit   # fast unit tests (no Docker required)
make test-e2e    # Playwright e2e (requires pip install -e .[dev,e2e] && playwright install chromium)
make coverage    # coverage report
make security    # bandit-equivalent security scan
make ci          # lint + security + unit tests
```

---

## License

MIT — see [LICENSE](LICENSE).
