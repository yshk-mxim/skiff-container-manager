# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [1.1.0] — 2026-04-16

### Added

**Security hardening**
- Setup endpoint per-IP brute-force lockout: 3 failed validation attempts → 429 for 300 s, mirroring WebSocket auth lockout; `audit.setup_failed` emitted on every failure for SIEM detection
- `ipc: shareable` blocked in compose sandbox (was: only `ipc: host` blocked); containers can no longer share IPC namespaces via compose
- Setup wizard token input changed to `type="password"` with Copy-to-clipboard button — prevents token appearing in browser clipboard history and screenshots
- Tunnel credentials (`tunnelUser`, `tunnelHost`) cleared from sessionStorage immediately after successful tunnel connection
- WebSocket close code 4003 (session expired) now triggers "Session expired" toast and suppresses reconnect — previously may have retried with stale token
- `setup-state` endpoint returns minimal payload (`configured`, `from_env` only) when server is already configured — no tunnel socket path leaked to unauthenticated callers
- DOCKER_HOST HTTP guard: startup logs `security.docker_host_unencrypted` warning if `DOCKER_HOST` is a non-localhost `http://` URL
- Startup audit log entry `app.dependency_versions` records installed versions of all direct dependencies for post-incident forensics

**Code architecture**
- Monolith split: `skiff/app.py` (≈2 500 lines) split into focused modules — `skiff/{config,auth,docker_client,logging_setup,validators}.py` and `skiff/routers/{containers,images,volumes,networks,compose,system}.py`; external API and root `app.py` shim unchanged

**CI / supply chain**
- `.github/workflows/security.yml` — `anthropics/claude-code-security-review` runs on every PR
- `.github/dependabot.yml` — weekly automated dependency update PRs (pip + github-actions)
- `.github/CODEOWNERS` — `.github/`, `pyproject.toml`, `requirements.txt` require maintainer review
- `.pre-commit-config.yaml` — ruff + pip-audit hooks
- `requirements.txt` regenerated with `pip-compile --generate-hashes` (708 SHA-256 hashes covering all platform wheels; cross-platform for pip and uv)
- `make deps` target added to regenerate hash-pinned requirements
- `pip-audit` added to CI and `[dev]` extras; `make security` coverage extended to all `skiff/` modules

**Documentation**
- `docs/production-hardening.md` — new 13-section operator hardening guide (TLS, network isolation, token lifecycle, registry scoping, SSO, audit/SIEM, session timeout, SSH hygiene, dependency scanning, supply chain, incident response, least-privilege account)
- `docs/code-quality-guide.md` — new "Documented Security Controls" table (11 non-obvious decisions with rationale) and "Zero-trust design limitations" reference
- `SECURITY.md` — new "Zero-trust gaps and known design limitations" table (mutable allowlist, single token, audit log integrity, compose allowlist timing)
- `README.md` — "Zero trust and cloud workstation environments" subsection in Why SKIFF?; local-first framing; Docker Desktop comparison column; platform socket path table; zero-config dev-mode quickstart; remote deployment clearly labelled section
- `docs/api-reference.md` — WebSocket handshake protocol spec, close codes table, corrected stats field names (`mem_usage_mb`, `blk_read_mb`, etc.)
- `docs/troubleshooting.md` — three new entries: setup window expired, WS 4003 session expired, WS auth lockout

### Changed

- **Default `ALLOWED_REGISTRIES`**: `us-docker.pkg.dev/` → `docker.io,ghcr.io` — the previous default silently blocked all Docker Hub pulls for local users
- Registry allowlist comparison is now case-insensitive (`DOCKER.IO` matches `docker.io`)
- Container filesystem diff endpoint: kind values capitalised (`Added`/`Modified`/`Deleted`) — was lowercase, breaking badge CSS colour logic in the UI
- `SECURITY.md` trimmed to a concise policy document; operational hardening content moved to `docs/production-hardening.md`
- `docs/production-hardening.md` §6 SIEM/logging: Grafana Alloy replaces Promtail (EOL 2026-03-02); OpenSearch recommendation corrected to Fluent Bit (Filebeat 7.13+ incompatible with OpenSearch); concrete Splunk SPL and Sentinel KQL alert queries added including the key co-occurrence pattern (`rate_limit.exceeded` + `auth.denied` from same IP)
- CI workflow `ci.yml`: Python 3.11 matrix entry removed (project requires 3.12+); `permissions: contents: read` added; step name corrected from "bandit" to "ruff"

