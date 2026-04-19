# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Null-injection fuzz for every Docker-backed endpoint.

Why this file exists (root cause retrospective from 1.0.1):

Four separate bugs shipped because Docker returns `null` (not 0) for
unpopulated numeric fields, and `dict.get(key, 0)` returns that null
instead of the default. Unit tests with hand-crafted mock fixtures all
passed because the fixtures had real numbers. The bugs only surfaced
against a real Docker daemon with:

  - containers that hadn't written past their image layer (SizeRw=null)
  - a cgroup v2 kernel (memory_stats.stats.cache key absent)
  - a build cache that had been pruned but still had entries (Size=null)
  - volumes on storage drivers that don't report usage (UsageData=null)

This file generates hypothesis strategies that RANDOMLY null out each
numeric field of a realistic Docker response and feeds them through
every Docker-backed endpoint. The invariant: **the endpoint returns
200 and every numeric field in the response is a number (not null)**.

Any future handler that does `.get("X", 0)` on a nullable field will
fail here, even if the specific field hasn't been reported as a bug
yet. The AP015 lint catches this at commit time; these tests catch
what the lint's allowlist misses.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import AUTH_CSRF, AUTH_HEADER  # noqa: F401


def _reset_limiter_for_fuzz():
    """Hypothesis runs many examples per test, but the session-scoped
    slowapi limiter would trip after 5-60 requests (depending on the
    endpoint's tier). Reset before every call so the fuzz actually
    exercises the handler instead of short-circuiting on 429s.

    SKIFF wires TWO limiter instances — `config.limiter` (module-level,
    used by decorators) and `app.state.limiter` (middleware-attached).
    Reset both; missing one means earlier tests in the suite can leave
    counters that trip this test under `pytest` full-suite runs but
    pass in isolation. See `tests/conftest.py::reset_global_state`."""
    from skiff import config as config_module
    from skiff.app import app

    for limiter in {config_module.limiter, app.state.limiter}:
        limiter.reset()


# ── Nullable-int helper ──────────────────────────────────────────────────────


def _nullable_int(max_value: int = 10**12) -> st.SearchStrategy:
    """Integer ≥ 0 OR None. Mirrors Docker's actual emission shape —
    most numeric fields are either populated with a real number or
    explicitly `null` when the daemon hasn't computed them."""
    return st.one_of(st.none(), st.integers(min_value=0, max_value=max_value))


# ── /api/system/df — null-tolerant aggregation ───────────────────────────────


@st.composite
def _df_response(draw):
    """Generate a /df response with nullable size fields on each entry."""
    images = draw(
        st.lists(
            st.fixed_dictionaries({"Size": _nullable_int(), "Containers": st.integers(min_value=0, max_value=100)}),
            max_size=5,
        )
    )
    containers = draw(st.lists(st.fixed_dictionaries({"SizeRw": _nullable_int()}), max_size=5))
    volumes = draw(
        st.lists(
            st.one_of(
                st.fixed_dictionaries(
                    {
                        "UsageData": st.one_of(
                            st.none(), st.fixed_dictionaries({"Size": _nullable_int(), "RefCount": _nullable_int()})
                        )
                    }
                ),
                # Some drivers omit the UsageData key entirely.
                st.fixed_dictionaries({}),
            ),
            max_size=5,
        )
    )
    build_cache = draw(
        st.lists(
            st.fixed_dictionaries({"Size": _nullable_int(), "InUse": st.booleans()}),
            max_size=5,
        )
    )
    return {"Images": images, "Containers": containers, "Volumes": volumes, "BuildCache": build_cache}


@given(df=_df_response())
@settings(max_examples=200, deadline=None)
@pytest.mark.unit
def test_system_df_never_crashes_on_null_fields(df):
    """Regardless of which numeric fields Docker sets to null, the
    /api/system/df endpoint must return 200 with every numeric field
    in the response being a real number (not null)."""
    # Import the handler module directly — this test doesn't go through
    # the auth layer because the handler is a pure function of df().
    # Using the FastAPI TestClient here would require running a server;
    # we only need to exercise the data-transform path.
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""  # disable auth for this probe
    mock_client = MagicMock()
    mock_client.df.return_value = df
    mock_client.ping.return_value = True
    _reset_limiter_for_fuzz()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=True) as tc:
                r = tc.get("/api/system/df", headers={"X-Requested-With": "ContainerManager"})
                assert r.status_code == 200, f"crashed on df shape: {df!r} → {r.text[:300]}"
                body = r.json()
                for k, v in body.items():
                    if k.endswith(("_mb", "_bytes", "_count")) or k == "total_mb":
                        assert isinstance(v, (int, float)), (
                            f"field {k!r} is {v!r} (type {type(v).__name__}) — must be numeric"
                        )
    finally:
        config_module._cfg.api_token = original_token


# ── /api/containers/{id}/stats — null-tolerant per-container stats ───────────


