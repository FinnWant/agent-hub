"""Container operations. `DockerBackend` talks to the real (rootless) daemon; `FakeBackend`
backs tests and DEMO_MODE.

Safety rule: every container and volume Agent Hub creates carries MANAGED_LABEL, and every
operation on an existing container first checks for it, so Agent Hub can never stop or
remove something it didn't create.
"""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from . import config
from .db import Sandbox
from .templates import Template
from .dockerio import ExecStream, attach_exec, tar_one, unix_socket_path, untar_one
from .terminal import DockerTerminal, FakeTerminal, TerminalSession

MANAGED_LABEL = "agent-hub.managed"
SANDBOX_LABEL = "agent-hub.sandbox-id"
WORKDIR = "/workspace"
SANDBOX_USER = "agent"


class BackendError(RuntimeError):
    pass


class NotManaged(BackendError):
    """The container exists but wasn't created by Agent Hub."""


@dataclass
class ExecResult:
    exit_code: int
    output: str
    truncated: bool
    timed_out: bool


def container_spec(sb: Sandbox, template: Template) -> dict[str, Any]:
    """Keyword arguments for `containers.create`. Every security setting lives here, so it can
    be unit-tested without Docker."""
    s = config.settings
    spec: dict[str, Any] = {
        "image": template.image,
        "name": f"sandbox-{sb.id}",
        "hostname": f"sandbox-{sb.id[:8]}",
        "command": ["sleep", "infinity"],
        "init": True,  # tini as PID 1 reaps zombies left by agents and tools
        "user": SANDBOX_USER,
        "working_dir": WORKDIR,
        "labels": {MANAGED_LABEL: "true", SANDBOX_LABEL: sb.id},
        # Privileges
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        # Resources
        "mem_limit": sb.memory,
        "memswap_limit": sb.memory,  # equal to mem_limit: no swap
        "nano_cpus": int(sb.cpus * 1e9),
        "pids_limit": s.pids_limit,
        "oom_score_adj": s.oom_score_adj,
        # Storage: only the sandbox's own named volume. Never host paths, never the Docker socket.
        "volumes": {sb.volume: {"bind": WORKDIR, "mode": "rw"}},
        "log_config": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "2"}},
        # Network: the dedicated bridge; egress is filtered on the host (deploy/egress.sh).
        "network": s.network_name,
        "restart_policy": {"Name": "no"},
        "detach": True,
    }
    if s.io_device:
        spec["device_read_bps"] = [{"Path": s.io_device, "Rate": s.io_read_bps}]
        spec["device_write_bps"] = [{"Path": s.io_device, "Rate": s.io_write_bps}]
    if s.container_runtime:
        spec["runtime"] = s.container_runtime
    return spec


