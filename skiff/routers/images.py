# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Image listing, pulling, pushing, tagging, inspecting; registry search proxy."""

from __future__ import annotations

import asyncio
import json
import socket as _socket
from typing import Any

import docker.errors
import requests
import requests.exceptions
import urllib3.exceptions as _urllib3_exc
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from skiff import config, validators
from skiff.auth import AUTH
from skiff.contract.errors import http_error
from skiff.contract.responses import (
    AllowedImageEntry,
    ImageInspectResponse,
    ImageSummary,
    OkResponse,
    UndoableResponse,
    _ImageHistoryEntry,
)
from skiff.docker_client import docker_client_dep
from skiff.rate import RATE
from skiff.secure import secure_route

router = APIRouter()

# Narrow tuple of "transport died mid-request" errors. Intentionally
# does NOT include docker.errors.DockerException (APIError is a
# subclass — a 400 "manifest not found" should surface as
# image.pull_failed, not 503). Mirrors the transport subset of
# DOCKER_TRANSIENT in skiff.docker_client without the base
# DockerException catch-all.
_TRANSPORT_ERRORS = (
    _urllib3_exc.ProtocolError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    _socket.timeout,
    ConnectionError,
)


@router.get("/api/registry/search", dependencies=AUTH, tags=["images"])
@secure_route.read(RATE.READ)
def registry_search(request: Request, q: str = Query(..., min_length=1, max_length=100)):
    """Proxy Docker Hub image search to avoid browser CORS restrictions."""
    try:
        resp = requests.get(
            "https://hub.docker.com/v2/search/repositories/",
            params={"query": q, "page_size": config.REGISTRY_SEARCH_PAGE_SIZE},
            timeout=config.REGISTRY_TIMEOUT,
            # SSRF hardening: refuse redirects. The URL host is hard-coded
            # to hub.docker.com; if the hub ever serves a 3xx to another
            # host we don't want to follow it transparently.
            allow_redirects=False,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {
                "repo_name": item.get("repo_name") or item.get("name", ""),
                "short_description": (item.get("short_description") or "")[: config.REGISTRY_DESC_MAX],
                "pull_count": item.get("pull_count", 0),
                "is_official": bool(item.get("is_official")),
            }
            for item in data.get("results", [])
            if item.get("repo_name") or item.get("name")
        ]
        return {"results": results}
    except requests.exceptions.RequestException as exc:
        raise http_error("image.registry_search_failed", message=f"Registry search failed: {exc}") from exc


@router.get("/api/registry/tags", dependencies=AUTH, tags=["images"])
@secure_route.read(RATE.READ)
def registry_tags(request: Request, image: str = Query(..., min_length=1, max_length=200)):
    """Fetch available tags for a Docker Hub image."""
    repo = image.strip("/")
    if "/" not in repo:
        repo = f"library/{repo}"
    # Reject anything outside Docker Hub's repo-name alphabet — belt and
    # braces on top of FastAPI's length limit so we never interpolate an
    # exotic codepoint into the upstream URL.
    # SSRF posture: the host is HARD-CODED to `hub.docker.com` below.
    # `repo` is additionally constrained by `HUB_REPO_RE` which excludes
    # `@`, `:`, backslash, and any scheme-introducing character, so the
    # f-string cannot be redirected to an attacker-controlled origin.
    # `allow_redirects=False` refuses any 3xx response so hub.docker.com
    # itself cannot bounce us off-host either. Semgrep's `ssrf-requests`
    # rule is a generic warning for `requests.get(f"...{var}...")` and
    # does not see the regex constraint; the finding is a false positive
    # under this combined-mitigation posture.
    if not validators.HUB_REPO_RE.fullmatch(repo):
        raise http_error("validation.bad_image_name")
    try:
        resp = requests.get(
            f"https://hub.docker.com/v2/repositories/{repo}/tags/",
            params={"page_size": config.REGISTRY_MAX_TAGS, "ordering": "last_updated"},
            timeout=config.REGISTRY_TIMEOUT,
            allow_redirects=False,
        )
        resp.raise_for_status()
        data = resp.json()
        tags = [t["name"] for t in data.get("results", []) if isinstance(t.get("name"), str) and t["name"]]
        return {"image": image, "tags": tags[: config.REGISTRY_MAX_TAGS]}
    except requests.exceptions.RequestException as exc:
        raise http_error("image.tag_fetch_failed", message=f"Tag fetch failed: {exc}") from exc


