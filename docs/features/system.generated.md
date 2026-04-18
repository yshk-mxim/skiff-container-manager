# API surface: system

GENERATED FROM `skiff/routers/system.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/system.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/` | — | — |  | `index` | Serve the SPA frontend. |
| GET | `/LICENSE` | — | — |  | `license_file` | Serve the MIT LICENSE file. |
| GET | `/api/config` | — | 60/minute | — | `get_config` | Return non-secret server configuration for the UI. |
| GET | `/api/connect-snippets` | — | 60/minute | — | `connect_snippets` | Return rendered per-tool snippets for the Connect-external-tool panel. |
| GET | `/api/docs` | — | 120/minute | — | `api_docs_landing` | Discoverability landing page for the OpenAPI spec (CSP-safe). |
| POST | `/api/profile/enter-reviewer` | — | 30/minute | ✓ | `enter_reviewer_mode` | One-way runtime switch into reviewer (read-only) profile. |
| GET | `/api/system/df` | — | 5/minute | — | `system_disk_usage` | Return disk usage breakdown for images, containers, volumes, and build cache. |
| GET | `/api/system/info` | — | 60/minute | — | `system_info` | Return Docker engine version, OS, hardware, and container counts. |
| GET | `/api/system/metrics` | — | 60/minute | — | `system_metrics` | Prometheus text-format metrics snapshot. |
| POST | `/api/system/prune` | — | 10/minute | ✓ | `system_prune` | Remove all stopped containers, dangling images, and unused networks. |
| POST | `/api/system/prune-build-cache` | — | 10/minute | ✓ | `prune_build_cache` | Clear Docker build cache and return the amount of space reclaimed. |
