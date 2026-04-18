# Audit event catalogue

GENERATED FROM `skiff/contract/events.py`. Run
`python tools/gen_catalogues.py` to regenerate; CI `--check`
fails if this file drifts from the Python source.

Every `log.info("<event>", ...)` used for audit purposes appears
here with its severity, required/optional fields, and a one-line
intent for SIEM rule authors.

| Event | Severity | Required fields | Optional fields | Description |
|---|---|---|---|---|
| `api.request` | info | `method`, `path`, `status` | `remote`, `auth` | Generic classified event_type attached to `audit.api_access` lines for any HTTP request that doesn't match a domain-specific action pattern. Not emitted as a top-level `event`; appears as `event_type` on `audit.api_access` lines. |
| `app.dependency_versions` | info | — | `skiff`, `fastapi`, `docker`, `structlog`, `slowapi`, `pydantic` | Installed versions of direct dependencies at startup (supply-chain forensics). |
| `app.shutdown` | info | — | — | Process shutting down cleanly. |
| `app.started` | info | — | `version`, `docker_host`, `bind`, `profile` | FastAPI app finished startup. |
| `audit.api_access` | info | `channel`, `event_type`, `method`, `path`, `status`, `remote`, `auth` | `token_suffix`, `user`, `resource_type`, `resource_id` | One line per completed HTTP request, emitted by AuditLogMiddleware. `event_type` carries the classified action name (api.request, container.started, compose.up, image.pulled, rate_limit.exceeded, auth.denied, …) and is what SIEM rules typically filter on. `channel="audit"` distinguishes this from debug/info lines from the same structlog configuration. |
| `audit.field_extraction_failed` | warning | `name`, `error` | — | secure_route's audit_fields callable raised; fired with empty fields. |
| `audit.setup_failed` | info | `remote`, `reason` | — | A /api/setup attempt was rejected (bad token, locked, after-window). |
| `audit.setup_lockout` | warning | `remote`, `remaining` | — | Setup endpoint tripped the per-IP lockout threshold. |
| `audit.undeclared_event` | warning | `undeclared` | — | secure_route saw an audit name not in the catalogue; drift alert. |
| `audit.ws_exec` | info | `container`, `remote` | — | Exec WS opened. |
| `audit.ws_exec_disconnect` | info | `container`, `remote` | — | Exec WS closed by client. |
| `audit.ws_exec_input` | info | `container`, `remote`, `bytes` | — | Exec WS received client input. The emitter logs byte-count ONLY — command content is intentionally NOT captured, so pasted credentials (`export TOKEN=…`, sudo prompts, etc.) never land in the audit log. |
| `audit.ws_exec_input_oversize` | warning | `container`, `remote`, `bytes`, `limit` | — | Exec WS client sent a single input message larger than `_EXEC_MAX_INPUT_BYTES`. The socket is closed with code 4008 (policy-violation). Useful for SIEM rules that want to flag malformed / oversized paste activity. |
| `audit.ws_handshake_failed` | warning | `reason`, `remote` | `container` | WebSocket upgrade rejected during the handshake. `reason` is one of: `token_in_query` (bearer smuggled via ?token=…), `origin_denied` (Origin not in allowlist), `bad_container_id` (path validation failed), `auth_failed` (AUTH message absent or wrong token). Pair with HTTP `auth.denied` for a complete denied-access signal. |
| `audit.ws_logs` | info | `container`, `remote` | — | Log stream WS opened. |
| `auth.config_reset` | info | `old_suffix` | — | Operator reset SKIFF's runtime config (wipes tunnel + token). |
| `auth.denied` | warning | `method`, `path`, `status` | `remote`, `auth` | event_type carried on `audit.api_access` lines when a request failed the bearer-token check. Pair with `audit.ws_auth_lockout` for WS-specific brute-force signal. |
| `auth.reset_tunnel_cleanup_failed` | warning | `error` | — | Tunnel teardown during config-reset raised; state may be partial. |
| `auth.token_rotated` | info | `old_suffix`, `new_suffix` | — | Operator rotated the API token. Old suffix retained for audit correlation. |
| `build_cache.pruned` | info | `space_mb` | — | Docker build cache pruned. |
| `compose.down` | info | `project` | — | Compose stack torn down. |
| `compose.down_failed` | warning | `project`, `stderr` | — | compose down returned non-zero. |
| `compose.service_logs_failed` | warning | `project`, `service`, `error` | — | Per-service log fetch failed; aggregate view fell back. |
| `compose.service_restarted` | info | `project`, `service` | `container_id` | Single compose service restarted. |
| `compose.up` | info | `project` | — | Compose stack deployed. |
| `compose.up_failed` | warning | `project`, `stderr` | — | compose up returned non-zero; stderr truncated at 500 chars. |
| `compose.upload` | info | `project`, `services` | — | Compose YAML accepted; stack about to deploy. |
| `container.created` | info | `id`, `name`, `image` | `memory`, `cpus`, `ports`, `readonly_rootfs`, `inherit_from` | New container created via /api/containers/run. `inherit_from` carries the source container ID when the caller asked to copy env vars from an existing container. |
| `container.delete_queued` | info | `id`, `force`, `token_suffix` | — | Destructive delete queued under the undo window. |
| `container.deleted` | info | `id`, `force` | — | Container deleted (either direct or after undo window). |
| `container.killed` | info | `id`, `signal` | — | Container killed with explicit signal (not default SIGKILL). |
| `container.paused` | info | `id` | — | Container paused. |
| `container.removed` | info | — | `user`, `resource_type`, `resource_id`, `token_suffix` | Middleware audit of DELETE /api/containers/{id}. |
| `container.renamed` | info | `id`, `new_name` | — | Container renamed. |
| `container.replace_cleanup_failed` | warning | `replace_id`, `error` | — | Failed to remove the old container during a replace — manual cleanup needed. |
| `container.replace_noop` | warning | `id`, `replace_id` | — | Clone target differs from replace_id; replace skipped. |
| `container.replaced` | info | `new_id`, `old_id` | — | Clone replaced an older container in-place. |
| `container.restarted` | info | `id` | — | Container restarted. |
| `container.run` | info | — | `image`, `name`, `user`, `resource_type`, `resource_id`, `token_suffix` | Middleware audit of POST /api/containers/run. |
| `container.started` | info | `id` | — | Container started. |
| `container.stopped` | info | `id` | — | Container stopped. |
| `container.unpaused` | info | `id` | — | Container unpaused. |
| `container.updated` | info | `id`, `name`, `changes` | — | Container resource limits updated in place. |
| `docker.client_stale` | warning | `action` | — | Ping failed; client marked for reconnect. |
| `docker.connected` | info | `host` | — | Docker SDK client reconnected. |
| `docker.connection_failed` | error | `host`, `error` | — | Docker SDK could not connect on startup. |
| `docker.transient_error` | warning | `error` | `action` | Transient Docker SDK error absorbed by safe_docker_call. |
| `image.delete_queued` | info | `id`, `token_suffix` | — | Image deletion queued under the undo window. |
| `image.deleted` | info | `id` | — | Image deleted. |
| `image.pulled` | info | `image` | — | Image pulled from a registry. |
| `image.pushed` | info | `image` | — | Image pushed to a registry. |
| `image.tagged` | info | `id`, `repository`, `tag` | — | Image tag operation succeeded. |
| `network.connect` | info | `network`, `container` | — | Container attached to a network. |
| `network.created` | info | `name`, `driver` | — | Network created. |
| `network.deleted` | info | `id` | — | Network deleted. |
| `network.disconnect` | info | `network`, `container` | — | Container detached from a network. |
| `networks.pruned` | info | `count` | — | Unused networks pruned. |
| `rate_limit.exceeded` | warning | `method`, `path`, `status` | `remote`, `auth` | event_type carried on `audit.api_access` lines when slowapi refused the request with 429. SIEM rules can count per `remote` to detect brute-force or scraping patterns. |
| `security.docker_host_unencrypted` | warning | `host` | — | DOCKER_HOST points at an HTTP URL off-localhost — traffic is unencrypted. |
| `security.empty_api_token_env` | warning | — | `msg` | API_TOKEN env var is present but empty — treated as unset. |
| `security.no_api_token` | warning | — | `msg` | Server started without API_TOKEN — no auth enforced. |
| `security.no_registry_allowlist` | warning | — | `msg` | ALLOWED_REGISTRIES is empty — every registry is implicitly allowed. |
| `security.proxy_headers_untrusted` | warning | `msg` | — | Startup heuristic detected that uvicorn may be running with --proxy-headers enabled while TRUST_FORWARDED_HEADERS is off. In that configuration X-Forwarded-For can forge audit `remote` and rate-limit keys. Surface so the operator fixes the launch recipe. |
| `security.short_env_token` | warning | `msg` | — | API_TOKEN from the environment is shorter than the setup wizard's 16-character minimum. Emitted once at startup so operators see weak-token deployments in their boot logs. |
| `setup.configured` | info | `docker_host`, `registries` | — | First-boot setup completed successfully. |
| `system.pruned` | info | — | `containers`, `images`, `networks`, `volumes`, `space_mb` | Docker system prune completed. |
| `tunnel.manual_reconnect_required` | info | `socket`, `managed` | — | Operator hit Reconnect but the tunnel was not wizard-managed and the socket is down. SKIFF cannot re-open a tunnel it did not open itself (it never learned the SSH target). The client response includes the socket path so the operator can re-run `ssh -fNL <socket>:...`. |
| `tunnel.reconnect_noop` | info | `socket`, `managed` | — | Operator hit Reconnect on a manual-tunnel deployment whose socket is still reachable. No SSH work was done — the Docker client was invalidated so the next API call refreshes state. |
| `tunnel.reconnected` | info | `socket` | — | SSH tunnel re-established after being dropped. |
| `tunnel.started` | info | `target`, `socket` | — | SSH tunnel established. |
| `undo.cancelled` | info | `token_suffix`, `kind`, `id` | — | Operator cancelled the pending op before the window elapsed. |
| `undo.enqueued` | info | `token_suffix`, `kind`, `id`, `expires_in` | — | Destructive op deferred under the undo window. |
| `undo.fire_failed` | error | `token_suffix`, `kind`, `id`, `error` | — | Op fired but raised; forensics needed — caller already got 200. |
| `undo.fired` | info | `token_suffix`, `kind`, `id` | — | Undo window elapsed; op executed. |
| `undo.fired_on_shutdown` | info | `token_suffix`, `kind`, `id` | — | Server shutdown (SIGTERM, lifespan exit) flushed a pending undo op before the window would have elapsed naturally. Distinguishable from `undo.fired` so an incident reviewer can tell scheduled fires from shutdown-flush fires. |
| `undo.queue_full` | warning | `depth`, `kind` | — | Undo queue at cap; new deletions run synchronously. |
| `volume.created` | info | `name` | — | Volume created. |
| `volume.delete_queued` | info | `name`, `token_suffix` | — | Volume deletion queued under the undo window. |
| `volume.deleted` | info | `name` | — | Volume deleted. |
| `volumes.pruned` | info | `count` | — | Unused volumes pruned. |
| `ws.exec_error` | warning | `container`, `error` | — | Exec WS raised; connection closed. |
| `ws.logs_error` | warning | `container`, `error` | — | Log stream WS raised; connection closed. |
