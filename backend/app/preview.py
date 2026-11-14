"""Previews: a dev server inside the sandbox, published on the Tailscale IP.

Each sandbox gets a host port from PREVIEW_PORT_RANGE at creation, mapped to its template's
preview port (8000 python, 5173 node, 3000 static). A server must listen on 0.0.0.0 inside
the container to be reachable. Links use PREVIEW_HOST (the MagicDNS name), never the
hostname Agent Hub itself is opened on, so preview pages never receive the session cookie.
"""

from __future__ import annotations

from typing import Any

from . import config
from .db import Sandbox
from .docker_backend import Backend
from .sandboxes import SandboxError
from .templates import TEMPLATES

LISTEN = "0A"
LOOPBACK4 = "0100007F"
ANY4 = "00000000"
ANY6 = "0" * 32
LOOPBACK6 = "0" * 24 + "01000000"


def listeners(proc_net_tcp: str, port: int) -> set[str]:
    """Addresses listening on `port`, from /proc/net/tcp and tcp6: 'any', 'loopback' or 'other'."""
    found = set()
    for line in proc_net_tcp.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[3] != LISTEN or ":" not in parts[1]:
            continue
        addr, hexport = parts[1].rsplit(":", 1)
        try:
            if int(hexport, 16) != port:
                continue
        except ValueError:
            continue
        found.add("any" if addr in (ANY4, ANY6) else "loopback" if addr in (LOOPBACK4, LOOPBACK6) else "other")
    return found


def preview_url(sb: Sandbox) -> str | None:
    s = config.settings
    if not (sb.preview_port and s.previews_enabled):
        return None
    return f"http://{s.preview_host.rstrip('.')}:{sb.preview_port}/"


def server_cmd(template: str, port: int) -> str:
    """The one-click static server: Python's (lists directories) where Python exists, else
    busybox httpd (serves files by path only)."""
    if template == "python":
        return f"python3 -m http.server {port} --bind 0.0.0.0 --directory /workspace"
    return f"busybox httpd -f -p 0.0.0.0:{port} -h /workspace"


def lists_directories(template: str) -> bool:
    return template == "python"


def server_pattern(port: int) -> str:
    # Anchored, so it never matches the shell that runs pgrep/pkill itself.
    return f"^(busybox httpd -f -p 0.0.0.0:{port} |python3 -m http.server {port} )"


def server_argv(template: str, port: int) -> list[str]:
    return ["bash", "-c", f"setsid -f {server_cmd(template, port)} >/dev/null 2>&1 </dev/null"]


def stop_argv(port: int) -> list[str]:
    return ["sh", "-c", f"pkill -f '{server_pattern(port)}' ; true"]


def status(backend: Backend, sb: Sandbox) -> dict[str, Any]:
    tpl = TEMPLATES[sb.template]
    out: dict[str, Any] = {"url": preview_url(sb), "port": tpl.preview_port, "host_port": sb.preview_port,
                           "enabled": config.settings.previews_enabled, "listening": "none",
                           "lists_directories": lists_directories(sb.template)}
    if sb.state != "running" or not sb.container_id:
        out["listening"] = "stopped"
        return out
    script = (f"cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; "
              f"pgrep -f '{server_pattern(tpl.preview_port)}' >/dev/null && echo AH_STATIC=yes; "
              f"[ -f /workspace/index.html ] && echo AH_INDEX=yes; true")
    r = backend.exec(sb.container_id, ["sh", "-c", script], 15)
    found = listeners(r.output, tpl.preview_port)
    out["listening"] = "any" if "any" in found or "other" in found else "loopback" if "loopback" in found else "none"
    out["static_server"] = "AH_STATIC=yes" in r.output
    out["has_index"] = "AH_INDEX=yes" in r.output
    return out


def start_static(backend: Backend, sb: Sandbox) -> None:
    if sb.state != "running" or not sb.container_id:
        raise SandboxError("Start the sandbox first")
    r = backend.exec(sb.container_id, server_argv(sb.template, TEMPLATES[sb.template].preview_port), 15)
    if r.exit_code != 0:
        raise SandboxError(f"Couldn't start the file server: {r.output.strip()[-200:]}")


def stop_static(backend: Backend, sb: Sandbox) -> None:
    if sb.state == "running" and sb.container_id:
        backend.exec(sb.container_id, stop_argv(TEMPLATES[sb.template].preview_port), 15)
