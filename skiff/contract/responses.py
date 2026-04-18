# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Response envelope Pydantic models.

Every mutating HTTP handler returns one of these envelopes
(`OkResponse`, `UndoableResponse`) so FastAPI emits a concrete OpenAPI
schema and the client can destructure `response.ok` uniformly. Inspect
endpoints (volumes, images, containers) use the dedicated
`*InspectResponse` models with `from_docker()` classmethods.

`model_config.extra = "forbid"` on every model: accidental extra keys
surface as test failures, not silently-passed payload drift.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer


class OkResponse(BaseModel):
    """Successful mutating operation. `ok=True` is always present.

    Additional fields are declared per-use-case — either on a subclass or
    via the generic `extra` dict when a one-off needs a richer body. Prefer
    subclassing when the same shape appears in ≥2 routes.

    `model_config.exclude_none` is baked into `model_dump()` at serialisation
    time via FastAPI's `response_model_exclude_none` — we default it here so
    every route gets the trimmed body without per-call plumbing. A
    container.start with nothing but `ok=True` now serialises to
    `{"ok": true}` instead of `{"ok": true, "id": null, "name": null, ...}`.
    """

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True

    # Common optional fields. Using Optional[...] on the base class so
    # callers don't have to define a new subclass for every tiny variant.
    id: str | None = None
    name: str | None = None
    image: str | None = None
    socket_path: str | None = None
    docker_host: str | None = None
    output: str | None = None
    cancelled: bool | None = None
    count: int | None = None

    @model_serializer(mode="wrap")
    def _omit_null_fields(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Drop `None` fields from the serialised shape.

        FastAPI serialises via `model_dump()` internally; the wrap-mode
        serializer runs on both that path and direct `model_dump()` calls.
        Net effect: `return OkResponse(id=xid)` emits `{"ok":true,"id":"xid"}`
        instead of the older `{"ok":true,"id":"xid","name":null,...,"count":null}`.
        """
        data = handler(self)
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items() if v is not None}


class UndoableResponse(BaseModel):
    """Destructive operation deferred by the UndoQueue.

    Returned when a DELETE was successfully enqueued but not yet fired —
    the caller has `expires_in` seconds to POST /api/undo/{token} to cancel.
    If the client received an OkResponse instead, the op already ran
    synchronously (queue full, undo disabled, etc.).
    """

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    undo_token: str = Field(..., description="Opaque token for POST /api/undo/{token}")
    expires_in: float = Field(..., description="Seconds remaining before the op fires")


class ErrorResponse(BaseModel):
    """FastAPI's default 4xx body wraps `detail`; we preserve that but add a
    machine-readable `code` so clients can switch on it without parsing strings.

    Not raised directly — the error-code catalogue (skiff.contract.errors)
    produces HTTPException(status_code, detail={"code": code, "message": ...})
    and FastAPI serialises it as this shape.
    """

    model_config = ConfigDict(extra="forbid")
    detail: dict[str, Any] = Field(
        ...,
        description="Error body: {code: str, message: str, help?: str}",
    )


class _ContainerConfigSection(BaseModel):
    """Pydantic-validated Config.* subsection of a container inspect response."""

    model_config = ConfigDict(extra="forbid")

    env: list[str] = Field(default_factory=list)
    cmd: list[str] | None = None
    entrypoint: list[str] | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    exposed_ports: list[str] = Field(default_factory=list)
    working_dir: str = ""
    user: str = ""
    hostname: str = ""
    tty: bool = False


class _ContainerHostConfigSection(BaseModel):
    """HostConfig.* subsection — the full resource surface the UI can read.

    Fields here match the `/api/containers/{id}/update` mutable surface:
    memory / cpu / pids_limit / restart_policy are live-updatable; ports /
    volumes / readonly_rootfs / security_opt / tmpfs require recreate.
    """

    model_config = ConfigDict(extra="forbid")

    port_bindings: dict[str, Any] = Field(default_factory=dict)
    restart_policy: dict[str, Any] = Field(default_factory=dict)
    binds: list[str] = Field(default_factory=list)
    memory_bytes: int = 0
    memory_reservation_bytes: int = 0
    cpu_shares: int = 0
    cpu_quota: int = 0
    cpu_period: int = 0
    nano_cpus: int = 0
    pids_limit: int = 0
    readonly_rootfs: bool = False
    security_opt: list[str] = Field(default_factory=list)
    tmpfs: dict[str, str] = Field(default_factory=dict)


class _ContainerHealthSection(BaseModel):
    """State.Health + Config.Healthcheck. `status='none'` when neither is set."""

    model_config = ConfigDict(extra="forbid")

    status: str = "none"
    failing_streak: int = 0
    test: list[str] | None = None
    log: list[dict[str, Any]] = Field(default_factory=list)


class _ContainerNetworkEntry(BaseModel):
    """One attached network's IP / gateway / mac."""

    model_config = ConfigDict(extra="forbid")

    ip_address: str = ""
    gateway: str = ""
    mac_address: str = ""


class _ContainerMountEntry(BaseModel):
    """One volume/bind mount entry (`type` is `volume` / `bind` / `tmpfs`)."""

    model_config = ConfigDict(extra="forbid")

    type: str = ""
    name: str = ""
    source: str = ""
    destination: str = ""
    mode: str = ""
    rw: bool = True


class ContainerInspectResponse(BaseModel):
    """Full shape for GET /api/containers/{id}/inspect.

    Assembled server-side from the Docker SDK's trust-untyped attrs dict
    so the client receives a single validated payload. Each sub-section
    is its own model: a change to one surface (e.g. a new field in
    Config) touches only one class.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    image: str
    created: str
    state: dict[str, Any]
    restart_count: int = 0
    platform: str = ""
    config: _ContainerConfigSection
    host_config: _ContainerHostConfigSection
    health_check: _ContainerHealthSection
    network: dict[str, _ContainerNetworkEntry] = Field(default_factory=dict)
    mounts: list[_ContainerMountEntry] = Field(default_factory=list)


class VolumeInspectResponse(BaseModel):
    """Response shape for GET /api/volumes/{name}/inspect.

    Built via `VolumeInspectResponse.from_docker(vol, containers)`.
    Malformed Docker attrs surface as a validation error at the
    boundary instead of silently degrading the response.

    `usage_bytes=-1` and `ref_count=-1` are the documented
    "driver-doesn't-report" sentinels. A volume driver that ships a
    UsageData dict surfaces the real numbers.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    driver: str = ""
    mountpoint: str = ""
    created: str = ""
    scope: str = ""
    status: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)
    usage_bytes: int = -1  # -1 = driver doesn't report
    ref_count: int = -1
    containers: list[str] = Field(default_factory=list)

    @classmethod
    def from_docker(cls, vol: Any, containers: list[str]) -> VolumeInspectResponse:
        """Build from a Docker SDK Volume object + pre-computed container list.

        The SDK's `.attrs` dict is trust-untyped JSON — we pick the fields
        we know about, coerce None-values to their declared defaults, and
        let Pydantic validate the rest.
        """
        attrs = vol.attrs or {}
        usage = attrs.get("UsageData") or {}
        return cls(
            name=vol.name,
            driver=attrs.get("Driver") or "",
            mountpoint=attrs.get("Mountpoint") or "",
            created=attrs.get("CreatedAt") or "",
            scope=attrs.get("Scope") or "",
            status=attrs.get("Status") or {},
            labels=attrs.get("Labels") or {},
            options=attrs.get("Options") or {},
            usage_bytes=usage.get("Size", -1) if usage else -1,
            ref_count=usage.get("RefCount", -1) if usage else -1,
            containers=containers,
        )


