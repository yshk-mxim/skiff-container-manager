# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Pydantic request bodies for mutating routes.

Each body model pins the JSON shape the client sends. Handlers take a
single `body: XRequest` parameter instead of a dozen `Body(default=...)`
kwargs, and FastAPI produces a faithful OpenAPI schema for it.

Type-level invariants live in the model; domain validation (registry
allowlists, mount-target blocks, tmpfs option syntax) lives in
`skiff.validators` so routes can share the checks and error codes
stay in the `http_error` catalogue.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RunContainerRequest(BaseModel):
    """Body for POST /api/containers/run.

    Query params (image, name) stay on the URL so clients can use the
    existing /api/containers/run?image=... pattern. Everything else is
    body-only.

    `model_config.extra = "forbid"` rejects unknown fields with 422
    before the handler sees them — defence against a client passing
    `{"privileged": true}` and hoping for surprise behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    ports: dict[str, str] | None = Field(default=None, description="containerPort/proto → hostPort")
    environment: list[str] | None = Field(default=None, description="KEY=VALUE strings")
    command: str | None = Field(default=None, description="override CMD; max 4096 chars")
    volumes: list[str] | None = Field(default=None, description="name:/path[:ro|rw] entries")
    restart_policy: str | None = Field(
        default=None,
        description="no | on-failure | unless-stopped | always",
    )
    network: str | None = Field(default=None, description="user-defined network name")
    labels: dict[str, str] | None = Field(default=None, description="Docker label map")
    read_only: bool = Field(default=True, description="mount rootfs read-only + auto tmpfs")
    tmpfs: dict[str, str] | None = Field(default=None, description="containerPath → mount options")
    inherit_from: str | None = Field(default=None, description="source container ID to inherit env from")
    replace_id: str | None = Field(default=None, description="container ID to stop+remove on success")


__all__ = ["RunContainerRequest"]
