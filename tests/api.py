# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""HTTP test helper — one-line calls against the TestClient.

Before this helper, every test composed the `/api/...` URL by hand,
remembered to include AUTH_HEADER / AUTH_CSRF, and often reached into
resp.json() / resp.status_code / resp.json()["detail"]["code"] by
hand. 199 such sites across 21 test files. This module collapses
those to `api.post(client, "/containers/run", json={...})`.

Not a fixture — a module of free functions taking `client` explicitly.
Reason: pytest fixtures compose awkwardly with Hypothesis's @given and
with monkeypatch.setattr in the same test. Free functions pass through
`client` which is already a fixture.

Keeps AUTH headers centralised so changing the token format (future
R2b secret-str lift, OAuth2, ...) is one file edit.
"""

from __future__ import annotations

from typing import Any

from tests.conftest import AUTH_CSRF, AUTH_HEADER

_API_PREFIX = "/api"


def _url(path: str) -> str:
    """Normalise `path` so callers can pass either '/containers' or
    '/api/containers' — both reach the same endpoint."""
    if path.startswith(_API_PREFIX):
        return path
    if path.startswith("/"):
        return _API_PREFIX + path
    return _API_PREFIX + "/" + path


def get(client, path: str, *, headers: dict | None = None, **params: Any):
    """GET /api/<path> with AUTH headers. Extra query params pass through."""
    merged = dict(AUTH_HEADER)
    if headers:
        merged.update(headers)
    return client.get(_url(path), params=params, headers=merged)


def post(client, path: str, *, json: Any = None, headers: dict | None = None, **params: Any):
    """POST /api/<path> with AUTH_CSRF headers. Extra kwargs become query params.

    The body is `json` (JSON POST body); use `data=` by falling through
    to the raw TestClient call if a form-encoded body is needed for a
    specific test.
    """
    merged = dict(AUTH_CSRF)
    if headers:
        merged.update(headers)
    return client.post(_url(path), params=params, json=json, headers=merged)


def delete(client, path: str, *, headers: dict | None = None, **params: Any):
    """DELETE /api/<path> with AUTH_CSRF headers."""
    merged = dict(AUTH_CSRF)
    if headers:
        merged.update(headers)
    return client.delete(_url(path), params=params, headers=merged)


def put(client, path: str, *, json: Any = None, headers: dict | None = None, **params: Any):
    """PUT /api/<path> with AUTH_CSRF headers."""
    merged = dict(AUTH_CSRF)
    if headers:
        merged.update(headers)
    return client.put(_url(path), params=params, json=json, headers=merged)


# ── Shortcut helpers for common shapes ─────────────────────────────


def ok(resp) -> dict:
    """Assert 2xx and return resp.json(). One-liner replacement for
    `assert r.status_code == 200; data = r.json()`."""
    assert 200 <= resp.status_code < 300, f"expected 2xx got {resp.status_code}: {resp.text[:200]}"
    return resp.json()


def error_code(resp) -> str:
    """Assert a 4xx/5xx and return the catalogue `code`. Pairs with the
    R4 structured-detail shape: {code, message, [help], [extra keys]}."""
    assert resp.status_code >= 400, f"expected 4xx/5xx got {resp.status_code}"
    body = resp.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail.get("code", "")
    # Legacy string-detail path (auth.py / validators.py raw raises not
    # yet migrated). Return a synthetic code so test assertions have
    # something comparable, until R4-followup lands.
    return ""


__all__ = ["delete", "error_code", "get", "ok", "post", "put"]
