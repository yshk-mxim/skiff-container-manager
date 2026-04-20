# API surface: containers

GENERATED FROM `skiff/routers/containers.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/containers.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/api/containers` | — | 60/minute | — | `list_containers` | Return all containers (running and stopped). |
| POST | `/api/containers/run` | `container.run` | 30/minute | ✓ | `run_container` | Create and start a new container. |
| DELETE | `/api/containers/{container_id}` | `container.removed` | 30/minute | ✓ | `delete_container` | Remove a container. Defaults to `undo=true` so a misclick (UI) or a |
| POST | `/api/containers/{container_id}/commit` | `container.committed` | 30/minute | ✓ | `container_commit` | Save a container's current filesystem state as a new image. |
| GET | `/api/containers/{container_id}/diff` | — | 60/minute | — | `container_diff` | Show filesystem changes in a container's writable layer since it was created. |
| DELETE | `/api/containers/{container_id}/files` | `container.cp_delete` | 30/minute | ✓ | `container_delete_file` | Remove a file or directory inside a container's filesystem. |
| GET | `/api/containers/{container_id}/files` | — | 60/minute | — | `container_get_file` | Stream a file or directory out of a container as a tar archive. |
| POST | `/api/containers/{container_id}/files` | `container.cp_put` | 30/minute | ✓ | `container_put_file` | Write a tar archive into a container's filesystem. |
| GET | `/api/containers/{container_id}/inspect` | — | 60/minute | — | `inspect_container` | Return detailed container metadata (config, state, mounts, network, health). |
| POST | `/api/containers/{container_id}/kill` | `container.killed` | 30/minute | ✓ | `kill_container` | Send a signal to a container (default SIGKILL). |
| GET | `/api/containers/{container_id}/logs` | — | 60/minute | — | `container_logs` | Fetch container log lines with optional time-range filtering. |
| GET | `/api/containers/{container_id}/logs/download` | — | 5/minute | — | `download_container_logs` | Download container logs as plain text. Auth via Authorization header. |
| GET | `/api/containers/{container_id}/logs/download.jsonl` | — | 5/minute | — | `download_container_logs_jsonl` | Download container logs as JSONL (one JSON object per line with timestamp + message). |
| GET | `/api/containers/{container_id}/ls` | — | 60/minute | — | `container_ls` | List a directory inside a container. |
| POST | `/api/containers/{container_id}/pause` | `container.paused` | 30/minute | ✓ | `pause_container` | Pause (freeze) all processes in a running container. |
| POST | `/api/containers/{container_id}/rename` | `container.renamed` | 30/minute | ✓ | `rename_container` | Rename a container. |
| POST | `/api/containers/{container_id}/restart` | `container.restarted` | 30/minute | ✓ | `restart_container` | Restart a container. |
| POST | `/api/containers/{container_id}/start` | `container.started` | 30/minute | ✓ | `start_container` | Start a stopped container. |
| GET | `/api/containers/{container_id}/stats` | — | 60/minute | — | `container_stats` | Return real-time CPU, memory, network, and disk I/O stats. |
| POST | `/api/containers/{container_id}/stop` | `container.stopped` | 30/minute | ✓ | `stop_container` | Stop a running container gracefully (SIGTERM, then SIGKILL after timeout). |
| GET | `/api/containers/{container_id}/top` | — | 60/minute | — | `container_top` | List processes running inside a container (like docker top). |
| POST | `/api/containers/{container_id}/unpause` | `container.unpaused` | 30/minute | ✓ | `unpause_container` | Resume a paused container. |
| POST | `/api/containers/{container_id}/update` | `container.updated` | 30/minute | ✓ | `update_container` | Update a running or stopped container's live-mutable resource constraints. |
| POST | `/api/containers/{container_id}/upload` | `container.cp_put` | 30/minute | ✓ | `container_upload_file` | Multipart file upload — browser-friendly variant. Delegates to |
