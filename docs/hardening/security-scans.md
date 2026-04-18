# Security Scans — what SKIFF runs against itself, and how to interpret output

Every SKIFF release is expected to pass the five scans below. An adopter
inheriting a given tag can assume that baseline held at release time; an
adopter forking or patching should rerun all five. In this repo's CI, the
first three run on PRs opened from branches in this repository — PRs
opened from forks skip the Claude-Code review step (it needs a repo
secret the fork cannot read) but still run the other gates.

The goal is defence in depth: each tool catches a different bug class,
and together they span the code + deps + runtime surface.

| Tool | Scope | Run locally | Runs in CI |
|---|---|---|---|
| **ruff `--select S` (Bandit)** | Python security smells | `make security` | Runs on in-repo PRs |
| **pip-audit** | Known CVEs in pinned deps | `pip-audit --strict -r requirements.txt` | Runs on in-repo PRs |
| **claude-code-security-review** | Context-aware AI review of each PR diff | GitHub Action | Runs on in-repo PRs |
| **CodeQL (query packs: python + javascript)** | Deep SAST (taint, path injection, etc.) | `codeql database analyze …` | Nightly on main |
| **cyclonedx-py** | SBOM generation for tagged releases | `cyclonedx-py environment --of JSON -o sbom.cdx.json` | Release workflow |

---

## 1. Ruff with security rules

Ruff's `S` ruleset re-implements Bandit's Python security rules. SKIFF's
`pyproject.toml` has them enabled in the linter config:

```bash
# All ruff rules including S*
ruff check skiff/ tests/
# or via the Makefile alias
make security
```

**Interpreting:** S-coded findings are security-relevant. Common false
positives in SKIFF:

- `S108` (insecure `/tmp`): SKIFF uses `/tmp/skiff-docker.sock` for the
  managed tunnel; the basename is validated via `_safe_tunnel_socket_path`
  and the path never comes from user input. Suppressed with
  `# noqa: S108 — <specific reason>`.
- `S603` (subprocess without shell=True): the compose and SSH calls pass a
  pre-built list to `subprocess.run`; shell=False is the safe path. No
  suppression needed.

**Fail-closed policy:** new S-coded findings MUST be either fixed or
suppressed with a specific justification comment (not a blanket `# noqa: S`).
Code review rejects bare suppressions.

---

## 2. pip-audit

Scans `requirements.txt` against the PyPA advisory DB:

```bash
pip install pip-audit
pip-audit --strict -r requirements.txt
```

`--strict` exits non-zero on *any* vulnerability, even without a fix
available. CI uses strict mode; local use during development can drop the
flag when a known-but-unpatched CVE is pending.

**When a finding appears:**

1. Check the CVE's severity and affected versions
2. If a fixed version exists: `make deps` to regenerate `requirements.txt`
   (runs `pip-compile --generate-hashes`)
3. If no fix exists:
   - Assess SKIFF's exposure (does the vulnerable code path get called?)
   - If exposed: stop release; pin to a non-vulnerable older version or
     vendor the dep
   - If not exposed: document in `../hardening/production.md` with the
     CVE ID and why SKIFF is unaffected

---

## 3. claude-code-security-review

Runs on PRs opened from branches in this repository (not from forks — the
action reads a repo secret). Scans the diff only (not the whole repo), so
reviewer comments are scoped to what changed.

**Local equivalent (before pushing):**

Ask any Claude model (CLI, web, or API) to review your PR diff with this
system prompt — same rubric the Action uses:

> You are a security code reviewer. Focus on OWASP Top 10:2025 issues
> in this diff, with special attention to A01 broken access control
> (which now subsumes SSRF), A02 security misconfiguration, A03
> software supply chain failures, A05 injection (command/path/YAML/SQL),
> A07 authentication failures, and A10 mishandling of exceptional
> conditions. For each finding: cite the file:line, explain the
> concrete attack, and propose a minimal fix. Ignore style issues.

Paste the `git diff main…HEAD` output after the prompt.

**Findings triage (same as the Action output):**

- `CRITICAL`: block merge, fix immediately
- `HIGH`: fix this PR unless deferred with ticket + owner + date
- `MEDIUM`: fix this PR or next, reviewer's call
- `LOW` / `INFO`: document in commit message if keeping

