"""Agent runs: lifecycle, limits, event log and live fan-out.

    queued -> running -> succeeded | failed | cancelled | timed_out

One run per sandbox at a time. Every event is redacted, stored (up to RUN_MAX_EVENTS) and
published to WebSocket subscribers. Whatever the outcome, the agent's work is committed to
`agent/<run-id>` so it can be reviewed; only a successful run is pushed (if allowed).

Agents run detached in their sandbox (see runners.py), so a run survives a Kiln restart: the
runner state stored with the events says where to pick the output up again, and the owner can
send messages to a running agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from typing import Any

from . import config
from .db import ACTIVE_RUN_STATES, Database, Run
from .redact import Redactor
from .docker_backend import BackendError
from .runners import MAX_MESSAGE, AgentRunner, RunError, RunProcess, RunSpec
from .templates import TEMPLATES
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
        self.on_finished = None  # callable(run, sandbox_name), e.g. Notifier.run_finished

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

    def emit(self, run: Run, event: dict[str, Any], runner_state: dict[str, Any] | None = None) -> None:
        """Store and publish an event; `runner_state` (if given) is stored with it atomically."""
        event = Redactor.from_settings().obj(event)
        seq = self._seq.get(run.id, 0)
        self._seq[run.id] = seq + 1
        ts = time.time()
        if seq < config.settings.run_max_events or event["type"] in ("status", "done", "cost"):
            self.db.add_run_event(run.id, seq, ts, event, runner_state)
        elif run.id not in self._dropped:
            self._dropped.add(run.id)
            self.db.add_run_event(run.id, seq, ts, {"type": "log", "kind": "system",
                                                    "text": "Log limit reached; further output isn't stored."},
                                  runner_state)
        elif runner_state is not None:
            self.db.add_run_event(run.id, seq, ts, None, runner_state)
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
        if host.free_disk < s.min_free_disk:
            reason = (f"Only {host.free_disk / 2**30:.0f} GB of disk is free; runs need at least "
                      f"{s.min_free_disk / 2**30:.0f} GB. Try Clean up on the Sandboxes page.")
            self.db.audit(ip, "run.start", sandbox_id, ok=False, detail={"blocked": [reason]})
            raise CapacityError([reason])
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

    def can_send(self, run_id: str) -> bool:
        proc = self._procs.get(run_id)
        return bool(proc is not None and getattr(proc, "can_send", False) and run_id not in self._cancel_reason)

    async def send(self, run_id: str, text: str, ip: str) -> None:
        """Deliver a message from the owner to a running agent."""
        run = self.get(run_id)
        text = text.strip()
        if not text or len(text) > MAX_MESSAGE:
            raise RunError(f"A message must be 1 to {MAX_MESSAGE} characters")
        proc = self._procs.get(run_id)
        if not run.active or proc is None or run_id in self._cancel_reason:
            raise RunError("The run isn't running any more; start a new run instead")
        if not hasattr(proc, "send"):
            raise RunError("This agent can't take messages while it works")
        self.db.audit(ip, "run.message", run.sandbox_id, detail={"run": run_id, "text": text[:200]})
        try:
            await proc.send(text)
        except BackendError as e:
            raise RunError(f"Couldn't deliver the message: {e}") from e

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

    def reconcile_on_startup(self) -> list[tuple[Run, dict[str, Any]]]:
        """Runs that were active when Kiln stopped: those whose agent is still running (or has
        finished since) are returned with their runner state, to `resume()`. The rest can't be
        followed any more: stop them and mark them failed."""
        from .runners import kill_argv

        resumable: list[tuple[Run, dict[str, Any]]] = []
        for run in self.db.list_runs(states=ACTIVE_RUN_STATES, limit=1000):
            sb = self.db.get_sandbox(run.sandbox_id)
            alive = bool(sb and sb.container_id and sb.state == "running")
            if alive and (state := self._resumable(run, sb)) is not None:
                resumable.append((run, state))
                continue
            if alive:
                try:
                    self.sandboxes.backend.exec(sb.container_id, kill_argv(run.id), 30)
                except Exception:  # noqa: BLE001
                    pass
            self.db.update_run(run, state="failed", error="Kiln restarted during the run",
                               finished_at=time.time())
        return resumable

    def _resumable(self, run: Run, sb) -> dict[str, Any] | None:
        runner = self.runners.get(run.runner)
        if run.state != "running" or not run.base_branch or not run.runner_state:
            return None
        if not (hasattr(runner, "attach") and hasattr(runner, "status")):
            return None
        try:
            state = json.loads(run.runner_state)
            status = runner.status(self._spec(run, sb))
        except Exception:  # noqa: BLE001
            log.warning("can't check run %s after the restart", run.id, exc_info=True)
            return None
        return state if isinstance(state, dict) and status in ("ALIVE", "DONE") else None

    def resume(self, run: Run, state: dict[str, Any]) -> asyncio.Task:
        task = asyncio.create_task(self._execute(run, resume=state))
        self._tasks[run.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run.id, None))
        return task

    # ---- the run itself --------------------------------------------------------

    @staticmethod
    def _spec(run: Run, sb) -> RunSpec:
        return RunSpec(run_id=run.id, container_id=sb.container_id or "", prompt=run.prompt,
                       timeout=run.timeout, max_turns=run.max_turns, branch=run.branch or "",
                       preview_port=TEMPLATES[sb.template].preview_port, sandbox_id=sb.id)

    def _progress(self, run: Run, proc: RunProcess) -> dict[str, Any] | None:
        state = proc.state() if hasattr(proc, "state") else None
        if state and state.get("session_id") and state["session_id"] != run.session_id:
            self.db.update_run(run, session_id=str(state["session_id"])[:100])
        return state

    async def _execute(self, run: Run, resume: dict[str, Any] | None = None) -> None:
        sb = self.sandboxes.get(run.sandbox_id)
        spec = self._spec(run, sb)
        state, error, outcome = "failed", None, None
        runner = self.runners[run.runner]
        if resume is None:
            self._status(run, "running", started_at=time.time())
            limit: float = run.timeout
        else:  # Kiln restarted while the agent ran: pick up where the stored events end
            self._seq[run.id] = self.db.last_run_seq(run.id) + 1
            limit = max(1.0, run.timeout - (time.time() - (run.started_at or time.time())))
        try:
            if run.id in self._cancel_reason:
                raise asyncio.CancelledError
            if resume is None:
                base = await asyncio.to_thread(self.workspace.prepare, sb, run, lambda t: self._sys(run, t))
                self.db.update_run(run, base_branch=base)
                if not sb.base_branch:
                    self.db.update_sandbox(sb, base_branch=base)
                proc = await runner.start(spec)
            else:
                try:
                    proc = await runner.attach(spec, resume)
                except Exception:
                    from .runners import kill_argv  # don't leave an agent running unsupervised

                    await asyncio.to_thread(self.sandboxes.backend.exec, spec.container_id, kill_argv(run.id), 30)
                    raise
                self._sys(run, "Kiln restarted; following the agent again")
            self._procs[run.id] = proc
            if resume is None and hasattr(proc, "state"):  # where to re-attach from, before any output
                self.db.update_run(run, runner_state=json.dumps(self._progress(run, proc)))
            if run.id in self._cancel_reason:
                await proc.cancel()
            try:
                async with asyncio.timeout(limit):
                    async for event in proc.events():
                        progress = event.pop("_progress", False)
                        self.emit(run, event, self._progress(run, proc) if progress else None)
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
        except (WorkspaceError, SandboxError, BackendError) as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001  (keep the run record honest whatever happened)
            log.exception("run %s crashed", run.id)
            error = f"Internal error: {e}"
        finally:
            self._procs.pop(run.id, None)
            release = getattr(self.runners.get(run.runner), "release", None)
            if release:
                try:
                    await release(run.id, sb.container_id)
                except Exception:  # noqa: BLE001
                    log.exception("releasing run %s failed", run.id)

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
        if self.on_finished:
            try:
                self.on_finished(run, sb.name)
            except Exception:  # noqa: BLE001
                log.exception("run notification failed")
        self.db.audit("system", "run.finish", run.sandbox_id, ok=state == "succeeded",
                      detail={"run": run.id, "state": state, "commit": commit, "pushed": pushed,
                              "cost_usd": run.cost_usd})
        self._cancel_reason.pop(run.id, None)
        self._seq.pop(run.id, None)
        self._dropped.discard(run.id)
