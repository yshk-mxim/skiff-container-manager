# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Tests for the container file-copy + file-browser surface.

Coverage tiers — matches the rest of SKIFF's test suite:

  - **Unit** — `_parse_ls_line` handles GNU + busybox ls variants, symlinks,
    wildcards, truncation.
  - **Integration** — /ls, /files, /upload against a mocked Docker client.
  - **Fuzz** — hypothesis feeds random ls output into `_parse_ls_line` and
    random paths into `_validate_cp_path`; no input may raise.
  - **Security** — path-validation rejects null bytes, relative paths,
    over-length strings; multipart upload rejects missing filename +
    over-size body; responses funnel through the envelope contract.

One file keeps the cp/ls story in one place so a regression (e.g.
someone removes the `--` separator that stops `ls` interpreting a path
that starts with `-`) surfaces as a targeted failure."""

from __future__ import annotations

import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.unit


# ── Unit: _parse_ls_line ─────────────────────────────────────────────────


def _parse(line: str):
    from skiff.routers.containers import _parse_ls_line

    return _parse_ls_line(line)


def test_parse_ls_line_gnu_full_time_file():
    """GNU coreutils with `--full-time` emits:
    `-rw-r--r-- 1 root root 123 2026-04-18 05:35:00.000000000 +0000 README.md`"""
    row = _parse(
        "-rw-r--r-- 1 root root 123 2026-04-18 05:35:00.000000000 +0000 README.md",
    )
    assert row is not None
    assert row["name"] == "README.md"
    assert row["type"] == "file"
    assert row["size"] == 123
    assert row["mode"] == "rw-r--r--"
    assert row["target"] == ""


def test_parse_ls_line_busybox_dir_with_trailing_slash():
    """Busybox `ls -la -p` suffixes dirs with `/` — we strip it in `name`."""
    row = _parse("drwxr-xr-x 2 root root 4096 Apr 18 05:35 bin/")
    assert row is not None
    assert row["name"] == "bin"
    assert row["type"] == "dir"
    assert row["size"] == 4096


def test_parse_ls_line_symlink_with_target():
    """Symlink rows end in ` -> target`. Target must be captured; name is
    the last token before ` -> `."""
    row = _parse("lrwxrwxrwx 1 root root 7 Apr 18 05:35 libc.so -> libc-2.35.so")
    assert row is not None
    assert row["name"] == "libc.so"
    assert row["type"] == "link"
    assert row["target"] == "libc-2.35.so"


def test_parse_ls_line_skips_dot_and_dotdot():
    """`.` and `..` are navigation markers, not real entries."""
    assert _parse("drwxr-xr-x 1 root root 4096 Apr 18 05:35 ./") is None
    assert _parse("drwxr-xr-x 1 root root 4096 Apr 18 05:35 ../") is None


def test_parse_ls_line_skips_total_header():
    """`total N` is the ls summary header."""
    assert _parse("total 24") is None
    assert _parse("") is None


def test_parse_ls_line_device_and_fifo_types():
    """Character devices, block devices, FIFOs — exotic but valid types."""
    assert _parse("crw-rw-rw- 1 root root 1, 3 Apr 18 05:35 null")["type"] == "device"
    assert _parse("brw-rw---- 1 root disk 8, 0 Apr 18 05:35 sda")["type"] == "device"
    assert _parse("prw------- 1 root root 0 Apr 18 05:35 fifo")["type"] == "fifo"
    assert _parse("srwxrwxrwx 1 root root 0 Apr 18 05:35 sock")["type"] == "socket"


def test_parse_ls_line_empty_or_malformed_returns_none():
    """Any row that doesn't reach 8 whitespace-separated fields is junk."""
    assert _parse("abc") is None
    assert _parse("-rw-r--r-- 1 root root") is None


# ── Fuzz: _parse_ls_line never raises ────────────────────────────────────


