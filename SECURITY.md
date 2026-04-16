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

## Design Trade-offs and Mitigations

SKIFF is intentionally scoped. Two limitations come up in comparisons with heavier management tools — both are by design, and both have straightforward mitigations.

### Single bearer token (no built-in RBAC)

**What it means.** Everyone who knows `API_TOKEN` has full access — there are no per-user roles or per-user audit trails.

**Why it's this way.** Adding a user database requires a persistent backing store, migrations, and account management UI. For a tool designed to run on-demand from a developer's workstation, that overhead is the wrong trade-off.

**Mitigation.** Place an OAuth2/OIDC proxy in front of SKIFF (see [docs/production-hardening.md §SSO](docs/production-hardening.md#5-sso-via-identity-proxy-optional-multi-user)). The proxy handles login with your identity provider and SKIFF's audit log gains per-user attribution via `X-Forwarded-User`. No code changes required.

### One Docker host per instance

**What it means.** SKIFF connects to exactly one Docker socket. Managing ten Docker hosts means running ten SKIFF instances.

**Why it's this way.** Multi-host state management adds substantial complexity that conflicts with the "no persistent server" design goal.

**Mitigation.** Each instance is a single Python process with no database, so running several in parallel is cheap. Use different `PORT` values or separate reverse proxy routes per host.

---

→ See [docs/production-hardening.md](docs/production-hardening.md) for the operator deployment and hardening guide.
