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

**Suggested rotation cadence** (operator policy, not maintainer-enforced):
- Rotate immediately on personnel changes (anyone who knew the token should no longer have access).
- Rotate at least every 90 days in shared environments.
- Rotate immediately on any suspected compromise.

**Revocation paths:**

- **Environment-configured (`API_TOKEN` set in env):** restart the server with the new token. All existing sessions are invalidated immediately. This is the recommended mode for non-local deployments.
- **Session-only configured (token set via the setup wizard):** call `POST /api/auth/rotate-token` with a valid current token and `{"new_token": "<new-value>"}`. The old token stops working immediately without a restart. The endpoint is disabled in environment-configured mode (returns 403) so the two paths don't compete.

**Full re-setup for session-only installs:** `POST /api/auth/reset-config` clears the in-memory token, Docker host, and registry list, stops any managed SSH tunnel, and reopens the 5-minute setup window so the next visitor runs the wizard fresh. Disabled when `API_TOKEN` came from the environment. Useful when handing a running instance off to another operator without a full restart.

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

Works with any OIDC provider: Google, Okta, Azure AD, Keycloak. No code changes required. The proxy handles login and passes the authenticated user's identity via `X-Forwarded-User` — SKIFF logs this as the `user` field in every audit entry **when `TRUST_FORWARDED_HEADERS=true` is set on the server**. Without that flag (the default), SKIFF's `StripForwardedHeadersMiddleware` drops the header so a direct caller can't forge audit attribution; enable the flag only when a trusted proxy (oauth2-proxy, Caddy, nginx) fronts SKIFF and sanitises the header. Launch the server with `--proxy-headers --forwarded-allow-ips "127.0.0.1"` (or equivalent) to mirror the flag at the uvicorn layer.

---

## 6. Audit Log Retention and SIEM Export

The app writes structured JSON to stdout and a rotating JSONL file. Each entry includes `event_type`, `method`, `path`, `status`, `remote`, `auth`, and optionally `user` (populated from `X-Forwarded-User` only when `TRUST_FORWARDED_HEADERS=true` — see §5).

### Default audit-log location (per OS)

If `AUDIT_LOG` is not set, SKIFF picks a writable per-user location based on the
platform's convention. No root privilege is required for the default.

| Platform | Default path |
|---|---|
| macOS | `~/Library/Application Support/skiff/audit.jsonl` |
| Linux (any distro) | `$XDG_STATE_HOME/skiff/audit.jsonl` if `XDG_STATE_HOME` is set, otherwise `~/.local/state/skiff/audit.jsonl` |
| WSL2 | Same as Linux |
| Windows (direct, rare) | `~/.local/state/skiff/audit.jsonl` (Python interprets `~` via `USERPROFILE`) |
| No `HOME`/`USERPROFILE` (containerised, systemd without `HOME=`) | `<working-dir>/.skiff/audit.jsonl` |

SKIFF prints a one-line WARNING at startup if the path is not writable and falls back to stdout-only logging. The `COMPOSE_DIR` default (where uploaded compose YAML files are stored) lives in the same parent directory: `…/skiff/compose/`.

**Production override:** set `AUDIT_LOG` explicitly for any deployment that needs a stable, rotated-by-ops path such as `/var/log/skiff-audit.jsonl`. The system account running SKIFF needs write access to the parent directory — see §13 Least-Privilege System Account.

**Configure retention:**

```bash
# ~1-year retention at ~4 MB/day
AUDIT_MAX_MB=200
AUDIT_BACKUP_COUNT=20
AUDIT_LOG=/var/log/skiff-audit.jsonl
```

**Ship to a log aggregator:**

- **Loki + Grafana** (open source): tail the JSONL file with **Grafana Alloy** (the current agent).
  Promtail reached EOL on **March 2, 2026** — do not use it for new deployments.
  Alloy requires an explicit JSON pipeline stage to promote fields to labels; without it,
  `{event_type="auth.denied"}` LogQL queries will not work:
  ```alloy
  loki.source.file "skiff_audit" {
    targets = [{ __path__ = "/var/log/skiff-audit.jsonl" }]
    forward_to = [loki.process.parse_json.receiver]
  }
  loki.process "parse_json" {
    stage.json {
      expressions = { event_type = "", status = "", remote = "" }
    }
    stage.labels {
      values = { event_type = "", status = "" }
    }
    forward_to = [loki.write.default.receiver]
  }
  ```

