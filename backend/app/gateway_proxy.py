"""Kiln's Claude gateway: a tiny HTTP proxy that keeps the real Claude token out of sandboxes.

Runs inside the `kiln-gateway` container (stdlib only; Kiln passes this file's source to
`python3 -c`). Sandboxes reach it at http://kiln-gateway:8080 on their own network and send a
per-run key instead of the token. A request is forwarded only if the SHA-256 of its key is a
file in KEYS_DIR (Kiln adds it when a run starts and removes it when the run ends); the key is
then replaced with the real token and the request goes to the Anthropic API, and nowhere else.

One request per connection: the response is relayed as it streams and the connection closed.
"""

import asyncio
import hashlib
import json
import os
import ssl

TOKEN = os.environ.pop("KILN_UPSTREAM_TOKEN", "")
UP_HOST = os.environ.get("KILN_UPSTREAM_HOST", "api.anthropic.com")
UP_PORT = int(os.environ.get("KILN_UPSTREAM_PORT", "443"))
UP_TLS = os.environ.get("KILN_UPSTREAM_TLS", "1") == "1"
KEYS_DIR = os.environ.get("KILN_KEYS_DIR", "/keys")
LISTEN_PORT = int(os.environ.get("KILN_LISTEN_PORT", "8080"))
OAUTH_BETA = "oauth-2025-04-20"
MAX_HEAD = 64 * 1024
MAX_BODY = 64 * 2**20
IDLE = 600  # seconds without data before a stalled stream is dropped
DROP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "transfer-encoding", "te",
        "trailer", "upgrade", "host", "authorization", "x-api-key", "content-length", "anthropic-beta",
        "expect"}

_ctx = ssl.create_default_context() if UP_TLS else None
_slots = asyncio.Semaphore(16)


def key_ok(key: str) -> bool:
    if not key or len(key) > 256:
        return False
    digest = hashlib.sha256(key.encode()).hexdigest()
    return os.path.isfile(os.path.join(KEYS_DIR, digest))


def presented_key(headers: dict) -> str:
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return headers.get("x-api-key", "").strip()


async def reply(w, status: int, message: str) -> None:
    body = json.dumps({"type": "error", "error": {"type": "kiln_gateway", "message": message}}).encode()
    w.write(f"HTTP/1.1 {status} Kiln gateway\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await w.drain()


async def handle(r, w) -> None:
    try:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 60)
        except (asyncio.LimitOverrunError, ValueError):
            return await reply(w, 431, "Request headers too large")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[1].startswith("/") or not parts[2].startswith("HTTP/1."):
            return await reply(w, 400, "Bad request")
        method, target, _ = parts
        pairs = []
        for line in lines[1:]:
            if line:
                k, _, v = line.partition(":")
                pairs.append((k.strip(), v.strip()))
        headers = {k.lower(): v for k, v in pairs}
        if target.split("?")[0] == "/api/hello":  # Claude Code's connectivity check (no key sent)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return await w.drain()
        if "transfer-encoding" in headers:
            return await reply(w, 411, "Send a Content-Length")
        try:
            length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return await reply(w, 400, "Bad Content-Length")
        if length < 0 or length > MAX_BODY:
            return await reply(w, 413, "Request too large")
        if not key_ok(presented_key(headers)):
            return await reply(w, 401, "This run's key isn't valid (the run has ended or Kiln restarted)")
        body = await asyncio.wait_for(r.readexactly(length), 120)

        betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
        out = [f"{method} {target} HTTP/1.1", f"Host: {UP_HOST}", f"Content-Length: {length}", "Connection: close"]
        if TOKEN.startswith("sk-ant-api"):
            out.append(f"x-api-key: {TOKEN}")
        else:
            out.append(f"Authorization: Bearer {TOKEN}")
            if OAUTH_BETA not in betas:
                betas.append(OAUTH_BETA)
        if betas:
            out.append("anthropic-beta: " + ",".join(betas))
        out += [f"{k}: {v}" for k, v in pairs if k.lower() not in DROP]

        async with _slots:
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(UP_HOST, UP_PORT, ssl=_ctx, server_hostname=UP_HOST if _ctx else None,
                                        limit=MAX_HEAD), 30)
            try:
                uw.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
                await uw.drain()
                rhead = await asyncio.wait_for(ur.readuntil(b"\r\n\r\n"), IDLE)
                rlines = rhead.decode("latin-1").split("\r\n")
                keep = [rlines[0]] + [x for x in rlines[1:] if x and
                                      x.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive")]
                w.write(("\r\n".join(keep + ["Connection: close"]) + "\r\n\r\n").encode("latin-1"))
                await w.drain()
                while chunk := await asyncio.wait_for(ur.read(65536), IDLE):
                    w.write(chunk)
                    await w.drain()
            finally:
                uw.close()
    except (OSError, asyncio.IncompleteReadError, TimeoutError, ssl.SSLError):
        pass
    finally:
        try:
            w.close()
        except OSError:
            pass


async def main() -> None:
    if not TOKEN:
        raise SystemExit("KILN_UPSTREAM_TOKEN is not set")
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT, limit=MAX_HEAD)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
