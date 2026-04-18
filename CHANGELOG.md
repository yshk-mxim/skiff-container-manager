# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_No changes queued._

## [1.0.0] — 2026-04-17

First public release of SKIFF — a lightweight web UI and JSON API for
Docker Engine, usable locally or over an SSH tunnel.

### Features
- **Container lifecycle** — list, inspect, run, start/stop/restart/remove,
  logs (live WebSocket), exec (interactive shell over WebSocket), stats.
- **Image management** — list, pull (with registry allowlist), inspect,
  history, remove; Docker Hub repo + tag validation.
- **Volume & network management** — list, create (validated name shapes),
  inspect, remove, prune.
- **Compose** — upload, validate (sandboxed YAML load + per-service checks),
  `up` / `down`, per-project directory derivation.
- **First-run setup wizard** — token generation, SSH tunnel connect, local
  socket probe (Docker Desktop, Colima, OrbStack, Rancher Desktop, Linux
  Engine), per-IP lockout.
- **Audit log** — structured JSONL, rotating file, redacted env values,
  classification per endpoint (`container.run`, `image.pulled`, etc.),
  download endpoint.
- **Undo queue** — tokenised reversal of destructive ops within a short
  TTL window.
- **System page** — info, df, metrics, connect-snippets (copy-paste
  integration hints for Prometheus, Loki, Grafana, Splunk, …).

### Security
- **Single-bearer-token auth** with optional oauth2-proxy front-ending for SSO.
- **CSRF header enforced** on all mutations (`X-Requested-With`).
- **Rate limiting** on every `/api/*` via SlowAPI.
- **Zero-trust browser model** — `sessionStorage` only; idle + absolute
  session timeouts; tab-close clears state.
- **WebSocket hardening** — Origin allowlist, per-IP brute-force lockout
  on AUTH handshake, 4003 close code on session expiry.
- **Path-injection defences** — `resolve() + is_relative_to()` on every
  filesystem boundary; CodeQL-clean.
- **Registry allowlist** — default `docker.io,ghcr.io`; compose and run
  paths both enforce it.
- **Constant-time token compare** via `hmac.compare_digest`.
- **Setup window** auto-closes 5 min after startup.
- **Startup warnings** for weak token, non-localhost bind without TLS.

### Supply chain
- Hash-pinned `requirements.txt` via `pip-compile --generate-hashes`.
- SBOM (CycloneDX) generated per release.
- Dependabot configured for pip + GitHub Actions.
- GitHub Actions SHA-pinned; `GITHUB_TOKEN` restricted to `contents: read`.
- `CODEOWNERS` gate on `.github/`, `pyproject.toml`, `requirements.txt`.
- `pre-commit` hook runs `ruff` + `pip-audit` before every commit.

### CI & code quality
- Custom AST linter (`tools/lint_antipatterns.py`) enforces 14 project
  anti-patterns (AP001–AP014): nested `try`, bulky imports, literal policy
  kwargs, hardcoded paths, inline identifier regex outside
  `skiff/validators.py`, archaeological comment markers, hardcoded
  policy literals in comments, etc.
- Ruff with `S` (bandit), `DTZ`, `FURB`, `PTH`, `PYI` rule families.
- `pip-audit --strict` on every PR.
- Markdown cross-link checker (`tools/check_md_links.py`) — no broken
  internal links or anchors.
- Auto-generated reference catalogues (errors, audit events, config knobs,
  per-router feature docs) with CI drift check.
- JS linter enforcing no `innerHTML` interpolation outside `ui.js`.
- `claude-code-security-review` GitHub Action scans each PR diff.
- SARIF upload step in `security.yml` integrates with GitHub's CodeQL
  default-setup (enable via repo Settings → Code security → Code scanning).

### Documentation
- Adopter docs under `docs/hardening/` (production, integrations,
  security-scans), `docs/runbooks/` (step-by-step recovery).
- Maintainer docs under `docs/dev/` (feature-development, code-quality-guide,
  storyboards, personas, zero-trust-review template).
- Reference: `docs/api-reference.md`, auto-generated `errors.md`,
  `audit-events.md`, `config-knobs.md`, `features/*.generated.md`.
- `SECURITY.md` scoped to policy; operator guide lives in
  `docs/hardening/production.md`.

### Known gaps
- No Dockerfile builds, no Kubernetes, no plugin system — all deliberate,
  documented in `SECURITY.md` scope statement.
- **Partial i18n infrastructure.** UI strings are English-only today,
  but the pre-i18n shape is in place: a central `strings.en.js` bundle,
  a `t(key, vars?)` lookup helper in `ui.js`, the volumes page fully
  migrated as the reference exemplar, and a contract test that missing
  keys fail CI. Other pages still have inline English literals — a
  future contributor migrates them incrementally. Runtime language
  switching, pluralisation, and RTL support land with the first
  concrete language request (see `docs/dev/i18n.md` for the full
  posture).

[Unreleased]: https://github.com/yshk-mxim/skiff-container-manager/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/yshk-mxim/skiff-container-manager/releases/tag/v1.0.0
