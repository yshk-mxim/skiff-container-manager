# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Diagnostic endpoints — AUTH + opt-in-env gated.

Separated from `routers.system` because these are low-traffic
troubleshooting handlers that reveal internals an attacker who
guesses an API token should NOT see for free. The extra
`SKIFF_DEBUG_THREADS=1` gate makes this a two-lock cabinet.

  GET /debug/threads   per-thread stack traces — AUTH + SKIFF_DEBUG_THREADS=1
"""

from __future__ import annotations

import sys

from fastapi import APIRouter

from skiff import config
from skiff.auth import AUTH
from skiff.contract.errors import http_error

router = APIRouter()


@router.get("/debug/threads", dependencies=AUTH, tags=["system"])
async def debug_threads():
    """Return active thread stack traces. AUTH-gated AND requires
    `SKIFF_DEBUG_THREADS=1` to be set — stack traces can contain in-flight
    local-variable reprs, so we want two independent gates: the token
    AND an explicit operator opt-in at server-start time."""
    if not config.DEBUG_THREADS_ENABLED:
        raise http_error("system.debug_disabled")
    import traceback

    frames = sys._current_frames()
    result = {str(tid): "".join(traceback.format_stack(frame)) for tid, frame in frames.items()}
    return {"thread_count": len(frames), "threads": result}
