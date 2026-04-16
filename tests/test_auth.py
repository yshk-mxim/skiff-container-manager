"""Tests for authentication and CSRF protection."""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import AUTH_HEADER, TOKEN


@pytest.mark.unit
def test_missing_token_returns_401(client):
    resp = client.get("/api/containers")
    assert resp.status_code == 401


@pytest.mark.unit
def test_wrong_token_returns_401(client):
    resp = client.get("/api/containers", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_valid_token_accepted(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200


@pytest.mark.unit
def test_no_auth_when_token_unset(noauth_client):
    """With no API_TOKEN, all requests pass auth regardless of header."""
    mock = MagicMock()
    mock.containers.list.return_value = []
    with patch("skiff.docker_client.get_client", return_value=mock):
        resp = noauth_client.get("/api/containers")
    assert resp.status_code == 200


@pytest.mark.unit
def test_csrf_missing_on_post_returns_403(client):
    resp = client.post(
        "/api/containers/abc123/start",
        headers=AUTH_HEADER,  # no X-Requested-With
    )
    assert resp.status_code == 403


@pytest.mark.unit
def test_csrf_wrong_value_returns_403(client):
    resp = client.post(
        "/api/containers/abc123/start",
        headers={**AUTH_HEADER, "X-Requested-With": "WrongValue"},
    )
    assert resp.status_code == 403


@pytest.mark.unit
def test_csrf_not_required_on_get(client, mock_docker):
    mock_docker.containers.list.return_value = []
    resp = client.get("/api/containers", headers=AUTH_HEADER)
    assert resp.status_code == 200


@pytest.mark.unit
def test_timing_safe_comparison_rejects_prefix(client):
    """A token that is a prefix of the real token must be rejected."""
    short = TOKEN[:4]
    resp = client.get("/api/containers", headers={"Authorization": f"Bearer {short}"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_timing_safe_comparison_rejects_superstring(client):
    resp = client.get("/api/containers", headers={"Authorization": f"Bearer {TOKEN}extra"})
    assert resp.status_code == 401
