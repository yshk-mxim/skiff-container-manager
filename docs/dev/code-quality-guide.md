# Code Quality Guide

This document defines code quality standards for the SKIFF Container Manager project.

---

## Automated Enforcement

### Ruff

All code must pass `make lint` before merging. The project uses ruff for linting, formatting, and security scanning.

```bash
make lint        # ruff check skiff/ app.py tests/ tools/
make format      # ruff format + auto-fix
make security    # ruff --select S security rules
make complexity  # cyclomatic complexity check
```

### Key rules enforced

| Rule | Description |
|---|---|
| `I` | Sorted imports |
| `E`, `W` | pycodestyle style issues |
| `F` | pyflakes (undefined names, unused imports) |
| `B` | flake8-bugbear (common bugs and design issues) |

---

## Standards

### Exception chaining

All `raise` inside an `except` block must chain the original exception:

```python
# Good
except SomeError as exc:
    raise HTTPException(400, "message") from exc

# Bad — loses the original traceback
except SomeError:
    raise HTTPException(400, "message")
```

### `startswith` / `endswith` with multiple prefixes

Use a tuple instead of chaining with `or`:

```python
# Good
if path.startswith(("/", "~", "..", "$")):
    ...

# Bad
if path.startswith("/") or path.startswith("~") or path.startswith(".."):
    ...
```

### No silent exception swallowing

Every `except` block must either log, re-raise, or have a documented justification for why `pass` is acceptable (e.g., best-effort cleanup in `finally`):

```python
# Good — cleanup, documented
finally:
    try:
        client.close()
    except Exception:
        pass  # best-effort; errors here are not actionable

# Bad — silently drops errors in a code path that matters
try:
    save_data()
except Exception:
    pass
```

### No magic numbers

Use named constants for non-obvious numeric values:

```python
# Good
MAX_COMPOSE_SIZE = 1024 * 256  # 256 KB
MAX_LOG_TAIL = 5000

# Bad
if len(content) > 262144:
    ...
```

### Named volumes only

Host path mounts are never allowed — neither in the compose validator nor in the run-container endpoint. All volume sources must be named Docker volumes.

---

## Complexity Thresholds

| Metric | Threshold |
|---|---|
| Cyclomatic complexity (McCabe) | ≤ 18 (ruff default; aim for ≤ 10) |
| Function length | ≤ 60 lines |
| Nesting depth | ≤ 4 levels |

The `validate_compose_file` and `run_container` functions are known to exceed the ideal complexity threshold due to the number of security validation branches required. These are acceptable exceptions; new functions should stay within the thresholds.

---

## Security Standards

- **Registry validation** must happen on all image inputs before any Docker call.
- **Input validation** must happen before any filesystem or subprocess operation.
- **Subprocess calls** must use an explicit, minimal environment (`PATH`, `DOCKER_HOST`, `HOME`, `SSH_AUTH_SOCK`) — never inherit the full environment.
- **Auth checks** must use constant-time comparison (`hmac.compare_digest`) for token comparison.

### Documented Security Controls

These are intentional, non-obvious security decisions. Do not remove or weaken them without a documented justification.

| Control | Location | Reason |
|---|---|---|
| Registry allowlist case-insensitive match | `skiff/validators.py:validate_image_registry` | Prevent `DOCKER.IO` bypass of `docker.io` allowlist entry |
| Compose sandbox: `ipc: host` and `ipc: shareable` blocked | `skiff/validators.py:BLOCKED_IPC_MODES` | `shareable` allows containers to read/write each other's IPC namespace |
| Compose sandbox: host path mounts blocked | `skiff/validators.py:validate_compose_file` | Prevents access to host filesystem via bind-mount |
| Setup endpoint lockout (3 failures → 429 for 300 s) | `skiff/routers/system.py:_setup_fail` | Mirror of WS auth lockout; prevents insider token-fishing on a running instance |
| Setup-state minimal response when configured | `skiff/routers/system.py:setup_state` | Avoid leaking tunnel socket paths to unauthenticated callers on live server |
| Audit log read: 5 req/min; download: 2 req/min | `skiff/routers/system.py` | Prevent high-volume log scraping by a compromised session |
| DOCKER_HOST HTTP guard (non-localhost) | `skiff/app.py` lifespan | Warn when Docker API is exposed unencrypted over network |
| WebSocket close 4003 → no reconnect | `skiff/static/app.js` | Session expiry during live WS must not auto-reconnect (would use stale token) |
| SSH tunnel credentials cleared from sessionStorage after use | `skiff/static/app.js:swConnectTunnel` | `tunnelUser`/`tunnelHost` removed once tunnel is established; no need to retain |
| WS input size: `len(data.encode()) > 65536` | `skiff/routers/containers.py` | Byte length, not character count; 65536 UTF-8 chars = up to 256 KB |
| Token input in setup wizard: `type="password"` | `skiff/static/app.js` | Prevents token appearing in clipboard history and screenshots |

