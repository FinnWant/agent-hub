"""Agent runners: how an agent is started in a sandbox, how its output becomes events, and how
it is stopped. The rest of Kiln only sees the `AgentRunner` / `RunProcess` interfaces.

Events are plain dicts sent to the browser and stored per run:
    {"type": "log", "kind": "text|tool|tool_result|system|stderr|stdout|result|error|user", "text": ...}
    {"type": "cost", "cost_usd": float|None, "turns": int|None, "duration_ms": int|None}

Exec runners start the agent *detached* inside the sandbox (its own session, output to files in
the run directory) and follow its output with a separate exec. Kiln can therefore restart
without killing the agent: on startup it re-attaches and keeps reading from the byte offset it
had stored. Interactive runners also get a FIFO as stdin, so Kiln can send the agent messages
while it works, and closes it when the agent has nothing left to do.

The run directory is owned by the sandbox user, like everything else in the sandbox, so an agent
could tamper with its own log. That is no new exposure (it could already write to its own
stdout through /proc), and the owner reviews the diff, not the log.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config
from .docker_backend import Backend, BackendError

MAX_LINE = 20_000         # characters kept from one output line
MAX_TOOL_RESULT = 4_000   # characters of a tool result shown in the log
MAX_MESSAGE = 4_000       # characters in one message sent to a running agent
RUN_ROOT = "/tmp/kiln-runs"
RECONNECTS = 5            # follower restarts in a row without new output before giving up


class RunError(Exception):
    """A request about a run that can't be honoured right now (shown to the user)."""


@dataclass
class RunSpec:
    run_id: str
    container_id: str
    prompt: str
    timeout: int
    max_turns: int
    branch: str
    preview_port: int = 8000
    sandbox_id: str = ""


@dataclass
class Outcome:
    exit_code: int | None = None
    is_error: bool | None = None   # the agent's own verdict, when it reports one
    error: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # runner state, persisted with the offset


class RunProcess(Protocol):
    outcome: Outcome

    def events(self) -> AsyncIterator[dict[str, Any]]:
        """Events in order. An event carrying `"_progress": True` ends a unit of output: once it is
        stored, `state()` may be persisted, and re-attaching from that state won't repeat it."""
        ...

    async def cancel(self) -> None: ...

    # Optional:
    #   `async def send(text)`      deliver a message to the running agent (raises RunError)
    #   `can_send: bool`            whether send() would currently be accepted
    #   `def state() -> dict`       JSON-able state to re-attach from after a Kiln restart


class AgentRunner(Protocol):
    name: str

    def problem(self) -> str | None:
        """Why this runner can't start right now (e.g. missing credential), or None."""
        ...

    async def start(self, spec: RunSpec) -> RunProcess: ...

    # Optional:
    #   `async def release(run_id, container_id)` called once the run has ended, whatever happened
    #   `def status(spec) -> "DONE"|"ALIVE"|"GONE"|None`   is the agent still there (after a restart)
    #   `async def attach(spec, state) -> RunProcess`       follow an agent that is still running


# ---------------------------------------------------------------------------
# Runners that exec a command in the sandbox
# ---------------------------------------------------------------------------

def run_dir(run_id: str) -> str:
    return f"{RUN_ROOT}/{run_id}"


def pidfile(run_id: str) -> str:
    return f"{run_dir(run_id)}/pid"


