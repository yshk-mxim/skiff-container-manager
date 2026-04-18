# Adding a Feature to SKIFF — End-to-End

This guide walks through the full lifecycle of adding a new API endpoint
and UI affordance. The repo already has patterns for each layer; follow
them instead of inventing new ones.

---

## 1. Decide where the feature fits

| The feature is… | Add it to |
|---|---|
| A new container-lifecycle operation | `skiff/routers/containers.py` |
| A new image-level operation | `skiff/routers/images.py` |
| A new volume or network op | `skiff/routers/volumes.py` / `networks.py` |
| Compose-related | `skiff/routers/compose.py` |
| Auth/session/config | `skiff/routers/system.py` (auth endpoints currently live there) |
| Cross-cutting (metrics, audit log browsing) | `skiff/routers/system.py` |

If the feature is big enough to warrant a new router module, wire it up in
`skiff/app.py` with the other `app.include_router(...)` calls.

---

## 2. Endpoint template (checklist)

Every route in SKIFF goes through `@secure_route.mutate(...)` or
`@secure_route.read(...)` (or `.public(...)` for unauthenticated health /
discovery endpoints). The decorator bundles the four things every route
needs — rate-limiting, CSRF check, audit-log emission, and (for
mutations) structured audit fields — so the handler body stays focused on
the operation. Look at `skiff/routers/volumes.py` for a short canonical
example.

Most existing endpoints take their parameters as query/path args
(look at `skiff/routers/volumes.py::create_volume` for the canonical
example — `name: str` as a query param). Only a small subset with
rich structured input (container run, compose upload) use Pydantic
request bodies. Both shapes are fine; pick the lighter one unless
you genuinely need nested / typed / unioned fields.

```python
# Shape A — query/path params (most routes). Short, mechanical, the
# canonical pattern in skiff/routers/volumes.py and similar.
@router.post("/api/<resource>/<id>/<action>", dependencies=AUTH, tags=["<resource>"])
@secure_route.mutate(RATE.WRITE, audit="<resource>.<action>")
def my_action(
    request: Request,
    id: str,
    name: str,           # query param validated by a shared regex
    client=Depends(docker_client_dep),
) -> OkResponse:
    """<one-line summary>. <why this exists>."""
    # 1. Validate identifiers BEFORE any Docker call.
    validators.validate_container_id(id)         # shape from skiff/validators.py
    validators.validate_container_name(name)     # or volume / project / …

    # 2. Fetch the resource.
    container = _get_container(client, id)       # or _get_volume / _get_network

    # 3. Perform the operation via safe_docker_call (maps Docker errors).
    result = safe_docker_call(container.my_action, name=name)

    # 4. Return a typed response; OkResponse trims None fields.
    return OkResponse(id=container.short_id, name=result.name)


# Shape B — Pydantic request body (when you genuinely need nested /
# union / optional-heavy input). See skiff/routers/containers.py::run
# for the canonical example, backed by ContainerRunRequest in
# skiff/contract/requests.py.
@router.post("/api/<resource>/run", dependencies=AUTH, tags=["<resource>"])
@secure_route.mutate(RATE.WRITE, audit="<resource>.run")
def run(
    request: Request,
    body: MyActionRequest,                       # extra="forbid" at the model
    client=Depends(docker_client_dep),
) -> OkResponse:
    ...
```

### What MUST be in every endpoint

- `dependencies=AUTH` (except `/health`, `/ready`, `/api/auth-required`,
  and `/api/docs` which are explicitly public).
- `@secure_route.mutate(RATE.<TIER>, audit="<domain>.<verb>")` on
  mutations, `@secure_route.read(RATE.<TIER>)` on reads. The decorator
  owns CSRF, rate limiting, and audit emission — do NOT re-implement
  these in the handler.
- For Pydantic-body routes: define the model in
  `skiff/contract/requests.py` with `model_config = ConfigDict(extra="forbid")`
  so mass-assignment is blocked. For query-param routes: no Pydantic
  body is needed.
- Identifier / body validation via the shared validators in
  `skiff/validators.py` — never inline a regex that should be shared
  (the `AP011` linter enforces this).
- `safe_docker_call(...)` wrapping every Docker SDK invocation — it
  maps Docker errors to the documented error envelope.
- Return a Pydantic response model from `skiff/contract/responses.py`
  (`OkResponse`, `ContainerSummary`, `VolumeSummary`, …) — never a raw dict.
