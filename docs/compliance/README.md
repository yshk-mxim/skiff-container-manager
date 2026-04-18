# Compliance framework coverage

> **Scope note.** This directory documents **SKIFF's own compliance
> posture as a personal, volunteer-maintained open-source project**.
> It is descriptive — what SKIFF does, the choices its author made,
> and the evidence pointing to those claims. It is **not** a standard,
> a guideline, or a recommendation for any other project, product,
> system, or organisation. Readers evaluating SKIFF for their own use
> should weigh these claims against their own risk appetite and
> operating environment.

SKIFF is open-source code, not a certified service. The pages below
map SKIFF's **code-level** posture to each framework — what SKIFF
implements, what the operator inherits, and what SKIFF explicitly
does NOT claim.

## Tier 1 — directly implemented or documented

| Framework | Doc | One-line summary |
|---|---|---|
| **OWASP ASVS v5.0** | [`../../SECURITY.md#owasp-asvs-v50-mapping`](../../SECURITY.md) | V1–V18 control mapping; 13 EVIDENCE / 1 PARTIAL / 1 OPERATOR / 4 N/A |
| **OWASP Top 10** | [`../hardening/security-scans.md`](../hardening/security-scans.md) | covered by semgrep `p/owasp-top-ten` ruleset on every PR |
| **WCAG 2.1 Level AA** | [`wcag-2-1-aa.md`](wcag-2-1-aa.md) | self-assessment + automated axe-core pass |
| **CIS Docker Benchmark** | [`cis-docker-benchmark.md`](cis-docker-benchmark.md) | how SKIFF's defaults align with operator-relevant items |
| **NIST SSDF (SP 800-218)** | [`nist-ssdf.md`](nist-ssdf.md) | practice-by-practice mapping |
| **NIST CSF 2.0** | [`nist-csf.md`](nist-csf.md) | Identify/Protect/Detect/Respond/Recover |
| **OpenSSF Scorecard** | [`openssf-scorecard.md`](openssf-scorecard.md) | automated score + `.github/workflows/scorecard.yml` |
| **OpenSSF Best Practices Badge** | [`openssf-best-practices.md`](openssf-best-practices.md) | self-attestation, Passing level target |
| **SLSA v1.0** | [`slsa.md`](slsa.md) | Level 2 target (signed releases + hosted build) |

## Tier 2 — operator-inherited, not SKIFF-level

| Framework | Status |
|---|---|
| **GDPR / CCPA / CPRA** | SKIFF processes no PII. See [`privacy.md`](privacy.md). |
| **Section 508 (US fed) / EN 301 549 (EU)** | Aligned with WCAG 2.1 AA work above. |

## Tier 3 — explicitly NOT claimed

These are **organizational** certifications requiring an external
auditor. SKIFF is a code project, not a certified service.

| Framework | Position |
|---|---|
| **SOC 2 Type II** | not claimed; operator's hosting environment may have its own SOC 2 scope |
| **ISO/IEC 27001 / 27017 / 27018** | not claimed; see above |
| **HIPAA** | no PHI processing by design; no BAA available |
| **PCI DSS 4.0** | no cardholder data processing by design |
| **FedRAMP** | not claimed; operator's hosting environment may be FedRAMP-authorized independently |

An operator evaluating SKIFF for a regulated environment will want to
treat this tier as "operator and hosting-environment responsibility"
and map SKIFF's Tier-1 code-level controls into their own compliance
program. SKIFF's author makes no claim about what that program must
contain.

## Report a compliance gap

A claim in this directory that doesn't match HEAD is a bug. File it
via the normal [security advisory process](../../SECURITY.md#reporting-a-vulnerability)
if the gap is security-material, or as a regular issue otherwise.
