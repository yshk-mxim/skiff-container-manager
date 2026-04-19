# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed — driver-seat persona audit (v1.0.2 pre-release)

User-reported UX + backend bugs surfaced during iterative testing. Each
`hb-*` ID is tracked in `tests/journeys/_history.py`; fix commits linked
below so `git show <SHA>` or the tracker at
`docs/dev/persona_audit_tracker.md` produces the full change trail.

- `hb-undo-on-delete` (99647c8) — container delete bypassed the undo
  queue via hard-coded `?force=true` in the UI.
- `hb-tag-search-3.12-slim` (c1e5e47) — Docker Hub tag list capped at
  the 100 most-recent; stable tags like `python:3.12-slim` fell
  outside the window. Added server-side `name=` filter passthrough.
- `hb-volumes-no-search` / `hb-networks-no-search` /
  `hb-compose-no-search` (99647c8) — three list pages had no search
  bar. Added consistent `.search-bar` pattern.
- `hb-audit-silently-truncated` (99647c8) — audit log hard-coded
  `tail=200` with no UI cue. Added tail selector + filter input.
- `hb-logs-connecting-forever` (cb07a77) — log viewer "Connecting..."
  text never cleared on WS open for silent containers.
- `hb-terminal-dies-on-tab-switch` (cb07a77) — terminal + WS torn
  down on detail-tab switch. Added per-container session cache.
- `hb-files-tab-misleading` (cb07a77) — "No filesystem changes
  detected" misread as a failure. Added explanation paragraph.
- `hb-volume-create-skinny-form` (38f9ce4) — volume create modal only
  accepted `name`. Added driver / labels / driver_opts.
- `hb-network-create-skinny-form` (38f9ce4) — network create modal
  only accepted `name + driver`. Added subnet / gateway / labels /
  internal / attachable / ipv6.
- `hb-image-prune-missing` (99647c8) — no dedicated Images Prune
  button. Added `POST /api/images/prune`.
- `hb-compose-no-pull-or-scale` (0683c08) — compose lacked pull /
  scale / start / stop / download. Added the full verb set.
- `hb-dashboard-missing` (a24b724) — no landing page.
  `/api/system/overview` + `pages/dashboard.js`.
- `hb-events-missing` (a24b724) — no Docker events stream viewer.
  `/api/system/events` + live panel on System page.
- `hb-no-bulk-actions` (cb07a77) — no multi-select on containers.
  Per-row checkboxes + floating action bar.
- `hb-no-context-menu` (cb07a77) — no right-click menu on rows.
- `hb-no-notifications-history` (462b6f7) — toasts ephemeral. Bell
  icon with 50-entry history panel.
- `hb-no-first-run-tour` (462b6f7) — new users dropped onto raw
  containers page. 4-step tour fires once after wizard.
- `hb-cp-ui-missing` (cb07a77) — Files tab only showed `docker diff`.
  Added Browse sub-view with `/ls` + `/upload` + download.
- `hb-commit-missing` (cb07a77) — no way to save running container
  as image. `POST /api/containers/{id}/commit` + ctx-menu entry.
- `hb-templates-missing` (a24b724) — no one-click deploy catalogue.
  `GET /api/templates` + `pages/templates.js`.
- `hb-files-tab-path-memory` (cb07a77) — Files tab path forgotten
  across tab switches.

### Added — driver-seat persona audit harness

- `tests/personas.py` — 7-persona catalogue (novice / developer /
  sre_ops / security_reviewer / ui_ux_auditor / super_user / hobbyist).
- `tests/audit_driver.py` — per-step screenshot / DOM / console /
  network / stderr / audit-log capture + `Finding` emitter.
- `tests/journeys/` — journey catalogue harness with `@journey(…)`
  decorator; parametrises over persona tags.
- `tests/journeys/_history.py` — 23-row historical bug registry.
- `tests/test_zero_trust_invariants.py` — 9 security invariants
  enforced against the live TestClient.
- `tests/test_docs_complete.py` — 8 documentation-completeness gates.
- `tests/test_history_coverage.py` — enforces every `hb-*` has a
  regression test + fix commit + CHANGELOG entry.
- `tests/test_crud_completeness.py` — per-resource CRUD matrix.
- `tests/test_ui_bug_class_regressions.py` — dead-external-link
  allowlist, interval-leak audit, countdown-label context check.
- `tests/test_ui_list_affordances.py` — every list page has a search
  bar; audit log discloses its cap; delete emits undo toast.
- `tests/test_container_cp.py` — 33 tests: ls parser (unit+fuzz),
  path-validator (fuzz), /ls + /upload + commit (integration).