- Every user-visible string goes through `t("namespace.key")` in the UI;
  if you ship a new one, add it to `skiff/static/strings.en.js` first
  (see [`docs/dev/i18n.md`](i18n.md)).
- Every new `audit="<name>"` string must also exist in
  `skiff/contract/events.py::known_events()`. The route-contract test
  (`tests/test_route_contract.py`) fails a PR that adds an undeclared one.

---

## 3. Validation — reuse, don't duplicate

Validators already exist for:

- `validate_container_name` / `CONTAINER_NAME_RE`
- `validate_project_name` / `PROJECT_NAME_RE`
- `SERVICE_NAME_RE` (compose service)
- `validate_image_registry` / `IMAGE_TAG_RE`
- `validate_container_id` / `CONTAINER_ID_RE`
- `_validate_mount_target` (host-bind guard)
- `_validate_tmpfs` (tmpfs-specific, different blocklist)
- `parse_memory_quantity`, `parse_cpu_quantity` (GCP units)

If your feature needs a new validator:

1. Add it to `skiff/validators.py` next to siblings, not in the router
2. Raise `HTTPException(400, <message>)` — never a generic `ValueError` that
   becomes a 500 at the edge
3. Add property tests to `tests/test_fuzz.py`:
   - A positive generator that proves well-formed input parses
   - A negative `hypothesis.strategies.text()` generator that proves
     garbage never raises anything other than `HTTPException(400)`

---

## 4. Caps and safety defaults

If the feature lets a caller specify numeric limits (memory, CPU, retries,
counts), every limit gets:

- A **server-owned cap constant** in `skiff/config.py`
  (naming: `MAX_<DOMAIN>_<THING>`)
- A **check before Docker invocation** that returns 400 if exceeded
- A **unit test** that asserts the cap rejects the over-limit value AND
  that `<docker_sdk_call>` was NOT invoked (defence-in-depth)

Never trust client-supplied values to stay within engine limits — Docker
will often accept them and create resource-exhaustion.

---

## 5. Zero-trust patterns to preserve

1. **Secrets don't cross the UI boundary.** If the feature needs a stored
   secret (SSH target, env value), keep it in server-side state and expose
   operations that reference it by ID, not by value. Example:
   `get_tunnel_ssh_target()` is internal; `POST /api/tunnel/reconnect`
   takes NO body and uses the stored target.
2. **Audit logs never contain the secret.** Token, key, passphrase → 8-char
   suffix at most. Write a unit test that patches `log.info` and asserts
   the full secret never appears in any recorded kwarg.
3. **Defense in depth even for local-only sockets.** Tunnel socket paths,
   compose project directories, volume names — all validated by regex
   *before* reaching the filesystem. Never `Path(user_input).open()`.

---

## 6. UI — add the affordance

### 6a. Pick the right page

- Container-level action → `tr` action buttons in `loadContainers` OR
  inline in the Inspect panel (`showInspectContent`)
- Image-level → `loadImages`
- Compose stack → `showCompose`
- Cross-cutting admin → `loadSystem`

### 6b. Modal template

```javascript
function _showMyActionModal(context) {
  var modal = document.createElement('div'); modal.className = 'modal-bg';
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };
  var box = document.createElement('div'); box.className = 'modal';
  var h3 = document.createElement('h3'); h3.textContent = 'My Action';
  box.appendChild(h3);

  // Use textContent for any server-sourced data. NEVER interpolate into innerHTML.
  // Use addField() (existing helper in showRunModal) for consistent form styling.

  var actions = document.createElement('div'); actions.className = 'actions';
  actions.append(
    makeBtn('Cancel', function() { modal.remove(); }),
    makeActionBtn('Apply', async function() {
      var body = { /* gather fields */ };
      await apiFetch(API + '/my-action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      modal.remove();
      toast('Applied', 'success');
      // Refresh the context (showCompose, loadContainers, etc.)
    }, 'btn primary', 'Applying…'),
  );
  box.appendChild(actions);
  modal.appendChild(box);
  document.body.appendChild(modal);
}
```

### 6c. XSS defence

Any data from the server (names, IDs, error messages) → `element.textContent = …`.
Never `element.innerHTML = server_string`. Never string-concat into an
`innerHTML` template. If you need structure, build it with `document.createElement`
and `append`.

### 6d. Error handling

