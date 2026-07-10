# OpenSSF Scorecard

[OpenSSF Scorecard](https://github.com/ossf/scorecard) automatically scores a public GitHub repository against 18 security practices on a 0–10 scale. The score is a trust signal for adopters and a regression detector for maintainers.

## Expected score at v1.0.1

SKIFF's implemented practices map to most of the Scorecard checks. The **current-expected** column below is the score this repo should earn once the Scorecard Action is enabled and one run completes.

| Check | Expected | Why |
|---|---|---|
| **Binary-Artifacts** | 10 | No binary blobs in `main`; only Python source + TOML + static web assets |
| **Branch-Protection** | 8–10 | Depends on GitHub branch-protection settings on `main` (maintainer-controlled) |
| **CI-Tests** | 10 | `make ci` runs on every PR; 1100+ tests + ≥94 % coverage gate |
| **CII-Best-Practices** | 10 | Tracked separately via [`openssf-best-practices.md`](openssf-best-practices.md) |
| **Code-Review** | 8–10 | Single-maintainer posture; CODEOWNERS gates governance paths. External contributors get full review on PRs |
| **Contributors** | 0–3 | Single-maintainer expected low here — deliberate trade-off documented in SECURITY.md §Design Trade-offs |
| **Dangerous-Workflow** | 10 | No `pull_request_target` on untrusted code paths; no `workflow_run` without checks. All workflows use `permissions: contents: read` by default |
| **Dependency-Update-Tool** | 10 | Dependabot configured for pip + github-actions |
| **Fuzzing** | 0–3 | Hypothesis state-machine fuzz counts partially; no OSS-Fuzz integration yet (post-1.0.x) |
| **License** | 10 | MIT, file present |
| **Maintained** | 10 | Regular commits |
| **Packaging** | 10 | Published via GitHub releases + pyproject source distribution |
| **Pinned-Dependencies** | 10 | CI installs every dep via `pip install --require-hashes -r requirements{,-dev}.txt` (both `pip-compile --generate-hashes` locks) then `pip install --no-deps -e .`; all GitHub Actions pinned to commit SHA. A plain `pip install -e .[dev]` scores 9 — Scorecard needs `--require-hashes`/`--no-deps` to treat the command as pinned |
| **SAST** | 10 | ruff-S + semgrep + claude-code-security-review on every PR |
| **Security-Policy** | 10 | `SECURITY.md` present at the repo root, linked from the GitHub Security tab |
| **Signed-Releases** | 0–3 | Tracked as a v1.0.x gap — see [`slsa.md`](slsa.md) |
| **Token-Permissions** | 10 | All workflows set `permissions: contents: read` at the workflow level; per-job escalation only where SARIF upload requires `security-events: write` |
| **Vulnerabilities** | 10 | pip-audit + Grype on every PR; zero open CVEs at HEAD |
| **Webhooks** | n/a | Not applicable for code-only repo |

Expected weighted score: **7.5–8.5 / 10** at v1.0.1. Points we can't hit solo: Contributors (single maintainer), Signed-Releases (tracked for 1.0.x), Fuzzing (hypothesis gets partial credit; OSS-Fuzz is post-1.0.x).

## Running Scorecard yourself

```bash
# One-off, against this repo
docker run --rm -e GITHUB_AUTH_TOKEN=<your_pat> \
    gcr.io/openssf/scorecard:stable \
    --repo=github.com/yshk-mxim/skiff-container-manager
```

## CI integration

The [OpenSSF Scorecard Action](https://github.com/ossf/scorecard-action) runs weekly and publishes SARIF to the GitHub Security tab. Configured at `.github/workflows/scorecard.yml` (see that file for pin + schedule).

## Reference

- [OpenSSF Scorecard](https://github.com/ossf/scorecard)
- [Check documentation](https://github.com/ossf/scorecard/blob/main/docs/checks.md)
