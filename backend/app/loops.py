"""Loop breaker: notices an agent repeating itself and steps in.

From the run's own log (tool calls and their results), not from anything the agent says:
  * the same tool call (tool + the start of its input) failing LOOP_REPEATS times among the last
    LOOP_WINDOW calls with nothing in between that suggests progress (that call succeeding, or
    a file edit), so fixing and re-running the tests doesn't count; or
  * the last 4 x LOOP_WINDOW calls use at most 3 distinct calls between them and change no file.
The first time, Kiln sends the agent a message to step back. If it trips again, the agent's
API calls are held and the owner is asked (a "stuck" request): Continue (with optional advice,
sent to the agent) or Stop. No answer within BUDGET_WAIT stops the run.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from . import config

EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def fingerprint(name: str, text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\d{3,}", "N", text)  # timestamps, ports, pids
    return f"{name}:{text[:120]}"


@dataclass
class LoopWatch:
    calls: deque = field(default_factory=lambda: deque(maxlen=4 * max(config.settings.loop_window, 5)))
    pending: str | None = None   # the last call, until its result arrives
    pending_name: str = ""
    tripped: set[str] = field(default_factory=set)
    trips: int = 0

    def tool(self, name: str, text: str) -> None:
        self.pending, self.pending_name = fingerprint(name, text), name

    def result(self, is_error: bool) -> str | None:
        """Record a tool result; returns why the agent looks stuck, the first time it does."""
        if self.pending is None:
            return None
        fp, name = self.pending, self.pending_name
        self.pending = None
        self.calls.append((fp, bool(is_error), name in EDITS))
        window = list(self.calls)[-config.settings.loop_window:]
        repeats = 0
        for call_fp, err, edit in reversed(window):  # back to the last sign of progress
            if edit or (call_fp == fp and not err):
                break
            repeats += call_fp == fp
        if is_error and repeats >= config.settings.loop_repeats and fp not in self.tripped:
            self.tripped.add(fp)
            return f"ran {fp.split(':', 1)[1][:100]!r} {repeats} times, failing the same way"
        calls = list(self.calls)
        if (len(calls) == self.calls.maxlen and len({c[0] for c in calls}) <= 3 and not any(c[2] for c in calls)
                and "stall" not in self.tripped):
            self.tripped.add("stall")
            return f"made the same few calls {len(calls)} times in a row without changing any file"
        return None
