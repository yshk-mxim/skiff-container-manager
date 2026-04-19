# NIST Secure Software Development Framework (SP 800-218)

SKIFF maps to **NIST SP 800-218 SSDF v1.1**, which organises secure-development practices into four groups: **Prepare the Organization (PO)**, **Protect the Software (PS)**, **Produce Well-Secured Software (PW)**, and **Respond to Vulnerabilities (RV)**.

This page lists each practice and what SKIFF does to satisfy it. Operators deploying SKIFF inherit their own organisation's PO/PS/PW/RV controls on top of these.

## PO — Prepare the Organization

| Practice | SKIFF posture |
|---|---|
| **PO.1 Define Security Requirements for Software Development** | [`SECURITY.md`](../../SECURITY.md) documents the ASVS v5.0 threat model + control mapping; [`CONTRIBUTING.md`](../../CONTRIBUTING.md) requires every PR to pass the security CI gates. |
| **PO.2 Implement Roles and Responsibilities** | Single-maintainer project. `CODEOWNERS` scopes governance edits to `@yshk-mxim` across `SECURITY.md`, `CHANGELOG.md`, `NOTICE`, `LICENSE`, `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/`, `pyproject.toml`, `requirements.txt`. |
| **PO.3 Implement Supporting Toolchains** | Ruff (lint + security), pip-audit, trivy, semgrep, Syft/Grype (SBOM + CVE), OWASP ZAP (weekly), pre-commit, gitleaks, custom AP linter. All pinned to commit SHA. |
| **PO.4 Define and Use Criteria for Software Security Checks** | `make ci` is the release gate: ≥94% coverage floor (pyproject `fail_under`), zero lint / security / anti-pattern / ASVS / docs-drift findings. Critical security modules (`secure.py`, `contract/errors.py`, `contract/events.py`) track at 100%. |
| **PO.5 Implement and Maintain Secure Environments for Software Development** | GitHub Actions CI with `contents: read` default permissions, SHA-pinned actions, `GITHUB_TOKEN` scope-limited per job. No build artefacts shipped without SARIF + SBOM. |

## PS — Protect the Software

| Practice | SKIFF posture |
|---|---|
| **PS.1 Protect All Forms of Code from Unauthorized Access and Tampering** | All source in `main` protected; CODEOWNERS gates governance paths; branch protection recommended on the canonical mirror. |
| **PS.2 Provide a Mechanism for Verifying Software Release Integrity** | Every release carries an Anchore-generated CycloneDX SBOM (30-day artifact retention today; signed-tag + attestation workflow tracked as a v1.0.x gap — see [`slsa.md`](slsa.md)). |
| **PS.3 Archive and Protect Each Software Release** | Git tag + GitHub Release. Release artefacts available for download; hash-pinned `requirements.txt` means a released version can be rebuilt bit-identical. |

## PW — Produce Well-Secured Software

| Practice | SKIFF posture |
|---|---|
| **PW.1 Design Software to Meet Security Requirements and Mitigate Security Risks** | Threat model documented in `SECURITY.md` (single-token trade-off, single-Docker-host scope, zero-trust defaults, fail-closed on empty allowlist). |
| **PW.2 Review the Software Design** | ASVS V1–V18 self-assessment in `SECURITY.md`. External static review via `anthropics/claude-code-security-review` on every PR. |
| **PW.3 Reuse Existing, Well-Secured Software When Feasible** | Minimal runtime-install surface (10 direct deps); dev / e2e / optional integrations isolated via `[project.optional-dependencies]` extras. Hash-pinned via `pip-compile --generate-hashes --strip-extras`. |
| **PW.4 Create Source Code by Adhering to Secure Coding Practices** | Ruff S-rules (Bandit equivalent), custom AP001–AP014 linter, cyclomatic-complexity cap (McCabe ≤ 10 hard, target ≤ 3), no `shell=True` / no `yaml.load` / no `eval` / no `exec` / no cookies / no CORS wildcards — enforced at lint + anti-pattern layers. |
| **PW.5 Configure the Compilation, Interpreter, and Build Processes to Improve Executable Security** | Python source; no compilation artefacts. CSP strict (`default-src 'self'`, no `unsafe-inline` on `script-src`). Security-headers middleware outermost (stripped forwarded headers → HSTS/CSP/XFO/permissions-policy → body-size limit → audit → handler). |
| **PW.6 Review and/or Analyze Human-Readable Code to Identify Vulnerabilities and Verify Compliance with Security Requirements** | PR-level: ruff, semgrep, trivy, claude-code-security-review. Release-level: full static+dynamic sweep (see [`../hardening/security-scans.md`](../hardening/security-scans.md)). |
| **PW.7 Test Executable Code to Identify Vulnerabilities and Verify Compliance with Security Requirements** | 1100+ unit + integration + hypothesis state-machine tests plus the persona-audit journey suite; ≥94% coverage gate, with critical security modules (`secure.py`, `contract/errors.py`, `contract/events.py`) at 100%. Weekly OWASP ZAP Baseline on a live SKIFF. |
| **PW.8 Configure Software to Have Secure Settings by Default** | `BIND_HOST=127.0.0.1` default; `read_only=true` container-run default; `undo=true` delete default; `ALLOWED_REGISTRIES` fail-closed on empty; positive-int validators for body/session knobs; foot-gun boot warnings for CI-profile, empty token, non-loopback bind, unencrypted DOCKER_HOST. |
| **PW.9 Archive and Protect Each Software Release** | Same as PS.3. |

## RV — Respond to Vulnerabilities

| Practice | SKIFF posture |
|---|---|
| **RV.1 Identify and Confirm Vulnerabilities on an Ongoing Basis** | Weekly Dependabot pip + GHA updates; weekly Grype CVE scan via `.github/workflows/security.yml`; per-PR pip-audit + ruff-S + semgrep + trivy. |
| **RV.2 Assess, Prioritize, and Remediate Vulnerabilities** | Advisory process documented in `SECURITY.md` §Reporting a Vulnerability. No hard SLA (single-maintainer posture); triage intent: 48 h acknowledge, 5 days assess, coordinated disclosure. |
| **RV.3 Analyze Vulnerabilities to Identify Their Root Causes** | Every fix lands with the CVE / CCSR / ASVS reference in the commit message; `CHANGELOG.md` `### Security` sub-section accumulates every security-relevant change since v1.0.0. |

## Limitations

- **RV.2 timelines** are best-effort intents, not contractual SLAs. A single-maintainer project cannot promise 24/7 vulnerability response. An organization deploying SKIFF into a high-assurance environment should fork and self-patch.
- **PS.2 signed releases** are tracked as a v1.0.x gap. See [`slsa.md`](slsa.md) for the roadmap toward SLSA Level 2.
- **PO.2 separation of duties** is not possible in a single-maintainer project. The implicit compensating control is the GitHub audit log and CODEOWNERS; a forking organization can add its own second-pair-of-eyes rule.

## Reference

- [NIST SP 800-218: Secure Software Development Framework v1.1](https://csrc.nist.gov/publications/detail/sp/800-218/final)
- SKIFF SECURITY.md — ASVS mapping + threat model
- [`../hardening/security-scans.md`](../hardening/security-scans.md) — scanner cadence + triage playbook
