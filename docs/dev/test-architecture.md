# SKIFF test architecture

This document maps every test file to a layer of the testing pyramid,
identifies which **bug class** each layer is best at catching, and
cites the external best-practice framework each class draws from.
Future contributors should add new tests at the layer that matches
the bug class — don't write e2e for what a unit fuzz catches cheaply,
and don't fuzz what only a real browser exposes.

## Layers and the bug classes each catches

```
                    ┌──────────────────────┐
                    │  e2e (Playwright)    │  ← Nielsen #1-#10, a11y (WCAG 2.1 AA),
                    │   Tier A / B / C / D │    real-browser JS, network stack
                    └──────────┬───────────┘
                               │
                    ┌──────────┴───────────┐
                    │  Integration + e2e   │  ← API contract drift, route wiring,
                    │  Server (TestClient) │    auth middleware, rate-limit tiers,
                    │                      │    error envelope shape
                    └──────────┬───────────┘
                               │
                    ┌──────────┴───────────┐
                    │  Contract / property │  ← OWASP API Top 10, state-machine
                    │  / stateful fuzz     │    safety, lifecycle correctness
                    └──────────┬───────────┘
                               │
                    ┌──────────┴───────────┐
                    │   Unit + parser fuzz │  ← RFC validation (8259 JSON, 2616
                    │   (hypothesis)       │    headers), ISO/IEC 25010 reliability,
                    │                      │    CWE-117/79/89 injection classes
                    └──────────────────────┘
```

Proportion target: 60% unit, 25% integration + contract, 15% e2e.
Current count (~950 tests) sits roughly in that band.

## Layer-by-layer

### L1 — Unit / parser fuzz (fastest, narrowest scope)

| File | Class caught | Framework cited |
|---|---|---|
| `test_fuzz.py` | Validator invariants (memory/CPU/name/tmpfs) — malformed input is rejected with catalogued `HTTPException(400)`, never 500 | Property-based testing (Haskell QuickCheck lineage); OWASP API8 |
| `test_parser_fuzz.py` | Parsers for YAML, image names, WS resize frames, audit classifiers, error envelopes survive full-Unicode input | Hypothesis; NIST SP 800-115 fuzzing guidance |
| `test_properties.py` | General validator properties | QuickCheck |
| `test_hypothesis_expansion.py` | Property coverage for state-shape invariants | QuickCheck |
| `test_state_transitions.py` | **FSMs**: UndoQueue, WS counter, PROFILE gate. Uses `RuleBasedStateMachine` to fuzz operation sequences | Concurrent state-machine testing (Leslie Lamport lineage) |
| `test_docker_null_tolerance.py` | **Data-shape drift** — Docker returns JSON `null` where code expects `0` (cgroup v1/v2 divergence, pruned build cache, storage driver without usage data) | AP015 anti-pattern lint + hypothesis; no direct external framework (SKIFF-specific) |
| `test_backend_bug_class_fuzz.py` | (1) Docker SDK exception funnel → catalogued envelope; (2) Unicode/CRLF round-trip through response body + headers; (3) Numeric boundaries (no NaN/Infinity in JSON); (4) Auth-gated route contract | **OWASP API Security Top 10 2023** (API2, API8, API10); **RFC 8259 §6** (JSON); **CWE-113** (CRLF injection) |

### L2 — Contract / stateful fuzz (route-wired, in-process server)

| File | Class caught | Framework cited |
|---|---|---|
| `test_contract.py` | Every Pydantic response model's shape matches the OpenAPI schema | Pact-style consumer contract testing |
| `test_route_contract.py` | Every registered route is allowlisted + tagged; no shadow endpoints | **OWASP API9** (improper inventory management) |
| `test_container_journey_fuzz.py` | **Container lifecycle FSM** — hypothesis generates random sequences of create/start/stop/pause/unpause/remove; invariants fire between every step (UI matches daemon, states valid, no 500s). Adapted from **Litmus**, **Chaos Mesh**, **Pumba** patterns | CNCF chaos engineering; Rancher `createE2EResourceName` convention |
| `test_secure_route.py` | Every mutating route is secure-decorated (auth + CSRF + origin) | OWASP API5 (broken function-level authorization) |
| `test_security.py` | Rate-limit tier boundaries, token rotation, reviewer-mode gate | OWASP API2, API4 |
| `test_coverage_middleware.py` | Audit / error / rate-limit middlewares fire in the right order | — |

### L3 — Integration (TestClient, real routers + mocks)

