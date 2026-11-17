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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .docker_backend import BackendError
from .hooks import HookCall, HookReply
from .loops import EDITS, LoopWatch
from .runners import MAX_MESSAGE, AgentRunner, RunError, RunProcess, RunSpec
from .templates import TEMPLATES
from .sandboxes import CapacityError, InvalidTransition, NotFound, SandboxError, SandboxService
from .workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)

FINAL_STATES = ("succeeded", "failed", "cancelled", "timed_out")


@dataclass
class HookHandler:
    """Reacts to a Claude Code hook (see hooks.py). `wait` > 0: the hook waits up to that many
    seconds for the handlers' reply (a deny / exit 2 from any handler wins); 0: fire and forget.
    `fn(manager, run, call)` gets the hook's input in `call.payload`."""

    event: str
    fn: Callable[[RunManager, Run, HookCall], Awaitable[HookReply | None]]
    wait: int = 0
    matcher: str | None = None
    applies: Callable[[Run], bool] | None = None  # None: every run; else installed only where True
    matcher_for: Callable[[Run], str] | None = None  # a matcher that depends on the run
    installed: bool = True  # False: not a Claude Code hook, but a Kiln command using the channel

    def for_run(self, run: Run) -> bool:
        return self.applies is None or bool(self.applies(run))

    def matcher_of(self, run: Run) -> str | None:
        return self.matcher_for(run) if self.matcher_for else self.matcher


async def mark_compaction(manager: RunManager, run: Run, call: HookCall) -> None:
    trigger = call.payload.get("trigger")
    how = "automatically, its context was full" if trigger == "auto" else "on request"
    manager._sys(run, f"The agent's context is being compacted ({how}); it continues from a summary")


def overflow_path(run_id: str):
    from pathlib import Path

    return Path(config.settings.db_path).parent / "run-logs" / f"{run_id}.jsonl.gz"


def overflow(run_id: str, event: dict[str, Any]) -> None:
    """Events past RUN_MAX_EVENTS: appended to a gzip file next to the database (the full log)."""
    import gzip

    path = overflow_path(run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "at") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        log.warning("couldn't keep the overflow log of run %s", run_id, exc_info=True)


def outcome(run: Run) -> str:
    """How a finished run ended, in words for the agent continuing it."""
    if run.state == "succeeded":
        how = "finished"
    elif run.state == "timed_out":
        how = f"was stopped at its time limit ({run.timeout // 60} min) before finishing"
    elif run.state == "cancelled":
        how = "was cancelled by the owner before finishing"
    else:
        how = "failed" + (f" ({run.error.splitlines()[0][:200]})" if run.error else "")
    if run.verify == "failed":
        how += f", and its checks (`{run.verify_cmd}`) fail"
    elif run.verify == "passed":
        how += ", and its checks pass"
    return how


