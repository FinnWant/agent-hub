from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from app.terminal import kill_argv, shell_argv, unix_socket_path
GIB = 2**30

ORIGIN = {"origin": "http://testserver"}


def make_sandbox(svc, name="t1"):
    return svc.create(name=name, template="python", repo_url=None, base_branch=None,
                      memory=1 * GIB, cpus=1, ip="test")


def url(sb):
    return f"/api/sandboxes/{sb.id}/terminal?cols=100&rows=30"


def read_until(ws, needle: bytes, limit=50) -> bytes:
    buf = b""
    for _ in range(limit):
        buf += ws.receive_bytes()
        if needle in buf:
            return buf
    raise AssertionError(f"{needle!r} not in {buf!r}")


def actions(svc):
    return [(e["action"], e["ok"]) for e in svc.db.list_audit(limit=50)]


def test_requires_login(client, svc):
    sb = make_sandbox(svc)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(url(sb), headers=ORIGIN):
            pass
    assert e.value.code == 4401


@pytest.mark.parametrize("headers", [{}, {"origin": "http://evil.example"}, {"origin": "null"}])
def test_rejects_cross_origin(admin, svc, headers):
    sb = make_sandbox(svc)
    with pytest.raises(WebSocketDisconnect) as e:
        with admin.websocket_connect(url(sb), headers=headers):
            pass
    assert e.value.code == 4401
    assert svc.open_terminals == 0


def test_shell_session_is_audited(admin, svc, backend):
    sb = make_sandbox(svc)
    with admin.websocket_connect(url(sb), headers=ORIGIN) as ws:
        read_until(ws, b"$ ")
        ws.send_bytes(b"python3 --version\r")
        assert b"Python 3." in read_until(ws, b"Python")
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        ws.send_bytes(b"whoami\r")
        read_until(ws, b"agent")
        assert svc.open_terminals == 1
    term = backend.terminals[-1]
    assert term.size == (120, 40)
    assert term.closed  # the shell is killed when the socket closes
    assert svc.open_terminals == 0
    acts = actions(svc)
    assert ("terminal.open", True) in acts and ("terminal.close", True) in acts
    close = next(e for e in svc.db.list_audit() if e["action"] == "terminal.close")
    assert close["target"] == sb.id and close["detail"]["reason"] == "client closed"


def test_exit_closes_socket(admin, svc):
    sb = make_sandbox(svc)
    with admin.websocket_connect(url(sb), headers=ORIGIN) as ws:
        read_until(ws, b"$ ")
        ws.send_bytes(b"exit\r")
        read_until(ws, b"logout")
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
        assert e.value.code == 1000
    close = next(e for e in svc.db.list_audit() if e["action"] == "terminal.close")
    assert close["detail"]["reason"] == "shell exited"


def test_stopped_sandbox_refused(admin, svc):
    sb = make_sandbox(svc)
    svc.stop(sb.id, "test")
    with pytest.raises(WebSocketDisconnect) as e:
        with admin.websocket_connect(url(sb), headers=ORIGIN) as ws:
            ws.receive_bytes()
    assert e.value.code == 4409
    assert svc.open_terminals == 0


def test_unknown_sandbox(admin):
    with pytest.raises(WebSocketDisconnect) as e:
        with admin.websocket_connect("/api/sandboxes/nope/terminal", headers=ORIGIN) as ws:
            ws.receive_bytes()
    assert e.value.code == 4404


def test_foreign_container_never_gets_a_shell(admin, svc, backend):
    sb = make_sandbox(svc)
    backend.add_foreign_container("f" * 64)
    svc.db.update_sandbox(sb, container_id="f" * 64)  # simulate a tampered row
    with pytest.raises(WebSocketDisconnect) as e:
        with admin.websocket_connect(url(sb), headers=ORIGIN) as ws:
            ws.receive_bytes()
    assert e.value.code in (4409, 4502)
    assert not backend.terminals


def test_terminal_cap(admin, svc, settings, monkeypatch):
    import dataclasses

    from app import config
    monkeypatch.setattr(config, "settings", dataclasses.replace(settings, max_terminals=1))
    sb = make_sandbox(svc)
    with admin.websocket_connect(url(sb), headers=ORIGIN) as first:
        read_until(first, b"$ ")
        with pytest.raises(WebSocketDisconnect) as e:
            with admin.websocket_connect(url(sb), headers=ORIGIN) as second:
                second.receive_bytes()
        assert e.value.code == 4409
    assert ("terminal.open", False) in actions(svc)
    assert svc.open_terminals == 0


def test_shell_helpers():
    tag = "ah-term-0123456789ab"
    assert shell_argv(tag) == ["bash", "-c", f"exec -a {tag} bash -l"]
    assert tag in kill_argv(tag)[2] and "pkill -KILL -s" in kill_argv(tag)[2]
    assert unix_socket_path("unix:///run/user/1001/docker.sock") == "/run/user/1001/docker.sock"
    with pytest.raises(ValueError):
        unix_socket_path("tcp://10.0.0.1:2375")


def test_security_headers_and_ui_fallback(client, settings, monkeypatch, tmp_path):
    import dataclasses

    from app import config
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Kiln</title>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(config, "settings", dataclasses.replace(settings, frontend_dir=tmp_path))
    r = client.get("/sandboxes/abc")  # client-side route -> index.html
    assert r.status_code == 200 and "Kiln" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "immutable" in client.get("/assets/app.js").headers["cache-control"]
    assert client.get("/../../etc/passwd").status_code in (200, 404)  # never escapes the root
    assert "root:" not in client.get("/..%2f..%2fetc/passwd").text
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/sandboxes").headers["cache-control"] == "no-store"
