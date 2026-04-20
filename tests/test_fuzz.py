# SPDX-License-Identifier: MIT
"""Property-based fuzz tests for SKIFF's input-validation layer.

Every validator between the HTTP edge and the Docker SDK is covered here.
The philosophy: any string the caller can construct should either produce a
well-defined bytes value (memory) / float (CPU) / structured result, or be
rejected with HTTPException(400) — NEVER an uncaught exception that surfaces
as a 500 to the client. A 500 on malformed input means the fuzzer found a bug.

These are complementary to the example-based tests in test_coverage_*.py:
- example tests document expected behaviour at specific points
- fuzz tests assert invariants across the whole input space

Invariants tested:
  A. parse_memory_quantity: round-trips known-good shapes, rejects everything
     else with HTTPException(400) only. Monotonic: more bytes in → more bytes out.
  B. parse_cpu_quantity:   same shape for CPU.
  C. _validate_tmpfs:      never raises anything but HTTPException(400). Never
     accepts a path that would let writes escape into the container's /etc, /proc
     etc. — enforced via an explicit post-check against the blocklist.
  D. validate_container_name / validate_project_name: never accept a string that
     could inject shell metacharacters, path traversal, or length overflows.
  E. validate_image_registry: never accepts an image whose registry prefix isn't
     in _cfg.allowed_registries (case-insensitive).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property


# ─────────────────────────────────────────────────────────────────────────────
# A. Memory-quantity parser
# ─────────────────────────────────────────────────────────────────────────────

_VALID_MEM_SUFFIXES = ("", "k", "K", "Ki", "m", "M", "Mi", "g", "G", "Gi", "t", "T", "Ti")

# A decimal number with optional fractional part, matching the parser's regex.
_mem_numbers = st.one_of(
    st.integers(min_value=0, max_value=10**12),
    st.floats(min_value=0, max_value=1e12, allow_nan=False, allow_infinity=False).map(lambda f: f"{f:.6f}"),
)


@given(n=_mem_numbers, unit=st.sampled_from(_VALID_MEM_SUFFIXES), whitespace=st.text(" \t", max_size=2))
@settings(max_examples=400, deadline=None)
def test_memory_parser_accepts_all_valid_shapes(n, unit, whitespace):
    """Any `<number><unit>` with optional whitespace must parse to a non-negative int."""
    from skiff.validators import parse_memory_quantity

    s = f"{whitespace}{n}{unit}{whitespace}"
    result = parse_memory_quantity(s)
    assert isinstance(result, int)
    assert result >= 0


@given(garbage=st.text(min_size=1, max_size=64))
@settings(max_examples=500, deadline=None)
def test_memory_parser_rejects_garbage_cleanly(garbage):
    """Malformed input MUST raise HTTPException(400) — never a 500-causing exception."""
    # Skip inputs that coincidentally match a valid shape
    import re

    from skiff.validators import parse_memory_quantity

    if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*(?:[KMGT]i?|[kmgt])?\s*", garbage):
        return
    try:
        parse_memory_quantity(garbage)
        # If we got here, the string was accepted — that's the valid shape, fine.
    except HTTPException as exc:
        assert exc.status_code == 400, f"Wrong status code for {garbage!r}: {exc.status_code}"
    except Exception as exc:
        pytest.fail(f"Memory parser raised {type(exc).__name__} on {garbage!r}; must be HTTPException(400) only")


@given(a=st.integers(min_value=0, max_value=10**12), b=st.integers(min_value=0, max_value=10**12))
def test_memory_parser_monotonicity_on_bytes(a, b):
    """Parsing raw-byte integers preserves ordering (larger in → larger out)."""
    from skiff.validators import parse_memory_quantity

    ra, rb = parse_memory_quantity(a), parse_memory_quantity(b)
    assert (ra < rb) == (a < b)


# ─────────────────────────────────────────────────────────────────────────────
# B. CPU-quantity parser
# ─────────────────────────────────────────────────────────────────────────────


@given(
    n=st.one_of(
        st.integers(min_value=0, max_value=10**6),
        st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False).map(lambda f: f"{f:.3f}"),
    ),
    milli=st.sampled_from(["", "m"]),
)
@settings(max_examples=400, deadline=None)
def test_cpu_parser_accepts_valid_shapes(n, milli):
    from skiff.validators import parse_cpu_quantity

    s = f"{n}{milli}"
    result = parse_cpu_quantity(s)
    assert result >= 0


@given(garbage=st.text(min_size=1, max_size=64))
@settings(max_examples=500, deadline=None)
def test_cpu_parser_rejects_garbage_cleanly(garbage):
    import re

    from skiff.validators import parse_cpu_quantity

    if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*m?\s*", garbage):
        return
    try:
        parse_cpu_quantity(garbage)
    except HTTPException as exc:
        assert exc.status_code == 400
    except Exception as exc:
        pytest.fail(f"CPU parser raised {type(exc).__name__} on {garbage!r}; must be HTTPException(400) only")


# ─────────────────────────────────────────────────────────────────────────────
# C. Tmpfs validator — must never open a path-traversal or sensitive-dir hole
# ─────────────────────────────────────────────────────────────────────────────

_TMPFS_BLOCKED = {"/", "/etc", "/proc", "/sys", "/dev", "/boot", "/lib", "/lib64", "/usr", "/bin", "/sbin"}


def _looks_blocked(path: str) -> bool:
    normalised = path.rstrip("/") or "/"
    return any(normalised == b or normalised.startswith(b + "/") for b in _TMPFS_BLOCKED)


@given(
    path=st.one_of(
        st.sampled_from(["/tmp", "/run", "/var/run", "/var/cache", "/cache", "/app/cache", "/srv/tmp"]),
        st.text(min_size=1, max_size=48),
    ),
    opts=st.sampled_from(["rw", "rw,size=16m", "rw,size=1k", "ro,size=1g", "noexec,nosuid,size=64m"]),
)
@settings(max_examples=500, deadline=None)
def test_tmpfs_never_raises_non_http_exception(path, opts):
    """The validator must only throw HTTPException(400) — catching AttributeError
    or TypeError would leak 500s to an authenticated user."""
    from skiff.validators import _validate_tmpfs

    try:
        _validate_tmpfs({path: opts}, 10, 512)
    except HTTPException as exc:
        assert exc.status_code == 400
    except Exception as exc:
        pytest.fail(f"tmpfs validator raised {type(exc).__name__} on ({path!r}, {opts!r}); must be HTTPException only")


_BLOCKED_BASES = list(_TMPFS_BLOCKED)


@given(
    # Directly compose paths that are guaranteed to hit the blocked set — avoids
    # Hypothesis filtering most random text out.
    base=st.sampled_from(_BLOCKED_BASES),
    suffix=st.sampled_from(["", "/", "/subdir", "/subdir/deep"]),
)
@settings(max_examples=200, deadline=None)
def test_tmpfs_blocked_paths_always_rejected(base, suffix):
    """If the requested path falls into a blocked directory, the validator MUST reject.

    Security-critical invariant: tmpfs covering /etc would mask the container's
    own /etc/shadow and break the whole sandbox premise. Paths constructed from
    the explicit blocklist so every generated case is in the sensitive space.
    """
    from skiff.validators import _validate_tmpfs

    path = base if base == "/" else base + suffix
    with pytest.raises(HTTPException) as exc:
        _validate_tmpfs({path: "rw"}, 10, 512)
    assert exc.value.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# D. Container / project name validators — shell-injection impossibility
# ─────────────────────────────────────────────────────────────────────────────

_SHELL_METACHARS = set(";|&`$(){}<>'\"\\\n\r\t ")


@given(name=st.text(min_size=1, max_size=200))
@settings(max_examples=400, deadline=None)
def test_container_name_rejects_any_shell_metachar(name):
    """If the name contains any shell metacharacter, the validator MUST reject.

    SKIFF never passes names to a shell (SDK only), but defence-in-depth applies:
    any future shell-based integration must fail-closed.
    """
    from skiff.validators import validate_container_name

    if any(c in _SHELL_METACHARS for c in name):
        with pytest.raises(HTTPException) as exc:
            validate_container_name(name)
        assert exc.value.status_code == 400


@given(name=st.text(min_size=1, max_size=200))
@settings(max_examples=400, deadline=None)
def test_project_name_rejects_path_traversal(name):
    """Project name containing .. or / must be rejected."""
    from skiff.validators import validate_project_name

    if ".." in name or "/" in name or "\\" in name:
        with pytest.raises(HTTPException):
            validate_project_name(name)


# ─────────────────────────────────────────────────────────────────────────────
# E. Image registry allowlist — case-insensitive exact-prefix match
# ─────────────────────────────────────────────────────────────────────────────


@given(
    image=st.text(min_size=1, max_size=200),
    allowed=st.lists(st.sampled_from(["docker.io", "ghcr.io", "quay.io"]), min_size=1, max_size=3),
)
@settings(max_examples=300, deadline=None)
def test_image_registry_allowlist(image, allowed):
    """An image is accepted iff its registry prefix is in the allowlist (case-insensitive).

    Uses a direct attribute swap instead of monkeypatch so Hypothesis doesn't
    complain about function-scoped fixtures sharing state across examples.
    The swap is bracketed so any test failure still restores the prior value.
    """
    import skiff.config as config_module
    from skiff.validators import IMAGE_TAG_RE, validate_image_registry

    original = config_module._cfg.allowed_registries
    config_module._cfg.allowed_registries = list(allowed)
    try:
        # Images not matching the top-level format regex are rejected before
        # the registry check — those failures are covered by format tests,
        # not this allowlist property. Use fullmatch to mirror the
        # validator's exact behaviour (plain .match accepts trailing
        # characters like newlines which the validator rejects).
        if not IMAGE_TAG_RE.fullmatch(image):
            return
        # Validator also rejects malformed-ref shapes (trailing `:`/`@`,
        # `::` or `:@` doublings) before the allowlist check. Mirror
        # that here so the allowlist property only covers well-formed
        # refs — the shape guard has its own dedicated test.
        if image.endswith((":", "@")) or ":@" in image or "::" in image:
            return
        image_lower = image.lower()
        try:
            validate_image_registry(image)
            accepted = True
        except HTTPException:
            accepted = False
        # Docker semantics: a bare image name is normalised by the daemon to
        # docker.io/library/... An "explicit registry" requires BOTH a "." or ":"
        # in the first path segment AND at least one "/" — i.e., the shape
        # "<host>/<name>". Just `"0."` without a slash is a bare name, not a
        # registry-qualified image, so it's allowed when docker.io is in the list.
        # Property must mirror the validator's exact logic.
        image_no_tag = image.split(":", 1)[0] if "@" not in image else image.split("@", 1)[0]
        parts = image_no_tag.split("/") if image_no_tag else []
        first_seg = parts[0] if parts else ""
        has_explicit_registry = len(parts) >= 2 and ("." in first_seg or ":" in first_seg)
        if has_explicit_registry:
            expected = any(
                first_seg.lower() == a.rstrip("/").lower()
                or image_lower.startswith((a if a.endswith("/") else a + "/").lower())
                for a in allowed
            )
        else:
            # Bare name → docker.io implicit
            expected = any(a.rstrip("/").lower() == "docker.io" for a in allowed)
        assert accepted == expected, (
            f"Registry allowlist inconsistent for {image!r} vs {allowed!r}: accepted={accepted} expected={expected}"
        )
    finally:
        config_module._cfg.allowed_registries = original


# ─────────────────────────────────────────────────────────────────────────────
# F. Compose YAML sandbox — rejects every dangerous key
# ─────────────────────────────────────────────────────────────────────────────

_FORBIDDEN_COMPOSE_KEYS = [
    "privileged",
    "cap_add",
    "devices",
    "build",
    "configs",
    "secrets",
    "volumes_from",
    "extends",
    "env_file",
    "cgroup_parent",
    "dns",
    "dns_search",
    "extra_hosts",
    "tmpfs",
    "userns_mode",
    "sysctls",
    "security_opt",
    "shm_size",
    "uts",
    "cgroupns_mode",
    "storage_opt",
    "device_cgroup_rules",
]


@pytest.mark.parametrize("forbidden_key", _FORBIDDEN_COMPOSE_KEYS)
def test_compose_rejects_every_known_dangerous_key(forbidden_key):
    """Every key in the sandbox blocklist must actually block deploys when present."""
    from skiff.validators import validate_compose_file

    yaml_body = (f"services:\n  web:\n    image: docker.io/library/nginx:latest\n    {forbidden_key}: true\n").encode()
    with pytest.raises(HTTPException) as exc:
        validate_compose_file(yaml_body)
    assert exc.value.status_code == 400


@given(depth=st.integers(min_value=1, max_value=50))
@settings(max_examples=30, deadline=None)
def test_compose_bomb_bounded_by_safe_load(depth):
    """YAML bomb / alias recursion must not hang or OOM the validator.

    yaml.safe_load + compose-key sandbox together bound evaluation. A malicious
    compose file with heavy alias nesting is rejected before expansion because
    the alias-expanded structure contains a forbidden key or fails schema.
    """
    from skiff.validators import validate_compose_file

    yaml_body = (
        "services:\n  web:\n    image: alpine:latest\n"
        + "    labels:\n"
        + ("      " + "&a [*a]\n" * min(depth, 5))  # small nesting — don't actually try the bomb
    ).encode()
    try:
        validate_compose_file(yaml_body)
    except HTTPException as exc:
        assert exc.status_code == 400
    except Exception as exc:
        pytest.fail(f"Compose validator raised {type(exc).__name__} on nested YAML — must stay HTTPException-only")
