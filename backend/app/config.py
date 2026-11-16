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
    # The address you open Kiln at, when it sits behind HTTPS (`tailscale serve`), e.g.
    # https://myserver.tailnet.ts.net. Enables Secure cookies, the installable app and Web Push.
    public_url: str = os.getenv("PUBLIC_URL", "").rstrip("/")
    # Host names Kiln answers to besides PUBLIC_URL's, BIND_HOSTS and localhost (comma-separated).
    extra_allowed_hosts: list[str] = field(default_factory=lambda: _list("ALLOWED_HOSTS", ""))
    # Behind `tailscale serve` (requests arrive from 127.0.0.1): take the client's address from
    # X-Forwarded-For and its Tailscale login from Tailscale-User-Login (Serve strips forged ones).
    trust_proxy: bool = _bool("TRUST_PROXY", False)

    preview_port_range: str = os.getenv("PREVIEW_PORT_RANGE", "8200-8299")
    # Previews are published on this address only (the Tailscale IP; never 0.0.0.0) ...
    preview_bind: str = os.getenv("PREVIEW_BIND", "")
    # ... and linked by this hostname (MagicDNS), which must differ from the one you use for
    # Kiln: browsers send cookies to every port of a host, so a preview on the same host
    # would receive the session cookie. Logins on this hostname are refused.
    preview_host: str = os.getenv("PREVIEW_HOST", "")

    # Docker: the agenthub user's rootless daemon.
    docker_host: str = os.getenv("DOCKER_HOST", "")
    network_name: str = os.getenv("SANDBOX_NETWORK", "agent-sandboxes")
    image_version: str = os.getenv("IMAGE_VERSION", "3")
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
    # Below this much free disk, running sandboxes are stopped so a runaway agent can't fill
    # the disk the rest of the server (Minecraft, ...) needs.
    disk_emergency_free: int = _size("DISK_EMERGENCY_FREE", "5g")
    disk_path: str = os.getenv("DISK_CHECK_PATH", "/")
    # Kernel OOM killer picks sandboxes before anything else (rootless may only raise it).
    oom_score_adj: int = _int("SANDBOX_OOM_SCORE_ADJ", 800)
    # Disk I/O throttling (needs the cgroup io controller delegated). Empty device = off.
    io_device: str = os.getenv("SANDBOX_IO_DEVICE", "")
    io_read_bps: int = _size("SANDBOX_IO_READ_BPS", "200m")
    io_write_bps: int = _size("SANDBOX_IO_WRITE_BPS", "100m")

    # Resource monitoring
    stats_interval: int = _int("STATS_INTERVAL", 15)               # seconds, memory use
    workspace_size_interval: int = _int("WORKSPACE_SIZE_INTERVAL", 300)  # seconds, du of /workspace

    # Exec endpoint
    exec_timeout_default: int = _int("EXEC_TIMEOUT_DEFAULT", 60)
    exec_timeout_max: int = _int("EXEC_TIMEOUT_MAX", 600)
    exec_output_limit: int = _int("EXEC_OUTPUT_LIMIT", 64 * 1024)

    # Browser terminals (docker exec with a TTY, one shell per WebSocket)
    max_terminals: int = _int("MAX_TERMINALS", 4)
    terminal_idle_timeout: int = _int("TERMINAL_IDLE_TIMEOUT", 3600)

    # Agent runs
    run_timeout_default: int = _int("RUN_TIMEOUT_DEFAULT", 30 * 60)
    run_timeout_max: int = _int("RUN_TIMEOUT_MAX", 4 * 60 * 60)
    run_max_turns_default: int = _int("RUN_MAX_TURNS_DEFAULT", 50)
    run_max_turns_max: int = _int("RUN_MAX_TURNS_MAX", 500)
    run_max_events: int = _int("RUN_MAX_EVENTS", 20_000)  # per run; later output is dropped
    agent_model: str = os.getenv("AGENT_MODEL", "")  # empty = the CLI's default model
    # "gateway" (default): agents get a per-run key, the token stays in the Claude gateway.
    # "direct": the token itself goes into the sandbox (only if the gateway doesn't work for you).
    claude_token_mode: str = os.getenv("CLAUDE_TOKEN_MODE", "gateway")
    git_author_name: str = os.getenv("AGENT_GIT_NAME", "Kiln")
    git_author_email: str = os.getenv("AGENT_GIT_EMAIL", "agent-hub@users.noreply.github.com")

    # Secrets (only in .env, mode 600; set with: python -m app.set_secret). Never sent to the
    # browser and redacted from every stored or streamed log. The Claude token is passed to the
    # agent process only; the GitHub token never enters a sandbox (clone/push happen host-side).
    claude_code_oauth_token: str = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")

    # Built web UI (frontend/dist); served at / when present.
    frontend_dir: Path = Path(os.getenv("FRONTEND_DIR", str(Path(__file__).resolve().parents[2] / "frontend" / "dist")))

    # Storage / modes
    db_path: Path = Path(os.getenv("DB_PATH", "data/agent-hub.db")).expanduser()
    demo_mode: bool = _bool("DEMO_MODE", False)


    @property
    def https(self) -> bool:
        return self.public_url.startswith("https://")

    def host_allowed(self, host: str) -> bool:
        """Requests must name a host Kiln is meant to be reached at, so a web page can't reach it
        through a hostname of its own that resolves here (DNS rebinding)."""
        from urllib.parse import urlsplit

        name = host.rsplit(":", 1)[0] if host.count(":") <= 1 else host.rsplit("]:", 1)[0] + "]"
        name = name.strip("[]").lower().rstrip(".")
        allowed = {"localhost", "127.0.0.1", "::1", *self.bind_hosts, *self.extra_allowed_hosts,
                   self.preview_host, urlsplit(self.public_url).hostname or ""}
        allowed = {h.strip("[]").lower().rstrip(".") for h in allowed if h}
        return "0.0.0.0" in allowed or "::" in allowed or name in allowed

    @property
    def preview_ports(self) -> range:
        lo, _, hi = self.preview_port_range.partition("-")
        return range(int(lo), int(hi or lo) + 1)

    @property
    def previews_enabled(self) -> bool:
        return bool(self.preview_bind and self.preview_host)


settings = Settings()
