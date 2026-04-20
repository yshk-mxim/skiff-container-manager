# NIST Cybersecurity Framework 2.0

[NIST CSF 2.0](https://www.nist.gov/cyberframework) organises cyber-risk management into six functions: **Govern (GV)**, **Identify (ID)**, **Protect (PR)**, **Detect (DE)**, **Respond (RS)**, **Recover (RC)**. CSF 2.0 is an **operator-facing** framework — most controls land on the organisation deploying SKIFF, not on SKIFF the codebase.

This page documents how SKIFF's features support an operator's CSF posture. A CIO mapping SKIFF into their existing CSF program can treat each row below as "SKIFF provides the primitive; the operator defines the policy".

## GV — Govern

Operator-inherited. SKIFF's contribution: transparent governance artefacts — `SECURITY.md`, `CHANGELOG.md`, `CODEOWNERS`, `NOTICE`, `LICENSE`, the ASVS mapping, and this compliance directory. A CIO can cite these when documenting supplier-risk reviews.

## ID — Identify

| Category | How SKIFF helps |
|---|---|
| **ID.AM — Asset Management** | `docs/api-reference.md` + `/api/openapi.json` give a complete inventory of SKIFF's attack surface. `docs/audit-events.md` enumerates every event an operator can ingest. |
| **ID.RA — Risk Assessment** | `SECURITY.md` threat model + ASVS V1–V18 table + [`cis-docker-benchmark.md`](cis-docker-benchmark.md) give an operator the raw material for a risk register. |
| **ID.SC — Supply Chain Risk Management** | Hash-pinned `requirements.txt`, Anchore Syft SBOM per release, Grype CVE scan, Dependabot + CODEOWNERS — see [`nist-ssdf.md`](nist-ssdf.md) PS.2 + PW.3. |

## PR — Protect

| Category | How SKIFF helps |
|---|---|
| **PR.AA — Identity + Access Management** | Constant-time bearer-token compare; 16-char minimum; per-IP brute-force lockout (setup + WS); reviewer-mode read-only posture; CSRF header on every mutation. |
| **PR.AT — Awareness and Training** | `CONTRIBUTING.md` + `docs/dev/feature-development.md` teach the SKIFF-specific secure-coding patterns. |
| **PR.DS — Data Security** | Audit log 0600 on open and rotation; env-var values redacted in inspect responses; WS exec input byte-count-only (never payload). |
| **PR.IR — Platform Security** | Container sandbox (compose forbidden keys), mount-target allowlist, registry allowlist (fail-closed), privileged-port rejection. |
| **PR.PS — Platform Security (Systems)** | Defence-in-depth middleware stack (stripped forwarded headers → CSP + HSTS + permissions-policy + X-Frame-Options → body-size limit → audit). |

## DE — Detect

| Category | How SKIFF helps |
|---|---|
| **DE.AE — Anomalies and Events** | Structured JSONL audit log per `docs/audit-events.md`; 114 error codes + 98 audit events categorised for SIEM indexing. |
| **DE.CM — Continuous Monitoring** | `/health` + `/ready` for probe orchestration; `/api/system/metrics` in Prometheus exposition format; `docs/hardening/production.md` includes SIEM-rule patterns for Loki / ELK. |

## RS — Respond

| Category | How SKIFF helps |
|---|---|
| **RS.CO — Communications** | `SECURITY.md` documents the private-advisory path. Audit events (`auth.denied`, `rate_limit.exceeded`, `audit.ws_auth_lockout`, `audit.setup_lockout`) are pre-classified for SIEM alerting. |
| **RS.AN — Analysis** | Audit log is JSONL with stable `event_type` / `resource_type` / `resource_id` fields — grepable by `jq` and indexable by every SIEM tested (Loki, ELK, Splunk). |
| **RS.MI — Incident Mitigation** | `POST /api/auth/rotate-token` (evicts active WS within one keepalive interval); `POST /api/profile/enter-reviewer` (one-way read-only lock-down); `POST /api/auth/reset-config` (soft restart without host shell access). |

## RC — Recover

| Category | How SKIFF helps |
|---|---|
| **RC.RP — Recovery Planning** | `SHUTDOWN_FLUSH_TIMEOUT` caps undo-queue drain on SIGTERM; lifespan teardown documented; stateless design means every running state is recoverable from env + Docker host state. |
| **RC.IM — Improvements** | Every post-incident learning gets a CHANGELOG entry + an ASVS row update + (when the bug was code-level) a regression test. |

## Mapping tip

A CIO writing a CSF-aligned runbook for SKIFF can lift each row above into their own Protect/Detect/Respond matrices. SKIFF does NOT claim CSF "Tier 4 Adaptive" certification (that's an organisational attestation, not a code attribute) — what SKIFF does claim is **the primitives necessary to operate at Tier 3+ when the organisation's other controls are in place**.

## Reference

- [NIST CSF 2.0](https://csrc.nist.gov/projects/cybersecurity-framework)
- [SECURITY.md — ASVS mapping](../../SECURITY.md)
- [docs/hardening/production.md](../hardening/production.md) — operator playbook
