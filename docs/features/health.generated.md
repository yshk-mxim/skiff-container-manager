# API surface: health

GENERATED FROM `skiff/routers/health.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/health.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/api/auth-required` | — | 120/minute | — | `auth_required` | Returns whether auth is required. No secrets exposed — callable pre-login. |
| GET | `/health` | — | — |  | `health` | Liveness — never checks Docker to avoid restart loops. |
| GET | `/ready` | — | 120/minute | — | `ready` | Readiness — returns 503 if Docker is unreachable. |
