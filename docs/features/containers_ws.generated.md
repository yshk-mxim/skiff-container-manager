# API surface: containers_ws

GENERATED FROM `skiff/routers/containers_ws.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/containers_ws.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
|  | `/ws/exec/{container_id}` | — | — |  | `exec_shell` | Open an interactive shell in a container over WebSocket. |
|  | `/ws/logs/{container_id}` | — | — |  | `stream_logs` | Stream container logs over WebSocket. |
