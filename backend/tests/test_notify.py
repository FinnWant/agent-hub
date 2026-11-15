"""HTTPS/proxy handling, the notification event stream and Web Push."""

from __future__ import annotations

import dataclasses
import json
import os
import time

import pytest
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from app import auth, config
from app import notify as notify_mod
from app.db import Run
from app.notify import Notifier, VapidKey, push_endpoint_ok, run_message
from tests.conftest import CSRF, PASSWORD

APPLE = "https://web.push.apple.com/QGuNh1qF5XqNRk"
SUB = {"endpoint": APPLE, "keys": {"p256dh": "B" + "A" * 86, "auth": "c" * 22}, "label": "iPhone"}


def req(peer: str, headers: dict[str, str]) -> Request:
    return Request({"type": "http", "client": (peer, 1234), "headers": [(k.lower().encode(), v.encode())
                                                                         for k, v in headers.items()]})


@pytest.fixture
def secure_client(client, https):
    client.base_url = "https://testserver"  # Secure cookies are only sent over https
    return client


@pytest.fixture
def https(settings, monkeypatch, tmp_path):
    s = dataclasses.replace(settings, public_url="https://box.tailnet.ts.net", trust_proxy=True,
                            db_path=tmp_path / "x.db")
    monkeypatch.setattr(config, "settings", s)
    return s


# ---------------------------------------------------------------------------
# Behind tailscale serve
# ---------------------------------------------------------------------------

