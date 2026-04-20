# Deployment Guide

SKIFF is one Python process talking to one Docker Engine API (local socket
or remote tunnel). Pick the deployment shape that fits your situation.

For production hardening after the basics work, see
[`hardening/production.md`](hardening/production.md).

---

## 1. Local development or homelab (simplest)

```bash
# One-time install
pip install -e '.[dev]'

# Start with a generated token, binding to localhost only
export API_TOKEN="$(openssl rand -hex 32)"
uvicorn skiff.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers

# Or use the first-run wizard: leave API_TOKEN unset and browse to
# http://127.0.0.1:8080 — wizard guides you through token + Docker host.
```

Works against any Docker Engine API-compatible runtime. The wizard
auto-detects reachable local sockets (the probe path list lives in
`skiff/_config/docker_probe.toml`). If you need help starting a runtime, the
unreachable-Docker empty state has copy-paste commands for Colima,
OrbStack, Rancher Desktop, Podman rootless, and system dockerd.

## 2. Remote Docker host via SSH tunnel

SKIFF runs on your local machine; the Docker daemon lives on a remote
host. The wizard's SSH Tunnel tab sets this up without shell commands —
just enter `user@host`.

Manual setup (if you prefer not to use the wizard):

```bash
# Your workstation
ssh-copy-id dev@docker-host       # one-time
# Remove any stale socket from a previous run; otherwise ssh -fNL silently
# fails to bind and the tunnel appears "up" while all API calls return 503.
rm -f /tmp/docker.sock
ssh -fNL /tmp/docker.sock:/var/run/docker.sock dev@docker-host

export API_TOKEN="$(openssl rand -hex 32)"
export DOCKER_HOST="unix:///tmp/docker.sock"
uvicorn skiff.app:app --host 127.0.0.1 --port 8080 --no-proxy-headers
```

## 3. Behind a TLS-terminating proxy (production)

For any deployment beyond your own machine, front SKIFF with a reverse
proxy that handles TLS. See [`hardening/production.md` §1](hardening/production.md#1-tls-termination)
for Caddy, nginx, Tailscale, and Cloudflare Tunnel examples. In short:

```bash
# Caddy (automatic HTTPS — recommended for most):
caddy reverse-proxy --from https://containers.example.com --to 127.0.0.1:8080

# Then update SKIFF to trust the origin:
export ALLOWED_ORIGINS="https://containers.example.com"
```

For multi-user installations, pair with an OAuth2 identity proxy
([`hardening/production.md` §5](hardening/production.md#5-sso-via-identity-proxy-optional-multi-user)) —
it passes `X-Forwarded-User`; SKIFF adds it to every audit entry
**when `TRUST_FORWARDED_HEADERS=true` is set** (required — by default
the `StripForwardedHeadersMiddleware` drops the header so a direct
caller can't forge attribution; enable only when a trusted proxy
fronts SKIFF).

## 4. systemd service

A ready-to-use unit file is in `docs/skiff.service`. Install as
`/etc/systemd/system/skiff@.service` (the `@` makes it per-user):

```bash
sudo cp docs/skiff.service /etc/systemd/system/skiff@.service
sudo systemctl enable --now skiff@$USER
journalctl -u skiff@$USER -f       # follow logs
```

The unit is per-instance (`skiff@<name>.service`). Each instance
reads `/opt/skiff/<name>.env`; `PORT`, `DOCKER_HOST`, `API_TOKEN`,
`AUDIT_LOG`, and `COMPOSE_DIR` belong there. Running a fleet is N
copies of the env file + N `systemctl enable skiff@<name>` calls.

---

## Environment Variables

The complete table lives in the [README](../README.md#configuration).
These are the ones you almost always touch for deployment:

| Variable | When to set | Default |
|---|---|---|
| `API_TOKEN` | Always in production (any network-reachable deploy) | _(empty)_ |
| `DOCKER_HOST` | When pointing at a non-default socket | `unix:///var/run/docker.sock` |
| `ALLOWED_ORIGINS` | When behind a reverse proxy or on a custom port | `http://127.0.0.1:8080` |
| `ALLOWED_REGISTRIES` | To restrict pulls to specific registries | `docker.io,ghcr.io` |
| `AUDIT_LOG` | To override the per-user state path (see §6 of hardening) | per-OS state dir |
| `PROFILE` | To apply a persona bundle (homelab/dev/sre/reviewer/tutor/ci) | _(empty)_ |
| `BIND_HOST` | Only set to `0.0.0.0` if behind a firewall / VPN | `127.0.0.1` |

---

## Security checklist before going public

- [ ] `API_TOKEN` set to 32+ random hex chars.
- [ ] `ALLOWED_ORIGINS` matches the exact URL users reach the app from.
- [ ] `ALLOWED_REGISTRIES` scoped to your org's registries.
- [ ] Behind a TLS proxy (Caddy / nginx / Cloudflare Tunnel).
- [ ] Audit log written to a location your SIEM can tail
      (see [`hardening/integrations.md`](hardening/integrations.md) for Loki/Splunk/Datadog/ELK
      snippets that pull from the live server).
- [ ] SSH key (if using a tunnel) is dedicated to SKIFF and restricted to
      the Docker host only.

---

## Updating

```bash
git pull
pip install -e '.[dev]'
sudo systemctl restart skiff@$USER    # or kill + re-run uvicorn
```

No database migrations, no persistent state on the SKIFF side. Compose
files stored in `COMPOSE_DIR` survive the upgrade; everything else is
stateless.
