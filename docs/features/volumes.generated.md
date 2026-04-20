# API surface: volumes

GENERATED FROM `skiff/routers/volumes.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/volumes.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/api/volumes` | — | 60/minute | — | `list_volumes` | Return all named volumes and which containers are using each one. |
| POST | `/api/volumes/create` | `volume.created` | 30/minute | ✓ | `create_volume` | Create a new named volume. |
| POST | `/api/volumes/prune` | `volumes.pruned` | 10/minute | ✓ | `prune_volumes` | Delete all unused named volumes. Default (`undo=true`) queues the |
| DELETE | `/api/volumes/{volume_name}` | — | 30/minute | ✓ | `delete_volume` | Remove a named volume. With `undo=true`, removal is delayed and the |
| DELETE | `/api/volumes/{volume_name}/browse` | `volume.browse_closed` | 30/minute | ✓ | `volume_browse_close` | Stop + remove a helper created by POST /browse. Refuses to |
| POST | `/api/volumes/{volume_name}/browse` | `volume.browse_opened` | 30/minute | ✓ | `volume_browse_open` | Return a (container_id, mount_path) the UI can use with the |
| GET | `/api/volumes/{volume_name}/inspect` | — | 60/minute | — | `inspect_volume` | Return detailed volume metadata: driver options, scope, status, usage. |
