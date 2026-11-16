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
    parts: list[bytes] = [b"event: a\n\n", b"event: b\n\n"]   # streamed, one chunk each
    json_body: bytes | None = None                                # instead: a plain JSON response

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Upstream.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        if Upstream.json_body is not None:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(Upstream.json_body)))
            self.end_headers()
            self.wfile.write(Upstream.json_body)
            return
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for part in Upstream.parts:
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *a):
        pass


@pytest.fixture
def proxy(tmp_path):
    Upstream.seen = []
    Upstream.parts = [b"event: a\n\n", b"event: b\n\n"]
    Upstream.json_body = None
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    port = free_port()
    env = {"KILN_UPSTREAM_TOKEN": REAL, "KILN_UPSTREAM_HOST": "127.0.0.1",
           "KILN_UPSTREAM_PORT": str(up.server_address[1]), "KILN_UPSTREAM_TLS": "0",
           "KILN_KEYS_DIR": str(tmp_path), "KILN_LISTEN_PORT": str(port), "PATH": os.environ["PATH"],
           "KILN_USAGE_DIR": str(tmp_path / "usage")}
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


# ---------------------------------------------------------------------------
# Metering and holds
# ---------------------------------------------------------------------------

DIGEST = hashlib.sha256(KEY.encode()).hexdigest()


def usage_lines(keys, expect=1):
    """The proxy writes a call's usage once the upstream response has ended, which can be just
    after the client has read the last chunk: wait for it."""
    f = keys / "usage" / f"{DIGEST}.jsonl"
    for _ in range(100):
        lines = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        if len(lines) >= expect:
            break
        time.sleep(0.05)
    time.sleep(0.1)  # and nothing more arrives
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def sse(*events):
    return b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_streamed_usage_is_metered_even_split_across_chunks(proxy):
    port, keys = proxy
    stream = sse({"type": "message_start", "message": {"usage": {
                     "input_tokens": 12, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 400,
                     "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 400},
                     "output_tokens": 1}}},
                 {"type": "content_block_delta", "delta": {"text": "hi"}},
                 {"type": "message_delta", "usage": {"output_tokens": 57}})
    Upstream.parts = [stream[i:i + 7] for i in range(0, len(stream), 7)]  # lines cut mid-way
    r, body = call(port, body=b'{"model":"claude-opus-5-5","stream":true}')
    assert r.status == 200 and body == stream  # relayed unchanged
    [u] = usage_lines(keys)
    assert (u["m"], u["in"], u["cr"], u["cw"], u["cw1h"], u["out"], u["s"]) == \
        ("claude-opus-5-5", 12, 3000, 400, 400, 57, 200)
    h = {k.lower(): v for k, v in Upstream.seen[0]["headers"].items()}
    assert h["accept-encoding"] == "identity"


def test_json_usage_is_metered_and_count_tokens_is_not(proxy):
    port, keys = proxy
    Upstream.json_body = json.dumps({"usage": {"input_tokens": 5, "output_tokens": 9}}).encode()
    call(port, body=b'{"model":"claude-haiku-5-5"}', headers={"Accept-Encoding": "gzip"})
    call(port, path="/v1/messages/count_tokens", body=b'{"model":"claude-haiku-5-5"}')
    [u] = usage_lines(keys)
    assert u["m"] == "claude-haiku-5-5" and u["in"] == 5 and u["out"] == 9


def test_a_hold_makes_requests_wait_until_lifted(proxy):
    port, keys = proxy
    hold = keys / f"{DIGEST}.hold"
    hold.touch()
    got = {}
    t = threading.Thread(target=lambda: got.update(r=call(port)[0].status))
    t.start()
    time.sleep(1.5)
    assert Upstream.seen == [] and "r" not in got  # waiting
    hold.unlink()
    t.join(10)
    assert got["r"] == 200 and len(Upstream.seen) == 1


def test_a_held_request_ends_when_the_key_is_revoked(proxy):
    port, keys = proxy
    (keys / f"{DIGEST}.hold").touch()
    got = {}
    t = threading.Thread(target=lambda: got.update(r=call(port)[0].status))
    t.start()
    time.sleep(0.5)
    (keys / DIGEST).unlink()  # the run ended (e.g. the owner stopped it)
    t.join(10)
    assert got["r"] == 401 and Upstream.seen == []
