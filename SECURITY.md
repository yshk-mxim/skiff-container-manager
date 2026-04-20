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
- **Compose sandbox** — dangerous keys (`privileged`, `cap_add`, `devices`, host mounts, `network_mode: host/container/service`, `ipc: host/shareable`, `pid: host`, `secrets`, `configs`, `include`, `extends`, `build`, `userns_mode`, `sysctls`, `security_opt`, `shm_size`, `volumes_from`, `env_file`, `cgroup_parent`, `dns`, `dns_search`, `extra_hosts`, `tmpfs`, `uts`, `cgroupns_mode`, `storage_opt`, `device_cgroup_rules`) are blocked before `docker compose up`. Non-security keys that SKIFF permits include `image`, `environment`, `ports`, `volumes` (named only), `depends_on`, `restart`, `labels`, `command`, `entrypoint`, `healthcheck`, `deploy.resources.limits`, and `platform` (QEMU emulation is not a privilege escalation, it only selects the image architecture). The full authoritative lists live in `skiff/_config/compose_sandbox.toml`.
- **Volume sandboxing** — host path mounts are rejected; only named volumes are allowed.
- **WebSocket token via message** — token sent as first WebSocket message, not as a query parameter, to avoid leaking into proxy logs.
- **Rate limiting** — every route that touches Docker or mutates state
  is rate-limited. Setup and compose endpoints have the strictest limits
  (`AUTH_SENSITIVE` tier); read-only endpoints are at `READ`; `/ready`,
  `/api/auth-required`, and `/api/docs` run at the `PUBLIC` tier.
  `/health` is deliberately UN-rate-limited — it returns an in-memory
  `{status, uptime, version}` dict without touching Docker, so an
  orchestrator hitting it at 10 Hz is a routine load, not a DoS surface.
  `/api/openapi.json` is FastAPI-managed and publishes the route
  catalogue without auth — the catalogue itself is not a secret.
  Rate-limit responses use the documented `{detail: {code: "auth.rate_limited", ...}}` envelope.
- **Security headers** — CSP `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'`. `unsafe-inline` is kept on `style-src` because the JS DOM builders set inline style attributes; `script-src` has no `unsafe-inline`. Also emits `X-Frame-Options: DENY`, HSTS (when behind TLS), `Referrer-Policy`, `Permissions-Policy`.
- **Audit logging** — all API requests are logged as structured JSON with `event_type`, `method`, `path`, `status`, `remote`, and auth state. Sensitive environment variable values are redacted from inspect responses.
- **Server-side session age** — tokens are tracked from first use and rejected after 8 hours regardless of client-side state. A token rotation (`POST /api/auth/rotate-token`) also force-closes any active WebSocket connections bound to the previous token, via an explicit token-value check in the WS keepalive path. The 8-hour absolute clock restarts for the newly issued token.
- **Port binding restrictions** — containers cannot bind to privileged host ports (< 1024).
- **Minimal runtime install surface** — the default `pip install skiff-container-manager` (or `pip install -r requirements.txt`) pulls only the 10 declared runtime dependencies (`fastapi`, `starlette`, `uvicorn[standard]`, `docker`, `pyyaml`, `slowapi`, `structlog`, `requests`, `python-multipart`, `websockets`) plus their transitive closure — 31 packages total. Dev tooling (`pytest`, `hypothesis`, `ruff`, `pip-audit`, `pip-tools`, `httpx`), e2e tooling (`playwright`, `pytest-playwright`), and optional integrations (`google-cloud-logging`) live under `[project.optional-dependencies]` extras (`[dev]`, `[e2e]`, `[gcp]`) and are NEVER installed on a production-intended system unless the operator opts in. `requirements.txt` is generated with `pip-compile --strip-extras` so the hash-pinned production manifest cannot accidentally pull dev packages. Supply-chain attack surface on a production install is strictly the runtime 10.

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

### First-run setup window (wizard race)

**What it means.** When SKIFF boots without an `API_TOKEN` in the environment, the first-run wizard is reachable on `BIND_HOST:PORT` for `SETUP_WINDOW_SECS` (default 300s). Anyone who can reach that socket during the window can `POST /api/setup` with a token of their choice and claim the instance. On a single-user workstation with the default `BIND_HOST=127.0.0.1`, that audience is the local user. On a multi-user host or an accidentally exposed bind, the audience is every reachable caller — first caller wins the race.

