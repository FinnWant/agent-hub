"""Tasks queued per sandbox: the next one starts when the current run ends.

A task becomes an ordinary run (RunManager.create), fresh from the base branch or continuing the
run before it. The queue pauses itself, with a note saying why, when a run doesn't succeed (or
its checks fail) or the next task can't start, so a bad run doesn't set off a chain of them; the
owner resumes it.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from typing import TYPE_CHECKING, Any

from .db import Run
from .sandboxes import InvalidTransition, NotFound, SandboxError

if TYPE_CHECKING:
    from .runs import RunManager

MAX_QUEUED = 20

ENDED = {"failed": "failed", "cancelled": "was cancelled", "timed_out": "hit its time limit"}


class TaskQueue:
    def __init__(self, manager: RunManager) -> None:
        self.m = manager
        self._lock = threading.Lock()

    @property
    def db(self):
        return self.m.db

    def view(self, sandbox_id: str) -> dict[str, Any]:
        sb = self.m.sandboxes.get(sandbox_id)
        return {"paused": sb.queue_paused, "note": sb.queue_note, "items": self.db.queued_tasks(sandbox_id)}

    def add(self, sandbox_id: str, *, prompt: str, runner: str, timeout: int | None, max_turns: int | None,
            continue_previous: bool, ip: str) -> dict[str, Any]:
        self.m.sandboxes.get(sandbox_id)
        if runner not in self.m.runners:
            raise SandboxError(f"Unknown runner {runner!r}")
        if len(self.db.queued_tasks(sandbox_id)) >= MAX_QUEUED:
            raise InvalidTransition(f"The queue holds at most {MAX_QUEUED} tasks")
        tid = secrets.token_hex(6)
        self.db.insert_queued_task({"id": tid, "sandbox_id": sandbox_id, "prompt": prompt, "runner": runner,
                                    "timeout": timeout, "max_turns": max_turns,
                                    "continue_previous": int(continue_previous)})
        self.db.audit(ip, "queue.add", sandbox_id, detail={"task": tid, "prompt": prompt[:200]})
        return self.view(sandbox_id)

    def remove(self, sandbox_id: str, task_id: str, ip: str) -> dict[str, Any]:
        if not self.db.delete_queued_task(task_id, sandbox_id):
            raise NotFound("No such task in this sandbox's queue")
        self.db.audit(ip, "queue.remove", sandbox_id, detail={"task": task_id})
        return self.view(sandbox_id)

    def move(self, sandbox_id: str, task_id: str, offset: int) -> dict[str, Any]:
        items = self.db.queued_tasks(sandbox_id)
        i = next((n for n, t in enumerate(items) if t["id"] == task_id), None)
        if i is None:
            raise NotFound("No such task in this sandbox's queue")
        j = max(0, min(len(items) - 1, i + offset))
        items.insert(j, items.pop(i))
        for n, t in enumerate(items):
            if t["position"] != n + 1:
                self.db.set_queued_position(t["id"], n + 1)
        return self.view(sandbox_id)

    def set_paused(self, sandbox_id: str, paused: bool, ip: str) -> None:
        sb = self.m.sandboxes.get(sandbox_id)
        self.db.update_sandbox(sb, queue_paused=paused, queue_note=None)
        self.db.audit(ip, "queue.pause" if paused else "queue.resume", sandbox_id)

    def _pause(self, sandbox_id: str, note: str) -> None:
        sb = self.db.get_sandbox(sandbox_id)
        if sb:
            self.db.update_sandbox(sb, queue_paused=True, queue_note=note[:500])
            self.db.audit("system", "queue.pause", sandbox_id, detail={"note": note[:200]})

    async def after_run(self, run: Run) -> None:
        """A run ended: start the next task, or pause the queue if the run went wrong."""
        if not self.db.queued_tasks(run.sandbox_id):
            return
        if run.state != "succeeded":
            self._pause(run.sandbox_id, f"Paused: run {run.id} {ENDED.get(run.state, run.state)}")
            return
        if run.verify == "failed":
            self._pause(run.sandbox_id, f"Paused: run {run.id} finished, but its checks fail")
            return
        await self.kick(run.sandbox_id)

    async def kick(self, sandbox_id: str) -> Run | None:
        """Start the next queued task if the sandbox is free. Call from the event loop."""
        run = await asyncio.to_thread(self._take, sandbox_id)
        if run is not None:
            self.m.launch(run)
        return run

    def _take(self, sandbox_id: str) -> Run | None:
        with self._lock:
            sb = self.db.get_sandbox(sandbox_id)
            if sb is None or sb.queue_paused or sb.state != "running" or self.m.active_for(sandbox_id):
                return None
            items = self.db.queued_tasks(sandbox_id)
            if not items:
                return None
            task = items[0]
            parent = None
            if task["continue_previous"]:
                last = self.db.list_runs(sandbox_id=sandbox_id, limit=1)
                parent = last[0] if last else None
                if parent is None:
                    self._pause(sandbox_id, "Paused: the next task continues the previous run, but there is none")
                    return None
            try:
                run = self.m.create(sandbox_id=sandbox_id, prompt=task["prompt"], runner=task["runner"],
                                    timeout=task["timeout"], max_turns=task["max_turns"], ip="queue",
                                    continue_from=parent.id if parent else None)
            except SandboxError as e:
                reasons = getattr(e, "reasons", None)
                self._pause(sandbox_id, f"Paused: couldn't start the next task: {' '.join(reasons) if reasons else e}")
                return None
            self.db.delete_queued_task(task["id"])
            self.m._sys(run, "Started from the queue")
            return run

    async def kick_all(self) -> None:
        """At startup: start queued work in sandboxes that have no run going."""
        for sb in self.db.list_sandboxes():
            if not sb.queue_paused and self.db.queued_tasks(sb.id):
                await self.kick(sb.id)
