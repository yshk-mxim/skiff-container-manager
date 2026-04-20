# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Audit-log read + download endpoints.

Separated from `routers.system` so audit retrieval, which has its own
strict-auth requirement (`verify_auth_strict`) and own rate-limit class,
isn't mixed in with system observe/operate surface. Operators grepping
for "audit" in the codebase find exactly the audit-log HTTP surface here.

  GET /api/system/audit-log           last N lines (tail=N, N ≤ MAX_AUDIT_LINES)
  GET /api/system/audit-log/download  full file, streamed as JSONL
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from skiff import auth, config
from skiff.rate import RATE
from skiff.secure import secure_route

router = APIRouter()


def _read_last_chunk(path: Path, chunk_size: int) -> list[str]:
    """Seek to the end of `path` and read back `chunk_size` bytes as decoded lines.

    Returns an empty list on any OSError (missing / rotated file). The
    first line is discarded when the chunk didn't start at offset 0 — it
    was almost certainly truncated mid-record.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            seek_to = max(0, file_size - chunk_size)
            fh.seek(seek_to)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if seek_to > 0 else lines


def _parse_audit_line(raw: str) -> dict | None:
    """Parse one audit line. Invalid JSON surfaces as `{"raw": …}`; empties drop."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"raw": stripped}


@router.get(
    "/api/system/audit-log",
    dependencies=[Depends(auth.verify_auth_strict)],
    tags=["audit"],
)
@secure_route.read(RATE.READ)
def get_audit_log(request: Request, tail: int = Query(default=200, le=config.MAX_AUDIT_LINES, ge=1)):
    """Return the last N lines of the audit log, read without loading the whole file.

    Response body stays a JSON array for back-compat with SIEM scrapers
    and the UI. Out-of-band transparency headers let a caller detect
    truncation + parse-drops without breaking legacy clients:
      X-Audit-Requested-Tail   what the caller asked for
      X-Audit-Returned-Count   what we actually returned
      X-Audit-Server-Cap       MAX_AUDIT_LINES (upper bound on `tail`)
      X-Audit-Parse-Errors     count of lines that JSON-decoded as {"raw":…}
    Prior behaviour silently returned fewer rows than requested when
    parse errors dropped lines — operators correlating with stderr had
    no way to tell those apart from a real 'no such event'.
    """

    def _headers(returned: int, parse_errors: int) -> dict[str, str]:
        return {
            "X-Audit-Requested-Tail": str(tail),
            "X-Audit-Returned-Count": str(returned),
            "X-Audit-Server-Cap": str(config.MAX_AUDIT_LINES),
            "X-Audit-Parse-Errors": str(parse_errors),
        }

    if not config.AUDIT_LOG_PATH.exists():
        return JSONResponse(content=[], headers=_headers(0, 0))
    # ~300 bytes/line average → 2x budget keeps us safely above `tail` records.
    raw_lines = _read_last_chunk(config.AUDIT_LOG_PATH, tail * 600)
    entries: list[dict] = []
    parse_errors = 0
    for line in raw_lines[-tail:]:
        parsed = _parse_audit_line(line)
        if parsed is None:
            continue
        if "raw" in parsed and set(parsed.keys()) == {"raw"}:
            parse_errors += 1
        entries.append(parsed)
    return JSONResponse(content=entries, headers=_headers(len(entries), parse_errors))


@router.get(
    "/api/system/audit-log/download",
    dependencies=[Depends(auth.verify_auth_strict)],
    tags=["audit"],
)
@secure_route.read(RATE.AUTH_SENSITIVE)  # streams file — low limit prevents disk thrash
def download_audit_log(request: Request):
    """Download the full audit log as a JSONL file (streamed to avoid memory spikes)."""
    if not config.AUDIT_LOG_PATH.exists():
        return PlainTextResponse(
            "",
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'},
        )
    return FileResponse(
        path=str(config.AUDIT_LOG_PATH),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'},
    )
