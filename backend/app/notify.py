"""Notifications: one event, two ways out.

* `/api/events` (WebSocket): every open Agent Hub window, including the Windows app, which
  turns events into native notifications.
* Web Push: devices that subscribed (the iPhone home-screen app, desktop browsers), even when
  Agent Hub isn't open. Needs HTTPS (PUBLIC_URL). Payloads are encrypted end to end by the
  Web Push protocol; the VAPID signing key is generated on first use and kept in the data dir.

Events: run finished, run failed/timed out, sandbox stopped unexpectedly or errored.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config
from .db import Database, Run, Sandbox
from .redact import Redactor

log = logging.getLogger(__name__)

# Push services browsers actually use. Subscriptions elsewhere are refused (no SSRF via push).
PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com")


def push_endpoint_ok(endpoint: str) -> bool:
    u = urlsplit(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class VapidKey:
    """P-256 key for signing push requests (RFC 8292), stored as PEM (mode 600)."""

    def __init__(self, path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.path = path
        if not path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.touch(mode=0o600)
            tmp.write_bytes(pem)
            tmp.replace(path)
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        point = key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)
        self.public_key = _b64url(point)  # the browser's applicationServerKey


def run_message(run: Run, sb_name: str) -> dict[str, Any] | None:
    prompt = (run.prompt.strip().splitlines() or [""])[0][:80]
    if run.state == "succeeded":
        extra = f"committed {run.commit_sha[:7]}" if run.commit_sha else "no changes"
        if run.pushed:
            extra += ", pushed"
        return {"kind": "run.finished", "title": f"{sb_name}: run finished", "body": f"{prompt} · {extra}"}
    if run.state in ("failed", "timed_out"):
        what = "timed out" if run.state == "timed_out" else "failed"
        return {"kind": "run.failed", "title": f"{sb_name}: run {what}",
                "body": f"{prompt} · {(run.error or '').splitlines()[0][:120] if run.error else what}"}
    return None  # cancelled by you: no need to tell you


class Notifier:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vapid: VapidKey | None = None
        self._vapid_lock = threading.Lock()
        self.sent: list[dict[str, Any]] = []  # recent events, for tests and debugging

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---- event sources -------------------------------------------------------

    def run_finished(self, run: Run, sb_name: str) -> None:
        msg = run_message(run, sb_name)
        if msg:
            self.publish({**msg, "sandbox": run.sandbox_id, "run": run.id, "state": run.state,
                          "url": f"/sandboxes/{run.sandbox_id}"})

    def sandbox_problem(self, sb: Sandbox, error: str) -> None:
        self.publish({"kind": "sandbox.problem", "title": f"{sb.name}: {error.splitlines()[0][:80]}",
                      "body": "Open Agent Hub to start it again or look at what happened.",
                      "sandbox": sb.id, "url": f"/sandboxes/{sb.id}"})

    # ---- fan-out ---------------------------------------------------------------

    def publish(self, event: dict[str, Any], push: bool = True) -> None:
        """Safe to call from any thread. `push=False` reaches open windows only."""
        event = Redactor.from_settings().obj({**event, "ts": time.time()})
        self.sent = (self.sent + [event])[-50:]
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._fanout, event)
        if push and config.settings.https:
            threading.Thread(target=self.push_all, args=(event,), daemon=True).start()

    def _fanout(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # ---- Web Push --------------------------------------------------------------

    @property
    def vapid(self) -> VapidKey:
        with self._vapid_lock:
            if self._vapid is None:
                self._vapid = VapidKey(Path(config.settings.db_path).parent / "vapid.pem")
            return self._vapid

    def push_all(self, event: dict[str, Any]) -> dict[str, int]:
        payload = json.dumps({k: event.get(k) for k in ("title", "body", "url", "kind")})
        result = {"sent": 0, "removed": 0, "failed": 0}
        for sub in self.db.list_push_subscriptions():
            outcome = self.push_one(sub, payload)
            result[outcome] += 1
        return result

    def push_one(self, sub: dict[str, Any], payload: str) -> str:
        from pywebpush import WebPushException, webpush

        try:
            webpush(subscription_info={"endpoint": sub["endpoint"],
                                       "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                    data=payload, vapid_private_key=str(self.vapid.path),
                    vapid_claims={"sub": config.settings.public_url or "https://agent-hub.invalid"},
                    ttl=6 * 3600, timeout=10)
            self.db.touch_push_subscription(sub["endpoint"], ok=True)
            return "sent"
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):  # the browser dropped this subscription
                self.db.delete_push_subscription(sub["endpoint"])
                return "removed"
            log.warning("push to %s failed: %s", urlsplit(sub["endpoint"]).hostname, status or e)
        except Exception as e:  # noqa: BLE001  (network trouble: try again next time)
            log.warning("push to %s failed: %s", urlsplit(sub["endpoint"]).hostname, e)
        self.db.touch_push_subscription(sub["endpoint"], ok=False)
        return "failed"
