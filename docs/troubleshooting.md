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
