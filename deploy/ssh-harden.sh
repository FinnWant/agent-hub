#!/usr/bin/env bash
# Turn off SSH password logins (and X11 forwarding, and root logins) once Tailscale SSH works.
#   sudo bash deploy/ssh-harden.sh          apply
#   sudo bash deploy/ssh-harden.sh undo     put things back as they were
#
# Tailscale SSH answers on this server's Tailscale address and checks who you are with your
# Tailscale login, so it needs no password or key. OpenSSH keeps running for the LAN, but
# after this it only accepts SSH keys there. The console (keyboard and screen) always works.
set -euo pipefail
CONF=/etc/ssh/sshd_config.d/10-kiln-hardening.conf
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0"; exit 1; }

if [[ ${1:-} == undo ]]; then
  rm -f "$CONF"
  sshd -t && systemctl reload ssh
  echo "Removed $CONF; SSH accepts passwords again."
  exit 0
fi

if ! tailscale debug prefs 2>/dev/null | grep -q '"RunSSH": true'; then
  echo "Tailscale SSH is off on this server. Turn it on first:  sudo tailscale set --ssh"
  echo "then log in from your laptop with Tailscale SSH (see the README) and run this again."
  exit 1
fi

cat <<'EOF'
This turns off SSH password logins. Before going on, make sure that from your laptop
  ssh <your-user>@<server-name>
logged you in WITHOUT asking for a password (that's Tailscale SSH working).
EOF
read -r -p "Did that work? [y/N] " answer
[[ $answer == [yY]* ]] || { echo "Nothing changed."; exit 1; }

cat > "$CONF" <<'EOF'
# Written by Kiln's deploy/ssh-harden.sh. Remove this file (or run: ssh-harden.sh undo) to revert.
# Files in sshd_config.d are read in name order and the first value wins, so this beats 50-cloud-init.conf.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
EOF
chmod 644 "$CONF"
if ! sshd -t; then
  rm -f "$CONF"
  echo "sshd rejected the settings; nothing changed."
  exit 1
fi
systemctl reload ssh
echo "Done. Effective settings:"
sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|x11forwarding) '
echo "Undo any time with: sudo bash deploy/ssh-harden.sh undo"