- **Elasticsearch / ELK**: use Filebeat to tail and forward. Filebeat is actively maintained by Elastic and connects natively to Elasticsearch.

- **OpenSearch**: use **Fluent Bit** with the `opensearch` output plugin (added in Fluent Bit 1.9, current version 3.x). Do NOT use Filebeat — Filebeat 7.13+ explicitly rejects connections to non-Elastic endpoints and is functionally incompatible with OpenSearch.
  ```ini
  [OUTPUT]
      Name            opensearch
      Match           *
      Host            your-opensearch-host
      Port            9200
      Index           skiff-audit
      Type            _doc
      tls             On
  ```

**Cloud-specific (optional):**
- **GCP Cloud Logging** — install the optional dep and set the project:
  ```bash
  pip install 'skiff-container-manager[gcp]'
  export GOOGLE_CLOUD_PROJECT=my-project-id
  export GCP_LOG_NAME=skiff-audit   # optional, default: skiff-audit
  ```
  SKIFF dual-writes every log entry to Cloud Logging alongside the local file and stdout.
  The `google-cloud-logging` v3.x library auto-detects the GCP resource from the
  environment (GKE, Cloud Run, GCE, etc.); `GOOGLE_CLOUD_PROJECT` overrides the project.

**Useful `event_type` values for alerting:**

| Event type | Priority | Description |
|---|---|---|
| `auth.denied` | Critical | 401/403 response — failed or missing token |
| `rate_limit.exceeded` | High | 429 response — automated tooling signal |
| `container.run` | High | New container started from an image |
| `container.started` / `container.stopped` / `container.restarted` / `container.killed` / `container.paused` / `container.unpaused` / `container.renamed` / `container.removed` / `container.deleted` | Medium | Lifecycle action on an existing container — see `docs/audit-events.md` for the full list |
| `compose.up` | Medium | Compose stack deployed |
| `compose.down` | Medium | Compose stack torn down |
| `image.pulled` | Medium | Image pulled from registry |
| `image.pushed` | Medium | Image pushed to registry |
| `audit.api_access` with `path` matching `/api/system/audit-log` | Low | Audit log tailed via API |
| `setup.configured` | Info | Server configured via setup wizard |

**Note on naming.** The left column is the `event_type` value emitted by the
middleware (see `docs/audit-events.md` for the complete catalogue). The top-level
`event` key on `audit.api_access` lines is literally `"audit.api_access"` — SIEM
rules should key on `event_type`, not `event`.

**Primary alert — single-IP auth-failure threshold.**
A brute-force attempt against the bearer token shows up as a burst of
`auth.denied` from one `remote` before the rate limiter intervenes.
This is the highest-fidelity signal at SKIFF's default rate-limit tiers
and should be the first alert an operator wires.

Splunk SPL:
```spl
index=skiff event_type="auth.denied"
| bin _time span=5m
| stats count as denied by _time, remote
| where denied >= 10
```

Microsoft Sentinel KQL:
```kql
SkiffAuditLogs
| where event_type == "auth.denied"
| summarize denied = count() by bin(TimeGenerated, 5m), remote
| where denied >= 10
```

**Secondary alert — rate-limiter co-occurrence with auth failures.**
A sustained attack will eventually trip the `AUTH_SENSITIVE` rate-limit
tier; watching for `rate_limit.exceeded` alongside `auth.denied` from
the same `remote` catches the patient attacker that pauses between
attempts. Lower fidelity than the threshold alert above because the
exact rate-limit floor depends on `RATE_LIMIT_SCALE` and the
`AUTH_SENSITIVE` tier value in `skiff/_config/rate.toml`.

Splunk SPL:
```spl
index=skiff event_type IN ("rate_limit.exceeded", "auth.denied") earliest=-10m
| stats countif(event_type="auth.denied")   as auth_denied,
        countif(event_type="rate_limit.exceeded") as rate_limited
  by remote
| where auth_denied > 5 AND rate_limited > 0
```

Microsoft Sentinel KQL:
```kql
SkiffAuditLogs
| where event_type in ("auth.denied", "rate_limit.exceeded")
| where TimeGenerated > ago(10m)
| summarize AuthDenied    = countif(event_type == "auth.denied"),
            RateLimited   = countif(event_type == "rate_limit.exceeded")
  by remote
| where AuthDenied > 5 and RateLimited > 0
```

---

## 7. Session Timeout Tuning

Defaults: **15-minute idle timeout** and **8-hour absolute timeout**.

