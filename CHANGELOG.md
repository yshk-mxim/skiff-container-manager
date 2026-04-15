# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