---

## 4. CodeQL

Default query suite (`python-security-extended`, `javascript-security-extended`)
runs nightly on main via GitHub code-scanning. For local runs:

```bash
# One-time setup
brew install codeql   # macOS; else install from github.com/github/codeql-cli-binaries

# Build DB
codeql database create codeql-db --language=python,javascript --source-root=.

# Analyze
codeql database analyze codeql-db \
  python-security-extended.qls javascript-security-extended.qls \
  --format=sarif-latest --output=codeql.sarif

# Human-readable summary
codeql database analyze codeql-db ... --format=csv --output=codeql.csv
column -t -s, codeql.csv | less -S
```

**SKIFF-specific:** `py/path-injection` has been closed out through the
`_safe_tunnel_socket_path` sanitiser and the compose filesystem-enumeration
pattern. If those reappear after a refactor, re-read the commit history
(`git log --grep "CodeQL"`) — several earlier commits document why the naïve
fixes don't work.

---

## 5a. Extended dynamic + SAST scanners (semgrep + trivy + ZAP)

Three independent scanners round out the static+CVE stack with
dynamic HTTP-level coverage and a broader SAST rule set. All three
run as Docker containers — no host toolchain changes required.

### Cadence

| Scanner | CI trigger | Local | Rationale |
|---|---|---|---|
| **Semgrep** (`p/owasp-top-ten` + `p/python` + `p/security-audit`) | every PR | `make security-scan` | low false-positive rate on SKIFF's tree, fast (< 30 s) |
| **Trivy fs** (vuln + secret + misconfig) | every PR | `make security-scan` | independent CVE DB to pip-audit, cross-validated coverage |
| **OWASP ZAP Baseline** | weekly schedule | `make security-scan` | dynamic HTTP audit, too slow for per-PR; passive-header signal is a cumulative posture check |

### Expected findings — baseline triage from pre-1.0.1 local run

- **Semgrep**: one inline-annotated finding at `skiff/routers/images.py`
  (SSRF rule). `HUB_REPO_RE` excludes scheme-introducing characters,
  host is hardcoded to `hub.docker.com`, `allow_redirects=False` —
  the combined mitigation is documented on-site so a future reader
  doesn't strip the regex without understanding why.
- **Trivy fs**: zero findings at HEAD on hash-pinned
  `requirements.txt`.
- **ZAP Baseline**: four WARNs permanently allowlisted in
  `.zap/baseline.conf` with documented justification
  (suspicious-comments = user-facing UI strings, storable-content
  = public static, private-IP = wizard example, COEP-missing = N/A
  for same-origin SPA).

### Triage playbook

A new finding in any of the three scanners follows this decision tree:

1. **Is the finding technically accurate?** If the scanner
   misidentifies the code (SSRF rule hitting a hardcoded-host
   endpoint; secret rule hitting a UI string) → false positive.
   Either add an inline annotation (semgrep) or an allowlist
   entry (ZAP `.zap/baseline.conf`, Trivy `.trivyignore`).
2. **Is the finding accurate AND exploitable?** Fix in code.
   Never silence a true positive with an allowlist.
3. **Is the finding accurate but defence-in-depth only?**
   Apply the strengthening if it's cheap (e.g. the Loop-10
   CSP-directive-fallback fix); otherwise document the deferral
   in the CHANGELOG Known Gaps and open a tracking issue.

### Reproducing locally

```bash
# Full run — semgrep + trivy + ZAP (requires docker, boots a local
# SKIFF on port 18300 for the dynamic scan)
make security-scan

# Just one scanner
docker run --rm -v "$PWD:/src:ro" returntocorp/semgrep:latest \
    semgrep scan --config=p/owasp-top-ten --config=p/python /src

docker run --rm -v "$PWD:/repo:ro" aquasec/trivy:latest \
    fs --scanners vuln,secret,misconfig /repo
```

Reports land in `/tmp/skiff-security-scans-local/`.

### Supply-chain note

Every scanner in the CI workflow is pinned to a specific commit SHA
(`aquasecurity/trivy-action@…`, `returntocorp/semgrep-action@…`,
`zaproxy/action-baseline@…`), not a floating `@v<major>` tag.
Dependabot opens update PRs on a weekly cadence; CODEOWNERS on
`.github/` requires maintainer review before a SHA change merges.
This limits the supply-chain blast radius to the pinned commit —
even if a tool's release artefacts are later poisoned, SKIFF keeps
running the audited version until the maintainer explicitly bumps.

