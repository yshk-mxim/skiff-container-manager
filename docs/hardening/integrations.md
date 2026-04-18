# SKIFF Integrations — Server Side and Container Side

Each integration below has **two perspectives**:

- **Server side** — what SKIFF exposes (endpoints, sockets, env vars) and
  what an operator configures on the SKIFF host.
- **Container side** — how users of the launched containers reach their
  workloads. Often this is the bit people forget: SKIFF gets Docker talking,
  but you still need to tell your IDE / debugger / SIEM how to find the
  containers.

---

## 1. VSCode / Cursor / Windsurf — Dev Containers or Attach-to-Running

### Server side (what SKIFF provides)

SKIFF terminates its control plane on the port you've bound (default
`127.0.0.1:8080`). The Docker daemon itself is at `DOCKER_HOST`:

- **Local Docker Desktop:** `unix:///var/run/docker.sock`
- **SKIFF-managed SSH tunnel:** `unix:///tmp/skiff-docker.sock` (on the
  SKIFF host; not shared across users)

Any other client on the SKIFF host can reuse that same `DOCKER_HOST` —
it's a per-host unix socket, not a SKIFF-exclusive channel.

### Container side (what VSCode/Cursor/Windsurf does)

**Option A — Attach to a running container (simplest).** Open VSCode's
Docker extension or the Remote Containers extension, point it at the same
`DOCKER_HOST` SKIFF uses, and attach to any container SKIFF created:

```jsonc
// .vscode/settings.json
{
  "docker.host": "unix:///var/run/docker.sock",        // local Docker Desktop
  // OR for a SSH-tunnelled remote host (after SKIFF opened the tunnel):
  "docker.host": "unix:///tmp/skiff-docker.sock",
  // OR direct SSH without SKIFF's tunnel:
  "docker.host": "ssh://user@remote-host"
}
```

Command palette → **Dev Containers: Attach to Running Container…** → pick
the container SKIFF launched → VSCode opens a window attached inside it.

**Option B — Dev container workflow with SKIFF managing it.**

1. Create `.devcontainer/devcontainer.json` in your project
2. Launch the dev container via VSCode (it calls `docker run` against
   `DOCKER_HOST`)
3. SKIFF's UI shows the dev container in its list — use SKIFF for lifecycle
   actions (start/stop, Inspect, logs), keep VSCode for editing

### Ports

If your dev container exposes port 3000 and SKIFF maps it to host port
`18080`:

- **Local:** browse `http://127.0.0.1:18080`
- **Remote (SSH tunnel):** add a port forward to the SSH tunnel command:
  `ssh -L 18080:127.0.0.1:18080 user@remote` — now `http://127.0.0.1:18080`
  on your laptop reaches the remote container.

---

## 2. JetBrains IDEs (PyCharm, IntelliJ, GoLand, WebStorm)

### Server side

Same as VSCode — JetBrains uses the same `DOCKER_HOST`.

### Container side

IDE → **Settings → Build, Execution, Deployment → Docker** → New connection:

- **Unix socket:** paste the path (e.g., `/var/run/docker.sock` or
  `/tmp/skiff-docker.sock`)
- **TCP:** `tcp://127.0.0.1:2376` (requires TLS)
- **SSH:** `ssh://user@remote-host` — JetBrains opens its own tunnel

Once connected, the **Services** tool window shows containers/images/volumes
managed by SKIFF. Use SKIFF's UI for compose lifecycle (JetBrains' compose
support overlaps but SKIFF's sandbox is stricter).

---

## 3. `docker` CLI on the operator's workstation

### Server side

The SKIFF-managed tunnel socket is on the SKIFF host, not on the operator's
laptop. If you want the `docker` CLI on your laptop to also see the remote
Docker, set:

```bash
export DOCKER_HOST="ssh://user@remote-host"
docker ps        # works via CLI's built-in SSH connector
```

This bypasses SKIFF entirely — they coexist fine because both just talk to
the same remote daemon.

### Container side

Once `docker ps` works, every `docker exec -it <id> /bin/sh` etc. works
too — same containers SKIFF shows.

---

## 4. SIEM / log aggregation — Loki, ELK, Splunk, Datadog

All SKIFF audit events land in **`audit.jsonl`** (JSON lines, one event per
line). Location per platform:

- macOS: `~/Library/Application Support/skiff/audit.jsonl`
- Linux: `~/.local/state/skiff/audit.jsonl` (or `$XDG_STATE_HOME/skiff/...`)
- Production override: set `AUDIT_LOG=/var/log/skiff-audit.jsonl`