# Start the agent in its own session and return as soon as it runs. Files in the run directory:
#   pid     session leader (cancel kills the whole session)    out/err  the agent's stdout/stderr
#   in      FIFO the agent reads (interactive runners only)     holder   keeps `in` open until closed
#   exit    the agent's exit status, written last (atomically)
# Arguments: run dir, backstop timeout (coreutils `timeout` behind Kiln's own timer), "1" if
# interactive, then the agent's argv. The first message comes in $AH_MSG.
LAUNCH_SCRIPT = r"""d=$1; t=$2; inter=$3; shift 3
mkdir -p "${d%/*}" && mkdir -m 700 "$d" || { echo "Couldn't create the run directory $d" >&2; exit 1; }
: > "$d/out"; : > "$d/err"
if [ -n "$inter" ]; then mkfifo -m 600 "$d/in" || exit 1; fi
setsid -f bash -c '
d=$1; t=$2; inter=$3; shift 3
echo $$ > "$d/pid.tmp" && mv "$d/pid.tmp" "$d/pid"
if [ -n "$inter" ]; then
  sleep 2147483647 > "$d/in" & echo $! > "$d/holder"
  printf "%s\n" "$AH_MSG" > "$d/in" & echo $! > "$d/writer"
  timeout -k 10 "$t" "$@" < "$d/in" >> "$d/out" 2>> "$d/err"
else
  timeout -k 10 "$t" "$@" < /dev/null >> "$d/out" 2>> "$d/err"
fi
code=$?
for f in holder writer; do [ -f "$d/$f" ] && kill "$(cat "$d/$f")" 2>/dev/null; done
echo "$code" > "$d/exit.tmp" && mv "$d/exit.tmp" "$d/exit"
' ah-run "$d" "$t" "$inter" "$@" < /dev/null > /dev/null 2>&1
i=0
while [ ! -s "$d/pid" ]; do
  i=$((i + 1)); [ "$i" -gt 200 ] && { echo "The agent didn't start" >&2; exit 1; }
  sleep 0.05
done
"""

# Stream `out` from a byte offset until the agent's session leader exits; then replay the end of
# `err` on stderr and exit with the agent's status (125: no status, the agent may still run).
FOLLOW_SCRIPT = r"""d=$1; off=$2
pid=$(cat "$d/pid" 2>/dev/null)
if [ -n "$pid" ] && [ ! -f "$d/exit" ]; then
  tail -q -c "+$((off + 1))" --pid="$pid" -s 0.3 -F "$d/out" 2>/dev/null
else
  tail -c "+$((off + 1))" "$d/out" 2>/dev/null
fi
if [ -f "$d/exit" ]; then
  tail -c 8192 "$d/err" >&2
  exit "$(cat "$d/exit")"
fi
exit 125
"""

STATUS_SCRIPT = r"""d=$1
if [ -f "$d/exit" ]; then echo DONE; exit 0; fi
pid=$(cat "$d/pid" 2>/dev/null)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo ALIVE; else echo GONE; fi
"""

SEND_SCRIPT = r"""d=$1
[ -p "$d/in" ] && [ ! -f "$d/exit" ] || { echo "The agent isn't reading messages any more" >&2; exit 3; }
printf '%s\n' "$AH_MSG" > "$d/in"
"""

CLOSE_SCRIPT = r"""d=$1
h=$(cat "$d/holder" 2>/dev/null) && kill "$h" 2>/dev/null
true
"""


def launch_argv(run_id: str, timeout: int, argv: list[str], interactive: bool = False) -> list[str]:
    return ["bash", "-c", LAUNCH_SCRIPT, "ah-launch", run_dir(run_id), str(timeout + 60),
            "1" if interactive else "", *argv]


def follow_argv(run_id: str, offset: int) -> list[str]:
    return ["bash", "-c", FOLLOW_SCRIPT, "ah-follow", run_dir(run_id), str(int(offset))]


def status_argv(run_id: str) -> list[str]:
    return ["bash", "-c", STATUS_SCRIPT, "ah-status", run_dir(run_id)]


def send_argv(run_id: str) -> list[str]:
    return ["bash", "-c", SEND_SCRIPT, "ah-send", run_dir(run_id)]


def close_argv(run_id: str) -> list[str]:
    return ["bash", "-c", CLOSE_SCRIPT, "ah-close", run_dir(run_id)]


def kill_argv(run_id: str) -> list[str]:
    # Also the pidfile of runs started before runs were detached (Kiln <= 0.6).
    files = f"{pidfile(run_id)} /tmp/ah-run-{run_id}.pid"
    return ["sh", "-c", f'for f in {files}; do [ -f "$f" ] && pkill -KILL -s "$(cat "$f")"; rm -f "$f"; done; true']


def cleanup_argv(run_id: str) -> list[str]:
    return ["rm", "-rf", run_dir(run_id)]


def parse_status(output: str) -> str | None:
    lines = output.strip().splitlines()
    last = lines[-1].strip() if lines else ""
    return last if last in ("DONE", "ALIVE", "GONE") else None


