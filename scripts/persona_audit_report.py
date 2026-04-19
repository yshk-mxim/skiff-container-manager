#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Generate docs/dev/persona_audit_report_<date>.md from the latest pass
artifacts at tests/e2e-artifacts/persona-audit/pass-<N>/findings/*.json.

Aggregates findings by severity, category, persona, journey. Links each
row to its evidence files. Referenced by `make persona-audit-report`.

Stub in this commit — reads and counts findings but produces a minimal
report. Richer formatting (screenshot thumbnails, diff tables, links to
git commits) lands in the tracker commit.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


ARTIFACT_ROOT = Path("tests/e2e-artifacts/persona-audit")
REPORT_DIR = Path("docs/dev")


def _latest_pass_dir() -> Path | None:
    candidates = sorted(
        (p for p in ARTIFACT_ROOT.glob("pass-*") if p.is_dir()),
        key=lambda p: int(p.name.split("-")[1]),
    )
    return candidates[-1] if candidates else None


def main() -> int:
    pass_dir = _latest_pass_dir()
    if pass_dir is None:
        print("no persona-audit passes yet; run `make persona-audit` first", file=sys.stderr)
        return 1

    findings_dir = pass_dir / "findings"
    findings: list[dict] = []
    if findings_dir.exists():
        for fj in sorted(findings_dir.glob("*.json")):
            try:
                findings.append(json.loads(fj.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue

    today = _dt.date.today().isoformat()
    out_path = REPORT_DIR / f"persona_audit_report_{today}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    pass_n = int(pass_dir.name.split("-")[1])

    # Aggregations.
    by_severity = Counter(f.get("severity", "unknown") for f in findings)
    by_category = Counter(f.get("category", "unknown") for f in findings)
    by_persona = Counter(f.get("persona", "unknown") for f in findings)
    zero_trust_hits = [f for f in findings if f.get("zero_trust_violation")]

    per_journey: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        per_journey[f.get("journey", "unknown")].append(f)

    lines = [
        f"# Persona audit report — pass {pass_n} — {today}",
        "",
        f"- Findings this pass: **{len(findings)}**",
        f"- Zero-trust violations: **{len(zero_trust_hits)}**",
        "",
        "## By severity",
        "",
    ]
    for sev in ("P0", "high", "medium", "low"):
        lines.append(f"- {sev}: {by_severity.get(sev, 0)}")
    lines += [
        "",
        "## By category",
        "",
    ]
    for cat, n in by_category.most_common():
        lines.append(f"- {cat}: {n}")
    lines += [
        "",
        "## By persona",
        "",
    ]
    for p, n in by_persona.most_common():
        lines.append(f"- {p}: {n}")
    if per_journey:
        lines += ["", "## By journey", ""]
        for journey, items in sorted(per_journey.items()):
            lines.append(f"### {journey} ({len(items)})")
            for it in items:
                lines.append(
                    f"- **[{it['severity']}]** `{it['id']}` — {it['title']}  \n"
                    f"  expected: {it['expected']!r}  \n"
                    f"  observed: {it['observed']!r}"
                )
            lines.append("")
    else:
        lines += ["", "## By journey", "", "(no findings this pass)", ""]

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