| File | Class caught | Framework cited |
|---|---|---|
| `test_coverage_*.py` (~20 files) | Per-router happy + sad paths with mocked Docker | — |
| `test_audit.py` | Audit events emitted, shape, redaction | CWE-117 log injection |
| `test_auth.py` | Token + CSRF + origin checks | OWASP API2 |
| `test_containers.py` / `test_images.py` / … | Per-resource CRUD | — |
| `test_setup.py` | Setup wizard state machine, lockout accounting | — |
| `test_validation.py` | Pydantic request-model rejection of extra fields (mass-assignment guard) | **OWASP API3** (BOPLA) |

### L4 — E2E (Playwright, real browser + real server + real/mocked Docker)

| File | Class caught | Tier |
|---|---|---|
| `test_e2e_tier_a.py` | First-user flows (wizard, run, exec, logs, compose) | A — every new user, first 10 min |
| `test_e2e_tier_b.py` | Mid-session correctness (rotate, session expiry, reviewer, WS lockout) | B — common mid-session ops |
| `test_e2e_tier_c_local.py` | Session-configured operator flows (reset, two-tab, zombie WS) | C — wizard-started admin |
| `test_e2e_tier_d_tunnel.py` | SSH tunnel regressions, remote Docker | D — remote |
| `test_e2e_silent_expiry.py` | Windows / sessions that expire invisibly surface a banner | Nielsen #1 visibility of system status |
| `test_e2e_ux_flows.py` | Undo toast copy, reviewer banner, etc. | Nielsen #2 match real-world |
| `test_e2e_accessibility.py` | axe-core sweep of signed-in SPA | **WCAG 2.1 AA** |
| `test_e2e_ui_gaps.py` / `test_e2e_ui.py` | Page-load + per-page smoke | — |
| `test_ui_bug_class_regressions.py` | **Class-level UI guards** — dead external links (Nielsen #5 error prevention), interval-lifecycle leaks (engineering race class), countdown-label noun-context (Nielsen #2) | Nielsen's 10 Usability Heuristics |
| `test_e2e_resilience.py` | Docker unreachable banner, network flap | — |
| `test_e2e_sad_paths.py` | Error-envelope rendering in UI | — |

## Adopted external frameworks (citations for future contributors)

- **OWASP API Security Top 10 (2023)** — [owasp.org/API-Security](https://owasp.org/API-Security/editions/2023/en/0x00-header/). SKIFF explicitly targets API2 (auth), API3 (BOPLA), API4 (rate limiting), API5 (BFLA), API8 (misconfiguration), API9 (inventory), API10 (unsafe upstream consumption = Docker daemon).
- **NIST SP 800-115** — fuzzing + DAST methodology. Hypothesis-driven property tests are the SAST-adjacent half; e2e is the DAST half.
- **Nielsen's 10 Usability Heuristics** — `test_ui_bug_class_regressions.py` maps each test to a numbered heuristic in its docstring.
- **WCAG 2.1 AA** — axe-core integration in `test_e2e_accessibility.py`; CI-gated.
- **CNCF Chaos Engineering** (Litmus, Chaos Mesh, Pumba) — pattern for `test_container_journey_fuzz.py`: bounded random operation sequences with between-step invariants.
- **Rancher UI extension conventions** — `_test_name()` prefix pattern (adapted from `createE2EResourceName`) so any test leak is greppable.
- **RFC 8259 (JSON) + RFC 7230 (HTTP headers)** — numeric + CRLF invariants enforced in `test_backend_bug_class_fuzz.py`.

## Deciding where a new test belongs

1. **Can the bug be repro'd with a pure function call and hypothesis input?**
   → L1 (unit fuzz). Cheap, runs in <1s.
2. **Does the bug need the full router + middleware stack, but not a browser?**
   → L2/L3 (contract/integration via `TestClient`). Runs in single-digit seconds.
3. **Does the bug only manifest in the browser (JS race, DOM contamination, a11y)?**
   → L4 (e2e Playwright). Run selectively in CI; amortise fixture startup.
4. **Is it a class, not an instance?** Add to the class-level files
   (`test_ui_bug_class_regressions.py`, `test_backend_bug_class_fuzz.py`,
   `test_container_journey_fuzz.py`). A future regression in the same
   shape should trip the same guard.

## Lints that back the test system

- `tools/lint_antipatterns.py` — AP001–AP015 AST-based project anti-patterns, notably AP015 (`.get("NullableDockerField", 0)` trap).
- Ruff + mypy strict — enforced via `make ci`.
- axe-core + pa11y — a11y gates in e2e sweep.
