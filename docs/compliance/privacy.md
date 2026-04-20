# Privacy posture — GDPR, CCPA, and the "no PII" story

SKIFF **processes no personally identifiable information by design**. This page documents what that means for an operator deploying SKIFF into a privacy-regulated environment (GDPR, CCPA/CPRA, HIPAA-adjacent, etc.) and which responsibilities land on the operator vs on SKIFF the code.

## What SKIFF stores, logs, or transmits

| Data class | Stored? | Logged to audit? | Transmitted where? | Retention |
|---|---|---|---|---|
| API token (bearer) | **in memory only** (never on disk) | **never** — only last 8 chars as `token_suffix` for correlation | `Authorization` header on localhost or TLS-terminated proxy | per-process (rotated via `/api/auth/rotate-token`) |
| Container / image / volume names + IDs | no SKIFF-side persistence (all state on Docker host) | yes — audit event `resource_id` field | UI response only | per Docker daemon |
| Environment variables in a container | visible via `/api/containers/{id}/inspect` | values redacted via `_ENV_SENSITIVE_RE` (`SECRET`, `PASSWORD`, `TOKEN`, `KEY`, `CREDENTIAL`, `AUTH`, `CERT`, `PRIVATE`) when returned to the client | UI response only | per Docker daemon |
| WebSocket exec input (shell typing) | no | **never** — only byte count; content is explicitly not captured | none — flows to the container's PTY and is lost when the exec session ends | session-scoped |
| Client IP | yes — audit event `remote` field | yes | never forwarded | per audit-log rotation window |
| X-Forwarded-User | only when `TRUST_FORWARDED_HEADERS=true` | yes — audit event `user` field | never forwarded | per audit-log rotation window |

Nothing else. SKIFF has no user database, no profile store, no analytics, no telemetry, no phone-home.

## GDPR — operator responsibilities

The GDPR (EU Regulation 2016/679) applies to processing "personal data of data subjects in the Union". The operator deploying SKIFF is the **controller** for any personal data that lands in SKIFF's audit log — specifically `remote` (client IP), `token_suffix` (an 8-char identifier), and `user` (X-Forwarded-User when enabled).

SKIFF as code does not hold any of the GDPR subject-rights obligations (right of access, rectification, erasure, portability) — those land on the operator. SKIFF's contribution:

- **Article 5(1)(c) data minimization**: the audit log stores what's necessary for security investigation (IP + token suffix + action) and nothing else. No names, no browser fingerprints, no correlation IDs beyond the salted session-cache key.
- **Article 5(1)(e) storage limitation**: audit log rotation is operator-configurable via `AUDIT_MAX_MB` + `AUDIT_BACKUP_COUNT`. Operator sets the retention window; SKIFF enforces it.
- **Article 25 data protection by design**: constant-time token compare, server-side session expiry, explicit reviewer-mode, compose sandbox — all documented in [SECURITY.md](../../SECURITY.md).
- **Article 32 security of processing**: see the full ASVS V1–V18 mapping.
- **Article 33 notification of breach**: out of scope for the code; operator process.

## CCPA / CPRA — same shape

California's privacy statute treats similar fields as "personal information" (IP addresses + inferences derived from session IDs). The operator is the **business** under CCPA. SKIFF's data-minimization posture + audit retention knobs apply identically.

## HIPAA — by design out of scope

SKIFF **processes no protected health information (PHI)**. SKIFF is a Docker UI; it has no understanding of what runs inside a given container. If an operator runs HIPAA-covered workloads in containers SKIFF manages, the HIPAA compliance boundary is at the **container**, not at SKIFF — the containers' own encryption, access controls, and audit trails satisfy HIPAA; SKIFF as a management plane does not need a BAA.

Operators in a HIPAA environment should:

- NOT paste PHI into the WS exec terminal (SKIFF's WS exec input is **not** captured in audit but the CONTAINER could log it — treat exec sessions as you would treat any `kubectl exec` for compliance purposes).
- Configure the Docker daemon's TLS posture per HIPAA §164.312(a)(1) access controls.
- Keep SKIFF's API token rotation on a schedule that matches their HIPAA program (SKIFF supports rotation at any cadence).

## PCI DSS 4.0 — by design out of scope

Same reasoning as HIPAA. No cardholder data passes through SKIFF. Operator's containers are the compliance boundary.

## Privacy gaps worth noting

- **`AUDIT_LOG` path default**: if the operator doesn't set `AUDIT_LOG`, SKIFF writes to `$HOME/Library/Application Support/skiff/audit.jsonl` (macOS) or the Linux XDG equivalent. That path's retention is governed by SKIFF's rotation knobs (`AUDIT_MAX_MB`, `AUDIT_BACKUP_COUNT`), but it's NOT backed up or mirrored anywhere by SKIFF itself. An operator who needs DSAR (data-subject access request) support should configure `AUDIT_LOG` to a path a SIEM is tailing, and use the SIEM's DSAR tooling.
- **TLS termination**: SKIFF doesn't terminate TLS (operator-inherited, per [`production.md`](../hardening/production.md)). Plaintext client IP reaches the audit log via the TCP peer; X-Forwarded-For is stripped by default.
- **No cookie-based tracking**: SKIFF emits no `Set-Cookie` headers. The browser stores the API token in `sessionStorage` (cleared on tab close); there's nothing to consent-banner.

## Reference

- [GDPR](https://gdpr.eu)
- [CCPA / CPRA](https://cppa.ca.gov/)
- SKIFF SECURITY.md — full threat model
- SKIFF docs/audit-events.md — what the audit log captures
