#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Generate `docs/features/*.generated.md` API-surface tables from FastAPI routes (R14).

Hand-written `docs/features/<module>.md` files carry the narrative
(what it is / who it's for / threat model). The API surface — every
route method, path, and its tags — is derivable from the registered
routes and should not be hand-maintained.

For each router module this script emits a companion
`docs/features/<module>.generated.md` with:

  - Every route: method, path, audit event (if set), rate tier,
    error codes the handler might raise.
  - The tags from `@router.<method>(... tags=[...])` so integrators
    can group routes by domain.

The hand-written file references the generated one with a "See also"
link; the generated file is regenerated on every release and never
edited by hand.

Usage:
    python tools/gen_feature_docs.py         # write all features
    python tools/gen_feature_docs.py --check # exit 1 if stale

--check is what CI runs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skiff.app import app


def _routes_by_module() -> dict[str, list[dict]]:
    """Group skiff.routers.* routes by the router module they live in.

    Routes from third-party modules (FastAPI's built-in /openapi.json,
    StaticFiles mount, etc.) are skipped — they're not part of the
    feature surface we document.
    """
    by_mod: dict[str, list[dict]] = {}
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        mod_name = getattr(endpoint, "__module__", "") or ""
        if "skiff.routers." not in mod_name:
            continue
        key = mod_name.rsplit(".", 1)[-1]
        marker = getattr(endpoint, "_skiff_secure", {}) or {}
        by_mod.setdefault(key, []).append({
            "method": ",".join(sorted(getattr(route, "methods", set()))),
            "path": getattr(route, "path", ""),
            "tags": getattr(route, "tags", []) or [],
            "audit": marker.get("audit") or "",
            "rate": marker.get("rate") or "",
            "csrf": marker.get("csrf"),
            "name": endpoint.__name__,
            "doc": (endpoint.__doc__ or "").strip().split("\n", 1)[0],
        })
    return by_mod


def _feature_md(module: str, entries: list[dict]) -> str:
    lines = [
        f"# API surface: {module}",
        "",
        f"GENERATED FROM `skiff/routers/{module}.py` by `tools/gen_feature_docs.py`.",
        "Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails",
        "on drift. The hand-written `docs/features/" + module + ".md` (if any)",
        "carries the narrative and threat-model context.",
        "",
        "| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in sorted(entries, key=lambda e: (e["path"], e["method"])):
        csrf_cell = "✓" if entry["csrf"] else ("—" if entry["csrf"] is False else "")
        doc = entry["doc"].replace("|", r"\|")
        audit = f"`{entry['audit']}`" if entry["audit"] else "—"
        rate = entry["rate"] or "—"
        lines.append(
            f"| {entry['method']} | `{entry['path']}` | {audit} | {rate} | {csrf_cell} | `{entry['name']}` | {doc} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if any generated feature doc is stale.")
    args = parser.parse_args()

    out_dir = ROOT / "docs" / "features"
    out_dir.mkdir(parents=True, exist_ok=True)
    by_mod = _routes_by_module()
    stale: list[str] = []
    for module, entries in sorted(by_mod.items()):
        target = out_dir / f"{module}.generated.md"
        expected = _feature_md(module, entries)
        current = target.read_text() if target.exists() else ""
        if current != expected:
            stale.append(str(target.relative_to(ROOT)))
            if not args.check:
                target.write_text(expected)
                print(f"wrote {target.relative_to(ROOT)}")

    if args.check and stale:
        print("Feature docs are stale — regenerate with: python tools/gen_feature_docs.py")
        for p in stale:
            print(f"  {p}")
        return 1
    if not args.check and not stale:
        print("Feature docs are up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
