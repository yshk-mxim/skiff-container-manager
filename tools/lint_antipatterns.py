#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Project-specific anti-pattern linter.

Ruff covers the broad Python hygiene surface; this file encodes the
SKIFF-specific rules that came out of the v3 architecture review.
Rules:

    AP001  nested try/except
        A try block whose body contains another try. The inner handler
        almost always wants to be a ``contextlib.suppress(...)`` or a
        one-line helper (``_close_fd_quiet``, ``_close_client_quiet``).

    AP002  try/except with if-on-success branching inside the try body
        Mixing the "does it succeed?" axis with the "is the result good?"
        axis in one block is nasty — split into sequential early-return
        blocks, one for each fail mode.

    AP003  bulky import block from one skiff module
        ``from skiff.X import (A, B, C, D, E, F, G, ...)`` with > 6 names
        is a namespacing smell — prefer ``from skiff import X`` + ``X.A``.
        Catalogue modules (contract.responses, contract.errors) are exempt
        because pulling one name is the common pattern anyway.

    AP004  module-level ``getattr(obj, "literal", default)`` with a
        non-``None`` default
        Code-smell: typically means "config item by string key"; prefer a
        dict lookup or a typed attribute. ``getattr(obj, "x", None)`` is
        permitted because it's the idiomatic "optional attribute" form.

    AP005  embedded policy literal at a wiring call site
        Flags int/str/list literals passed as keyword args for known
        "policy" parameter names (``port``, ``workers``, ``maxBytes``,
        ``backupCount``, ``timeout``, ``allow_methods``, ``allow_headers``,
        ``allow_origins``, ``title``, ``description``, ``log_level``,
        ``host``, ``bind_host``, ``max_body_bytes``, etc.) when the call
        is NOT inside ``skiff/config.py``. Config lives in config.py; an
        uvicorn/logging/FastAPI call should reference ``config.X``.

    AP006  ``os.environ.get()`` / ``os.environ[...]`` outside config.py
        Every env var read belongs in ``skiff/config.py`` so defaults,
        validators, and doc live in one place. Exempt: system-env
        pass-through reads for subprocess spawning (PATH, HOME,
        SSH_AUTH_SOCK) are read by name and allowlisted below.

    AP007  excessive block nesting (>= 4 levels deep)
        More than three nested block scopes inside one function
        (if/for/while/try/with combinations) is untestable by example —
        2^4 = 16 branch paths. Extract inner blocks into helpers or
        collapse with early returns.

    AP008  isinstance-ladder (3+ sequential isinstance checks)
        ``if isinstance(x, A): ...; elif isinstance(x, B): ...;
        elif isinstance(x, C): ...`` is a type-dispatch wanting a
        Pydantic discriminated union or a dict-based factory. Manual
        type parsing at the boundary is exactly what Pydantic is for.

    AP009  long ``if/elif`` chain (5+ branches)
        A runaway elif is a dispatch table in disguise — extract the
        cases into a ``dict[key, handler]`` and the body becomes
        ``handler = table.get(key); return handler(...)``. Builder
        pattern when each branch is a step in a sequence.

    AP010  hardcoded absolute filesystem path outside config.py
        Paths like ``/var/log/...``, ``/usr/bin/...``, ``/etc/...``,
        ``/data/...`` embedded in production code are platform-specific
        defaults masquerading as constants. Declare them via
        ``config_knob(...)`` so operators can override without a source
        change. ``/tmp/...`` paths get special treatment — they're
        allowed only with a ``# noqa: S108`` already present.

    AP011  inline ``re.compile`` of an anchored identifier regex in a
        router / handler module
        A pattern like ``^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`` is an
        *identifier* regex — it belongs in ``skiff/validators.py`` as a
        named constant so every router references the same rule.
        Heuristic: starts with ``^[`` and contains a ``{N,M}`` length
        bound. Handler-local patterns (port ``\\d{1,5}``, env KV shape,
        URL path templates) don't match this heuristic and stay local.

    AP012  archaeological marker in a comment or docstring
        Release code comments describe WHY, not project history.
        Phrases like ``R17 — ``, ``F6 migration``, ``Phase 3``,
        ``Previously had CC ≈ 18``, ``was here but moved to`` pollute
        reader attention with past-version churn. `git log` owns that
        narrative. Fix: rephrase to the current-state invariant, or
        delete the line.

    AP013  bloated section heading inside a docstring
        "Design goals:", "Design properties:", "Migration path:",
        "Historical note:", "Rationale:", "Trade-offs:" as section
        headings inside a docstring are PR-review artifacts — the
        signal they carried (one specific non-obvious invariant)
        belongs in a single paragraph, not a labeled list. Fix:
        collapse to the one or two sentences a reader of the class /
        module actually needs.

    AP014  hardcoded policy literal in a comment or docstring outside
        ``skiff/config.py`` / ``skiff/_config/``
        Comments that spell out numeric policy values (``5/minute``,
        ``30/minute``, ``120 per hour``, ``8 hours``, ``16 characters``)
        in the prose become silent doc-drift when the real value in
        config.py or the TOML changes. The module referencing the knob
        should describe WHAT the knob is for, not restate its CURRENT
        VALUE. Fix: drop the number; point at the source of truth.
        Exempt: `skiff/config.py`, `skiff/_config/**`, doc tables that
        are auto-generated.

Run:

    python tools/lint_antipatterns.py           # scan skiff/
    python tools/lint_antipatterns.py --check   # CI mode (exit non-zero on hit)

Exits 0 with "ok: N files, 0 findings" when clean.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

# Modules from which bulky imports are OK — they're content catalogues,
# so callers typically only pull one or two names anyway, and expanding
# them to ``contract.responses.FooResponse`` hurts readability.
_BULK_IMPORT_EXEMPT = frozenset({
    "skiff.contract.responses",
    "skiff.contract.requests",
    "skiff.contract.errors",
    "skiff.secure",
    "skiff.routers",          # app.py aggregates every router module here
    "skiff",                  # "from skiff import config, auth, ..." is encouraged
})
_BULK_IMPORT_CAP = 6

# Objects whose attribute surface is defined by Starlette/FastAPI/Python
# exception classes — getattr with a default is a legitimate interop
# pattern for these, not a code smell.
_GETATTR_FRAMEWORK_TARGETS = frozenset({
    "route", "endpoint", "app", "request", "req",
    "websocket", "ws", "scope",
    "exc", "error", "err",
})

# Keyword-argument names that carry policy values. When one of these is
# passed a literal int/str/list in skiff/*.py (outside config.py), the
# value is a hardcoded default that should have been sourced from config.
# NOTE: deliberately excludes `title`, `description`, `summary`, `doc` —
# those are documentation strings, not policy values.
_POLICY_KWARGS = frozenset({
    "port", "host", "bind_host",
    "workers", "log_level", "log_format",
    "maxBytes", "max_bytes", "backupCount", "backup_count",
    "maxBackups", "max_backups",
    "timeout", "timeout_seconds",
    "allow_methods", "allow_headers", "allow_origins",
    "allow_credentials",
    "max_body_bytes", "max_size", "max_pool_size",
})

# Files/paths where policy literals are legitimate. Matched by suffix.
#   skiff/config.py            — the source of truth for every knob
#   skiff/contract/**          — declarative catalogues (errors, events,
#                                response schemas). ``description=`` /
#                                ``message=`` literals are the DATA.
#   tools/                     — one-off CLI helpers; no policy surface
_CONFIG_FILE_SUFFIXES = (
    "skiff/config.py",
)
_CONFIG_DIR_SUFFIXES = (
    "skiff/contract",
)

# Env vars whose reads are legitimate outside config.py (subprocess env
# pass-through for compose, etc.). Not policy values — we're just
# forwarding what the host process has.
_ENVIRON_PASSTHROUGH = frozenset({"PATH", "HOME", "SSH_AUTH_SOCK"})

# AP007: block-nesting ceiling. 3 = three nested scopes; a 4th triggers.
_NESTING_CEILING = 3

# AP008: isinstance-ladder threshold.
_ISINSTANCE_LADDER_MIN = 3

# AP009: long elif-chain threshold.
_ELIF_CHAIN_MIN = 5

# AP010: absolute path prefixes that signal platform-specific defaults.
# `/tmp/` is intentionally allowed (lots of legitimate socket/tempfile
# usage). Each prefix here is detected as a module-level or literal-arg
# string in non-config production code.
_ABS_PATH_PREFIXES = ("/var/", "/usr/", "/etc/", "/data/", "/opt/", "/root/")

# AP011: regex-as-identifier heuristic. Matches `^[...]` with a `{N,M}`
# length cap somewhere in the pattern — classic "Docker identifier"
# shape that belongs in validators.py.
_IDENTIFIER_REGEX_HINT = re.compile(r"^\^\[.+\{[0-9]+,[0-9]+\}")

# Files that legitimately define identifier patterns and can't reference
# validators (which imports from these modules, causing cycles).
_REGEX_ALLOWLIST_SUFFIXES = ("skiff/validators.py", "skiff/contract/errors.py")

# Rows in the code-quality-guide that document the AP0## rules themselves
# legitimately contain the marker shapes as examples. Matches any MD table
# row of the form `| **AP0##** | ... |`.
_CATALOGUE_ROW_RE = re.compile(r"\bAP\d{3}\b")


# AP014: policy-literal leaks in comments. Match N/minute, N/second,
# N/hour, "N per minute" etc. — the kind of rate-limit / window / size
# spec that should live ONCE in config.py or a TOML. Also catches long-
# enough numeric thresholds worded as English ("at least 16 characters",
# "8-hour absolute") where the number would drift if someone tuned the
# knob. The regex is narrow on purpose — `page 5` / `step 3` / `Python
# 3.12` in prose would false-positive if we matched bare ints.
_POLICY_WINDOW_WORDS = "absolute|idle|session|timeout|lifetime|lockout|window|grace"
_POLICY_LITERAL_PATTERNS = (
    # "5/minute", "120 / hour", "10 per second"
    re.compile(r"\b\d+\s*(?:/|\s+per\s+)(?:second|minute|hour|day)\b", re.IGNORECASE),
    # "at least 16 characters", "minimum 16 chars", "minimum 32 character"
    re.compile(r"\b(?:at least|minimum|min\.?|≥)\s+\d+\s+char(?:acter)?s?\b", re.IGNORECASE),
    # "N-hour absolute", "8 hour session", "15-minute idle"
    re.compile(
        r"\b\d+[- ](?:hour|minute|second|day)"
        r"(?:\s+(?:" + _POLICY_WINDOW_WORDS + r"))?\b",
        re.IGNORECASE,
    ),
    # "8 hours absolute", "5 minutes idle" tied to a config-knob window
    re.compile(
        r"\b\d+\s+(?:hours?|minutes?|seconds?|days?)\s+"
        r"(?:" + _POLICY_WINDOW_WORDS + r")\b",
        re.IGNORECASE,
    ),
)

# Files where hardcoded policy literals are legitimate — config.py is
# the source of truth; the TOMLs ARE the literals; tests assert specific
# values by design; auto-generated doc files restate the numbers.
_POLICY_LITERAL_EXEMPT_SUFFIXES = (
    "skiff/config.py",
    "skiff/_config/",
    "tests/",
    "docs/config-knobs.md",
    "docs/errors.md",
    "docs/audit-events.md",
    ".generated.md",
)


def _policy_literal_exempt(path: pathlib.Path) -> bool:
    """Files for which AP014 is not applicable."""
    p = str(path)
    return any(suf in p for suf in _POLICY_LITERAL_EXEMPT_SUFFIXES)


# AP012: archaeological markers in comments and docstrings. Each pattern
# is deliberately anchored on a word boundary so `R17` in a literal
# (e.g. status code 417) or a function name doesn't get swept up — only
# R / F refactor tags followed by a space, dash, colon, or em-dash.
_ARCHAEOLOGY_MARKERS = (
    re.compile(r"\b[RF]\d{1,3}\b[\s:—-]"),
    re.compile(r"\bPreviously\b", re.IGNORECASE),
    re.compile(r"\bMigrated (from|to|in)\b"),
    re.compile(r"\bused to (be|do|have)\b"),
    re.compile(r"\bwas here but moved\b"),
    re.compile(r"\bHistorical note\b"),
    re.compile(r"\bas of R\d"),
)
# "Phase N" is deliberately NOT in the marker list: legitimate pipeline
# documentation ("Phase 1: cache check; Phase 2: ping; Phase 3: rebuild")
# is indistinguishable from archaeological migration prose at the
# regex level. Reviewer judgement owns that distinction.

# AP013: docstring section headings that consistently indicate bloat.
_BLOAT_SECTIONS = (
    "Design goals:",
    "Design properties:",
    "Migration path:",
    "Historical note:",
    "Rationale:",
    "Trade-offs:",
)


_NESTING_BLOCK_TYPES = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.AsyncFor, ast.AsyncWith)


