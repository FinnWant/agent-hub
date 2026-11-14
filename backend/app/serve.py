"""Run the API on every address in BIND_HOSTS (never 0.0.0.0).

Uvicorn takes one --host, so we open the sockets ourselves. IP_FREEBIND lets us bind the
Tailscale address even if tailscaled hasn't brought it up yet at boot.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import sys

import uvicorn

from . import config

IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)


def bind(host: str, port: int) -> socket.socket:
    addr = ipaddress.ip_address(host)
    if addr.is_unspecified:
        raise SystemExit(f"Refusing to bind {host}: list specific addresses in BIND_HOSTS")
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, IP_FREEBIND, 1)
    else:
        sock.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def main() -> int:
    s = config.settings
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sockets = [bind(h, s.port) for h in s.bind_hosts]
    logging.getLogger("agent_hub").info("Listening on %s port %d", ", ".join(s.bind_hosts), s.port)
    server = uvicorn.Server(uvicorn.Config("app.main:app", log_level="info", proxy_headers=False,
                                           server_header=False))
    server.run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
