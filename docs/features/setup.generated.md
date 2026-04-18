# API surface: setup

GENERATED FROM `skiff/routers/setup.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/setup.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| POST | `/api/auth/reset-config` | — | 5/minute | ✓ | `reset_config` | Clear in-memory state + stop tunnel + reopen the setup window. |
| POST | `/api/auth/rotate-token` | — | 5/minute | ✓ | `rotate_token` | Replace the in-memory API_TOKEN. Old token stops working on success. |
| POST | `/api/setup` | — | 5/minute | ✓ | `do_setup` | Commit the server's in-memory configuration from the wizard. |
| GET | `/api/setup-state` | — | — |  | `setup_state` | Return wizard state: configured? from_env? tunnel reachable? |
| GET | `/api/setup/probe-docker` | — | 5/minute | — | `probe_docker` | Probe the curated local-socket allowlist; return which responded. |
| DELETE | `/api/setup/tunnel` | — | 5/minute | ✓ | `stop_tunnel_endpoint` | Stop the managed SSH tunnel (wizard flow only; AUTH'd path is reset-config). |
| POST | `/api/setup/tunnel` | — | 5/minute | ✓ | `start_tunnel` | Start the managed SSH ControlMaster tunnel. Wizard-only. |
| POST | `/api/tunnel/reconnect` | — | 5/minute | ✓ | `tunnel_reconnect` | Re-open or probe the SSH tunnel — handles both wizard-managed |
| GET | `/api/tunnel/status` | — | 60/minute | — | `tunnel_status` | Return the tunnel's live state, whether wizard-managed or manual. |
