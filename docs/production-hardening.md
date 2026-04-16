# Production Hardening Guide

Operational guidance for deploying SKIFF securely. The defaults are tuned for local development — apply the steps below for any deployment that is accessible beyond your own laptop.

---

## 1. TLS Termination

The app serves plain HTTP. Place it behind a TLS-terminating reverse proxy.

**Caddy (automatic HTTPS — recommended for most deployments):**

```bash
caddy reverse-proxy --from https://containers.example.com --to localhost:8080
```

**nginx:**

```nginx
server {
    listen 443 ssl;
    server_name containers.example.com;
    ssl_certificate     /etc/ssl/certs/your.crt;
    ssl_certificate_key /etc/ssl/private/your.key;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**Alternatives:**
- **Tailscale HTTPS** — `tailscale serve https / http://localhost:8080` (no cert management)
- **Cloud Workstation proxy** — GCP Cloud Workstations automatically forward ports over HTTPS; no additional proxy needed

---

## 2. Binding and Network Isolation

Prevent direct network exposure by binding to loopback only (the default):

```bash
BIND_HOST=127.0.0.1  # default — keep this
```

Do not set `BIND_HOST=0.0.0.0` unless you have a firewall or are inside an isolated VPC.

**Additional network controls:**
- Firewall rules / security groups limiting source IPs
- VPN (Tailscale, WireGuard) — only VPN members can reach the port
- Cloud VPC / private subnet

**Cloud-specific (optional):**
- **GCP**: Cloud Workstation IAP proxy handles identity-aware access at the network layer
- **AWS**: Security groups + SSM Session Manager for tunnel-free access
- **Azure**: Azure Bastion or private endpoint

---

## 3. API_TOKEN Lifecycle

Generate a strong token (minimum 16 characters, 32+ recommended):

```bash
openssl rand -hex 32
```

**Rotation cadence:**
- Rotate immediately on personnel changes (anyone who knew the token should no longer have access)
- Rotate at least every 90 days in shared environments
- Rotate immediately if you suspect compromise

**Revocation:** Restart the server with the new token. All existing sessions are invalidated immediately.

The setup wizard enforces a minimum 16-character token. Shorter tokens are rejected at startup.

---

## 4. ALLOWED_REGISTRIES Scoping

The default (`docker.io,ghcr.io`) permits Docker Hub and GitHub Container Registry. Restrict further in production:

```bash
# Docker Hub public images only
ALLOWED_REGISTRIES=docker.io

# Your org's GHCR only
ALLOWED_REGISTRIES=ghcr.io/myorg/

# Self-hosted registry
ALLOWED_REGISTRIES=registry.internal.example.com

# Multiple registries
ALLOWED_REGISTRIES=ghcr.io/myorg/,registry.internal.example.com
```

An empty `ALLOWED_REGISTRIES` permits all registries and triggers a startup warning — acceptable for local dev, not for production.

**Cloud registries (optional):**
- **AWS ECR**: `ALLOWED_REGISTRIES=123456789.dkr.ecr.us-east-1.amazonaws.com/`
- **GCP Artifact Registry**: `ALLOWED_REGISTRIES=us-docker.pkg.dev/my-project/`

---

## 5. SSO via Identity Proxy (optional, multi-user)

For teams that need per-user identity without modifying the app, place an OAuth2 proxy in front:

**GitHub (recommended for open-source teams):**

```bash
oauth2-proxy \
  --provider=github \
  --upstream=http://127.0.0.1:8080 \
  --cookie-secret=$(openssl rand -hex 16) \
  --client-id=<github-app-client-id> \
  --client-secret=<github-app-client-secret> \
  --github-org=myorg
```

Works with any OIDC provider: Google, Okta, Azure AD, Keycloak. No code changes required. The proxy handles login and passes the authenticated user's identity via `X-Forwarded-User` — SKIFF logs this automatically as the `user` field in every audit entry.

---

## 6. Audit Log Retention and SIEM Export

The app writes structured JSON to stdout and a rotating JSONL file. Each entry includes `event_type`, `method`, `path`, `status`, `remote`, `auth`, and optionally `user` (from `X-Forwarded-User`).

**Configure retention:**

```bash
# ~1-year retention at ~4 MB/day
AUDIT_MAX_MB=200
AUDIT_BACKUP_COUNT=20
AUDIT_LOG=/var/log/skiff-audit.jsonl
```

**Ship to a log aggregator:**
- **Loki + Grafana** (open source): tail the JSONL file with Promtail or Alloy
- **ELK / OpenSearch**: use Filebeat to tail and forward

**Cloud-specific (optional):**
- **GCP Cloud Logging** — install the optional dep and set the project:
  ```bash
  pip install skiff[gcp]
  export GOOGLE_CLOUD_PROJECT=my-project-id
  export GCP_LOG_NAME=skiff-audit   # optional, default: skiff-audit
  ```
  SKIFF dual-writes every log entry to Cloud Logging alongside the local file and stdout.

**Useful `event_type` values for alerting:**

| Event type | Description |
|---|---|
| `auth.denied` | 401/403 response — failed or missing token |
| `rate_limit.exceeded` | 429 response |
| `container.run` | New container started |
| `container.action` | Start/stop/restart/kill on existing container |
| `compose.deployed` | Compose stack brought up |
| `image.pull` | Image pulled from registry |
| `audit.log_read` | Audit log accessed |
| `setup.configured` | Server configured via setup wizard |

