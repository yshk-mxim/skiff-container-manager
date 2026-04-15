# Code Quality Guide

This document defines code quality standards for the SKIFF Container Manager project.

---

## Automated Enforcement

### Ruff

All code must pass `make lint` before merging. The project uses ruff for linting, formatting, and security scanning.

```bash
make lint        # ruff check skiff/ app.py tests/
make format      # ruff format + auto-fix
make security    # bandit-equivalent security rules (S rules)
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

---

## Checklist for PRs

- [ ] `make lint` passes with no warnings.
- [ ] `make security` passes with no warnings.
- [ ] `make test-unit` passes (142 tests).
- [ ] All `raise` in `except` blocks use `from exc`.
- [ ] No new magic numbers without named constants.
- [ ] Registry allowlist enforced on any new image input.
- [ ] Host path detection present on any new volume/mount input.
- [ ] New endpoints documented in `docs/api-reference.md`.
- [ ] New env vars documented in `README.md` and `docs/deployment.md`.
- [ ] `CHANGELOG.md` updated under `[Unreleased]`.
