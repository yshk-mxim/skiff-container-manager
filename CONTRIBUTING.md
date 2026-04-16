# Contributing to SKIFF Container Manager

Thank you for your interest in contributing! This document covers how to get started and what to expect.

---

## Getting Started

### Prerequisites

- Python 3.12+
- Docker CLI (for `docker compose` support)
- SSH key configured for a Docker Engine VM

### Setup

```bash
git clone https://github.com/yshk-mxim/skiff-container-manager.git
cd skiff
python -m venv .venv
source .venv/bin/activate

# Unit tests only (CI, most contributors)
pip install -e .[dev]
make test-unit

# E2e tests (needs Docker daemon accessible + browser)
pip install -e .[dev,e2e]
playwright install chromium
make test-e2e
```

### Run the server locally

```bash
# No auth, local Docker socket or SSH:
API_TOKEN="" DOCKER_HOST="ssh://dev@docker-vm" uvicorn skiff.app:app --reload --host 127.0.0.1 --port 8080
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
- `refactor:` Code restructure without behavior change
- `chore:` Build or tooling changes

### Pull requests

Before opening a PR:

1. Run `make lint` — must be clean.
2. Test the golden path manually if changing behavior.
3. Update `CHANGELOG.md` under `[Unreleased]`.
4. Update docs if adding or changing endpoints or config.

---

## Code Standards

- Follow the patterns established in `docs/code-quality-guide.md` (adapted from the project's quality standards).
- No magic numbers — use named constants.
- `raise X from exc` in all `except` blocks.
- No bare `except Exception: pass` — log or re-raise.
- No host path mounts in any compose or volume validation path.

---

## License

By contributing, you agree your contributions will be licensed under the MIT License.