- `tests/test_e2e_file_browser.py` — 3 Playwright journeys: list /
  navigate / upload / download round-trip; over-size rejection;
  path memory.
- `tests/test_new_endpoint_coverage.py` — coverage for every new
  endpoint (overview / events / templates / images-prune / compose
  verbs / commit).
- `tests/test_backend_bug_class_fuzz.py` — Docker SDK exception
  funnel + unicode/CRLF round-trip + NaN/Infinity numeric boundary +
  auth-envelope contract.
- `tests/test_container_journey_fuzz.py` — hypothesis state-machine
  over container lifecycle against a FakeDaemon.
- `tests/test_docker_null_tolerance.py` — hypothesis null-injection
  across every Docker-backed numeric response.
- `Makefile` — `make persona-audit`, `make persona-audit-report`,
  `make tracker` targets.
- `docs/dev/test-architecture.md` — per-layer test mapping.
- `docs/dev/persona_audit_tracker.md` — living dashboard (findings
  / parity / GUI elements / testing / open-work).

### Security
- Reviewer persona gate: mutations 403 server-side, exec WS forcibly
  closed on entering reviewer mode (TOCTOU closed via `_ws_lock`
  re-check), UI hides destructive buttons + surfaces a sticky banner.
  `allow_in_reviewer=True` carve-out for `/api/undo/{token}` and
  `/api/auth/reset-config` so a reviewer still has an escape path.
- `reset-config` restores `config.PROFILE` to the boot-time value so
  a reviewer who escapes via the soft-restart isn't trapped.
- Startup warning `security.bind_non_loopback` when `BIND_HOST` is
  not a loopback alias; backs the SECURITY.md V13 ASVS claim.
- Server-side enforcement of `SESSION_IDLE_SECS`
  (`_session_last_seen`); a leaked bearer token now expires on idle
  regardless of the absolute timeout.
- `WS_KEEPALIVE_REVALIDATE_EVERY` default 4 → 1 so token rotation
  evicts live WebSockets within one keepalive interval.
- Audit middleware: 128-char `resource_id` truncation at the
  classifier boundary + Pydantic `ValidationError` guard so an
  oversized URL can't 500 the user or drop the audit line.
- Audit classifier separates `auth.reviewer_denied` from the
  generic `auth.denied` (threaded via a `contextvars`-keyed error
  code); SIEM can whitelist reviewer noise without losing
  visibility into stolen-token mutation attempts.
- Audit middleware strips URL verbs (`run`/`create`/`prune`/`up`/
  `down`/`stacks`/…) from `resource_id`; the System page's Resource
  column no longer renders "container run" for every new-container
  action.
- Tunnel lifecycle: `_start_tunnel` holds `_tunnel_lock` across the
  full `validate → write → invoke → wait → commit` pipeline so
  concurrent reconnects no longer leak orphan `ssh` processes.
- `/api/tunnel/status` now probes the socket for reachability; a
  stale AF_UNIX file no longer shows a green UI indicator while
  Docker requests return 503.

### Added
- `POST /api/profile/enter-reviewer` — one-way runtime switch into
  the read-only reviewer profile. UI dropdown in the sidebar footer.
- `GET /api/networks/{id}/inspect` — parity with volumes-inspect.
- `ws/exec/{id}` accepts `{"type":"resize","cols":N,"rows":M}`
  frames and calls `exec_resize`; the client sends these on open +
  debounced on window resize so TUI apps render without broken wrap.
- `/api/containers/run` polls for ~800 ms after creation and returns
  `exit_code` + `logs_tail` when the container exits early; covers
  the read-only-rootfs + nginx `/var/cache` shadowing foot-gun.
- `validate_container_id` accepts a container NAME in addition to a
  hex id; `/api/containers/<name>/inspect` works without a
  list-then-id round-trip.
- `ContainerSummary` surfaces `compose_project` + `compose_service`
  from Docker labels.
- Configurable test target matrix: `SKIFF_TEST_TARGET` +
  `SKIFF_TEST_DOCKER_HOST` env vars so the same suite exercises a
  MagicMock, a workstation daemon, a tunnelled host, or a future
  GCE daemon without any hostname embedded in the repo.
- `tools/check_md_links.py` ignores `.venv`, `venv`, `site-packages`,
  and build dirs so `make docs-check` passes on a fresh clone.
- Hypothesis state-machine fuzz tests for the undo queue, WS
  connection counter, and profile-transition gate.

### Changed
- `DELETE /api/containers/{id}` defaults to `undo=true` so a misclick
  is recoverable via `/api/undo/{token}`. Passing `?undo=false` or
  `?force=true` shortcuts to the synchronous path.
