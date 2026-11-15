"""Background resource monitoring: memory in use per running sandbox (every STATS_INTERVAL s)
and the size of each /workspace (every WORKSPACE_SIZE_INTERVAL s, measured with low CPU and
I/O priority and stored, so stopped sandboxes still show their last size)."""

from __future__ import annotations

import asyncio
import logging
import time

from . import config
from .sandboxes import SandboxService

log = logging.getLogger(__name__)

DU_ARGV = ["sh", "-c", "nice -n 19 ionice -c 3 du -sxb /workspace 2>/dev/null | cut -f1"]


class Monitor:
    def __init__(self, svc: SandboxService) -> None:
        self.svc = svc
        self.memory: dict[str, int] = {}
        self._sized_at: dict[str, float] = {}

    def measure_memory(self) -> None:
        seen = {}
        for sb in self.svc.running():
            try:
                usage = self.svc.backend.memory_usage(sb.container_id or "")
            except Exception:  # noqa: BLE001
                continue
            if usage is not None:
                seen[sb.id] = usage
        self.memory = seen

    def measure_workspace(self, sandbox_id: str) -> int | None:
        sb = self.svc.db.get_sandbox(sandbox_id)
        if not sb or sb.state != "running" or not sb.container_id:
            return None
        r = self.svc.backend.exec(sb.container_id, DU_ARGV, 120)
        try:
            size = int(r.output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        self.svc.db.update_sandbox(sb, workspace_bytes=size)
        self._sized_at[sandbox_id] = time.monotonic()
        return size

    def tick(self) -> None:
        self.svc.list()  # reconciles states: notices sandboxes that died on their own
        self.measure_memory()
        interval = config.settings.workspace_size_interval
        for sb in self.svc.running():
            if time.monotonic() - self._sized_at.get(sb.id, -1e9) >= interval:
                try:
                    self.measure_workspace(sb.id)
                except Exception as e:  # noqa: BLE001
                    log.debug("workspace size for %s: %s", sb.id, e)

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as e:  # noqa: BLE001  (Docker briefly unavailable, etc.)
                log.warning("monitor: %s", e)
            await asyncio.sleep(config.settings.stats_interval)
