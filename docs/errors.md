# Error code catalogue

GENERATED FROM `skiff/contract/errors.py`. Run
`python tools/gen_catalogues.py` to regenerate; CI `--check`
fails if this file drifts from the Python source.

Every 4xx/5xx response carries `detail = {code, message, help?}`.
Client switches on `code` (stable); displays `message` (human).

| Code | Status | Message template | Help |
|---|---|---|---|
| `auth.csrf_invalid` | 403 | invalid X-Requested-With header value |  |
| `auth.csrf_missing` | 403 | missing X-Requested-With header |  |
| `auth.env_managed` | 403 | API_TOKEN is managed via environment variable — update .env and restart to rotate |  |
| `auth.invalid_token` | 401 | invalid token |  |
| `auth.missing_token` | 401 | authentication required |  |
| `auth.not_configured` | 503 | server not configured — set API_TOKEN before accessing this endpoint |  |
| `auth.rate_limited` | 429 | too many requests |  |
| `auth.reset_env_managed` | 403 | cannot reset: server is configured via environment variables |  |
| `auth.reviewer_read_only` | 403 | reviewer profile is read-only; mutations are disabled | docs/dev/personas.md |
| `auth.session_expired` | 401 | session expired; please sign in again |  |
| `auth.setup_locked` | 429 | setup endpoint is temporarily locked |  |
| `auth.token_unchanged` | 400 | new_token is identical to the current token |  |
| `compose.bad_services` | 400 | invalid services section |  |
| `compose.bad_yaml` | 400 | invalid compose YAML |  |
| `compose.deploy_failed` | 400 | compose up failed |  |
| `compose.file_missing` | 400 | no compose file uploaded and no existing file found for this project |  |
| `compose.forbidden_key` | 400 | compose key '{key}' is not allowed |  |
| `compose.not_found` | 404 | compose project not found |  |
| `compose.project_dir_create_failed` | 500 | failed to create project directory |  |
| `compose.service_bad_ipc` | 400 | service '{svc_name}': ipc mode '{ipc_mode}' is not allowed |  |
| `compose.service_bad_network_mode` | 400 | service '{svc_name}': network_mode '{net_mode}' is not allowed |  |
| `compose.service_forbidden_key` | 400 | service '{svc_name}': '{key}' is not allowed for security reasons |  |
| `compose.service_host_pid` | 400 | service '{svc_name}': pid mode 'host' is not allowed |  |
| `compose.service_host_volume` | 400 | service '{svc_name}': host path mounts are not allowed |  |
| `compose.service_not_mapping` | 400 | service '{svc_name}' must be a mapping |  |
| `compose.teardown_failed` | 400 | compose down failed |  |
| `compose.timeout` | 504 | compose operation timed out |  |
| `compose.too_large` | 400 | compose file exceeds size limit |  |
| `container.bad_id` | 400 | invalid container id |  |
| `container.bad_name` | 400 | invalid container name (alphanumeric, dots, hyphens, underscores) |  |
| `container.bad_signal` | 400 | unsupported signal |  |
| `container.command_too_long` | 400 | command too long (max {limit} chars) |  |
| `container.conflict` | 409 | container conflict (already started/stopped?) |  |
| `container.cpu_above_cap` | 400 | cpus exceeds cap of {cap} |  |
| `container.cpu_shares_bad` | 400 | cpu_shares must be an integer in [2, 1024] |  |
| `container.label_bad` | 400 | invalid label |  |
| `container.label_count_exceeds_cap` | 400 | too many labels (max {limit}) |  |
| `container.limit_reached` | 400 | container limit ({limit}) reached |  |
| `container.memory_above_cap` | 400 | memory exceeds cap of {cap} |  |
| `container.memory_below_minimum` | 400 | memory must be >= {minimum} bytes (Docker minimum) |  |
| `container.memory_uncap_unsupported` | 400 | Docker Engine does not support removing a memory cap on a running container; recreate the container with no memory limit instead. | docs/api-reference.md#post-apicontainersidupdate |
| `container.name_taken` | 409 | name '{name}' is already in use |  |
| `container.not_found` | 404 | container {id} not found |  |
| `container.op_failed` | 400 | container operation failed |  |
| `container.pids_limit_bad` | 400 | pids_limit must be an integer in [1, {cap}] |  |
| `container.port_count_exceeds_cap` | 400 | too many port mappings (max {limit}) |  |
| `container.port_format` | 400 | invalid port format |  |
| `container.port_host_privileged` | 400 | host port {port} is privileged (< {threshold}) |  |
| `container.restart_policy_shape` | 400 | restart_policy must be an object |  |
| `container.restart_retry_bad` | 400 | MaximumRetryCount must be an integer in [0, {cap}] |  |
| `container.signal_bad` | 400 | unsupported signal |  |
| `container.stats_timeout` | 504 | stats call timed out |  |
| `container.update_no_fields` | 400 | no updatable fields provided |  |
| `container.volume_format` | 400 | invalid volume spec |  |
| `container.volume_host_path_blocked` | 400 | host path mounts are not allowed — use named volumes only |  |
| `docker.sdk_error` | 500 | Docker daemon returned {status}: {message} | docs/audit-events.md#docker-client-events |
| `image.bad_id` | 400 | invalid image id |  |
| `image.not_found` | 404 | image not found |  |
| `image.prune_failed` | 400 | image prune failed |  |
| `image.pull_failed` | 400 | image pull failed |  |
| `image.pull_timed_out` | 504 | image pull timed out |  |
| `image.push_failed` | 400 | image push failed |  |
| `image.push_timed_out` | 504 | image push timed out |  |
| `image.registry_blocked` | 400 | registry '{registry}' is not in the allowlist |  |
| `image.registry_search_failed` | 502 | registry search failed |  |
| `image.tag_fetch_failed` | 502 | tag fetch failed |  |
| `network.bad_driver` | 400 | unsupported network driver |  |
| `network.bad_gateway` | 400 | invalid gateway |  |
| `network.bad_labels` | 400 | invalid network labels |  |
| `network.bad_name` | 400 | invalid network name |  |
| `network.bad_subnet` | 400 | invalid subnet |  |
| `network.builtin_protected` | 400 | built-in network cannot be removed |  |
| `network.not_found` | 404 | network not found |  |
| `resource.in_use` | 409 | resource is in use: {detail} |  |
| `resource.not_found` | 404 | resource not found |  |
| `setup.already_configured` | 409 | setup is already complete |  |
| `setup.already_done` | 403 | already configured |  |
| `setup.docker_host_required` | 400 | docker_host is required |  |
| `setup.env_managed` | 403 | server is configured via environment variables — setup endpoint disabled |  |
| `setup.probe_disabled` | 403 | probe endpoint disabled after setup completes |  |
| `setup.scheme_bad` | 400 | docker_host must use unix://, tcp://, or npipe:// scheme |  |
| `setup.ssh_target_bad` | 400 | ssh_target must be user@host |  |
| `setup.tcp_host_bad` | 400 | tcp:// docker_host must specify an IP address, not a hostname |  |
| `setup.tcp_port_bad` | 400 | tcp:// docker_host must include a valid port |  |
| `setup.token_bad_charset` | 400 | api_token contains characters HTTP Authorization can't carry | Use `openssl rand -hex 32` (or similar). Allowed: letters, digits, and `. _ ~ + / = -`. Unicode / bidi / control chars would travel in the HTTP header but can't be sent back on subsequent requests, silently locking the operator out. |
| `setup.token_too_short` | 400 | api_token must be at least {minimum} characters |  |
| `setup.window_expired` | 403 | setup window has expired; restart the server to re-enable |  |
| `system.debug_disabled` | 403 | debug endpoint disabled — set SKIFF_DEBUG_THREADS=1 on the server to enable |  |
| `system.docker_unreachable` | 503 | container engine unreachable |  |
| `system.method_not_allowed` | 405 | this route does not accept that HTTP method | Check the `Allow` header on the response for accepted methods. |
| `system.route_not_found` | 404 | no route matches this path + method | Check `docs/api-reference.md` or GET /api/openapi.json. |
| `system.tunnel_failed` | 502 | tunnel did not come up |  |
| `system.undo_not_found` | 404 | undo token not found or expired |  |
| `tunnel.already_connected` | 409 | Docker host is already reachable — no reconnect needed | The socket is present and a Docker ping succeeded. The server-side Docker client was invalidated so the next request opens a fresh connection. |
| `tunnel.manual_reconnect_required` | 503 | Docker host is unreachable. The tunnel was not opened by SKIFF so it can't be re-opened server-side — re-run your `ssh -fNL …` command (or equivalent) to restore the socket. | SKIFF only auto-reconnects tunnels it opened itself (via the setup wizard). A manual `ssh -fNL` tunnel needs to be re-opened by the operator. The DOCKER_HOST socket path is included in the response so you can pass it back to ssh. |
| `tunnel.not_configured` | 404 | no managed tunnel configured | The server has no stored SSH target. Run setup again via the wizard. |
| `validation.bad_cpu` | 400 | invalid cpu quantity |  |
| `validation.bad_env` | 400 | environment variable must be KEY=VALUE |  |
| `validation.bad_image_name` | 400 | invalid image name format |  |
| `validation.bad_input` | 400 | invalid input |  |
| `validation.bad_memory` | 400 | invalid memory quantity |  |
| `validation.bad_mount_target` | 400 | volume mount target must be an absolute path |  |
| `validation.bad_project_name` | 400 | invalid project name |  |
| `validation.bad_restart_policy` | 400 | unsupported restart policy |  |
| `validation.bad_tmpfs_shape` | 400 | tmpfs must be an object mapping paths to options |  |
| `validation.body_timeout` | 408 | request body not received within the allowed window | Raise `BODY_READ_TIMEOUT_SECS` on the server OR have your client send the full body in one shot — the timeout is a slow-POST defence, not a per-operation budget. |
| `validation.body_too_large` | 413 | request body exceeds size cap | Lower the payload or raise `MAX_BODY_BYTES` server-side. |
| `validation.mount_target_blocked` | 400 | mount target {path!r} is not permitted |  |
| `validation.path_traversal` | 400 | path traversal attempt rejected |  |
| `validation.tmpfs_bad_options` | 400 | invalid tmpfs options |  |
| `validation.tmpfs_bad_path` | 400 | invalid tmpfs path |  |
| `validation.tmpfs_path_blocked` | 400 | tmpfs on {path!r} is not permitted |  |
| `validation.tmpfs_size_exceeds_cap` | 400 | total tmpfs size {total_mb:.0f}MB exceeds cap ({max_total_mb}MB) |  |
| `validation.tmpfs_too_many` | 400 | too many tmpfs mounts (max {max_mounts}) |  |
| `volume.bad_driver` | 400 | unsupported volume driver |  |
| `volume.bad_driver_opts` | 400 | invalid volume driver options |  |
| `volume.bad_labels` | 400 | invalid volume labels |  |
| `volume.bad_name` | 400 | invalid volume name |  |
| `volume.in_use` | 409 | volume is in use |  |
| `volume.not_found` | 404 | volume not found |  |
| `ws.connections_exhausted` | 429 | too many WebSocket connections from this IP |  |
