#!/usr/bin/env bash
# Read-only security snapshot of this server. Changes nothing.
#   sudo bash deploy/security-audit.sh
# Paste the output back to review it. It prints no secrets, but it does show your
# LAN/Tailscale addresses and user names, so share it only where you'd share those.
set -u
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0"; exit 1; }
h() { printf '\n== %s\n' "$1"; }

h "Firewall (ufw)"
ufw status verbose 2>&1 | sed -n '1,60p'

h "Open ports and the programs behind them (not loopback)"
ss -Hltnup 2>/dev/null | awk '$5 !~ /^(127\.|\[::1\]|\[?::ffff:127)/ {
  n=$7; sub(/.*\(\("/,"",n); sub(/".*/,"",n); print $1, $5, n }' | sort -u

h "SSH (effective settings)"
sshd -T 2>/dev/null | grep -Ei '^(port|listenaddress|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|x11forwarding|maxauthtries|allowusers|allowgroups) ' | sort
for u in $(awk -F: '$7 !~ /(nologin|false|sync)$/ {print $1}' /etc/passwd); do
  d=$(getent passwd "$u" | cut -d: -f6)
  printf '%s: %s authorized key(s)\n' "$u" "$(grep -sE '^(ssh|ecdsa|sk-)' "$d/.ssh/authorized_keys" | wc -l)"
done
printf 'Tailscale SSH: '; tailscale debug prefs 2>/dev/null | grep -q '"RunSSH": true' && echo on || echo off

h "SSH logins in the last 7 days"
journalctl -u ssh --since -7d --no-pager 2>/dev/null | grep -Eo 'Accepted [a-z-]+ for [^ ]+ from [^ ]+' | sort | uniq -c | sort -rn | head
printf 'Failed password attempts: '; journalctl -u ssh --since -7d --no-pager 2>/dev/null | grep -c 'Failed password'

h "Accounts"
printf 'sudo: %s\n' "$(getent group sudo | cut -d: -f4)"
printf 'docker group (root-equivalent): %s\n' "$(getent group docker | cut -d: -f4)"
printf 'Login shell for agenthub: %s\n' "$(getent passwd agenthub | cut -d: -f7)"
printf 'Passwordless sudo rules: '; grep -rhE 'NOPASSWD' /etc/sudoers /etc/sudoers.d 2>/dev/null | grep -v '^#' | paste -sd';' || true; echo

h "Updates"
apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep -c '^Inst' | xargs printf 'Pending upgrades: %s\n'
apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep '^Inst' | grep -ci security | xargs printf '  of which security: %s\n'
[[ -f /var/run/reboot-required ]] && echo "Reboot required: yes ($(paste -sd' ' /var/run/reboot-required.pkgs 2>/dev/null | cut -c1-120))" || echo "Reboot required: no"
printf 'Unattended upgrades: %s\n' "$(systemctl is-enabled unattended-upgrades 2>&1)"

h "Kiln"
ls -ld /opt/agent-hub /opt/agent-hub/.env /opt/agent-hub/data
stat -c '%a %U %n' /opt/agent-hub/data/* 2>/dev/null
printf 'agent-hub, egress filter: %s\n' "$(systemctl is-active agent-hub agent-hub-egress | paste -sd' ')"
grep -E '^(BIND_HOSTS|PUBLIC_URL|TRUST_PROXY|PREVIEW_BIND|HTTPS)=' /opt/agent-hub/.env
tailscale serve status 2>&1 | head -4
printf 'Tailscale Funnel (public internet): '; tailscale funnel status 2>&1 | grep -qi 'Funnel on' && echo ON || echo off

h "Kernel and AppArmor"
uname -r
sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null | xargs printf 'apparmor_restrict_unprivileged_userns=%s\n'
aa-status --enabled 2>/dev/null && echo "AppArmor: enabled" || echo "AppArmor: not enabled"