def test_client_identity_behind_the_proxy(https):
    proxied = req("127.0.0.1", {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": "me@example.com"})
    assert auth.client_ip(proxied) == "100.64.0.7 (me@example.com)"
    # Headers from anywhere but the local proxy are ignored (can't be spoofed remotely).
    direct = req("100.64.0.9", {"x-forwarded-for": "1.2.3.4", "tailscale-user-login": "evil@example.com"})
    assert auth.client_ip(direct) == "100.64.0.9"


def test_client_identity_without_proxy_trust(settings):
    assert auth.client_ip(req("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})) == "127.0.0.1"


def test_secure_cookie_and_hsts_on_https(client, https):
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    assert "secure" in r.headers["set-cookie"].lower()
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_websocket_origin_matches_public_url_even_if_host_is_rewritten(secure_client):
    client = secure_client
    client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    cookie = f"ah_session={client.cookies.get('ah_session')}"  # the ws test client is plain ws://
    with client.websocket_connect("/api/events", headers={"origin": "https://box.tailnet.ts.net",
                                                          "host": "127.0.0.1:8090", "cookie": cookie}) as ws:
        pass  # accepted
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/events", headers={"origin": "https://evil.example", "host": "127.0.0.1:8090",
                                                              "cookie": cookie}):
            pass
    assert e.value.code == 4401


def test_csp_allows_service_worker_and_app_bridge(client):
    csp = client.get("/api/health").headers["content-security-policy"]
    assert "worker-src 'self'" in csp and "http://ipc.localhost" in csp and "script-src 'self';" in csp


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def run(state, **kw):
    return Run(id="0123456789ab", sandbox_id="s1", runner="claude-code", prompt="Add a README section\nmore",
               timeout=1, max_turns=1, state=state, **kw)


def test_run_messages():
    ok = run_message(run("succeeded", commit_sha="abcdef1234", pushed=True), "api")
    assert ok["kind"] == "run.finished" and ok["title"] == "api: run finished"
    assert ok["body"] == "Add a README section · committed abcdef1, pushed"
    assert run_message(run("succeeded"), "api")["body"].endswith("no changes")
    bad = run_message(run("timed_out", error="Time limit reached"), "api")
    assert bad["kind"] == "run.failed" and "timed out" in bad["title"] and "Time limit" in bad["body"]
    assert run_message(run("cancelled"), "api") is None


def test_run_finish_reaches_open_windows(admin):
    sb = admin.post("/api/sandboxes", json={"name": "api", "template": "python"}, headers=CSRF).json()
    with admin.websocket_connect("/api/events", headers={"origin": "http://testserver"}) as ws:
        admin.post(f"/api/sandboxes/{sb['id']}/runs", json={"prompt": "Add docs"}, headers=CSRF)
        got = []
        deadline = time.monotonic() + 10
        while not got and time.monotonic() < deadline:
            got = ws.receive_json()["events"]
    assert got[0]["kind"] == "run.finished" and got[0]["title"] == "api: run finished"
    assert got[0]["url"] == f"/sandboxes/{sb['id']}"


def test_sandbox_dying_on_its_own_is_reported_but_your_stop_is_not(admin, svc, backend):
    import app.main as main

    a = admin.post("/api/sandboxes", json={"name": "oomy", "template": "python", "memory": "1g"}, headers=CSRF).json()
    b = admin.post("/api/sandboxes", json={"name": "calm", "template": "python", "memory": "1g"}, headers=CSRF).json()
    notifier = main.app.state.notifier
    cid = svc.db.get_sandbox(a["id"]).container_id
    backend.containers[cid].update(status="exited", oom=True, exit_code=137)
    sb = admin.get(f"/api/sandboxes/{a['id']}").json()
    assert sb["state"] == "stopped" and "too much memory" in sb["error"]
    admin.post(f"/api/sandboxes/{b['id']}/stop", headers=CSRF)
    admin.get("/api/sandboxes")
    kinds = [(e["kind"], e["sandbox"]) for e in notifier.sent]
    assert kinds == [("sandbox.problem", a["id"])]
    assert "too much memory" in notifier.sent[0]["title"]


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------

def test_push_endpoint_allowlist():
    assert push_endpoint_ok(APPLE)
    assert push_endpoint_ok("https://fcm.googleapis.com/fcm/send/abc")
    assert push_endpoint_ok("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert push_endpoint_ok("https://wns2-par02p.notify.windows.com/w/?token=x")
    for bad in ("http://web.push.apple.com/x", "https://evil.example/x", "https://push.apple.com.evil.example/x",
                "https://169.254.169.254/latest", "https://localhost:8090/api/x"):
        assert not push_endpoint_ok(bad), bad


def test_vapid_key_is_private_and_stable(tmp_path):
    k = VapidKey(tmp_path / "vapid.pem")
    assert oct(os.stat(tmp_path / "vapid.pem").st_mode & 0o777) == "0o600"
    assert len(k.public_key) == 87 and VapidKey(tmp_path / "vapid.pem").public_key == k.public_key


def test_push_needs_https(admin):
    assert admin.get("/api/push").json()["available"] is False
    assert admin.post("/api/push/subscribe", json=SUB, headers=CSRF).status_code == 400


def test_subscribe_send_and_expire(secure_client, monkeypatch):
    client = secure_client
    sent = []

    class Gone:
        status_code = 410

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, ttl, timeout):
        assert vapid_claims["sub"] == "https://box.tailnet.ts.net" and os.path.exists(vapid_private_key)
        if subscription_info["endpoint"].endswith("gone"):
            from pywebpush import WebPushException
            raise WebPushException("gone", response=Gone())
        sent.append((subscription_info["endpoint"], json.loads(data)))

    monkeypatch.setattr("pywebpush.webpush", fake_webpush)
    client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    info = client.get("/api/push").json()
    assert info["available"] is True and len(info["public_key"]) == 87
    assert client.post("/api/push/subscribe", json={**SUB, "endpoint": "https://evil.example/x"},
                       headers=CSRF).status_code == 400
    assert client.post("/api/push/subscribe", json=SUB, headers=CSRF).status_code == 201
    assert client.post("/api/push/subscribe", json={**SUB, "endpoint": APPLE + "gone"}, headers=CSRF).status_code == 201
    r = client.post("/api/push/test", headers=CSRF).json()
    assert r == {"sent": 1, "removed": 1, "failed": 0}
    assert sent[0][1] == {"title": "Agent Hub", "body": "Notifications work on this device.", "url": "/", "kind": "test"}
    assert [d["label"] for d in client.get("/api/push").json()["devices"]] == ["iPhone"]
    assert client.post("/api/push/unsubscribe", json={"endpoint": APPLE}, headers=CSRF).json() == {"removed": True}
    assert client.get("/api/push").json()["devices"] == []


def test_push_payload_is_redacted(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token="sk-ant-oat01-" + "S" * 40, db_path=tmp_path / "d.db"))
    from app.db import Database

    n = Notifier(Database(":memory:"))
    n.publish({"kind": "run.failed", "title": "x", "body": "leaked sk-ant-oat01-" + "S" * 40})
    assert "SSSS" not in json.dumps(n.sent) and "[redacted]" in n.sent[0]["body"]
    assert notify_mod  # module imported for monkeypatching elsewhere
