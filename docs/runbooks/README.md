# SKIFF Runbooks — Recovery and Operational Procedures

Every runbook is a concrete step-by-step for a specific "stuck" scenario.
No theory, no background — get unstuck first, learn why second (link at the
end of each section).

---

## Contents

1. [I lost my API token](#1-i-lost-my-api-token)
2. [Server won't start](#2-server-wont-start)
3. [Audit log is empty / missing](#3-audit-log-is-empty--missing)
4. [SSH tunnel drops every few minutes](#4-ssh-tunnel-drops-every-few-minutes)
5. [Token rotated and I'm locked out](#5-token-rotated-and-im-locked-out)
6. [Setup wizard keeps reappearing after config](#6-setup-wizard-keeps-reappearing-after-config)
7. [Docker Desktop is running but SKIFF says unreachable](#7-docker-desktop-is-running-but-skiff-says-unreachable)
8. [Compose stack stays in "stopped" state after deploy](#8-compose-stack-stays-in-stopped-state-after-deploy)
9. [Container keeps exiting with code 1 immediately after start](#9-container-keeps-exiting-with-code-1-immediately-after-start)
10. [I need to hand the running server off to another operator](#10-i-need-to-hand-the-running-server-off-to-another-operator)

---

## 1. I lost my API token

You had the token in the browser's sessionStorage, closed the tab, and now
you can't sign back in. Which recovery works depends on how you set SKIFF up
in the first place:

### 1a. You used "Save .env & Continue" in the wizard

```bash
grep '^API_TOKEN=' .env
# or if you moved it:
find ~ -name '.env' -exec grep -l '^API_TOKEN=' {} \;
```

Paste the value into the sign-in page. Done.

### 1b. You chose "In-memory only" (session-only mode) — the token was never saved to disk

The server still has the token in memory, but there is no way to retrieve
it without restarting the process. Two options, both require access to the
host running SKIFF:

**Option 1 — Restart with a new token, no wizard detour.** Kill the server,
export `API_TOKEN` in the environment, and start it again. The server sees
`from_env=true` and goes straight to the login page:

```bash
# Find the running process
pgrep -fl uvicorn
# Kill it (adjust PID)
kill <PID>
# Relaunch with a known token (generate one if you don't have one)
export API_TOKEN="$(openssl rand -hex 32)"
uvicorn skiff.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

Open the UI → sign in with the new token. No wizard shown because `from_env`
is true after env-configured setups.

**Option 2 — Restart and run through the wizard again.** Kill the server
without setting `API_TOKEN`. On restart, the setup wizard reappears (the
5-minute setup window resets on startup):

```bash
kill <PID>
uvicorn skiff.app:app --host 127.0.0.1 --port 8080
# Browse to http://127.0.0.1:8080 → wizard shows
```

### 1c. You have another operator still logged in

Ask them to rotate the token via **System → Account → Rotate API token**,
then share the new value over a secure channel. Old sessions are
invalidated immediately on rotation.

---

## 2. Server won't start

### 2a. Port already bound

```
[Errno 48] Address already in use
```

Find and kill whatever's on the port:

```bash
lsof -iTCP:8080 -sTCP:LISTEN | awk 'NR>1 {print $2}' | xargs kill
```

### 2b. `API_TOKEN must be at least 16 characters`

The wizard-minimum applies at startup too. Set a stronger token:

```bash
export API_TOKEN="$(openssl rand -hex 32)"
```

### 2c. `ALLOWED_ORIGINS must not contain '*'`

`'*'` disables CSRF protection; SKIFF refuses to start with it. Set the
exact origin of your browser client:

```bash
export ALLOWED_ORIGINS="http://127.0.0.1:8080"
# Or for a reverse-proxy setup:
export ALLOWED_ORIGINS="https://containers.example.com"
```

### 2d. Python version

```
ERROR: Package 'skiff' requires a different Python: 3.11.x not in '>=3.12'
```

Upgrade Python to 3.12 or 3.13. Using `pyenv`:

```bash
pyenv install 3.12.0
pyenv local 3.12.0
pip install -e .
```

---

## 3. Audit log is empty / missing

### 3a. Find where the log is

The default location is platform-specific and writable without root:

- **macOS:** `~/Library/Application Support/skiff/audit.jsonl`
- **Linux:** `$XDG_STATE_HOME/skiff/audit.jsonl` or `~/.local/state/skiff/audit.jsonl`
- **Override:** `$AUDIT_LOG` env var if set

Check what SKIFF actually chose:

```bash
curl -s http://127.0.0.1:8080/health > /dev/null
# Then in the server stderr, look for:
# "docker_host": "...", "bind": "127.0.0.1", "event": "app.started"
# No "WARNING: audit log path ... is not writable" line means the file is being written.
```

Or via Python:

```bash
python -c "from skiff.config import AUDIT_LOG_PATH; print(AUDIT_LOG_PATH)"
```

### 3b. File exists but is empty

Every API request produces an entry. Hit a logged endpoint:

```bash
curl -s http://127.0.0.1:8080/health
tail -5 "$(python -c 'from skiff.config import AUDIT_LOG_PATH; print(AUDIT_LOG_PATH)')"
```

If still empty, the write is probably going to the wrong path because of an
unexpected `$AUDIT_LOG` override. `unset AUDIT_LOG` and restart.

### 3c. You want the log in `/var/log` instead

```bash
# As root:
mkdir -p /var/log && touch /var/log/skiff-audit.jsonl
chown skiff:skiff /var/log/skiff-audit.jsonl
chmod 640 /var/log/skiff-audit.jsonl

# Restart SKIFF with:
export AUDIT_LOG=/var/log/skiff-audit.jsonl
```

---

## 4. SSH tunnel drops every few minutes

### Symptoms

- Containers load for a minute, then "Cannot reach Docker engine"
- The Reconnect button works but drops again soon

### Usual causes

1. **Remote sshd has `ClientAliveInterval` below what SKIFF's tunnel expects.**
   SKIFF sends keepalives every 30 s (`TUNNEL_SERVER_ALIVE_INTERVAL`); if
   the remote kills idle sessions faster, raise the remote's limit:
   ```
   # /etc/ssh/sshd_config on the Docker host
   ClientAliveInterval 60
   ClientAliveCountMax 3
   ```
2. **Intermediate NAT/firewall connection table timeout** — some corporate
   networks drop idle TCP after 5 minutes. Workaround: browse SKIFF
   regularly (the log-streaming WebSocket's own ping helps keep the path
   warm), or route through a different network.
3. **Remote host swapping to sleep** — laptops on battery often sleep after
   15 min. Prevent with `caffeinate` (macOS) or `systemd-inhibit` (Linux).

### Recovery

Click the **Reconnect tunnel** button on the Containers page. Behaviour
depends on how the tunnel was opened:

- **Wizard-managed tunnel** — SKIFF re-opens the ControlMaster tunnel
  using the SSH target stored at setup time. Common case if you used
  the setup wizard to connect.
- **Manual `ssh -fNL` tunnel, still live** — SKIFF detects the socket
  is reachable, invalidates its stale Docker client, and returns
  `tunnel.already_connected`. The next API call flows through the
  existing socket.
- **Manual `ssh -fNL` tunnel, dropped** — SKIFF cannot re-open a
  tunnel it did not open itself (it never learned the SSH target;
  accepting one at runtime would widen the attack surface). The
  button returns `tunnel.manual_reconnect_required` with the socket
  path. Re-run your original `ssh -fNL <socket>:/var/run/docker.sock
  user@docker-host` command; SKIFF picks up the restored socket on
  its next call.

Use the `ssh -o ExitOnForwardFailure=yes -o ConnectTimeout=30 …`
options if you need to script reconnection externally.

---

## 5. Token rotated and I'm locked out

You clicked **Rotate API token** and the new value didn't make it to
`sessionStorage` (typos, clipboard failure, browser crash mid-flow).

**If you still have a recent browser tab with the old session open:**
sessionStorage in that tab may still have the *new* token — the rotate
success path writes it immediately. Open DevTools → Application →
Session Storage → copy `api_token`.

**If every tab is dead:** the only recovery is to restart the server.
See §1b Option 1.

**Prevention:** the Save button in the Rotate modal is disabled until you
click Copy, which selects the text and triggers the clipboard API. If the
browser rejects clipboard write (iframe, non-HTTPS insecure context), the
button label flips to "Select + ⌘C" and the token field is highlighted so
you can copy manually. See `skiff/static/app.js:_showRotateTokenModal`.

---

## 6. Setup wizard keeps reappearing after config

### 6a. "Continue (In-memory only)" was used but the server keeps restarting

Session-only configs live in process memory; any restart clears them.
Switch to persistent config:

```bash
export API_TOKEN="..."
export DOCKER_HOST="..."
```

Or use "Save .env" next time, which writes a ready-to-source `.env` file.

### 6b. Uvicorn auto-reload keeps restarting the process

`--reload` re-launches the server on every file change, wiping in-memory
config. Only use `--reload` for code development, not for the instance
you're trying to configure through the wizard.

---

## 7. Docker Desktop is running but SKIFF says unreachable

Likely causes:

1. **DOCKER_HOST points at the wrong socket.** Docker Desktop on macOS ≥
   4.25 also creates `~/.docker/run/docker.sock`. The symlink at
   `/var/run/docker.sock` should work, but if it doesn't:
   ```
   export DOCKER_HOST="unix://$HOME/.docker/run/docker.sock"
   ```
2. **Docker Desktop is starting / restarting.** Its socket exists but the
   daemon isn't listening yet. Wait 10–20 seconds and retry.
3. **SKIFF is running inside a container without the host socket mounted.**
   Add `-v /var/run/docker.sock:/var/run/docker.sock` to the SKIFF launch
   command (and be aware this grants root-equivalent access — ONLY on a
   trusted host).

---

## 8. Compose stack stays in "stopped" state after deploy

The compose YAML was accepted but the services didn't start. Check:

1. **Per-service logs:** click the service's Logs button on the Compose
   page. Most failures (image pull errors, `command not found`, bad env)
   show up in stderr here.
2. **Engine-level check:**
   ```bash
   docker ps -a --filter "label=com.docker.compose.project=<your-project>"
   ```
3. **Image not on the allowlist:** the compose validator accepts the file
   but `docker compose up` fails to pull. Check `ALLOWED_REGISTRIES`
   matches every `image:` line's registry.

---

## 9. Container keeps exiting with code 1 immediately after start

The Start button shows an "exited immediately" toast. Most common causes:

- **Read-only rootfs default + app that writes to rootfs.** Covered by
  default tmpfs on `/tmp`, `/run`, `/var/run`, `/var/cache` — but nginx-
  family images need those. Redis/Postgres want to write to
  `/var/lib/<dbname>` which is outside the default tmpfs — either
  uncheck "Read-only root filesystem" in the Run modal or provide a named
  volume at `/var/lib/postgres` etc.
- **Missing config:** some images require env vars (`POSTGRES_PASSWORD`,
  `MYSQL_ROOT_PASSWORD`). Check logs.
- **One-shot command:** `alpine echo hello` exits after printing. Expected.

---

## 10. I need to hand the running server off to another operator

System page → Account → **Reset configuration**. This clears the in-memory
API token, Docker host, and registry list, stops the managed SSH tunnel,
and reopens the 5-minute setup window so the next visitor runs the wizard
fresh. Every current user gets signed out.

Disabled when `API_TOKEN` came from the environment — for env-managed
setups, hand off `.env` through a secure channel instead.
