# Code Quality Guide

This document defines code quality standards for the SKIFF Container Manager project.

---

## Automated Enforcement

### Ruff

All code must pass `make lint` before merging. The project uses ruff for linting, formatting, and security scanning.

```bash
make lint        # ruff check skiff/ app.py tests/
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
- **Dependabot PRs**: Dependency update PRs are auto-created weekly. Review and merge within 5 business days for non-breaking updates.
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
