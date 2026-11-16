"""Container operations. `DockerBackend` talks to the real (rootless) daemon; `FakeBackend`
backs tests and DEMO_MODE.

Safety rule: every container and volume Kiln creates carries MANAGED_LABEL, and every
operation on an existing container first checks for it, so Kiln can never stop or
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
from .dockerio import ExecStream, attach_exec, tar_one, unix_socket_path, untar_one, untar_stream_to
from .terminal import DockerTerminal, FakeTerminal, TerminalSession

MANAGED_LABEL = "agent-hub.managed"
SANDBOX_LABEL = "agent-hub.sandbox-id"
WORKDIR = "/workspace"
SANDBOX_USER = "agent"
# Kiln's own commands in a sandbox use only system directories (an agent can't plant a fake
# `git` or `sh` there; /home/agent/.local/bin comes first in the image's PATH for the agent).
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
GATEWAY_NAME = "kiln-gateway"
GATEWAY_NETWORK = "kiln-gateway-net"
GATEWAY_LABEL = "agent-hub.gateway"


def sandbox_network(sandbox_id: str) -> str:
    """Every sandbox has its own network, so sandboxes can't reach each other."""
    return f"kiln-sb-{sandbox_id}"


class BackendError(RuntimeError):
    pass


class NotManaged(BackendError):
    """The container exists but wasn't created by Kiln."""


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
        # Network: its own bridge (no other sandbox on it); egress is filtered on the host
        # (deploy/egress.sh).
        "network": sandbox_network(sb.id),
        "restart_policy": {"Name": "no"},
        "detach": True,
    }
    if s.io_device:
        spec["device_read_bps"] = [{"Path": s.io_device, "Rate": s.io_read_bps}]
        spec["device_write_bps"] = [{"Path": s.io_device, "Rate": s.io_write_bps}]
    if s.container_runtime:
        spec["runtime"] = s.container_runtime
    if sb.preview_port and s.preview_bind:
        # Tailscale IP only, never 0.0.0.0.
        spec["ports"] = {f"{template.preview_port}/tcp": (s.preview_bind, sb.preview_port)}
    return spec


def exec_argv(cmd: str | list[str], timeout: int) -> list[str]:
    """Wrap a command with coreutils `timeout` so a stuck process is killed inside the container."""
    argv = ["bash", "-lc", cmd] if isinstance(cmd, str) else list(cmd)
    return ["/usr/bin/timeout", "-k", "5", str(timeout), *argv]


