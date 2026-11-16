"""Kiln's Claude gateway: a tiny HTTP proxy that keeps the real Claude token out of sandboxes.

Runs inside the `kiln-gateway` container (stdlib only; Kiln passes this file's source to
`python3 -c`). Sandboxes reach it at http://kiln-gateway:8080 on their own network and send a
per-run key instead of the token. A request is forwarded only if the SHA-256 of its key is a
file in KEYS_DIR (Kiln adds it when a run starts and removes it when the run ends); the key is
then replaced with the real token and the request goes to the Anthropic API, and nowhere else.

One request per connection: the response is relayed as it streams and the connection closed.

Metering: responses are requested uncompressed, and the token usage of each Messages API call
(from the JSON body, or the `message_start` / `message_delta` events of a stream) is appended
to USAGE_DIR/<key digest>.jsonl, which Kiln reads. The relayed bytes are never changed.

Holds: while KEYS_DIR/<key digest>.hold exists (Kiln creates it when a run is over its budget),
that key's new requests wait instead of going upstream, until Kiln removes the hold or the key.
"""

import asyncio
import hashlib
import json
import os
import ssl
import time

TOKEN = os.environ.pop("KILN_UPSTREAM_TOKEN", "")
UP_HOST = os.environ.get("KILN_UPSTREAM_HOST", "api.anthropic.com")
UP_PORT = int(os.environ.get("KILN_UPSTREAM_PORT", "443"))
UP_TLS = os.environ.get("KILN_UPSTREAM_TLS", "1") == "1"
KEYS_DIR = os.environ.get("KILN_KEYS_DIR", "/keys")
LISTEN_PORT = int(os.environ.get("KILN_LISTEN_PORT", "8080"))
USAGE_DIR = os.environ.get("KILN_USAGE_DIR", "/tmp/usage")
HOLD_MAX = 3 * 3600  # seconds a held request waits before giving up
OAUTH_BETA = "oauth-2025-04-20"
MAX_HEAD = 64 * 1024
MAX_BODY = 64 * 2**20
IDLE = 600  # seconds without data before a stalled stream is dropped
DROP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "transfer-encoding", "te",
        "trailer", "upgrade", "host", "authorization", "x-api-key", "content-length", "anthropic-beta",
        "expect", "accept-encoding"}
METERED_USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

_ctx = ssl.create_default_context() if UP_TLS else None
_slots = asyncio.Semaphore(16)


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest() if key and len(key) <= 256 else ""


def key_ok(key: str) -> bool:
    digest = key_digest(key)
    return bool(digest) and os.path.isfile(os.path.join(KEYS_DIR, digest))


class Meter:
    """Reads the token usage out of a relayed response (without changing it): an incremental
    chunked-transfer decoder, then server-sent events line by line, or a JSON body."""

    MAX_JSON = 8 * 2**20

    def __init__(self, chunked: bool, sse: bool) -> None:
        self.chunked, self.sse = chunked, sse
        self.raw = b""        # undecoded chunked framing
        self.left = 0         # bytes left in the current chunk (+2 for its CRLF while negative)
        self.text = b""       # decoded body not yet consumed
        self.usage: dict = {}
        self.cache_5m = self.cache_1h = 0
        self.done = False     # the chunked body has ended (the upstream may keep the connection)

    def feed(self, data: bytes) -> None:
        if not self.chunked:
            self._body(data)
            return
        self.raw += data
        while self.raw:
            if self.left > 0:
                take = self.raw[:self.left]
                self.raw, self.left = self.raw[len(take):], self.left - len(take)
                self._body(take)
                if self.left == 0:
                    self.left = -2  # the CRLF after the chunk
                continue
            if self.left < 0:
                drop = min(-self.left, len(self.raw))
                self.raw, self.left = self.raw[drop:], self.left + drop
                continue
            line, sep, rest = self.raw.partition(b"\r\n")
            if not sep:
                return
            self.raw = rest
            try:
                self.left = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                self.raw = b""
                return
            if self.left == 0:
                self.raw = b""
                self.done = True
                return

    def _body(self, data: bytes) -> None:
        self.text += data
        if not self.sse:
            if len(self.text) > self.MAX_JSON:
                self.text = b""
                self.sse = None  # too big to look at; never mind
            return
        if self.sse is None:
            return
        *lines, self.text = self.text.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                try:
                    self._event(json.loads(line[5:]))
                except ValueError:
                    pass

    def _event(self, ev: dict) -> None:
        if ev.get("type") == "message_start":
            self._take((ev.get("message") or {}).get("usage") or {})
        elif ev.get("type") == "message_delta":
            self._take(ev.get("usage") or {})

    def _take(self, u: dict) -> None:
        for k in METERED_USAGE:  # counts are cumulative: the largest seen is the total
            if isinstance(u.get(k), int):
                self.usage[k] = max(self.usage.get(k, 0), u[k])
        cc = u.get("cache_creation") or {}
        self.cache_5m = max(self.cache_5m, int(cc.get("ephemeral_5m_input_tokens") or 0))
        self.cache_1h = max(self.cache_1h, int(cc.get("ephemeral_1h_input_tokens") or 0))

    def finish(self) -> dict:
        if not self.sse and self.sse is not None and self.text:
            try:
                self._take(json.loads(self.text).get("usage") or {})
            except (ValueError, AttributeError):
                pass
        return self.usage


