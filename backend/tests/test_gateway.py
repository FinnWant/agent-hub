"""The Claude gateway proxy (gateway_proxy.py), run for real against a fake upstream API."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import gateway

REAL = "sk-ant-oat01-REAL-TOKEN-" + "x" * 40
KEY = "sk-ant-oat01-kiln-runkey"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Upstream.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for part in (b"event: a\n\n", b"event: b\n\n"):
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *a):
        pass


@pytest.fixture
def proxy(tmp_path):
    Upstream.seen = []
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    port = free_port()
    env = {"KILN_UPSTREAM_TOKEN": REAL, "KILN_UPSTREAM_HOST": "127.0.0.1",
           "KILN_UPSTREAM_PORT": str(up.server_address[1]), "KILN_UPSTREAM_TLS": "0",
           "KILN_KEYS_DIR": str(tmp_path), "KILN_LISTEN_PORT": str(port), "PATH": os.environ["PATH"]}
    proc = subprocess.Popen([sys.executable, "-I", "-c", gateway.SOURCE], env=env)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    (tmp_path / hashlib.sha256(KEY.encode()).hexdigest()).touch()
    yield port, tmp_path
    proc.terminate()
    proc.wait(5)
    up.shutdown()


def call(port, key=KEY, path="/v1/messages?beta=true", body=b'{"model":"x"}', headers=None, method="POST"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Authorization": f"Bearer {key}", "anthropic-beta": "claude-code-20250219", "Content-Type": "application/json",
         "x-api-key": "junk", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r, r.read()


def test_forwards_with_the_real_token_and_streams_back(proxy):
    port, _ = proxy
    r, body = call(port)
    assert r.status == 200 and body == b"event: a\n\nevent: b\n\n"
    assert r.getheader("Connection") == "close"
    [req] = Upstream.seen
    h = {k.lower(): v for k, v in req["headers"].items()}
    assert req["path"] == "/v1/messages?beta=true" and req["body"] == b'{"model":"x"}'
    assert h["authorization"] == f"Bearer {REAL}" and "x-api-key" not in h
    assert h["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert h["host"] == "127.0.0.1"


def test_refuses_unknown_and_revoked_keys(proxy):
    port, keys = proxy
    r, body = call(port, key="sk-ant-oat01-kiln-guess")
    assert r.status == 401 and "isn't valid" in json.loads(body)["error"]["message"]
    for f in keys.iterdir():
        f.unlink()  # Kiln revokes the key when the run ends
    r, _ = call(port)
    assert r.status == 401
    assert Upstream.seen == []  # nothing reached the API


def test_connectivity_check_and_malformed_requests(proxy):
    port, _ = proxy
    r, _ = call(port, key="", path="/api/hello", body=None, method="HEAD")
    assert r.status == 200
    r, _ = call(port, headers={"Content-Length": str(100 * 2**20)}, body=b"")
    assert r.status == 413
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("POST", "/v1/messages")
    c.putheader("Authorization", f"Bearer {KEY}")
    c.putheader("Transfer-Encoding", "chunked")
    c.endheaders()
    c.send(b"0\r\n\r\n")
    assert c.getresponse().status == 411
    assert Upstream.seen == []


def test_gateway_container_is_locked_down():
    spec = gateway.gateway_spec(REAL)
    assert spec["cap_drop"] == ["ALL"] and spec["read_only"] is True and spec["user"] == "agent"
    assert "no-new-privileges:true" in spec["security_opt"] and spec["pids_limit"] == 64
    assert spec["network"] == "kiln-gateway-net" and "ports" not in spec and "volumes" not in spec
    assert gateway.gateway_spec(REAL)["labels"] == spec["labels"]
    assert gateway.gateway_spec(REAL + "2")["labels"] != spec["labels"]  # new token: new container
