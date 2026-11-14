from __future__ import annotations

import dataclasses

import pytest

from app import auth, config
from tests.conftest import CSRF, PASSWORD

PROTECTED = [
    ("get", "/api/auth/me"), ("post", "/api/auth/logout"), ("get", "/api/system"),
    ("get", "/api/templates"), ("get", "/api/sandboxes"), ("post", "/api/sandboxes"),
    ("get", "/api/sandboxes/abc"), ("post", "/api/sandboxes/abc/start"),
    ("post", "/api/sandboxes/abc/stop"), ("delete", "/api/sandboxes/abc"),
    ("post", "/api/sandboxes/abc/exec"), ("get", "/api/audit"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_endpoints_need_a_session(client, method, path):
    r = getattr(client, method)(path, headers=CSRF)
    assert r.status_code == 401


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_fails_closed_without_password(client, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, admin_password_hash=""))
    assert client.get("/api/sandboxes").status_code == 503
    assert client.post("/api/auth/login", json={"password": "x"}, headers=CSRF).status_code == 503


def test_login_sets_strict_httponly_cookie(client):
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/auth/me").status_code == 200


def test_wrong_password(client):
    r = client.post("/api/auth/login", json={"password": "nope"}, headers=CSRF)
    assert r.status_code == 401


def test_mutations_need_csrf_header(admin):
    assert admin.post("/api/sandboxes", json={"name": "x", "template": "python"}).status_code == 403
    assert admin.post("/api/auth/login", json={"password": PASSWORD}).status_code == 403


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"password": "nope"}, headers=CSRF)
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_new_password_invalidates_sessions(admin, monkeypatch):
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, admin_password_hash=auth.hash_password("another one!")))
    assert admin.get("/api/auth/me").status_code == 401


def test_expired_or_forged_tokens_rejected(settings):
    token, _ = auth.make_token()
    assert auth.token_valid(token)
    expiry, _, sig = token.partition(".")
    assert not auth.token_valid(f"{int(expiry) + 1}.{sig}")
    assert not auth.token_valid("1.abc")
    assert not auth.token_valid(None)


def test_logins_are_audited(client, svc):
    client.post("/api/auth/login", json={"password": "nope"}, headers=CSRF)
    client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    entries = svc.db.list_audit()
    assert [(e["action"], e["ok"]) for e in entries] == [("auth.login", True), ("auth.login", False)]


class _WS:
    def __init__(self, origin, host, token):
        self.headers = {"host": host, **({"origin": origin} if origin else {})}
        self.cookies = {auth.COOKIE: token} if token else {}


def test_websocket_origin_check(settings):
    token, _ = auth.make_token()
    assert auth.ws_allowed(_WS("http://hub:8090", "hub:8090", token))
    assert not auth.ws_allowed(_WS("http://evil.example", "hub:8090", token))
    assert not auth.ws_allowed(_WS(None, "hub:8090", token))
    assert not auth.ws_allowed(_WS("http://hub:8090", "hub:8090", None))