def record_usage(digest: str, model: str, status: int, meter: Meter) -> None:
    usage = meter.finish()
    if not usage or not digest:
        return
    rec = {"t": round(time.time(), 3), "m": model[:80], "s": status,
           "in": usage.get("input_tokens", 0), "cr": usage.get("cache_read_input_tokens", 0),
           "cw": usage.get("cache_creation_input_tokens", 0), "cw1h": meter.cache_1h,
           "out": usage.get("output_tokens", 0)}
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        with open(os.path.join(USAGE_DIR, digest + ".jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


async def wait_while_held(digest: str, key: str) -> str | None:
    """None to go ahead, or why not."""
    hold = os.path.join(KEYS_DIR, digest + ".hold")
    start = time.monotonic()
    while os.path.exists(hold):
        if not key_ok(key):
            return "This run's key isn't valid (the run has ended or Kiln restarted)"
        if time.monotonic() - start > HOLD_MAX:
            return "This run is paused by its owner (over its budget)"
        await asyncio.sleep(1)
    return None


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
        key = presented_key(headers)
        if not key_ok(key):
            return await reply(w, 401, "This run's key isn't valid (the run has ended or Kiln restarted)")
        body = await asyncio.wait_for(r.readexactly(length), 120)
        digest = key_digest(key)
        if (why := await wait_while_held(digest, key)) is not None:
            return await reply(w, 401 if "key" in why else 503, why)
        path = target.split("?")[0]
        model = ""
        if method == "POST" and path == "/v1/messages":  # metered: the Messages API, not count_tokens
            try:
                model = str(json.loads(body).get("model") or "")
            except (ValueError, AttributeError):
                model = "?"

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
        out.append("Accept-Encoding: identity")  # so the usage can be read

        async with _slots:
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(UP_HOST, UP_PORT, ssl=_ctx, server_hostname=UP_HOST if _ctx else None,
                                        limit=MAX_HEAD), 30)
            meter: Meter | None = None
            rlines: list[str] = []
            try:
                uw.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
                await uw.drain()
                rhead = await asyncio.wait_for(ur.readuntil(b"\r\n\r\n"), IDLE)
                rlines = rhead.decode("latin-1").split("\r\n")
                keep = [rlines[0]] + [x for x in rlines[1:] if x and
                                      x.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive")]
                w.write(("\r\n".join(keep + ["Connection: close"]) + "\r\n\r\n").encode("latin-1"))
                await w.drain()
                rheaders = {x.split(":", 1)[0].strip().lower(): x.split(":", 1)[1].strip()
                            for x in rlines[1:] if ":" in x}
                meter = None
                if model and "content-encoding" not in rheaders:
                    meter = Meter("chunked" in rheaders.get("transfer-encoding", "").lower(),
                                  "text/event-stream" in rheaders.get("content-type", ""))
                while chunk := await asyncio.wait_for(ur.read(65536), IDLE):
                    w.write(chunk)
                    if meter is not None:
                        meter.feed(chunk)
                    await w.drain()
                    if meter is not None and meter.done:
                        break
            finally:
                uw.close()
                if meter is not None:
                    try:
                        status = int(rlines[0].split(" ")[1])
                    except (IndexError, ValueError):
                        status = 0
                    record_usage(digest, model, status, meter)
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
