# Local Docker Engine

The app connects to any Docker Engine via the standard socket — not only a remote GCP VM. Set `DOCKER_HOST` to your local socket and all API endpoints operate against your local daemon.

## Quick start

```bash
# Install (development)
pip install -e .[dev]
# — or for a one-time install from PyPI/git —
# pip install skiff

export API_TOKEN="$(openssl rand -hex 32)"

# DOCKER_HOST defaults to unix:///var/run/docker.sock when unset,
# which works with Docker Engine on Linux or any local daemon.
uvicorn skiff.app:app --host 127.0.0.1 --port 8080
# — or use the CLI entry point —
# skiff
```

To point at a specific socket explicitly:

```bash
export DOCKER_HOST="unix:///var/run/docker.sock"
```

## Available API surface

All endpoints documented in [api-reference.md](api-reference.md) work against a local daemon. Key groups:

| Group | Base path |
|---|---|
| Containers | `/api/containers` |
| Images | `/api/images` |
| Volumes | `/api/volumes` |
| Networks | `/api/networks` |
| Compose | `/api/compose` |
| System | `/api/system` |

## Registry allowlist

Locally you likely want to pull from Docker Hub or `ghcr.io`. Expand `ALLOWED_REGISTRIES`:

```bash
export ALLOWED_REGISTRIES="docker.io,ghcr.io,us-docker.pkg.dev,quay.io"
```

The app validates every pull/push/run image reference against this list, so add any registry you use.

## Differences from GCP deployment

| | Local | GCP Cloud Workstation |
|---|---|---|
| `DOCKER_HOST` | `unix:///var/run/docker.sock` (or omit) | `unix:///tmp/docker.sock` (SSH tunnel) |
| `DOCKER_VM_HOST` | not needed | set to VM internal IP (SSH connectivity check) |
| Network overhead | none | SSH tunnel latency |
| Auth to registries | `docker login` locally | `gcloud auth configure-docker` on VM |
| HTTPS / TLS | optional (loopback only) | recommended behind Cloud Workstation proxy |

## HTTPS locally (optional)

For browser access with a real origin header, run behind a local reverse proxy:

```bash
# With Caddy (auto-TLS on localhost)
caddy reverse-proxy --from localhost:443 --to 127.0.0.1:8080
```

Or use `mkcert` + uvicorn:

```bash
mkcert localhost
uvicorn skiff.app:app --ssl-keyfile localhost-key.pem --ssl-certfile localhost.pem --port 8443
```

## Verify it works

```bash
curl -sf http://127.0.0.1:8080/health        # no auth required
curl -sf http://127.0.0.1:8080/ready         # 200 = daemon reachable, 503 = not

curl -sf -H "Authorization: Bearer $API_TOKEN" \
  http://127.0.0.1:8080/api/containers | python3 -m json.tool
```