def exec_argv(cmd: str | list[str], timeout: int) -> list[str]:
    """Wrap a command with coreutils `timeout` so a stuck process is killed inside the container."""
    argv = ["bash", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
    return ["timeout", "-k", "5", str(timeout), *argv]


class Backend(Protocol):
    def ping(self) -> bool: ...
    def ensure_network(self) -> None: ...
    def image_exists(self, image: str) -> bool: ...
    def create(self, sb: Sandbox, template: Template) -> str: ...
    def start(self, container_id: str) -> None: ...
    def stop(self, container_id: str) -> None: ...
    def remove(self, container_id: str | None, volume: str) -> None: ...
    def status(self, container_id: str) -> str: ...
    def exec(self, container_id: str, cmd: str | list[str], timeout: int,
             env: dict[str, str] | None = None) -> ExecResult: ...
    async def exec_stream(self, container_id: str, argv: list[str], env: dict[str, str]) -> Any: ...
    def put_file(self, container_id: str, directory: str, name: str, data: bytes) -> None: ...
    def get_file(self, container_id: str, path: str, max_size: int) -> bytes: ...
    async def open_terminal(self, container_id: str, cols: int, rows: int) -> TerminalSession: ...


def _limit(data: bytes) -> tuple[str, bool]:
    limit = config.settings.exec_output_limit
    truncated = len(data) > limit
    return data[-limit:].decode("utf-8", errors="replace"), truncated


class DockerBackend:
    def __init__(self) -> None:
        import docker  # imported lazily so tests and DEMO_MODE don't need a daemon

        self._docker = docker
        kwargs: dict[str, Any] = {"timeout": 60}
        if config.settings.docker_host:
            self.client = docker.DockerClient(base_url=config.settings.docker_host, **kwargs)
        else:
            self.client = docker.from_env(**kwargs)

    def _managed(self, container_id: str):
        try:
            c = self.client.containers.get(container_id)
        except self._docker.errors.NotFound:
            return None
        if c.labels.get(MANAGED_LABEL) != "true":
            raise NotManaged(f"Container {container_id[:12]} is not managed by Agent Hub")
        return c

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def ensure_network(self) -> None:
        name = config.settings.network_name
        if not self.client.networks.list(names=[name]):
            self.client.networks.create(name, driver="bridge", labels={MANAGED_LABEL: "true"})

    def image_exists(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except self._docker.errors.ImageNotFound:
            return False

    def create(self, sb: Sandbox, template: Template) -> str:
        self.client.volumes.create(sb.volume, labels={MANAGED_LABEL: "true", SANDBOX_LABEL: sb.id})
        return self.client.containers.create(**container_spec(sb, template)).id

    def start(self, container_id: str) -> None:
        c = self._managed(container_id)
        if c is None:
            raise BackendError("Container no longer exists")
        c.start()

    def stop(self, container_id: str) -> None:
        c = self._managed(container_id)
        if c is not None:
            c.stop(timeout=10)

    def remove(self, container_id: str | None, volume: str) -> None:
        if container_id:
            c = self._managed(container_id)
            if c is not None:
                c.remove(force=True)
        try:
            v = self.client.volumes.get(volume)
        except self._docker.errors.NotFound:
            return
        if (v.attrs.get("Labels") or {}).get(MANAGED_LABEL) != "true":
            raise NotManaged(f"Volume {volume} is not managed by Agent Hub")
        v.remove(force=True)

    def status(self, container_id: str) -> str:
        c = self._managed(container_id)
        return "missing" if c is None else c.status

    def _running(self, container_id: str):
        c = self._managed(container_id)
        if c is None or c.status != "running":
            raise BackendError("Sandbox is not running")
        return c

    def exec(self, container_id: str, cmd: str | list[str], timeout: int,
             env: dict[str, str] | None = None) -> ExecResult:
        c = self._running(container_id)
        code, out = c.exec_run(exec_argv(cmd, timeout), user=SANDBOX_USER, workdir=WORKDIR,
                               stdout=True, stderr=True, environment=env)
        text, truncated = _limit(out or b"")
        return ExecResult(exit_code=code, output=text, truncated=truncated, timed_out=code in (124, 137))

    async def exec_stream(self, container_id: str, argv: list[str], env: dict[str, str]) -> ExecStream:
        import asyncio

        c = await asyncio.to_thread(self._running, container_id)
        api = self.client.api
        ex = await asyncio.to_thread(api.exec_create, c.id, argv, stdout=True, stderr=True, stdin=False,
                                     tty=False, user=SANDBOX_USER, workdir=WORKDIR, environment=env)
        reader, writer = await attach_exec(api.api_version, unix_socket_path(config.settings.docker_host),
                                           ex["Id"], tty=False)
        return ExecStream(api, ex["Id"], reader, writer)

    def put_file(self, container_id: str, directory: str, name: str, data: bytes) -> None:
        if not self._running(container_id).put_archive(directory, tar_one(name, data)):
            raise BackendError(f"Couldn't copy {name} into the sandbox")

    def get_file(self, container_id: str, path: str, max_size: int) -> bytes:
        c = self._running(container_id)
        try:
            bits, _ = c.get_archive(path)
        except self._docker.errors.NotFound as e:
            raise BackendError(f"{path} not found in the sandbox") from e
        raw, limit = bytearray(), max_size + 2**20  # tar headers and padding
        for chunk in bits:
            raw += chunk
            if len(raw) > limit:
                raise BackendError(f"{path} is larger than {max_size} bytes")
        try:
            return untar_one(bytes(raw), max_size)
        except ValueError as e:
            raise BackendError(str(e)) from e

    async def open_terminal(self, container_id: str, cols: int, rows: int) -> TerminalSession:
        import asyncio

        c = await asyncio.to_thread(self._managed, container_id)
        if c is None or c.status != "running":
            raise BackendError("Sandbox is not running")
        term = DockerTerminal(self.client.api, c.id, unix_socket_path(config.settings.docker_host),
                              SANDBOX_USER, WORKDIR)
        await term.start(cols, rows)
        return term


class FakeBackend:
    """In-memory stand-in with the same safety checks, for tests and DEMO_MODE."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, str]] = {}
        self.networks: set[str] = set()
        self.images: set[str] = set()
        self.created_specs: list[dict[str, Any]] = []
        self.fail_next_start: str | None = None
        self.terminals: list[FakeTerminal] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.exec_calls: list[tuple[list[str] | str, dict[str, str] | None]] = []
        self.exec_replies: list[tuple[str, ExecResult]] = []  # (substring of the command, reply)
        self.stream_calls: list[tuple[list[str], dict[str, str]]] = []
        self.stream_script: list[tuple[float, int, bytes]] = []  # (delay, stream, data)
        self.stream_exit_code = 0

    def ping(self) -> bool:
        return True

    def ensure_network(self) -> None:
        self.networks.add(config.settings.network_name)

    def image_exists(self, image: str) -> bool:
        return config.settings.demo_mode or image in self.images

    def add_foreign_container(self, container_id: str) -> None:
        """Simulate a container Agent Hub didn't create (e.g. something else on the host)."""
        self.containers[container_id] = {"status": "running", "labels": {}}

    def _managed(self, container_id: str) -> dict[str, Any] | None:
        c = self.containers.get(container_id)
        if c is not None and c["labels"].get(MANAGED_LABEL) != "true":
            raise NotManaged(f"Container {container_id[:12]} is not managed by Agent Hub")
        return c

    def create(self, sb: Sandbox, template: Template) -> str:
        spec = container_spec(sb, template)
        self.created_specs.append(spec)
        self.volumes[sb.volume] = {MANAGED_LABEL: "true"}
        cid = uuid.uuid4().hex + uuid.uuid4().hex
        self.containers[cid] = {"status": "created", "labels": spec["labels"], "spec": spec}
        return cid

    def start(self, container_id: str) -> None:
        c = self._managed(container_id)
        if c is None:
            raise BackendError("Container no longer exists")
        if self.fail_next_start:
            msg, self.fail_next_start = self.fail_next_start, None
            raise BackendError(msg)
        c["status"] = "running"

    def stop(self, container_id: str) -> None:
        c = self._managed(container_id)
        if c is not None:
            c["status"] = "exited"

    def remove(self, container_id: str | None, volume: str) -> None:
        if container_id and self._managed(container_id) is not None:
            del self.containers[container_id]
        self.volumes.pop(volume, None)

    def status(self, container_id: str) -> str:
        c = self._managed(container_id)
        return "missing" if c is None else c["status"]

    def _running(self, container_id: str) -> dict[str, Any]:
        c = self._managed(container_id)
        if c is None or c["status"] != "running":
            raise BackendError("Sandbox is not running")
        return c

    def exec(self, container_id: str, cmd: str | list[str], timeout: int,
             env: dict[str, str] | None = None) -> ExecResult:
        self._running(container_id)
        self.exec_calls.append((cmd, env))
        shown = cmd if isinstance(cmd, str) else shlex.join(cmd)
        for needle, reply in self.exec_replies:
            if needle in shown:
                return reply
        return ExecResult(exit_code=0, output=f"(demo) $ {shown}\n", truncated=False, timed_out=False)

    async def exec_stream(self, container_id: str, argv: list[str], env: dict[str, str]) -> FakeExecStream:
        self._running(container_id)
        self.stream_calls.append((argv, env))
        return FakeExecStream(list(self.stream_script), self.stream_exit_code)

    def put_file(self, container_id: str, directory: str, name: str, data: bytes) -> None:
        self._running(container_id)
        self.files[(container_id, f"{directory.rstrip('/')}/{name}")] = data

    def get_file(self, container_id: str, path: str, max_size: int) -> bytes:
        self._running(container_id)
        if (container_id, path) not in self.files:
            raise BackendError(f"{path} not found in the sandbox")
        return self.files[(container_id, path)]

    async def open_terminal(self, container_id: str, cols: int, rows: int) -> TerminalSession:
        c = self._managed(container_id)
        if c is None or c["status"] != "running":
            raise BackendError("Sandbox is not running")
        term = FakeTerminal()
        await term.resize(cols, rows)
        self.terminals.append(term)
        return term


class FakeExecStream:
    def __init__(self, script: list[tuple[float, int, bytes]], code: int) -> None:
        import asyncio

        self.script = script
        self.code = code
        self.closed = asyncio.Event()

    async def chunks(self):
        import asyncio

        for delay, stream, data in self.script:
            try:
                await asyncio.wait_for(self.closed.wait(), timeout=delay)
                return
            except TimeoutError:
                yield stream, data

    async def exit_code(self) -> int | None:
        return None if self.closed.is_set() else self.code

    def close(self) -> None:
        self.closed.set()
