# Contributing to SKIFF Container Manager

Thank you for your interest in contributing! This document covers how to get started and what to expect.

---

## Your first PR

SKIFF uses the standard GitHub fork-and-PR flow. Contributors don't push
branches to this repo directly — you push to your own fork and open a
pull request against `yshk-mxim/skiff-container-manager:main`.

1. Click the **Fork** button on the top right of the repo page.
2. Clone *your fork* locally (not the upstream):
   ```bash
   git clone https://github.com/<your-user>/skiff-container-manager.git skiff
   cd skiff
   git remote add upstream https://github.com/yshk-mxim/skiff-container-manager.git
   ```
3. Create a branch on your fork, make changes, run `make ci` locally, commit.
4. Push to your fork, then open a PR from the GitHub UI.

One required-status check (`security / Security Review`) is skipped on
forks because it depends on a repo secret forks cannot read. This is
intentional — every other gate runs unchanged.

---

## Getting Started

### Prerequisites

- Python 3.12+
- Docker CLI (for `docker compose` support)
- Any Docker-API-compatible runtime reachable locally (Docker Desktop,
  Colima, OrbStack, Rancher Desktop, Linux Docker Engine, Podman
  rootless) OR an SSH tunnel to a remote Docker host. Unit tests need
  neither — only the e2e suite does.

### Setup

```bash
# Fork first, then clone your fork (see "Your first PR" below).
git clone https://github.com/<your-user>/skiff-container-manager.git skiff
cd skiff
git remote add upstream https://github.com/yshk-mxim/skiff-container-manager.git
python -m venv .venv
source .venv/bin/activate

# Unit tests only (CI, most contributors)
pip install -e ".[dev]"
pre-commit install   # gitleaks / ruff / AP-lint / check-yaml hooks
make test-unit

# E2e tests (needs Docker daemon accessible + browser)
pip install -e ".[dev,e2e]"
playwright install chromium
make test-e2e
```

The `[dev]` and `[dev,e2e]` extras are quoted because zsh (macOS default)
treats `[...]` as a glob pattern; the quotes make the literal extras
syntax survive to pip. On bash-only hosts the quotes are harmless.

### Optional: run the full suite against a live Docker host

Unit and integration tests default to a MagicMock Docker client so the
suite runs without a daemon. Two env vars opt a contributor's live
daemon into the same tests:

| env var                  | required when                   | example                                   |
|--------------------------|---------------------------------|-------------------------------------------|
| `SKIFF_TEST_TARGET`      | you want live Docker at all     | `mock` (default) / `local` / `remote` / `gcp` |
| `SKIFF_TEST_DOCKER_HOST` | `SKIFF_TEST_TARGET` ≠ `mock`    | `unix:///var/run/docker.sock`, `unix:///tmp/my-tunnel.sock`, `tcp://host:2375` |

`local` expects a workstation daemon. `remote` expects a daemon reached
via an SSH ControlMaster tunnel the caller brought. `gcp` is reserved.

Example: `SKIFF_TEST_TARGET=local SKIFF_TEST_DOCKER_HOST=unix:///var/run/docker.sock make test`.

### Run the server locally

```bash
# Local socket (no auth, localhost only — dev mode)
API_TOKEN="" uvicorn skiff.app:app --reload --host 127.0.0.1 --port 8080 --no-proxy-headers

# Or against a remote Docker host via an SSH tunnel
ssh -fNL /tmp/docker.sock:/var/run/docker.sock user@docker-host
API_TOKEN="$(openssl rand -hex 32)" DOCKER_HOST=unix:///tmp/docker.sock \
  uvicorn skiff.app:app --reload --host 127.0.0.1 --port 8080 --no-proxy-headers
```

---

## Development Workflow

### Branch naming

- `feature/your-feature` — new functionality
- `fix/issue-description` — bug fixes
- `docs/topic` — documentation only

### Code quality

```bash
make lint        # ruff check
make format      # ruff format + auto-fix
make security    # ruff --select S security rules
make test-unit   # fast unit tests, no Docker required
```

### Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation only
- `refactor:` Code restructure without behaviour change
- `chore:` Build or tooling changes

### Pull requests

Before opening a PR:

1. Run `make ci` locally — must be clean. It runs the same gates as
   the CI workflow: `lint` (ruff), `lint-antipatterns` (AP001–AP014),
   `lint-js` (no innerHTML interpolation), `lint-md` (no broken
   internal links), `lint-asvs` (SECURITY.md V1–V18 coverage),
   `lint-notice` (NOTICE vs `requirements.txt`), `security` (ruff S
   + pip-audit --strict), `docs-check` (auto-generated docs drift)
   and `coverage` (unit + property tests against the 94% hard floor;
   project target is ≥95 per `docs/dev/feature-development.md §7d`).
2. Test the golden path manually if changing behaviour.
3. Update `CHANGELOG.md` under `[Unreleased]`.
4. Update docs if adding or changing endpoints or config.

### What to expect on review

This project is maintained by one person in spare time. Expect review
and response on a best-effort cadence — the maintainer does not commit
to a specific turnaround.

Changes under `.github/`, `pyproject.toml`, or `requirements.txt` require
maintainer review (see `.github/CODEOWNERS`) — expect a longer wait for
dependency bumps and CI tweaks than for application-code PRs.

PRs opened from **forks** will see one required-status check
(`security / Security Review`) skip because it depends on a repo secret
(`CLAUDE_API_KEY`) that forks cannot read. The other gates
(lint, anti-pattern, markdown, pip-audit, tests, SBOM/Grype) run
unchanged.

---

## Code Standards

- Follow the patterns established in `docs/dev/code-quality-guide.md` (adapted from the project's quality standards).
- No magic numbers — use named constants.
- `raise X from exc` in all `except` blocks.
- No bare `except Exception: pass` — log or re-raise.
- No host path mounts in any compose or volume validation path.

---

## License

By contributing, you agree your contributions will be licensed under the MIT License.
