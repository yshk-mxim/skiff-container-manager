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
1. Generate a new one: `openssl rand -hex 32`
2. Update `API_TOKEN` in `.env`
3. Restart the server: `systemctl restart skiff@$USER` or re-run `./run.sh`
4. Re-enter the new token in the browser login screen

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

1. Check the configured path is writable:
   ```bash
   ls -la $(dirname "$AUDIT_LOG")
   ```
2. The default path `/var/log/skiff-audit.jsonl` requires root or special permissions. Override it:
   ```
   # .env
   AUDIT_LOG=./audit.jsonl
   ```
3. A startup warning is printed if the path is not writable — check `journalctl -u skiff@$USER`.

---

## 429 Too Many Requests

You have hit a rate limit. The limits are per-endpoint (typically 60/minute for reads, 10–30/minute for mutations). Wait and retry.

If you are behind a reverse proxy and all requests appear to come from `127.0.0.1`, set `FORWARDED_ALLOW_IPS`:
```bash
FORWARDED_ALLOW_IPS="*" uvicorn skiff.app:app ...
```
This tells slowapi to trust the `X-Forwarded-For` header so the real client IP is used for rate limiting.

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

The setup wizard is only available within 15 minutes of server start. After that, `POST /api/setup` returns 403.

**Fix:** Restart the server. The 15-minute setup window reopens.

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
