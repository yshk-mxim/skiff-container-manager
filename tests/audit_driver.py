# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Observation + finding emission layer for the persona-audit harness.

Public API:
  - `step(name)` — context manager used inside every journey to mark a
    named step. Captures screenshot, DOM, console log, network trace,
    stderr delta, audit-log delta.
  - `Finding` dataclass — structured emission format.
  - `redact_artifact(text)` — pre-commit redactor.
  - `sweep_related(category, surface)` — used after a fix to look for
    the same shape of bug on adjacent surfaces.

Design:
  - Step artifacts land under
    `tests/e2e-artifacts/persona-audit/<pass>/<persona>/<journey>/<step>.*`.
  - The test file that uses `step(name)` must have a pytest fixture
    `audit_observer` that scopes the pass/persona/journey. That fixture
    is installed by `tests/conftest_audit.py`.
  - No secrets ever land on disk raw — the redactor runs on every text
    artifact before write, and `tests/test_audit_artifact_redaction.py`
    enforces the invariant at CI time.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Root where every observation artifact lives. Git-ignored — each pass
# is a subdirectory so previous passes stay for baseline diffing.
ARTIFACT_ROOT = Path("tests/e2e-artifacts/persona-audit")

# Regex catalogue for the redactor. Expanded as journeys surface new
# leak vectors; each entry has a comment explaining why.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Personal filesystem paths — every macOS developer has one.
    (re.compile(r"/Users/[A-Za-z0-9_.\-]+"), "/Users/<user>"),
    # Bearer tokens (long base64-ish).
    (re.compile(r"Bearer [A-Za-z0-9_.\-]{16,}"), "Bearer <redacted>"),
    # Generic api_token / token / password query params (shared with
    # skiff/logging_setup.py::_TOKEN_QS_RE).
    (re.compile(r"((?:api_token|token|password|bearer)=)[^\s&\"'<>]+", re.IGNORECASE),
     r"\1<redacted>"),
    # Anthropic-style sk- tokens (in case a test user pastes one).
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-<redacted>"),
    # IPv4 outside loopback/private/link-local. Replace only anything
    # that looks like a real host IP. Defensive.
    (re.compile(r"\b(?:(?:[0-9]{1,3}\.){3}[0-9]{1,3})\b"), "<ip>"),
)

# Things a redactor must NEVER touch. Keep short — the point is to
# anchor legitimate tokens that look redact-able but aren't.
_REDACTION_KEEP = frozenset({
    "127.0.0.1", "0.0.0.0", "255.255.255.255", "::1",
})


def redact_artifact(text: str) -> str:
    """Run every redaction regex over `text`. Pattern-preserve known-safe
    tokens by bracketing them with a sentinel we replace back after all
    redactions run."""
    if not text:
        return text
    # Temporary sentinels around safe tokens so later regexes don't
    # clobber them. Sentinel chars are chosen to never appear in real
    # output.
    sentinel = "\x00KEEP\x01"
    protected = text
    for safe in _REDACTION_KEEP:
        protected = protected.replace(safe, sentinel + safe + sentinel)
    for pattern, replacement in _REDACTION_PATTERNS:
        protected = pattern.sub(replacement, protected)
    # Unbracket the safe tokens.
    result = protected.replace(sentinel, "")
    return result


# ── Step context ─────────────────────────────────────────────────────

# The current `audit_observer` fixture stashes itself here so `step(name)`
# can find it without asking every journey to pass it down. Set by
# `tests/conftest_audit.py` (module-scoped). Multi-threaded tests would
# need an asyncio-context variable; persona-audit runs single-threaded.
_CURRENT_OBSERVER: AuditObserver | None = None