class _AntiPatternVisitor(ast.NodeVisitor):
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.findings: list[tuple[int, str, str]] = []
        self._try_depth = 0
        self._is_config_file = self._path_matches_config(path)

    @staticmethod
    def _path_matches_config(path: pathlib.Path) -> bool:
        """True when `path` is a config file or inside a data-catalogue dir."""
        s = path.as_posix()
        if any(s.endswith(cf) for cf in _CONFIG_FILE_SUFFIXES):
            return True
        return any(f"/{d}/" in f"/{s}" for d in _CONFIG_DIR_SUFFIXES)

    # ── AP001 / AP002 ──
    def visit_Try(self, node: ast.Try) -> None:
        self._try_depth += 1
        if self._try_depth >= 2:
            self.findings.append((
                node.lineno, "AP001", "nested try/except — extract a _quiet/_fallback helper",
            ))
        if self._try_body_branches_on_its_own_output(node):
            self.findings.append((
                node.lineno, "AP002",
                "try/except with if branching on a name the try assigned — "
                "split the success / fail axes into sequential blocks",
            ))
        self.generic_visit(node)
        self._try_depth -= 1

    @staticmethod
    def _try_body_branches_on_its_own_output(node: ast.Try) -> bool:
        """Flag only when the try body both assigns a name AND branches on it.

        This targets the real anti-pattern: "compute → check result → act".
        A gate-check like ``if revalidate_now: f()`` inside a try — where
        ``revalidate_now`` is a parameter or outer variable — is a
        legitimate conditional call, not the pattern we're worried about.
        """
        assigned: set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        assigned.add(tgt.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                assigned.add(stmt.target.id)
            elif isinstance(stmt, ast.If):
                for n in ast.walk(stmt.test):
                    if isinstance(n, ast.Name) and n.id in assigned:
                        return True
        return False

    # ── AP003 ──
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod in _BULK_IMPORT_EXEMPT:
            return
        if mod.startswith("skiff") and len(node.names) > _BULK_IMPORT_CAP:
            self.findings.append((
                node.lineno, "AP003",
                f"bulky import ({len(node.names)} names from {mod!r}) — "
                f"prefer `from skiff import X` + namespace access",
            ))

    # ── AP004 ──
    @staticmethod
    def _is_bad_getattr(node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
            return False
        if len(node.args) != 3:
            return False
        target, name_arg, default_arg = node.args[0], node.args[1], node.args[2]
        if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
            return False
        # `getattr(obj, "x", None)` is the legitimate optional-attr idiom.
        if isinstance(default_arg, ast.Constant) and default_arg.value is None:
            return False
        # Framework/exception interop: Starlette/FastAPI types expose
        # attributes depending on subclass — getattr-with-default is
        # the right call, not a smell.
        return not (isinstance(target, ast.Name) and target.id in _GETATTR_FRAMEWORK_TARGETS)

    # ── AP004 / AP005 / AP006 / AP011 — Call-site checks ──
    def visit_Call(self, node: ast.Call) -> None:
        if self._is_bad_getattr(node):
            self.findings.append((
                node.lineno, "AP004",
                "getattr(obj, 'literal', non-None default) — prefer a dict "
                "lookup or typed attribute",
            ))
        if not self._is_config_file:
            self._check_policy_kwargs(node)
            self._check_os_environ(node)
            if not self._is_regex_allowlisted():
                self._check_inline_identifier_regex(node)
        self.generic_visit(node)

    # ── AP011 ──
    def _is_regex_allowlisted(self) -> bool:
        s = self.path.as_posix()
        return any(s.endswith(p) for p in _REGEX_ALLOWLIST_SUFFIXES)

    def _check_inline_identifier_regex(self, node: ast.Call) -> None:
        """Flag ``re.compile/match/search/fullmatch(r"^[...]{N,M}...")``."""
        if not self._is_re_pattern_call(node):
            return
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            return
        if not _IDENTIFIER_REGEX_HINT.match(first.value):
            return
        self.findings.append((
            node.lineno, "AP011",
            f"inline identifier regex {first.value!r} — move to "
            f"skiff/validators.py as a named constant and reference it",
        ))

    @staticmethod
    def _is_re_pattern_call(node: ast.Call) -> bool:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr not in {"compile", "match", "search", "fullmatch"}:
            return False
        base = func.value
        return isinstance(base, ast.Name) and base.id == "re" and bool(node.args)

    # ── AP005 ──
    def _check_policy_kwargs(self, node: ast.Call) -> None:
        """Flag policy kwargs whose value is a literal int/str/list."""
        for kw in node.keywords:
            if kw.arg is None or kw.arg not in _POLICY_KWARGS:
                continue
            if self._is_literal_value(kw.value):
                self.findings.append((
                    kw.value.lineno, "AP005",
                    f"literal value for policy kwarg `{kw.arg}=` — "
                    f"reference `config.X` instead of embedding the default",
                ))

    @staticmethod
    def _is_literal_value(expr: ast.expr) -> bool:
        """True if `expr` is a literal int / str / list / tuple of literals."""
        if isinstance(expr, ast.Constant):
            return isinstance(expr.value, (int, str, bool))
        if isinstance(expr, (ast.List, ast.Tuple)):
            return all(
                isinstance(e, ast.Constant) and isinstance(e.value, (int, str, bool))
                for e in expr.elts
            )
        return False

    # ── AP006 ──
    def _check_os_environ(self, node: ast.Call) -> None:
        """Flag ``os.environ.get("LITERAL_NAME", …)`` outside config.py.

        Only fires when the env var name is a string literal — that's the
        "hardcoded knob" pattern. `os.environ.get(variable_name, ...)`
        (meta-reads like `_resolve_knob`) is allowed because the name
        comes from the config registry, not a baked-in literal.
        """
        if not self._is_os_environ_get(node):
            return
        if not (node.args and isinstance(node.args[0], ast.Constant)):
            return
        name = node.args[0].value
        if not isinstance(name, str):
            return
        if name in _ENVIRON_PASSTHROUGH:
            return
        self.findings.append((
            node.lineno, "AP006",
            f"os.environ.get({name!r}, ...) outside skiff/config.py — "
            f"declare this as a config_knob(...) so the default, validator, "
            f"and doc live in one place",
        ))

    @staticmethod
    def _is_os_environ_get(node: ast.Call) -> bool:
        """True when `node` is `os.environ.get(...)`."""
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            return False
        inner = func.value
        if not isinstance(inner, ast.Attribute) or inner.attr != "environ":
            return False
        return isinstance(inner.value, ast.Name) and inner.value.id == "os"

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """AP006 second form: os.environ['FOO']."""
        if self._is_config_file:
            self.generic_visit(node)
            return
        if self._is_os_environ_subscript(node):
            self.findings.append((
                node.lineno, "AP006",
                "os.environ[...] outside skiff/config.py — env vars belong to "
                "config_knob() so the default, validator, and doc live in one place",
            ))
        self.generic_visit(node)

    @staticmethod
    def _is_os_environ_subscript(node: ast.Subscript) -> bool:
        inner = node.value
        if not isinstance(inner, ast.Attribute) or inner.attr != "environ":
            return False
        return isinstance(inner.value, ast.Name) and inner.value.id == "os"

    # ── AP007 — excessive block nesting ──
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scan_function_for_nesting(node)
        self._scan_function_for_isinstance_ladder(node)
        self._scan_function_for_long_elif(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def _scan_function_for_nesting(self, node: ast.FunctionDef) -> None:
        """Walk `node.body`, report if any branch nests > _NESTING_CEILING."""
        worst = self._max_block_depth(node.body, depth=0)
        if worst > _NESTING_CEILING:
            self.findings.append((
                node.lineno, "AP007",
                f"function `{node.name}` nests blocks {worst} levels deep "
                f"(max {_NESTING_CEILING}) — extract helpers or collapse "
                f"with early returns",
            ))

    def _max_block_depth(self, stmts: list[ast.stmt], depth: int) -> int:
        worst = depth
        for stmt in stmts:
            if isinstance(stmt, _NESTING_BLOCK_TYPES):
                # Recurse into each branch's body and walk to its own depth.
                for branch in self._branches_of(stmt):
                    sub = self._max_block_depth(branch, depth + 1)
                    worst = max(worst, sub)
            else:
                for sub_list in self._inner_stmt_lists(stmt):
                    sub = self._max_block_depth(sub_list, depth)
                    worst = max(worst, sub)
        return worst

    @staticmethod
    def _branches_of(stmt: ast.stmt) -> list[list[ast.stmt]]:
        """Yield each `list[stmt]` branch attached to a block-forming stmt."""
        branches: list[list[ast.stmt]] = []
        if isinstance(stmt, (ast.If, ast.For, ast.While, ast.AsyncFor)):
            branches.append(stmt.body)
            branches.append(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            branches.append(stmt.body)
            branches.extend(h.body for h in stmt.handlers)
            branches.append(stmt.orelse)
            branches.append(stmt.finalbody)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            branches.append(stmt.body)
        return [b for b in branches if b]

    @staticmethod
    def _inner_stmt_lists(_stmt: ast.stmt) -> list[list[ast.stmt]]:
        return []  # non-block stmts — no inner lists that count toward nesting

    # ── AP008 — isinstance-ladder ──
    def _scan_function_for_isinstance_ladder(self, node: ast.FunctionDef) -> None:
        for n in ast.walk(node):
            if isinstance(n, ast.If) and self._is_isinstance_ladder_head(n):
                self.findings.append((
                    n.lineno, "AP008",
                    f"isinstance ladder with {self._isinstance_ladder_length(n)} "
                    f"arms in `{node.name}` — consider a Pydantic discriminated "
                    f"union or a dict-based factory",
                ))

    @staticmethod
    def _is_isinstance_call(expr: ast.expr) -> bool:
        return (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "isinstance"
        )

    def _isinstance_ladder_length(self, node: ast.If) -> int:
        length = 0
        cur: ast.stmt | None = node
        while isinstance(cur, ast.If) and self._is_isinstance_call(cur.test):
            length += 1
            # Walk into the elif (a single-If in orelse).
            cur = cur.orelse[0] if (len(cur.orelse) == 1) else None
        return length

    def _is_isinstance_ladder_head(self, node: ast.If) -> bool:
        """Return True only for the FIRST If in a ladder, to avoid dup reports."""
        if not self._is_isinstance_call(node.test):
            return False
        return self._isinstance_ladder_length(node) >= _ISINSTANCE_LADDER_MIN

    # ── AP009 — long elif chain ──
    def _scan_function_for_long_elif(self, node: ast.FunctionDef) -> None:
        for n in ast.walk(node):
            if isinstance(n, ast.If):
                length = self._if_chain_length(n)
                if length >= _ELIF_CHAIN_MIN and not self._is_chain_continuation(node, n):
                    self.findings.append((
                        n.lineno, "AP009",
                        f"if/elif chain with {length} branches in "
                        f"`{node.name}` — extract a dispatch table "
                        f"(dict[key, handler]) or a builder sequence",
                    ))

    @staticmethod
    def _if_chain_length(node: ast.If) -> int:
        length = 1
        cur = node
        while len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
            cur = cur.orelse[0]
            length += 1
        return length

    def _is_chain_continuation(self, fn: ast.FunctionDef, target: ast.If) -> bool:
        """True when `target` is the `elif` tail of another If in the same fn."""
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.If) and n is not target
                and len(n.orelse) == 1 and n.orelse[0] is target
            ):
                return True
        return False

    # ── AP010 — hardcoded absolute filesystem path ──
    def visit_Constant(self, node: ast.Constant) -> None:
        if self._is_config_file:
            return
        if not isinstance(node.value, str):
            return
        val = node.value
        if not val.startswith(_ABS_PATH_PREFIXES):
            return
        # Allow when the line already carries a security-waiver suppression.
        self.findings.append((
            node.lineno, "AP010",
            f"hardcoded absolute path {val!r} — move to config_knob() so "
            f"operators can override without a source change",
        ))


def _scan_comment_archaeology(text: str, path: pathlib.Path) -> list[tuple[int, str, str]]:
    """AP012 + AP014 on `#` comments.

    Walks line-by-line so a variable or constant named `R12` in code
    isn't confused with a comment tag. Docstring hits are caught
    separately from the AST walk since they're multi-line.
    """
    findings: list[tuple[int, str, str]] = []
    scan_ap014 = not _policy_literal_exempt(path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        comment = stripped.lstrip("#").strip()
        # AP012 — archaeology.
        hit_012 = False
        for rx in _ARCHAEOLOGY_MARKERS:
            m = rx.search(comment)
            if m:
                findings.append((
                    lineno, "AP012",
                    f"archaeological marker {m.group(0)!r} in comment — "
                    f"describe the current state or delete the line",
                ))
                hit_012 = True
                break
        if hit_012 or not scan_ap014:
            continue
        # AP014 — hardcoded policy literal.
        for rx in _POLICY_LITERAL_PATTERNS:
            m = rx.search(comment)
            if m:
                findings.append((
                    lineno, "AP014",
                    f"hardcoded policy literal {m.group(0)!r} in comment — "
                    f"point at config.py / rate.toml instead so the doc "
                    f"can't rot when the knob is tuned",
                ))
                break
    return findings


def _docstring_ap012(doc: str, lineno: int) -> tuple[int, str, str] | None:
    """AP012 — archaeological markers in a docstring, or None."""
    for rx in _ARCHAEOLOGY_MARKERS:
        m = rx.search(doc)
        if m:
            return (
                lineno, "AP012",
                f"archaeological marker {m.group(0)!r} in docstring — "
                f"describe the current behaviour or delete the phrase",
            )
    return None


def _docstring_ap013(doc: str, lineno: int) -> tuple[int, str, str] | None:
    """AP013 — bloat section heading in a docstring, or None."""
    for section in _BLOAT_SECTIONS:
        if section in doc:
            return (
                lineno, "AP013",
                f"docstring contains bloat section {section!r} — "
                f"collapse to a single invariant sentence",
            )
    return None


def _docstring_ap014(doc: str, lineno: int) -> tuple[int, str, str] | None:
    """AP014 — hardcoded policy literal in a docstring, or None."""
    for rx in _POLICY_LITERAL_PATTERNS:
        m = rx.search(doc)
        if m:
            return (
                lineno, "AP014",
                f"hardcoded policy literal {m.group(0)!r} in docstring — "
                f"point at config.py / _config/*.toml instead so the "
                f"doc can't rot when the knob is tuned",
            )
    return None


def _scan_docstring_smells(tree: ast.AST, lines: list[str], path: pathlib.Path) -> list[tuple[int, str, str]]:
    """AP012 + AP013 + AP014 on docstrings.

    `ast.get_docstring` only recognises module/class/function docstrings,
    which is the set we care about — triple-quoted string *constants*
    elsewhere in the code aren't user-facing docs.
    """
    findings: list[tuple[int, str, str]] = []
    checks = [_docstring_ap012, _docstring_ap013]
    if not _policy_literal_exempt(path):
        checks.append(_docstring_ap014)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        lineno = getattr(node, "lineno", 1)
        for check in checks:
            finding = check(doc, lineno)
            if finding:
                findings.append(finding)
    return findings


def _scan(path: pathlib.Path) -> list[tuple[pathlib.Path, int, str, str]]:
    try:
        text = path.read_text()
        tree = ast.parse(text)
    except (SyntaxError, OSError):
        return []
    visitor = _AntiPatternVisitor(path)
    visitor.visit(tree)
    all_findings = list(visitor.findings)
    # Comment + docstring smells are gated on non-exempt files only,
    # so catalogue modules / tools keep their reference wording.
    if not visitor._is_config_file:
        all_findings.extend(_scan_comment_archaeology(text, path))
        all_findings.extend(_scan_docstring_smells(tree, text.splitlines(), path))
    return [(path, *finding) for finding in all_findings]


def _iter_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        p for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _iter_text_files_for_archaeology(root: pathlib.Path) -> list[pathlib.Path]:
    """Return browser-visible and operator-facing text files in scope for
    the AP012 plain-text sweep.

    The AST linter covers Python; the other file types don't have AST
    passes here but can still leak archaeological markers (`R17`, `F7`,
    `Phase 4`) into output an outside reviewer will see:
      - `.js`   — shipped to browsers, visible in DevTools Sources.
      - `.md`   — the published documentation set.
      - `.html` — the main app shell, visible in DevTools Elements.
      - `.css`  — the stylesheet, visible in DevTools Sources.
      - `.toml` — config files an operator reads to tune behaviour.
    """
    patterns = ("*.js", "*.md", "*.html", "*.css", "*.toml")
    seen: set[pathlib.Path] = set()
    for pat in patterns:
        for p in root.rglob(pat):
            if any(skip in p.parts for skip in (".git", "__pycache__", "node_modules", ".venv")):
                continue
            seen.add(p)
    return sorted(seen)


def _scan_archaeology_text(path: pathlib.Path) -> list[tuple[pathlib.Path, int, str, str]]:
    """Plain-text AP012 scan for JS / Markdown files.

    Deliberately simple — walks each line, applies the AP012 regex set
    that the AST scanner uses on Python comments. The one subtlety is
    the doc file `docs/dev/code-quality-guide.md` itself, which *must*
    reference the marker shapes to document them; its AP012 marker
    citations appear inside a table row headed by `AP012` so we skip
    any line that starts with `|` and contains `AP012`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[tuple[pathlib.Path, int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Skip rule-catalogue rows: these legitimately name the marker
        # shapes as part of documenting the linter itself.
        if stripped.startswith("|") and _CATALOGUE_ROW_RE.search(stripped):
            continue
        for rx in _ARCHAEOLOGY_MARKERS:
            if rx.search(line):
                out.append((
                    path, lineno, "AP012",
                    f"archaeological marker in {path.suffix[1:]} file — "
                    "describe the current state or delete the line",
                ))
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", default=["skiff"],
        help="Files or directories to scan (default: skiff/).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="CI mode: exit non-zero if any findings.",
    )
    args = parser.parse_args()

    files: list[pathlib.Path] = []
    text_files: list[pathlib.Path] = []
    for raw in args.paths:
        p = pathlib.Path(raw)
        if p.is_dir():
            files.extend(_iter_python_files(p))
            text_files.extend(_iter_text_files_for_archaeology(p))
        else:
            files.append(p)

    findings: list[tuple[pathlib.Path, int, str, str]] = []
    for f in files:
        findings.extend(_scan(f))
    for f in text_files:
        findings.extend(_scan_archaeology_text(f))

    for path, line, code, msg in findings:
        print(f"{path}:{line}: {code} {msg}")

    summary = f"{len(files)} files, {len(findings)} findings"
    if findings:
        print(f"FAIL: {summary}", file=sys.stderr)
        return 1 if args.check else 0
    print(f"ok: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
