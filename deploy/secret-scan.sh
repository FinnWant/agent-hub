#!/usr/bin/env bash
# Check that no configured secret appears in Kiln's database or service logs.
#   sudo bash deploy/secret-scan.sh
# The secret values are read from .env into a private temp file and passed to grep with -f,
# so they never appear on a command line. Prints counts only, never the values.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
ENV_FILE=/opt/agent-hub/.env
DATA=/opt/agent-hub/data

PATTERNS=$(mktemp); chmod 600 "$PATTERNS"; trap 'rm -f "$PATTERNS"' EXIT
for key in CLAUDE_CODE_OAUTH_TOKEN GITHUB_TOKEN SESSION_SECRET ADMIN_PASSWORD_HASH; do
  v=$(grep -E "^$key=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)
  [[ ${#v} -ge 8 ]] && printf '%s\n' "$v" >> "$PATTERNS" && echo "checking $key (${#v} chars)"
done
[[ -s $PATTERNS ]] || { echo "no secrets configured"; exit 0; }

found=0
for f in "$DATA"/*.db "$DATA"/*.db-wal; do
  [[ -f $f ]] || continue
  n=$(grep -a -c -F -f "$PATTERNS" "$f" || true)
  echo "  $(basename "$f"): $n matching lines"; found=$((found + n))
done
n=$(journalctl -u agent-hub --no-pager -o cat 2>/dev/null | grep -c -F -f "$PATTERNS" || true)
echo "  journal (agent-hub): $n matching lines"; found=$((found + n))

if [[ $found -eq 0 ]]; then echo "PASS  no secret found"; else echo "FAIL  secrets found in $found places"; exit 1; fi
