# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Image listing, pulling, pushing, tagging, inspecting; registry search proxy."""
from __future__ import annotations

import asyncio

import requests
import requests.exceptions
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from skiff.auth import AUTH, verify_csrf
from skiff.config import (
    IMAGE_PULL_TIMEOUT,
    RL_DEFAULT,
    RL_FAST,
    RL_SLOW,
    REGISTRY_DESC_MAX,
    REGISTRY_MAX_TAGS,
    REGISTRY_SEARCH_PAGE_SIZE,
    REGISTRY_TIMEOUT,
    _cfg,
    _limit,
    limiter,
)
from skiff.docker_client import docker_client_dep
from skiff.validators import (
    _redact_dict,
    safe_docker_call,
    validate_image_id,
    validate_image_registry,
)

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/api/registry/search", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_DEFAULT))
def registry_search(request: Request, q: str = Query(..., min_length=1, max_length=100)):
    """Proxy Docker Hub image search to avoid browser CORS restrictions."""
    try:
        resp = requests.get(
            "https://hub.docker.com/v2/search/repositories/",
            params={"query": q, "page_size": REGISTRY_SEARCH_PAGE_SIZE},
            timeout=REGISTRY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {
                "repo_name": item.get("repo_name") or item.get("name", ""),
                "short_description": (item.get("short_description") or "")[:REGISTRY_DESC_MAX],
                "pull_count": item.get("pull_count", 0),
                "is_official": bool(item.get("is_official")),
            }
            for item in data.get("results", [])
            if item.get("repo_name") or item.get("name")
        ]
        return {"results": results}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(502, f"Registry search failed: {exc}") from exc


@router.get("/api/registry/tags", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_DEFAULT))
def registry_tags(request: Request, image: str = Query(..., min_length=1, max_length=200)):
    """Fetch available tags for a Docker Hub image."""
    repo = image.strip("/")
    if "/" not in repo:
        repo = f"library/{repo}"
    try:
        resp = requests.get(
            f"https://hub.docker.com/v2/repositories/{repo}/tags/",
            params={"page_size": REGISTRY_MAX_TAGS, "ordering": "last_updated"},
            timeout=REGISTRY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        tags = [
            t["name"] for t in data.get("results", [])
            if isinstance(t.get("name"), str) and t["name"]
        ]
        return {"image": image, "tags": tags[:REGISTRY_MAX_TAGS]}
    except requests.exceptions.RequestException as exc:
        raise HTTPException(502, f"Tag fetch failed: {exc}") from exc


@router.get("/api/images", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_FAST))
def list_images(request: Request, client=Depends(docker_client_dep)) -> list[dict]:
    """Return all locally available Docker images."""
    images = safe_docker_call(client.images.list, all=False)
    return [
        {
            "id": img.short_id,
            "tags": img.tags,
            "created": img.attrs.get("Created", ""),
            "size_mb": round(img.attrs.get("Size", 0) / 1024 / 1024, 1),
        }
        for img in images
    ]


@router.get("/api/images/allowed", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_FAST))
def list_allowed_images(request: Request, client=Depends(docker_client_dep)) -> list[dict]:
    """Return images from allowed registries only."""
    images = safe_docker_call(client.images.list, all=False)
    result = []
    for img in images:
        for tag in img.tags:
            try:
                validate_image_registry(tag)
                result.append({
                    "id": img.short_id,
                    "tag": tag,
                    "size_mb": round(img.attrs.get("Size", 0) / 1024 / 1024, 1),
                })
                break
            except HTTPException:
                pass
    return result


@router.post("/api/images/pull", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_SLOW))
async def pull_image(request: Request, image: str, client=Depends(docker_client_dep)) -> dict:
    """Pull an image from an allowed registry."""
    verify_csrf(request)
    validate_image_registry(image)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, client.images.pull, image),
            timeout=IMAGE_PULL_TIMEOUT,
        )
    except TimeoutError as exc:
        raise HTTPException(504, "Image pull timed out") from exc
    except Exception as exc:
        raise HTTPException(400, f"Pull failed: {exc}") from exc
    log.info("image.pulled", image=image)
    return {"ok": True, "image": image}


@router.post("/api/images/{image_id}/tag", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_SLOW))
def tag_image(
    request: Request,
    image_id: str,
    repository: str,
    tag: str = "latest",
    client=Depends(docker_client_dep),
) -> dict:
    """Tag an image with a new name."""
    verify_csrf(request)
    validate_image_id(image_id)
    validate_image_registry(f"{repository}:{tag}")
    img = safe_docker_call(client.images.get, image_id)
    safe_docker_call(img.tag, repository, tag=tag)
    log.info("image.tagged", id=image_id, repository=repository, tag=tag)
    return {"ok": True}


@router.post("/api/images/push", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_SLOW))
async def push_image(request: Request, image: str, client=Depends(docker_client_dep)) -> dict:
    """Push an image to an allowed registry."""
    verify_csrf(request)
    validate_image_registry(image)
    loop = asyncio.get_running_loop()
    try:
        output = await asyncio.wait_for(
            loop.run_in_executor(None, client.images.push, image),
            timeout=IMAGE_PULL_TIMEOUT,
        )
    except TimeoutError as exc:
        raise HTTPException(504, "Image push timed out") from exc
    except Exception as exc:
        raise HTTPException(400, f"Push failed: {exc}") from exc
    # Check for error in streamed output
    if output and '"error"' in output:
        import json  # noqa: PLC0415
        for line in output.splitlines():
            try:
                d = json.loads(line)
                if "error" in d:
                    raise HTTPException(400, d["error"][:200])
            except (json.JSONDecodeError, HTTPException):
                raise
            except Exception:
                pass
    log.info("image.pushed", image=image)
    return {"ok": True, "image": image}


@router.delete("/api/images/{image_id}", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_SLOW))
def delete_image(
    request: Request, image_id: str, force: bool = False, client=Depends(docker_client_dep)
) -> dict:
    """Remove a local image by ID."""
    verify_csrf(request)
    validate_image_id(image_id)
    img = safe_docker_call(client.images.get, image_id)
    safe_docker_call(client.images.remove, img.id, force=force)
    log.info("image.deleted", id=image_id)
    return {"ok": True}


@router.get("/api/images/{image_id}/inspect", dependencies=AUTH, tags=["images"])
@limiter.limit(_limit(RL_DEFAULT))
def inspect_image(request: Request, image_id: str, client=Depends(docker_client_dep)) -> dict:
    """Return detailed image metadata and layer history."""
    validate_image_id(image_id)
    img = safe_docker_call(client.images.get, image_id)
    attrs = img.attrs
    history = safe_docker_call(client.api.history, img.id)
    return {
        "id": attrs["Id"][:19],
        "tags": attrs.get("RepoTags", []),
        "digests": attrs.get("RepoDigests", []),
        "created": attrs.get("Created", ""),
        "size_mb": round(attrs.get("Size", 0) / 1024 / 1024, 1),
        "virtual_size_mb": round(attrs.get("VirtualSize", 0) / 1024 / 1024, 1),
        "os": attrs.get("Os", ""),
        "architecture": attrs.get("Architecture", ""),
        "config": _redact_dict(attrs.get("Config", {})),
        "layers": len(attrs.get("RootFS", {}).get("Layers", [])),
        "history": [
            {
                "created": h.get("Created", ""),
                "created_by": h.get("CreatedBy", "")[:200],
                "size_mb": round(h.get("Size", 0) / 1024 / 1024, 3),
                "comment": h.get("Comment", ""),
            }
            for h in (history or [])[:20]
        ],
    }