**Why it's this way.** Zero-touch first-run requires a callable setup endpoint. Requiring a token to set the token is a chicken-and-egg problem. The bounded 5-minute window plus per-IP lockout is the compromise: enough time for a real first-run, narrow enough to bound the race surface.

**Mitigation.**
- **The clean fix:** set `API_TOKEN` in the environment before starting. The server sees `from_env=true`, the wizard is dead from boot-0, no race exists.
- Defaults-in-depth: `BIND_HOST=127.0.0.1` keeps the wizard unreachable from the network. `SETUP_MAX_ATTEMPTS=3` + `SETUP_LOCKOUT_SECS=300` throttles enumeration.
- A **`security.setup_window_open`** startup warning is emitted on every boot that opens the wizard — naming the bind, port, duration, and lockout policy — so an operator who didn't intend to open the wizard sees it immediately in the startup log.
- See [docs/hardening/production.md §setup-window](docs/hardening/production.md#setup-window) for the pre-configured `API_TOKEN` deployment pattern.

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

## Compliance framework coverage

SKIFF maps code-level posture to the frameworks a CIO typically
evaluates in a supplier-risk review. Full per-framework detail lives
at [`docs/compliance/`](docs/compliance/README.md). Summary:

| Framework | Status | Doc |
|---|---|---|
| OWASP ASVS v5.0 | EVIDENCE on 13/18 chapters | below + `docs/compliance/README.md` |
| OWASP Top 10 | covered via semgrep `p/owasp-top-ten` on every PR | `docs/hardening/security-scans.md` |
| WCAG 2.1 Level AA | 0 issues on login flow (pa11y in CI) + 0 issues across authenticated SPA (Playwright + axe-core 4.10 local e2e) | `docs/compliance/wcag-2-1-aa.md` |
| CIS Docker Benchmark | SKIFF sandbox enforces relevant §5 items | `docs/compliance/cis-docker-benchmark.md` |
| NIST SSDF (SP 800-218) | full PO/PS/PW/RV mapping | `docs/compliance/nist-ssdf.md` |
| NIST CSF 2.0 | primitives for GV/ID/PR/DE/RS/RC | `docs/compliance/nist-csf.md` |
| OpenSSF Scorecard | automated weekly scan | `docs/compliance/openssf-scorecard.md` |
| OpenSSF Best Practices | Passing-level attestation ready | `docs/compliance/openssf-best-practices.md` |
| SLSA v1.0 | L1 met; L2 tracked | `docs/compliance/slsa.md` |
| GDPR / CCPA / HIPAA / PCI DSS | no PII processed by design | `docs/compliance/privacy.md` |
| SOC 2 / ISO 27001 / FedRAMP | not claimed (org-level certifications, not code) | `docs/compliance/README.md#tier-3--explicitly-not-claimed` |

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
| V13 Configuration | In scope | Secret-knob `secret=True` flag on `config_knob` declarations + redaction in `/api/config` output; startup warnings (`_STARTUP_WARNINGS` in `skiff/app.py`) for weak token / unencrypted `DOCKER_HOST` / non-loopback bind (`security.bind_non_loopback`) / trusted-forwarded-headers misconfig. |
| V14 Data Protection | In scope | Audit log is 0600 and rotated; env-values in container-inspect responses redacted via `_ENV_SENSITIVE_RE`; WS exec input is NOT captured (byte-count only) so pasted credentials don't land in the audit log. |
| V15 Secure Coding and Architecture | In scope | CI gates on ruff (S-rules via Bandit), pip-audit, `claude-code-security-review` on PR diffs, custom AP001–AP014 anti-pattern linter, Grype + Syft (SBOM + vulnerability scan). |
| V16 Security Logging and Error Handling | In scope | Structured JSONL audit per `docs/audit-events.md` catalogue; error envelope is designed to avoid leaking stack traces, filesystem paths, and dependency versions (caller-facing `detail` is `{code, message, help?}` only — no Python-side internals); `uvicorn.access` filter scrubs `?token=` leaks from the access log. |
| V17 WebRTC | N/A | |
| V18 Mobile | N/A | |

Adopters who want to re-verify the mapping can run
`docs/dev/zero-trust-review-template.md` against a fresh deployment.

---

→ See [docs/hardening/production.md](docs/hardening/production.md) for the operator deployment and hardening guide.
