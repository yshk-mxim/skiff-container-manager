# API surface: images

GENERATED FROM `skiff/routers/images.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/images.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/api/images` | — | 60/minute | — | `list_images` | Return all locally available Docker images. |
| GET | `/api/images/allowed` | — | 60/minute | — | `list_allowed_images` | Return images from allowed registries only. |
| POST | `/api/images/prune` | `image.pruned` | 30/minute | ✓ | `prune_images` | Remove dangling (untagged) images. With `dangling_only=false`, also |
| POST | `/api/images/pull` | `image.pulled` | 30/minute | ✓ | `pull_image` | Pull an image from an allowed registry. |
| POST | `/api/images/push` | `image.pushed` | 30/minute | ✓ | `push_image` | Push an image to an allowed registry. |
| DELETE | `/api/images/{image_id}` | — | 30/minute | ✓ | `delete_image` | Remove a local image by ID. With `undo=true`, removal is delayed 5 s and |
| GET | `/api/images/{image_id}/inspect` | — | 60/minute | — | `inspect_image` | Return detailed image metadata and layer history. |
| POST | `/api/images/{image_id}/tag` | `image.tagged` | 30/minute | ✓ | `tag_image` | Tag an image with a new name. |
| GET | `/api/registry/search` | — | 60/minute | — | `registry_search` | Proxy Docker Hub image search to avoid browser CORS restrictions. |
| GET | `/api/registry/tags` | — | 60/minute | — | `registry_tags` | Fetch available tags for a Docker Hub image. |
| GET | `/api/templates` | — | 60/minute | — | `list_app_templates` | Return the prebuilt quick-start catalogue — nginx, postgres, etc. |