@router.get("/api/images", dependencies=AUTH, tags=["images"])
@secure_route.read(RATE.READ)
def list_images(request: Request, client=Depends(docker_client_dep)) -> list[ImageSummary]:
    """Return all locally available Docker images."""
    images = validators.safe_docker_call(client.images.list, all=False)
    return [ImageSummary.from_docker(img) for img in images]


def _first_allowed_tag(img: Any) -> AllowedImageEntry | None:
    """Return the first tag on `img` whose registry is allowed, else None."""
    for tag in img.tags:
        try:
            validators.validate_image_registry(tag)
        except HTTPException:
            continue
        return AllowedImageEntry(
            id=img.short_id,
            tag=tag,
            size_mb=round((img.attrs.get("Size") or 0) / 1024 / 1024, 1),
        )
    return None


@router.get("/api/images/allowed", dependencies=AUTH, tags=["images"])
@secure_route.read(RATE.READ)
def list_allowed_images(
    request: Request,
    client=Depends(docker_client_dep),
) -> list[AllowedImageEntry]:
    """Return images from allowed registries only."""
    images = validators.safe_docker_call(client.images.list, all=False)
    entries = (_first_allowed_tag(img) for img in images)
    return [entry for entry in entries if entry is not None]


@router.post("/api/images/pull", dependencies=AUTH, tags=["images"])
@secure_route.mutate(
    RATE.WRITE,
    audit="image.pulled",
    audit_fields=lambda request, image, **kw: {"image": image},  # noqa: ARG005
)
async def pull_image(request: Request, image: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Pull an image from an allowed registry."""
    validators.validate_image_registry(image)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, client.images.pull, image),
            timeout=config.IMAGE_PULL_TIMEOUT,
        )
    except TimeoutError as exc:
        raise http_error("image.pull_timed_out") from exc
    except docker.errors.ImageNotFound as exc:
        # Upstream registry says the repo or tag doesn't exist
        # (`alpine:typo`, `user/nope`). Map to the documented 404
        # envelope instead of collapsing to the generic 400
        # image.pull_failed — scripted callers need to distinguish
        # "typo" from "transient network error".
        raise http_error("image.not_found") from exc
    except docker.errors.NotFound as exc:
        # Older docker-py versions raise the broader NotFound for
        # missing manifests; same semantic.
        raise http_error("image.not_found") from exc
    except _TRANSPORT_ERRORS as exc:
        # Transport failure during the streamed pull (urllib3
        # ProtocolError from a mid-stream tunnel drop, requests
        # ConnectionError / Timeout, socket.timeout). Narrow tuple
        # — does NOT include the broader docker.errors.DockerException
        # because APIError is a subclass of that and a 400 "manifest
        # not found" should surface as image.pull_failed, not 503.
        raise http_error("system.docker_unreachable") from exc
    except docker.errors.DockerException as exc:
        raise http_error("image.pull_failed") from exc
    return OkResponse(image=image)


@router.post("/api/images/{image_id}/tag", dependencies=AUTH, tags=["images"])
@secure_route.mutate(
    RATE.WRITE,
    audit="image.tagged",
    audit_fields=lambda request, image_id, repository, tag="latest", **kw: (  # noqa: ARG005
        {"id": image_id, "repository": repository, "tag": tag}
    ),
)
def tag_image(
    request: Request,
    image_id: str,
    repository: str,
    tag: str = "latest",
    client=Depends(docker_client_dep),
) -> OkResponse:
    """Tag an image with a new name."""
    validators.validate_image_id(image_id)
    validators.validate_image_registry(f"{repository}:{tag}")
    img = validators.safe_docker_call(client.images.get, image_id)
    validators.safe_docker_call(img.tag, repository, tag=tag)
    return OkResponse()


def _first_push_error(output: str | None) -> str | None:
    """Parse streamed push output for an error line. Returns the error or None.

    Docker's push streams JSON-per-line; a push failure embeds
    `{"error": "…"}`. We only scan when `"error"` appears in the raw
    string to skip JSON-parse overhead on the happy path.
    """
    if not output or '"error"' not in output:
        return None
    for line in output.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in d:
            return d["error"][:200]
    return None


async def _run_push(image: str, client) -> str | None:
    """Execute the Docker push in the executor, translating errors to http_error."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, client.images.push, image),
            timeout=config.IMAGE_PULL_TIMEOUT,
        )
    except TimeoutError as exc:
        raise http_error("image.push_timed_out") from exc
    except _TRANSPORT_ERRORS as exc:
        # Same transport-level failure handling as pull — mid-stream
        # tunnel death raises urllib3.ProtocolError which the bare
        # `docker.errors.DockerException` catch misses. 503 instead
        # of a leaked 500 + traceback.
        raise http_error("system.docker_unreachable") from exc
    except docker.errors.DockerException as exc:
        raise http_error("image.push_failed") from exc


