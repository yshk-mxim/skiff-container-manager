# Feature: Image management

## What it is
List local images, pull from allowed registries, push, tag, inspect
(including layer history), and delete. Docker Hub search is proxied
through the server so the UI isn't blocked by CORS.

## Who it's for
Primary: `dev`. Secondary: all other personas.

## UI flow
- Sidebar → *Images*.
- Pull modal: image name + tag (defaulting to `latest`). Registry
  autocomplete proxies `/api/registry/search`.
- Row actions: *Inspect*, *Tag*, *Push*, *Delete* (with optional undo).

## API surface
| Method | Path | Request | Response | Error codes |
|---|---|---|---|---|
| GET  | /api/registry/search?q=...           | — | `{results: [...]}` | — |
| GET  | /api/registry/tags?image=...         | — | `{image, tags}` | — |
| GET  | /api/images                          | — | `list[dict]` | — |
| GET  | /api/images/allowed                  | — | `list[dict]` | — |
| POST | /api/images/pull?image=...           | — | `OkResponse(image)` | `image.registry_blocked`, `image.pull_failed` |
| POST | /api/images/{id}/tag?repository=...  | — | `OkResponse` | `image.bad_id`, `image.registry_blocked` |
| POST | /api/images/push?image=...           | — | `OkResponse(image)` | `image.push_failed` |
| DEL  | /api/images/{id}?undo=true           | — | `UndoableResponse` or `OkResponse` | `image.bad_id`, `image.not_found` |
| GET  | /api/images/{id}/inspect             | — | `dict` including `history` | `image.bad_id`, `image.not_found` |

## Security model
- `@secure_route.read(RATE.READ)` for list / inspect / registry search.
- `@secure_route.mutate(RATE.WRITE, audit="image.X")` for pull / tag /
  push / delete.
- Registry allowlist (via `validate_image_registry`) gates every mutating
  path — a pull from `evil.example.com/...` is rejected with
  `image.registry_blocked` before any network I/O.
- Audit events: `image.pulled`, `image.pushed`, `image.tagged`,
  `image.delete_queued`, `image.deleted`.
- Threat model: tag operations can rewrite existing repository:tag
  pointers — SKIFF doesn't treat that as destructive, but a future
  enhancement could undo-wrap tag operations on existing tags.

## Tests
- Unit: `tests/test_images.py`, `tests/test_coverage_images.py`.
- Route contract: automatic.

## Troubleshooting
- "registry 'X' is not in the allowlist" → add X to `ALLOWED_REGISTRIES`.
- "Pull failed" → `docker.io` rate limits anonymous pulls to 100 per 6h;
  set a `~/.docker/config.json` with credentials on the Docker host.
- Inspect shows empty `history` → the image comes from `scratch` or the
  SDK couldn't reach the daemon — check `/health`.
