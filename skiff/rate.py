# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Named rate-limit tiers (values loaded from `skiff/_config/rate.toml`).

Each route picks a tier by name (e.g. `RATE.WRITE`) rather than a
magic "N/minute" string. Reviewers see the intent at the call site;
changing the tier policy is one TOML edit.

Five tiers cover the taxonomy:

| Tier              | Use for                                    |
|-------------------|--------------------------------------------|
| AUTH_SENSITIVE    | Setup, token rotate, login brute-force     |
| WRITE             | Mutating container / image / network / vol |
| READ              | List, inspect, status                      |
| PUBLIC            | /health, /ready — no auth                  |
| BURST             | Prune / docker-system-wide ops             |

Authoritative per-tier specs live in `skiff/_config/rate.toml` —
numbers are NOT duplicated here so an edit to the TOML can't rot this
docstring. All tiers flow through `_limit()` so `RATE_LIMIT_SCALE`
still applies. This module just exposes an attribute-access shim so
`RATE.WRITE` returns the scaled value at call time.

Use:
    from skiff.rate import RATE
    @limiter.limit(RATE.WRITE)
    def endpoint(...): ...
"""
from __future__ import annotations

from skiff.config import _TOML_RATE, _limit

# Tier specs come from skiff/_config/rate.toml [tiers] exclusively. Changing a
# tier is a single TOML edit; adding one requires a property on _Tiers.
_TIER_SPECS: dict[str, str] = dict(_TOML_RATE["tiers"])


class _Tiers:
    """Attribute accessor — each property lazy-evaluates against
    `_limit()` so RATE_LIMIT_SCALE applies. The underlying spec strings
    come from `_TIER_SPECS` which was merged at import time from
    `skiff/_config/rate.toml` + defaults.

    Property names are intentionally uppercase for tier-constant
    ergonomics at call sites (`RATE.WRITE`, `RATE.READ`).
    """

    def _spec(self, name: str) -> str:
        spec = _TIER_SPECS.get(name)
        if spec is None:
            raise KeyError(f"rate tier {name!r} not declared in skiff/_config/rate.toml [tiers]")
        return _limit(spec)

    @property
    def AUTH_SENSITIVE(self) -> str:  # noqa: N802 — tier constant
        """For auth-sensitive endpoints: setup, login-brute-force surfaces."""
        return self._spec("AUTH_SENSITIVE")

    @property
    def WRITE(self) -> str:  # noqa: N802 — tier constant
        """For mutating container / image / volume / network / compose routes."""
        return self._spec("WRITE")

    @property
    def READ(self) -> str:  # noqa: N802 — tier constant
        """For list / inspect / status / logs routes."""
        return self._spec("READ")

    @property
    def PUBLIC(self) -> str:  # noqa: N802 — tier constant
        """For unauthenticated endpoints: /health, /ready."""
        return self._spec("PUBLIC")

    @property
    def BURST(self) -> str:  # noqa: N802 — tier constant
        """For rarely-called but potentially expensive operations (prune)."""
        return self._spec("BURST")


RATE = _Tiers()


def known_tiers() -> frozenset[str]:
    """Introspection helper for tests / docs generators."""
    return frozenset(_TIER_SPECS)


__all__ = ["RATE", "known_tiers"]