@dataclass
class Finding:
    """Structured finding emission. One file per finding, committed to
    `tests/e2e-artifacts/persona-audit/<pass>/findings/<id>.json`."""

    id: str
    journey: str
    persona: str
    step: str
    severity: str                           # P0 | high | medium | low
    category: str                           # copy|contract|security|perf|a11y|layout|behaviour|parity
    zero_trust_violation: bool
    title: str
    expected: str
    observed: str
    evidence_paths: list[str] = field(default_factory=list)
    competitor_note: str = ""
    doc_mismatch: str = ""
    class_sweep: str = ""
    covers_historical: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


class AuditObserver:
    """Captures per-step artifacts for a single journey run.

    Owns the current step's screenshot/DOM/console/network captures
    + the finding emitter. Created by the `audit_observer` fixture in
    `tests/conftest_audit.py` per journey-persona combination."""

    def __init__(self, *, pass_n: int, persona: str, journey: str, page: Any | None = None) -> None:
        self.pass_n = pass_n
        self.persona = persona
        self.journey = journey
        self.page = page          # Playwright page — set lazily by the journey
        self.findings: list[Finding] = []
        self.root = ARTIFACT_ROOT / f"pass-{pass_n}" / persona / journey
        self.root.mkdir(parents=True, exist_ok=True)
        # Subscribe to page events if provided up-front.
        self._console_lines: list[str] = []
        self._js_errors: list[str] = []
        self._network_trace: list[dict[str, Any]] = []
        if page is not None:
            self._wire_page(page)

    def set_page(self, page: Any) -> None:
        """Attach page subscriptions late (journeys often create the page
        inside the function body)."""
        self.page = page
        self._wire_page(page)

    def _wire_page(self, page: Any) -> None:
        page.on("console", lambda msg: self._console_lines.append(
            f"[{msg.type}] {msg.text}",
        ))
        page.on("pageerror", lambda exc: self._js_errors.append(str(exc)))
        # Request/response pairs — headers + status only; no bodies.
        page.on("request", lambda req: self._network_trace.append({
            "t": "request", "ts": time.time(),
            "method": req.method, "url": req.url,
            "headers": {k: v for k, v in req.headers.items()
                        if k.lower() not in {"authorization", "cookie"}},
        }))
        page.on("response", lambda resp: self._network_trace.append({
            "t": "response", "ts": time.time(),
            "url": resp.url, "status": resp.status,
            "content_type": resp.headers.get("content-type", ""),
            "csp": resp.headers.get("content-security-policy", ""),
        }))

    # ─── step capture ────────────────────────────────────────────
    @contextmanager
    def step(self, name: str):
        """Mark a named step. Captures artifacts at step exit."""
        safe = _safe_filename(name)
        stem = self.root / safe
        started_at = time.time()
        yield
        # Flush artifacts. Screenshot is the primary signal; the rest
        # are forensic.
        if self.page is not None:
            try:
                self.page.screenshot(path=str(stem) + ".png", full_page=True)
            except Exception:
                pass
            try:
                dom = self.page.content()
                (stem.with_suffix(".dom.html")).write_text(
                    redact_artifact(dom), encoding="utf-8",
                )
            except Exception:
                pass
        # Console + errors + network: flush only the NEW entries since
        # the last step. A naive copy-and-clear keeps memory bounded.
        if self._console_lines:
            (stem.with_suffix(".console.log")).write_text(
                redact_artifact("\n".join(self._console_lines)),
                encoding="utf-8",
            )
            self._console_lines = []
        if self._js_errors:
            (stem.with_suffix(".js-errors.txt")).write_text(
                redact_artifact("\n".join(self._js_errors)),
                encoding="utf-8",
            )
            self._js_errors = []
        if self._network_trace:
            (stem.with_suffix(".har.json")).write_text(
                redact_artifact(json.dumps(self._network_trace, indent=2)),
                encoding="utf-8",
            )
            self._network_trace = []
        # Timing row for overall journey profiling.
        with (self.root / "_timing.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "step": name,
                "duration_s": round(time.time() - started_at, 3),
                "ts": started_at,
            }) + "\n")

    # ─── finding emission ────────────────────────────────────────
    def emit(
        self,
        *,
        step: str,
        severity: str,
        category: str,
        title: str,
        expected: str,
        observed: str,
        zero_trust: bool = False,
        competitor_note: str = "",
        doc_mismatch: str = "",
        class_sweep: str = "",
        covers_historical: str = "",
    ) -> Finding:
        """Record a finding. Stored to disk and returned so the journey
        can optionally fail fast on P0."""
        finding_id = (
            f"pa-{time.strftime('%Y%m%d-%H%M%S')}"
            f"-{self.persona}-{self.journey}-{_safe_filename(step)}"
        )
        f = Finding(
            id=finding_id,
            journey=self.journey,
            persona=self.persona,
            step=step,
            severity=severity,
            category=category,
            zero_trust_violation=zero_trust,
            title=title,
            expected=expected,
            observed=observed,
            evidence_paths=sorted(
                str(p) for p in self.root.glob(f"{_safe_filename(step)}.*")
            ),
            competitor_note=competitor_note,
            doc_mismatch=doc_mismatch,
            class_sweep=class_sweep,
            covers_historical=covers_historical,
        )
        self.findings.append(f)
        findings_dir = ARTIFACT_ROOT / f"pass-{self.pass_n}" / "findings"
        findings_dir.mkdir(parents=True, exist_ok=True)
        (findings_dir / f"{finding_id}.json").write_text(
            f.to_json(), encoding="utf-8",
        )
        return f


