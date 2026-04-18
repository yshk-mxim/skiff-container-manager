# API surface: debug

GENERATED FROM `skiff/routers/debug.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/debug.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/debug/threads` | — | — |  | `debug_threads` | Return active thread stack traces. AUTH-gated AND requires |
