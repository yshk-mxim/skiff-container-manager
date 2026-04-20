# CIS Docker Benchmark — SKIFF posture mapping

The [CIS Docker Benchmark](https://www.cisecurity.org/benchmark/docker) is a consensus document covering Docker Engine, daemon, image, and container configuration. SKIFF wraps the Docker Engine, so most benchmark items land on the **operator's Docker host**, not SKIFF itself — but SKIFF's defaults should never push an operator off-benchmark.

This page maps each benchmark category to SKIFF's role.

## 1. Host configuration

Operator responsibility. SKIFF is a client; it doesn't change the Docker host's filesystem, auditd rules, or kernel settings.

SKIFF-side note: the default `BIND_HOST=127.0.0.1` posture keeps the SKIFF UI off the network entirely — a non-loopback bind surfaces a startup warning (`security.bind_non_loopback` in `skiff/contract/events.py`).

## 2. Docker daemon configuration

Operator responsibility. SKIFF connects to whatever Docker socket the operator provides.

SKIFF-side notes:

| CIS item | SKIFF behavior |
|---|---|
| 2.1 — don't expose Docker daemon over unauthenticated TCP | SKIFF warns at boot if `DOCKER_HOST` is unencrypted HTTP/TCP to a non-localhost host (`security.docker_host_unencrypted`). |
| 2.2 — restrict network traffic between containers | Operator responsibility; SKIFF neither relaxes nor tightens the daemon default. |
| 2.8 — enable user namespace support | Operator responsibility. SKIFF's compose sandbox BLOCKS `userns_mode` in user-submitted compose so a user can't disable userns for their container. |

## 3. Docker daemon configuration files

Operator responsibility. SKIFF never edits `/etc/docker/daemon.json` or similar.

## 4. Container images and build files

Operator responsibility — SKIFF pulls whatever images the operator's compose / run requests, subject to the registry allowlist.

SKIFF-side notes:

| CIS item | SKIFF behavior |
|---|---|
| 4.1 — content trust | Operator responsibility via `DOCKER_CONTENT_TRUST=1` on the Docker host. |
| 4.3 — install only necessary packages | Not SKIFF's decision. |

## 5. Container runtime

This is where SKIFF has the most to say — the compose sandbox enforces many CIS-recommended defaults for any container launched via `docker compose up` through SKIFF.

| CIS item | Benchmark says | SKIFF enforces |
|---|---|---|
| 5.4 — privileged container | Do not enable | `privileged: true` is in `skiff/_config/compose_sandbox.toml:forbidden.presence` |
| 5.5 — sensitive host directories | Do not mount | Host-path bind mounts are rejected in `skiff/validators.py:_validate_mount_target`; only named volumes permitted |
| 5.9 — sharing host network namespace | Do not share | `network_mode: host` / `container` / `service` blocked in `compose_sandbox.toml:blocked.network_modes` |
| 5.12 — mount container root fs as read-only | Read-only root | SKIFF's `/api/containers/run` defaults `read_only=true` (operator can opt out per-run) |
| 5.16 — sharing host process namespace | Do not share | `pid: host` blocked |
| 5.17 — sharing host IPC namespace | Do not share | `ipc: host` / `shareable` blocked |
| 5.22 — don't acquire additional capabilities | Restrict | `cap_add` is in `forbidden.truthy` — empty array permitted, non-empty rejected |
| 5.24 — cgroup usage | Default | `cgroup_parent` and `cgroupns_mode` blocked via `forbidden.truthy` |
| 5.25 — restrict acquisition of additional privileges | `no-new-privileges:true` recommended | Not currently enforced by SKIFF — operator can add via `security_opt`, but `security_opt` is in `forbidden.truthy`. Known gap tracked for post-1.0.x. |
| 5.28 — restrict container's use of runtime ulimit | Default | `ulimits` not rejected — operator choice. |

## 6. Docker security operations

Operator responsibility. SKIFF's audit log (events catalogue in `docs/audit-events.md`) produces SIEM-ready JSONL that a CIS-8-compliant ops team can ingest.

## 7. Docker swarm configuration

Not applicable — SKIFF does not manage swarm mode.

## Explicit limitations

SKIFF does not currently implement:

- **5.25** — automatic `no-new-privileges` injection on operator-submitted containers. The `security_opt` key is blocked in the compose sandbox, which prevents an operator from relaxing security but also prevents them from positively setting `no-new-privileges`. A post-1.0.x issue will split the block list: `security_opt` containing only safe values (`no-new-privileges:true`, `apparmor=…`, `seccomp=…`) becomes allowed.
- **CIS Docker scan in CI**. A `docker-bench-security` run is not in SKIFF's CI today. If you're deploying SKIFF into a CIS-scanning environment, run `docker-bench-security` against the Docker host directly — SKIFF does not change the host's posture.

## Reference

- [CIS Docker Benchmark v1.6.0](https://www.cisecurity.org/benchmark/docker)
- SKIFF's compose sandbox policy: `skiff/_config/compose_sandbox.toml`
- SKIFF's mount-target policy: `skiff/_config/mount_targets.toml`