- `_ws_acquire` returns a bool instead of raising `HTTPException`
  (can't be translated to a close frame after `accept`); callers
  close with WebSocket code 1013.
- Compose `up` rolls back the project directory on validation
  failure; `compose down` removes the project directory on success
  and returns 404 for un-deployed names.
- Lifespan shutdown caps the undo-queue flush at
  `SHUTDOWN_FLUSH_TIMEOUT` (default 20 s).
- `/api/system/df` wrapped in `wait_for(DF_TIMEOUT=30 s)`.
- CONTRIBUTING.md extras quoted (`pip install -e ".[dev]"`) so zsh
  doesn't glob-expand the bracket set; `pre-commit install` called
  out on the setup path.

### Fixed
- Duplicate audit emission on container lifecycle actions
  (`audit_fields=` path now owns the id; inline `log.info` removed
  from start/stop/restart/pause/unpause/kill/rename).
- Undo queue: `_fire` skips when `PROFILE=reviewer`; NotFound on
  fire is `undo.fired_already_gone` not a failure bump.
- Audit event `profile.switched` description reflects reality —
  emitted regardless of caller, not only from the UI dropdown.
- `docs/dev/code-quality-guide.md`: AP014 row added; stale file
  paths corrected.
- `auth.reviewer_denied` actually emits on the audit line — Loop-7's
  contextvars-scoped fix didn't cross the anyio task boundary; the
  AuditLogMiddleware now peeks the serialized response body for
  `detail.code` so the classification is bulletproof. Regression
  test `test_reviewer_mutation_audit_uses_reviewer_denied` spies on
  the structlog emit through the full FastAPI stack.
- `volumes=["myvol:/"]` rejects with 400
  `validation.mount_target_blocked` at the API boundary instead of
  slipping past `rstrip("/")` into Docker and producing a 500.
- Unknown image refs (`alpine:typo`, `user/nope`) classify as 404
  `image.not_found` instead of the generic 400 `image.pull_failed`.
- 409 conflict envelopes preserve the Docker daemon's `explanation`
  in the `message` field instead of masking memory-swap / unsupported
  config errors as "already started/stopped?".
- `alpine:` (trailing colon), `alpine::` (double separator), and
  `alpine@sha256:` (empty digest) now reject with 400
  `validation.bad_image_name` before the registry allow-list check;
  previously Docker would silently substitute `:latest` and defeat
  pin-only operator policies.
- `POST /api/containers/{id}/update` with `memory=""` no longer
  returns false success. Docker Engine silently ignores `memory=0`
  on a running container; the API now rejects with 400
  `container.memory_uncap_unsupported` so scripted callers see the
  reality and recreate the container to remove the cap.
- Audit `event_type` catalogue drift closed: `auth.denied`,
  `auth.reviewer_denied`, `rate_limit.exceeded`, `api.request`,
  `image.list`, `audit.log_read`, `container.logs_stream`,
  `container.exec_session` declared in `contract/events.py`
  alongside their handler-emitted counterparts.
- Pre-commit ruff pin bumped 0.11.2 → 0.15.1 to match `make ci`'s
  ruff version; `make lint` now also runs `ruff format --check` so
  a contributor's first `pre-commit run --all-files` cannot surface
  style issues CI missed.
- `PROFILE=ci` with empty `API_TOKEN` emits
  `security.ci_profile_needs_token` at boot (the automation persona
  does not fit a wizard-driven first run).
- Foot-gun guards on `MAX_BODY_BYTES` (min 1024), `SESSION_ABS_TIMEOUT`
  (min 60 s), `SESSION_IDLE_SECS` (min 30 s) via a new
  `_positive_int_validator` — a typo (`=0`, `=-1`) now fails the
  process at import instead of serving unusable traffic.
- `audit.ws_auth_lockout` emitted on the exact attempt that crosses
  `WS_AUTH_MAX_ATTEMPTS` so SIEM alerts fire once per activation
  rather than on every failed attempt.
- `_validate_mount_target` rejects `/` and trailing-slash variants
  explicitly so an operator gets a pre-Docker 400.
- CODEOWNERS covers SECURITY.md, CHANGELOG.md, NOTICE, LICENSE — a
  drive-by PR can't silently rewrite the governance story.
- Version string moved to `1.0.1.dev0` during Unreleased; `/health`
  no longer reports `1.0.0` while the code carries 30+ newer entries.
- `_APP_VERSION` resolves from `pyproject.toml` first (editable dev,
  repo checkout) then from `importlib.metadata` (packaged install).

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
