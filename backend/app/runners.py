"""Agent runners: how an agent is started in a sandbox, how its output becomes events, and how
it is stopped. The rest of Agent Hub only sees the `AgentRunner` / `RunProcess` interfaces.

Events are plain dicts sent to the browser and stored per run:
    {"type": "log", "kind": "text|tool|tool_result|system|stderr|stdout|result|error", "text": ...}
    {"type": "cost", "cost_usd": float|None, "turns": int|None, "duration_ms": int|None}
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


@dataclass
class RunSpec:
    run_id: str
    container_id: str
    prompt: str
    timeout: int
    max_turns: int
    branch: str


@dataclass
class Outcome:
    exit_code: int | None = None
    is_error: bool | None = None   # the agent's own verdict, when it reports one
    error: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class RunProcess(Protocol):
    outcome: Outcome

    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def cancel(self) -> None: ...


class AgentRunner(Protocol):
    name: str

    def problem(self) -> str | None:
        """Why this runner can't start right now (e.g. missing credential), or None."""
        ...

    async def start(self, spec: RunSpec) -> RunProcess: ...


# ---------------------------------------------------------------------------
# Runners that exec a command in the sandbox
# ---------------------------------------------------------------------------

def pidfile(run_id: str) -> str:
    return f"/tmp/ah-run-{run_id}.pid"


def launch_argv(run_id: str, timeout: int, argv: list[str]) -> list[str]:
    """Run `argv` in its own session (so cancel can kill everything it started), record the
    session id, and keep coreutils `timeout` as a backstop behind Agent Hub's own timer."""
    script = f'echo $$ > {pidfile(run_id)}; exec timeout -k 10 {timeout + 60} "$@"'
    return ["setsid", "-w", "bash", "-c", script, "ah-run", *argv]


def kill_argv(run_id: str) -> list[str]:
    f = pidfile(run_id)
    return ["sh", "-c", f'[ -f {f} ] && pkill -KILL -s "$(cat {f})"; rm -f {f}; true']


class ExecProcess:
    def __init__(self, runner: ExecRunner, backend: Backend, spec: RunSpec, stream: Any) -> None:
        self.runner = runner
        self.backend = backend
        self.spec = spec
        self.stream = stream
        self.outcome = Outcome()
        self._cancelled = False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        buffers = {1: b"", 2: b""}
        async for stream_no, data in self.stream.chunks():
            buf = buffers.get(stream_no, b"") + data
            *lines, buffers[stream_no] = buf.split(b"\n")
            for raw in lines:
                for ev in self.runner.parse(stream_no, raw.decode("utf-8", "replace")[:MAX_LINE], self.outcome):
                    yield ev
        for stream_no, rest in buffers.items():
            if rest.strip():
                for ev in self.runner.parse(stream_no, rest.decode("utf-8", "replace")[:MAX_LINE], self.outcome):
                    yield ev
        if not self._cancelled:
            self.outcome.exit_code = await self.stream.exit_code()

    async def cancel(self) -> None:
        self._cancelled = True
        try:
            await asyncio.to_thread(self.backend.exec, self.spec.container_id, kill_argv(self.spec.run_id), 30)
        except BackendError:
            pass  # sandbox already stopped: the process is gone with it
        self.stream.close()


class ExecRunner:
    name = "exec"

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def problem(self) -> str | None:
        return None

    def argv(self, spec: RunSpec) -> list[str]:
        raise NotImplementedError

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {}

    def parse(self, stream_no: int, line: str, outcome: Outcome) -> list[dict[str, Any]]:
        if not line.strip():
            return []
        return [{"type": "log", "kind": "stderr" if stream_no == 2 else "stdout", "text": line}]

    async def start(self, spec: RunSpec) -> RunProcess:
        argv = launch_argv(spec.run_id, spec.timeout, self.argv(spec))
        stream = await self.backend.exec_stream(spec.container_id, argv, self.env(spec))
        return ExecProcess(self, self.backend, spec, stream)


class ScriptRunner(ExecRunner):
    """Runs the prompt as a bash script. For tests (timeout/cancel with a sleeping script);
    not offered in the UI."""

    name = "script"

    def argv(self, spec: RunSpec) -> list[str]:
        return ["bash", "-c", spec.prompt]


SYSTEM_PROMPT = (
    "You are running unattended in a disposable, network-restricted sandbox managed by Agent Hub. "
    "Work in /workspace, a git repository already checked out on branch {branch}. Stay on this branch; "
    "don't push and don't rewrite history: Agent Hub commits whatever you leave in the working tree "
    "and the owner reviews the diff. Nobody can answer questions during the run, so make reasonable "
    "decisions, keep changes focused on the request, and finish with a short summary of what you did."
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
    CLI reference and headless pages). Authenticates with CLAUDE_CODE_OAUTH_TOKEN from
    `claude setup-token`, passed to this process only."""

    name = "claude-code"

    def problem(self) -> str | None:
        if not config.settings.claude_code_oauth_token:
            return ("No Claude token is configured. On the server run "
                    "`python -m app.set_secret claude` and restart Agent Hub.")
        return None

    def argv(self, spec: RunSpec) -> list[str]:
        prompt = spec.prompt if not spec.prompt.startswith("-") else " " + spec.prompt
        argv = ["claude", "-p", prompt,
                "--output-format", "stream-json", "--verbose",
                "--max-turns", str(spec.max_turns),
                # The sandbox is the permission boundary: non-root, no capabilities, own volume,
                # filtered network, no GitHub credential.
                "--permission-mode", "bypassPermissions",
                "--append-system-prompt", SYSTEM_PROMPT.format(branch=spec.branch)]
        if config.settings.agent_model:
            argv += ["--model", config.settings.agent_model]
        return argv

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {"CLAUDE_CODE_OAUTH_TOKEN": config.settings.claude_code_oauth_token,
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
            events.append({"type": "log", "kind": "system",
                           "text": f"Claude Code {msg.get('claude_code_version', '')} · {msg.get('model', '')}".strip()})
        elif kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.append({"type": "log", "kind": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    events.append({"type": "log", "kind": "tool", "name": block.get("name", "tool"),
                                   "text": summarize_tool_input(block.get("name", ""), block.get("input"))})
        elif kind == "user":
            for block in (msg.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = tool_result_text(block.get("content"))
                    events.append({"type": "log", "kind": "tool_result", "is_error": bool(block.get("is_error")),
                                   "text": text[:MAX_TOOL_RESULT] + ("…" if len(text) > MAX_TOOL_RESULT else "")})
        elif kind == "result":
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

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if self.spec.prompt.strip() == "fail":
            yield {"type": "log", "kind": "error", "text": "Simulated failure"}
            self.outcome = Outcome(exit_code=1, is_error=True, error="Simulated failure")
            return
        for delay, ev in DEMO_SCRIPT:
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=delay * self.scale)
                return
            except TimeoutError:
                yield ev
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