`apiFetch` throws `Error` with `.message` as a string and `.detail` as the
structured payload (if the server sent one). Render `.message`; use `.code`
on `.detail` to tailor the UX for classified errors (e.g., `auth_failed` →
copyable `ssh-copy-id` command). See `_renderTunnelError` for the pattern.

---

## 7. Tests — three layers

### 7a. Unit tests (examples)

```python
# tests/test_coverage_<router>.py
def test_my_action_happy_path(client, mock_docker):
    mock_docker.X.return_value = <expected>
    resp = client.post("/api/<resource>/<id>/<action>",
                       headers=AUTH_CSRF, json={...})
    assert resp.status_code == 200
    # Assert Docker SDK was called with the right args — the endpoint's real
    # logic, not the mock return value.
    assert mock_docker.X.call_args.kwargs == {...}

def test_my_action_requires_csrf(client, mock_docker):
    resp = client.post(..., headers={"Authorization": AUTH_CSRF["Authorization"]})
    assert resp.status_code == 403

def test_my_action_cap_enforced(client, mock_docker):
    resp = client.post(..., json={"field": "over-cap-value"})
    assert resp.status_code == 400
    mock_docker.X.assert_not_called()  # critical — cap CANNOT be bypassed

def test_my_action_ignores_unknown_fields(client, mock_docker):
    resp = client.post(..., json={"field": "ok", "privileged": True})
    assert resp.status_code == 200
    kw = mock_docker.X.call_args.kwargs
    assert "privileged" not in kw  # parameter smuggling defence
```

### 7b. Property tests (fuzz)

If the feature introduces a validator or parser, add Hypothesis tests to
`tests/test_fuzz.py`:

```python
@given(garbage=st.text(min_size=1, max_size=64))
@settings(max_examples=300)
def test_my_validator_only_raises_http(garbage):
    try:
        my_validator(garbage)
    except HTTPException as exc:
        assert exc.status_code == 400
    except Exception as exc:
        pytest.fail(f"my_validator raised {type(exc).__name__} — must be HTTPException only")
```

### 7c. E2E (journey)

Add a step to an existing journey in `tests/test_e2e_journeys.py`, OR add
a new journey if the feature is a distinct user-visible flow. Every journey
must:

1. Chain ≥ 3 user actions
2. Use `watch_server_log()` to assert no unexpected stderr
3. Verify via the Docker SDK (source of truth), not just UI state

### 7d. Coverage bar

`make coverage` must stay ≥ 95%. Critical modules (`auth`, `validators`,
`routers/networks`, `routers/volumes`) stay at 100%. If your change drops
one below its target, the PR doesn't merge until you fix it — either with
real tests (preferred) or with documented `# pragma: no cover` on
provably-dead defensive branches.

---

## 8. Documentation

Every new endpoint or user-visible change updates at least one of:

- `docs/api-reference.md` — always (the endpoint table)
- `README.md` — if a new top-level concept
- `../dev/storyboards.md` — add a journey row
- `../runbooks/README.md` — if the feature has common failure modes
- `../hardening/integrations.md` — if it exposes a new integration surface
- `../hardening/production.md` — if it has production-tuning knobs
- `SECURITY.md` Zero-trust gaps table — if it expands or closes a gap

---

## 9. Commit message style

Every feature commit body must include:

- **What** — the user-visible change in one sentence
- **Why** — the motivation (fix a gap, close a security finding, align
  with Docker API, etc.)
- **How** — the approach (new endpoint, shared helper, cap constant)
- **Tests** — count + coverage delta + any e2e added
- **Security review** — at minimum "no new surface" or an explicit note on
  the threat model change
- **Co-author** — `Co-Authored-By:` line for AI-assisted commits.
  GitHub attributes these contributions correctly and the audit trail
  stays honest about how the change was written.

Look at recent commits for the cadence:

```bash
git log --format='%B' -20
```

---

## 10. Checklist before opening the PR

- [ ] `ruff check skiff/ tests/` — clean
- [ ] `pytest -m "not e2e" --cov=skiff` — all pass, coverage ≥ 95%
- [ ] `pytest -m property tests/test_fuzz.py` — no 500-level exceptions
      leaking from your new validator
- [ ] `pytest -m e2e tests/test_e2e_journeys.py` — at least the journey
      that touches your feature passes against live Docker
- [ ] `pip-audit --strict -r requirements.txt` — no new CVEs introduced
- [ ] PR description has: what/why, screenshots for UI, breaking-change
      notes (if any), test plan
- [ ] Relevant docs updated per §8
