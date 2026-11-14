"""Runtime configuration, loaded from environment variables / a .env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(value: str | int) -> int:
    """'2g' -> bytes. Accepts plain integers (bytes) and k/m/g suffixes (binary units)."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgb]?)i?b?\s*", str(value).lower())
    if not m:
        raise ValueError(f"Invalid size: {value!r} (use e.g. 512m or 2g)")
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def format_size(n: int) -> str:
    for unit, factor in (("g", 1024**3), ("m", 1024**2), ("k", 1024)):
        if n >= factor and n % factor == 0:
            return f"{n // factor}{unit}"
    return str(n)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _size(name: str, default: str) -> int:
    return parse_size(os.getenv(name) or default)


def _list(name: str, default: str) -> list[str]:
    return [p.strip() for p in (os.getenv(name) or default).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # Network. Each host is bound with IP_FREEBIND, so the Tailscale IP may come up after us.
    bind_hosts: list[str] = field(default_factory=lambda: _list("BIND_HOSTS", "127.0.0.1"))
    port: int = _int("PORT", 8090)
    preview_port_range: str = os.getenv("PREVIEW_PORT_RANGE", "8200-8299")

    # Docker: the agenthub user's rootless daemon.
    docker_host: str = os.getenv("DOCKER_HOST", "")
    network_name: str = os.getenv("SANDBOX_NETWORK", "agent-sandboxes")
    image_version: str = os.getenv("IMAGE_VERSION", "1")
    # "runc" today; "runsc" (gVisor) later is a one-line change.
    container_runtime: str = os.getenv("CONTAINER_RUNTIME", "")

    # Auth. Everything stays locked until both are set (python -m app.set_password).
    admin_password_hash: str = os.getenv("ADMIN_PASSWORD_HASH", "")
    session_secret: str = os.getenv("SESSION_SECRET", "")
    session_days: int = _int("SESSION_DAYS", 7)

    # Capacity: protects everything else on the host (Minecraft first).
    max_concurrent_sandboxes: int = _int("MAX_CONCURRENT_SANDBOXES", 2)
    default_sandbox_memory: int = _size("DEFAULT_SANDBOX_MEMORY", "2g")
    max_sandbox_memory: int = _size("MAX_SANDBOX_MEMORY", "4g")
    total_sandbox_memory: int = _size("TOTAL_SANDBOX_MEMORY", "6g")
    memory_reserve: int = _size("MEMORY_RESERVE", "3g")
    default_sandbox_cpus: float = _float("DEFAULT_SANDBOX_CPUS", 2)
    max_sandbox_cpus: float = _float("MAX_SANDBOX_CPUS", 4)
    pids_limit: int = _int("SANDBOX_PIDS_LIMIT", 512)
    min_free_disk: int = _size("MIN_FREE_DISK", "15g")
    disk_path: str = os.getenv("DISK_CHECK_PATH", "/")
    # Kernel OOM killer picks sandboxes before anything else (rootless may only raise it).
    oom_score_adj: int = _int("SANDBOX_OOM_SCORE_ADJ", 800)
    # Disk I/O throttling (needs the cgroup io controller delegated). Empty device = off.
    io_device: str = os.getenv("SANDBOX_IO_DEVICE", "")
    io_read_bps: int = _size("SANDBOX_IO_READ_BPS", "200m")
    io_write_bps: int = _size("SANDBOX_IO_WRITE_BPS", "100m")

    # Exec endpoint
    exec_timeout_default: int = _int("EXEC_TIMEOUT_DEFAULT", 60)
    exec_timeout_max: int = _int("EXEC_TIMEOUT_MAX", 600)
    exec_output_limit: int = _int("EXEC_OUTPUT_LIMIT", 64 * 1024)

    # Storage / modes
    db_path: Path = Path(os.getenv("DB_PATH", "data/agent-hub.db")).expanduser()
    demo_mode: bool = _bool("DEMO_MODE", False)


settings = Settings()
