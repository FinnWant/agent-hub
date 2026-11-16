"""Claude Code hooks installed and answered by Kiln.

Before a Claude Code run starts, Kiln copies into the sandbox, owned by root (the agent can read
but not change them):

    /etc/claude-code/managed-settings.json   the hooks Kiln wants, for this run. Managed settings
                                             can't be overridden or disabled by user or project
                                             settings (code.claude.com/docs, settings/hooks).
    /usr/local/lib/kiln/kiln-hook            the one command every hook runs
    /var/lib/kiln/replies/<run-id>/          where Kiln answers hooks that wait for a decision

The channel back to Kiln needs no network. `kiln-hook` saves its input (the hook's JSON) in the
run directory and appends one marker line to the agent's output file, which Kiln already
follows (see runners.py). Kiln reads the input when a handler wants it. A hook that waits polls
for Kiln's reply, a root-owned file the agent can't forge, and gives up (no decision: Claude Code
carries on) after its wait time.

Repositories' own hooks keep working: Kiln doesn't set `allowManagedHooksOnly`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

HOOK_COMMAND = "/usr/local/lib/kiln/kiln-hook"
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
REPLY_ROOT = "/var/lib/kiln/replies"
MAX_PAYLOAD = 2 * 2**20       # bytes of hook input Kiln reads
MAX_REPLY = 64 * 1024         # bytes of hook output Kiln writes
HOOK_ID = re.compile(r"\d{1,20}-\d{1,10}")
EVENT = re.compile(r"[A-Za-z]{1,40}")

# kiln-hook <run dir> <reply dir> <event> <wait seconds>
# Output marker: {"type":"kiln_hook","event":...,"id":...}. Reply file: exit code, "out" or
# "err", the text, and a last line "#end" (so a half-written file is never used).
HOOK_SCRIPT = r"""#!/bin/bash
d=$1; r=$2; ev=$3; wait=${4:-0}
[ -d "$d" ] && [ -f "$d/out" ] || exit 0
id="$(date +%s%N)-$$"
umask 077
head -c 2097152 > "$d/hook-$id.json"; cat > /dev/null
printf '{"type":"kiln_hook","event":"%s","id":"%s"}\n' "$ev" "$id" >> "$d/out"
[ "$wait" -gt 0 ] 2>/dev/null || exit 0
end=$((SECONDS + wait))
f="$r/$id"
while [ "$SECONDS" -lt "$end" ]; do
  if [ -f "$f" ] && [ "$(tail -n 1 "$f")" = "#end" ]; then
    code=$(sed -n 1p "$f"); to=$(sed -n 2p "$f")
    body=$(sed '1,2d;$d' "$f")
    if [ -n "$body" ]; then
      if [ "$to" = err ]; then printf '%s\n' "$body" >&2; else printf '%s\n' "$body"; fi
    fi
    exit "${code:-0}"
  fi
  sleep 0.2
done
exit 0
"""


# kiln-ask "question" ["option" ...]: the agent asks the owner (owner.py). Same channel as the
# hooks: the question (then a \x1e line, then one option per line) goes to Kiln as a "KilnAsk"
# call, and the answer, or a "no answer yet" note, is printed.
ASK_SCRIPT = r"""#!/bin/bash
if [ $# -lt 1 ] || [ -z "$1" ]; then
  echo 'usage: kiln-ask "question" ["option" ...]' >&2; exit 64
fi
if [ -z "$KILN_RUN_DIR" ] || [ -z "$KILN_REPLY_DIR" ]; then
  echo "kiln-ask only works inside a Kiln agent run" >&2; exit 69
fi
q=$1; shift
{ printf '%s\n\036\n' "$q"; for o in "$@"; do printf '%s\n' "$o"; done; } |
  /usr/local/lib/kiln/kiln-hook "$KILN_RUN_DIR" "$KILN_REPLY_DIR" KilnAsk "${KILN_ASK_WAIT:-90}"
"""


@dataclass
class HookReply:
    """What a waiting hook returns to Claude Code: exit code 0 with JSON on stdout (e.g. a
    PreToolUse `permissionDecision`), or exit code 2 with a reason on stderr (blocks)."""

    code: int = 0
    stdout: dict[str, Any] | None = None
    stderr: str = ""
    text: str = ""  # plain output instead of JSON (what kiln-ask prints)

    def render(self) -> bytes:
        if self.code == 2:
            to, body = "err", self.stderr
        else:
            to, body = "out", json.dumps(self.stdout) if self.stdout else self.text
        body = body.replace("\r", "")[:MAX_REPLY]
        return f"{int(self.code)}\n{to}\n{body}\n#end\n".encode()


@dataclass
class HookCall:
    event: str
    id: str
    payload: dict[str, Any] = field(default_factory=dict)


def run_reply_dir(run_id: str) -> str:
    return f"{REPLY_ROOT}/{run_id}"


def managed_settings(run_dir: str, run_id: str, hooks: dict[str, int],
                     matchers: dict[str, str] | None = None) -> dict[str, Any]:
    """hooks: event name -> seconds the hook waits for Kiln's reply (0: doesn't wait)."""
    out: dict[str, Any] = {}
    for event, wait in sorted(hooks.items()):
        if not EVENT.fullmatch(event):
            raise ValueError(f"bad hook event {event!r}")
        wait = max(0, min(int(wait), 3600))
        entry: dict[str, Any] = {"hooks": [{
            "type": "command",
            "command": f"{HOOK_COMMAND} {run_dir} {run_reply_dir(run_id)} {event} {wait}",
            "timeout": wait + 15,
        }]}
        if matchers and event in matchers:
            entry["matcher"] = matchers[event]
        out[event] = [entry]
    return {"hooks": out}


def provision_entries(run_dir: str, run_id: str, hooks: dict[str, int],
                      matchers: dict[str, str] | None = None) -> list[tuple[str, bytes | None, int]]:
    """The root-owned files and directories for one run (see Backend.put_tree)."""
    settings = json.dumps(managed_settings(run_dir, run_id, hooks, matchers), indent=2).encode()
    return [
        ("etc/claude-code", None, 0o755),
        ("etc/claude-code/managed-settings.json", settings, 0o644),
        ("usr/local/lib/kiln", None, 0o755),
        ("usr/local/lib/kiln/kiln-hook", HOOK_SCRIPT.encode(), 0o755),
        ("usr/local/bin/kiln-ask", ASK_SCRIPT.encode(), 0o755),
        ("var/lib/kiln", None, 0o755),
        ("var/lib/kiln/replies", None, 0o755),
        (run_reply_dir(run_id).lstrip("/"), None, 0o755),
    ]


def reply_entries(run_id: str, hook_id: str, reply: HookReply) -> list[tuple[str, bytes | None, int]]:
    if not HOOK_ID.fullmatch(hook_id):
        raise ValueError("bad hook id")
    return [(f"{run_reply_dir(run_id).lstrip('/')}/{hook_id}", reply.render(), 0o644)]


def parse_marker(msg: dict[str, Any]) -> HookCall | None:
    """The marker line kiln-hook appends to the output (anything else in it is ignored)."""
    event, hook_id = msg.get("event"), msg.get("id")
    if isinstance(event, str) and isinstance(hook_id, str) and EVENT.fullmatch(event) and HOOK_ID.fullmatch(hook_id):
        return HookCall(event=event, id=hook_id)
    return None