class ContainerSummary(BaseModel):
    """One row in GET /api/containers. Replaces the ad-hoc `attrs.get()` dict.

    `state` is the engine-level lifecycle ("running"/"exited"/…); `status`
    is the Docker SDK's derived human string ("Up 3 hours"). Both are kept
    because the UI uses `state` for classification and `status` for display.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    image: str
    status: str
    state: str = "unknown"
    health: str = "none"
    ports: dict[str, Any] = Field(default_factory=dict)
    created: str = ""

    @classmethod
    def from_docker(cls, c: Any) -> ContainerSummary:
        """Build from a Docker SDK Container object.

        Handles the orphaned-image case (referenced digest pruned) by
        falling back to `image='unknown'` — a container whose image was
        pruned is still listable and we must not 500 the endpoint.
        """
        # Orphan image (pruned digest) → c.image may raise ImageNotFound.
        # Keep docker.errors out of this module — this is the fallback path.
        try:
            image_name = c.image.tags[0] if c.image.tags else c.image.short_id
        except Exception:
            image_name = "unknown"
        state = (c.attrs.get("State") or {})
        health_dict = state.get("Health") if isinstance(state.get("Health"), dict) else None
        return cls(
            id=c.short_id,
            name=c.name,
            image=image_name,
            status=c.status,
            state=state.get("Status", "unknown"),
            health=(health_dict or {}).get("Status", "none"),
            ports=c.ports or {},
            created=c.attrs.get("Created") or "",
        )


class ImageSummary(BaseModel):
    """One row in GET /api/images."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tags: list[str] = Field(default_factory=list)
    created: str = ""
    size_mb: float = 0.0

    @classmethod
    def from_docker(cls, img: Any) -> ImageSummary:
        return cls(
            id=img.short_id,
            tags=img.tags or [],
            created=img.attrs.get("Created") or "",
            size_mb=round((img.attrs.get("Size") or 0) / 1024 / 1024, 1),
        )


