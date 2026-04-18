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
| `reviewer`| Security-review mode                       | `RATE_LIMIT_SCALE=1` — baseline; destructive actions de-emphasised |
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
