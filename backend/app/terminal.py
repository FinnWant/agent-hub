"""Interactive shells for the browser terminal.

`DockerTerminal` runs `bash -l` as the sandbox user through `docker exec` with a TTY. The exec
stream is opened with a raw, asyncio-native HTTP upgrade on the Docker socket (rather than the
docker SDK's blocking socket), so reads and writes never tie up a thread and no bytes are lost
to an HTTP read-ahead buffer.

Docker has no "kill exec" call, and closing the stream doesn't reliably end the shell, so each
shell gets a unique argv[0] tag and `close()` kills its whole session (the shell and anything
it started) from inside the container.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Protocol
from urllib.parse import urlsplit

SHELL_ENV = ["TERM=xterm-256color", "COLORTERM=truecolor", "LANG=C.UTF-8"]
READ_CHUNK = 64 * 1024


class TerminalSession(Protocol):
    async def read(self) -> bytes:
        """Next chunk of output; b"" when the shell has exited."""
        ...

    async def write(self, data: bytes) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...


def shell_argv(tag: str) -> list[str]:
    # `exec -a` gives the shell a unique argv[0] we can find with pgrep; -l makes it a login shell.
    return ["bash", "-c", f"exec -a {tag} bash -l"]


def kill_argv(tag: str) -> list[str]:
    # The exec'd shell leads its own session (runc calls setsid for TTY execs): kill the session.
    return ["sh", "-c", f"for p in $(pgrep -f '^{tag}( |$)'); do pkill -KILL -s \"$p\"; done; true"]


def unix_socket_path(docker_host: str) -> str:
    u = urlsplit(docker_host or "unix:///var/run/docker.sock")
    if u.scheme != "unix":
        raise ValueError("The browser terminal needs a unix:// DOCKER_HOST")
    return u.path


class DockerTerminal:
    def __init__(self, api: Any, container_id: str, socket_path: str, user: str, workdir: str) -> None:
        self.api = api  # docker.APIClient, for the (blocking) create/resize/kill calls
        self.container_id = container_id
        self.socket_path = socket_path
        self.user = user
        self.workdir = workdir
        self.tag = f"ah-term-{secrets.token_hex(6)}"
        self.exec_id = ""
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def start(self, cols: int, rows: int) -> None:
        ex = await asyncio.to_thread(
            self.api.exec_create, self.container_id, shell_argv(self.tag), stdin=True, tty=True,
            user=self.user, workdir=self.workdir, environment=SHELL_ENV)
        self.exec_id = ex["Id"]
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        body = json.dumps({"Detach": False, "Tty": True, "ConsoleSize": [rows, cols]}).encode()
        writer.write(
            f"POST /v{self.api.api_version}/exec/{self.exec_id}/start HTTP/1.1\r\n"
            "Host: docker\r\nContent-Type: application/json\r\n"
            "Connection: Upgrade\r\nUpgrade: tcp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status and b" 200 " not in status:
            writer.close()
            raise RuntimeError(f"Docker refused the terminal: {status.decode(errors='replace')}")
        self._reader, self._writer = reader, writer
        await self.resize(cols, rows)

    async def read(self) -> bytes:
        assert self._reader is not None
        try:
            return await self._reader.read(READ_CHUNK)
        except (ConnectionError, OSError):
            return b""

    async def write(self, data: bytes) -> None:
        if self._writer is None or self._closed:
            return
        self._writer.write(data)
        await self._writer.drain()

    async def resize(self, cols: int, rows: int) -> None:
        try:
            await asyncio.to_thread(self.api.exec_resize, self.exec_id, height=rows, width=cols)
        except Exception:
            pass  # the shell may have just exited; resizing is best-effort

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._writer.close()
        try:  # end the shell and everything it started
            ex = await asyncio.to_thread(self.api.exec_create, self.container_id,
                                         kill_argv(self.tag), user=self.user)
            await asyncio.to_thread(self.api.exec_start, ex["Id"])
        except Exception:
            pass  # container already stopped or gone: the shell is gone with it


class FakeTerminal:
    """A pretend shell for tests and DEMO_MODE: echoes input and answers a few commands."""

    PROMPT = b"\x1b[38;5;209magent@sandbox\x1b[0m:\x1b[38;5;110m/workspace\x1b[0m$ "

    def __init__(self) -> None:
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._line = bytearray()
        self.size = (80, 24)
        self.closed = False
        self.received = bytearray()
        self._out.put_nowait(b"Demo terminal: commands are simulated.\r\n" + self.PROMPT)

    async def read(self) -> bytes:
        return await self._out.get()

    def _answer(self, line: str) -> bytes | None:
        parts = [p.strip() for p in line.split(";")]
        if any(p in ("exit", "logout") for p in parts):
            return None
        lines = [self._reply(p) for p in parts]
        return "".join(f"{t}\r\n" for t in lines if t).encode() + self.PROMPT

    def _reply(self, cmd: str) -> str:
        if cmd.startswith("echo "):
            subs = {"$(id -un)": "agent", "$(stty size)": f"{self.size[1]} {self.size[0]}", "$(pwd)": "/workspace"}
            text = cmd[5:].strip().strip("'\"")
            for k, v in subs.items():
                text = text.replace(k, v)
            return text
        replies = {
            "": "",
            "python3 --version": "Python 3.13.0",
            "whoami": "agent",
            "id": "uid=1000(agent) gid=1000(agent) groups=1000(agent)",
            "pwd": "/workspace",
            "ls": "README.md  src  tests",
        }
        return replies.get(cmd, f"(demo) {cmd.split()[0]}: simulated, nothing ran")

    async def write(self, data: bytes) -> None:
        self.received += data
        for ch in data:
            if ch in (13, 10):
                reply = self._answer(self._line.decode(errors="replace").strip())
                self._line.clear()
                if reply is None:
                    self._out.put_nowait(b"\r\nlogout\r\n")
                    self._out.put_nowait(b"")
                    return
                self._out.put_nowait(b"\r\n" + reply)
            elif ch in (127, 8):
                if self._line:
                    self._line.pop()
                    self._out.put_nowait(b"\b \b")
            elif ch == 3:  # Ctrl-C
                self._line.clear()
                self._out.put_nowait(b"^C\r\n" + self.PROMPT)
            elif ch >= 32:
                self._line.append(ch)
                self._out.put_nowait(bytes([ch]))

    async def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    async def close(self) -> None:
        self.closed = True
