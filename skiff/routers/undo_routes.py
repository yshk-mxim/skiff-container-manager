# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""HTTP endpoint for the undo queue (the queue itself lives in skiff.undo).

Separated from `routers.system` so the undo surface is one file —
it's the HTTP veneer over `skiff.undo.UndoQueue`, and has nothing to do
with system info / metrics / audit. Grepping for "undo" now surfaces:

  skiff/undo.py           the queue + timer machinery
  skiff/routers/undo_routes.py  the one HTTP route

  POST /api/undo/{token}  cancel a queued destructive operation
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from skiff.auth import AUTH
from skiff.contract.errors import http_error
from skiff.contract.responses import OkResponse
from skiff.rate import RATE
from skiff.secure import secure_route

router = APIRouter()


@router.post("/api/undo/{token}", dependencies=AUTH, tags=["system"])
@secure_route.mutate(RATE.WRITE)
def undo_operation(request: Request, token: str) -> dict:
    """Cancel a pending destructive operation queued by a DELETE ?undo=1 call.

    Returns `{ok: true, cancelled: bool}`. `cancelled=false` means the token
    was not in the queue — either already fired (too late) or never valid
    (typo, forged, or replayed). Idempotent: a second call with the same
    token always returns `cancelled=false`.
    """
    # Token format defence: our generator uses token_urlsafe(16) which is
    # base64url (A-Z a-z 0-9 _ -), always 22 chars. Reject anything else
    # before touching the queue so garbage never reaches the internal map.
    if not token or len(token) > 64 or not all(
        c.isalnum() or c in ("-", "_") for c in token
    ):
        raise http_error("validation.bad_input")
    from skiff.undo import get_queue
    cancelled = get_queue().cancel(token)
    return OkResponse(cancelled=cancelled).model_dump(exclude_none=True)
