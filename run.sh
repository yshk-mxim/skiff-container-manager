#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Yakov Shkolnikov and contributors
# SKIFF Container Manager — startup script
# Connects to a Docker Engine via a local Unix socket or SSH tunnel.
#
# Prerequisites:
#   - Python 3.11+ with pip
#   - docker CLI installed (for compose commands)
#   - For remote Docker: open an SSH tunnel first:
#       ssh -fNL /tmp/docker.sock:/var/run/docker.sock user@docker-host
#       export DOCKER_HOST=unix:///tmp/docker.sock
#
# Configuration — set via environment variables or a .env file in this directory:
#   API_TOKEN          — Bearer token for API auth (MUST be set in production)
#   DOCKER_HOST        — Docker socket (default: unix:///var/run/docker.sock)
#   ALLOWED_REGISTRIES — Comma-separated registry prefixes (default: us-docker.pkg.dev/)
#   ALLOWED_ORIGINS    — Comma-separated CORS origins (default: http://127.0.0.1:8080)
#   BIND_HOST          — Listen address (default: 127.0.0.1)
#   DOCKER_VM_HOST     — Hostname/IP shown for container port links in the UI
#   COMPOSE_DIR        — Directory for compose files (default: /data/compose)
#   PORT               — Listen port (default: 8080)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env file if present (simple KEY=VALUE, no export required)
if [ -f ".env" ]; then
  echo "Loading .env file..."
  set -o allexport
  # shellcheck disable=SC1091
  source .env
  set +o allexport
fi

# Enforce API_TOKEN in production
if [ -z "${API_TOKEN:-}" ]; then
  echo "WARNING: API_TOKEN not set — running without authentication."
  echo "Set API_TOKEN in your environment or in a .env file for production use."
fi

# Verify Python version
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
  echo "ERROR: Python 3.11+ required (found: $(python3 --version 2>&1))"
  echo "Install: https://www.python.org/downloads/"
  exit 1
fi

# Create and activate a virtual environment if not already inside one
if [ -z "${VIRTUAL_ENV:-}" ]; then
  if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv/ ..."
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Install / sync dependencies (always run so version mismatches are caught)
pip install --quiet -r requirements.txt

# Verify docker CLI is available (needed for compose commands)
if ! command -v docker &>/dev/null; then
  echo "ERROR: docker CLI not found. Install docker CLI for compose support."
  echo "  macOS:  brew install docker"
  echo "  Debian: sudo apt-get install -y docker-ce-cli"
  exit 1
fi

DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"
PORT="${PORT:-8080}"
echo "Docker host : $DOCKER_HOST"
echo "Listening on: ${BIND_HOST:-127.0.0.1}:$PORT"
echo "Open http://127.0.0.1:$PORT in your browser"

exec uvicorn app:app \
  --host "${BIND_HOST:-127.0.0.1}" \
  --port "$PORT" \
  --workers 1 \
  --log-level warning
