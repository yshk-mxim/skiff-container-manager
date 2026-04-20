# Claude-review checklist

This file is loaded by `.github/workflows/security.yml` as the
review-instructions block. When reviewing a PR Claude walks this list
and flags any rule broken. Keep it tight — one-line invariants, no
exposition. Cross-references in parentheses point at the enforcing
test file (if any) so the reviewer knows the rule is not subjective.

## Zero-trust invariants

- No raw `API_TOKEN` value in any log line. Only last-N-character
  suffixes are acceptable.
- No environment variable value leaked to the client in any response.
  `/api/config` surfaces only `expose=True` knobs from `_KNOBS`.
- Every mutating `/api/*` route carries CSRF protection (enforced by
  `tests/test_route_contract.py`).
- Every unauthenticated `/api/*` route is in `_PUBLIC_ROUTES` with an
  inline justification comment.
- No `eval`, `exec`, `yaml.load` (must use `yaml.safe_load`), `pickle`.
- Subprocess calls never spread a user-controlled string into `shell=True`.
- Path-manipulation code uses `Path.resolve()` + `is_relative_to(base)`
  before reading/writing.

## Router invariants

- New HTTP handler is decorated with `@secure_route.{mutate,read,public}`
  from `skiff/secure.py`.
- New handler's `audit=...` event name is declared in
  `skiff/contract/events.py` (enforced by `tests/test_contract.py`).
- Handler returns `OkResponse` / `UndoableResponse` / a dict that
  intentionally exposes specific fields — not a random `{"ok": True}`.
- Error paths raise `http_error("<domain>.<code>")` from the catalogue
  rather than `HTTPException(400, "string")`.
- `tags=[...]` is present on every route (enforced by
  `tests/test_route_contract.py`).

## Test invariants

- New unit test uses `tests/factories.py` builders (`make_container`
  etc.) not a hand-rolled MagicMock.
- New e2e test imports from `tests/e2e_helpers.py` (`login`, `nav_to`,
  ...) rather than duplicating.
- New Hypothesis fuzz uses `tests/strategies.py` builders where an
  existing one fits.
- Audit-emission assertion uses `tests/audit.py::assert_audit_event`.

## UI invariants

- No `document.createElement` outside `skiff/static/ui.js` (enforced
  by pattern via code review; later a CodeQL query can automate).
- No `innerHTML =` outside the explicit `html:` branch of `UI.el()`.
  Specifically: `innerHTML = '<tag>' + someVar + '</tag>'` (string
  concatenation or template literal with a user-sourced value) is a
  blocking XSS finding — use `textContent` or `UI.el({text: ...})`.
- No `localStorage` usage. `sessionStorage` only.
- New page calls `UI.registerPage({...})` exactly once, declaring its
  personas.
- New user-facing string appears in `docs/dev/copy.md`.

## Governance invariants

- No references to internal-only systems or identifiers (maintainers'
  private risk registers, company-internal tooling, etc.) in any
  tracked file. The actual denylist is supplied at CI time via the
  `INTERNAL_DENYLIST_REGEX` repository secret — the pattern is never
  committed to the repo.
- No customer / tenant identifiers in test fixtures or sample data.
  Hardcoded domains in examples: `example.com`, `example.org` only.

## Config invariants

- New env var flows through `config_knob(name, default, validator,
  doc, expose, secret)`. No bare `os.environ.get(...)` in production
  code paths added by the PR.
- `.env.example` / README config table updated if `expose=True`.

## Supply chain

- New dependency is added via `pyproject.toml` + `pip-compile` so
  `requirements.txt` gets regenerated with hash pins.
- Major-version bumps include a one-line changelog note.
- GitHub Actions pinned to commit SHA, not `@v<major>` tags, when
  the action is third-party (Dependabot handles updates).

## Documentation

- PR touches a feature → `docs/features/<name>.md` updated or added
  via `docs/dev/feature-template.md`.
- New audit event → add to `docs/features/` and run the
  `tests/test_contract.py` drift test locally.
- Breaking API change → add a note in `docs/api-reference.md`.
