# Capability matrix

Capability-by-capability review of SKIFF against the expected baseline
for a Docker-management tool. Each row records one user-facing
capability; the `expectation` column captures whether that capability
is industry-standard (derived from a survey of commonly-cited docker-
management tooling). Product names are deliberately omitted — this
matrix is about *what SKIFF should support*, not about head-to-head
benchmarking.

**Legend**

| Symbol | Meaning |
|---|---|
| `Y` | Supported |
| `Y*` | Supported with a caveat (see footnote) |
| `N` | Not supported — gap for SKIFF if `expectation=Y` |
| `N!` | Intentional NO with security-policy rationale (see footnote) |
| `?` | Not verified this quarter — flagged for re-verification |

The `expectation` value is derived from the anonymised survey of
docker-management tooling. Y means "this capability is commonly
available in tools of this class"; N means "it is not a baseline
feature" (SKIFF may still ship it, but has no parity pressure).

The backing CSV at `docs/dev/capability_matrix.csv` is what
`tests/test_capability_parity.py` diffs against — keep them in sync
(run `make tracker`).

---

## UI reachability (top-level surfaces)

| Capability | SKIFF | Expectation |
|---|---|---|
| Dashboard / home page | Y | Y |
| Containers page | Y | Y |
| Images page | Y | Y |
| Volumes page | Y | Y |
| Networks page | Y | Y |
| Compose / Stacks page | Y | Y |
| System / Host page | Y | Y |
| Events stream viewer | Y | Y |
| Audit log viewer | Y | Y |
| Templates / quick-start | Y | Y |
| First-run tour | Y | Y |
| Notifications bell / history | Y | Y |
| Command palette (⌘K) | Y | N |
| Global search | Y*(per page) | Y |
| Settings page | N | Y |
| Help overlay | Y*(tour) | Y |

## Container actions

| Capability | SKIFF | Expectation |
|---|---|---|
| List all containers | Y | Y |
| Run / create container | Y | Y |
| Start | Y | Y |
| Stop | Y | Y |
| Restart | Y | Y |
| Pause / unpause | Y | Y |
| Kill | Y | Y |
| Rename | Y | Y |
| Update limits (`docker update`) | Y | Y |
| Remove (undoable) | Y | Y |
| Bulk actions (multi-select) | Y | Y |
| Right-click context menu | Y | Y |
| Logs (stream) | Y | Y |
| Logs search | Y | Y |
| Logs download | Y | Y |
| Stats (CPU/RAM) | Y | Y |
| Stats time-series chart | N! | Y |
| Top (processes) | Y | Y |
| Diff (filesystem changes) | Y | Y |
| Inspect (full JSON) | Y | Y |
| Inspect JSON download | Y | Y |
| Exec (interactive shell) | Y | Y |
| File copy in | Y | Y |
| File copy out | Y | Y |
| Filesystem browser | Y | Y |
| Commit to image | Y | Y |
| Attach (non-interactive) | N | Y |
| Export to tarball | N! | Y |
| Template/clone existing | Y*(commit) | Y |
| Health check status | Y | Y |
| Port mapping clickable | Y | Y |

## Image actions

| Capability | SKIFF | Expectation |
|---|---|---|
| List images | Y | Y |
| Pull from registry | Y | Y |
| Tag image | Y | Y |
| Push to registry | Y | Y |
| Remove image | Y | Y |
| Inspect image | Y | Y |
| Layer history | Y | Y |
| Prune dangling | Y | Y |
| Prune all unused | Y | Y |
| Search Docker Hub | Y | Y |
| Tags for repo | Y | Y |
| Build from Dockerfile | N! | Y |
| Import from tar | N! | Y |
| Save to tar | N! | Y |

## Volume actions

| Capability | SKIFF | Expectation |
|---|---|---|
| List volumes | Y | Y |
| Create (name only) | Y | Y |
| Create with driver | Y | Y |
| Create with labels | Y | Y |
| Create with driver_opts | Y | Y |
| Inspect volume | Y | Y |
| Delete volume | Y | Y |
| Prune volumes | Y | Y |
| Browse volume contents | N | Y |
| Back up volume | Y*(cp) | Y |

## Network actions

| Capability | SKIFF | Expectation |
|---|---|---|
| List networks | Y | Y |
| Create (name+driver) | Y | Y |
| Create with subnet | Y | Y |
| Create with gateway | Y | Y |
| Create with labels | Y | Y |
| Create internal/attachable/ipv6 | Y | Y |
| Inspect network | Y | Y |
| Connect container | Y | Y |
| Disconnect container | Y | Y |
| Delete network | Y | Y |
| Prune networks | Y | Y |

## Compose / Stack actions

| Capability | SKIFF | Expectation |
|---|---|---|
| List stacks | Y | Y |
| Deploy from YAML | Y | Y |
| Deploy from git repo | N | Y |
| Deploy from template | Y | Y |
| Tear down (down) | Y | Y |
| Stop | Y | Y |
| Start | Y | Y |
| Restart (stack) | Y | Y |
| Restart (service) | Y | Y |
| Pull updates | Y | Y |
| Scale service | Y | Y |
| Validate YAML | N*(deploy-only) | Y |
| Aggregated logs | Y | Y |
| Service-level logs | Y | Y |
| Download YAML | Y | Y |
| Edit YAML in UI | N | Y |
| Git-managed auto-redeploy | N | Y |

## Security / Auth

| Capability | SKIFF | Expectation |
|---|---|---|
| Bearer token auth | Y | Y |
| Token rotation | Y | Y |
| Reviewer / read-only mode | Y | Y |
| Rate limiting (per-IP) | Y | Y |
| CSRF protection | Y | Y |
| Origin allowlist | Y | Y |
| WS auth + lockout | Y | Y |
| Registry allowlist | Y | Y |
| Compose policy (block build/privileged) | Y | N |
| Container resource caps (floor) | Y | N |
| Audit log | Y | Y |
| Per-user RBAC | N! | Y |
| SSO / OAuth | N | Y |

## Observability

| Capability | SKIFF | Expectation |
|---|---|---|
| Stats current | Y | Y |
| Events stream | Y | Y |
| Audit log UI | Y | Y |
| Prometheus metrics | Y | Y |
| Structured JSON stderr | Y | Y |
| Debug endpoints | Y | Y |

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

### Gap findings (`SKIFF=N` where `expectation=Y`)

These create open findings — either fix them or reclassify as `N!`
with an explicit rationale.

| Capability | Finding ID |
|---|---|
| Settings page (preferences — refresh rate, theme, log tail cap) | `pa-settings-page-missing` |
| Deploy stack from git repo | `pa-compose-git-deploy-missing` |
| Edit compose YAML in UI | `pa-compose-inline-edit-missing` |
| Git-managed auto-redeploy | `pa-compose-autodeploy-missing` |
| Browse volume contents | `pa-volume-browse-missing` |
| Compose validate (config-only) | `pa-compose-validate-missing` |

Each gap row either becomes a follow-up commit (feature implemented) or
gets re-classified as `N!` with a rationale during the iteration loop.
