# API surface: undo_routes

GENERATED FROM `skiff/routers/undo_routes.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/undo_routes.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| POST | `/api/undo/{token}` | — | 30/minute | ✓ | `undo_operation` | Cancel a pending destructive operation queued by a DELETE ?undo=1 call. |
