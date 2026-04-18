# OpenSSF Best Practices Badge

[OpenSSF Best Practices](https://www.bestpractices.dev) (formerly CII Best Practices) is a self-attestation program with three levels: **Passing → Silver → Gold**. Each level has concrete criteria a public OSS project can claim, and the badge renders as a trust signal in the project README.

## Target and status

| Level | Target date | Status |
|---|---|---|
| **Passing** | v1.0.1 | self-attestation drafted, see below — ready to file |
| **Silver** | post-1.0.x | requires per-release signing + vulnerability-response SLA with an SLO |
| **Gold** | not planned for single-maintainer posture | requires multiple active developers + N-of-M release review |

## Passing-level self-attestation

The canonical list of Passing criteria lives at [bestpractices.dev/criteria/0](https://www.bestpractices.dev/criteria/0). Against HEAD at v1.0.1 SKIFF meets:

### Basics

- **Public website**: [the GitHub repo](https://github.com/yshk-mxim/skiff-container-manager) serves as the project home.
- **Describe non-trivially what the project does**: README.md opening paragraph.
- **OSS license**: MIT, in [`LICENSE`](../../LICENSE).
- **Include the license in the source distribution**: `LICENSE` in the repo root + `license = {text = "MIT"}` in `pyproject.toml`.
- **Basic documentation for users**: README.md + [docs/api-reference.md](../api-reference.md).
- **Basic documentation for developers**: [CONTRIBUTING.md](../../CONTRIBUTING.md) + [docs/dev/feature-development.md](../dev/feature-development.md).
- **Report mechanism for bugs**: GitHub Issues, with bug-report template at `.github/ISSUE_TEMPLATE/`.

### Change control

- **Publicly-available version-controlled source**: yes — git on GitHub.
- **Distribute updates**: GitHub releases + CHANGELOG.md.
- **Use unique IDs for releases**: SemVer (v1.0.0, v1.0.1, …).
- **Provide release notes for each release**: CHANGELOG.md per-version sections.

### Reporting

- **Provide a process for reporting vulnerabilities**: [SECURITY.md](../../SECURITY.md) § Reporting a Vulnerability documents the private-advisory path.
- **Have a response time commitment**: SECURITY.md states 48-hour acknowledge intent + 5-day assessment + coordinated disclosure. Best-effort (single-maintainer), not contractual.

### Quality

- **Project MUST have at least one automated test suite**: 889 tests (pytest + hypothesis + integration).
- **Tests invoked by a standard command**: `make test` / `make ci`.
- **Policy that new functionality should be added with new tests**: documented in [docs/dev/feature-development.md §7d](../dev/feature-development.md).
- **Project MUST have a well-known compiler/interpreter for the language it's written in**: Python 3.12+, specified in pyproject.toml's `requires-python`.
- **Project MUST produce warning-free builds**: `make ci` treats warnings as errors (ruff, AP-linter, anti-patterns).

### Security

- **Know common vulnerability types**: [SECURITY.md](../../SECURITY.md) ASVS V1–V18 mapping covers OWASP Top 10 + CWE Top 25 indirectly.
- **Use good cryptographic practices**: constant-time HMAC for token compare; secrets.token_bytes(32) for the session cache salt; no stored secrets.
- **Secure delivery against MITM**: distribution via HTTPS (GitHub) + hash-pinned `requirements.txt`.
- **Fix publicly-known vulnerabilities**: no known open CVEs at HEAD (pip-audit + Grype + trivy all clean).

### Analysis

- **At least one SAST tool**: ruff-S + semgrep + claude-code-security-review.
- **At least one dependency-scanning tool**: pip-audit + Grype + trivy.
- **Warnings from the tools above are addressed**: PR cannot merge with a SAST/CVE finding open.

## Filing the badge

Maintainer task — self-attestation requires a form submission at https://www.bestpractices.dev/projects/new.

Once filed, add the badge to the README:

```markdown
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/<PROJECT_ID>/badge)](https://www.bestpractices.dev/projects/<PROJECT_ID>)
```

## Silver-level gaps

Silver requires, and SKIFF does not yet provide:

- **Cryptographic signatures on releases** — tracked in [`slsa.md`](slsa.md).
- **Two-factor authentication on all maintainer accounts** — operator task; GitHub enforces on org accounts.
- **At least 50% of contributors must have taken security training** — single-maintainer posture satisfies trivially when the one maintainer documents their own training.
- **Vulnerability response SLO with an attestation of meeting it** — SECURITY.md has intent, not SLO. A Silver claim would require a historical record of meeting the stated timelines.

## Reference

- [OpenSSF Best Practices](https://www.bestpractices.dev)
- [Passing criteria](https://www.bestpractices.dev/criteria/0)
- [Silver criteria](https://www.bestpractices.dev/criteria/1)