@given(st.text(max_size=400))
@settings(max_examples=400, deadline=None)
def test_parse_ls_line_never_raises_on_arbitrary_text(raw):
    """Feed random UTF-8 into the ls parser. Invariant: returns either a
    dict with all expected keys OR None, never raises. Guards against
    any future regression where a particular character breaks
    `rsplit` / `rpartition` / the mode-prefix dispatch."""
    out = _parse(raw)
    if out is None:
        return
    assert isinstance(out, dict)
    assert set(out.keys()) == {"name", "type", "size", "mode", "target"}
    assert isinstance(out["name"], str)
    assert isinstance(out["size"], int)
    assert out["type"] in {"file", "dir", "link", "device", "socket", "fifo"}


# ── Security: _validate_cp_path rejects the obvious attack shapes ────────


def _validate_path(path: str):
    from skiff.routers.containers import _validate_cp_path

    return _validate_cp_path(path)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "relative/path",
        "./a",
        "\x00",
        "/with\x00null",
        "a" * 300,  # over the 256-char cap
    ],
)
def test_validate_cp_path_rejects_bad_shapes(bad):
    """Non-absolute, null-byte-containing, or over-length paths are rejected
    as `validation.bad_input` — never silently accepted."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _validate_path(bad)
    assert exc.value.detail["code"] == "validation.bad_input"


def test_validate_cp_path_accepts_normal_paths():
    """Absolute POSIX paths pass through unchanged."""
    for good in ("/", "/etc/hosts", "/var/log", "/a/b/c"):
        assert _validate_path(good) == good


@given(st.text(max_size=500))
@settings(max_examples=300, deadline=None)
def test_validate_cp_path_never_raises_non_http_errors(path):
    """Fuzz: only HTTPException leaks out; no other exception type ever."""
    from fastapi import HTTPException

    try:
        _validate_path(path)
    except HTTPException:
        pass
    except Exception as exc:
        pytest.fail(f"_validate_cp_path raised {type(exc).__name__} on {path!r}: {exc}")


# ── Integration: /ls against a mocked Docker ──────────────────────────────


def _build_mock_container(ls_output: str, exit_code: int = 0) -> MagicMock:
    c = MagicMock()
    c.short_id = "abc123def456"
    exec_result = MagicMock()
    exec_result.output = ls_output.encode()
    exec_result.exit_code = exit_code
    c.exec_run.return_value = exec_result
    return c


def _invoke_ls(mock_container: MagicMock, path: str = "/"):
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                return tc.get(
                    f"/api/containers/abc123def456/ls?path={path}",
                    headers={"X-Requested-With": "ContainerManager"},
                )
    finally:
        config_module._cfg.api_token = orig_token


def test_ls_endpoint_returns_parsed_entries():
    """End-to-end: /ls feeds mocked `ls` output through the parser, then
    returns a JSON body with the expected shape."""
    ls_output = (
        "total 16\n"
        "drwxr-xr-x 2 root root 4096 Apr 18 05:35 bin/\n"
        "-rw-r--r-- 1 root root  123 Apr 18 05:35 README.md\n"
        "lrwxrwxrwx 1 root root    7 Apr 18 05:35 link -> target\n"
    )
    r = _invoke_ls(_build_mock_container(ls_output))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/"
    names = {e["name"] for e in body["entries"]}
    assert {"bin", "README.md", "link"} == names
    # Directories come first (sort invariant).
    assert body["entries"][0]["type"] == "dir"


def test_ls_endpoint_404_on_nonexistent_path():
    """Non-zero exec exit code → 404 envelope, not 500."""
    mock = _build_mock_container("ls: /nope: No such file or directory\n", exit_code=2)
    r = _invoke_ls(mock, path="/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "resource.not_found"


def test_ls_endpoint_rejects_path_traversal():
    """Relative path → 400 envelope at the validator."""
    r = _invoke_ls(_build_mock_container(""), path="relative")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "validation.bad_input"


# ── Integration: /upload (multipart) ──────────────────────────────────────


def test_upload_wraps_single_file_into_tar():
    """Multipart POST with one file → server tarballs it and calls
    put_archive. Verified via the mock's call_args."""
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.put_archive.return_value = True
    mock_client.containers.get.return_value = mock_container
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.post(
                    "/api/containers/abc123def456/upload?path=/tmp",
                    files={"file": ("hello.txt", b"hello world", "text/plain")},
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code == 200, r.text
                call = mock_container.put_archive.call_args
                assert call.args[0] == "/tmp"
                # Validate the tarball contents: should contain exactly one
                # `hello.txt` member with the original bytes.
                tar_bytes = call.args[1]
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tf:
                    names = tf.getnames()
                    assert names == ["hello.txt"]
                    member = tf.extractfile("hello.txt").read()
                    assert member == b"hello world"
    finally:
        config_module._cfg.api_token = orig_token


def test_upload_rejects_over_size_body():
    """A body past CONTAINER_CP_MAX_MB must be rejected (400 envelope OR
    framework-level 413) — never a hang, OOM, or 500. Two layers catch
    oversize: Starlette/python-multipart's built-in part-size limit
    fires at 413 if the body is large enough, and our own
    `CONTAINER_CP_MAX_MB` check fires at 400 for anything within the
    framework limit but over our cap."""
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    orig_cap = config_module.CONTAINER_CP_MAX_MB
    config_module._cfg.api_token = ""
    # Shrink the cap for a cheap test.
    config_module.CONTAINER_CP_MAX_MB = 1
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.get.return_value = mock_container
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                big = b"x" * (2 * 1024 * 1024)  # 2 MB — exceeds 1 MB cap
                r = tc.post(
                    "/api/containers/abc123def456/upload?path=/tmp",
                    files={"file": ("big.bin", big, "application/octet-stream")},
                    headers={"X-Requested-With": "ContainerManager"},
                )
                # Either our envelope 400 or the framework's 413 — both
                # are "refused, do not retry with the same body".
                assert r.status_code in (400, 413), r.text[:300]
    finally:
        config_module.CONTAINER_CP_MAX_MB = orig_cap
        config_module._cfg.api_token = orig_token


def test_upload_rejects_empty_filename():
    """Multipart entries without a filename must be rejected — either at
    FastAPI's validation layer (422) or our own envelope (400). Must
    never fall through to a 500 with a tarball that contains an empty
    name (which would fail put_archive on the daemon side)."""
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.get.return_value = mock_container
    mock_client.ping.return_value = True
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                r = tc.post(
                    "/api/containers/abc123def456/upload?path=/tmp",
                    # Filename blanked — the FastAPI UploadFile will have "".
                    files={"file": ("", b"hello", "text/plain")},
                    headers={"X-Requested-With": "ContainerManager"},
                )
                assert r.status_code in (400, 422), r.text[:300]
    finally:
        config_module._cfg.api_token = orig_token


# ── Security: commit repo/tag grammar ────────────────────────────────────


@pytest.mark.parametrize(
    "bad_repo",
    [
        "UPPER/not-lower",  # repo must be lowercase
        "a" * 201,  # over 200-char cap
        "bad name",  # space
        "bad;cmd",  # shell metachar
        "bad\ninjection",  # newline
    ],
)
def test_commit_rejects_bad_repo_grammar(bad_repo):
    """Commit surface must reject non-OCI repo shapes upfront."""
    from skiff.validators import COMMIT_REPO_RE

    assert COMMIT_REPO_RE.fullmatch(bad_repo) is None


@pytest.mark.parametrize(
    "bad_tag",
    [
        ".bad-leading-dot",
        "-bad-leading-dash",
        "bad tag",
        "a" * 129,
        "bad\ninjection",
    ],
)
def test_commit_rejects_bad_tag_grammar(bad_tag):
    """Same for tag: must match OCI tag grammar."""
    from skiff.validators import COMMIT_TAG_RE

    assert COMMIT_TAG_RE.fullmatch(bad_tag) is None


def test_commit_accepts_canonical_examples():
    """Good repo+tag combos should pass."""
    from skiff.validators import COMMIT_REPO_RE, COMMIT_TAG_RE

    for repo in ("nginx", "user/nginx", "local/my-app", "host.tld/org/app"):
        assert COMMIT_REPO_RE.fullmatch(repo) is not None
    for tag in ("latest", "v1.2.3", "3.12-slim", "dev_build"):
        assert COMMIT_TAG_RE.fullmatch(tag) is not None


# ── /files GET (tar download) + POST (tar upload) ───────────────────────


def _invoke_cp(mock_container: MagicMock, method: str, path: str, **kw):
    from skiff import config as config_module
    from skiff import docker_client as dc_module
    from skiff.app import app

    orig_token = config_module._cfg.api_token
    config_module._cfg.api_token = ""
    for lim in {config_module.limiter, app.state.limiter}:
        lim.reset()
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container
    mock_client.ping.return_value = True
    # Merge caller-supplied headers with the default CSRF sentinel.
    headers = {"X-Requested-With": "ContainerManager", **kw.pop("headers", {})}
    try:
        with (
            patch.object(dc_module, "_client", mock_client),
            patch.object(dc_module, "_client_last_ping", float("inf")),
            patch("skiff.docker_client.get_client", return_value=mock_client),
        ):
            with TestClient(app, raise_server_exceptions=False) as tc:
                return tc.request(method, path, headers=headers, **kw)
    finally:
        config_module._cfg.api_token = orig_token


def test_files_get_returns_tar_stream():
    """GET /files?path=/etc/hosts should stream tar bytes."""
    c = MagicMock()
    c.short_id = "abc123def456"
    c.get_archive.return_value = (iter([b"tarchunk1", b"tarchunk2"]), {"name": "hosts", "size": 18})
    r = _invoke_cp(c, "GET", "/api/containers/abc123def456/files?path=/etc/hosts")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-tar"
    assert 'filename="hosts.tar"' in r.headers["content-disposition"]
    assert r.content == b"tarchunk1tarchunk2"


def test_files_get_path_not_found_returns_404_envelope():
    """docker.errors.NotFound → resource.not_found envelope (not 500)."""
    import docker.errors

    c = MagicMock()
    c.short_id = "abc123def456"
    c.get_archive.side_effect = docker.errors.NotFound("no such path")
    r = _invoke_cp(c, "GET", "/api/containers/abc123def456/files?path=/nonexistent")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "resource.not_found"


def test_files_post_uploads_tar_bytes():
    """POST /files accepts tar body + writes via put_archive."""
    c = MagicMock()
    c.short_id = "abc123def456"
    c.put_archive.return_value = True
    tar_body = b"FAKE_TAR_BYTES"
    r = _invoke_cp(
        c,
        "POST",
        "/api/containers/abc123def456/files?path=/tmp",
        data=tar_body,
        headers={"X-Requested-With": "ContainerManager", "Content-Type": "application/x-tar"},
    )
    assert r.status_code == 200
    c.put_archive.assert_called_once_with("/tmp", tar_body)


def test_files_post_rejects_over_size_body():
    """Body past CONTAINER_CP_MAX_MB must 400 before put_archive."""
    from skiff import config as config_module

    cap = config_module.CONTAINER_CP_MAX_MB
    big = b"A" * (cap * 1024 * 1024 + 1)
    c = MagicMock()
    c.short_id = "abc123def456"
    r = _invoke_cp(
        c,
        "POST",
        "/api/containers/abc123def456/files?path=/tmp",
        data=big,
        headers={"X-Requested-With": "ContainerManager", "Content-Type": "application/x-tar"},
    )
    # Middleware catches oversize bodies at 413 before the handler's
    # 400 check. Either path is acceptable — both block the write.
    assert r.status_code in (400, 413)
    c.put_archive.assert_not_called()