class DetachedProcess:
    """Follows an agent started by LAUNCH_SCRIPT. `offset` counts the bytes of `out` already
    turned into events; following again from it (after a restart) repeats nothing."""

    def __init__(self, runner: ExecRunner, backend: Backend, spec: RunSpec,
                 offset: int = 0, extra: dict[str, Any] | None = None) -> None:
        self.runner = runner
        self.backend = backend
        self.spec = spec
        self.offset = offset
        self.outcome = Outcome(extra=dict(extra or {}))
        self.stream: Any = None
        self._cancelled = False

    def state(self) -> dict[str, Any]:
        return {**self.outcome.extra, "offset": self.offset}

    @property
    def can_send(self) -> bool:
        return self.runner.interactive and not self._cancelled and not self.outcome.extra.get("input_closed")

    async def _exec(self, argv: list[str], timeout: int = 30, env: dict[str, str] | None = None):
        return await asyncio.to_thread(self.backend.exec, self.spec.container_id, argv, timeout, env)

    def _lines(self, stream_no: int, raw: bytes) -> list[dict[str, Any]]:
        return self.runner.parse(stream_no, raw.decode("utf-8", "replace")[:MAX_LINE], self.outcome)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        stale = 0
        while True:
            self.stream = await self.backend.exec_stream(
                self.spec.container_id, follow_argv(self.spec.run_id, self.offset), {})
            out, err, progressed = b"", b"", False
            async for stream_no, data in self.stream.chunks():
                if stream_no != 1:
                    err += data
                    *lines, err = err.split(b"\n")
                    for raw in lines:
                        for ev in self._lines(2, raw):
                            yield ev
                    continue
                out += data
                *lines, out = out.split(b"\n")
                for raw in lines:
                    async for ev in self._unit(raw, len(raw) + 1):
                        yield ev
                    progressed = True
            if self._cancelled:
                return
            code = await self.stream.exit_code()
            status = parse_status((await self._exec(status_argv(self.spec.run_id))).output)
            if status == "ALIVE":  # the follower stopped but the agent didn't: follow again
                stale = 0 if progressed else stale + 1
                if stale <= RECONNECTS:
                    await asyncio.sleep(1)
                    continue
                self.outcome.error = "Lost contact with the agent's output"
                await self.cancel()
                return
            if out.strip():  # a last line without a newline
                async for ev in self._unit(out, len(out)):
                    yield ev
            if err.strip():
                for ev in self._lines(2, err):
                    yield ev
            self.outcome.exit_code = None if status == "GONE" else code
            return

    async def _unit(self, raw: bytes, size: int) -> AsyncIterator[dict[str, Any]]:
        evs = self._lines(1, raw)
        self.offset += size
        if evs:
            evs[-1]["_progress"] = True
        for ev in evs:
            yield ev
        await self._maybe_close()

    async def _maybe_close(self) -> None:
        """Interactive agents read until their input closes: close it once they are idle and
        every message sent to them has been seen."""
        if self.runner.interactive and not self.outcome.extra.get("input_closed") \
                and self.runner.should_close(self.outcome):
            self.outcome.extra["input_closed"] = True
            try:
                await self._exec(close_argv(self.spec.run_id))
            except BackendError:
                pass

    async def send(self, text: str) -> None:
        if not self.can_send:
            raise RunError("The agent has finished its work and isn't taking messages")
        extra = self.outcome.extra
        extra["sent"] = extra.get("sent", 1) + 1  # before the write, so the input can't close meanwhile
        try:
            r = await self._exec(send_argv(self.spec.run_id), 15, {"AH_MSG": self.runner.user_message(text)})
            if r.exit_code != 0:
                raise RunError(r.output.strip()[-200:] or "Couldn't deliver the message")
        except (BackendError, RunError):
            extra["sent"] -= 1
            await self._maybe_close()
            raise

    async def cancel(self) -> None:
        self._cancelled = True
        try:
            await self._exec(kill_argv(self.spec.run_id))
        except BackendError:
            pass  # sandbox already stopped: the process is gone with it
        if self.stream is not None:
            self.stream.close()


