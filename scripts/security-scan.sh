#!/usr/bin/env bash
# Local dynamic + extended-static security scan for SKIFF.
#
# Mirrors the scanners the CI workflow runs (semgrep, trivy, ZAP
# baseline) so a contributor can reproduce a finding without waiting
# for CI. All scanners run as docker containers — the host stays
# clean, no brew / pip install required beyond docker itself.
#
# Triage playbook: docs/hardening/security-scans.md
#
# Exit status:
#   0 — every scanner clean
#   1 — at least one scanner surfaced a finding NOT already on the
#       allowlist (.zap/baseline.conf for ZAP, inline annotations
#       for semgrep). Output points at the specific file:line.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT_DIR=${OUT_DIR:-/tmp/skiff-security-scans-local}
PORT=${SKIFF_SCAN_PORT:-18300}
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

mkdir -p "$OUT_DIR"
chmod 777 "$OUT_DIR"

echo "=== security-scan (output → $OUT_DIR) ==="

# ── 1. Semgrep (extended SAST) ────────────────────────────────
echo
echo "── semgrep ──"
docker run --rm -v "$REPO_ROOT:/src:ro" returntocorp/semgrep:latest \
    semgrep scan \
    --config=p/owasp-top-ten \
    --config=p/python \
    --config=p/security-audit \
    --json --quiet /src \
    2>/dev/null > "$OUT_DIR/semgrep.json" || true
SEMGREP_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUT_DIR/semgrep.json')).get('results') or []))")
echo "  findings: $SEMGREP_COUNT"
if [ "$SEMGREP_COUNT" -gt 0 ]; then
    python3 -c "
import json
d = json.load(open('$OUT_DIR/semgrep.json'))
for f in d.get('results') or []:
    print(f\"    {f.get('check_id')} — {f.get('path')}:{f.get('start',{}).get('line')}\")
"
fi

# ── 2. Trivy fs (vuln + secret + misconfig) ───────────────────
echo
echo "── trivy fs ──"
docker run --rm -v "$REPO_ROOT:/repo:ro" aquasec/trivy:latest \
    fs --scanners vuln,secret,misconfig --format json /repo \
    2>/dev/null > "$OUT_DIR/trivy-fs.json" || true
TRIVY_COUNT=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/trivy-fs.json'))
n = 0
for r in (d.get('Results') or []):
    n += len(r.get('Vulnerabilities') or []) + len(r.get('Misconfigurations') or []) + len(r.get('Secrets') or [])
print(n)
")
echo "  findings: $TRIVY_COUNT"

# ── 3. ZAP Baseline (dynamic HTTP passive scan) ───────────────
# Requires a running SKIFF. Boot one on $PORT and tear down after.
echo
echo "── ZAP baseline (boots SKIFF on :$PORT) ──"
export API_TOKEN=${API_TOKEN:-$(openssl rand -hex 32)}
export DOCKER_HOST=${DOCKER_HOST:-unix:///var/run/docker.sock}
export BIND_HOST=127.0.0.1
export AUDIT_LOG=$OUT_DIR/audit.jsonl

python3 -m uvicorn skiff.app:app \
    --host 127.0.0.1 --port "$PORT" \
    --no-proxy-headers --forwarded-allow-ips "" \
    >"$OUT_DIR/server.log" 2>&1 &
SERVER_PID=$!

# Wait for /health (up to 15 s).
for _ in $(seq 1 15); do
    curl -sSf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
done

mkdir -p "$OUT_DIR/zap-work"
chmod 777 "$OUT_DIR/zap-work"
cp "$REPO_ROOT/.zap/baseline.conf" "$OUT_DIR/zap-work/baseline.conf" 2>/dev/null || true

docker run --rm --user root \
    -v "$OUT_DIR/zap-work:/zap/wrk/:rw" \
    ghcr.io/zaproxy/zaproxy:stable \
    zap-baseline.py \
    -t "http://host.docker.internal:$PORT" \
    -c baseline.conf \
    -J zap-baseline.json \
    -I > "$OUT_DIR/zap-baseline.log" 2>&1 || true
# `grep -c` returns 0 on no-match; the bare command-substitution gives
# us exactly one line (no fallback concatenation). A later `strip non-
# digits` guard catches the edge case of an empty log file.
# `grep -c` exits 1 on no-match; under `set -euo pipefail` that
# aborts the whole script before the summary prints. Trailing
# `|| true` keeps the substitution happy regardless of grep's exit.
ZAP_FAIL=$(grep -c "^FAIL-NEW:" "$OUT_DIR/zap-baseline.log" 2>/dev/null || true)
ZAP_FAIL=${ZAP_FAIL//[!0-9]/}   # defensive: ensure integer
ZAP_FAIL=${ZAP_FAIL:-0}
echo "  FAIL lines: $ZAP_FAIL"
grep -E "^(FAIL|WARN)-NEW:" "$OUT_DIR/zap-baseline.log" | head -20 || true

# ── Summary ────────────────────────────────────────────────────
echo
echo "=== summary ==="
echo "  semgrep:    $SEMGREP_COUNT finding(s)"
echo "  trivy-fs:   $TRIVY_COUNT finding(s)"
echo "  zap-baseline: see log above"
echo
echo "Reports: $OUT_DIR"
echo "Triage playbook: docs/hardening/security-scans.md"

# Exit non-zero only if any NEW finding surfaces that isn't on an allowlist.
# Semgrep's one inline-annotated false positive (skiff/routers/images.py
# SSRF) is unavoidably counted above — the script doesn't re-parse the
# annotation. Treat "clean" as SEMGREP_COUNT ≤ 1 AND TRIVY_COUNT == 0
# AND ZAP FAIL == 0; anything above is a real delta.
test "$SEMGREP_COUNT" -le 1 && test "$TRIVY_COUNT" -eq 0 && test "$ZAP_FAIL" -eq 0
