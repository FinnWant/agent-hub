"""Low-level Docker I/O shared by the terminal and agent runs.

`attach_exec` starts an exec with a raw, asyncio-native HTTP upgrade on the Docker unix socket
(the docker SDK's socket is blocking and can lose bytes to its HTTP read-ahead buffer).
`ExecStream` reads a non-TTY exec, whose stdout and stderr arrive multiplexed in 8-byte-header
frames. The tar helpers move single files in and out of a container.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import tarfile
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

STDOUT, STDERR = 1, 2


def unix_socket_path(docker_host: str) -> str:
    u = urlsplit(docker_host or "unix:///var/run/docker.sock")
    if u.scheme != "unix":
        raise ValueError("Kiln needs a unix:// DOCKER_HOST")
    return u.path


async def attach_exec(api_version: str, socket_path: str, exec_id: str, tty: bool,
                      size: tuple[int, int] | None = None
                      ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    body_obj: dict[str, Any] = {"Detach": False, "Tty": tty}
    if size:
        body_obj["ConsoleSize"] = [size[1], size[0]]  # rows, cols
    body = json.dumps(body_obj).encode()
    writer.write(
        f"POST /v{api_version}/exec/{exec_id}/start HTTP/1.1\r\n"
        "Host: docker\r\nContent-Type: application/json\r\n"
        "Connection: Upgrade\r\nUpgrade: tcp\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status and b" 200 " not in status:
        writer.close()
        raise RuntimeError(f"Docker refused the exec: {status.decode(errors='replace')}")
    return reader, writer


class ExecStream:
    """A running non-TTY exec. Iterate `chunks()` for (stream, bytes); then `exit_code()`."""

    def __init__(self, api: Any, exec_id: str, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.api = api
        self.exec_id = exec_id
        self._reader = reader
        self._writer = writer

    async def chunks(self) -> AsyncIterator[tuple[int, bytes]]:
        try:
            while True:
                header = await self._reader.readexactly(8)
                stream, size = header[0], struct.unpack(">I", header[4:])[0]
                yield stream, await self._reader.readexactly(size)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            self._writer.close()

    async def exit_code(self) -> int | None:
        for _ in range(50):  # the exit code lands a moment after the stream closes
            info = await asyncio.to_thread(self.api.exec_inspect, self.exec_id)
            if not info.get("Running") and info.get("ExitCode") is not None:
                return int(info["ExitCode"])
            await asyncio.sleep(0.1)
        return None

    def close(self) -> None:
        self._writer.close()


def tar_one(name: str, data: bytes, uid: int = 1000, mode: int = 0o600) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, uid, uid, int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def untar_one(stream: bytes, max_size: int) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(stream), mode="r") as tar:
        member = next((m for m in tar.getmembers() if m.isfile()), None)
        if member is None:
            raise ValueError("archive has no regular file")
        if member.size > max_size:
            raise ValueError(f"file is larger than {max_size} bytes")
        f = tar.extractfile(member)
        assert f is not None
        return f.read()
