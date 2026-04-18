# Security Policy

## Supported Versions

Security fixes land on `main` and ship in the next tagged release.
**No backports** are promised — to receive a security fix, upgrade to
the latest tagged release.

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Use GitHub's private security advisory flow — this is the only channel
the maintainer monitors:

1. Go to **Security → Advisories → Report a vulnerability** on the
   repo, or open the form directly:
   https://github.com/yshk-mxim/skiff-container-manager/security/advisories/new
2. Include steps to reproduce and, if possible, a suggested fix.

No email channel is monitored. The maintainer will not reply to security
reports sent to any email address.

### What to expect

SKIFF is maintained in the spare time of one person. Response is
best-effort. The maintainer does not commit to a formal service-level
agreement for acknowledgement, triage, or fix windows.

- **Acknowledgment**: best-effort; no guaranteed response time. The
  maintainer reads advisories as time allows and will acknowledge once
  triage begins.
- **Assessment & fix**: prioritised by severity; critical RCE / auth-bypass
  ahead of everything else, low-severity hardening suggestions may take
  longer or be queued for a future minor.
- **Disclosure**: coordinated with the reporter via the same advisory
  thread before publication. Reporters are asked to hold disclosure for
  a reasonable period (typically 90 days) while a fix is prepared.
- **Credit**: reporters are credited in the published advisory unless
  they ask not to be.

## Scope

**In scope:**

- Authentication / authorisation bypass
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
- **Rate limiting** — every route is rate-limited. Setup and compose
  endpoints have the strictest limits (`AUTH_SENSITIVE` tier); read-only
  endpoints are at `READ`; the four unauthenticated discovery endpoints
  (`/health`, `/ready`, `/api/auth-required`, `/api/docs`) run at the
  `PUBLIC` tier. `/api/openapi.json` is FastAPI-managed and publishes the
  route catalogue without auth — the catalogue itself is not a secret.
  Rate-limit responses use the documented `{detail: {code: "auth.rate_limited", ...}}` envelope.
- **Security headers** — CSP `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'`. `unsafe-inline` is kept on `style-src` because the JS DOM builders set inline style attributes; `script-src` has no `unsafe-inline`. Also emits `X-Frame-Options: DENY`, HSTS (when behind TLS), `Referrer-Policy`, `Permissions-Policy`.
- **Audit logging** — all API requests are logged as structured JSON with `event_type`, `method`, `path`, `status`, `remote`, and auth state. Sensitive environment variable values are redacted from inspect responses.
- **Server-side session age** — tokens are tracked from first use and rejected after 8 hours regardless of client-side state. A token rotation (`POST /api/auth/rotate-token`) also force-closes any active WebSocket connections bound to the previous token, via an explicit token-value check in the WS keepalive path. The 8-hour absolute clock restarts for the newly issued token.
- **Port binding restrictions** — containers cannot bind to privileged host ports (< 1024).

## Design Trade-offs and Mitigations

SKIFF is intentionally scoped. Two limitations come up in comparisons with heavier management tools — both are by design, and both have straightforward mitigations.

### Single bearer token (no built-in RBAC)

**What it means.** Everyone who knows `API_TOKEN` has full access — there are no per-user roles or per-user audit trails.

**Why it's this way.** Adding a user database requires a persistent backing store, migrations, and account management UI. For a tool designed to run on-demand from a developer's workstation, that overhead is the wrong trade-off.