# Module-level `step` — resolves to the current observer. Journeys do
# `with step("my_step"): …` without plumbing the observer through.
@contextmanager
def step(name: str):
    if _CURRENT_OBSERVER is None:
        # When running outside the persona-audit harness (e.g. a
        # journey executed as a plain pytest-e2e test), the step
        # context degrades to a no-op so the test still runs.
        yield
        return
    with _CURRENT_OBSERVER.step(name):
        yield


def set_current_observer(obs: AuditObserver | None) -> None:
    """Installed by the `audit_observer` fixture at the start of each
    journey. Unset in teardown."""
    global _CURRENT_OBSERVER
    _CURRENT_OBSERVER = obs


# ── sweep_related ────────────────────────────────────────────────────


def sweep_related(category: str, surface: str) -> list[str]:
    """After a fix, walk OTHER surfaces sharing the finding's category.
    Returns a list of re-run journey names — the caller (usually the
    persona-audit runner) runs them and surfaces new findings.

    Current implementation is a stub — it picks every journey in the
    same category from the registry. Real implementation (phase 2) will
    weight by surface (e.g. if the surface is 'forms', prioritise
    journeys that touch a form)."""
    from tests.journeys import JOURNEY_REGISTRY  # local import to avoid circular

    return [
        key for key, meta in JOURNEY_REGISTRY.items()
        if meta.category == category
    ]


# ── Utilities ────────────────────────────────────────────────────────


def _safe_filename(name: str) -> str:
    """Sanitise a step name for use as a path component. Lowercases,
    replaces runs of non-alphanumeric with `_`, caps length at 80."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_").lower()
    return s[:80] or "step"


# ── Pass number discovery ────────────────────────────────────────────


def current_pass_number() -> int:
    """Pass numbers live in a plain-text file so multiple invocations
    of the harness append to a single pass directory while subsequent
    full passes get a fresh number."""
    pass_file = ARTIFACT_ROOT / ".current_pass"
    pass_file.parent.mkdir(parents=True, exist_ok=True)
    env_override = os.environ.get("PERSONA_AUDIT_PASS")
    if env_override and env_override.isdigit():
        return int(env_override)
    if not pass_file.exists():
        pass_file.write_text("1", encoding="utf-8")
        return 1
    return int(pass_file.read_text(encoding="utf-8").strip() or "1")


def bump_pass_number() -> int:
    pass_file = ARTIFACT_ROOT / ".current_pass"
    current = current_pass_number()
    next_n = current + 1
    pass_file.write_text(str(next_n), encoding="utf-8")
    return next_n