@router.post("/api/images/push", dependencies=AUTH, tags=["images"])
@secure_route.mutate(
    RATE.WRITE,
    audit="image.pushed",
    audit_fields=lambda request, image, **kw: {"image": image},  # noqa: ARG005
)
async def push_image(request: Request, image: str, client=Depends(docker_client_dep)) -> OkResponse:
    """Push an image to an allowed registry."""
    validators.validate_image_registry(image)
    output = await _run_push(image, client)
    err = _first_push_error(output)
    if err:
        raise http_error("image.push_failed", message=err)
    return OkResponse(image=image)


@router.delete("/api/images/{image_id}", dependencies=AUTH, tags=["images"])
@secure_route.mutate(RATE.WRITE)
def delete_image(
    request: Request,
    image_id: str,
    force: bool = False,
    undo: bool = False,
    client=Depends(docker_client_dep),
) -> Any:
    """Remove a local image by ID. With `undo=true`, removal is delayed 5 s and
    the response carries an `undo_token` the caller can POST to cancel."""
    validators.validate_image_id(image_id)
    img = validators.safe_docker_call(client.images.get, image_id)
    if undo:
        from skiff.undo import get_queue

        token = get_queue().enqueue(
            "image",
            image_id,
            validators.safe_docker_call,
            client.images.remove,
            img.id,
            force=force,
        )
        if token is not None:
            import structlog

            structlog.get_logger(__name__).info(
                "image.delete_queued",
                id=image_id,
                token_suffix=token[-6:],
            )
            return UndoableResponse(undo_token=token, expires_in=config.UNDO_DELAY_SECS)
    validators.safe_docker_call(client.images.remove, img.id, force=force)
    import structlog

    structlog.get_logger(__name__).info("image.deleted", id=image_id)
    return OkResponse()


def _history_created_to_iso(created: Any) -> str:
    """Normalize `created` from Docker's image.history() to an ISO-8601 string.

    Unlike `image.attrs["Created"]` (which is an ISO-8601 string),
    `image.history()` returns each layer's `Created` as a Unix timestamp
    int. The UI presents history entries side-by-side with the top-level
    created field, so we coerce to ISO here at the boundary to give the
    contract model one type. An empty value → "" so downstream string
    operations don't blow up.
    """
    from datetime import UTC, datetime

    if isinstance(created, (int, float)) and created > 0:
        return datetime.fromtimestamp(created, tz=UTC).isoformat().replace("+00:00", "Z")
    return str(created or "")


def _image_history_entries(img: Any) -> list[_ImageHistoryEntry]:
    """Best-effort history — an engine error yields an empty list, not a 503.

    History is cosmetic: a missing one shouldn't fail the whole inspect.
    That's why this doesn't go through `safe_docker_call`.
    """
    try:
        raw = img.history() or []
    except docker.errors.DockerException:
        return []
    return [
        _ImageHistoryEntry(
            created=_history_created_to_iso(h.get("Created")),
            created_by=(h.get("CreatedBy") or "")[:200],
            size_mb=round((h.get("Size") or 0) / 1024 / 1024, 3),
            comment=h.get("Comment") or "",
        )
        for h in raw[:20]
    ]


@router.get("/api/images/{image_id}/inspect", dependencies=AUTH, tags=["images"])
@secure_route.read(RATE.READ)
def inspect_image(
    request: Request,
    image_id: str,
    client=Depends(docker_client_dep),
) -> ImageInspectResponse:
    """Return detailed image metadata and layer history."""
    validators.validate_image_id(image_id)
    img = validators.safe_docker_call(client.images.get, image_id)
    return ImageInspectResponse.from_docker(
        img,
        history=_image_history_entries(img),
        redacted_config=validators._redact_dict(img.attrs.get("Config") or {}),
    )
