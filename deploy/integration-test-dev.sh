#!/usr/bin/env bash
# Run the Docker integration tests against this working copy (not the installed app), as
# agenthub, with the installed app's venv and settings (.env, e.g. the Claude token).
# The running Kiln service is not touched.
#   bash deploy/integration-test-dev.sh [pytest args, e.g. -k restart]
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
AUID=$(id -u agenthub)
TMP=$(mktemp -d /tmp/kiln-it.XXXXXX)
trap 'sudo rm -rf "$TMP"' EXIT

rsync -a --exclude .venv --exclude __pycache__ --exclude data --exclude .pytest_cache \
  "$REPO/backend/" "$TMP/backend/"
chmod -R a+rX "$TMP"
ln -s /opt/agent-hub/.env "$TMP/.env"

cd "$TMP/backend"
sudo -u agenthub env \
  XDG_RUNTIME_DIR="/run/user/$AUID" \
  DOCKER_HOST="unix:///run/user/$AUID/docker.sock" \
  PYTHONDONTWRITEBYTECODE=1 /opt/agent-hub/.venv/bin/python -m pytest -m docker -p no:cacheprovider "$@"
