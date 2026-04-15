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

SKIFF has several defence layers:

- **Bearer token auth** — constant-time HMAC comparison; minimum 16-character token enforced by the setup wizard.
- **CSRF protection** — `X-Requested-With: ContainerManager` required on all mutating endpoints.
- **Registry allowlist** — images outside `ALLOWED_REGISTRIES` are rejected at the API layer before any Docker call is made.
- **Compose sandbox** — dangerous keys (`privileged`, `cap_add`, `devices`, host mounts, `network_mode: host/container/service`, `secrets`, `configs`) are blocked before `docker compose up`.
- **Volume sandboxing** — host path mounts are rejected; only named volumes are allowed.
- **WebSocket token via message** — token sent as first WebSocket message, not as a query parameter, to avoid leaking into proxy logs.
- **Rate limiting** — all endpoints are rate-limited; setup and compose endpoints have stricter limits.
- **Security headers** — CSP (`script-src 'self'`, no `unsafe-inline`), `X-Frame-Options: DENY`, HSTS (when behind TLS), `Referrer-Policy`, `Permissions-Policy`.
- **Audit logging** — all API requests are logged as structured JSON with `event_type`, `method`, `path`, `status`, `remote`, and auth state. Sensitive environment variable values are redacted from inspect responses.
- **Server-side session age** — tokens are tracked from first use and rejected after 8 hours regardless of client-side state.
- **Port binding restrictions** — containers cannot bind to privileged host ports (< 1024).

## Security Best Practices for Operators

- Always set `API_TOKEN` in production. The app warns at startup if it is unset or empty.
- Scope `ALLOWED_REGISTRIES` to your own project prefix. An empty allowlist permits all registries and triggers a startup warning.
- Do not expose port 8080 directly to the internet — run behind a TLS-terminating reverse proxy or Cloud Workstation proxy.
- Restrict the SSH key used for tunnelling to the target Docker VM only (use a dedicated key pair).

## Production Hardening Guide

The defaults are tuned for local development. For production deployments, apply the following:

### 1. TLS termination via reverse proxy

The app serves plain HTTP. Place it behind a TLS-terminating reverse proxy:

```bash
# Caddy (automatic HTTPS)
caddy reverse-proxy --from https://containers.example.com --to localhost:8080

# nginx — configure ssl_certificate + proxy_pass to 127.0.0.1:8080
```

### 2. Bind to localhost

Prevent direct network access by binding to loopback only.

```bash
uvicorn app:app --host 127.0.0.1 --port 8080
```

### 3. Rotate API_TOKEN

Treat `API_TOKEN` like any credential. Rotate on a regular cadence and immediately on personnel changes. Restarting the server with a new token invalidates all active sessions.

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

Works with any OIDC provider (Google, GitHub, Azure AD, Okta, Keycloak). No code changes required. The proxy handles login and passes the authenticated user's identity via `X-Forwarded-User` — SKIFF logs this automatically as the `user` field in every audit entry.

### 7. Audit log retention and export

The app writes structured JSON to stdout and a rotating JSONL file. Each entry includes `event_type`, `method`, `path`, `status`, `remote`, `auth`, and optionally `user` (from `X-Forwarded-User`) and `resource_type`/`resource_id`.

**Configure retention:**

```bash
# ~1-year retention at ~4 MB/day
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

### 8. Dependency scanning

Run `pip-audit` on a regular schedule and before every deployment:

```bash
pip-audit -r requirements.txt
```

Dependabot alerts are enabled on the repository.

### 9. File permissions

```bash
chmod 600 .env                    # API_TOKEN and secrets
chmod 600 ~/.ssh/docker_engine    # SSH key for tunnel
```

Use a dedicated SSH key pair for the Docker tunnel — not a personal key.

### 10. Session timeout tuning

Defaults: 15-minute idle timeout and 8-hour absolute timeout, enforced both client-side (JS) and server-side. To tighten for high-security environments, edit the constants in `skiff/static/app.js`:

```javascript
var SESSION_IDLE_MS     = 10 * 60 * 1000;  // 10 minutes
var SESSION_ABSOLUTE_MS =  4 * 60 * 60 * 1000;  // 4 hours
```

Also update `SESSION_ABS_TIMEOUT` in `skiff/app.py` to match.

---

## Design Trade-offs and Mitigations

SKIFF is intentionally scoped. Two limitations come up in comparisons with heavier management tools — both are by design, and both have straightforward mitigations.

### Single bearer token (no built-in RBAC)

**What it means.** Everyone who knows `API_TOKEN` has full access — there are no per-user roles or per-user audit trails.

**Why it's this way.** Adding a user database requires a persistent backing store, migrations, and account management UI. For a tool designed to run on-demand from a developer's workstation, that overhead is the wrong trade-off.

**Mitigation.** Place an OAuth2/OIDC proxy in front of SKIFF (see §6 above). The proxy handles login with your identity provider and SKIFF's audit log gains per-user attribution via `X-Forwarded-User`. No code changes required.

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

**What it means.** SKIFF connects to exactly one Docker socket. Managing ten Docker hosts means running ten SKIFF instances.

**Why it's this way.** Multi-host state management adds substantial complexity that conflicts with the "no persistent server" design goal.

**Mitigation.** Each instance is a single Python process with no database, so running several in parallel is cheap. Use different `PORT` values or separate reverse proxy routes per host.