---

## Supply Chain Policy

- **Hash-pinned requirements**: `requirements.txt` is generated with `pip-compile --generate-hashes`. Do not edit it manually. To update deps, run `make deps` (regenerates with hashes).
- **Dependabot PRs**: Dependency update PRs are auto-created weekly. Review and merge at the maintainer's earliest opportunity for non-breaking updates; security-relevant bumps take priority over cosmetic ones.
- **Approved upgrade process**: For major version bumps, run the full test suite and check the package changelog for breaking changes before merging.
- **No `yaml.load`**: The compose validator must use `yaml.safe_load` only. Any `yaml.load(` call without `Loader=yaml.SafeLoader` is a critical finding.

## Security Scan Requirements

Every PR must pass:

```bash
make security    # ruff --select S security rules — zero warnings required
pip-audit --strict -r requirements.txt   # no known CVEs
```

The GitHub Actions CI workflow runs both automatically. A PR with failing security checks must not be merged.

## Browser Security Conventions

- **sessionStorage only** — never write to `localStorage`. The API token must live in `sessionStorage` so it is cleared when the tab closes.
- **No hardcoded secrets** — no tokens, no credentials, no API keys in JavaScript source.
- **Token lifecycle** — the token is stored in `sessionStorage.api_token`. It is cleared on 401, idle timeout, absolute timeout, logout, and tab close. Do not persist it beyond these boundaries.
- **Input escaping** — all dynamic HTML must be constructed through the `esc()` helper or `textContent` assignment. Never use `innerHTML` with unsanitised input.

---

## Project-specific anti-patterns (AP001–AP014)

Enforced automatically by `make lint-antipatterns` (runs
`tools/lint_antipatterns.py`) on every CI build and every pre-commit.
The rules encode the lessons from the v3 architecture review:

| Code | Anti-pattern | Fix |
|---|---|---|
| **AP001** | Nested `try/except` — one `try` inside another's body | Extract a `_quiet` / `_fallback` helper that wraps the inner cleanup with `contextlib.suppress(...)`, so each handler has exactly one responsibility. |
| **AP002** | `try/except` with an `if` inside the body that branches on a name the `try` assigned | Split the success / fail axes into sequential blocks — one `try` per failure mode, the `if` after it. Don't mix "did it succeed?" with "is the result good?" in one block. |
| **AP003** | Bulky (`> 6` names) `from skiff.X import (A, B, C, …)` block | Prefer `from skiff import X` + namespaced access (`X.A`). Exempt: `skiff.contract.*`, `skiff.routers` (router aggregator in `app.py`). |
| **AP004** | `getattr(obj, "literal", non-None default)` on a non-framework target | Use a dict lookup or typed attribute. `getattr(obj, "x", None)` stays — that's the idiomatic "optional attribute" form. Framework targets (`route`, `endpoint`, `app`, `request`, `websocket`, `scope`, `exc`) are exempt because Starlette/FastAPI types legitimately vary their attribute surface by subclass. |
| **AP005** | Hardcoded literal (int / str / list) passed to a policy kwarg (`port`, `host`, `workers`, `log_level`, `maxBytes`, `backupCount`, `timeout`, `allow_methods`, `allow_headers`, `allow_origins`, `max_body_bytes`, …) outside `skiff/config.py` | Declare a named constant or `config_knob(...)` in `skiff/config.py`; reference it at the call site. Documentation kwargs (`title`, `description`, `summary`, `doc`) are NOT policy and stay inline. |
| **AP006** | `os.environ.get("LITERAL_NAME", …)` outside `skiff/config.py` | Declare the env var as `config_knob(NAME, default=..., validator=..., doc=...)` in config. Meta-reads with a variable name (`os.environ.get(knob_name, ...)`) are allowed — those read the registry, not a baked-in value. System env pass-through (`PATH`, `HOME`, `SSH_AUTH_SOCK`) for subprocess spawning is exempt. |
| **AP007** | Excessive block nesting (function nests > 3 levels of `if` / `for` / `while` / `try` / `with`) | Extract inner blocks into named helpers or collapse with early returns. 2⁴ = 16 branch paths is where example-based tests stop being able to cover the space. |
| **AP008** | isinstance ladder (≥ 3 sequential `isinstance(x, T)` branches) | Manual type parsing is exactly what Pydantic is for. Replace with a discriminated union (`model_config = ConfigDict(extra="forbid")` + `Field(discriminator="type")`) or a dict-based factory `{cls: handler}`. |
| **AP009** | Long `if/elif` chain (≥ 5 branches) | The runaway elif is a dispatch table in disguise. Extract the cases into `TABLE: dict[key, handler]` and collapse the body to `handler = TABLE[key]; return handler(...)`. Use the builder pattern when the branches are sequential *steps*, not independent dispatch arms. |
| **AP010** | Hardcoded absolute filesystem path (`/var/...`, `/usr/...`, `/etc/...`, `/data/...`, `/opt/...`, `/root/...`) outside `skiff/config.py` | Declare a named constant or `config_knob(...)` in config. These paths are platform-specific defaults masquerading as constants — `/usr/bin/docker` breaks on macOS; `/var/log/...` requires root. `/tmp/...` is not flagged (legitimate ephemeral socket / tempfile usage) but still benefits from a named constant. |
| **AP011** | Inline `re.compile` / `re.match` of an anchored identifier regex (`^[...]...{N,M}...$`) outside `skiff/validators.py` | Identifier patterns (container IDs, volume names, image tags, labels) live in `skiff/validators.py` as named constants so every router references the same rule. Handler-local patterns (e.g. port `\d{1,5}`, env `KEY=VALUE`, URL path templates) don't match the heuristic and stay inline. |
| **AP012** | Archaeological marker (`R17`, `F6 migration`, `Previously`, `was here but moved`, `Migrated from`, `Historical note`, `as of R22`) in a `#` comment or docstring | Release comments describe WHY / what-is-true-now, not project history. `git log` owns the past. Fix: rephrase as the current-state invariant, or delete the line. `Phase N` is deliberately NOT flagged because pipeline docs ("Phase 1: cache; Phase 2: ping; Phase 3: rebuild") are a legitimate use. |
| **AP013** | Bloat section heading inside a docstring (`Design goals:`, `Design properties:`, `Migration path:`, `Historical note:`, `Rationale:`, `Trade-offs:`) | These are PR-review narratives. The one non-obvious invariant each section carried belongs in a single paragraph, not a labelled list. |