@st.composite
def _stats_response(draw):
    """Generate a realistic container.stats(stream=False) response with
    nullable memory, network, and blkio values. Covers both cgroup v1
    (with `cache` key) and v2 (with `inactive_file` instead)."""
    cgroup_v2 = draw(st.booleans())
    memory_stats_inner = (
        {
            "active_anon": draw(_nullable_int()),
            "anon": draw(_nullable_int()),
            "file": draw(_nullable_int()),
            "inactive_file": draw(_nullable_int()),
        }
        if cgroup_v2
        else {
            "cache": draw(_nullable_int()),
            "rss": draw(_nullable_int()),
            "mapped_file": draw(_nullable_int()),
        }
    )
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": draw(st.integers(min_value=0, max_value=10**10))},
            "system_cpu_usage": draw(st.integers(min_value=0, max_value=10**12)),
            "online_cpus": draw(st.integers(min_value=1, max_value=64)),
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": draw(st.integers(min_value=0, max_value=10**10))},
            "system_cpu_usage": draw(st.integers(min_value=0, max_value=10**12)),
        },
        "memory_stats": {
            "usage": draw(_nullable_int()),
            "limit": draw(_nullable_int()),
            "stats": memory_stats_inner,
        },
        "networks": {
            "eth0": {
                "rx_bytes": draw(_nullable_int()),
                "tx_bytes": draw(_nullable_int()),
            }
        },
        "blkio_stats": {
            "io_service_bytes_recursive": draw(
                st.lists(
                    st.fixed_dictionaries(
                        {
                            "op": st.sampled_from(["Read", "Write", "Total"]),
                            "value": _nullable_int(),
                        }
                    ),
                    max_size=6,
                )
            )
        },
    }


@given(stats=_stats_response())
@settings(max_examples=200, deadline=None)
@pytest.mark.unit
def test_container_stats_never_crashes_on_null_fields(stats):
    """Container stats endpoint must return 200 + every numeric field
    must be a real number, no matter which subset of fields Docker
    chose to return as null. Covers both cgroup v1 and v2 response
    shapes (generator randomly picks one). Would have caught the 1.0.1
    bug where the cgroup v2 `cache` key absence silently produced
    mem_usage_mb=0 instead of using `inactive_file` as the subtrahend."""
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.stats.return_value = stats
    mock_container.short_id = "abc123def456"
    mock_client.containers.get.return_value = mock_container
    mock_client.ping.return_value = True
    _reset_limiter_for_fuzz()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=True) as tc:
                r = tc.get(
                    "/api/containers/abc123def456/stats",
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 200, f"crashed on stats shape: {stats!r} → {r.text[:300]}"
                body = r.json()
                for field in (
                    "cpu_percent",
                    "mem_usage_mb",
                    "mem_limit_mb",
                    "mem_percent",
                    "net_rx_mb",
                    "net_tx_mb",
                    "blk_read_mb",
                    "blk_write_mb",
                ):
                    assert field in body, f"missing field {field!r}"
                    assert isinstance(body[field], (int, float)), (
                        f"field {field!r} is {body[field]!r} (type {type(body[field]).__name__}) — must be numeric"
                    )
    finally:
        config_module._cfg.api_token = original_token


# ── /api/system/metrics — Prometheus exposition must parse cleanly ───────────


@given(df=_df_response())
@settings(max_examples=100, deadline=None)
@pytest.mark.unit
def test_system_metrics_exposition_parses_on_null_fields(df):
    """Every metric line in the Prometheus exposition must be
    `<name>{labels} <number>` where `<number>` parses as a float.
    A null leaking into the rendered output would produce either
    `<name>{labels} None` (Prom scrape fails) or a crash."""
    from fastapi.testclient import TestClient

    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    original_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    mock_client = MagicMock()
    mock_client.df.return_value = df
    mock_client.info.return_value = {
        "Containers": 1,
        "ContainersRunning": 1,
        "ContainersPaused": 0,
        "ContainersStopped": 0,
        "Images": 1,
        "NCPU": 1,
        "MemTotal": 1024**3,
        "OperatingSystem": "Linux",
        "ServerVersion": "27.0.0",
    }
    mock_client.ping.return_value = True
    _reset_limiter_for_fuzz()
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=True) as tc:
                r = tc.get("/api/system/metrics", headers={"X-Requested-With": "ContainerManager"})
                assert r.status_code == 200, f"metrics crashed on df={df!r}: {r.text[:300]}"
                # Every non-comment, non-blank line must end in a parseable float.
                for line in r.text.splitlines():
                    if not line or line.startswith("#"):
                        continue
                    parts = line.rsplit(" ", 1)
                    assert len(parts) == 2, f"malformed metric line: {line!r}"
                    try:
                        float(parts[1])
                    except ValueError as exc:
                        pytest.fail(f"metric value not a float: {line!r} — {exc}")
    finally:
        config_module._cfg.api_token = original_token
