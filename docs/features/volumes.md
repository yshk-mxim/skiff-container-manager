# Feature: Named volume management

## What it is
Create, list, inspect, delete, and prune Docker named volumes from the
SKIFF UI. Volume deletion can be deferred through the 5-second undo
window (undo queue) so a misclick is recoverable.

## Who it's for
Primary: `dev`, `homelab`. Secondary: `sre` (bulk prune).

## UI flow
- Sidebar → *Volumes* (`pages/volumes.js` registers the page).
- Create button → modal with a `name` field (validated against
  `_VOLUME_NAME_RE`).
- Each row shows driver, mountpoint, in-use/container list.
- Row → *Remove* opens a confirmation; if the operator chose the
  undo window the toast includes an *Undo* button.
- Row → *Inspect* opens a detail modal with scope, options, UsageData.

User-facing strings (authoritative values live in
`skiff/static/strings.en.js`; this list is informational):
- `volumes.confirm.remove` — confirmation prompt
- `undo.button` — "Undo" action label
- `volumes.toast.removed` / `undo.toast` — outcome toasts

## API surface
| Method | Path | Request | Response | Error codes |
|---|---|---|---|---|
| GET  | /api/volumes                     | — | `list[dict]` | — |
| GET  | /api/volumes/{name}/inspect      | — | `dict` | `volume.bad_name`, `volume.not_found` |
| POST | /api/volumes/create?name=...     | query param | `OkResponse(name)` | `volume.bad_name` |
| DEL  | /api/volumes/{name}?undo=true    | — | `UndoableResponse` or `OkResponse` | `volume.bad_name`, `volume.not_found` |
| POST | /api/volumes/prune               | — | `{deleted, space_reclaimed_mb}` | — |

## Security model
- Decorator: `@secure_route.{read,mutate}` from `skiff/secure.py`.
- CSRF: enforced by `.mutate` on POST/DELETE.
- Rate limits: `RATE.READ` for lists, `RATE.WRITE` for create/delete,
  `RATE.BURST` for prune.
- Audit events (declared in `skiff/contract/events.py`):
  `volume.created`, `volume.delete_queued`, `volume.deleted`,
  `volumes.pruned`.
- Threat model: volume deletion with `force=true` can unmount a
  container's data — undo window exists precisely to recover from this.

## Tests
- Unit: `tests/test_volumes.py`, `tests/test_coverage_volumes_networks.py`
  (use `tests/factories.py::make_volume`).
- Property: `tests/test_fuzz.py` exercises name validator boundaries.
- Route contract: `tests/test_route_contract.py` verifies each mutating
  route has CSRF; `tests/test_contract.py` verifies catalogue
  consistency.
- E2E: `tests/test_e2e_ui.py` (volume section) + `tests/test_e2e_ui_gaps.py`.

## Troubleshooting
- "Invalid volume name" → matches `[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}`;
  reject paths with `/`.
- Volume still listed after delete → `undo=true` in the query string
  means it's still pending. Wait 5 seconds or hit *Undo*.

## References
- Undo queue implementation: `skiff/undo.py`.
- Docker volume documentation: https://docs.docker.com/storage/volumes/