**Mitigation.** Place an OAuth2/OIDC proxy in front of SKIFF (see [docs/hardening/production.md §SSO](docs/hardening/production.md#5-sso-via-identity-proxy-optional-multi-user)). The proxy handles login with your identity provider and SKIFF's audit log gains per-user attribution via `X-Forwarded-User`. No code changes required.

### One Docker host per instance

**What it means.** SKIFF connects to exactly one Docker socket. Managing ten Docker hosts means running ten SKIFF instances.

**Why it's this way.** Multi-host state management adds substantial complexity that conflicts with the "no persistent server" design goal.

**Mitigation.** Each instance is a single Python process with no database, so running several in parallel is cheap. Use different `PORT` values or separate reverse proxy routes per host.

### Zero-trust gaps and known design limitations

These limitations are accepted design trade-offs in the current version. Each has a documented mitigation. Security researchers should be aware of them.

| Gap | Impact | Mitigation |
|---|---|---|
| Mutable registry allowlist at runtime | POST `/api/setup` can change `ALLOWED_REGISTRIES` from `docker.io` to a malicious registry after configuration | Set `ALLOWED_REGISTRIES` via environment variable; this disables `/api/setup` (`from_env` guard) |
| Single-token auth (no per-user identity) | Stolen token grants full access for the lifetime of the token | Rotate via `POST /api/auth/rotate-token` (session-only configs) or restart with a new `API_TOKEN` env var (persistent configs); rotate on any suspected leak. For multi-user installations, front SKIFF with an OAuth2/OIDC proxy (see `docs/hardening/production.md` §5) |
| All sessions share one token (no per-user invalidation) | Revoking one user's access requires rotating the token (which signs every holder out) | SSO proxy with `X-Forwarded-User` per user and a reverse-proxy–level session revoke |
| Audit log integrity (append-only file, not tamper-evident) | A compromised process can overwrite the file | Export to a remote SIEM in real time; keep local file as fast buffer only |
| Compose allowlist validated at submit, not at runtime | A valid image could be replaced by a malicious one between pull and run | Use a private registry with image signing (Notary / cosign); pin images by digest (`image@sha256:...`) |
| Resource-limit updates persist only in memory | A user with the token can raise a running container's memory/CPU up to `MAX_CONTAINER_*` and the engine will honour it until the container exits | Caps enforced server-side (cannot exceed global `MAX_CONTAINER_MEM`/`MAX_CONTAINER_CPU`); audit log records before/after per field so unexpected changes are detectable |

## OWASP ASVS v5.0 mapping

SKIFF's security posture is self-assessed against the chapters of
[OWASP ASVS v5.0](https://github.com/OWASP/ASVS/tree/v5.0.0/5.0) that
are applicable to its shape (single-process API + browser UI, no user
database, no stored secrets beyond the in-memory API token). The
mapping is a self-assessment — not a paid attestation — and reflects
the state at commit time; adopters running at a different ASVS level
should repeat the audit against their own threat model.

| ASVS v5.0 chapter | Applicability | SKIFF posture |
|---|---|---|
| V1 Encoding and Sandboxing of Untrusted Data | In scope | `yaml.safe_load` everywhere; `resolve()` + `is_relative_to()` at every filesystem boundary; compose validator sandboxes dangerous keys + host paths + network modes. |
| V2 Validation and Business Logic | In scope | Pydantic `extra="forbid"` on every mutating body; registry allowlist enforced at the API layer before any Docker call; image-tag / container-name / project-name regexes; per-container resource caps. |
| V3 Web Frontend Security | In scope | Strict CSP `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'`; `X-Content-Type-Options: nosniff`; `X-Frame-Options: DENY`; `Referrer-Policy: strict-origin-when-cross-origin`; `Permissions-Policy: camera=(), microphone=(), geolocation=(), usb=()`; no inline scripts (theme-init is a same-origin `.js` file); sessionStorage-only credential lifetime; XSS boundary centralised in `ui.js` (every DOM write uses `textContent`). |
| V4 API and Web Service | In scope | CSRF sentinel header on every mutation (distinct `csrf_missing` vs `csrf_invalid` codes); documented error envelope `{detail: {code, message, help?}}` on every 4xx/5xx; OpenAPI 3.1 served at `/api/openapi.json`. |
| V5 File Handling | In scope | Compose uploads size-capped, MIME-checked, path-resolved under `COMPOSE_DIR`; no arbitrary filesystem writes from user input. |
| V6 Authentication | In scope | Constant-time bearer-token compare; 16-char minimum (wizard-enforced); startup warning if an env-supplied token falls below the minimum; per-IP lockout on setup-POST + tunnel-start + WS auth. |
| V7 Session Management | In scope | sessionStorage only (no cookies, no localStorage except theme preference); idle + absolute timeouts env-overridable via `SESSION_IDLE_SECS` / `SESSION_ABS_TIMEOUT`; server-side cache keyed by salted HMAC; token rotation force-closes live WebSocket sessions. |
| V8 Authorisation | Partial | Single-token authorisation is the documented design trade-off (see "Design Trade-offs" above). Per-user authorisation is delegated to a fronting SSO proxy. |
| V9 Self-contained Tokens | N/A | No JWT or other self-contained token format; opaque bearer tokens only. |
| V10 OAuth and OIDC | N/A | SKIFF does not issue OAuth/OIDC tokens. Documented integration with oauth2-proxy for adopters who need SSO. |
| V11 Cryptography | Minimal | No stored secrets. `hmac.compare_digest` for token comparison; `secrets.token_bytes(32)` salt for the session-cache HMAC. |
| V12 Secure Communication | Operator-responsible | TLS termination is delegated to the front proxy. `docs/hardening/production.md §1` documents Caddy / nginx / Tailscale / Cloud IAP patterns. |
| V13 Configuration | In scope | Secret-knob `SecretStr` marking + redaction in `/api/config` output; startup warnings for weak token / unencrypted `DOCKER_HOST` / non-loopback bind / trusted-forwarded-headers misconfig. |
| V14 Data Protection | In scope | Audit log is 0600 and rotated; env-values in container-inspect responses redacted via `_ENV_SENSITIVE_RE`; WS exec input is NOT captured (byte-count only) so pasted credentials don't land in the audit log. |
| V15 Secure Coding and Architecture | In scope | CI gates on ruff (S-rules via Bandit), pip-audit, `claude-code-security-review` on PR diffs, custom AP001–AP014 anti-pattern linter, Grype + Syft (SBOM + vulnerability scan). |
| V16 Security Logging and Error Handling | In scope | Structured JSONL audit per `docs/audit-events.md` catalogue; error envelope never leaks stack traces / internal paths / versions; `uvicorn.access` filter scrubs `?token=` leaks from the access log. |
| V17 WebRTC | N/A | |
| V18 Mobile | N/A | |

Adopters who want to re-verify the mapping can run
`docs/dev/zero-trust-review-template.md` against a fresh deployment.

---

→ See [docs/hardening/production.md](docs/hardening/production.md) for the operator deployment and hardening guide.