def exec_env(cmd: str | list[str], env: dict[str, str] | None) -> dict[str, str] | None:
    """Kiln's own commands (lists) get a PATH of system directories only; your commands from
    the UI (strings) run in a normal login shell."""
    if isinstance(cmd, str):
        return env
    return {"PATH": SAFE_PATH, **(env or {})}


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
    def memory_usage(self, container_id: str) -> int | None: ...
    def exit_info(self, container_id: str) -> dict[str, Any]: ...
    def prune(self, keep_sandbox_ids: set[str], current_images: set[str]) -> dict[str, Any]: ...
    def published_ports(self) -> set[int]: ...
    def ensure_gateway(self, spec: dict[str, Any]) -> None: ...
    def attach_gateway(self, sandbox_id: str) -> None: ...
    def gateway_exec(self, argv: list[str]) -> ExecResult: ...
    def get_file_to(self, container_id: str, path: str, dest: str, max_size: int) -> int: ...
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
            raise NotManaged(f"Container {container_id[:12]} is not managed by Kiln")
        return c

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def _network(self, name: str):
        try:
            return self.client.networks.get(name)
        except self._docker.errors.NotFound:
            return None

    def _ensure_net(self, name: str, labels: dict[str, str]):
        net = self._network(name)
        if net is None:
            net = self.client.networks.create(name, driver="bridge", labels=labels)
        return net

    def _remove_net(self, name: str) -> None:
        net = self._network(name)
        if net is None or (net.attrs.get("Labels") or {}).get(MANAGED_LABEL) != "true":
            return
        net.reload()
        for c in net.containers:  # the gateway, if it was attached
            net.disconnect(c, force=True)
        net.remove()

    def _move_to_own_network(self, c) -> None:
        """Sandboxes made before each had its own network: move one (when it isn't running)."""
        legacy = config.settings.network_name
        nets = (c.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        sid = c.labels.get(SANDBOX_LABEL)
        if legacy not in nets or not sid:
            return
        own = self._ensure_net(sandbox_network(sid), {MANAGED_LABEL: "true", SANDBOX_LABEL: sid})
        if own.name not in nets:
            own.connect(c)
        old = self._network(legacy)
        if old is not None:
            old.disconnect(c, force=True)
        c.reload()

    def ensure_network(self) -> None:
        """At startup: the gateway's network, and moving stopped sandboxes off the old shared
        network (running ones move the next time they start)."""
        self._ensure_net(GATEWAY_NETWORK, {MANAGED_LABEL: "true"})
        legacy = self._network(config.settings.network_name)
        if legacy is None:
            return
        still_on_it = False
        for c in self.client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=true"}):
            if config.settings.network_name not in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}):
                continue
            if c.status == "running":
                still_on_it = True
                continue
            try:
                self._move_to_own_network(c)
            except Exception as e:  # noqa: BLE001  (keep the old network so this one still starts)
                still_on_it = True
                import logging

                logging.getLogger(__name__).warning("couldn't move %s to its own network: %s", c.name, e)
        if not still_on_it and (legacy.attrs.get("Labels") or {}).get(MANAGED_LABEL) == "true":
            legacy.remove()

    def image_exists(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except self._docker.errors.ImageNotFound:
            return False

    def create(self, sb: Sandbox, template: Template) -> str:
        self.client.volumes.create(sb.volume, labels={MANAGED_LABEL: "true", SANDBOX_LABEL: sb.id})
        self._ensure_net(sandbox_network(sb.id), {MANAGED_LABEL: "true", SANDBOX_LABEL: sb.id})
        return self.client.containers.create(**container_spec(sb, template)).id

    def start(self, container_id: str) -> None:
        c = self._managed(container_id)
        if c is None:
            raise BackendError("Container no longer exists")
        if c.status != "running":
            self._move_to_own_network(c)
        c.start()

    def stop(self, container_id: str) -> None:
        c = self._managed(container_id)
        if c is not None:
            c.stop(timeout=10)

    def remove(self, container_id: str | None, volume: str) -> None:
        sid = volume.removeprefix("ah-")
        if container_id:
            c = self._managed(container_id)
            if c is not None:
                sid = c.labels.get(SANDBOX_LABEL) or sid
                c.remove(force=True)
        self._remove_net(sandbox_network(sid))
        try:
            v = self.client.volumes.get(volume)
        except self._docker.errors.NotFound:
            return
        if (v.attrs.get("Labels") or {}).get(MANAGED_LABEL) != "true":
            raise NotManaged(f"Volume {volume} is not managed by Kiln")
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
                               stdout=True, stderr=True, environment=exec_env(cmd, env))
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

    def get_file_to(self, container_id: str, path: str, dest: str, max_size: int) -> int:
        """Copy one file out of the sandbox to `dest` on the host, streaming (constant memory)."""
        c = self._running(container_id)
        try:
            bits, _ = c.get_archive(path)
        except self._docker.errors.NotFound as e:
            raise BackendError(f"{path} not found in the sandbox") from e
        try:
            return untar_stream_to(bits, dest, max_size)
        except ValueError as e:
            raise BackendError(str(e)) from e

    # ---- the Claude gateway (see gateway.py) ---------------------------------------

    def _gateway(self):
        try:
            c = self.client.containers.get(GATEWAY_NAME)
        except self._docker.errors.NotFound:
            return None
        if c.labels.get(MANAGED_LABEL) != "true":
            raise NotManaged(f"A container named {GATEWAY_NAME} exists but isn't Kiln's")
        return c

    def ensure_gateway(self, spec: dict[str, Any]) -> None:
        self._ensure_net(GATEWAY_NETWORK, {MANAGED_LABEL: "true"})
        c = self._gateway()
        if c is not None and c.labels.get(GATEWAY_LABEL) != spec["labels"][GATEWAY_LABEL]:
            c.remove(force=True)
            c = None
        if c is None:
            c = self.client.containers.create(**spec)
        c.reload()
        if c.status != "running":
            c.start()
        # Wait until the proxy listens (a run started right away would otherwise be refused).
        port = int(spec["environment"].get("KILN_LISTEN_PORT", "8080"))
        probe = ["sh", "-c", f"grep -qi ':{port:04X} 00000000:0000 0A' /proc/net/tcp"]
        import time

        deadline = time.monotonic() + 20
        while True:
            c.reload()
            if c.status == "running" and c.exec_run(probe, user="agent").exit_code == 0:
                return
            if time.monotonic() > deadline:
                logs = c.logs(tail=5).decode(errors="replace").strip()
                raise BackendError(f"The Claude gateway didn't start: {logs[-300:] or c.status}")
            time.sleep(0.25)

    def attach_gateway(self, sandbox_id: str) -> None:
        net = self._network(sandbox_network(sandbox_id))
        if net is None:
            raise BackendError("This sandbox is still on the old shared network. Stop it and start it "
                               "again once (that moves it to its own network), then start the run.")
        gw = self._gateway()
        if gw is None:
            raise BackendError("The Claude gateway isn't running")
        gw.reload()
        if net.name not in ((gw.attrs.get("NetworkSettings") or {}).get("Networks") or {}):
            net.connect(gw, aliases=[GATEWAY_NAME])

    def gateway_exec(self, argv: list[str]) -> ExecResult:
        gw = self._gateway()
        if gw is None or gw.status != "running":
            raise BackendError("The Claude gateway isn't running")
        code, out = gw.exec_run(["/usr/bin/timeout", "-k", "5", "20", *argv], user="agent",
                                environment={"PATH": SAFE_PATH})
        text, truncated = _limit(out or b"")
        return ExecResult(exit_code=code, output=text, truncated=truncated, timed_out=code in (124, 137))

    def exit_info(self, container_id: str) -> dict[str, Any]:
        c = self._managed(container_id)
        if c is None:
            return {}
        st = c.attrs.get("State") or {}
        return {"exit_code": st.get("ExitCode"), "oom": bool(st.get("OOMKilled"))}

    def memory_usage(self, container_id: str) -> int | None:
        """Memory in use, as `docker stats` counts it (minus reclaimable page cache)."""
        c = self._managed(container_id)
        if c is None or c.status != "running":
            return None
        st = c.stats(stream=False, one_shot=True).get("memory_stats") or {}
        usage = st.get("usage")
        if usage is None:
            return None
        inactive = (st.get("stats") or {}).get("inactive_file", 0)
        return max(0, usage - inactive)

    def published_ports(self) -> set[int]:
        """Host ports any container on this Docker daemon publishes (running or not), so a new
        sandbox never asks for one that is taken, e.g. by a sandbox from another database."""
        ports: set[int] = set()
        for c in self.client.containers.list(all=True):
            for binds in (c.attrs.get("HostConfig", {}).get("PortBindings") or {}).values():
                for b in binds or []:
                    if str(b.get("HostPort", "")).isdigit():
                        ports.add(int(b["HostPort"]))
        return ports

    def prune(self, keep_sandbox_ids: set[str], current_images: set[str]) -> dict[str, Any]:
        """Reclaim disk without touching anything a live sandbox uses or anything Kiln
        didn't create: managed containers/volumes of destroyed sandboxes, sandbox images of
        older versions no container uses, dangling images and the build cache."""
        out: dict[str, Any] = {"containers": [], "volumes": [], "images": [], "bytes": 0}
        managed = {"label": f"{MANAGED_LABEL}=true"}
        for c in self.client.containers.list(all=True, filters=managed):
            sid = c.labels.get(SANDBOX_LABEL)
            if sid and sid not in keep_sandbox_ids:
                c.remove(force=True)
                out["containers"].append(c.name)
        for v in self.client.volumes.list(filters=managed):
            sid = (v.attrs.get("Labels") or {}).get(SANDBOX_LABEL)
            if sid and sid not in keep_sandbox_ids:
                v.remove(force=True)
                out["volumes"].append(v.name)
        for net in self.client.networks.list(filters=managed):
            sid = (net.attrs.get("Labels") or {}).get(SANDBOX_LABEL)
            if sid and sid not in keep_sandbox_ids:
                self._remove_net(net.name)
                out.setdefault("networks", []).append(net.name)
        for img in self.client.images.list(name="agent-hub/*"):
            tags = [t for t in img.tags if t.startswith("agent-hub/")]
            if tags and not set(tags) & current_images:
                try:
                    self.client.images.remove(img.id)
                    out["images"] += tags
                    out["bytes"] += img.attrs.get("Size", 0)
                except self._docker.errors.APIError:
                    pass  # still used by a stopped sandbox: keep it
        r = self.client.images.prune(filters={"dangling": True})
        out["bytes"] += r.get("SpaceReclaimed") or 0
        try:
            out["bytes"] += self.client.api.prune_builds().get("SpaceReclaimed") or 0
        except self._docker.errors.APIError:
            pass
        return out

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
        self.foreign_ports: set[int] = set()  # host ports held by containers outside this database
        self.gateway: dict[str, Any] | None = None   # {"spec": ..., "networks": set(), "keys": set()}
        self.gateway_calls: list[list[str]] = []

    def ping(self) -> bool:
        return True

    def ensure_network(self) -> None:
        self.networks.add(GATEWAY_NETWORK)

    def ensure_gateway(self, spec: dict[str, Any]) -> None:
        if self.gateway is None or self.gateway["spec"]["labels"] != spec["labels"]:
            self.gateway = {"spec": spec, "networks": {GATEWAY_NETWORK}, "keys": set()}

    def attach_gateway(self, sandbox_id: str) -> None:
        if self.gateway is None:
            raise BackendError("The Claude gateway isn't running")
        if sandbox_network(sandbox_id) not in self.networks:
            raise BackendError("This sandbox is still on the old shared network.")
        self.gateway["networks"].add(sandbox_network(sandbox_id))

    def gateway_exec(self, argv: list[str]) -> ExecResult:
        if self.gateway is None:
            raise BackendError("The Claude gateway isn't running")
        self.gateway_calls.append(list(argv))
        keys = self.gateway["keys"]
        if argv[:2] == ["sh", "-c"]:      # touch "$0/$1" KEYS_DIR digest
            keys.add(argv[-1])
        elif argv[:2] == ["rm", "-f"]:
            keys.discard(argv[-1].rsplit("/", 1)[-1])
        elif argv[0] == "find":           # find KEYS_DIR -type f [! -name keep]... -delete
            kept = {argv[i + 2] for i, a in enumerate(argv) if a == "!" and argv[i + 1] == "-name"}
            keys.intersection_update(kept)
        return ExecResult(exit_code=0, output="", truncated=False, timed_out=False)

    def get_file_to(self, container_id: str, path: str, dest: str, max_size: int) -> int:
        data = self.get_file(container_id, path, max_size)
        with open(dest, "wb") as f:
            f.write(data)
        return len(data)

    def image_exists(self, image: str) -> bool:
        return config.settings.demo_mode or image in self.images

    def published_ports(self) -> set[int]:
        return set(self.foreign_ports)

    def add_foreign_container(self, container_id: str) -> None:
        """Simulate a container Kiln didn't create (e.g. something else on the host)."""
        self.containers[container_id] = {"status": "running", "labels": {}}

    def _managed(self, container_id: str) -> dict[str, Any] | None:
        c = self.containers.get(container_id)
        if c is not None and c["labels"].get(MANAGED_LABEL) != "true":
            raise NotManaged(f"Container {container_id[:12]} is not managed by Kiln")
        return c

    def create(self, sb: Sandbox, template: Template) -> str:
        spec = container_spec(sb, template)
        self.created_specs.append(spec)
        self.volumes[sb.volume] = {MANAGED_LABEL: "true"}
        self.networks.add(sandbox_network(sb.id))
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
        net = sandbox_network(volume.removeprefix("ah-"))
        self.networks.discard(net)
        if self.gateway:
            self.gateway["networks"].discard(net)

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

    def exit_info(self, container_id: str) -> dict[str, Any]:
        c = self._managed(container_id)
        return {"exit_code": (c or {}).get("exit_code", 137), "oom": (c or {}).get("oom", False)}

    def memory_usage(self, container_id: str) -> int | None:
        c = self._managed(container_id)
        return 300 * 2**20 if c is not None and c["status"] == "running" else None

    def prune(self, keep_sandbox_ids: set[str], current_images: set[str]) -> dict[str, Any]:
        gone = [cid for cid, c in self.containers.items()
                if c["labels"].get(MANAGED_LABEL) == "true" and c["labels"].get(SANDBOX_LABEL) not in keep_sandbox_ids]
        for cid in gone:
            del self.containers[cid]
        return {"containers": gone, "volumes": [], "images": [], "bytes": 0}

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
