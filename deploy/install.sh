#!/usr/bin/env bash
# Install or update Agent Hub on an Ubuntu server.
# Usage: sudo bash deploy/install.sh
#
# Prerequisites (see README): a system user `agenthub` with rootless Docker set up and
# lingering enabled. Safe to re-run: updates the app, images and units without touching
# existing settings in /opt/agent-hub/.env.
set -euo pipefail

APP_DIR=/opt/agent-hub
SERVICE_USER=agenthub
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

die() { echo "Error: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo"
id "$SERVICE_USER" &>/dev/null || die "user '$SERVICE_USER' does not exist"
AGENT_UID=$(id -u "$SERVICE_USER")
SOCK=/run/user/$AGENT_UID/docker.sock
[[ -S $SOCK ]] || die "rootless Docker isn't running for $SERVICE_USER ($SOCK missing)"
loginctl show-user "$SERVICE_USER" -p Linger 2>/dev/null | grep -q yes \
  || echo "Warning: lingering is off for $SERVICE_USER; run: sudo loginctl enable-linger $SERVICE_USER"

as_agent() {
  sudo -u "$SERVICE_USER" -H env XDG_RUNTIME_DIR="/run/user/$AGENT_UID" DOCKER_HOST="unix://$SOCK" "$@"
}

echo "==> Installing system packages"
apt-get install -y -qq python3-venv rsync iptables >/dev/null

echo "==> Copying app to $APP_DIR"
install -d -m 755 "$APP_DIR"
install -d -m 700 "$APP_DIR/data"
rsync -a --delete \
  --exclude .git --exclude .venv --exclude data --exclude .env --exclude AGENT_HUB_HANDOFF.md \
  --exclude node_modules --exclude dist --exclude __pycache__ --exclude .pytest_cache \
  "$SRC_DIR/" "$APP_DIR/"

ENV_FILE=$APP_DIR/.env
[[ -f $ENV_FILE ]] || install -m 600 "$APP_DIR/deploy/env.example" "$ENV_FILE"
env_get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
env_set() {
  if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
# Add KEY if missing, or fill it if empty. Never overwrites a value you set.
env_default() { [[ -n $(env_get "$1") ]] || env_set "$1" "$2"; }

env_set DB_PATH "$APP_DIR/data/agent-hub.db"
# The image version follows the Dockerfiles (images/VERSION), not a user setting.
env_set IMAGE_VERSION "$(cat "$APP_DIR/images/VERSION")"
env_set DOCKER_HOST "unix://$SOCK"
env_default SESSION_SECRET "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
grep -qE '^ADMIN_PASSWORD_HASH=' "$ENV_FILE" || echo "ADMIN_PASSWORD_HASH=" >> "$ENV_FILE"
if [[ $(env_get BIND_HOSTS) == *"<tailscale-ip>"* || -z $(env_get BIND_HOSTS) ]]; then
  TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
  [[ -n $TS_IP ]] || die "couldn't read the Tailscale IP; set BIND_HOSTS in $ENV_FILE"
  env_set BIND_HOSTS "127.0.0.1,$TS_IP"
fi
# Previews: published on the Tailscale IP only, linked by the MagicDNS name (a different
# hostname from the one you open Agent Hub on, so previews never see its session cookie).
if [[ -z $(env_get PREVIEW_BIND) || $(env_get PREVIEW_BIND) == *"<"* ]]; then
  env_set PREVIEW_BIND "$(env_get BIND_HOSTS | tr ',' '\n' | grep -v '^127\.' | head -1)"
fi
if [[ -z $(env_get PREVIEW_HOST) || $(env_get PREVIEW_HOST) == *"<"* ]]; then
  TS_NAME=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
  if [[ -n $TS_NAME ]]; then env_set PREVIEW_HOST "$TS_NAME"
  else echo "Warning: no MagicDNS name found; previews stay off until PREVIEW_HOST is set in $ENV_FILE"; fi
fi
# Disk I/O throttling applies to the block device holding Docker's data (on /).
if [[ -z $(env_get SANDBOX_IO_DEVICE) ]] \
   && grep -qw io "/sys/fs/cgroup/user.slice/user-$AGENT_UID.slice/user@$AGENT_UID.service/cgroup.controllers" 2>/dev/null; then
  env_set SANDBOX_IO_DEVICE "$(readlink -f "$(findmnt -no SOURCE /)")"
fi

echo "==> Creating Python environment"
[[ -x $APP_DIR/.venv/bin/python ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 600 "$ENV_FILE"

echo "==> Building the web UI"
command -v npm >/dev/null || die "Node.js (20.19+ or 22.12+) with npm is needed to build the web UI"
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=12)||(a===20&&b>=19)||a===21?0:1)' \
  || die "Node.js $(node --version) is too old for the web UI build (need 20.19+ or 22.12+)"
# As agenthub, with dependency install scripts disabled; versions are pinned by package-lock.json.
as_agent bash -c "cd '$APP_DIR/frontend' && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error >/dev/null && npm run -s build >/dev/null"

echo "==> Applying the sandbox egress filter"
# Runs as root, so it lives in a root-owned path agenthub can't modify.
install -m 755 -o root -g root "$APP_DIR/deploy/egress.sh" /usr/local/sbin/agent-hub-egress
sed "s/AGENT_UID/$AGENT_UID/g" "$APP_DIR/deploy/agent-hub-egress.service" > /etc/systemd/system/agent-hub-egress.service
systemctl daemon-reload
systemctl enable -q agent-hub-egress.service
systemctl restart agent-hub-egress.service

echo "==> Building sandbox images (first run takes a few minutes)"
VERSION=$(env_get IMAGE_VERSION); VERSION=${VERSION:-1}
for dir in "$APP_DIR"/images/*/; do  # python, node, static
  name=$(basename "$dir")
  echo "    agent-hub/$name:$VERSION"
  as_agent docker build -q -t "agent-hub/$name:$VERSION" "$dir" >/dev/null
done

echo "==> Installing the agent-hub service"
sed "s/AGENT_UID/$AGENT_UID/g" "$APP_DIR/deploy/agent-hub.service" > /etc/systemd/system/agent-hub.service
systemctl daemon-reload
systemctl enable -q agent-hub.service
systemctl restart agent-hub.service

PORT=$(env_get PORT); PORT=${PORT:-8090}
if ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "==> Allowing port $PORT on tailscale0 (ufw)"
  ufw allow in on tailscale0 to any port "$PORT" proto tcp comment 'agent-hub' >/dev/null
  PREVIEW_RANGE=$(env_get PREVIEW_PORT_RANGE); PREVIEW_RANGE=${PREVIEW_RANGE:-8200-8299}
  echo "==> Allowing preview ports $PREVIEW_RANGE on tailscale0 (ufw)"
  ufw allow in on tailscale0 to any port "${PREVIEW_RANGE/-/:}" proto tcp comment 'agent-hub previews' >/dev/null
fi

sleep 2
systemctl is-active -q agent-hub.service || { journalctl -u agent-hub -n 30 --no-pager; die "agent-hub failed to start"; }

cat <<EOF

Agent Hub is installed and listening on $(env_get BIND_HOSTS) port $PORT.

Open http://$(env_get BIND_HOSTS | tr ',' '\n' | grep -v '^127\.' | head -1):$PORT in a browser on your tailnet.
If you haven't yet:
  - set the admin password (until then every request gets 503):
       cd $APP_DIR/backend && sudo -u $SERVICE_USER ../.venv/bin/python -m app.set_password
       sudo systemctl restart agent-hub
  - in the Tailscale admin console, add "tcp:$PORT" and "tcp:8200-8299" (previews) to the
    ACL grant for this server.
  - for agent runs, store the Claude token (from `claude setup-token`) and, to push
    branches, a fine-grained GitHub token:
       cd $APP_DIR/backend && sudo -u $SERVICE_USER ../.venv/bin/python -m app.set_secret claude
       cd $APP_DIR/backend && sudo -u $SERVICE_USER ../.venv/bin/python -m app.set_secret github
       sudo systemctl restart agent-hub
Checks: bash deploy/smoke-test.sh (API) and bash deploy/integration-test.sh (Docker).
EOF