### Fixed

- Images page search filter silently failed when `/api/images` returned `tags` (array) — `img.tag` was `undefined`, causing a `TypeError` in the `oninput` handler that left all images unfiltered; filter and render now correctly use `img.tags`
- WebSocket input size check used character count (`len(data)`) instead of byte count (`len(data.encode())`); a 65 536-character string of 4-byte UTF-8 sequences = 256 KB actual data
- Tunnel path containment used string `startswith` which fails on macOS where `/tmp` is a symlink to `/private/tmp`; replaced with `Path.is_relative_to()` on resolved paths
- `images.py` pull/push used `lambda: client.images.pull(image)` in `run_in_executor`; replaced with direct callable `(client.images.pull, image)` — lambdas capture by reference and are unnecessarily indirect

---

## [1.0.0] — 2026-04-15

Initial public release of SKIFF Container Manager.

### Added

**Core backend**
- FastAPI backend connecting to a remote Docker Engine VM via SSH tunnel
- Container lifecycle: list, run, start, stop, restart, pause, unpause, kill, rename, remove
- Log tail, download (plain text + JSONL), and real-time WebSocket streaming
- Interactive shell via WebSocket exec
- Image management: list, allowed, pull, push, tag, remove, inspect
- Volume management: list, create, remove, prune (named volumes only)
- Network management: list, create, remove, connect, disconnect, prune
- Docker Compose support: deploy and tear down stacks with sandbox validation
- System info, disk usage, prune, and build-cache prune endpoints
- Registry search and tag proxy endpoints (`/api/registry/search`, `/api/registry/tags`)
- Config endpoint (`/api/config`) returning allowed registries and Docker VM host to UI
- Audit log query and download endpoints (`/api/system/audit-log`, `/api/system/audit-log/download`)

**Security**
- Bearer token authentication with constant-time comparison (`hmac.compare_digest`)
- CSRF protection via `X-Requested-With: ContainerManager` header
- Registry allowlist enforcement — every pull/push/run validated against `ALLOWED_REGISTRIES`
- Compose file sandbox: blocks `privileged`, host path mounts, `cap_add`, `devices`, `build`, `secrets`, unapproved registries
- Volume sandbox: named volumes only, host paths rejected
- WebSocket auth via first message (avoids query-param token leakage)
- Rate limiting via slowapi with `RATE_LIMIT_SCALE` multiplier for CI/testing
- Security headers middleware: CSP, X-Frame-Options, HSTS, Referrer-Policy, Permissions-Policy
- Audit logging middleware (SOC 2 CC7.1) with structured JSONL output
- `ALLOWED_ORIGINS` wildcard guard: startup raises `ValueError` if `*` is configured
- SPDX license headers on all source files

**Packaging**
- `skiff` PyPI package — `pip install skiff` + `skiff` CLI entry point
- Proper package structure (`skiff/app.py`, `skiff/static/`) so static assets are bundled in the wheel
- `[dev]` optional dep group: pytest, httpx, hypothesis (no browser required)
- `[e2e]` optional dep group: playwright, pytest-playwright (requires `playwright install chromium`)
- `setuptools.build_meta` build backend

**UI**
- Single-page web UI (`static/index.html`) — no build step, vanilla JS
- Keyboard shortcuts, container search/filter, Hub image search with tag picker
- Real-time log streaming and interactive terminal in the browser

**Ops**
- `run.sh` startup script for Cloud Workstation / Linux
- systemd unit file (`docs/skiff.service`)
- `RATE_LIMIT_SCALE` env var for CI and load-test environments

**Documentation**
- `README.md` with quick start, platform setup, configuration reference, and API overview
- `docs/api-reference.md` — full REST and WebSocket endpoint reference
- `docs/deployment.md` — GCP Cloud Workstation deployment guide
- `docs/local-docker.md` — local Docker Engine setup
- `docs/gcp-testing.md` — day-to-day GCP operations guide
- `docs/troubleshooting.md` — common issues and fixes
- `docs/code-quality-guide.md` — code standards and PR checklist
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`

**Tests**
- 142 unit tests (no Docker daemon required)
- 26 Playwright e2e tests covering 100% of UI flows
- Hypothesis property-based tests for input validation

### Security fixes at release
- `python-multipart` `>=0.0.22` — CVE-2026-24486
- `fastapi` `>=0.116` + `starlette` `>=0.49.1` — CVE-2025-54121, CVE-2025-62727
