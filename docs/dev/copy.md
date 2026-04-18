# User-facing string inventory

Every string the user reads lives here. The list is the source for any
future i18n work — right now it's English-only, but centralising means a
translator has one file instead of a repo-wide grep.

Conventions:
- Keys are scoped `<page>.<context>.<intent>` (dots separate).
- Values use sentence case, no trailing period unless multi-sentence.
- Avoid app-specific jargon — "container", "image", "volume" are fine;
  "rootfs", "tmpfs" only with tooltips.

---

## Auth / session

| Key | Value |
|---|---|
| `auth.signin.title` | Sign in |
| `auth.signin.token_label` | API token |
| `auth.signin.button` | Sign in |
| `auth.signin.bad_token` | Invalid token |
| `auth.session.expired` | Session expired — please sign in again |

## Setup wizard

| Key | Value |
|---|---|
| `setup.title` | Welcome — first-time setup |
| `setup.token.label` | Your new API token |
| `setup.token.copy_hint` | Copy this now — it's shown only once. |
| `setup.tunnel.title` | Connect to a Docker host |
| `setup.tunnel.user_label` | SSH user |
| `setup.tunnel.host_label` | SSH host |
| `setup.tunnel.test_button` | Test connection |
| `setup.done.title` | All set |

## Containers

| Key | Value |
|---|---|
| `containers.title` | Containers |
| `containers.run_button` | Run new container |
| `containers.actions.start` | Start |
| `containers.actions.stop` | Stop |
| `containers.actions.restart` | Restart |
| `containers.actions.kill` | Kill |
| `containers.actions.delete` | Delete |
| `containers.actions.inspect` | Inspect |
| `containers.actions.logs` | Logs |
| `containers.delete.confirm` | Delete this container? |
| `containers.start.exited_toast` | Container exited immediately — check logs |

## Images

| Key | Value |
|---|---|
| `images.title` | Images |
| `images.pull_button` | Pull image |
| `images.delete.confirm` | Delete this image? |
| `images.pull.failed` | Image pull failed |

## Volumes

| Key | Value |
|---|---|
| `volumes.title` | Volumes |
| `volumes.create_button` | Create volume |
| `volumes.delete.confirm` | Delete this volume? |
| `volumes.deleted.toast` | Volume deleted |
| `volumes.deleted.undo_toast` | Volume will delete in 5s — undo? |

## Networks

| Key | Value |
|---|---|
| `networks.title` | Networks |
| `networks.create_button` | Create network |
| `networks.delete.builtin_refused` | Built-in networks cannot be deleted |

## Compose

| Key | Value |
|---|---|
| `compose.title` | Compose |
| `compose.upload_button` | Upload compose file |
| `compose.up.failed` | Compose deployment failed |
| `compose.down.failed` | Compose teardown failed |

## System

| Key | Value |
|---|---|
| `system.title` | System |
| `system.prune_button` | Prune unused |
| `system.tunnel.reconnect` | Reconnect tunnel |
| `system.audit.title` | Audit log |

## Generic

| Key | Value |
|---|---|
| `common.error.unreachable` | Container engine unreachable |
| `common.error.rate_limited` | Too many requests — please wait |
| `common.undo.label` | Undo |
| `common.copy.done` | Copied! |
| `common.loading` | Loading… |

---

**How to extend:** when you add a string to the UI, append a row here
under the appropriate section (or add a new section). Drift is checked
by eye today; a future CI task can diff strings in `skiff/static/**.js`
against this file.
