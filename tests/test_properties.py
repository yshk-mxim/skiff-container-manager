# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""
Property-based tests using Hypothesis.

These tests generate random inputs and verify invariants that must hold
for all inputs, catching edge cases that hand-crafted examples miss.
"""

import re
import string

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st

from skiff.validators import (
    CONTAINER_ID_RE,
    PROJECT_NAME_RE,
    validate_compose_file,
    validate_container_id,
    validate_image_registry,
    validate_project_name,
)

# ── Container ID ──────────────────────────────────────────────────────────────

VALID_HEX_CHARS = string.hexdigits[:16]  # "0123456789abcdef"


@given(st.text(alphabet=VALID_HEX_CHARS, min_size=4, max_size=64))
@settings(max_examples=200)
@pytest.mark.unit
def test_valid_container_id_always_passes(hex_id):
    """Any string of 4-64 lowercase hex characters must pass validation."""
    result = validate_container_id(hex_id)
    assert result == hex_id


@given(
    st.one_of(
        # Over-length: beyond both the 64-char hex cap AND the 128-char name cap.
        st.text(alphabet=VALID_HEX_CHARS, min_size=129, max_size=200),
        # Punctuation that is NOT in either the hex-id charset or the
        # container-name charset [a-zA-Z0-9_.-].
        st.text(
            alphabet=(string.punctuation.replace("_", "").replace(".", "").replace("-", "")),
            min_size=4,
            max_size=12,
        ),
    )
)
@settings(max_examples=200)
@pytest.mark.unit
def test_invalid_container_id_always_rejected(bad_id):
    """Inputs outside BOTH the hex-id and container-name regexes must be rejected."""
    # validate_container_id now accepts either a hex id OR a container
    # name (Docker's SDK resolves either). Skip any generated string
    # that happens to match either regex.
    from skiff.validators import CONTAINER_NAME_RE

    if CONTAINER_ID_RE.match(bad_id) or CONTAINER_NAME_RE.match(bad_id):
        return
    with pytest.raises(HTTPException) as exc:
        validate_container_id(bad_id)
    assert exc.value.status_code == 400


# ── Project name ──────────────────────────────────────────────────────────────

VALID_PROJECT_CHARS = string.ascii_lowercase + string.digits + "-_"
VALID_PROJECT_START = string.ascii_lowercase + string.digits


@given(
    st.text(alphabet=VALID_PROJECT_START, min_size=1, max_size=1),
    st.text(alphabet=VALID_PROJECT_CHARS, min_size=0, max_size=62),
)
@settings(max_examples=200)
@pytest.mark.unit
def test_valid_project_name_always_passes(first_char, rest):
    name = first_char + rest
    result = validate_project_name(name)
    assert result == name


@given(st.text(alphabet=string.ascii_uppercase + string.punctuation, min_size=1, max_size=20))
@settings(max_examples=200)
@pytest.mark.unit
def test_uppercase_project_names_rejected(name):
    """Project names with uppercase letters must always be rejected."""
    if PROJECT_NAME_RE.match(name):
        return
    with pytest.raises(HTTPException) as exc:
        validate_project_name(name)
    assert exc.value.status_code == 400


# ── Image registry ────────────────────────────────────────────────────────────

ALLOWED_PREFIX = "docker.io/"


@given(st.text(alphabet=string.ascii_letters + string.digits + "/._:-", min_size=1, max_size=50))
@settings(max_examples=300)
@pytest.mark.unit
def test_image_without_allowed_prefix_rejected(suffix):
    """Any image that does NOT start with the allowed registry must be rejected,
    unless it happens to form a valid allowed image (skip those)."""
    # Images that start with the allowed prefix would pass — exclude them
    image = f"evil.example.com/{suffix}"
    # Only test if it doesn't accidentally start with allowed registry
    if image.startswith(ALLOWED_PREFIX.rstrip("/")):
        return
    with pytest.raises(HTTPException) as exc:
        validate_image_registry(image)
    assert exc.value.status_code == 400


@given(
    st.text(alphabet=string.ascii_letters + string.digits + "/._:-@", min_size=1, max_size=80),
)
@settings(max_examples=300)
@pytest.mark.unit
def test_valid_allowed_registry_image_passes(path):
    """Any image prefixed with the allowed registry and a valid path must pass,
    unless it contains characters invalid per IMAGE_TAG_RE."""
    image = ALLOWED_PREFIX + path
    # Skip if the image contains characters that would fail format validation
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,255}$", image):
        return
    # Skip malformed refs that the Loop-9 shape-guard explicitly rejects
    # (trailing `:` or `@`, `::` double separator, `:@` empty tag).
    if image.endswith((":", "@")) or ":@" in image or "::" in image:
        return
    # Should not raise for any valid-format image from the allowed registry
    validate_image_registry(image)


# ── Compose file size limit ───────────────────────────────────────────────────

# Source the live cap instead of hardcoding — the defaults.toml value has
# changed over time (256 KiB → 2 MiB when real compose stacks started
# tripping the limit), and a stale constant here silently makes the test
# stop exercising oversize rejection.
from skiff import config as _skiff_config

MAX_COMPOSE_SIZE = _skiff_config.MAX_COMPOSE_SIZE


@given(st.integers(min_value=MAX_COMPOSE_SIZE + 1, max_value=MAX_COMPOSE_SIZE + 4096))
@settings(max_examples=20)
@pytest.mark.unit
def test_oversized_compose_always_rejected(size):
    """Any compose file larger than the configured cap must be rejected."""
    content = b"x" * size
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(content)
    assert exc.value.status_code == 400


@given(st.integers(min_value=0, max_value=100))
@settings(max_examples=50)
@pytest.mark.unit
def test_small_invalid_yaml_never_crashes(size):
    """Random small byte strings must never crash the validator — only raise HTTPException."""
    content = b"@" * size  # always invalid YAML structure
    try:
        validate_compose_file(content)
    except HTTPException:
        pass  # expected: invalid YAML or invalid structure
    except Exception as exc:
        pytest.fail(f"Unexpected exception type {type(exc).__name__}: {exc}")