def default_hooks() -> list[HookHandler]:
    from .owner import owner_hooks
    from .verify import stop_handler

    from .ledger import ledger_hook

    return [HookHandler("PreCompact", mark_compaction), stop_handler(), *owner_hooks(), ledger_hook()]


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
        self._hook_tasks: set[asyncio.Task] = set()
        self.hooks: list[HookHandler] = default_hooks()
        self.verify_attempts: dict[str, int] = {}  # run id -> times sent back by failing checks
        self.verify_gave_up: set[str] = set()
        from .owner import Owner

        self.owner = Owner(self)
        self._timeouts: dict[str, asyncio.Timeout] = {}
        self._paused: dict[str, tuple[int, float]] = {}  # run id -> (open owner requests, since)
        self._budget_warned: set[str] = set()
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
        seq = self._seq.get(run.id)
        if seq is None:  # e.g. a rewind after the run ended: carry on from the stored log
            seq = self.db.last_run_seq(run.id) + 1
        self._seq[run.id] = seq + 1
        ts = time.time()
        if seq < config.settings.run_max_events or event["type"] in ("status", "done", "cost"):
            self.db.add_run_event(run.id, seq, ts, event, runner_state)
        else:
            if run.id not in self._dropped:
                self._dropped.add(run.id)
                self.db.add_run_event(run.id, seq, ts, {"type": "log", "kind": "system",
                                                        "text": "Log limit reached; the rest is kept in the full "
                                                                "log (download it from the run)."}, runner_state)
            elif runner_state is not None:
                self.db.add_run_event(run.id, seq, ts, None, runner_state)
            overflow(run.id, {"seq": seq, "ts": ts, **event})
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
               max_turns: int | None, ip: str, continue_from: str | None = None) -> Run:
        """Validate and record a run. Call `launch(run)` from the event loop afterwards.
        `continue_from`: a finished run of this sandbox whose branch (and agent session) the new
        run picks up."""
        s = config.settings
        if continue_from is not None:
            parent = self.db.get_run(continue_from)
            if parent is None or parent.sandbox_id != sandbox_id:
                raise NotFound("No such run in this sandbox to continue")
            if parent.active:
                raise InvalidTransition("That run is still going; send it a message instead")
            if not parent.base_branch or not parent.branch:
                raise InvalidTransition("That run never got as far as its branch, so there's nothing to continue")
            if parent.runner != runner:
                raise InvalidTransition("A run can only be continued by the same agent")
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
                      branch=f"agent/{rid}", verify_cmd=sb.verify_cmd or None, parent_run_id=continue_from,
                      budget_usd=sb.budget_usd or None, approval_rules=sb.approval_rules or None)
            self.db.insert_run(run)
        self.db.audit(ip, "run.start", sandbox_id, detail={"run": rid, "runner": runner, "prompt": prompt[:200],
                                                           **({"continues": continue_from} if continue_from else {})})
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

    def _spec(self, run: Run, sb) -> RunSpec:
        hooks: dict[str, int] = {}
        matchers: dict[str, set[str | None]] = {}
        for h in self.hooks:
            if not h.installed or not h.for_run(run):
                continue
            hooks[h.event] = max(hooks.get(h.event, 0), h.wait)
            matchers.setdefault(h.event, set()).add(h.matcher_of(run))
        return RunSpec(run_id=run.id, container_id=sb.container_id or "", prompt=run.prompt,
                       timeout=run.timeout, max_turns=run.max_turns, branch=run.branch or "",
                       preview_port=TEMPLATES[sb.template].preview_port, sandbox_id=sb.id, hooks=hooks,
                       # one matcher only if every handler of the event agrees; else all calls
                       hook_matchers={e: next(iter(m)) for e, m in matchers.items() if len(m) == 1 and None not in m})

    async def _ledger(self, sb, run: Run) -> None:
        if run.runner == "claude-code":
            from .ledger import refresh_quietly

            await refresh_quietly(self, self.sandboxes.get(sb.id), run)

    # ---- checkpoints, loops, report --------------------------------------------

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._hook_tasks.add(task)
        task.add_done_callback(self._hook_tasks.discard)

    async def _checkpoint_loop(self, run: Run, sb, poke: asyncio.Event) -> None:
        s = config.settings
        while True:
            try:
                await asyncio.wait_for(poke.wait(), s.checkpoint_interval)
            except TimeoutError:
                pass
            if poke.is_set():
                poke.clear()
                await asyncio.sleep(s.checkpoint_debounce)  # let a burst of edits finish
            await self._checkpoint(run, sb)

    async def _checkpoint(self, run: Run, sb) -> None:
        if not hasattr(self.workspace, "checkpoint") or not run.base_branch:
            return
        n = len(self.db.checkpoints(run.id)) + 1
        try:
            res = await asyncio.to_thread(self.workspace.checkpoint, sb, run, n)
        except Exception:  # noqa: BLE001  (e.g. the agent holds a git lock; next time then)
            log.info("no checkpoint for run %s this time", run.id, exc_info=True)
            return
        if res is None:
            return
        sha, files = res
        self.db.add_checkpoint(run.id, n, sha, self._seq.get(run.id, 1) - 1, files)
        self.emit(run, {"type": "log", "kind": "checkpoint", "n": n, "sha": sha, "files": files[:20],
                        "text": f"Checkpoint {n}: {len(files)} file{'' if len(files) == 1 else 's'} changed"})

    async def rewind(self, run_id: str, n: int, ip: str) -> Run:
        """Put a finished run's branch back to one of its checkpoints."""
        run = self.get(run_id)
        if run.active:
            raise InvalidTransition("Stop the run before rewinding it")
        if active := self.active_for(run.sandbox_id):
            raise InvalidTransition(f"Run {active.id} is going in this sandbox")
        cp = next((c for c in self.db.checkpoints(run_id) if c["n"] == n), None)
        if cp is None:
            raise NotFound("No such checkpoint")
        sb = self.sandboxes.get(run.sandbox_id)
        if sb.state != "running":
            raise InvalidTransition("Start the sandbox first")
        await asyncio.to_thread(self.workspace.rewind, sb, run, cp["sha"])
        self.db.update_run(run, rewound_to=n, commit_sha=cp["sha"])
        self.db.audit(ip, "run.rewind", run.sandbox_id, detail={"run": run_id, "checkpoint": n})
        self.emit(run, {"type": "log", "kind": "system",
                        "text": f"Rewound {run.branch} to checkpoint {n}; continue the run to carry on from there"})
        return run

    async def _loop_tripped(self, run: Run, proc: RunProcess, watch: LoopWatch, why: str) -> None:
        watch.trips += 1
        self.emit(run, {"type": "log", "kind": "loop", "text": f"The agent {why}"})
        if watch.trips == 1 and getattr(proc, "can_send", False):
            try:
                await proc.send(f"[Kiln] You {why}. Stop and step back: work out why it keeps failing before trying "
                                "again, and take a different approach. If the sandbox itself is in the way (network "
                                "rules, no root), work around it or say so in your summary.")
                return
            except Exception:  # noqa: BLE001
                pass
        gw, digest = self._gateway_for(run, proc)
        if gw is not None:
            await asyncio.to_thread(gw.hold, digest, True)
        self.owner.create(run, "stuck", "The agent seems stuck",
                          f"It {why}" + (", even after being told to step back" if watch.trips > 1 else "") +
                          ". Its API calls are on hold. Let it continue (optionally with advice, which is sent to "
                          "it), or stop the run.", ["Continue", "Stop"], config.settings.budget_wait)

    async def stuck_decided(self, run_id: str, ans: dict[str, Any] | None) -> None:
        run = self.db.get_run(run_id)
        if run is None or not run.active:
            return
        proc = self._procs.get(run_id)
        if ans and ans.get("choice") == "continue":
            gw, digest = self._gateway_for(run, proc)
            if gw is not None:
                await asyncio.to_thread(gw.hold, digest, False)
            if ans.get("text") and proc is not None and getattr(proc, "can_send", False):
                await proc.send(f"[Owner] {ans['text']}")
            return
        self._sys(run, "Stopped: the agent was stuck" + ("" if ans else " and nobody answered"))
        await self.cancel(run_id, "owner" if ans else "system", reason="cancelled")

    async def _report(self, sb, run: Run) -> None:
        from .report import build

        stats = None
        if run.base_branch and hasattr(self.workspace, "stats"):
            try:
                stats = await asyncio.to_thread(self.workspace.stats, sb, run)
            except Exception:  # noqa: BLE001
                log.info("no diff stats for run %s", run.id, exc_info=True)
        fresh = self.db.get_run(run.id) or run
        report = build(fresh, self._last_result(run.id), stats, len(self.db.checkpoints(run.id)),
                       self.db.list_requests(run_id=run.id, limit=200), self.db.run_events(run.id))
        self.db.update_run(run, report=json.dumps(report))

    # ---- waiting on the owner -------------------------------------------------

    def pause_clock(self, run: Run, delta: int) -> None:
        """Owner requests open (+1) and close (-1). While any is open the run's time limit stops."""
        count, since = self._paused.get(run.id, (0, 0.0))
        now = time.monotonic()
        if count == 0 and delta > 0:
            since = now
        count = max(0, count + delta)
        if count == 0 and delta < 0 and since:
            waited = now - since
            fresh = self.db.get_run(run.id)
            total = (fresh.paused_seconds if fresh else 0) + waited
            self.db.update_run(run, paused_seconds=total)
            if clock := self._timeouts.get(run.id):
                if (when := clock.when()) is not None:
                    clock.reschedule(when + waited)
            since = 0.0
        self._paused[run.id] = (count, since)

    # ---- usage and budget -----------------------------------------------------

    def _gateway_for(self, run: Run, proc: RunProcess | None):
        gw = getattr(self.runners.get(run.runner), "gateway", None)
        state = proc.state() if proc is not None and hasattr(proc, "state") else {}
        digest = state.get("key_digest")
        if gw is None or not digest or not getattr(self.runners.get(run.runner), "via_gateway", False):
            return None, None
        return gw, digest

    async def _meter_loop(self, run: Run, proc: RunProcess) -> None:
        while True:
            await asyncio.sleep(config.settings.meter_interval)
            await self._meter_once(run, proc)

    async def _meter_once(self, run: Run, proc: RunProcess | None) -> None:
        from .usage import RunUsage

        gw, digest = self._gateway_for(run, proc)
        if gw is None:
            return
        fresh = self.db.get_run(run.id)
        u = RunUsage.load(fresh.usage if fresh else None)
        try:
            records, u.offset = await asyncio.to_thread(gw.read_usage, digest, u.offset)
        except Exception:  # noqa: BLE001  (the gateway may be restarting)
            log.warning("couldn't read the usage of run %s", run.id, exc_info=True)
            return
        if not records:
            return
        for rec in records:
            u.add(rec)
        self.db.update_run(run, usage=u.dump(), cost_est=round(u.cost, 4))
        await self._check_budget(run, u.cost, gw, digest)

    async def _check_budget(self, run: Run, cost: float, gw, digest: str) -> None:
        fresh = self.db.get_run(run.id)
        budget = fresh.budget_usd if fresh else None
        if not budget or fresh is None or not fresh.active:
            return
        if cost >= 0.8 * budget and run.id not in self._budget_warned and self.owner.notify:
            self._budget_warned.add(run.id)
            sb = self.sandboxes.get(run.sandbox_id)
            self._sys(run, f"The run has used about ${cost:.2f} of its ${budget:.2f} budget")
            self.owner.notify({"kind": "run.budget", "title": f"{sb.name}: run at 80% of its budget",
                               "body": f"About ${cost:.2f} of ${budget:.2f}", "sandbox": run.sandbox_id,
                               "run": run.id, "url": f"/sandboxes/{run.sandbox_id}/runs?run={run.id}"})
        open_budget = [r for r in self.db.list_requests(state="pending", run_id=run.id) if r["kind"] == "budget"]
        if cost >= budget and not open_budget:
            await asyncio.to_thread(gw.hold, digest, True)
            self._sys(run, f"Over its ${budget:.2f} budget (about ${cost:.2f} used): the agent is paused")
            self.owner.create(run, "budget", f"Over its ${budget:.2f} budget",
                              f"The run has used about ${cost:.2f} (estimated at API prices) of its ${budget:.2f} "
                              "budget. Its API calls are on hold. Extend the budget to let it carry on, or stop it.",
                              ["Extend", "Stop"], config.settings.budget_wait, data={"spent": round(cost, 4),
                                                                                       "budget": budget})

    async def budget_decided(self, run_id: str, ans: dict[str, Any] | None) -> None:
        run = self.db.get_run(run_id)
        if run is None or not run.active:
            return
        gw, digest = self._gateway_for(run, self._procs.get(run_id))
        if ans and ans.get("choice") == "extend":
            self.db.update_run(run, budget_usd=(run.budget_usd or 0) + float(ans["amount"]))
            self._budget_warned.discard(run_id)
            if gw is not None:
                await asyncio.to_thread(gw.hold, digest, False)
            self._sys(run, f"Budget raised to ${run.budget_usd:.2f}; the agent carries on")
            return
        self._sys(run, "Stopped at its budget" + ("" if ans else " (no answer in time)"))
        await self.cancel(run_id, "owner" if ans else "system", reason="cancelled")

    # ---- continuing a run ------------------------------------------------------

    def _continue(self, spec: RunSpec, run: Run, parent: Run) -> None:
        """The new run's first message: how the previous run ended, then the new request. If the
        agent's session can't be resumed, also what the previous run was asked and said."""
        note = (f"[Kiln] This continues an earlier run in this sandbox. That run {outcome(parent)}. "
                f"Its work is committed on this branch, which starts where it stopped.")
        spec.prompt = f"{note}\n\n{run.prompt}"
        spec.resume_session = parent.session_id
        summary = self._last_result(parent.id) or "(it gave no summary)"
        spec.fallback_prompt = (f"{note} You don't have that conversation any more; here is what matters.\n\n"
                                f"It was asked:\n{parent.prompt[:4000]}\n\nIts final summary:\n{summary[:4000]}"
                                + ("\n\nIt continued an even earlier run; look at the git log for the rest."
                                   if parent.parent_run_id else "")
                                + f"\n\nNow:\n{run.prompt}")

    def _last_result(self, run_id: str) -> str | None:
        texts = [e.get("text") for e in self.db.run_events(run_id) if e.get("kind") == "result"]
        return texts[-1] if texts else None

    # ---- hooks ---------------------------------------------------------------

    def _on_hook(self, run: Run, proc: RunProcess, event: dict[str, Any]) -> None:
        call = HookCall(event=event["event"], id=event["id"])
        task = asyncio.create_task(self._handle_hook(run, proc, call))
        self._hook_tasks.add(task)  # handled alongside the log: a waiting handler holds nothing up
        task.add_done_callback(self._hook_tasks.discard)

    async def _handle_hook(self, run: Run, proc: RunProcess, call: HookCall) -> None:
        handlers = [h for h in self.hooks if h.event == call.event and h.for_run(run)]
        waits = any(h.wait > 0 for h in handlers)
        replies: list[HookReply] = []
        try:
            if handlers and hasattr(proc, "hook_payload"):
                call.payload = await proc.hook_payload(call.id)
            for h in handlers:
                try:
                    if (r := await h.fn(self, run, call)) is not None:
                        replies.append(r)
                except Exception:  # noqa: BLE001  (one handler never breaks the run)
                    log.exception("hook handler for %s failed in run %s", call.event, run.id)
            if waits and hasattr(proc, "reply_hook"):
                blocking = [r for r in replies if r.code == 2]
                denied = [r for r in replies if (r.stdout or {}).get("hookSpecificOutput", {})
                          .get("permissionDecision") == "deny"]
                reply = (blocking or denied or replies or [HookReply()])[0]
                await proc.reply_hook(call.id, reply)
        except Exception:  # noqa: BLE001  (e.g. the sandbox stopped meanwhile)
            log.warning("couldn't handle the %s hook of run %s", call.event, run.id, exc_info=True)

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
            limit = max(1.0, run.timeout + (run.paused_seconds or 0) - (time.time() - (run.started_at or time.time())))
        proc: RunProcess | None = None
        meter: asyncio.Task | None = None
        checkpoints: asyncio.Task | None = None
        try:
            if run.id in self._cancel_reason:
                raise asyncio.CancelledError
            if resume is None:
                parent = self.db.get_run(run.parent_run_id) if run.parent_run_id else None
                base = await asyncio.to_thread(self.workspace.prepare, sb, run, lambda t: self._sys(run, t),
                                               parent.branch if parent else None)
                self.db.update_run(run, base_branch=base)
                if not sb.base_branch:
                    self.db.update_sandbox(sb, base_branch=base)
                await self._ledger(sb, run)
                if parent:
                    self._continue(spec, run, parent)
                proc = await runner.start(spec)
                if parent:
                    resumed = getattr(proc, "outcome", None) and proc.outcome.extra.get("resumed")
                    self._sys(run, f"Continuing run {parent.id} in the same agent session" if resumed else
                              f"Continuing run {parent.id}; its agent session isn't available, so the agent "
                              "gets a summary of it instead")
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
            meter = asyncio.create_task(self._meter_loop(run, proc))
            poke = asyncio.Event()
            checkpoints = asyncio.create_task(self._checkpoint_loop(run, sb, poke))
            watch = LoopWatch()
            try:
                async with asyncio.timeout(limit) as clock:
                    self._timeouts[run.id] = clock
                    async for event in proc.events():
                        progress = event.pop("_progress", False)
                        if event.get("type") == "hook":
                            self._on_hook(run, proc, event)
                            if progress:
                                self.db.add_run_event(run.id, 0, 0, None, self._progress(run, proc))
                            continue
                        self.emit(run, event, self._progress(run, proc) if progress else None)
                        if event.get("kind") == "tool":
                            watch.tool(event.get("name", ""), event.get("text", ""))
                            if event.get("name") in EDITS:
                                poke.set()
                        elif event.get("kind") == "tool_result" and (why := watch.result(bool(event.get("is_error")))):
                            self._background(self._loop_tripped(run, proc, watch, why))
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
            self._timeouts.pop(run.id, None)
            if checkpoints is not None:
                checkpoints.cancel()
            if meter is not None:
                meter.cancel()
                await self._meter_once(run, proc)  # the last calls, before the key goes
            self._procs.pop(run.id, None)
            self.owner.close_for_run(run.id)
            self._paused.pop(run.id, None)
            self._budget_warned.discard(run.id)
            release = getattr(self.runners.get(run.runner), "release", None)
            if release:
                try:
                    await release(run.id, sb.container_id)
                except Exception:  # noqa: BLE001
                    log.exception("releasing run %s failed", run.id)

        if state == "succeeded" and run.verify_cmd and run.base_branch:
            from .verify import run_check

            self._sys(run, f"Running the checks: {run.verify_cmd}")
            check = await run_check(self, run)
            self.emit(run, check.event("final"))
            self.db.update_run(run, verify="passed" if check.passed else "failed")

        commit = None
        pushed = False
        if run.base_branch:  # the branch exists: keep whatever the agent produced
            try:
                fin = await asyncio.to_thread(self.workspace.finalize, sb, run, lambda t: self._sys(run, t))
                commit = fin.commit_sha if fin.commits else None
                if not fin.commits:
                    self._sys(run, "No changes were made")
                elif state == "succeeded" and run.verify == "failed":
                    if sb.allow_push:
                        self._sys(run, "Not pushed: the checks fail")
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
        if run.base_branch:
            await self._ledger(sb, run)
        await self._report(sb, run)
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
        self.verify_attempts.pop(run.id, None)
        self.verify_gave_up.discard(run.id)
