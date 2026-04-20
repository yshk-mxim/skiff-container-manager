# Troubleshooting

Quick fixes for the most common problems.

---

## `/ready` returns 503 — "Container engine unreachable"

The app cannot connect to the Docker daemon.

**If using a local Docker Engine (Linux / macOS):**
```bash
# Check Docker is running
docker version

# macOS — Docker Desktop must be open
# Linux — start the daemon:
sudo systemctl start docker

# Confirm the socket exists
ls -la /var/run/docker.sock
```

**If using a remote host via SSH tunnel:**
```bash
# Re-open the tunnel
ssh -fNL /tmp/docker.sock:/var/run/docker.sock user@docker-host

# Confirm it worked
ls -la /tmp/docker.sock
DOCKER_HOST=unix:///tmp/docker.sock docker version
```

Then reload the page.

---

## 401 Unauthorized on all API calls

Your `API_TOKEN` is wrong or missing.

```bash
# Check the token the server started with
grep API_TOKEN .env

# Test with curl
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8080/api/containers
```

Rotate the token:

- **Session-only mode:** System page → Account → **Rotate API token**
  (generates, copies, swaps without a restart; session continues with the
  new token).
- **Env-configured mode:** generate `openssl rand -hex 32`, replace
  `API_TOKEN=` in `.env`, restart (rotate endpoint is disabled for
  env-configured servers by design).
- Re-enter the new token in the browser login page.

---

## 403 on mutating requests (POST / DELETE)

Missing the CSRF header. All mutating requests require:
```
X-Requested-With: ContainerManager
```

The browser UI adds this automatically. If you are scripting with curl, add:
```bash
curl -H "X-Requested-With: ContainerManager" ...
```

---

## CORS errors in browser (cross-origin request blocked)

Set `ALLOWED_ORIGINS` to match the exact URL you are accessing the app from, including the port:

```
# .env
ALLOWED_ORIGINS=http://my-workstation.example.com:8080
```

Do not use `*` — this disables CSRF protections.

---

## Image blocked — "not in allowed registries"

Only images whose name starts with one of the `ALLOWED_REGISTRIES` prefixes are permitted.

```
# .env — allow Docker Hub and GitHub Container Registry
ALLOWED_REGISTRIES=docker.io,ghcr.io

# GCP Artifact Registry
ALLOWED_REGISTRIES=us-docker.pkg.dev/my-project/
```

Short Docker Hub names (`nginx`, `redis`, `alpine`) are allowed when `docker.io` is in the list.

---

## Audit log not growing

1. Find the path SKIFF is actually writing to — it's logged on startup:
   ```bash
   # If you ran via run.sh / uvicorn directly, look at the startup stderr for:
   #   {"event": "app.started", ..., "audit_log": "/Users/you/Library/.../audit.jsonl"}
   python -c "from skiff.config import AUDIT_LOG_PATH; print(AUDIT_LOG_PATH)"
   ```
2. Defaults (no `AUDIT_LOG` env override) per platform, all writable without root:
   - macOS: `~/Library/Application Support/skiff/audit.jsonl`
   - Linux / WSL2: `$XDG_STATE_HOME/skiff/audit.jsonl` or `~/.local/state/skiff/audit.jsonl`
3. For production, override to a dedicated path:
   ```
   # .env
   AUDIT_LOG=/var/log/skiff-audit.jsonl
   ```
4. A startup WARNING is printed if the chosen path is not writable, then
   logging falls back to stdout only.

---

## 429 Too Many Requests

You have hit a rate limit. The limits are per-endpoint (typically 60/minute for reads, 10–30/minute for mutations). Wait and retry.

If you are behind a reverse proxy and all requests appear to come from `127.0.0.1`, set `TRUST_FORWARDED_HEADERS=true` AND restart uvicorn with the proxy-headers support enabled. Do this **only** when a trusted proxy (oauth2-proxy, Caddy, nginx) fronts SKIFF and sanitises `X-Forwarded-*` headers — otherwise any caller can forge their audit `remote` and rate-limit bucket key.

```bash
# Only when behind a trusted reverse proxy:
TRUST_FORWARDED_HEADERS=true \
  uvicorn skiff.app:app --host 127.0.0.1 --port 8080 \
    --proxy-headers --forwarded-allow-ips "127.0.0.1"
```

Without a trusted proxy, leave `TRUST_FORWARDED_HEADERS` unset (the default) and run with `--no-proxy-headers`. SKIFF's `StripForwardedHeadersMiddleware` then refuses to read any `X-Forwarded-*` header, so a forged value cannot reach rate-limit keying or the audit log.

---

## Compose deploy silently uses old file

Each project name stores exactly one compose file. If you do not upload a new file, the previous one is reused. Check the stored file timestamp in the Compose section of the UI before deploying.

---

## WebSocket exec / log stream disconnects immediately

1. Check that `ALLOWED_ORIGINS` includes the browser URL.
2. Check that the WebSocket path is correct:
   - Logs: `ws://host:8080/ws/logs/{container-id}`
   - Exec: `ws://host:8080/ws/exec/{container-id}`
3. The WS rate limit is 5 concurrent sessions per IP. Close unused sessions first.

---

## 403 on `/api/setup` — "Setup window expired"

The setup wizard is only callable within 5 minutes of server startup
(`SETUP_WINDOW_SECS`). After that, `POST /api/setup` returns 403.

**Fixes (in order of least to most disruptive):**

1. **Session-only mode, still have a valid token:** System page → Account
   → **Reset configuration**. Clears in-memory state AND re-opens the
   setup window without a restart.
2. **Env-configured mode (token set via `API_TOKEN`):** reset-config is
   disabled in this mode — update the env and restart.
3. **No access to the server:** restart the process:
   ```bash
   systemctl restart skiff@$USER   # or: kill the uvicorn process and re-run ./run.sh
   ```

---

## WebSocket closes immediately with code 4003 — "Session expired"

Your session (started when you first entered your token) has exceeded the 8-hour absolute timeout. The server rejected the auth token, and the browser will show a "Session expired" message.

**Fix:** Log out and log back in with your `API_TOKEN`. The session timer resets on re-authentication.

Note: Do NOT try to reconnect the WebSocket manually — the session is expired and reconnect attempts will continue to fail with `4003` until you log in again.

---

## WebSocket auth lockout — connections refused after repeated failures

After 3 failed WebSocket authentication attempts from the same IP, new WebSocket connections are blocked for 5 minutes. This protects against token-guessing attacks.

**Symptoms:** WebSocket connects but closes immediately with a 4003 or auth error, even with the correct token.

**Fix:** Wait 5 minutes for the lockout to expire, then reconnect. If this happens repeatedly with the correct token, check for a clock skew issue or a misbehaving client sending auth messages incorrectly.

---

## journalctl one-liners

```bash
# Follow live logs
journalctl -u skiff@$USER -f

# Last 100 lines
journalctl -u skiff@$USER -n 100

# Errors only
journalctl -u skiff@$USER -p err

# Since last restart
journalctl -u skiff@$USER -b
```
