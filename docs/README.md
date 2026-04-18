# SKIFF Documentation Index

The docs are tiered — **casual users only need Tier 1**. Each deeper
tier is for a specific audience and can be safely skipped until you
need it.

## Tier 1 — Getting started (casual users)

Start here. 90% of SKIFF users never need to go past this tier.

| If you're… | Go to |
|---|---|
| Opening SKIFF for the first time | [README.md](../README.md) — top-level Quick Start |
| Looking for a UI walkthrough or feature by feature | [features/](features/) — per-router generated docs |
| Stuck — something isn't working | [troubleshooting.md](troubleshooting.md) for symptom → fix, [runbooks/README.md](runbooks/README.md) for "I'm mid-sequence and hit a wall" |
| Connecting an external tool (IDE, Prometheus, Loki…) | Use the **Connect external tool** panel on the System page in-app |
| Reporting a security issue | [../SECURITY.md](../SECURITY.md) |

## Tier 2 — Operator / deployment

For running SKIFF beyond a single workstation — homelab, shared host,
reverse proxy, audit-log pipeline.

| If you're… | Go to |
|---|---|
| Deploying on a homelab / remote host | [deployment.md](deployment.md) |
| Tuning configuration (env vars, TOML defaults) | [configuration.md](configuration.md) · [config-knobs.md](config-knobs.md) |
| Hardening a deployment (TLS, SSO, SIEM, secrets) | [hardening/production.md](hardening/production.md) |
| Integrating with observability stacks | [hardening/integrations.md](hardening/integrations.md) |
| Running security scans locally or in CI | [hardening/security-scans.md](hardening/security-scans.md) |
| Looking up API endpoints | [api-reference.md](api-reference.md) or live OpenAPI at `/api/docs` |
| Looking up error codes / audit events | [errors.md](errors.md) · [audit-events.md](audit-events.md) |

## Tier 3 — Developer / contributor

For people modifying SKIFF's code or tests.

| If you're… | Go to |
|---|---|
| Contributing a new feature or endpoint | [dev/feature-development.md](dev/feature-development.md) · [dev/feature-template.md](dev/feature-template.md) |
| Auditing invariants (maintainer review template) | [dev/zero-trust-review-template.md](dev/zero-trust-review-template.md) |
| Running the security-review checklist against a PR diff | [dev/review-checklist.md](dev/review-checklist.md) |
| Understanding the code organisation | [dev/code-quality-guide.md](dev/code-quality-guide.md) |
| Exploring test scenarios / personas | [dev/storyboards.md](dev/storyboards.md) · [dev/personas.md](dev/personas.md) |
| Adding a UI string / working on i18n | [dev/i18n.md](dev/i18n.md) · [dev/copy.md](dev/copy.md) |

## Tier 4 — Compliance & research reference

> **Scope note.** [`compliance/`](compliance/) documents **SKIFF's own
> posture** against frameworks a researcher or evaluator might map
> against a code project. It is a descriptive first-party reference,
> not guidance for other projects or organisations.

See [`compliance/README.md`](compliance/README.md) for the full index.

## Two-minute orientation

- **SKIFF is one Python process.** No database, no persistent state
  beyond the rotating audit-log file and uploaded compose YAML.
- **Single bearer token auth** (optionally fronted by oauth2-proxy for
  multi-user SSO). Zero-trust posture: env values, SSH targets,
  password prompts never cross the UI.
- **Works with any Docker Engine API runtime** (Docker Engine, Colima,
  OrbStack, Rancher Desktop, Podman rootless, …) over a local socket
  or a managed SSH tunnel. Defaults work without root on macOS and Linux.
- **Not yet**: Dockerfile builds, Kubernetes cluster management, plugin
  system. Deliberately out of scope — see `SECURITY.md` for the full
  scope statement.

## Docs that overlap (and how to pick)

- **runbooks/README.md vs troubleshooting.md** — RUNBOOKS is storyline-based
  ("I lost my token, how do I recover?"); troubleshooting.md is a
  symptom → fix quick-reference table. Use RUNBOOKS for "I'm stuck in
  a sequence", troubleshooting for "what does this specific error mean?"
- **hardening/integrations.md vs the in-UI Connect panel** — the Connect panel
  on the System page generates snippets from live server config; that's
  what you paste day-to-day. hardening/integrations.md documents the full catalog
  including edge cases, GCP Cloud Logging specifics, and CI workflows.
- **api-reference.md vs `/api/docs`** — `/api/docs` serves the live
  OpenAPI 3.1 spec and is the source of truth. api-reference.md is a
  hand-curated narrative view of the same surface — slower to update
  but easier to skim for "what's the rate limit on this endpoint?"
  during code review.
