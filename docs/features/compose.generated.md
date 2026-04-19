# API surface: compose

GENERATED FROM `skiff/routers/compose.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/compose.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| POST | `/api/compose/down` | `compose.down` | 5/minute | ✓ | `compose_down` | Tear down a running Compose stack. |
| GET | `/api/compose/stacks` | — | 60/minute | — | `list_compose_stacks` | List running compose stacks by inspecting container labels. |
| POST | `/api/compose/up` | `compose.up` | 5/minute | ✓ | `compose_up` | Deploy a Compose stack (upload a new file or redeploy an existing one). |
| GET | `/api/compose/{project_name}/download` | — | 60/minute | — | `compose_download_yaml` | Serve the stored `docker-compose.yml` for a deployed project. |
| GET | `/api/compose/{project_name}/logs` | — | 60/minute | — | `compose_project_logs` | Aggregated logs for a compose project, optionally filtered to one service. |
| POST | `/api/compose/{project_name}/pull` | `compose.pulled` | 30/minute | ✓ | `compose_pull` | Pull the latest versions of every service image in a stack. |
| POST | `/api/compose/{project_name}/scale` | `compose.scaled` | 30/minute | ✓ | `compose_scale` | Scale a service to N replicas. |
| POST | `/api/compose/{project_name}/services/{service_name}/restart` | `compose.service_restarted` | 30/minute | ✓ | `compose_service_restart` | Restart every container belonging to a single service in a compose stack. |
| POST | `/api/compose/{project_name}/start` | `compose.started` | 30/minute | ✓ | `compose_start` | Bring a stopped stack back up. Paired with `/stop`. |
| POST | `/api/compose/{project_name}/stop` | `compose.stopped` | 30/minute | ✓ | `compose_stop` | Stop every service container in a stack without removing them. |
