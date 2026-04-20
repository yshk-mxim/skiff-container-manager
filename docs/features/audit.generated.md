# API surface: audit

GENERATED FROM `skiff/routers/audit.py` by `tools/gen_feature_docs.py`.
Regenerate via `python tools/gen_feature_docs.py`; CI `--check` fails
on drift. The hand-written `docs/features/audit.md` (if any)
carries the narrative and threat-model context.

| Method | Path | Audit event | Rate tier | CSRF | Handler | Description |
|---|---|---|---|---|---|---|
| GET | `/api/system/audit-log` | — | 60/minute | — | `get_audit_log` | Return the last N lines of the audit log, read without loading the whole file. |
| GET | `/api/system/audit-log/download` | — | 5/minute | — | `download_audit_log` | Download the full audit log as a JSONL file (streamed to avoid memory spikes). |
