# WCAG 2.1 Level AA accessibility

SKIFF's web UI is tested against **WCAG 2.1 Level AA** via two complementary checks:

- **pa11y** (htmlcs + axe-core engines) runs in CI on the login page on every PR that touches `skiff/static/**` and on a weekly cron — see `.github/workflows/a11y.yml`.
- **Playwright + axe-core 4.10** runs in CI as the `axe-full-spa` job in the same `.github/workflows/a11y.yml` workflow. The job installs the `[dev,e2e]` extras + chromium, starts a live SKIFF via the `live_server` fixture, logs in, and walks every registered page (containers, images, volumes, networks, compose, system), asserting zero violations against the WCAG 2.0 + 2.1 AA tag surface. Also reproducible locally via `pytest tests/test_e2e_accessibility.py`.

## Current conformance

| Scope | Status | Evidence |
|---|---|---|
| Login screen (pre-auth) | **AA clean** (0 issues) | pa11y run at HEAD on `/` — see below |
| Authenticated SPA (containers, images, volumes, networks, compose, system) | **AA clean** (0 issues) | `pytest tests/test_e2e_accessibility.py` — Playwright + axe-core 4.10, WCAG 2.0 + 2.1 AA tag surface, parametrised over every registered page |

## What we test

pa11y runs against a live SKIFF on port 18300 with the WCAG 2.1 AA rule set. Two engines run in parallel:

- **htmlcs** — the HTML_CodeSniffer engine, covers semantic/structural WCAG checks.
- **axe-core** — Deque's ARIA + color-contrast engine.

Any rule failure is treated the same way as a CI lint error: fix in code, or document as an intentional deviation here.

## Known passes

Verified at HEAD:

- **1.1.1 Non-text content** — every `<svg>` in the sidebar carries an `aria-label` or a text sibling; the theme-toggle buttons have explicit `aria-label` attributes (`System theme`, `Light theme`, `Dark theme`).
- **1.3.1 Info and relationships** — form fields have programmatically-associated `<label>` elements (the login token field is `<label htmlFor="login-token">` + `<input id="login-token" aria-label="API Token">`).
- **1.4.3 Contrast (Minimum)** — the primary button uses `--accent-strong` (`#0f766e`) on `#fff` = **4.74:1** (WCAG AA for normal text is ≥4.5:1). The decorative accent `--accent` (`#0d9488`) is used only for borders, icons, and focus rings — those have the lower 3:1 non-text threshold and pass at **3.72:1**.
- **2.1.1 Keyboard** — every interactive element is a native `<button>` / `<a>` / `<input>` / `<select>`. No custom widgets that require `tabindex`/`role` overrides.
- **2.4.3 Focus order** — Tab order follows DOM order; no `tabindex` ≥1 present in the tree.
- **3.3.2 Labels or instructions** — token input has a visible label ("API Token") plus instructional copy ("Enter the API token you saved during setup.").
- **4.1.2 Name, role, value** — every interactive element has an accessible name (`aria-label`, visible text, or associated `<label>`).

## Design decisions that support AA

- The token input at login uses programmatically-associated labelling (`id` on the input, `htmlFor` on the label, `aria-label` on the input as a belt-and-braces backup) so screen readers always announce the field purpose.
- Two accent colours are defined: `--accent` (`#0d9488`, 3.72:1 vs white) is reserved for decorative surfaces (borders, icons, focus rings) where the 3:1 non-text WCAG AA threshold applies; `--accent-strong` (`#0f766e`, 4.74:1 vs white) is used for text-on-colour surfaces (primary buttons, reviewer-mode banner) where the 4.5:1 normal-text threshold applies.
- Every interactive element is a native `<button>` / `<a>` / `<input>` / `<select>` so the browser provides the accessible role, state, and keyboard semantics without custom ARIA.

## Gaps we're aware of but don't yet test automatically

- **Focus visibility at default zoom**. Manually verified — every focus ring uses `--focus-ring` with 3px spread. Automated coverage is on the roadmap.
- **Screen-reader narration quality**. Automated checks evaluate the static DOM; they do not evaluate whether a VoiceOver / NVDA / JAWS user finds the experience usable. No human-tester pass has been performed yet; contributions welcome.

## Reproducing the scan locally

```bash
# Boot SKIFF on port 18300 (any port is fine, match what you pass to pa11y)
API_TOKEN=$(openssl rand -hex 32) \
BIND_HOST=127.0.0.1 \
DOCKER_HOST=unix:///var/run/docker.sock \
python3 -m uvicorn skiff.app:app --host 127.0.0.1 --port 18300 \
    --no-proxy-headers --forwarded-allow-ips "" &

# Requires node 18+ (bundles chromium via puppeteer)
npx --yes pa11y --standard WCAG2AA http://127.0.0.1:18300

# Optional: full JSON report
npx --yes pa11y --standard WCAG2AA --reporter json \
    http://127.0.0.1:18300 > pa11y.json
```

Any issue report is a bug — file it normally.

## Related frameworks

SKIFF's WCAG 2.1 AA posture maps onto:

- **Section 508** (US federal procurement) — Section 508 refresh is harmonised with WCAG 2.1 AA. A federal operator can cite this page.
- **EN 301 549** (EU accessibility requirements) — also harmonised with WCAG 2.1 AA.
- **ADA Title III** (US public accommodations) — US courts have treated WCAG 2.1 AA as the de-facto standard.