---

## 7. Session Timeout Tuning

Defaults: **15-minute idle timeout** (client-side) and **8-hour absolute timeout** (client-side and server-side).

Session timeouts involve constants in two places that must be kept in sync:

| Constant | File | Default |
|---|---|---|
| `SESSION_IDLE_MS` | `skiff/static/app.js` | `15 * 60 * 1000` (15 min) |
| `SESSION_ABSOLUTE_MS` | `skiff/static/app.js` | `8 * 60 * 60 * 1000` (8 hr) |
| `SESSION_ABS_TIMEOUT` | `skiff/config.py` | `8 * 60 * 60` (8 hr, seconds) |

To tighten for high-security environments, edit both files and keep them in sync:

```javascript
// skiff/static/app.js
var SESSION_IDLE_MS     = 10 * 60 * 1000;   // 10 minutes
var SESSION_ABSOLUTE_MS =  4 * 60 * 60 * 1000;  // 4 hours
```

```python
# skiff/config.py
SESSION_ABS_TIMEOUT = 4 * 60 * 60  # seconds — must match SESSION_ABSOLUTE_MS above
```

> **Known gap:** These constants are not yet configurable via environment variables. A future version will expose them as `SESSION_IDLE_MINUTES` and `SESSION_ABSOLUTE_HOURS` env vars.

---

## 8. Browser Security Model

The browser UI stores the API token in `sessionStorage` only:

| Key | Storage | Cleared on |
|---|---|---|
| `api_token` | `sessionStorage` | 401 response, idle timeout, absolute timeout, logout, tab close |
| `session_start` | `sessionStorage` | same |

**What is NOT stored:**
- No `localStorage` usage — credentials do not persist across browser sessions
- No cookies — no CSRF cookie needed (CSRF header is used instead)

Closing the browser tab clears all credentials. Opening a new tab requires re-entering the token.

---

## 9. File Permissions and SSH Key Hygiene

```bash
chmod 600 .env                    # API_TOKEN and secrets
chmod 600 ~/.ssh/docker_engine    # SSH key for tunnel
```

Use a dedicated SSH key pair for the Docker tunnel — not a personal key. Restrict it to the target host only via `~/.ssh/authorized_keys` options on the Docker host:

```
command="/bin/false",no-pty,no-agent-forwarding,no-X11-forwarding,permitopen="/var/run/docker.sock" ssh-ed25519 AAAA... skiff-tunnel-key
```

> **Local use only:** These steps apply only when using SKIFF with a remote Docker host over SSH.

---

## 10. Dependency Scanning

Run `pip-audit` before every deployment and on a weekly schedule:

```bash
pip install pip-audit
pip-audit --strict -r requirements.txt
```

A GitHub Actions workflow can automate this — see `.github/workflows/security.yml` if present in the repository.

---

## 11. Supply Chain Hardening

- **Hash-pinned requirements** — `requirements.txt` is generated with `pip-compile --generate-hashes` and includes SHA-256 hashes for all available wheels (every platform, every Python version) plus source distributions. Because pip-compile queries PyPI for the full set of available distributions, the file is **cross-platform** for pip and uv users on macOS, Linux, and Windows. Install with `pip install --require-hashes -r requirements.txt` to reject tampered packages.

  > **Platform note:** pip and uv both consume the same PyPI hashes. Conda uses a separate package ecosystem with incompatible hashes; conda users should install from conda-forge or use a conda environment file. The hash-pinned `requirements.txt` is not intended for conda.

  To regenerate for a new release:
  ```bash
  make deps   # runs pip-compile --generate-hashes
  ```

- **Dependabot** — automated dependency update PRs (see `.github/dependabot.yml`).
- **YAML safety** — the compose validator uses `yaml.safe_load` only; `yaml.load` is never called.
- **Startup version logging** — installed dependency versions are logged to the audit log at startup to aid post-incident forensics.

---

## 12. Incident Response Outline

1. **Detect** — pip-audit alert, anomalous audit log event (`auth.denied` spike, unexpected `image.pull` from unknown registry)
2. **Contain** — stop server (`systemctl stop skiff@$USER`), rotate `API_TOKEN`, snapshot audit log (`cp audit.jsonl audit-incident-$(date +%s).jsonl`)
3. **Investigate** — `pip show <package>` for installed versions, audit JSONL for request history, OS process accounting
4. **Recover** — deploy from a clean environment with verified `requirements.txt` hashes; verify SBOM matches expected
5. **Notify** — if user data could be affected, follow responsible disclosure practices

---

## 13. Least-Privilege System Account

Run SKIFF as a dedicated non-root OS user with no sudo rights:

```bash
# Linux
sudo useradd --system --no-create-home --shell /bin/false skiff
sudo chown -R skiff:skiff /opt/skiff
sudo -u skiff ./run.sh
```

The provided `docs/skiff.service` systemd unit uses `User=` to run as a dedicated account. Set it to the dedicated user:

```ini
[Service]
User=skiff
Group=skiff
```

The `skiff` account needs:
- Read access to the SKIFF directory
- Write access to `AUDIT_LOG` path
- Read access to `DOCKER_HOST` socket (add to `docker` group if needed)
- No sudo, no shell, no home directory
