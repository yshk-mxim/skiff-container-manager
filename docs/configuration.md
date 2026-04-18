# Configuration Guide

How to tune SKIFF without editing Python source.

Every operational knob — timeouts, sizes, concurrency caps, retention,
lockout windows — has three layers that decide its final value:

1. **Environment variable** (per-host override) — highest precedence.
2. **`skiff/_config/defaults.toml`** (fleet-wide defaults) — middle precedence.
3. **Validator fallback** in `skiff/config.py` — last resort.

A per-host tweak is an env var. A fleet-wide change is a TOML edit +
restart. Changes that alter security policy stay in Python and require a
reviewed source patch — see [Policy-pinned values](#policy-pinned-values)
below.

The full list of every registered knob lives in
[`docs/config-knobs.md`](config-knobs.md) (auto-generated from the
registry). This guide explains *how* to use them; the catalogue lists
*what* they are.

---

## The three layers

### 1. Env var (per-host, per-restart)

Every knob's name IS its env var name. Setting

```bash
DOCKER_CLIENT_TIMEOUT=30 skiff
```

overrides the TOML default for this one process. Useful for:

- Per-host tuning (e.g. a slow link needs `TUNNEL_CONNECT_TIMEOUT=60`).
- Feature flags off/on for debugging (`SKIFF_DEBUG_THREADS=1`).
- CI overrides (`RATE_LIMIT_SCALE=100` in test runs).

Env beats TOML. No other precedence arm changes once the process is
running.

### 2. `skiff/_config/defaults.toml` (fleet-wide)

To shift the baseline for every deployment in your org, edit
`skiff/_config/defaults.toml`:

```toml
# Before
DOCKER_CLIENT_TIMEOUT = 15

# After — every host now gets 30s unless a host-specific env var overrides
DOCKER_CLIENT_TIMEOUT = 30
```

Restart the server to pick it up. The TOML is loaded at import time,
then each `config_knob(...)` call reads it as the default for its env
var. `skiff/_config/defaults.toml` is in the repo so `git diff` reviews every
baseline change.

### 3. Validator fallback (compile-time)

A handful of knobs pass `default="..."` inline instead of pulling from
the TOML (`MAX_BODY_BYTES`, `AUDIT_MAX_MB`, `SKIFF_DEBUG_THREADS`). They
still honour env-var overrides; they just don't show up in
`defaults.toml`. New knobs SHOULD go through the TOML — the inline-only
form is reserved for values that are genuinely one-off.

---

## Examples

### Make audit logs cover one year of retention

`skiff/_config/defaults.toml` doesn't own `AUDIT_MAX_MB` / `AUDIT_BACKUP_COUNT`
— those are inline knobs. Per-host env overrides:

```bash
AUDIT_MAX_MB=200 AUDIT_BACKUP_COUNT=20 skiff
# 200 MB × 20 files = 4 GB → ~13 months at 10 MB/day
```

Fleet-wide: add them to `skiff/_config/defaults.toml` and redeploy.

### Slow link between SKIFF and the Docker host

```bash
DOCKER_CLIENT_TIMEOUT=45 TUNNEL_CONNECT_TIMEOUT=60 COMPOSE_UP_TIMEOUT=300 skiff
```

Or drop those three lines in `defaults.toml` once and restart.

### Disable WebSocket auth lockout for a dev sandbox

```bash
WS_AUTH_MAX_ATTEMPTS=999 WS_AUTH_LOCKOUT_SECS=1 skiff
```

**Do not ship this to production.** The lockout is a brute-force guard;
raising the cap means a credential-stuffer gets more guesses per window.

### Inspect the live configuration

`GET /api/config` returns every knob with `expose=True, secret=False`.
Scope it to the network / rev-proxy you trust; the shape is identical
to `docs/config-knobs.md`:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     -H "X-Requested-With: ContainerManager" \
     http://127.0.0.1:8080/api/config | jq
```

---

## Policy-pinned values

These live in Python, not TOML or env vars, because changing them
weakens the sandbox or contradicts an engine/OS invariant. Operator
intent to change should produce a reviewed source patch, not a silent
env tweak:

| Name | Where | Why it's pinned |
|---|---|---|
| `MAX_CONTAINER_CPU = 2.0` | `skiff/config.py` | Sandbox cap. Raising this lets a single container starve the host. |
| `MAX_CONTAINER_MEM = "2g"` | `skiff/config.py` | Sandbox cap. Same rationale. |
| `PRIVILEGED_PORT_THRESHOLD = 1024` | `skiff/config.py` | OS constant; binding privileged ports requires `CAP_NET_BIND_SERVICE` on the host. |
| `MAX_RESTART_RETRIES = 5` | `skiff/config.py` | Bounds on-failure restart loops; bigger values turn crashloops into DOS. |
| `MAX_PIDS_LIMIT = 4096` | `skiff/config.py` | Per-container PID cap; bigger values leak fork-bomb surface. |
| `MAX_TMPFS_SIZE_MB = 512` | `skiff/config.py` | Sum of tmpfs per container; RAM-exhaustion guard. |
| `MAX_TMPFS_MOUNTS = 10` | `skiff/config.py` | tmpfs count cap. |
| `MIN_TOKEN_LENGTH = 16` | `skiff/config.py` | Auth policy floor — weaker tokens are brute-forceable. |
| `TOKEN_AUDIT_SUFFIX_LEN = 8` | `skiff/config.py` | Disclosure boundary for the audit-log token suffix. |
| `DOCKER_MIN_MEM_BYTES = 6 MiB` | `skiff/config.py` | Docker engine rejects `Memory<6MiB`; not an operator choice. |
| `VALID_RESTART_POLICIES` | `skiff/config.py` | Docker Engine API enum — source-of-truth is the engine, not us. |
| `MAX_VOLUME_NAME_LENGTH = 63` | `skiff/config.py` | Docker spec constraint. |

Everything else that governs a timeout, size, count, or retention goes
through the TOML + env-var path.

---

## Data catalogues live in their own TOML files

Beyond `defaults.toml`, the repo has other TOML files that own specific
categories of data. Most are operator-editable but subject to CODEOWNERS
review because a change here is a security-policy change:

| File | Owns | Changing this… |
|---|---|---|
| `skiff/_config/defaults.toml` | Operational tunables — this guide. | Fleet-wide tuning. |
| `skiff/_config/profiles.toml` | Persona presets (`PROFILE=dev/reviewer/tutor/ci`). | New persona = new row. |
| `skiff/_config/rate.toml` | Rate-limit envelope per route class. | Lowers / raises request thresholds. |
| `skiff/_config/compose_sandbox.toml` | Forbidden compose keys / network modes / IPC modes. | Sandbox escape surface. |
| `skiff/_config/mount_targets.toml` | Blocklist of mount paths (host path escapes). | Volume sandbox surface. |
| `skiff/_config/networks.toml` | Built-in network names, allowed drivers. | Network CRUD surface. |
| `skiff/_config/security_headers.toml` | CSP, HSTS, Permissions-Policy. | Browser-side policy. |
| `skiff/_config/ssh_tunnel.toml` | Static + dynamic SSH options for tunnel start. | Tunnel hardening. |
| `skiff/_config/ssh_errors.toml` | stderr → classified-code table for tunnel errors. | Cosmetic. |
| `skiff/_config/docker_probe.toml` | Paths the wizard probes for a Docker socket. | Wizard UX. |
| `skiff/_config/tmpfs.toml` | Default tmpfs mounts applied with `read_only=true`. | Runtime compatibility. |
| `skiff/_config/connect_snippets.toml` | Templates for the "Connect external tool" panel. | UI content. |

See each file's top-of-file comment for the zero-trust rationale.

---

## Regenerating the knob catalogue

```bash
python tools/gen_catalogues.py
```

Writes `docs/config-knobs.md`, `docs/errors.md`, `docs/audit-events.md`.
CI runs `--check` and fails if the generated files drift from the
Python source.

---

## When to add a new knob

Ask, in order:

1. **Is this a security policy value?** → Python constant + comment.
   Adding this to env/TOML means an operator can silently weaken a
   sandbox cap.
2. **Is this an engine/OS invariant?** → Python constant.
3. **Otherwise**: add a `_int_knob("NAME", doc="...")` line in
   `skiff/config.py`, and a matching row in `skiff/_config/defaults.toml`.
   Regenerate `docs/config-knobs.md`.

A new knob that doesn't fit the TOML's `int | float | bool | str` type
set (e.g. a list, a regex) should stay an inline `config_knob(...)`
with a custom validator — the TOML loader doesn't try to handle
structured types because every case observed in SKIFF has been a scalar.
