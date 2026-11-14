"""Sandbox lifecycle: state machine, capacity checks, and auditing around the backend."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable

from . import config
from .capacity import HostState, start_blockers
from .db import Database, Sandbox
from .docker_backend import Backend, BackendError, ExecResult
from .templates import TEMPLATES

log = logging.getLogger(__name__)

# creating → running → stopped → destroyed, plus error. Destroyed is terminal.
TRANSITIONS: dict[str, set[str]] = {
    "creating": {"running", "error", "destroyed"},
    "running": {"stopped", "error", "destroyed"},
    "stopped": {"running", "error", "destroyed"},
    "error": {"running", "stopped", "destroyed"},
    "destroyed": set(),
}


class SandboxError(Exception):
    status_code = 400


class NotFound(SandboxError):
    status_code = 404


class InvalidTransition(SandboxError):
    status_code = 409


class CapacityError(SandboxError):
    status_code = 409

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(" ".join(reasons))
        self.reasons = reasons


class UnavailableError(SandboxError):
    status_code = 503


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"Can't go from {current} to {new}")


class SandboxService:
    def __init__(self, db: Database, backend: Backend,
                 host_state: Callable[[], HostState] = HostState.read) -> None:
        self.db = db
        self.backend = backend
        self.host_state = host_state
        # Serialises capacity check + start so two requests can't both squeeze past a cap.
        self._lock = threading.Lock()

    # ---- queries -----------------------------------------------------------

    def get(self, sandbox_id: str) -> Sandbox:
        sb = self.db.get_sandbox(sandbox_id)
        if sb is None or sb.state == "destroyed":
            raise NotFound("No such sandbox")
        return self.sync(sb)

    def list(self) -> list[Sandbox]:
        return [self.sync(sb) for sb in self.db.list_sandboxes()]

    def running(self) -> list[Sandbox]:
        return [sb for sb in self.db.list_sandboxes() if sb.state == "running"]

    def sync(self, sb: Sandbox) -> Sandbox:
        """Reconcile stored state with the container (it may have exited or been OOM-killed)."""
        if sb.state not in ("running", "stopped") or not sb.container_id:
            return sb
        try:
            status = self.backend.status(sb.container_id)
        except BackendError as e:
            log.warning("status check failed for %s: %s", sb.id, e)
            return sb
        if status == "running" and sb.state != "running":
            return self.db.update_sandbox(sb, state="running", error=None)
        if status in ("exited", "created", "dead") and sb.state == "running":
            return self.db.update_sandbox(sb, state="stopped", error="Container exited unexpectedly")
        if status == "missing":
            return self.db.update_sandbox(sb, state="error", error="Container no longer exists")
        return sb

    def blockers(self, memory: int, exclude: str | None = None) -> list[str]:
        running = [sb.memory for sb in self.running() if sb.id != exclude]
        return start_blockers(memory, running, self.host_state())

    # ---- commands ----------------------------------------------------------

    def create(self, *, name: str, template: str, repo_url: str | None, base_branch: str | None,
               memory: int | None, cpus: float | None, ip: str) -> Sandbox:
        s = config.settings
        tpl = TEMPLATES[template]
        memory = memory or s.default_sandbox_memory
        cpus = cpus or s.default_sandbox_cpus
        if memory > s.max_sandbox_memory:
            raise SandboxError(f"Memory is capped at {s.max_sandbox_memory // 2**20} MiB per sandbox")
        if cpus > s.max_sandbox_cpus:
            raise SandboxError(f"CPUs are capped at {s.max_sandbox_cpus:g} per sandbox")
        if not self.backend.image_exists(tpl.image):
            raise UnavailableError(f"Image {tpl.image} isn't built. Run deploy/install.sh.")

        with self._lock:
            reasons = self.blockers(memory)
            if reasons:
                self.db.audit(ip, "sandbox.create", None, ok=False, detail={"name": name, "blocked": reasons})
                raise CapacityError(reasons)
            sid = secrets.token_hex(6)
            sb = Sandbox(id=sid, name=name, template=template, volume=f"ah-{sid}", memory=memory,
                         cpus=cpus, repo_url=repo_url, base_branch=base_branch)
            self.db.insert_sandbox(sb)
            try:
                cid = self.backend.create(sb, tpl)
                self.db.update_sandbox(sb, container_id=cid)
                self.backend.start(cid)
                self.db.update_sandbox(sb, state="running")
            except Exception as e:  # leave a visible error row rather than half-created junk
                log.exception("create failed for %s", sid)
                self.db.update_sandbox(sb, state="error", error=str(e)[:500])
                self.db.audit(ip, "sandbox.create", sid, ok=False, detail={"name": name, "error": str(e)[:200]})
                return sb
        self.db.audit(ip, "sandbox.create", sid, detail={"name": name, "template": template,
                                                         "memory": memory, "cpus": cpus})
        return sb

    def start(self, sandbox_id: str, ip: str) -> Sandbox:
        sb = self.get(sandbox_id)
        if sb.state == "running":
            return sb
        check_transition(sb.state, "running")
        if not sb.container_id:
            raise InvalidTransition("Sandbox has no container; destroy it and create a new one")
        with self._lock:
            reasons = self.blockers(sb.memory, exclude=sb.id)
            if reasons:
                self.db.audit(ip, "sandbox.start", sb.id, ok=False, detail={"blocked": reasons})
                raise CapacityError(reasons)
            try:
                self.backend.start(sb.container_id)
            except BackendError as e:
                self.db.update_sandbox(sb, state="error", error=str(e)[:500])
                self.db.audit(ip, "sandbox.start", sb.id, ok=False, detail={"error": str(e)[:200]})
                raise SandboxError(str(e)) from e
            self.db.update_sandbox(sb, state="running", error=None)
        self.db.audit(ip, "sandbox.start", sb.id)
        return sb

    def stop(self, sandbox_id: str, ip: str) -> Sandbox:
        sb = self.get(sandbox_id)
        if sb.state == "stopped":
            return sb
        check_transition(sb.state, "stopped")
        if sb.container_id:
            self.backend.stop(sb.container_id)
        self.db.update_sandbox(sb, state="stopped", error=None)
        self.db.audit(ip, "sandbox.stop", sb.id)
        return sb

    def destroy(self, sandbox_id: str, ip: str) -> None:
        sb = self.get(sandbox_id)
        check_transition(sb.state, "destroyed")
        self.backend.remove(sb.container_id, sb.volume)
        self.db.update_sandbox(sb, state="destroyed", container_id=None)
        self.db.audit(ip, "sandbox.destroy", sb.id, detail={"name": sb.name})

    def exec(self, sandbox_id: str, cmd: str | list[str], timeout: int | None, ip: str) -> ExecResult:
        s = config.settings
        sb = self.get(sandbox_id)
        if sb.state != "running" or not sb.container_id:
            raise InvalidTransition("Sandbox is not running")
        timeout = min(timeout or s.exec_timeout_default, s.exec_timeout_max)
        shown = cmd if isinstance(cmd, str) else " ".join(cmd)
        try:
            result = self.backend.exec(sb.container_id, cmd, timeout)
        except BackendError as e:
            self.db.audit(ip, "sandbox.exec", sb.id, ok=False, detail={"cmd": shown[:200], "error": str(e)[:200]})
            raise SandboxError(str(e)) from e
        self.db.audit(ip, "sandbox.exec", sb.id, ok=result.exit_code == 0,
                      detail={"cmd": shown[:200], "exit_code": result.exit_code})
        return result

    def reconcile_on_startup(self) -> None:
        """A crash mid-create leaves rows in 'creating'; mark them so the user can destroy them."""
        for sb in self.db.list_sandboxes():
            if sb.state == "creating":
                self.db.update_sandbox(sb, state="error", error="Interrupted while creating")