---

## 5. SBOM + dependency drift

**Generate:**

```bash
pip install cyclonedx-bom
cyclonedx-py environment --of JSON -o sbom.cdx.json
```

**Compare two releases:**

```bash
jq '.components[] | {name: .name, version: .version}' sbom-v1.0.0.cdx.json | sort > old.txt
jq '.components[] | {name: .name, version: .version}' sbom-v1.1.0.cdx.json | sort > new.txt
diff old.txt new.txt
```

Any new package in a patch release is a red flag (unexpected transitive
dependency pulled in). Investigate before publishing.

---

## 6. Pre-commit hook

`.pre-commit-config.yaml` runs ruff + pip-audit before every commit. One-time
install:

```bash
pip install pre-commit
pre-commit install
```

To temporarily bypass for an emergency commit:

```bash
git commit --no-verify -m 'hotfix: …'
```

Bypass is audit-visible: add a `WHY --no-verify` line in the commit body so
reviewers see the tradeoff.

---

## 7. Runtime posture — what to check on a deployed instance

Beyond pre-release scans, verify the running instance. Every 4xx/5xx
body uses the documented envelope (`{"detail":{"code":…,"message":…}}`);
the snippets below `jq` the `code` field so they stay robust across
translation / wording changes:

```bash
# Auth required
curl -s http://127.0.0.1:8080/api/containers | jq -r .detail.code
# → auth.missing_token

# CSRF enforced on mutations (valid token + missing X-Requested-With)
curl -s -X POST http://127.0.0.1:8080/api/containers/abc/start \
  -H "Authorization: Bearer $TOKEN" | jq -r .detail.code
# → auth.csrf_missing

# Wrong CSRF header value (not the sentinel)
curl -s -X POST http://127.0.0.1:8080/api/containers/abc/start \
  -H "Authorization: Bearer $TOKEN" -H 'X-Requested-With: other' \
  | jq -r .detail.code
# → auth.csrf_invalid

# Metrics endpoint is authed
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/system/metrics
# → 401

# Setup token minimum enforced even if the body is all-whitespace
curl -s -X POST http://127.0.0.1:8080/api/setup \
  -H 'Content-Type: application/json' -H 'X-Requested-With: ContainerManager' \
  -d '{"api_token":"   ","docker_host":"unix:///var/run/docker.sock"}' \
  | jq -r .detail.code
# → setup.token_too_short

# Unknown route returns the documented envelope, not FastAPI's default string
curl -s http://127.0.0.1:8080/api/nonsense | jq -r .detail.code
# → system.route_not_found
```

All of the above must fail closed with the listed `code`. A naive
`grep -q 'Authentication required'` style check is intentionally
avoided — the message wording is subject to i18n / re-phrasing, but
the `code` is a stable contract (see [`docs/errors.md`](../errors.md)).

---

## 8. Incident response

On a confirmed security incident:

1. **Detect** — pip-audit alert, CodeQL regression, or anomalous audit
   event (unusual `container.run` volume, unexpected-registry pull).
2. **Contain** — stop the running SKIFF process (pick the command for
   your deployment shape), rotate `API_TOKEN`, then snapshot the audit
   log before it rotates (quote the path — the macOS default has
   spaces):
   ```bash
   # systemd deployment (per-instance):
   systemctl stop skiff@<instance>

   # run.sh / uvicorn-directly deployment:
   pkill -f 'uvicorn skiff.app:app'

   # snapshot audit log + generate a new token:
   cp "$AUDIT_LOG" "/tmp/skiff-audit-incident-$(date +%s).jsonl"
   export API_TOKEN="$(openssl rand -hex 32)"
   ```
3. **Investigate** — work against the snapshot (not the live file,
   which may rotate); `pip show <suspect-package>` for the installed
   version and `pip-audit` for known CVEs.
4. **Recover** — deploy from a clean environment with verified
   `requirements.txt` hashes; verify SBOM matches expected
5. **Notify** — coordinate disclosure with any affected users via private
   GitHub Security Advisory
