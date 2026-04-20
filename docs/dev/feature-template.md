# Feature documentation template

Every user-visible feature should have a one-page entry in
`docs/features/<name>.md` following the structure below. The template
exists to keep PRs consistent — reviewers check that each section is
filled or explicitly marked N/A; readers learn the feature by scanning
the same seven headings every time.

Copy the skeleton below into a new file and replace the `<angle-bracket placeholders>`.

---

```markdown
# Feature: <name>

## What it is
One-paragraph description of the feature. Plain English — what the user
can do that they couldn't before.

## Who it's for
Pick one primary persona: `homelab`, `dev`, `sre`, `reviewer`, `tutor`,
`ci`. Note any secondary personas the feature also serves.

## UI flow
Screenshot path (or "N/A — backend only"). Verbatim copy of every
user-facing string the feature introduces — save these into
`../dev/copy.md` too so i18n is a mechanical later step.

Key interactions:
1. User clicks X.
2. UI does Y.
3. Error case: if Z fails, toast says "...".

## API surface
| Method | Path | Request | Response | Error codes |
|---|---|---|---|---|
| POST | /api/... | JSON body | `OkResponse` | `validation.bad_input`, `domain.not_found` |

## Security model
- Decorator: `@secure_route.mutate(RATE.WRITE, audit="domain.action")`
- CSRF: enforced by decorator.
- Rate limit: RATE.WRITE (30/minute by default).
- Audit events: `domain.action` (declared in `skiff/contract/events.py`).
- Threat model: what an attacker could do if this endpoint is broken —
  inform the blast radius.

## Tests
- Unit: `tests/test_<feature>.py` — factories from `tests/factories.py`.
- Property: `tests/test_fuzz.py` or `tests/test_hypothesis_expansion.py`
  (add a fuzzer if the feature parses user strings).
- E2E: `tests/test_e2e_ui.py::test_<feature>` or a new file — use
  helpers from `tests/e2e_helpers.py`.
- Route contract: covered automatically by
  `tests/test_route_contract.py` — no per-feature addition needed.

## Troubleshooting
- "<user-visible error message>" → likely cause + fix.
- Audit-log search: `grep '"event": "domain.action"'
  ~/Library/Application\ Support/skiff/audit.jsonl`.

## References
- Storyboards: `../dev/storyboards.md §<section>` if applicable.
- Production hardening: `../hardening/production.md §<section>`.
```

---

## Required PR checklist

When a PR adds or changes a feature, the author should check:

- [ ] `docs/features/<name>.md` is added or updated.
- [ ] Any new audit event is declared in `skiff/contract/events.py`.
- [ ] Any new error code is declared in `skiff/contract/errors.py`.
- [ ] Any new env knob is registered via `config_knob(...)`.
- [ ] A new user-facing string is also recorded in `../dev/copy.md`.

Drift tests in `tests/test_contract.py` and `tests/test_route_contract.py`
catch the code-side half of this automatically; the docs half is by
convention.
