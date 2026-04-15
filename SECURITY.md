# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

1. Open a private security advisory on GitHub (Security → Advisories → New draft).
2. Include steps to reproduce and, if possible, a suggested fix.

### What to expect

- **Acknowledgment**: Within 48 hours.
- **Assessment**: Within 5 business days.
- **Resolution**: Critical issues patched as soon as possible.
- **Disclosure**: Coordinated with the reporter.

## Scope

**In scope:**

- Authentication/authorization bypass
- Registry allowlist bypass (running images from unapproved registries)
- Compose sandbox escape (host path mount, privileged mode bypass)
- Volume sandbox escape (host path mount bypass)
- WebSocket token leakage
- Remote code execution via API

**Out of scope:**

- Denial of service via legitimate API usage
- Issues requiring physical access to the host
- Vulnerabilities in dependencies without a known exploit

## Security Architecture

SKIFF Container Manager has several defense layers:

- **Bearer token auth** with constant-time comparison (HMAC).
- **CSRF protection** via `X-Requested-With: ContainerManager` on all mutations.
- **Registry allowlist** — images outside `ALLOWED_REGISTRIES` are rejected at the API level.
- **Compose validation** — dangerous keys (`privileged`, `cap_add`, `devices`, host mounts, `secrets`, `configs`) are rejected before `docker compose up`.
- **Volume sandboxing** — host path mounts are rejected; only named volumes are allowed.
- **WebSocket token via message** — token sent as first message, not query parameter, to avoid leaking to proxy logs.
- **Rate limiting** — all endpoints are rate-limited.
- **Security headers** — CSP, X-Frame-Options, HSTS (behind TLS), Permissions-Policy.
- **Audit logging** — all authenticated API requests are logged with method, path, status, and remote IP (SOC 2 CC7.1).

## Security Best Practices for Operators

- Always set `API_TOKEN` in production.
- Scope `ALLOWED_REGISTRIES` to your own project prefix.
- Do not expose port 8080 directly to the internet — run behind the Cloud Workstation proxy or a TLS-terminating reverse proxy.
- Restrict the SSH key used for `DOCKER_HOST` to the Docker VM only.

## Production Hardening Guide

The defaults are tuned for local development. For production or security-sensitive deployments, apply the following:

### 1. TLS termination via reverse proxy

The app serves plain HTTP. Place it behind a TLS-terminating reverse proxy:

```bash
# Caddy (automatic HTTPS)
caddy reverse-proxy --from https://containers.example.com --to localhost:8080

# nginx — configure ssl_certificate + proxy_pass to 127.0.0.1:8080
```

### 2. Bind to localhost

Prevent direct network access by binding to loopback only. All traffic should enter through the reverse proxy.

```bash
uvicorn app:app --host 127.0.0.1 --port 8080
```

### 3. Rotate API_TOKEN

Treat `API_TOKEN` like any credential. Rotate on a regular cadence (e.g., 90 days) and immediately on personnel changes.

### 4. Restrict ALLOWED_REGISTRIES

In development, an empty allowlist permits all registries. In production, always set an explicit allowlist:

```bash
# GCP Artifact Registry only
ALLOWED_REGISTRIES=us-docker.pkg.dev,europe-docker.pkg.dev

# Self-hosted registry
ALLOWED_REGISTRIES=registry.internal.example.com
```

### 5. Network isolation

The app has no built-in network ACLs. Restrict access at the network layer:

- Firewall rules or security groups limiting source IPs
- VPN or private network (Tailscale, WireGuard, cloud VPC)
- Cloud IAP or bastion host

### 6. SSO via identity proxy (optional, multi-user)

For teams that need per-user identity without modifying the app, place an OAuth2 proxy in front:

```bash
# oauth2-proxy with GitHub provider
oauth2-proxy --provider=github \
  --upstream=http://127.0.0.1:8080 \
  --cookie-secret=... --client-id=... --client-secret=...
```

Works with any OIDC provider (Google, GitHub, Azure AD, Okta, Keycloak). No code changes required — the proxy handles login and passes identity headers to the app.

### 7. Audit log export and retention

The app writes structured JSON logs to stdout and a rotating JSONL file. Each entry includes `event_type` (semantic label for SIEM rules), `method`, `path`, `status`, `remote`, `auth`, and optionally `user` (from `X-Forwarded-User`) and `resource_type`/`resource_id`.

