# Runbook: <incident title>

**Impact:** blocks-all-use / degraded-use / cosmetic
**Updated:** YYYY-MM-DD

## Detection

How an operator notices this is happening:
- Audit log signal: `"event_type": "<domain>.<verb>"` spike or new error
- User report pattern: "I can't see any containers"
- Health probe failure: `/health` 503

## Immediate mitigation

Exactly what to do in the first 5 minutes, in order:

1. Snapshot audit log: `cp ~/Library/Application\ Support/skiff/audit.jsonl /tmp/skiff-$(date +%s).jsonl`
2. Check `/health` — if unreachable, see <other runbook>.
3. ...

## Diagnosis

Follow-ups once users are unblocked:

- Verify Docker daemon alive: `docker info`
- Check for exhausted tunnel: `ls /tmp/skiff-docker.sock`
- ...

## Recovery

1. Steps to restore normal service.
2. Including any state cleanup.

## Prevention

- Tests that should exist (do they?).
- Alerts that would have caught this earlier.
- Config changes to consider.

## Related

- Audit events: ...
- Code paths: ...
- Past incidents: ...
