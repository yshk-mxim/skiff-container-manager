# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Pure-data contract definitions shared across the server.

This package is intentionally dependency-free (no FastAPI, no Docker SDK, no
structlog) so it can be imported from any layer — validators, routers,
tests, future CLI — without pulling the world. Every module here declares a
catalogue (response envelopes, error codes, audit events, metrics) and
makes it the single source of truth.

Modules:
  responses    Pydantic envelope models (OkResponse, UndoableResponse,
               ErrorResponse) that routers return instead of ad-hoc dicts.
  errors       Error-code catalogue mapping `code → (http_status, message,
               help_url)`. Raise helpers ensure every 4xx is tagged.
  events       Audit event catalogue listing every `log.info("<name>")`
               used for audit logging. `assert_event` rejects drift.
  metrics      Prometheus metric-name registry so metrics don't get renamed
               silently after v1 ships.

Design rule: modules here have only standard-library + pydantic imports.
"""
from __future__ import annotations
