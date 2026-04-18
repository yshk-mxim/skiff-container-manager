# SKIFF personas

Persona presets bundle sensible defaults for different operator contexts.
Set `PROFILE=<name>` at startup to apply. Everything in `_PROFILE_PRESETS`
uses `os.environ.setdefault`, so any explicit env var takes precedence
over the preset.

## Persona catalogue

| Persona   | Best for                                   | Key defaults           |
|-----------|--------------------------------------------|------------------------|
| `homelab` | Local Pi / NAS install, casual home use    | `RATE_LIMIT_SCALE=10` — loose limits; everything visible |
| `dev`     | Developer workstation (default)            | No overrides           |
| `sre`     | Remote Docker host, ops-heavy workflow     | `RATE_LIMIT_SCALE=3` — faster scrapers |
| `reviewer`| Security-review mode (read-only)           | `RATE_LIMIT_SCALE=1` — tight limits. Mutations are blocked server-side (`auth.reviewer_read_only` 403); destructive buttons hidden in UI; sticky banner shows the mode is active. |
| `tutor`   | Classroom / demo with low-stakes stacks    | `RATE_LIMIT_SCALE=50` — loose; few confirmations |
| `ci`      | CI runner or automation                    | `RATE_LIMIT_SCALE=100` — effectively uncapped; `API_TOKEN` required |

## What pages each persona sees

Page visibility is driven by `UI.registerPage({personas: [...]})` — a
page whose `personas` list omits a persona is hidden. The currently
registered pages and their persona tags:

| Page       | homelab | dev | sre | reviewer | tutor | ci |
|------------|:-------:|:---:|:---:|:--------:|:-----:|:--:|
| containers |    ✓    |  ✓  |  ✓  |    ✓     |   ✓   |  ✓ |
| images     |    ✓    |  ✓  |  ✓  |    ✓     |   ✓   |  ✓ |
| volumes    |    ✓    |  ✓  |  ✓  |    ✓     |   ✓   |  ✓ |
| networks   |    ✓    |  ✓  |  ✓  |    ✓     |   ✓   |  ✓ |
| compose    |    ✓    |  ✓  |  ✓  |    ✓     |   ✓   |  ✓ |
| system     |         |  ✓  |  ✓  |    ✓     |       |  ✓ |

Audit and metrics are surfaced as tabs inside the System page, not as
top-level pages — use the System page's "Audit log" and "Metrics"
sections for those. A future release may promote them if persona
demand warrants.

## Reviewer profile — runtime lifecycle

Entering reviewer mode is a one-way switch from the UI side: an admin
uses the sidebar-footer dropdown "Enter reviewer mode" (visible only
when `PROFILE != reviewer`) and the browser posts to
`POST /api/profile/enter-reviewer`. The server flips
`config.PROFILE = "reviewer"` under `_ws_lock`, then force-closes
every active exec WebSocket via `close_active_exec_sessions()` — a
handler caught in the acquire→register gap fails the re-check and
is rejected before it can execute a shell.

While `PROFILE == "reviewer"`:

| Request class                                     | Behaviour                              |
|---------------------------------------------------|----------------------------------------|
| `GET /api/*` (reads, inspects, logs, audit)       | allowed                                |
| `POST /api/profile/enter-reviewer`                | idempotent 403 (gate catches it)       |
| Any `@secure_route.mutate` route                  | 403 `auth.reviewer_read_only`          |
| `POST /api/undo/{token}` (cancel a destroy)       | allowed (`allow_in_reviewer=True`)     |
| `POST /api/auth/reset-config` (soft restart)      | allowed (`allow_in_reviewer=True`)     |
| `ws/exec/{id}` (new upgrades + live sessions)     | closed with code 4003                  |

Exiting reviewer mode is deliberately NOT a one-click operation: the
point of entering is to lock the instance for an audit session. The
two supported exits are:

1. **Soft-restart via `reset-config`.** The handler clears the token
   and registry allow-list AND restores `config.PROFILE` to the
   boot-time value captured in `config._BOOT_PROFILE` at import time.
   A reviewer who escapes via reset-config and completes the
   re-opened wizard lands back in the boot PROFILE (typically `dev`)
   — not stuck in `reviewer` with a fresh token.
2. **Process restart.** Whatever PROFILE the env declares at boot
   wins on the next start.

Starting with `PROFILE=reviewer API_TOKEN=""` is supported: the
reviewer gate short-circuits while `api_token` is empty so the
wizard can complete. The gate re-engages the moment setup finishes
and a token is in place.

## Adding a persona

1. Declare the preset in `skiff/config.py::_PROFILE_PRESETS`.
2. Document it in the table above.
3. Add a row to `../dev/copy.md` under a new *Personas* section if the UI
   adds persona-specific strings.
4. If the persona has a dedicated onboarding doc, link it here.

## Why not user accounts?

SKIFF is single-token by design (see `SECURITY.md`). Personas describe
what a single operator wants the UI to emphasise; they're not
access-control roles. If multi-user access becomes a requirement, that
decision reopens the whole token model — document the trade-off before
adding.
