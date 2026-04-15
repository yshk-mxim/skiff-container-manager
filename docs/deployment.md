# Deployment Guide

## GCP Cloud Workstation (recommended)

SKIFF Container Manager is a cloud-native container manager designed to run on a GCP Cloud Workstation (or any cloud-hosted machine), connecting to a separate Docker Engine VM via SSH.

### Prerequisites

1. **Python 3.12+** on the workstation.
2. **Docker CLI** installed:
   ```bash
   sudo apt-get install -y docker-ce-cli
   ```
3. **SSH tunnel** to the Docker VM socket (key-based auth, no passphrase):
   ```bash
   ssh-copy-id dev@docker-vm
   ssh -fNL /tmp/docker.sock:/var/run/docker.sock dev@docker-vm
   ```
   Verify:
   ```bash
   DOCKER_HOST=unix:///tmp/docker.sock docker info
   ```
4. **GCP Artifact Registry auth** configured on the Docker VM:
   ```bash
   ssh dev@docker-vm gcloud auth configure-docker us-docker.pkg.dev
   ```

### Running

```bash
ssh -fNL /tmp/docker.sock:/var/run/docker.sock dev@docker-vm

export API_TOKEN="$(openssl rand -hex 32)"
export DOCKER_HOST="unix:///tmp/docker.sock"
export ALLOWED_REGISTRIES="us-docker.pkg.dev/my-project/"
./run.sh
```

The script installs Python dependencies if missing, validates the environment, and starts uvicorn on `127.0.0.1:8080`.

### Cloud Workstation port forwarding

Cloud Workstations automatically proxy ports to `https://<port>-<id>.cloudworkstations.dev`. The UI's WebSocket and CORS are handled transparently via the same-origin check in the server.

Set `DOCKER_VM_HOST` to the hostname shown for container port links if the Docker VM has a known hostname:
```bash
export DOCKER_VM_HOST="docker-vm.internal"
```

---

## Environment Variables Reference

| Variable | Default | Required in production |
|---|---|---|
| `API_TOKEN` | _(none)_ | Yes |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Yes (set to tunnel socket) |
| `ALLOWED_REGISTRIES` | `us-docker.pkg.dev/` | Recommended |
| `ALLOWED_ORIGINS` | `http://127.0.0.1:8080` | Yes (set to your workstation URL) |
| `BIND_HOST` | `127.0.0.1` | No |
| `PORT` | `8080` | No |
| `DOCKER_VM_HOST` | _(empty)_ | No |
| `COMPOSE_DIR` | `/data/compose` | No |
| `AUDIT_LOG` | `/var/log/skiff-audit.jsonl` | No (ensure path is writable) |
| `RATE_LIMIT_SCALE` | `1` | No (increase for CI or load tests) |

---

## Security Checklist

- [ ] `API_TOKEN` is set and not empty.
- [ ] `ALLOWED_REGISTRIES` is scoped to your project (e.g., `us-docker.pkg.dev/my-project/`).
- [ ] `ALLOWED_ORIGINS` is set to the actual Cloud Workstation URL (wildcards `*` are rejected at startup).
- [ ] `AUDIT_LOG` is set to a writable path or the default `/var/log/skiff-audit.jsonl` is writable.
- [ ] SSH key for `DOCKER_HOST` is restricted to the Docker VM only.
- [ ] The server is not exposed to the public internet (runs behind the Cloud Workstation proxy).

---

## Updating

```bash
git pull
pip install -e .[dev]   # or: pip install skiff --upgrade
./run.sh
```

No database migrations or state to manage — all state lives in Docker on the remote VM.
