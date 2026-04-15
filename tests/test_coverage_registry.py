"""Tests for registry proxy endpoints."""

from unittest.mock import MagicMock, patch

import requests.exceptions

from tests.conftest import AUTH_HEADER


def test_registry_search_success(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {
                "repo_name": "nginx",
                "short_description": "Official nginx image",
                "pull_count": 1000000,
                "is_official": True,
            },
            {
                "name": "alpine",
                "short_description": "",
                "pull_count": 500000,
                "is_official": False,
            },
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    with patch("app.requests.get", return_value=mock_resp):
        resp = client.get("/api/registry/search?q=nginx", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert len(data["results"]) == 2


def test_registry_search_filters_empty_names(client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "results": [
            {"repo_name": "", "name": "", "short_description": "", "pull_count": 0, "is_official": False},
            {"repo_name": "nginx", "short_description": "", "pull_count": 0, "is_official": False},
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    with patch("app.requests.get", return_value=mock_resp):
        resp = client.get("/api/registry/search?q=nginx", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


def test_registry_search_timeout_returns_502(client):
    with patch("app.requests.get", side_effect=requests.exceptions.Timeout("timeout")):
        resp = client.get("/api/registry/search?q=nginx", headers=AUTH_HEADER)
    assert resp.status_code == 502


def test_registry_search_connection_error_returns_502(client):
    with patch("app.requests.get", side_effect=requests.exceptions.ConnectionError("failed")):
        resp = client.get("/api/registry/search?q=nginx", headers=AUTH_HEADER)
    assert resp.status_code == 502


def test_registry_tags_official_image(client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "results": [
            {"name": "latest"},
            {"name": "1.25"},
            {"name": ""},  # empty, should be filtered
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    with patch("app.requests.get", return_value=mock_resp) as mock_get:
        resp = client.get("/api/registry/tags?image=nginx", headers=AUTH_HEADER)
        # Verify library/ prefix was added
        call_url = mock_get.call_args[0][0]
        assert "library/nginx" in call_url
    assert resp.status_code == 200
    data = resp.json()
    assert "latest" in data["tags"]
    assert "1.25" in data["tags"]
    assert "" not in data["tags"]


def test_registry_tags_user_image(client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"results": [{"name": "v1.0"}]}
    mock_resp.raise_for_status = MagicMock()
    with patch("app.requests.get", return_value=mock_resp) as mock_get:
        resp = client.get("/api/registry/tags?image=myuser/myimage", headers=AUTH_HEADER)
        call_url = mock_get.call_args[0][0]
        assert "myuser/myimage" in call_url
    assert resp.status_code == 200
    assert resp.json()["image"] == "myuser/myimage"


def test_registry_tags_error_returns_502(client):
    with patch("app.requests.get", side_effect=requests.exceptions.RequestException("error")):
        resp = client.get("/api/registry/tags?image=nginx", headers=AUTH_HEADER)
    assert resp.status_code == 502
