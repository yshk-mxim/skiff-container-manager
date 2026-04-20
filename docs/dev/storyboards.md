# SKIFF User Storyboards

A reference matrix of user personas, target configurations, workloads, and usage
scenarios. Each row is a candidate acceptance scenario — the Playwright journey
suite (`tests/test_e2e_journeys.py`) exercises the highlighted intersections
end-to-end and watches server-stderr for warnings during each journey. Rows
without an automated journey are documented as manual-verification scenarios.

---

## 1. Platforms

| Platform | Docker runtime | DOCKER_HOST | Audit-log default | Tested |
|---|---|---|---|---|
| macOS Sonoma+ | Any Docker Engine API runtime (Docker Desktop, OrbStack, etc.) | `unix:///var/run/docker.sock` | `~/Library/Application Support/skiff/audit.jsonl` | ✅ live (local + remote macOS via SSH tunnel) |
| macOS | OrbStack | `unix:///var/run/docker.sock` | same | Manual |
| macOS | Colima | `unix://$HOME/.colima/default/docker.sock` | same | Manual |
| macOS | Rancher Desktop | `unix://$HOME/.rd/docker.sock` | same | Manual |
| Linux (Debian/Ubuntu) | dockerd | `unix:///var/run/docker.sock` | `~/.local/state/skiff/audit.jsonl` | CI |
| Linux (Fedora/RHEL) | podman rootless | `unix:///run/user/$UID/podman/podman.sock` | same | Manual |
| Linux (Alpine) | dockerd | same | same | Manual |
| WSL2 + Docker Desktop | via WSL integration | `unix:///var/run/docker.sock` | `~/.local/state/skiff/audit.jsonl` | Manual |
| WSL2 without integration | dockerd in WSL | `unix:///var/run/docker.sock` | same | Manual |
| Remote VM via SSH tunnel | (target host's engine) | `unix:///tmp/skiff-docker.sock` | local (SKIFF side) | ✅ live |
| Remote TCP (TLS, e.g. AWS/GCP VM) | (target) | `tcp://ip:2376` | local | Manual |

---

## 2. User personas (expertise levels)

| # | Persona | Background | First need | First friction likely at |
|---|---|---|---|---|
| P1 | **Novice** — first Docker UI | Never used `docker` CLI, just wants to run nginx | Run a container, see it in the list | Knowing what "Read-only rootfs" means; port mapping format |
| P2 | **Developer** — local dev | Uses Docker daily, wants a nicer UI | Iterating on a dev container, seeing live logs | Finding the Clone button; the Save-.env vs In-memory-only trade-off |
| P3 | **DevOps engineer** | Manages a team's shared Docker host remotely | SSH tunnel + per-user auth | Token rotation flow; audit-log retention |
| P4 | **Security reviewer** | Auditing an install | Confirm hardening defaults, verify logs | Finding the audit.jsonl location; understanding the zero-trust boundary |
| P5 | **Hobbyist/homelab** | Running on a Raspberry Pi / NAS | Compose stack deploy | SSH key setup; bind-mount blocking |

---

## 3. Container workload families

| Family | Example images | Defaults work? | Common pitfall |
|---|---|---|---|
| **Stateless web servers** | `nginx`, `caddy`, `httpd` | ✅ with tmpfs defaults | Need write on `/var/cache/nginx/*` — covered by DEFAULT_TMPFS |
| **Databases** | `postgres`, `mysql`, `redis` | ⚠ need writable rootfs OR a volume for data | initdb writes to `/var/lib/...`; user must uncheck read-only OR add a named volume |
| **Dev containers (interactive)** | `alpine`, `ubuntu`, `python`, `node` | ⚠ need tty=true for interactive shells | Exec works via WS; `docker run -it` semantics not exposed in Run modal |
| **Long-running workers** | custom `worker:latest` | ✅ | — |
| **Compose stacks** | web+db+redis | ✅ | Bind-mounts blocked by validator; must use named volumes |
| **One-shot jobs** | `alpine echo hi` | ⚠ start detects exit ≤600ms and shows error toast | Expected behaviour |
| **GPU workloads** | `tensorflow/tensorflow:gpu` | ❌ no `--gpus` exposure | Out of scope |

---

## 4. Authentication & tunnel combinations

| Config | Setup path | Notes |
|---|---|---|
| Local, no auth | leave `API_TOKEN=""` | Dev only; startup warning |
| Local, token in .env | `API_TOKEN=…` in `.env` | `from_env=true`, rotate endpoint disabled |
| Local, session-only | complete wizard in browser | Rotate + reset endpoints available |
| SSH tunnel (pre-configured keys, no passphrase) | wizard → SSH Tunnel tab | Zero-trust: target stays server-side |
| SSH tunnel (no key yet on remote) | wizard shows `ssh-copy-id <target>` | Password never touches SKIFF (BatchMode=yes) |
| SSH tunnel with `ProxyJump` / bastion | wizard (uses `~/.ssh/config`) | Config include preserves ProxyJump |
| SSH tunnel after drop (reconnect) | Containers page Reconnect button | Uses stored target; zero-trust |
| TLS-reverse-proxied (Caddy/nginx + OIDC) | `../hardening/production.md §1, §5` | SSO via oauth2-proxy; `X-Forwarded-User` in audit log (requires `TRUST_FORWARDED_HEADERS=true`) |
| GCP Cloud Workstation | env-configured | Out-of-box HTTPS; no local proxy needed |

---

## 5. Multi-step journeys (exercised by `tests/test_e2e_journeys.py`)

### J1 — Novice local run (P1 + macOS + nginx)
1. Open SKIFF, complete wizard with Local/Custom + session-only
2. Containers → Run new → type `nginx:latest` → Run (defaults)
3. Verify container running, ports as specified
4. Click Logs → verify stream
5. Click Inspect → verify read-only rootfs = yes, tmpfs shown
6. Stop → Delete
7. **Watch:** no stderr warnings; audit.jsonl has `container.created`, `container.stopped`, `container.deleted` events

### J2 — Developer clone-and-edit (P2 + remote SSH + alpine)
1. Run alpine sleep-loop with env `SECRET=abc`
2. Inspect → edit memory 64M → 128Mi → Save
3. Inspect → Clone with changes → change cpus → Run (both exist)
4. Verify `SECRET=abc` preserved on clone (via Docker SDK, server-side)
5. Clone again → check "Replace original" → confirm source removed
6. **Watch:** no "undefined" in UI, no 5xx, all cap checks green

### J3 — Compose lifecycle (P3 + remote + 2-service stack)
1. Upload compose YAML (web + db, alpine-based)
2. Stack appears; click per-service Logs → view
3. Per-service Restart → verify state
4. All-service logs modal → web + db lines prefixed
5. Tear down → stack gone
6. **Watch:** no compose-validator false positives; no orphaned networks

### J4 — Token rotation and reset (P3 + session-only)
1. Configure via wizard, session-only mode
2. System page → Account → Rotate API token → generate new → copy → save
3. Verify old token 401s, new token 200s on subsequent API
4. Account → Reset configuration → confirm → reload → setup wizard shown

### J5 — Sad-path: tunnel drops mid-session (P3 + remote SSH)
1. Healthy app, containers listed
2. (out-of-band) kill the SSH tunnel socket
3. Navigate → "Cannot reach Docker" panel → Reconnect button
4. Click Reconnect → verify recovery
5. **Watch:** no stuck WebSockets from killed tunnel

### J6 — Security reviewer audit trail (P4 + session-only)
1. Complete setup, do several actions (pull, run, stop, rotate token)
2. Verify `~/Library/Application Support/skiff/audit.jsonl` exists and contains each event
3. Verify token values never appear in the file (suffix only)
4. Download audit log via System page

---

## 6. Capability matrix (what SKIFF covers / intentionally skips)

Capabilities are framed in terms of what SKIFF does, not relative to any
specific competing product — that kind of feature-by-feature comparison
is both fragile (the other side keeps shipping) and invites trademark
friction. Coverage status follows the Docker Engine API surface.

| Capability | Status in SKIFF |
|---|---|
| Container run / stop / start / restart / remove | Full coverage |
| Container rename | Full coverage |
| Container inspect | Full coverage, with inline edit for live-updatable resource fields |
| Container clone with changes | Zero-trust clone preserves env server-side; `replace_id` for safe recreate |
| Container logs — historical + live-stream | Full coverage via HTTP + WebSocket |
| Container exec shell | WS-based; per-IP concurrency limit |
| Container stats | One-shot today; ring-buffered trend graphs on the roadmap |
| Container filesystem diff | Full coverage |
| Container resource update | `POST /api/containers/{id}/update` mirrors Docker Engine API v1.47 |
| Image list / pull / tag / push / inspect / remove | Full coverage |
| Docker Hub search | Full coverage; other registries return their configured search surface |
| Compose deploy / teardown | Full coverage with strict sandbox (privileged, cap_add, host mounts blocked) |
| Compose per-service logs + aggregated logs | Full coverage |
| Compose per-service restart | Full coverage via Docker SDK, no subprocess |
| Volumes create / list / inspect / remove / prune | Full coverage |
| Networks create / list / remove / connect / disconnect / prune | Full coverage |
| Prometheus-format metrics scrape | `/api/system/metrics` — standard text/plain v0.0.4 |
| Structured audit log (JSONL + GCP Cloud Logging sink) | Full coverage |
| Token rotation + config reset | Full coverage without server restart |
| Building images from Dockerfiles | Out of scope (supply-chain blast radius) |
| Kubernetes cluster management | Out of scope (different mental model) |
| Plugin / extension system | Out of scope (API-stability cost > flexibility gain) |
| Persistent DB-backed state | Out of scope (single-process, no-DB is a core property) |

---

## 7. Known edge cases documented for manual verification

- **Colima/Rancher socket symlinks** — docker.sock under `$HOME` may need the full resolved path; `unix://$HOME/.colima/default/docker.sock` works, but WebSocket exec can fail if the symlink target changes after container start.
- **Podman rootless** — runs fine but `no-new-privileges` interacts with rootless namespacing; some images that drop privileges internally may fail.
- **Filesystem case sensitivity** — macOS APFS is case-insensitive by default; Linux is case-sensitive. Registry allowlist compares case-insensitively (`docker.io` matches `Docker.io`) per the validator.
- **Slow first SSH connect** — first contact to a `.local` host needs mDNS + host-key add; budget ≥ 20s. `TUNNEL_CONNECT_TIMEOUT=15` + 5s subprocess buffer is on the edge.
- **Network reliability** — if the TCP connection to the Docker daemon drops mid-API-call (remote TCP or tunnel), the app shows "Container engine unreachable" and offers Reconnect for managed tunnels.
- **Clock skew** — session absolute timeout uses server wall-clock; a large skew between client JS timer and server clock can cut sessions short or extend them. Not currently mitigated.
- **Browser back-forward cache (bfcache)** — session storage survives bfcache restore; sensitive actions always re-verify session age before execution.
