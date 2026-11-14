#!/usr/bin/env bash
# Sandbox egress filter for rootless Docker.
#
# With rootless Docker, container traffic leaves the host as ordinary traffic from the
# daemon's user (agenthub) — it never crosses a host bridge, so Docker's DOCKER-USER chain
# doesn't apply. Instead, every NEW outbound connection from that user is checked here:
#   - allowed: replies to connections others opened to us (e.g. the API itself), and DNS to
#     the local systemd-resolved stub
#   - rejected: any address this host owns (LAN, Tailscale, loopback, public IPv6, ...),
#     private and carrier-grade NAT ranges (home LAN, Tailscale), link-local (cloud
#     metadata), multicast and reserved ranges
#   - everything else (the public internet) is allowed
#
# Usage: egress.sh apply | remove | status        (needs root; run by agent-hub-egress.service)
set -euo pipefail

USER_NAME=${AGENT_HUB_USER:-agenthub}
CHAIN=AGENT-HUB-EGRESS
REJECT_CHAIN=AGENT-HUB-REJECT

V4_BLOCK=(0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12
          192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4)
V6_BLOCK=(::1/128 ::ffff:0:0/96 64:ff9b::/96 fc00::/7 fe80::/10 ff00::/8)
DNS_STUBS=(127.0.0.53 127.0.0.54)   # systemd-resolved; it does the upstream query as its own user

id "$USER_NAME" >/dev/null

remove_for() {
  local ipt=$1
  while $ipt -w -C OUTPUT -m owner --uid-owner "$USER_NAME" -j "$CHAIN" 2>/dev/null; do
    $ipt -w -D OUTPUT -m owner --uid-owner "$USER_NAME" -j "$CHAIN"
  done
  for c in "$CHAIN" "$REJECT_CHAIN"; do
    if $ipt -w -L "$c" -n >/dev/null 2>&1; then $ipt -w -F "$c"; $ipt -w -X "$c"; fi
  done
}

apply_for() {
  local ipt=$1 reject=$2; shift 2
  local blocks=("$@")
  remove_for "$ipt"
  $ipt -w -N "$REJECT_CHAIN"
  $ipt -w -A "$REJECT_CHAIN" -m limit --limit 6/min -j LOG --log-prefix "agent-hub-egress: "
  $ipt -w -A "$REJECT_CHAIN" -p tcp -j REJECT --reject-with tcp-reset
  $ipt -w -A "$REJECT_CHAIN" -j REJECT --reject-with "$reject"

  $ipt -w -N "$CHAIN"
  $ipt -w -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  if [[ $ipt == iptables ]]; then
    for stub in "${DNS_STUBS[@]}"; do
      for proto in udp tcp; do
        $ipt -w -A "$CHAIN" -o lo -d "$stub" -p "$proto" --dport 53 -j RETURN
      done
    done
  fi
  $ipt -w -A "$CHAIN" -m addrtype --dst-type LOCAL -j "$REJECT_CHAIN"
  for net in "${blocks[@]}"; do
    $ipt -w -A "$CHAIN" -d "$net" -j "$REJECT_CHAIN"
  done
  $ipt -w -A "$CHAIN" -j RETURN
  $ipt -w -I OUTPUT 1 -m owner --uid-owner "$USER_NAME" -j "$CHAIN"
}

case ${1:-} in
  apply)
    apply_for iptables icmp-admin-prohibited "${V4_BLOCK[@]}"
    apply_for ip6tables icmp6-adm-prohibited "${V6_BLOCK[@]}"
    echo "Egress filter applied for user $USER_NAME"
    ;;
  remove)
    remove_for iptables
    remove_for ip6tables
    echo "Egress filter removed"
    ;;
  status)
    for ipt in iptables ip6tables; do
      echo "== $ipt"
      $ipt -w -S OUTPUT | grep -- "-j $CHAIN" || echo "(no OUTPUT jump — filter NOT active)"
      $ipt -w -S "$CHAIN" 2>/dev/null || true
    done
    ;;
  *)
    echo "Usage: $0 apply|remove|status" >&2
    exit 2
    ;;
esac
