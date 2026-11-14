"""Host resource checks that keep sandboxes from crowding out everything else on the server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import config
from .config import format_size


def mem_available() -> int:
    """MemAvailable from /proc/meminfo, in bytes (includes reclaimable page cache)."""
    return _meminfo()["MemAvailable"]


def mem_total() -> int:
    return _meminfo()["MemTotal"]


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def disk_usage(path: str) -> tuple[int, int]:
    """(total, free) bytes for the filesystem holding `path`."""
    st = os.statvfs(path)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


@dataclass(frozen=True)
class HostState:
    mem_available: int
    free_disk: int

    @classmethod
    def read(cls) -> "HostState":
        return cls(mem_available=mem_available(), free_disk=disk_usage(config.settings.disk_path)[1])


def start_blockers(new_memory: int, running_memory: list[int], host: HostState) -> list[str]:
    """Reasons a sandbox with `new_memory` can't start now. Empty list = OK to start."""
    s = config.settings
    reasons = []
    if len(running_memory) >= s.max_concurrent_sandboxes:
        reasons.append(f"{len(running_memory)} sandboxes already running "
                       f"(limit {s.max_concurrent_sandboxes}). Stop one first.")
    used = sum(running_memory)
    if used + new_memory > s.total_sandbox_memory:
        reasons.append(f"Sandbox memory would reach {format_size(used + new_memory)}, over the "
                       f"{format_size(s.total_sandbox_memory)} total cap.")
    left = host.mem_available - new_memory
    if left < s.memory_reserve:
        reasons.append(f"Starting it would leave {max(left, 0) // 2**20} MiB available on the host; "
                       f"the reserve is {format_size(s.memory_reserve)}.")
    if host.free_disk < s.min_free_disk:
        reasons.append(f"Only {host.free_disk // 2**30} GiB free disk; "
                       f"at least {format_size(s.min_free_disk)} is required.")
    return reasons
