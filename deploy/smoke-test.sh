#!/usr/bin/env bash
# Acceptance checks (phases 1-2), run on the server after install.sh and set_password.
# Talks to the API over the Tailscale address with curl, like a remote client would.
#   bash deploy/smoke-test.sh [base-url]
# Prompts for the admin password and (once) for sudo, to show `docker inspect` as agenthub.
set -uo pipefail

BASE=${1:-http://$(tailscale ip -4 | head -1):8090}
CSRF=(-H "X-Requested-With: agent-hub")
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
PASS=0; FAIL=0
ok()   { echo "  PASS  $*"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }
api()  { curl -sS -b "$JAR" -c "$JAR" "$@"; }
code() { curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" -c "$JAR" "$@"; }
sx()   { # run a shell command in the sandbox; prints output, returns its exit code
  local out; out=$(api "${CSRF[@]}" -H 'Content-Type: application/json' -X POST "$BASE/api/sandboxes/$SID/exec" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"cmd": sys.argv[1], "timeout": 30}))' "$1")")
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d.get("output", d).strip() if isinstance(d.get("output"), str) else d); sys.exit(d.get("exit_code", 99))' "$out"
}

echo "Agent Hub checks against $BASE"

echo "-- auth"
[[ $(code "$BASE/api/health") == 200 ]] && ok "health is public" || bad "health"
[[ $(code "$BASE/api/sandboxes") == 401 ]] && ok "unauthenticated request gets 401" || bad "expected 401"
read -rsp "Admin password: " PW; echo
c=$(code "${CSRF[@]}" -H 'Content-Type: application/json' -X POST "$BASE/api/auth/login" \
     -d "$(python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$PW")")
unset PW
[[ $c == 200 ]] && ok "login" || { bad "login returned $c"; exit 1; }

echo "-- create"
api "$BASE/api/system" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("        host mem available %.1f GiB, can start: %s %s" % (d["host"]["mem_available"]/2**30, d["can_start_default"], d["blockers"]))'
SB=$(api "${CSRF[@]}" -H 'Content-Type: application/json' -X POST "$BASE/api/sandboxes" \
      -d '{"name": "smoke-test", "template": "python", "memory": "1g", "cpus": 1}')
SID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("id",""))' "$SB")
STATE=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("state",""))' "$SB")
[[ $STATE == running ]] && ok "python sandbox $SID is running" || { bad "create: $SB"; exit 1; }

echo "-- exec"
out=$(sx "python3 --version") && [[ $out == Python\ 3* ]] && ok "exec: $out" || bad "exec python3 --version: $out"
out=$(sx 'echo "$(id -un) uid=$(id -u)"') && [[ $out == "agent uid=1000" ]] && ok "runs as $out" || bad "user: $out"
out=$(sx "grep CapEff /proc/self/status") && [[ $out == *0000000000000000 ]] && ok "no capabilities ($out)" || bad "caps: $out"

echo "-- terminal (WebSocket)"
TOKEN=$(awk '$6 == "ah_session" {print $7}' "$JAR")
out=$(AH_SESSION=$TOKEN /opt/agent-hub/.venv/bin/python "$(dirname "$0")/terminal-check.py" "$BASE" "$SID" 2>&1)
if grep -qE '^  (PASS|FAIL)' <<<"$out"; then
  grep -E '^  (PASS|FAIL)' <<<"$out"
  PASS=$((PASS + $(grep -c '^  PASS' <<<"$out"))); FAIL=$((FAIL + $(grep -c '^  FAIL' <<<"$out")))
else
  bad "terminal check crashed: $out"
fi
api "$BASE/api/audit?limit=10" | python3 -c 'import json,sys; a=[e["action"] for e in json.load(sys.stdin)]; sys.exit(0 if "terminal.open" in a and "terminal.close" in a else 1)' \
  && ok "terminal session is in the audit log" || bad "terminal session missing from audit log"

echo "-- limits (docker inspect as agenthub)"
AUID=$(id -u agenthub)
INSPECT=$(sudo -u agenthub env XDG_RUNTIME_DIR=/run/user/$AUID DOCKER_HOST=unix:///run/user/$AUID/docker.sock \
  docker inspect "sandbox-$SID" --format \
  'Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}} NanoCpus={{.HostConfig.NanoCpus}} PidsLimit={{.HostConfig.PidsLimit}} CapDrop={{.HostConfig.CapDrop}} SecurityOpt={{.HostConfig.SecurityOpt}} Privileged={{.HostConfig.Privileged}} User={{.Config.User}} OomScoreAdj={{.HostConfig.OomScoreAdj}} Mounts={{range .Mounts}}{{.Type}}:{{.Name}}->{{.Destination}} {{end}}ReadBps={{.HostConfig.BlkioDeviceReadBps}}')
echo "        $INSPECT"
[[ $INSPECT == *"Memory=1073741824 MemorySwap=1073741824"* ]] && ok "memory 1 GiB, no swap" || bad "memory limits"
[[ $INSPECT == *"NanoCpus=1000000000"* && $INSPECT == *"PidsLimit=512"* ]] && ok "cpu and pid limits" || bad "cpu/pids"
[[ $INSPECT == *"CapDrop=[ALL]"* && $INSPECT == *"no-new-privileges"* && $INSPECT == *"Privileged=false"* ]] \
  && ok "caps dropped, no-new-privileges, unprivileged" || bad "security options"
[[ $INSPECT == *"Mounts=volume:ah-$SID->/workspace ReadBps"* ]] && ok "only its own volume is mounted" || bad "mounts"

echo "-- egress"
out=$(sx "getent hosts pypi.org | head -1") && ok "DNS resolves ($out)" || bad "DNS: $out"
out=$(sx "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://pypi.org/simple/") && [[ $out == 200 ]] \
  && ok "internet reachable (pypi.org -> $out)" || bad "pypi.org: $out"
# Every address this host owns (LAN, Tailscale, ...), on ports that are really listening here.
TARGETS=()
for ip in $(hostname -I); do
  [[ $ip == *:* ]] && continue
  for port in 22 8080 8090; do TARGETS+=("$ip:$port"); done
done
TARGETS+=("10.0.2.2:25575" "10.0.2.2:8080" "169.254.169.254:80" "192.168.0.1:80")
for t in "${TARGETS[@]}"; do
  if sx "nc -z -w 3 ${t%:*} ${t#*:}" >/dev/null; then bad "reached $t (must be blocked)"; else ok "blocked $t"; fi
done

echo "-- destroy"
c=$(code "${CSRF[@]}" -X DELETE "$BASE/api/sandboxes/$SID")
[[ $c == 204 ]] && ok "destroyed" || bad "destroy returned $c"
[[ $(code "$BASE/api/sandboxes/$SID") == 404 ]] && ok "gone (404)" || bad "still present"

echo "-- audit (latest 5)"
api "$BASE/api/audit?limit=5" | python3 -c 'import json,sys,time
for e in json.load(sys.stdin): print("       ", time.strftime("%H:%M:%S", time.localtime(e["ts"])), e["action"], e["target"] or "", "ok" if e["ok"] else "FAILED")'

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
