# Zero-Trust Invariant Walk — Maintainer Review Template

> **This file is a blank template, by design.** The `Status:` / `Evidence:`
> lines under each invariant below are placeholders meant to be filled
> in during a specific review run — this repo publishes the empty form,
> not a filed review. Adopters fork it to run their own audit cadence;
> the SKIFF maintainer uses a private, filled-in copy per release.
>
> For operator-facing guidance on how SKIFF mitigates each of the
> threats listed here, read [`../hardening/production.md`](../hardening/production.md)
> — that document is the substantive security reference.

This is the audit template the SKIFF maintainer uses to verify the
codebase still honours the invariants declared in `SECURITY.md`. It is
NOT a deployment checklist — an operator deploying SKIFF should start
at [`../hardening/production.md`](../hardening/production.md) instead.

Adopters are welcome to fork this template for their own internal
audit cycle; nothing in SKIFF depends on the review being filed. Adapt
the invariants to your own threat model.

**How to use**: fill each row with **PASS** / **FAIL** / **DEFERRED**
plus one sentence of evidence (file path + line, test name, or
dashboard screenshot). A FAIL is a P0 — either fix the code or
consciously revise the invariant with a decision record.

## Reviewer checklist

Before starting:

- [ ] Pull latest `main` and rebuild the venv (`make dev`).
- [ ] Run `make test` and confirm green; flaky tests are not evidence.
- [ ] Skim the last quarter's audit log for anomalies (commands
      executed, token rotations, setup-lockout events).

---

## Z1 — No env value reaches the client (only `expose=True` knobs)
Enforced by: `/api/config` handler, `skiff.config.knobs()` registry.

Evidence:

Status:

---

## Z2 — API_TOKEN logs show only the last 8 chars (suffix)
Enforced by: `auth.token_rotated` audit event spec.

Evidence:

Status:

---

## Z3 — Tunnel ssh_target is server-only (never in any response)
Enforced by: `setup_state()`, `tunnel_status()`.

Evidence:

Status:

---

## Z4 — No `shell=True` in subprocess calls
Enforced by: compose up / down handlers.

Evidence (grep): `git grep -n "shell=True" skiff/` should return
zero matches.

Status:

---

## Z5 — Every write uses a typed, validated body
Enforced by: `@secure_route.mutate` + Pydantic bodies on every write route.

Evidence:

Status:

---

## Z6 — No `localStorage`; sessionStorage only (except theme pref)
Enforced by: client convention + `../hardening/production.md §8`.

Evidence (grep): `git grep -n "localStorage" skiff/static/` should
match only the theme-preference code path.

Status:

---

## Z7 — Every path-handling site uses `resolve() + is_relative_to()`
Enforced by: `skiff.validators._validate_mount_target`, compose dir
derivation.

Evidence:

Status:

---

## Z8 — Every registry pull / push hits the allowlist
Enforced by: `validate_image_registry`.

Evidence:

Status:

---

## Z9 — First-boot setup window closes 5 min after startup
Enforced by: `do_setup()` gating against `APP_START_MONOTONIC`.

Evidence:

Status:

---

## Z10 — Rate limits apply to every `/api/*`
Enforced by: `@secure_route.*(RATE.X)` on every route.

Evidence (grep): `git grep -n "@router\.\(get\|post\|delete\|put\)" skiff/routers/` — each hit must have a `@secure_route.*` decorator above it.

Status:

---

## Z11 — Secret knobs never serialise
Enforced by: `config_knob(secret=True)` + Pydantic `SecretStr` (future).

Evidence:

Status:

---

## Z12 — WS upgrades validate Origin + first-message AUTH
Enforced by: `_validate_ws_origin`, `_validate_ws_token_from_message`.

Evidence:

Status:

---

## Z13 — Setup POST is per-IP lockout-guarded
Enforced by: `_setup_fail` + `SETUP_MAX_ATTEMPTS` + `SETUP_LOCKOUT_SECS`.

Evidence:

Status:

---

## Z14 — No third-party CDN resources (CSP `script-src 'self'`)
Enforced by: CSP header from `skiff/_config/security_headers.toml`.

Evidence:

Status:

---

## Z15 — SBOM + NOTICES shipped per release
Enforced by: `.github/workflows/security.yml` (anchore/sbom-action, SHA-pinned).

Evidence:

Status:

---

## Summary

- Overall: PASS / FAIL
- P0 items to open: …
- Follow-up scheduled for next quarter: …

Reviewer: ………  Date: YYYY-MM-DD
