# API surface: networks

GENERATED FROM `skiff/routers/networks.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/networks.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/api/networks` | — | 60/minute | — | `list_networks` | Return all Docker networks with IPAM config and attached containers. |
| POST | `/api/networks/create` | `network.created` | 30/minute | ✓ | `create_network` | Create a new Docker network. |
| POST | `/api/networks/prune` | `networks.pruned` | 10/minute | ✓ | `prune_networks` | Delete all unused networks. |
| DELETE | `/api/networks/{network_id}` | `network.deleted` | 30/minute | ✓ | `delete_network` | Remove a user-defined network (default networks are protected). |
| POST | `/api/networks/{network_id}/connect` | `network.connect` | 30/minute | ✓ | `connect_container_to_network` | Attach a container to a network. |
| POST | `/api/networks/{network_id}/disconnect` | `network.disconnect` | 30/minute | ✓ | `disconnect_container_from_network` | Detach a container from a network. |
| GET | `/api/networks/{network_id}/inspect` | — | 60/minute | — | `inspect_network` | Return the full Docker inspect payload for a network. |