class AllowedImageEntry(BaseModel):
    """One `(id, tag)` entry in GET /api/images/allowed — only tags whose
    registry passes `validate_image_registry` are emitted."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tag: str
    size_mb: float = 0.0


class _ImageHistoryEntry(BaseModel):
    """One layer in Image.history() output."""

    model_config = ConfigDict(extra="forbid")

    created: str = ""
    created_by: str = ""
    size_mb: float = 0.0
    comment: str = ""


class ImageInspectResponse(BaseModel):
    """Response for GET /api/images/{image_id}/inspect. `history` is best-effort
    (an engine error makes it `[]` rather than failing the whole inspect)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tags: list[str] = Field(default_factory=list)
    digests: list[str] = Field(default_factory=list)
    created: str = ""
    size_mb: float = 0.0
    virtual_size_mb: float = 0.0
    os: str = ""
    architecture: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    layers: int = 0
    history: list[_ImageHistoryEntry] = Field(default_factory=list)

    @classmethod
    def from_docker(
        cls,
        img: Any,
        *,
        history: list[_ImageHistoryEntry],
        redacted_config: dict[str, Any],
    ) -> ImageInspectResponse:
        """Build from a Docker SDK Image object.

        `history` and `redacted_config` are pre-computed by the caller to
        keep this module free of dependencies on `skiff.validators` and
        `docker.errors` (contract modules are the dependency sink, not a
        source).
        """
        attrs = img.attrs or {}
        return cls(
            id=attrs["Id"][:19],
            tags=attrs.get("RepoTags") or [],
            digests=attrs.get("RepoDigests") or [],
            created=attrs.get("Created") or "",
            size_mb=round((attrs.get("Size") or 0) / 1024 / 1024, 1),
            virtual_size_mb=round((attrs.get("VirtualSize") or 0) / 1024 / 1024, 1),
            os=attrs.get("Os") or "",
            architecture=attrs.get("Architecture") or "",
            config=redacted_config,
            layers=len((attrs.get("RootFS") or {}).get("Layers") or []),
            history=history,
        )


class NetworkSummary(BaseModel):
    """One row in GET /api/networks. `containers` maps short-id → name for
    every endpoint currently attached to the network."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    driver: str = ""
    scope: str = ""
    internal: bool = False
    ipam: list[dict[str, Any]] = Field(default_factory=list)
    containers: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_docker(cls, n: Any) -> NetworkSummary:
        attrs = n.attrs or {}
        return cls(
            id=n.short_id,
            name=n.name,
            driver=attrs.get("Driver") or "",
            scope=attrs.get("Scope") or "",
            internal=attrs.get("Internal", False),
            ipam=(attrs.get("IPAM") or {}).get("Config") or [],
            containers={
                cid[:12]: info.get("Name", "")
                for cid, info in (attrs.get("Containers") or {}).items()
            },
        )


class VolumeSummary(BaseModel):
    """One row in GET /api/volumes. `in_use` is derived from the
    pre-computed `containers` list so the UI doesn't have to JOIN."""

    model_config = ConfigDict(extra="forbid")

    name: str
    driver: str = ""
    mountpoint: str = ""
    created: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    in_use: bool = False
    containers: list[str] = Field(default_factory=list)

    @classmethod
    def from_docker(cls, v: Any, containers: list[str]) -> VolumeSummary:
        attrs = v.attrs or {}
        return cls(
            name=v.name,
            driver=attrs.get("Driver") or "",
            mountpoint=attrs.get("Mountpoint") or "",
            created=attrs.get("CreatedAt") or "",
            labels=attrs.get("Labels") or {},
            in_use=len(containers) > 0,
            containers=containers,
        )


__all__ = [
    "AllowedImageEntry",
    "ContainerInspectResponse",
    "ContainerSummary",
    "ErrorResponse",
    "ImageInspectResponse",
    "ImageSummary",
    "NetworkSummary",
    "OkResponse",
    "UndoableResponse",
    "VolumeInspectResponse",
    "VolumeSummary",
]