**Configure retention** for compliance (SOC 2 Type II requires ≥ 90 days; 13 months recommended):

```bash
# 1-year retention at ~4 MB/day: 200 MB × 20 = 4 GB total
AUDIT_MAX_MB=200
AUDIT_BACKUP_COUNT=20
AUDIT_LOG=/var/log/skiff-audit.jsonl
```

**Ship to a log aggregator:**

- **Loki + Grafana** (open source)
- **ELK / OpenSearch**
- **Cloud-native**: Cloud Logging (GCP), CloudWatch (AWS), Azure Monitor

**GCP Cloud Logging native sink** — install the optional dep and set the project:

```bash
pip install skiff[gcp]
export GOOGLE_CLOUD_PROJECT=my-project-id
export GCP_LOG_NAME=skiff-audit          # optional, default: skiff-audit
```

SKIFF will dual-write every log entry to Cloud Logging alongside the local file and stdout.

**Per-user identity** — when running behind oauth2-proxy, pass the authenticated user email via `X-Forwarded-User`. SKIFF logs this as the `user` field automatically — no configuration required.

**SIEM alert rules** — the `event_type` field enables per-event alerting. Recommended alert thresholds:

| Event type | Threshold | Suggested severity |
|---|---|---|
| `auth.denied` | > 5 in 10 min from same IP | P2 |
| `compose.deployed` | Any, outside business hours | P3 |
| `container.run` | Any with `resource_id` containing "priv" | P2 |
| `image.pull` | Outside allowed registry (blocked at API, still logged) | P2 |
| `audit.log_read` | Any | P3 |

### 8. Dependency scanning

Run `pip-audit` on a regular schedule and before every deployment:

```bash
pip-audit -r requirements.txt
```

Enable GitHub Dependabot alerts on the repository for continuous monitoring.

### 9. File permissions

```bash
chmod 600 .env                    # API_TOKEN and secrets
chmod 600 ~/.ssh/docker_engine    # SSH key for DOCKER_HOST
```

Ensure the SSH key used for Docker engine access is scoped to that host only (use a dedicated key pair, not a personal key).

### 10. Session timeout tuning

Defaults are 15-minute idle timeout and 8-hour absolute timeout. For high-security environments, tighten these by editing the constants in `static/index.html`:

```javascript
var IDLE_TIMEOUT   = 10 * 60 * 1000;  // 10 minutes
var ABS_TIMEOUT    = 4 * 60 * 60 * 1000;  // 4 hours
```

---

## Design Trade-offs and Mitigations

SKIFF is intentionally scoped. Two limitations come up in comparisons with heavier management tools — both are by design, and both have straightforward mitigations.

### Single bearer token (no built-in RBAC)

**What it means.** Everyone who knows `API_TOKEN` has full access — there are no per-user roles or audit trails that identify individual users.

**Why it's this way.** Adding a user database would require a persistent backing store, migrations, and account management UI. For a tool designed to run on-demand from a developer's workstation, that overhead is the wrong trade-off.

**Mitigation.** Place an OAuth2/OIDC proxy in front of SKIFF (see §6 above). The proxy handles login with your identity provider (Google, GitHub, Okta, Azure AD) and enforces per-user access. SKIFF's audit log then receives requests only after the proxy has authenticated the user, and you can add the `X-Forwarded-User` header to your log pipeline for per-user attribution. No code changes to SKIFF required.

```bash
# Example: oauth2-proxy with Google provider, in front of SKIFF on :8080
oauth2-proxy \
  --provider=google \
  --upstream=http://127.0.0.1:8080 \
  --email-domain=yourcompany.com \
  --cookie-secret=<random-32-bytes> \
  --client-id=<oauth-client-id> \
  --client-secret=<oauth-client-secret>
```

### One Docker host per instance

**What it means.** SKIFF connects to exactly one Docker socket. Managing ten Docker hosts means running ten SKIFF instances (one per workstation, or behind separate ports).

**Why it's this way.** Multi-host state management (cross-host routing, host health tracking, aggregate views) adds substantial complexity that conflicts with the "no persistent server" design goal.

**Mitigation.** The lightweight footprint makes this practical: each instance is a single Python process with no database, so running several in parallel is cheap. Use different `PORT` values or separate reverse proxy routes per host. If you genuinely need a unified view across many hosts, a server-based manager with native multi-host support is the better fit for that use case.