Both values are env-overridable — app.js reads them from `/api/config`
at boot, so tightening the window is a restart, not a source edit. The
hardcoded values in `skiff/static/app.js` are only the fallback if the
initial config fetch hasn't completed yet.

| Knob | Units | Default | Typical tighter value |
|---|---|---|---|
| `SESSION_IDLE_SECS`   | seconds | 900   (15 min)  | 600   (10 min) |
| `SESSION_ABS_TIMEOUT` | seconds | 28800 (8 hours) | 14400 (4 hours) |

```bash
# Tighten for a regulated environment. Both take effect on next restart.
export SESSION_IDLE_SECS=600
export SESSION_ABS_TIMEOUT=14400
```

The server enforces `SESSION_ABS_TIMEOUT` independently — an HTTP or
WebSocket session older than this window is rejected even if the JS
clock somehow disagreed. The client's idle timer is enforcement for
the user's physical convenience; the server's absolute timer is the
security-relevant one.

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

1. **Detect** — pip-audit alert, anomalous audit log event (`auth.denied` spike, unexpected `image.pulled` from unknown registry).
2. **Contain** — stop the running SKIFF process (see commands below),
   rotate `API_TOKEN`, snapshot the audit log so rotation doesn't eat
   the evidence:
   ```bash
   # systemd deployment (per-instance):
   systemctl stop skiff@<instance>

   # run.sh / uvicorn-directly deployment:
   pkill -f 'uvicorn skiff.app:app'

   # snapshot audit log (quote the path — macOS default has spaces):
   cp "$AUDIT_LOG" "/tmp/skiff-audit-incident-$(date +%s).jsonl"
   ```
3. **Investigate** — `pip show <package>` for installed versions, the
   snapshot audit JSONL for request history, OS process accounting
   (`auditd`, `last`, `who`).
4. **Recover** — deploy from a clean environment with verified
   `requirements.txt` hashes; verify the generated SBOM matches the
   expected component list.
5. **Notify** — if user data could be affected, follow responsible
   disclosure practices.

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

---

## 14. Profile presets

Six named profiles bundle sensible defaults for common deployment shapes.
Set `PROFILE=<name>` in the environment — any explicit env var (including
the one the preset would have set) still wins, so presets never override
your intent.

| `PROFILE` | Intent | What it sets |
|---|---|---|
| `homelab` | Raspberry Pi / NAS, single operator | `RATE_LIMIT_SCALE=10` |
| `dev` | Developer workstation | (no overrides; current defaults) |
| `sre` | Remote Docker host via tunnel / reverse proxy | `RATE_LIMIT_SCALE=3` |
| `reviewer` | Read-only security review | `RATE_LIMIT_SCALE=1` (tight) |
| `tutor` | Classroom / workshop instance | `RATE_LIMIT_SCALE=50` (loose) |
| `ci` | Non-interactive CI runner | `RATE_LIMIT_SCALE=100` (uncapped) |

Example:

```bash
# On a homelab install
PROFILE=homelab API_TOKEN=... DOCKER_HOST=unix:///var/run/docker.sock \
  uvicorn skiff.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

Unknown profile values fail-closed at startup (`ValueError`) — the server
refuses to boot with an invalid profile rather than silently ignoring it.

---

## 15. Metrics Scraping (optional)

SKIFF exposes a Prometheus-format metrics endpoint at `GET /api/system/metrics`.
The endpoint is **authenticated** — metrics include container counts and the
configured Docker host as a label, which can reveal workload topology. Scrapers
must present a valid Bearer token.

Gauges (all `skiff_*`-prefixed): `uptime_seconds`, `containers_{total,running,paused,stopped}`, `images_total`, `engine_cpus`, `engine_memory_bytes`, `disk_{images,containers,volumes,build_cache}_bytes`.

**Prometheus:**

```yaml
# prometheus.yml
scrape_configs:
  - job_name: skiff
    metrics_path: /api/system/metrics
    scheme: http   # use https through your TLS proxy in production
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/skiff-token  # 0600, single line
    static_configs:
      - targets: ['127.0.0.1:8080']
```

**Compatible collectors:**

- Vanilla Prometheus (above)
- Grafana Agent / Alloy (same Bearer-token config shape)
- Datadog Agent with the OpenMetrics / Prometheus check
- Any managed service that supports `text/plain; version=0.0.4` exposition

Rotate the scrape token the same way as the main `API_TOKEN` (§3). The
credentials_file makes token-rotation a single-file replace with no scraper restart.
