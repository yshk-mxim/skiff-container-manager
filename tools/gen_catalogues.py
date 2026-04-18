#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Generate docs/errors.md and docs/audit-events.md from the contract catalogues (R15).

Reads `skiff/contract/errors.py::_ERRORS` and
`skiff/contract/events.py::_EVENTS` and emits Markdown tables so SIEM
integrators and API clients can see every code and event without reading
Python.

Usage:
    python tools/gen_catalogues.py           # write both files
    python tools/gen_catalogues.py --check   # exit 1 if out of date

The --check mode is what CI runs to ensure the generated docs stay in
sync with the catalogues. Regenerate before committing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skiff import config as _config
from skiff.contract.errors import _ERRORS
from skiff.contract.events import _EVENTS


def _errors_md() -> str:
    lines = [
        "# Error code catalogue",
        "",
        "GENERATED FROM `skiff/contract/errors.py`. Run",
        "`python tools/gen_catalogues.py` to regenerate; CI `--check`",
        "fails if this file drifts from the Python source.",
        "",
        "Every 4xx/5xx response carries `detail = {code, message, help?}`.",
        "Client switches on `code` (stable); displays `message` (human).",
        "",
        "| Code | Status | Message template | Help |",
        "|---|---|---|---|",
    ]
    for code in sorted(_ERRORS):
        spec = _ERRORS[code]
        help_cell = spec.help.replace("|", r"\|") if spec.help else ""
        msg_cell = spec.message.replace("|", r"\|")
        lines.append(f"| `{code}` | {spec.status} | {msg_cell} | {help_cell} |")
    lines.append("")
    return "\n".join(lines)


def _events_md() -> str:
    lines = [
        "# Audit event catalogue",
        "",
        "GENERATED FROM `skiff/contract/events.py`. Run",
        "`python tools/gen_catalogues.py` to regenerate; CI `--check`",
        "fails if this file drifts from the Python source.",
        "",
        "Every `log.info(\"<event>\", ...)` used for audit purposes appears",
        "here with its severity, required/optional fields, and a one-line",
        "intent for SIEM rule authors.",
        "",
        "| Event | Severity | Required fields | Optional fields | Description |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(_EVENTS):
        spec = _EVENTS[name]
        req = ", ".join(f"`{f}`" for f in spec.required) or "—"
        opt = ", ".join(f"`{f}`" for f in spec.optional) or "—"
        desc = spec.description.replace("|", r"\|")
        lines.append(f"| `{name}` | {spec.severity} | {req} | {opt} | {desc} |")
    lines.append("")
    return "\n".join(lines)


def _config_knobs_md() -> str:
    lines = [
        "# Configuration knob catalogue",
        "",
        "GENERATED FROM `skiff/config.py` + `skiff/_config/defaults.toml`. Run",
        "`python tools/gen_catalogues.py` to regenerate; CI `--check`",
        "fails if this file drifts.",
        "",
        "Every tunable the server reads from the environment or TOML",
        "defaults is registered via `config_knob(...)`. The table lists",
        "every knob with its default, type, and one-line doc. See",
        "[`docs/configuration.md`](configuration.md) for how to override.",
        "",
        "| Env var | Default | Validator | Exposed? | Secret? | Doc |",
        "|---|---|---|---|---|---|",
    ]
    home = os.environ.get("HOME", "")
    for name in sorted(_config.knobs()):
        spec = _config.knobs()[name]
        default = spec.default if spec.default is not None else ""
        # Normalize any default that embeds the generator's home dir back
        # to the portable `$HOME` form so the catalogue isn't machine-
        # specific (and doesn't leak the generator's username).
        if home and default.startswith(home):
            default = "$HOME" + default[len(home):]
        if len(default) > 48:
            default = default[:45] + "…"
        validator = spec.validator.__name__ if spec.validator and hasattr(spec.validator, "__name__") else ""
        if validator == "<lambda>":
            validator = "lambda"
        exposed = "yes" if spec.expose else "no"
        secret = "yes" if spec.secret else "no"
        doc = (spec.doc or "").replace("|", r"\|").replace("\n", " ")
        lines.append(f"| `{name}` | `{default}` | {validator} | {exposed} | {secret} | {doc} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if generated docs are stale.")
    args = parser.parse_args()

    targets = {
        ROOT / "docs" / "errors.md": _errors_md(),
        ROOT / "docs" / "audit-events.md": _events_md(),
        ROOT / "docs" / "config-knobs.md": _config_knobs_md(),
    }
    stale: list[str] = []
    for path, expected in targets.items():
        current = path.read_text() if path.exists() else ""
        if current != expected:
            stale.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.write_text(expected)
                print(f"wrote {path.relative_to(ROOT)}")

    if args.check and stale:
        print("Catalogue docs are stale — regenerate with: python tools/gen_catalogues.py")
        for p in stale:
            print(f"  {p}")
        return 1
    if not args.check and not stale:
        print("Catalogue docs are up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