Fields per entry: `event_type`, `method`, `path`, `status`, `remote`,
`auth`, `token_suffix` (8 chars — never full token), optionally
`user` (from `X-Forwarded-User` when `TRUST_FORWARDED_HEADERS=true` and an OAuth2 proxy is in front — see [production.md §5](production.md#5-sso-via-identity-proxy-optional-multi-user)).

### 4a. Loki + Grafana Alloy (open-source, current pattern)

**Server side — forward the file:**

```alloy
loki.source.file "skiff_audit" {
  targets = [{
    __path__ = "/Users/*/Library/Application Support/skiff/audit.jsonl",
    job = "skiff",
  }]
  forward_to = [loki.process.parse_json.receiver]
}
loki.process "parse_json" {
  stage.json {
    expressions = { event_type = "", status = "", remote = "" }
  }
  stage.labels {
    values = { event_type = "", status = "" }
  }
  forward_to = [loki.write.default.receiver]
}
```

**Container side — browse:**

Grafana Explore → Loki datasource → `{job="skiff"}` as the LogQL query.
Filter by fields: `{job="skiff", event_type="auth.token_rotated"}`.

### 4b. Splunk

**Server side:**

```conf
# inputs.conf on the forwarder
[monitor:///Users/*/Library/Application Support/skiff/audit.jsonl]
sourcetype = _json
index = security
```

**Container side:**

SPL: `index=security sourcetype=_json source="*skiff*audit.jsonl" event_type=*`

### 4c. Datadog

Forward via the Agent with the `log_processing_rules` and the `json` parser:

```yaml
logs:
  - type: file
    path: /Users/*/Library/Application Support/skiff/audit.jsonl
    service: skiff
    source: skiff
    log_processing_rules:
      - type: multi_line
        name: new_log_entry
        pattern: '^\{'
```

Facets: `@event_type`, `@status`, `@path` — Datadog's JSON parser picks these up.

### 4d. ELK / OpenSearch

Filebeat:

```yaml
filebeat.inputs:
  - type: filestream
    id: skiff-audit
    paths:
      - /Users/*/Library/Application Support/skiff/audit.jsonl
    parsers:
      - ndjson:
          keys_under_root: true
          add_error_key: true
output.elasticsearch:
  hosts: ["https://es:9200"]
  index: "skiff-audit-%{+yyyy.MM.dd}"
```

### 4e. GCP Cloud Logging (native, no tail needed)

SKIFF writes directly to Cloud Logging when `GOOGLE_CLOUD_PROJECT` is set:

```bash
export GOOGLE_CLOUD_PROJECT=my-project-id
pip install 'skiff-container-manager[gcp]'  # installs google-cloud-logging
uvicorn skiff.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

Browse in Cloud Logging console → Log Name = `skiff-audit` (override via
`GCP_LOG_NAME`). The severity field maps automatically (INFO/WARNING/ERROR).

---

## 5. Prometheus scrape — local or managed

### Server side

```yaml
# prometheus.yml
scrape_configs:
  - job_name: skiff
    metrics_path: /api/system/metrics
    scheme: http
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/skiff-token
    static_configs:
      - targets: ['127.0.0.1:8080']
```

The token file contains one line — the API token. Rotation via SKIFF's
**Rotate API token** button → update the file → Prometheus re-reads on next
scrape (no restart needed with `credentials_file`).

### Managed variants

- **GCP Cloud Managed Service for Prometheus** — same config via the
  `google-cloud-ops-agent` or Managed Prometheus collector.
- **Grafana Alloy** — `prometheus.scrape` with `bearer_token_file`.
- **Datadog Agent** — the OpenMetrics check:
  ```yaml
  openmetrics:
    instances:
      - openmetrics_endpoint: http://127.0.0.1:8080/api/system/metrics
        namespace: skiff
        metrics: ['skiff_*']
        extra_headers:
          Authorization: Bearer <token>
  ```

### Container side

The metrics are about the Docker engine SKIFF manages — there's no
per-container agent-side integration. For per-container metrics
(CPU/memory/network), use cAdvisor or Docker's own
`/containers/<id>/stats` via `docker stats` on the host.

---

## 6. OIDC / SSO via OAuth2 proxy

### Server side

Put `oauth2-proxy` in front of SKIFF. SKIFF doesn't know about OIDC — the
proxy terminates identity and sets `X-Forwarded-User: <email>`, which SKIFF
logs on every audit entry when `TRUST_FORWARDED_HEADERS=true` is set on the
server (required — without it, the default `StripForwardedHeadersMiddleware`
drops the header so a direct caller can't forge attribution).

```bash
oauth2-proxy \
  --provider=github \
  --upstream=http://127.0.0.1:8080 \
  --http-address=127.0.0.1:4180 \
  --cookie-secret=$(openssl rand -hex 16) \
  --client-id=$GITHUB_APP_ID \
  --client-secret=$GITHUB_APP_SECRET \
  --email-domain='*' \
  --github-org=myorg
```

Now users visit `http://127.0.0.1:4180` (or the TLS proxy on top of it),
authenticate through GitHub/Google/Okta, and the audit log shows each
action under their real identity.

### Container side

No changes — containers launched via SKIFF behave identically. The
per-user attribution is purely at the audit-log layer.

---

## 7. Let's Encrypt / TLS termination — Caddy, nginx, Cloudflare Tunnel

### Server side (three choices)

**Caddy (simplest — automatic HTTPS):**

```bash
caddy reverse-proxy --from https://containers.example.com --to 127.0.0.1:8080
```

**nginx:**

```nginx
server {
    listen 443 ssl;
    server_name containers.example.com;
    ssl_certificate     /etc/letsencrypt/live/.../fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;
    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        # WebSocket upgrades for /ws/logs and /ws/exec
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
    }
}
```

**Cloudflare Tunnel (no public IP needed):**

```bash
cloudflared tunnel --hostname containers.example.com --url http://127.0.0.1:8080
```

### Container side

Browsers now use `https://containers.example.com` — the TLS is at the proxy
layer. Remember to update `ALLOWED_ORIGINS` on SKIFF:

```bash
export ALLOWED_ORIGINS="https://containers.example.com"
```

Otherwise the CSRF check (which is Origin-aware) will reject mutating
requests even over TLS.

---

## 8. CI / GitHub Actions — pulling SKIFF's audit log for review

Store the audit log as an artifact after an integration-test run:

```yaml
- name: Archive audit log
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: skiff-audit-${{ github.run_id }}
    path: |
      /home/runner/.local/state/skiff/audit.jsonl
      /home/runner/work/_temp/skiff-e2e-server.stderr
```

On failure, this lets reviewers see every API call made during the CI run —
essential for debugging integration-test flakiness.
