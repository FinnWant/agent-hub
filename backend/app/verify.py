"""Checks: a sandbox's own command (e.g. `npm test && npm run lint`) that says whether the
agent's work is done. "The agent says it's done" is the weakest signal there is, so Kiln runs
the checks itself:

  * when the agent tries to stop (a Stop hook, see hooks.py): failing checks block the stop,
    and the agent gets the output and keeps working, up to VERIFY_MAX_REPAIRS times;
  * once more after the agent has finished, before the work is committed and pushed. The run
    records `verify` = passed | failed; a branch whose checks fail is never pushed.

Checks run in /workspace as the sandbox user, with the sandbox's limits, like anything the agent
runs. They prove what the project's own tests prove, no more: the agent can edit the tests too,
which is why the diff still gets reviewed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import config
from .docker_backend import BackendError
from .hooks import HookCall, HookReply

if TYPE_CHECKING:
    from .db import Run
    from .runs import RunManager

TAIL = 3000  # characters of check output shown in the log and given to the agent
MAX_COMMAND = 1000


@dataclass
class Check:
    passed: bool
    exit_code: int | None
    output: str          # the end of it
    timed_out: bool
    seconds: float

    def event(self, phase: str, attempt: int | None = None) -> dict[str, Any]:
        ev: dict[str, Any] = {"type": "log", "kind": "check", "phase": phase, "passed": self.passed,
                              "exit_code": self.exit_code, "timed_out": self.timed_out,
                              "seconds": round(self.seconds, 1), "text": self.output}
        if attempt is not None:
            ev["attempt"] = attempt
        return ev

    def summary(self) -> str:
        if self.passed:
            return "pass"
        if self.timed_out:
            return "took too long"
        return f"fail (exit {self.exit_code})" if self.exit_code is not None else "fail"


def tail(text: str, n: int = TAIL) -> str:
    text = text.rstrip()
    return text if len(text) <= n else "…" + text[-n:]


async def run_check(manager: RunManager, run: Run) -> Check:
    sb = manager.sandboxes.get(run.sandbox_id)
    t0 = time.monotonic()
    try:
        r = await asyncio.to_thread(manager.sandboxes.backend.exec, sb.container_id or "",
                                    ["bash", "-c", run.verify_cmd or "true"], config.settings.verify_timeout)
    except BackendError as e:
        return Check(False, None, f"Couldn't run the checks: {e}", False, time.monotonic() - t0)
    passed = r.exit_code == 0 and not r.timed_out
    return Check(passed, r.exit_code, tail(r.output), r.timed_out, time.monotonic() - t0)


async def check_before_stop(manager: RunManager, run: Run, call: HookCall) -> HookReply | None:
    """Stop hook: the agent wants to finish. Failing checks send it back, a few times at most."""
    attempts = manager.verify_attempts.get(run.id, run.verify_attempts or 0)
    limit = config.settings.verify_max_repairs
    if attempts >= limit:
        if run.id not in manager.verify_gave_up:
            manager.verify_gave_up.add(run.id)
            manager._sys(run, f"Checks still fail after {limit} rounds of fixes; the agent may stop")
        return None
    manager._sys(run, f"The agent is done; running the checks: {run.verify_cmd}")
    check = await run_check(manager, run)
    manager.emit(run, check.event("stop", attempts + 1))
    if check.passed:
        return None
    attempts += 1
    manager.verify_attempts[run.id] = attempts
    manager.db.update_run(run, verify_attempts=attempts)
    left = limit - attempts
    return HookReply(code=2, stderr=(
        f"Kiln ran this project's checks (`{run.verify_cmd}`) and they {check.summary()}. "
        "Fix the cause (don't weaken or skip the checks), then finish. "
        f"{'This is the last round: after it you may stop either way. ' if left == 0 else ''}"
        f"Output (the end of it):\n{check.output}"))


def stop_handler():
    from .runs import HookHandler

    # The hook waits for the checks to run, plus a margin.
    return HookHandler("Stop", check_before_stop, wait=config.settings.verify_timeout + 60,
                       applies=lambda run: bool(run.verify_cmd))
