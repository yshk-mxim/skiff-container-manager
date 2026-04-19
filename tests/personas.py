# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
"""Persona catalogue for the driver-seat audit harness.

Seven personas, each with a mental model, interaction style, and a
concrete acceptance rubric. Journeys declare which personas they support
via the `@journey(persona=…)` decorator; the harness picks per-persona
conditions (viewport, theme, typing cadence, rate-limit scale) when
driving the journey.

Design choice: personas are dataclasses, not enums, because each row
carries both policy (rate-limit scale) and UX expectations (viewport,
interaction cadence, done rubric). A journey can introspect the
persona for "how should I click here" without a global switch.

See `docs/dev/personas.md` for the narrative map that pairs each
persona with pages they visit; that document is the source of truth
for which persona SHOULD have access to which page.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Viewport:
    """Browser viewport for Playwright. Matches devices the target users
    actually run: desktop 14-inch, desktop 24-inch, tablet, mobile."""

    name: str
    width: int
    height: int


_DESKTOP_14 = Viewport("desktop-14in", 1280, 800)
_DESKTOP_24 = Viewport("desktop-24in", 1920, 1080)
_TABLET_10 = Viewport("tablet-10in", 1024, 768)
_MOBILE_IPHONE_13 = Viewport("mobile-iphone13", 390, 844)
_MOBILE_PIXEL_5 = Viewport("mobile-pixel5", 393, 851)


@dataclass(frozen=True)
class Persona:
    """One driver-seat personality. Journeys dispatch on persona.tag
    (e.g. `novice`) so adding a new persona doesn't mean editing every
    journey — only the ones that explicitly want to opt in."""

    tag: str  # 'novice', 'developer', etc
    name: str  # human-readable
    rate_limit_profile: str  # maps to skiff/_config/profiles.toml
    description: str  # one-line mental model
    click_delay_ms: int  # typing / click cadence
    preferred_viewport: Viewport  # default browser size
    supports_viewports: tuple[Viewport, ...]  # acceptable alternates
    prefers_theme: str  # 'light' | 'dark' | 'system'
    uses_keyboard_shortcuts: bool  # ⌘K, Tab-nav
    uses_palette: bool  # opens ⌘K for nav
    starting_pages: tuple[str, ...]  # first nav targets
    done_rubric: tuple[str, ...]  # acceptance criteria
    driving_heuristics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Defensive — keeps the catalogue honest even though dataclass
        # types already check.
        assert self.tag, "persona tag is required"
        assert self.preferred_viewport in self.supports_viewports, (
            f"{self.tag}: preferred_viewport must be in supports_viewports"
        )


NOVICE = Persona(
    tag="novice",
    name="Novice operator",
    rate_limit_profile="tutor",
    description=(
        "Docker is magic; clicks the thing that looks right. Reads every prompt. Prefers chips, suggestions, templates."
    ),
    click_delay_ms=1200,
    preferred_viewport=_DESKTOP_14,
    supports_viewports=(_DESKTOP_14, _DESKTOP_24),
    prefers_theme="system",
    uses_keyboard_shortcuts=False,
    uses_palette=False,
    starting_pages=("dashboard", "templates", "containers"),
    done_rubric=(
        "Can deploy postgres / nginx / python-dev in ≤5 clicks from wizard exit.",
        "Every error message has an inline recovery action.",
        "No terminology on screen that hasn't appeared elsewhere in the UI.",
        "Zero free-text typing required for the common deploys.",
    ),
    driving_heuristics=(
        "Waits for animations to finish before clicking.",
        "Re-reads tooltips before committing destructive actions.",
        "Gives up and reads the help tour if confused.",
    ),
)


DEVELOPER = Persona(
    tag="developer",
    name="Developer",
    rate_limit_profile="dev",
    description="Lives in docker-compose.yml and the Terminal tab.",
    click_delay_ms=300,
    preferred_viewport=_DESKTOP_14,
    supports_viewports=(_DESKTOP_14, _DESKTOP_24),
    prefers_theme="dark",
    uses_keyboard_shortcuts=True,
    uses_palette=True,
    starting_pages=("containers", "compose", "images"),
    done_rubric=(
        "Can edit a file in a running container (exec OR Files→edit→upload) and see the change in ≤90s.",
        "⌘K palette jumps to any container by name.",
        "Tab key follows visual order in every modal.",
        "Enter submits every non-multiline form.",
    ),
    driving_heuristics=(
        "Prefers keyboard over mouse.",
        "Uses the audit log to understand state changes.",
    ),
)


SRE_OPS = Persona(
    tag="sre_ops",
    name="SRE / Ops operator",
    rate_limit_profile="sre",
    description="Watches stats, follows logs, prunes, scales compose services.",
    click_delay_ms=400,
    preferred_viewport=_DESKTOP_24,
    supports_viewports=(_DESKTOP_24, _DESKTOP_14),
    prefers_theme="dark",
    uses_keyboard_shortcuts=True,
    uses_palette=True,
    starting_pages=("dashboard", "system", "compose"),
    done_rubric=(
        "Can diagnose a failing compose stack via logs+events+audit.",
        "Scale / restart / pull a stack without leaving SKIFF.",
        "Prometheus scrape returns valid metrics.",
        "Audit log filter + download covers forensic needs.",
    ),
    driving_heuristics=(
        "Correlates stderr → audit → UI state.",
        "Checks system/df before pruning.",
    ),
)


SECURITY_REVIEWER = Persona(
    tag="security_reviewer",
    name="Security reviewer",
    rate_limit_profile="reviewer",
    description=(
        "Tries every mutation button; pastes `..` / `\\0` / `file://` / "
        "forged origins into every input; never actually wants to change state."
    ),
    click_delay_ms=600,
    preferred_viewport=_DESKTOP_14,
    supports_viewports=(_DESKTOP_14,),
    prefers_theme="light",
    uses_keyboard_shortcuts=False,
    uses_palette=False,
    starting_pages=("system", "containers", "images"),
    done_rubric=(
        "No mutation reaches the daemon (reviewer gate active server-side).",
        "No credentials / paths / hostnames leak to DOM or stderr or audit.",
        "Every 4xx returns a catalogued envelope; no stack traces.",
        "No new route surfaces outside the OpenAPI spec.",
    ),
    driving_heuristics=(
        "Treats every input as a potential injection vector.",
        "Reads the Network tab for unexpected requests.",
    ),
)


UI_UX_AUDITOR = Persona(
    tag="ui_ux_auditor",
    name="UI/UX auditor",
    rate_limit_profile="dev",
    description=(
        "Tab-only navigation; dark/light/high-contrast; mobile viewport; prefers-reduced-motion; screen-reader labels."
    ),
    click_delay_ms=300,
    preferred_viewport=_DESKTOP_14,
    supports_viewports=(_DESKTOP_14, _TABLET_10, _MOBILE_IPHONE_13, _MOBILE_PIXEL_5),
    prefers_theme="light",
    uses_keyboard_shortcuts=True,
    uses_palette=True,
    starting_pages=("dashboard", "containers", "images", "volumes", "networks", "compose", "system"),
    done_rubric=(
        "Every interactive element is tabbable in visual order.",
        "Focus ring visible on every focusable element.",
        "No content lost at 375x667 viewport width.",
        "axe-core reports zero WCAG 2.1 AA violations on every sampled page.",
        "Every empty state explains what's missing (not just 'no …').",
        "Every list has a search bar (Nielsen #2 match real world).",
    ),
    driving_heuristics=(
        "Tabs through the entire page before clicking anything.",
        "Resizes the browser mid-flow to catch reflow bugs.",
    ),
)


SUPER_USER = Persona(
    tag="super_user",
    name="Super user (API + UI)",
    rate_limit_profile="ci",
    description="Scripts against OpenAPI; uses curl alongside the UI.",
    click_delay_ms=150,
    preferred_viewport=_DESKTOP_24,
    supports_viewports=(_DESKTOP_24, _DESKTOP_14),
    prefers_theme="dark",
    uses_keyboard_shortcuts=True,
    uses_palette=True,
    starting_pages=("system",),
    done_rubric=(
        "Every UI action has an equivalent documented API call.",
        "Error envelopes stable across versions (codes unchanged).",
        "GET /api/openapi.json describes every route with a description.",
        "No UI feature requires a UI-only workaround.",
    ),
    driving_heuristics=(
        "Checks /api/openapi.json diffs between releases.",
        "Pastes curl commands into a shell alongside the UI.",
    ),
)


HOBBYIST = Persona(
    tag="hobbyist",
    name="Home-lab hobbyist",
    rate_limit_profile="homelab",
    description="Runs pihole / homer / linkding on a Raspberry Pi.",
    click_delay_ms=700,
    preferred_viewport=_DESKTOP_14,
    supports_viewports=(_DESKTOP_14, _TABLET_10, _MOBILE_IPHONE_13),
    prefers_theme="system",
    uses_keyboard_shortcuts=False,
    uses_palette=False,
    starting_pages=("dashboard", "compose", "containers"),
    done_rubric=(
        "Can import a stack YAML from a home-lab blog with zero edits.",
        "Can expose a service on a sensible port from the UI.",
        "Works on ARM / Raspberry Pi (image choice is a knob).",
    ),
    driving_heuristics=(
        "Copies docker-compose.yml snippets from the internet.",
        "Reuses port assignments across deploys.",
    ),
)


# Registry — importable as `from tests.personas import ALL_PERSONAS`.
ALL_PERSONAS: tuple[Persona, ...] = (
    NOVICE,
    DEVELOPER,
    SRE_OPS,
    SECURITY_REVIEWER,
    UI_UX_AUDITOR,
    SUPER_USER,
    HOBBYIST,
)

PERSONAS_BY_TAG: dict[str, Persona] = {p.tag: p for p in ALL_PERSONAS}


def get_persona(tag: str) -> Persona:
    """Look up a persona by tag. Raises KeyError with the valid tag set
    so typos become easy to spot at test-collection time."""
    try:
        return PERSONAS_BY_TAG[tag]
    except KeyError as exc:
        valid = sorted(PERSONAS_BY_TAG.keys())
        raise KeyError(f"Unknown persona tag {tag!r}; valid: {valid}") from exc
