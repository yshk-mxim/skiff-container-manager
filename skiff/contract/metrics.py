# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Prometheus metric name registry.

The `/api/system/metrics` endpoint emits a small set of Prometheus-text
metrics today. Names get locked in at v1 — renames are ABI-breaking for
every scraper. This registry is the single place metric names live so
renames are a conscious, reviewed change.

When the metric catalogue grows (we have exactly the basics today), this
file becomes the contract layer between `routers/system.py::metrics` and
any future `routers/system.py::metrics_json` or SDK export.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class _MetricSpec:
    """One Prometheus metric declaration.

    kind:   "counter" | "gauge" | "histogram". Matches Prometheus types.
    labels: Label names this metric MAY carry. No free-form labels.
    help:   Short description emitted as `# HELP`.
    """

    kind: Literal["counter", "gauge", "histogram"]
    labels: tuple[str, ...] = ()
    help: str = ""


_METRICS: dict[str, _MetricSpec] = {
    "skiff_containers_total": _MetricSpec(
        kind="gauge",
        labels=("status",),
        help="Number of containers by status.",
    ),
    "skiff_images_total": _MetricSpec(
        kind="gauge",
        help="Total number of local images.",
    ),
    "skiff_volumes_total": _MetricSpec(
        kind="gauge",
        help="Total number of local volumes.",
    ),
    "skiff_networks_total": _MetricSpec(
        kind="gauge",
        help="Total number of local networks.",
    ),
    "skiff_requests_total": _MetricSpec(
        kind="counter",
        labels=("route", "method", "status"),
        help="HTTP request counter by route / method / response status class.",
    ),
    "skiff_undo_queue_depth": _MetricSpec(
        kind="gauge",
        help="Number of destructive operations currently pending in the undo queue.",
    ),
    "skiff_audit_events_total": _MetricSpec(
        kind="counter",
        labels=("event",),
        help="Audit event counter by declared event name.",
    ),
}


def known_metrics() -> frozenset[str]:
    return frozenset(_METRICS)


def spec_for(name: str) -> _MetricSpec | None:
    return _METRICS.get(name)


__all__ = ["known_metrics", "spec_for"]
