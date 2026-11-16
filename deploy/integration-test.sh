#!/usr/bin/env bash
# Run the Docker integration tests against agenthub's rootless daemon, as agenthub.
#   bash deploy/integration-test.sh
# install.sh puts pytest and httpx in the app venv.
set -euo pipefail
AUID=$(id -u agenthub)
cd /opt/agent-hub/backend
exec sudo -u agenthub env \
  XDG_RUNTIME_DIR="/run/user/$AUID" \
  DOCKER_HOST="unix:///run/user/$AUID/docker.sock" \
  PYTHONDONTWRITEBYTECODE=1 /opt/agent-hub/.venv/bin/python -m pytest -m docker -p no:cacheprovider "$@"
