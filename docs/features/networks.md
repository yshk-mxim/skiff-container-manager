# Feature: Docker network management

## What it is
Create, list, delete, connect, and disconnect Docker networks from the
SKIFF UI. Built-in networks (`bridge`, `host`, `none`) are protected
against deletion.

## Who it's for
Primary: `dev`. Secondary: `sre`, `homelab`.

## UI flow
- Sidebar → *Networks* (`pages/networks.js`).
- Create button → modal with `name` + `driver` (dropdown limited to
  `bridge`, `overlay`, `macvlan`, `none`).
- Each row shows driver, scope, IPAM config, attached containers.
- Row → *Connect* / *Disconnect* opens a container picker.

## API surface
| Method | Path | Request | Response | Error codes |
|---|---|---|---|---|
| GET  | /api/networks                      | — | `list[dict]` | — |
| POST | /api/networks/create               | query `name`, `driver` | `OkResponse(id, name)` | `network.bad_name`, `network.bad_driver` |
| DEL  | /api/networks/{id}                 | — | `OkResponse` | `validation.bad_input`, `network.builtin_protected` |
| POST | /api/networks/{id}/connect         | query `container_id` | `OkResponse` | `validation.bad_input` |
| POST | /api/networks/{id}/disconnect      | query `container_id` | `OkResponse` | `validation.bad_input` |
| POST | /api/networks/prune                | — | `{deleted}` | — |

## Security model
- `@secure_route.read(RATE.READ)` for list; `@secure_route.mutate` for
  everything else.
- CSRF: enforced by `.mutate`.
- Rate limit tiers: `RATE.READ` / `RATE.WRITE` / `RATE.BURST`.
- Audit events: `network.created`, `network.deleted`, `network.connect`,
  `network.disconnect`, `networks.pruned`.
- Threat model: disconnecting a container from its only network isolates
  it silently. The UI prompts for explicit confirmation before every
  `Disconnect` click and warns that the container may lose all network
  access; the server does not currently gate this.

## Tests
- Unit: `tests/test_networks.py` + `tests/test_coverage_volumes_networks.py`.
- Route contract: automatic via `tests/test_route_contract.py`.

## Troubleshooting
- "Cannot delete default network" → you're trying to delete `bridge` /
  `host` / `none` — those are Docker-managed.
- Container won't connect → verify container id is valid hex (4–64
  chars), check if the network's driver accepts the container's
  platform.

## References
- Docker network documentation: https://docs.docker.com/engine/network/
