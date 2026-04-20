# SPDX-License-Identifier: MIT
"""Shared Hypothesis strategies.

Before this file, `test_fuzz.py` and `test_properties.py` each declared
their own regexes / unit lists / filter logic for generating valid
memory quantities, container IDs, project names, etc. That's duplication
that drifts — one test updates a regex and the other doesn't.

This module is the single source of truth. Import from here:

    from tests.strategies import (
        container_id_st,        # hypothesis strategy generating valid IDs
        project_name_st,
        memory_quantity_st,
        cpu_quantity_st,
        registry_image_st,
        compose_yaml_body_st,
    )

Naming convention: `<name>_st` for strategies (st for strategy).
"""

from __future__ import annotations

import string

from hypothesis import strategies as st

# ─────────────────────────────────────────────────────────────────────────────
# Valid-shape character sets (mirrors validators — import only for constants)
# ─────────────────────────────────────────────────────────────────────────────

_HEX_CHARS = string.hexdigits[:16]  # "0123456789abcdef"
_PROJECT_CHARS = string.ascii_lowercase + string.digits + "-_"
_PROJECT_START = string.ascii_lowercase + string.digits
_NAME_CHARS = string.ascii_letters + string.digits + "._-"

_MEM_SUFFIXES = ("", "k", "K", "Ki", "m", "M", "Mi", "g", "G", "Gi", "t", "T", "Ti")
_CPU_SUFFIXES = ("", "m")

_REGISTRIES = ["docker.io", "ghcr.io", "quay.io"]


# ─────────────────────────────────────────────────────────────────────────────
# Resource-name strategies
# ─────────────────────────────────────────────────────────────────────────────


def container_id_st(min_size: int = 4, max_size: int = 64) -> st.SearchStrategy[str]:
    """Hex-only container IDs matching CONTAINER_ID_RE."""
    return st.text(alphabet=_HEX_CHARS, min_size=min_size, max_size=max_size)


def project_name_st(min_size: int = 1, max_size: int = 63) -> st.SearchStrategy[str]:
    """Docker-compose project names: start with [a-z0-9], then [a-z0-9_-]."""
    first = st.text(alphabet=_PROJECT_START, min_size=1, max_size=1)
    rest = st.text(alphabet=_PROJECT_CHARS, min_size=max(0, min_size - 1), max_size=max(0, max_size - 1))
    return st.builds(lambda a, b: a + b, first, rest)


def container_name_st(min_size: int = 1, max_size: int = 63) -> st.SearchStrategy[str]:
    """Docker container names. Letters/digits/._- per validator."""
    first = st.text(
        alphabet=string.ascii_letters + string.digits,
        min_size=1,
        max_size=1,
    )
    rest = st.text(alphabet=_NAME_CHARS, min_size=max(0, min_size - 1), max_size=max(0, max_size - 1))
    return st.builds(lambda a, b: a + b, first, rest)


# ─────────────────────────────────────────────────────────────────────────────
# Quantity strategies (with/without unit suffix)
# ─────────────────────────────────────────────────────────────────────────────


def memory_quantity_st() -> st.SearchStrategy[str]:
    """Valid `<number><unit?>` memory quantities. Hits every _MEM_UNIT_BYTES suffix."""
    number = st.one_of(
        st.integers(min_value=0, max_value=10**12).map(str),
        st.floats(min_value=0, max_value=1e12, allow_nan=False, allow_infinity=False).map(lambda f: f"{f:.6f}"),
    )
    return st.builds(lambda n, u: f"{n}{u}", number, st.sampled_from(_MEM_SUFFIXES))


def cpu_quantity_st() -> st.SearchStrategy[str]:
    """Valid `<number>` or `<millicores>m` CPU quantities."""
    number = st.one_of(
        st.integers(min_value=0, max_value=10**6).map(str),
        st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False).map(lambda f: f"{f:.3f}"),
    )
    return st.builds(lambda n, u: f"{n}{u}", number, st.sampled_from(_CPU_SUFFIXES))


# ─────────────────────────────────────────────────────────────────────────────
# Registry / image strategies
# ─────────────────────────────────────────────────────────────────────────────


def registry_image_st() -> st.SearchStrategy[str]:
    """Well-formed images under one of the known registries."""
    name = st.text(
        alphabet=string.ascii_lowercase + string.digits + "/._-",
        min_size=1,
        max_size=40,
    )
    tag = st.text(
        alphabet=string.ascii_letters + string.digits + "._-",
        min_size=1,
        max_size=20,
    )
    return st.builds(
        lambda r, n, t: f"{r}/{n}:{t}",
        st.sampled_from(_REGISTRIES),
        name,
        tag,
    )


def registry_allowlist_st() -> st.SearchStrategy[list[str]]:
    """Random non-empty subsets of the known registries list."""
    return st.lists(
        st.sampled_from(_REGISTRIES),
        min_size=1,
        max_size=len(_REGISTRIES),
        unique=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Compose YAML body strategies
# ─────────────────────────────────────────────────────────────────────────────


def compose_service_count_st() -> st.SearchStrategy[int]:
    """Number of services in a generated compose body."""
    return st.integers(min_value=1, max_value=20)


def compose_yaml_body_st() -> st.SearchStrategy[bytes]:
    """Minimal compose YAML with varying service counts. Always valid shape."""

    def _build(n: int) -> bytes:
        services = "".join(f"  svc_{i}:\n    image: docker.io/library/alpine:latest\n" for i in range(n))
        return f"services:\n{services}".encode()

    return compose_service_count_st().map(_build)


__all__ = [
    "compose_service_count_st",
    "compose_yaml_body_st",
    "container_id_st",
    "container_name_st",
    "cpu_quantity_st",
    "memory_quantity_st",
    "project_name_st",
    "registry_allowlist_st",
    "registry_image_st",
]