class ExecRunner:
    name = "exec"
    interactive = False  # reads messages from stdin (see LAUNCH_SCRIPT)

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def problem(self) -> str | None:
        return None

    def argv(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {}

    def first_message(self, spec: RunSpec) -> str:
        return ""

    def user_message(self, text: str) -> str:
        return text

    def should_close(self, outcome: Outcome) -> bool:
        return False

    def initial_state(self, spec: RunSpec) -> dict[str, Any]:
        return {}

    def parse(self, stream_no: int, line: str, outcome: Outcome) -> list[dict[str, Any]]:
        if not line.strip():
            return []
        return [{"type": "log", "kind": "stderr" if stream_no == 2 else "stdout", "text": line}]

    async def start(self, spec: RunSpec) -> RunProcess:
        env = dict(self.env(spec))
        if self.interactive:
            env["AH_MSG"] = self.first_message(spec)
        argv = launch_argv(spec.run_id, spec.timeout, self.argv(spec), self.interactive)
        r = await asyncio.to_thread(self.backend.exec, spec.container_id, argv, 60, env)
        if r.exit_code != 0:
            raise BackendError(f"Couldn't start the agent: {r.output.strip()[-300:]}")
        return DetachedProcess(self, self.backend, spec, extra=self.initial_state(spec))

    def status(self, spec: RunSpec) -> str | None:
        return parse_status(self.backend.exec(spec.container_id, status_argv(spec.run_id), 30).output)

    async def attach(self, spec: RunSpec, state: dict[str, Any]) -> RunProcess:
        state = dict(state)
        offset = int(state.pop("offset", 0))
        return DetachedProcess(self, self.backend, spec, offset=offset, extra=state)

    async def release(self, run_id: str, container_id: str | None = None) -> None:
        if container_id:
            try:
                await asyncio.to_thread(self.backend.exec, container_id, cleanup_argv(run_id), 30)
            except BackendError:
                pass


class ScriptRunner(ExecRunner):
    """Runs the prompt as a bash script. For tests (timeout/cancel with a sleeping script);
    not offered in the UI."""

    name = "script"

    def argv(self, spec: RunSpec) -> list[str]:
        return ["bash", "-c", spec.prompt]


SYSTEM_PROMPT = (
    "You are running unattended in a disposable, network-restricted sandbox managed by Kiln. "
    "Work in /workspace, a git repository already checked out on branch {branch}. Stay on this branch; "
    "don't push and don't rewrite history: Kiln commits whatever you leave in the working tree "
    "and the owner reviews the diff. Nobody will answer questions during the run, so make reasonable "
    "decisions, keep changes focused on the request, and finish with a short summary of what you did. "
    "The owner may send you messages while you work; take them into account when they arrive. "
    "If you start a web server for the owner to preview, it must listen on 0.0.0.0 port {preview_port} "
    "and keep running in the background."
)


def summarize_tool_input(name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)[:300]
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        if isinstance(data.get(key), str):
            return data[key][:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


class ClaudeCodeRunner(ExecRunner):
    """Claude Code's CLI in print mode with stream-json output (flags per code.claude.com/docs,
    CLI reference and headless pages).

    The real token (from `claude setup-token`) never enters the sandbox: the agent gets a
    per-run key and talks to the Claude gateway (gateway.py), which swaps in the token and only
    forwards to the Anthropic API. The key stops working when the run ends.
    CLAUDE_TOKEN_MODE=direct passes the real token instead (fallback, not recommended)."""

    name = "claude-code"
    # Messages arrive as stream-json on stdin. With --replay-user-messages the CLI echoes each one
    # when the agent takes it in (at the next tool boundary, or as a new turn once idle), so Kiln
    # knows when every message has been seen. After a `result` the CLI waits for more input; it
    # exits when stdin closes (checked against Claude Code 2.1.325).
    interactive = True

    def __init__(self, backend: Backend, gateway: Any = None) -> None:
        super().__init__(backend)
        from .gateway import Gateway

        self.gateway = gateway or Gateway(backend)
        self._keys: dict[str, str] = {}

    @property
    def via_gateway(self) -> bool:
        return config.settings.claude_token_mode != "direct"

    async def start(self, spec: RunSpec) -> RunProcess:
        if self.via_gateway:
            self._keys[spec.run_id] = await asyncio.to_thread(self.gateway.issue, spec.sandbox_id, spec.run_id)
        try:
            return await super().start(spec)
        finally:
            self._keys.pop(spec.run_id, None)  # only needed for the launch environment

    def initial_state(self, spec: RunSpec) -> dict[str, Any]:
        state: dict[str, Any] = {"sent": 1, "acked": 0}
        if digest := self.key_digest(spec.run_id):
            state["key_digest"] = digest  # to register the key again after a restart
        return state

    async def attach(self, spec: RunSpec, state: dict[str, Any]) -> RunProcess:
        """Follow an agent that kept running while Kiln restarted. Its gateway key is registered
        again (the gateway may have been replaced since)."""
        if self.via_gateway:
            await asyncio.to_thread(self.gateway.restore, spec.sandbox_id, spec.run_id, state.get("key_digest") or "")
        return await super().attach(spec, state)

    async def release(self, run_id: str, container_id: str | None = None) -> None:
        self._keys.pop(run_id, None)
        if self.via_gateway:
            await asyncio.to_thread(self.gateway.revoke, run_id)
        await super().release(run_id, container_id)

    def key_digest(self, run_id: str) -> str | None:
        return self.gateway.digest(run_id) if self.via_gateway else None

    def first_message(self, spec: RunSpec) -> str:
        return self.user_message(spec.prompt)

    def user_message(self, text: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def should_close(self, outcome: Outcome) -> bool:
        x = outcome.extra
        if not x.get("idle"):
            return False
        # A CLI that never echoed anything (not even the prompt) doesn't support replays: then
        # there is no telling whether a message is still unread, so the turn's end is the end.
        return x.get("acked", 0) == 0 or x.get("acked", 0) >= x.get("sent", 1)

    def problem(self) -> str | None:
        if not config.settings.claude_code_oauth_token:
            return ("No Claude token is configured. On the server run "
                    "`python -m app.set_secret claude` and restart Kiln.")
        return None

    def argv(self, spec: RunSpec) -> list[str]:
        argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--replay-user-messages",
                "--max-turns", str(spec.max_turns),
                # The sandbox is the permission boundary: non-root, no capabilities, own volume,
                # filtered network, no GitHub credential.
                "--permission-mode", "bypassPermissions",
                "--append-system-prompt", SYSTEM_PROMPT.format(branch=spec.branch, preview_port=spec.preview_port)]
        if config.settings.agent_model:
            argv += ["--model", config.settings.agent_model]
        return argv

    def env(self, spec: RunSpec) -> dict[str, str]:
        if not self.via_gateway:
            return {"CLAUDE_CODE_OAUTH_TOKEN": config.settings.claude_code_oauth_token, "HOME": "/home/agent"}
        from .gateway import GATEWAY_URL

        return {"CLAUDE_CODE_OAUTH_TOKEN": self._keys[spec.run_id], "ANTHROPIC_BASE_URL": GATEWAY_URL,
                "HOME": "/home/agent"}

    def parse(self, stream_no: int, line: str, outcome: Outcome) -> list[dict[str, Any]]:
        if stream_no == 2 or not line.startswith("{"):
            return super().parse(stream_no, line, outcome)
        try:
            msg = json.loads(line)
        except ValueError:
            return super().parse(stream_no, line, outcome)
        kind = msg.get("type")
        events: list[dict[str, Any]] = []
        if kind == "system" and msg.get("subtype") == "init":
            outcome.extra["session_id"] = msg.get("session_id")
            if outcome.extra.get("init_seen"):  # once per run is enough, whatever the CLI does
                return events
            outcome.extra["init_seen"] = True
            events.append({"type": "log", "kind": "system",
                           "text": f"Claude Code {msg.get('claude_code_version', '')} · {msg.get('model', '')}".strip()})
        elif kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.append({"type": "log", "kind": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    events.append({"type": "log", "kind": "tool", "name": block.get("name", "tool"),
                                   "text": summarize_tool_input(block.get("name", ""), block.get("input"))})
        elif kind == "user" and msg.get("isReplay"):
            text = tool_result_text((msg.get("message") or {}).get("content"))
            outcome.extra["acked"] = outcome.extra.get("acked", 0) + 1
            outcome.extra["idle"] = False
            if outcome.extra["acked"] > 1:  # the first one is the prompt, shown with the run already
                events.append({"type": "log", "kind": "user", "text": text})
        elif kind == "user":
            for block in (msg.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = tool_result_text(block.get("content"))
                    events.append({"type": "log", "kind": "tool_result", "is_error": bool(block.get("is_error")),
                                   "text": text[:MAX_TOOL_RESULT] + ("…" if len(text) > MAX_TOOL_RESULT else "")})
        elif kind == "result":
            outcome.extra["idle"] = True
            outcome.is_error = bool(msg.get("is_error"))
            outcome.cost_usd = msg.get("total_cost_usd")
            outcome.num_turns = msg.get("num_turns")
            if outcome.is_error:
                errors = msg.get("errors") or []
                outcome.error = "; ".join(map(str, errors)) or str(msg.get("subtype") or "agent reported an error")
                if msg.get("subtype") == "error_max_turns":
                    outcome.error = f"Stopped after the turn limit ({outcome.num_turns} turns)"
                events.append({"type": "log", "kind": "error", "text": outcome.error})
            elif msg.get("result"):
                events.append({"type": "log", "kind": "result", "text": str(msg["result"])})
            events.append({"type": "cost", "cost_usd": outcome.cost_usd, "turns": outcome.num_turns,
                           "duration_ms": msg.get("duration_ms")})
        return events


# ---------------------------------------------------------------------------
# Demo runner (DEMO_MODE): a scripted agent, no Docker
# ---------------------------------------------------------------------------

DEMO_SCRIPT: list[tuple[float, dict[str, Any]]] = [
    (0.4, {"type": "log", "kind": "system", "text": "Claude Code (demo) · simulated model"}),
    (0.8, {"type": "log", "kind": "text", "text": "I'll start by looking at the project layout."}),
    (0.5, {"type": "log", "kind": "tool", "name": "Bash", "text": "ls -la"}),
    (0.6, {"type": "log", "kind": "tool_result", "is_error": False,
           "text": "README.md\nsrc/\ntests/\npyproject.toml"}),
    (0.7, {"type": "log", "kind": "tool", "name": "Read", "text": "README.md"}),
    (0.5, {"type": "log", "kind": "tool_result", "is_error": False, "text": "# Demo project\n\nA small example."}),
    (1.0, {"type": "log", "kind": "text", "text": "The README is thin. I'll add a section describing the project."}),
    (0.6, {"type": "log", "kind": "tool", "name": "Edit", "text": "README.md"}),
    (0.5, {"type": "log", "kind": "tool_result", "is_error": False, "text": "The file README.md has been updated."}),
    (0.8, {"type": "log", "kind": "result",
           "text": "Added an \"About this project\" section to README.md describing what it does and how it's laid out."}),
]


class DemoProcess:
    def __init__(self, spec: RunSpec, scale: float = 1.0) -> None:
        self.spec = spec
        self.scale = scale
        self.outcome = Outcome()
        self._cancel = asyncio.Event()
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._done = False

    @property
    def can_send(self) -> bool:
        return not self._done and not self._cancel.is_set()

    async def send(self, text: str) -> None:
        if not self.can_send:
            raise RunError("The agent has finished its work and isn't taking messages")
        self._inbox.put_nowait(text)

    def _replies(self) -> list[dict[str, Any]]:
        evs: list[dict[str, Any]] = []
        while not self._inbox.empty():
            text = self._inbox.get_nowait()
            evs += [{"type": "log", "kind": "user", "text": text},
                    {"type": "log", "kind": "text", "text": f"(demo) Noted: {text[:200]}"}]
        return evs

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if self.spec.prompt.strip() == "fail":
            self._done = True
            yield {"type": "log", "kind": "error", "text": "Simulated failure"}
            self.outcome = Outcome(exit_code=1, is_error=True, error="Simulated failure")
            return
        for delay, ev in DEMO_SCRIPT:
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=delay * self.scale)
                return
            except TimeoutError:
                for reply in self._replies():
                    yield reply
                yield ev
        self._done = True
        for reply in self._replies():
            yield reply
        self.outcome = Outcome(exit_code=0, is_error=False, cost_usd=0.0123, num_turns=4)
        yield {"type": "cost", "cost_usd": 0.0123, "turns": 4, "duration_ms": 6400}

    async def cancel(self) -> None:
        self._cancel.set()


class DemoRunner:
    name = "claude-code"

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale  # tests speed the script up

    def problem(self) -> str | None:
        return None

    async def start(self, spec: RunSpec) -> RunProcess:
        return DemoProcess(spec, self.scale)


def default_runners(backend: Backend) -> dict[str, AgentRunner]:
    if config.settings.demo_mode:
        return {"claude-code": DemoRunner()}
    return {"claude-code": ClaudeCodeRunner(backend)}
