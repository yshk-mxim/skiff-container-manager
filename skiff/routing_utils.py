# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Version-agnostic flattening of FastAPI's route tree.

fastapi < 0.139 eagerly flattened every ``include_router()`` call into
``app.routes`` as individual ``APIRoute`` objects. fastapi 0.139 replaced
that with a lazy model: each ``include_router()`` adds a single private
``_IncludedRouter`` wrapper whose ``.original_router`` holds the real
routes, so ``app.routes`` no longer contains ``APIRoute`` leaves directly.

``iter_leaf_routes`` walks either shape and yields the underlying leaf
routes, so route introspection (audit-event registration, contract tests)
keeps working across both fastapi versions. SKIFF includes its routers with
``prefix=""`` and full paths, so the flattened ``APIRoute`` objects already
carry their final paths and their auth/CSRF dependencies.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


def iter_leaf_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Yield leaf routes, expanding fastapi 0.139 ``_IncludedRouter`` wrappers.

    Only ``_IncludedRouter`` wrappers are descended into (via their
    ``original_router``); every other entry — ``APIRoute``, ``Route``,
    ``WebSocketRoute``, ``Mount`` — is yielded as-is, exactly matching the
    flat ``app.routes`` shape fastapi <= 0.138 produced. On those older
    versions no wrapper is present, so this is a passthrough.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            # `original_router` is the APIRouter passed to include_router(),
            # which always exposes `.routes`.
            yield from iter_leaf_routes(included.routes)
        else:
            yield route
