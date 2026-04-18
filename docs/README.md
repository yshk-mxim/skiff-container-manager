# SKIFF Documentation Index

Each doc answers one question. Use the table below as a router.

| If you're… | Go to |
|---|---|
| A brand-new user opening SKIFF for the first time | [README.md](../README.md) (top-level, Quick Start) |
| Deploying for real (homelab, remote host, reverse proxy) | [deployment.md](deployment.md) |
| Tuning config for your fleet (env vars, TOML defaults) | [configuration.md](configuration.md) · [config-knobs.md](config-knobs.md) |
| Hardening a production install (TLS, SSO, SIEM, secrets) | [hardening/production.md](hardening/production.md) |
| Stuck — something isn't working | [runbooks/README.md](runbooks/README.md) or [troubleshooting.md](troubleshooting.md) |
| Connecting an external tool (IDE, Prometheus, Loki, Splunk…) | Use the **Connect external tool** panel in the System page — or [hardening/integrations.md](hardening/integrations.md) for the full catalog |
| Reporting a security issue | [../SECURITY.md](../SECURITY.md) |
| Running security scans locally or in CI | [hardening/security-scans.md](hardening/security-scans.md) |
| Contributing a new feature or endpoint | [dev/feature-development.md](dev/feature-development.md) · [dev/feature-template.md](dev/feature-template.md) |
| Auditing the invariants (maintainer review template) | [dev/zero-trust-review-template.md](dev/zero-trust-review-template.md) |
| Adding a UI string / localisation posture | [dev/i18n.md](dev/i18n.md) |
| Looking up API endpoints | [api-reference.md](api-reference.md) or the live OpenAPI at `/api/docs` |
| Looking up error codes or audit events | [errors.md](errors.md) · [audit-events.md](audit-events.md) |
| Curious about the code organisation | [dev/code-quality-guide.md](dev/code-quality-guide.md) |
| Exploring test scenarios / user personas | [dev/storyboards.md](dev/storyboards.md) · [dev/personas.md](dev/personas.md) |
| Tracking user-facing copy | [dev/copy.md](dev/copy.md) |
| Running the security-review checklist against a PR diff | [dev/review-checklist.md](dev/review-checklist.md) |
| Per-router generated feature docs | [features/](features/) — `features/<router>.generated.md` |

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