### Related ruff rule families (also enforced)

- `C901` — cyclomatic complexity ≤ 10 hard ceiling; aim for ≤ 5 preferred, ≤ 3 default.
- `TRY` — tryceratops: proper exception construction, `raise ... from exc` hygiene.
- `PTH` — prefer `pathlib.Path` over `os.path`.
- `DTZ` — timezone-aware datetimes only.
- `FURB` — refurb: modernize legacy idioms.
- `PYI` — stub / type conventions (e.g. `__enter__ → Self`).

### Complexity policy recap

- **CC ≤ 3** — default. 2³ = 8 branch paths; exhaustively testable by example.
- **CC ≤ 5** — preferred. Senior review + Hypothesis property tests cover the space.
- **CC ≤ 10** — hard ceiling (enforced by `C901`). Above this is a combinatorial explosion no example-based suite can cover — functions hitting the ceiling must be decomposed, or rely on Pydantic validation at the boundary to shrink the test surface ("accurate by design").

### Pydantic / builder decision tree for CC > 3

Ask, in order:

1. **Is it input validation?** → make it a Pydantic `BaseModel` with `ConfigDict(extra="forbid")` + `@field_validator`. The boundary check replaces a hand-written validator; FastAPI gets the OpenAPI schema for free.
2. **Is it a sequence of independent steps that can fail + recover?** → builder pattern. Each step is a method returning `Self`; the caller composes `.step_a().step_b().commit()`. Example: `_TunnelBuilder` in `skiff/docker_client.py`.
3. **Is it a dispatch on a discrete key?** → table lookup (dict literal at module level) + a single `_apply` function. Example: `_UPDATE_KWARG_TO_HOSTCONFIG` in `skiff/routers/containers.py`.
4. **Otherwise** — decompose into named helpers, one per rule. Each helper is its own `def`, each has a test, and the main function becomes a pipeline.

### Running the linter locally

```bash
make lint                  # ruff (includes C901, PTH, DTZ, FURB, PYI, TRY, …)
make lint-antipatterns     # AP001–AP014 project rules
make security              # ruff --select S security ruleset
make ci                    # everything above + unit tests
```

A failing rule can be suppressed *at the point of use* with an inline
`# noqa: APNNN` comment — but prefer refactoring over suppression.
Unjustified `noqa` comments are themselves a smell.

---

## Checklist for PRs

- [ ] `make lint` passes with no warnings.
- [ ] `make security` passes with no warnings.
- [ ] `make test-unit` passes.
- [ ] All `raise` in `except` blocks use `from exc`.
- [ ] No new magic numbers without named constants.
- [ ] Registry allowlist enforced on any new image input.
- [ ] Host path detection present on any new volume/mount input.
- [ ] New endpoints documented in `docs/api-reference.md`.
- [ ] New env vars documented in `README.md` and `docs/deployment.md`.
- [ ] `CHANGELOG.md` updated under `[Unreleased]`.
