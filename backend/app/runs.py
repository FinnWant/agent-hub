"""Agent runs: lifecycle, limits, event log and live fan-out.

    queued -> running -> succeeded | failed | cancelled | timed_out

One run per sandbox at a time. Every event is redacted, stored (up to RUN_MAX_EVENTS) and
published to WebSocket subscribers. Whatever the outcome, the agent's work is committed to
`agent/<run-id>` so it can be reviewed; only a successful run is pushed (if allowed).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from typing import Any

from . import config
from .db import ACTIVE_RUN_STATES, Database, Run
from .redact import Redactor
from .runners import AgentRunner, RunProcess, RunSpec
from .sandboxes import CapacityError, InvalidTransition, NotFound, SandboxError, SandboxService
from .workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)

FINAL_STATES = ("succeeded", "failed", "cancelled", "timed_out")


class RunManager:
    def __init__(self, db: Database, sandboxes: SandboxService, runners: dict[str, AgentRunner],
                 workspace: Workspace) -> None:
        self.db = db
        self.sandboxes = sandboxes
        self.runners = runners
        self.workspace = workspace
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._procs: dict[str, RunProcess] = {}
        self._cancel_reason: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._dropped: set[str] = set()

    # ---- queries -------------------------------------------------------------

    def get(self, run_id: str) -> Run:
        run = self.db.get_run(run_id)
        if run is None:
            raise NotFound("No such run")
        return run

    def active_for(self, sandbox_id: str) -> Run | None:
        runs = self.db.list_runs(sandbox_id=sandbox_id, states=ACTIVE_RUN_STATES, limit=1)
        return runs[0] if runs else None

    # ---- events --------------------------------------------------------------

    def emit(self, run: Run, event: dict[str, Any]) -> None:
        event = Redactor.from_settings().obj(event)
        seq = self._seq.get(run.id, 0)
        self._seq[run.id] = seq + 1
        ts = time.time()
        if seq < config.settings.run_max_events or event["type"] in ("status", "done", "cost"):
            self.db.add_run_event(run.id, seq, ts, event)
        elif run.id not in self._dropped:
            self._dropped.add(run.id)
            self.db.add_run_event(run.id, seq, ts, {"type": "log", "kind": "system",
                                                    "text": "Log limit reached; further output isn't stored."})
        for q in list(self._subscribers.get(run.id, ())):
            q.put_nowait({"seq": seq, "ts": ts, **event})

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._subscribers[run_id]

    def _sys(self, run: Run, text: str) -> None:
        self.emit(run, {"type": "log", "kind": "system", "text": text})

    def _status(self, run: Run, state: str, **fields: Any) -> None:
        self.db.update_run(run, state=state, **fields)
        self.emit(run, {"type": "status", "state": state})

    # ---- commands ------------------------------------------------------------

    def create(self, *, sandbox_id: str, prompt: str, runner: str, timeout: int | None,
               max_turns: int | None, ip: str) -> Run:
        """Validate and record a run. Call `launch(run)` from the event loop afterwards."""
        s = config.settings
        sb = self.sandboxes.get(sandbox_id)
        if sb.state != "running" or not sb.container_id:
            raise InvalidTransition("Start the sandbox first")
        agent = self.runners.get(runner)
        if agent is None:
            raise SandboxError(f"Unknown runner {runner!r}")
        if problem := agent.problem():
            raise SandboxError(problem)
        host = self.sandboxes.host_state()
        if host.mem_available < s.memory_reserve:
            reason = (f"Only {host.mem_available / 2**30:.1f} GB of memory is available; runs need "
                      f"{s.memory_reserve / 2**30:.0f} GB left free for the rest of the server.")
            self.db.audit(ip, "run.start", sandbox_id, ok=False, detail={"blocked": [reason]})
            raise CapacityError([reason])
        with self._lock:
            if active := self.active_for(sandbox_id):
                raise InvalidTransition(f"Run {active.id} is still {active.state} in this sandbox")
            rid = secrets.token_hex(6)
            run = Run(id=rid, sandbox_id=sandbox_id, runner=runner, prompt=prompt,
                      timeout=min(timeout or s.run_timeout_default, s.run_timeout_max),
                      max_turns=min(max_turns or s.run_max_turns_default, s.run_max_turns_max),
                      branch=f"agent/{rid}")
            self.db.insert_run(run)
        self.db.audit(ip, "run.start", sandbox_id, detail={"run": rid, "runner": runner,
                                                           "prompt": prompt[:200]})
        self.emit(run, {"type": "status", "state": "queued"})
        return run

    def launch(self, run: Run) -> asyncio.Task:
        task = asyncio.create_task(self._execute(run))
        self._tasks[run.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run.id, None))
        return task

    async def cancel(self, run_id: str, ip: str, reason: str = "cancelled") -> Run:
        run = self.get(run_id)
        if not run.active:
            return run
        self._cancel_reason.setdefault(run_id, reason)
        self.db.audit(ip, "run.cancel", run.sandbox_id, detail={"run": run_id, "reason": reason})
        if proc := self._procs.get(run_id):
            await proc.cancel()
        return self.get(run_id)

    async def cancel_for_sandbox(self, sandbox_id: str, ip: str, wait: float = 30) -> None:
        """Before a sandbox is stopped or destroyed: end its run cleanly."""
        run = self.active_for(sandbox_id)
        if run is None:
            return
        await self.cancel(run.id, ip, reason="cancelled")
        if task := self._tasks.get(run.id):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait)
            except TimeoutError:
                pass

    def reconcile_on_startup(self) -> None:
        """Runs that were active when Agent Hub stopped can't be followed any more: stop them."""
        from .runners import kill_argv

        for run in self.db.list_runs(states=ACTIVE_RUN_STATES, limit=1000):
            sb = self.db.get_sandbox(run.sandbox_id)
            if sb and sb.container_id and sb.state == "running":
                try:
                    self.sandboxes.backend.exec(sb.container_id, kill_argv(run.id), 30)
                except Exception:  # noqa: BLE001
                    pass
            self.db.update_run(run, state="failed", error="Agent Hub restarted during the run",
                               finished_at=time.time())

    # ---- the run itself --------------------------------------------------------

    async def _execute(self, run: Run) -> None:
        sb = self.sandboxes.get(run.sandbox_id)
        spec = RunSpec(run_id=run.id, container_id=sb.container_id or "", prompt=run.prompt,
                       timeout=run.timeout, max_turns=run.max_turns, branch=run.branch or "")
        state, error, outcome = "failed", None, None
        self._status(run, "running", started_at=time.time())
        try:
            if run.id in self._cancel_reason:
                raise asyncio.CancelledError
            base = await asyncio.to_thread(self.workspace.prepare, sb, run, lambda t: self._sys(run, t))
            self.db.update_run(run, base_branch=base)
            if not sb.base_branch:
                self.db.update_sandbox(sb, base_branch=base)

            proc = await self.runners[run.runner].start(spec)
            self._procs[run.id] = proc
            if run.id in self._cancel_reason:
                await proc.cancel()
            try:
                async with asyncio.timeout(run.timeout):
                    async for event in proc.events():
                        self.emit(run, event)
            except TimeoutError:
                self._cancel_reason.setdefault(run.id, "timed_out")
                self._sys(run, f"Time limit reached ({run.timeout // 60} min); stopping the agent")
                await proc.cancel()
            outcome = proc.outcome
            reason = self._cancel_reason.get(run.id)
            if reason:
                state = "timed_out" if reason == "timed_out" else "cancelled"
            elif outcome.exit_code == 0 and outcome.is_error is not True:
                state = "succeeded"
            elif outcome.exit_code == 127 and outcome.is_error is None:
                error = ("The agent CLI isn't installed in this sandbox (it was created from an older "
                         "image). Create a new sandbox.")
            else:
                error = outcome.error or (f"Agent exited with code {outcome.exit_code}"
                                          if outcome.exit_code is not None else "Agent stopped unexpectedly")
        except asyncio.CancelledError:
            state = "cancelled"
        except (WorkspaceError, SandboxError) as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001  (keep the run record honest whatever happened)
            log.exception("run %s crashed", run.id)
            error = f"Internal error: {e}"
        finally:
            self._procs.pop(run.id, None)

        commit = None
        pushed = False
        if run.base_branch:  # the branch exists: keep whatever the agent produced
            try:
                fin = await asyncio.to_thread(self.workspace.finalize, sb, run, lambda t: self._sys(run, t))
                commit = fin.commit_sha if fin.commits else None
                if not fin.commits:
                    self._sys(run, "No changes were made")
                elif state == "succeeded":
                    pushed = await asyncio.to_thread(self.workspace.push, sb, run, lambda t: self._sys(run, t))
            except (WorkspaceError, SandboxError) as e:
                self._sys(run, f"Couldn't save the agent's work: {e}")
                if state == "succeeded":
                    state, error = "failed", str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("finalize failed for run %s", run.id)
                self._sys(run, f"Couldn't save the agent's work: {e}")

        if error:
            error = Redactor.from_settings().text(error)[:1000]
        self.db.update_run(run, state=state, error=error, finished_at=time.time(), commit_sha=commit,
                           pushed=pushed, exit_code=outcome.exit_code if outcome else None,
                           cost_usd=outcome.cost_usd if outcome else None,
                           num_turns=outcome.num_turns if outcome else None)
        self.emit(run, {"type": "done", "state": state, "error": error, "commit": commit,
                        "branch": run.branch, "pushed": pushed})
        self.db.audit("system", "run.finish", run.sandbox_id, ok=state == "succeeded",
                      detail={"run": run.id, "state": state, "commit": commit, "pushed": pushed,
                              "cost_usd": run.cost_usd})
        self._cancel_reason.pop(run.id, None)
        self._seq.pop(run.id, None)
        self._dropped.discard(run.id)
