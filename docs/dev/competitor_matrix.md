# Competitor parity matrix

Capability-by-capability comparison of SKIFF against the six tools most
frequently cited as Docker-management alternatives: Portainer CE, Docker
Desktop, Lens, Dockge, Yacht, and LazyDocker. Each row represents one
user-facing capability; the cells record whether the tool supports it.

**Legend**

| Symbol | Meaning |
|---|---|
| `Y` | Supported |
| `Y*` | Supported with a caveat (see footnote) |
| `N` | Not supported — gap for SKIFF if competitor has it |
| `N!` | Intentional NO with security-policy rationale (see footnote) |
| `?` | Not verified this quarter — flagged for re-verification |

**Sources** (all verified 2026-04-18):
- Portainer CE: [docs.portainer.io](https://docs.portainer.io)
- Docker Desktop: [docs.docker.com/desktop](https://docs.docker.com/desktop/)
- Lens: [docs.k8slens.dev](https://docs.k8slens.dev) (Docker-relevant subset)
- Dockge: [github.com/louislam/dockge](https://github.com/louislam/dockge)
- Yacht: [yacht.sh](https://yacht.sh) + [github.com/SelfhostedPro/Yacht](https://github.com/SelfhostedPro/Yacht)
- LazyDocker: [github.com/jesseduffield/lazydocker](https://github.com/jesseduffield/lazydocker)

The backing CSV at `docs/dev/competitor_matrix.csv` is what
`tests/test_capability_parity.py` diffs against — keep them in sync
(run `make tracker`).

---

## UI reachability (top-level surfaces)

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| Dashboard / home page | Y | Y | Y | Y | Y (stacks home) | Y | N |
| Containers page | Y | Y | Y | Y | N | Y | Y |
| Images page | Y | Y | Y | N | N | Y | Y |
| Volumes page | Y | Y | Y | N | N | Y | N |
| Networks page | Y | Y | Y | N | N | Y | N |
| Compose / Stacks page | Y | Y | Y | N | Y | Y | Y |
| System / Host page | Y | Y | Y | Y | N | Y | N |
| Events stream viewer | Y | Y | Y | Y | N | N | N |
| Audit log viewer | Y | Y (BE only) | N | N | N | N | N |
| Templates / quick-start | Y | Y | N | N | N | Y | N |
| First-run tour | Y | Y | Y | Y | N | N | N |
| Notifications bell / history | Y | Y | Y | Y | N | N | N |
| Command palette (⌘K) | Y | N | N | N | N | N | N |
| Global search | Y*(per page) | Y | Y | Y | Y | Y | Y |
| Settings page | N | Y | Y | Y | N | Y | N |
| Help overlay | Y*(tour) | Y | Y | Y | N | N | Y |

## Container actions

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| List all containers | Y | Y | Y | Y | Y (per stack) | Y | Y |
| Run / create container | Y | Y | Y | N | Y (compose) | Y | N |
| Start | Y | Y | Y | Y | Y | Y | Y |
| Stop | Y | Y | Y | Y | Y | Y | Y |
| Restart | Y | Y | Y | Y | Y | Y | Y |
| Pause / unpause | Y | Y | Y | Y | N | Y | Y |
| Kill | Y | Y | Y | Y | N | Y | Y |
| Rename | Y | Y | N | N | N | N | N |
| Update limits (`docker update`) | Y | Y | N | N | N | N | N |
| Remove (undoable) | Y | Y* | Y | Y | Y | Y | Y |
| Bulk actions (multi-select) | Y | Y | Y | Y | N | Y | N |
| Right-click context menu | Y | Y | Y | Y | N | N | N |
| Logs (stream) | Y | Y | Y | Y | Y | Y | Y |
| Logs search | Y | Y | Y | Y | N | N | N |
| Logs download | Y | Y | Y | N | N | Y | N |
| Stats (CPU/RAM) | Y | Y | Y | Y | N | Y | Y |
| Stats time-series chart | N! | Y | Y | Y | N | Y | N |
| Top (processes) | Y | Y | Y | Y | N | Y | N |
| Diff (filesystem changes) | Y | Y | N | N | N | N | N |
| Inspect (full JSON) | Y | Y | Y | Y | Y | Y | Y |
| Inspect JSON download | Y | N | Y | Y | N | N | N |
| Exec (interactive shell) | Y | Y | Y | Y | Y | Y | Y |
| File copy in | Y | Y | Y | N | N | N | N |
| File copy out | Y | Y | Y | N | N | N | N |
| Filesystem browser | Y | Y | Y | N | N | N | N |
| Commit to image | Y | Y | N | N | N | N | N |
| Attach (non-interactive) | N | Y* | N | N | N | N | N |
| Export to tarball | N! | Y | N | N | N | N | N |
| Template/clone existing | Y*(commit) | Y | N | N | N | N | N |
| Health check status | Y | Y | Y | Y | N | Y | N |
| Port mapping clickable | Y | Y | Y | Y | N | Y | N |

## Image actions

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| List images | Y | Y | Y | N | N | Y | Y |
| Pull from registry | Y | Y | Y | N | N | Y | N |
| Tag image | Y | Y | Y | N | N | N | N |
| Push to registry | Y | Y | Y | N | N | N | N |
| Remove image | Y | Y | Y | N | N | Y | Y |
| Inspect image | Y | Y | Y | N | N | Y | Y |
| Layer history | Y | Y | Y | N | N | N | Y |
| Prune dangling | Y | Y | Y | N | N | Y | Y |
| Prune all unused | Y | Y | Y | N | N | Y | N |
| Search Docker Hub | Y | Y | Y | N | N | Y | N |
| Tags for repo | Y | Y | Y | N | N | N | N |
| Build from Dockerfile | N! | Y | Y | N | N | N | N |
| Import from tar | N! | Y | Y | N | N | N | N |
| Save to tar | N! | Y | Y | N | N | N | N |

## Volume actions

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| List volumes | Y | Y | Y | N | N | Y | Y |
| Create (name only) | Y | Y | Y | N | N | Y | N |
| Create with driver | Y | Y | Y*(plugin) | N | N | Y | N |
| Create with labels | Y | Y | N | N | N | N | N |
| Create with driver_opts | Y | Y | N | N | N | N | N |
| Inspect volume | Y | Y | Y | N | N | Y | N |
| Delete volume | Y | Y | Y | N | N | Y | Y |
| Prune volumes | Y | Y | Y | N | N | Y | N |
| Browse volume contents | N | Y | Y | N | N | Y | N |
| Back up volume | Y*(cp) | Y | N | N | N | N | N |

## Network actions

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| List networks | Y | Y | Y | N | N | Y | N |
| Create (name+driver) | Y | Y | Y | N | N | Y | N |
| Create with subnet | Y | Y | Y | N | N | N | N |
| Create with gateway | Y | Y | Y | N | N | N | N |
| Create with labels | Y | Y | N | N | N | N | N |
| Create internal/attachable/ipv6 | Y | Y | Y | N | N | N | N |
| Inspect network | Y | Y | Y | N | N | Y | N |
| Connect container | Y | Y | Y | N | N | Y | N |
| Disconnect container | Y | Y | Y | N | N | Y | N |
| Delete network | Y | Y | Y | N | N | Y | N |
| Prune networks | Y | Y | Y | N | N | Y | N |

## Compose / Stack actions

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| List stacks | Y | Y | Y | N | Y | Y | Y |
| Deploy from YAML | Y | Y | Y | N | Y | Y | Y |
| Deploy from git repo | N | Y | Y | N | Y | Y | N |
| Deploy from template | Y | Y | N | N | Y | Y | N |
| Tear down (down) | Y | Y | Y | N | Y | Y | Y |
| Stop | Y | Y | Y | N | Y | N | Y |
| Start | Y | Y | Y | N | Y | N | Y |
| Restart (stack) | Y | Y | Y | N | Y | Y | Y |
| Restart (service) | Y | Y | Y | N | Y | N | Y |
| Pull updates | Y | Y | Y | N | Y | N | N |
| Scale service | Y | Y | N | N | N | N | N |
| Validate YAML | N*(deploy-only) | Y | Y | N | Y | N | N |
| Aggregated logs | Y | Y | Y | N | Y | Y | Y |
| Service-level logs | Y | Y | Y | N | Y | Y | Y |
| Download YAML | Y | Y | Y | N | Y | N | N |
| Edit YAML in UI | N | Y | Y | N | Y | N | N |
| Git-managed auto-redeploy | N | Y | N | N | Y | N | N |

## Security / Auth

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| Bearer token auth | Y | Y | N | N | Y | Y | N |
| Token rotation | Y | Y | N | N | N | N | N |
| Reviewer / read-only mode | Y | Y (RBAC) | N | N | N | Y | N |
| Rate limiting (per-IP) | Y | Y | N | N | N | N | N |
| CSRF protection | Y | Y | N | N | Y | Y | N |
| Origin allowlist | Y | Y | Y | N | Y | Y | N |
| WS auth + lockout | Y | Y | N | N | N | N | N |
| Registry allowlist | Y | Y (BE) | N | N | N | N | N |
| Compose policy (block build/privileged) | Y | N | N | N | N | N | N |
| Container resource caps (floor) | Y | N | N | N | N | N | N |
| Audit log | Y | Y (BE) | N | N | N | N | N |
| Per-user RBAC | N! | Y (BE) | Y | Y | N | N | N |
| SSO / OAuth | N | Y (BE) | Y | Y | N | N | N |

## Observability

| Capability | SKIFF | Portainer | Docker Desktop | Lens | Dockge | Yacht | LazyDocker |
|---|---|---|---|---|---|---|---|
| Stats current | Y | Y | Y | Y | N | Y | Y |
| Events stream | Y | Y | Y | Y | N | N | N |
| Audit log UI | Y | Y (BE) | N | N | N | N | N |
| Prometheus metrics | Y | Y | N | N | N | N | N |
| Structured JSON stderr | Y | Y | N | N | N | N | N |
| Debug endpoints | Y | N | N | Y | N | N | N |

---

## Gap footnotes

### Intentional `N!` (SKIFF's security-policy NOs)

| Capability | Rationale |
|---|---|
| Build from Dockerfile | Supply-chain: a Dockerfile can `FROM` any registry, bypassing the allowlist. |
| Import image from tar | Allowlist bypass: tarballs can encode any image regardless of registry. |
| Save image to tar | Currently out-of-scope; operator can use `docker save` via Terminal. Not a security NO strictly, more an operational simplicity NO. |
| Export container to tar | Same as save — operator has CLI fallback. |
| Stats time-series chart | Prometheus endpoint + Grafana covers this; in-browser ring buffer is a worse compound bet. |
| Per-user RBAC | Single-operator design; reviewer mode is the one concession. |
| SSO / OAuth | Single-operator; bearer+rotation is the auth model. |
| Privileged containers | Compose + run validators block. |
| Host bind mounts | Compose validator blocks; operators use named volumes. |

### Gap findings (`N` in SKIFF where competitor has `Y`)

These create open findings — either fix them or reclassify as `N!`
with an explicit rationale.

| Capability | Competitors with Y | Finding ID |
|---|---|---|
| Settings page (preferences — refresh rate, theme, log tail cap) | Portainer, DockerDesktop, Lens, Yacht | `pa-settings-page-missing` |
| Deploy stack from git repo | Portainer, DockerDesktop, Dockge, Yacht | `pa-compose-git-deploy-missing` |
| Edit compose YAML in UI | Portainer, DockerDesktop, Dockge | `pa-compose-inline-edit-missing` |
| Git-managed auto-redeploy | Portainer, Dockge | `pa-compose-autodeploy-missing` |
| Browse volume contents | Portainer, DockerDesktop, Yacht | `pa-volume-browse-missing` |
| Compose validate (config-only) | Portainer, DockerDesktop, Dockge | `pa-compose-validate-missing` |

Each gap row either becomes a follow-up commit (feature implemented) or
gets re-classified as `N!` with a rationale during the iteration loop.
