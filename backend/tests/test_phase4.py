"""Files, diff, previews, prune, resource monitoring and disk checks."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from app import config, files, preview
from app.db import Run, Sandbox
from app.docker_backend import MANAGED_LABEL, SANDBOX_LABEL, ExecResult, container_spec
from app.monitor import Monitor
from app.templates import TEMPLATES
from tests.conftest import CSRF, GIB, PASSWORD


# ---------------------------------------------------------------------------
# A backend that runs commands locally, with /workspace mapped to a temp dir
# ---------------------------------------------------------------------------

class MappedBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ws = root / "workspace"
        self.ws.mkdir(parents=True)
        (root / "tmp").mkdir()

    def map(self, s: str) -> str:
        return s.replace("/tmp/ah-", f"{self.root}/tmp/ah-").replace("/workspace", str(self.ws))

    def exec(self, container_id, cmd, timeout, env=None):
        argv = [self.map(a) for a in cmd]
        e = {"PATH": os.environ["PATH"], "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             **{k: self.map(v) for k, v in (env or {}).items()}}
        r = subprocess.run(argv, cwd=self.ws, env=e, capture_output=True, timeout=timeout)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        return ExecResult(exit_code=r.returncode, output=out, truncated=False, timed_out=False)

    def get_file(self, container_id, path, max_size):
        data = Path(self.map(path)).read_bytes()
        assert len(data) <= max_size
        return data

    def get_file_to(self, container_id, path, dest, max_size):
        data = self.get_file(container_id, path, max_size)
        Path(dest).write_bytes(data)
        return len(data)


@pytest.fixture
def box(tmp_path):
    return MappedBackend(tmp_path)


def sandbox(**kw):
    return Sandbox(id="s1", name="s", template="python", volume="v", memory=GIB, cpus=1, state="running",
                   container_id="c1", **kw)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def test_paths_stay_in_workspace():
    assert files.resolve("") == "/workspace"
    assert files.resolve("/src/../README.md") == "/workspace/README.md"
    for bad in ("..", "../etc/passwd", "/../../etc", "src/../../root"):
        with pytest.raises(files.PathError):
            files.resolve(bad)


def test_browse_directory_text_binary_large_and_links(box):
    (box.ws / "src").mkdir()
    (box.ws / "README.md").write_text("# Hi\nünïcode\n")
    (box.ws / "blob.bin").write_bytes(b"\x00\x01\x02")
    (box.ws / "big.txt").write_bytes(b"x" * (files.MAX_FILE + 1))
    (box.ws / "weird name\twith tab").write_text("t")
    os.symlink("/etc/passwd", box.ws / "link")
    listing = files.browse(box, sandbox(), "")
    names = [(e["name"], e["type"]) for e in listing["entries"]]
    assert names[0] == ("src", "dir") and ("README.md", "file") in names and ("link", "link") in names
    assert ("weird name\twith tab", "file") in names
    assert next(e for e in listing["entries"] if e["name"] == "link")["target"] == "/etc/passwd"
    readme = files.browse(box, sandbox(), "README.md")
    assert readme["kind"] == "file" and readme["content"] == "# Hi\nünïcode\n"
    assert files.browse(box, sandbox(), "blob.bin")["binary"] is True
    assert files.browse(box, sandbox(), "big.txt")["too_large"] is True
    link = files.browse(box, sandbox(), "link")
    assert link["kind"] == "link" and "content" not in link  # never follows a link out
    with pytest.raises(files.PathError, match="No such"):
        files.browse(box, sandbox(), "missing.txt")


def test_run_diff(box):
    git("init", "-q", "-b", "main", cwd=box.ws)
    (box.ws / "README.md").write_text("# Project\n")
    (box.ws / "old.txt").write_text("bye\n")
    git("add", "-A", cwd=box.ws)
    git("commit", "-q", "-m", "base", cwd=box.ws)
    git("checkout", "-q", "-b", "agent/0123456789ab", cwd=box.ws)
    (box.ws / "README.md").write_text("# Project\n\n## About\nIt does things.\n")
    (box.ws / "old.txt").unlink()
    (box.ws / "img.bin").write_bytes(b"\x00\xff" * 10)
    git("add", "-A", cwd=box.ws)
    git("commit", "-q", "-m", "agent", cwd=box.ws)
    git("checkout", "-q", "main", cwd=box.ws)
    (box.ws / "later-on-main.txt").write_text("not part of the run\n")
    git("add", "-A", cwd=box.ws)
    git("commit", "-q", "-m", "main moves on", cwd=box.ws)

    run = Run(id="0123456789ab", sandbox_id="s1", runner="x", prompt="p", timeout=1, max_turns=1,
              branch="agent/0123456789ab", base_branch="main")
    d = files.run_diff(box, sandbox(), run)
    by = {f["path"]: f for f in d["files"]}
    assert set(by) == {"README.md", "old.txt", "img.bin"}  # three-dot: main's later commit excluded
    assert by["README.md"]["added"] == 3 and by["old.txt"]["removed"] == 1 and by["img.bin"]["added"] is None
    assert "+## About" in d["patch"] and "later-on-main" not in d["patch"]
    # The agent can't hide changes from review: attributes in its repo don't apply.
    (box.ws / ".git" / "info").mkdir(exist_ok=True)
    (box.ws / ".git" / "info" / "attributes").write_text("*.md -diff\n")
    assert "+## About" in files.run_diff(box, sandbox(), run)["patch"]
    assert not list((box.root / "tmp").iterdir())  # temp patch cleaned up
    with pytest.raises(Exception, match="no longer exists"):
        files.run_diff(box, sandbox(), dataclasses.replace(run, branch="agent/ffffffffffff"))
    with pytest.raises(Exception, match="Unexpected"):
        files.run_diff(box, sandbox(), dataclasses.replace(run, base_branch="--output=/etc/x"))


def test_python_preview_server_hides_dotfiles(tmp_path):
    import socket
    import sys
    import time
    import urllib.request
    from urllib.error import HTTPError

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    (tmp_path / ".env").write_text("K=1")
    (tmp_path / "index.html").write_text("hi")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.txt").write_text("p")
    (tmp_path / "docs" / ".secret").write_text("s")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = preview.PY_SERVER.replace('"/workspace"', repr(str(tmp_path)))
    proc = subprocess.Popen([sys.executable, "-I", "-c", script, str(port)])
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        get = lambda p: urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=5).read().decode()
        assert get("/index.html") == "hi"
        listing = get("/docs/")
        assert "page.txt" in listing and ".secret" not in listing
        for p in ("/.git/config", "/.env", "/%2egit/config", "/x/../.git/config", "/docs/.secret"):
            with pytest.raises(HTTPError) as e:
                get(p)
            assert e.value.code == 404, p
    finally:
        proc.terminate()
        proc.wait(5)


def test_files_api_requires_running_sandbox(admin, backend):
    sb = admin.post("/api/sandboxes", json={"name": "f", "template": "python"}, headers=CSRF).json()
    assert admin.get(f"/api/sandboxes/{sb['id']}/files", params={"path": "../.."}).status_code == 400
    admin.post(f"/api/sandboxes/{sb['id']}/stop", headers=CSRF)
    assert admin.get(f"/api/sandboxes/{sb['id']}/files").status_code == 409


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------

PROC_NET = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1
   1: 0100007F:1435 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2 1
   2: 0100007F:0BB8 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 3 1
   0: 00000000000000000000000000000000:0BB8 00000000000000000000000000000000:0000 0A 0 0 0 1000 0 4 1
"""


def test_static_server_commands_match_their_stop_pattern():
    import re

    for template in ("python", "node", "static"):
        cmd = preview.server_cmd(template, 8000)
        assert re.search(preview.server_pattern(8000), cmd + " "), template
        assert ("0.0.0.0" in cmd and "/workspace" in cmd) or cmd.startswith("python3 -I /tmp/kiln-preview.py")
        assert not re.search(preview.server_pattern(8000), " ".join(preview.stop_argv(8000)))  # never itself


def test_listener_detection():
    assert preview.listeners(PROC_NET, 8000) == {"any"}        # 0.0.0.0:8000
    assert preview.listeners(PROC_NET, 5173) == {"loopback"}   # 127.0.0.1:5173 only
    assert preview.listeners(PROC_NET, 3000) == {"any"}        # [::]:3000 (the 127.0.0.1 line is a connection)
    assert preview.listeners(PROC_NET, 9999) == set()


@pytest.fixture
def previews(settings, monkeypatch):
    s = dataclasses.replace(settings, preview_bind="100.64.0.1", preview_host="box.tailnet.ts.net.",
                            preview_port_range="8200-8201")
    monkeypatch.setattr(config, "settings", s)
    return s


def test_preview_ports_are_allocated_and_published_on_tailscale_ip_only(admin, backend, previews):
    a = admin.post("/api/sandboxes", json={"name": "a", "template": "python", "memory": "512m"}, headers=CSRF).json()
    b = admin.post("/api/sandboxes", json={"name": "b", "template": "node", "memory": "512m"}, headers=CSRF).json()
    assert (a["preview_port"], b["preview_port"]) == (8200, 8201)
    assert a["preview_url"] == "http://box.tailnet.ts.net:8200/"
    assert backend.created_specs[0]["ports"] == {"8000/tcp": ("100.64.0.1", 8200)}
    assert backend.created_specs[1]["ports"] == {"5173/tcp": ("100.64.0.1", 8201)}
    admin.post(f"/api/sandboxes/{b['id']}/stop", headers=CSRF)
    c = admin.post("/api/sandboxes", json={"name": "c", "template": "static", "memory": "512m"}, headers=CSRF).json()
    assert c["preview_port"] is None and c["preview_url"] is None  # pool used up: no preview, still works
    admin.delete(f"/api/sandboxes/{a['id']}", headers=CSRF)
    d = admin.post("/api/sandboxes", json={"name": "d", "template": "static", "memory": "512m"}, headers=CSRF).json()
    assert d["preview_port"] == 8200  # freed by destroy


def test_preview_ports_skip_ports_other_containers_hold(admin, backend, previews):
    backend.foreign_ports.add(8200)  # e.g. a real sandbox while the integration tests run
    a = admin.post("/api/sandboxes", json={"name": "a", "template": "python", "memory": "512m"}, headers=CSRF).json()
    assert a["preview_port"] == 8201


def test_no_previews_without_bind_and_host(admin, backend):
    sb = admin.post("/api/sandboxes", json={"name": "a", "template": "python"}, headers=CSRF).json()
    assert sb["preview_port"] is None and "ports" not in backend.created_specs[0]


def test_container_spec_never_binds_all_interfaces(previews):
    sb = Sandbox(id="x", name="x", template="python", volume="v", memory=GIB, cpus=1, preview_port=8200)
    host_ip, port = container_spec(sb, TEMPLATES["python"])["ports"]["8000/tcp"]
    assert host_ip == "100.64.0.1" and host_ip not in ("0.0.0.0", "", "::")


def test_no_session_on_the_preview_hostname(client, previews):
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers={**CSRF, "host": "box.tailnet.ts.net:8090"})
    assert r.status_code == 403 and "previews" in r.json()["detail"]
    ok = client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    assert ok.status_code == 200
    assert client.get("/api/sandboxes", headers={"host": "BOX.tailnet.ts.net"}).status_code == 403
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/runs/x/events", headers={"host": "box.tailnet.ts.net:8090",
                                                                     "origin": "http://box.tailnet.ts.net:8090"}):
            pass
    assert e.value.code == 4401


def test_preview_status_and_static_server(admin, backend, previews):
    sb = admin.post("/api/sandboxes", json={"name": "p", "template": "python"}, headers=CSRF).json()
    backend.exec_replies = [("/proc/net/tcp", ExecResult(0, PROC_NET + "AH_INDEX=yes\n", False, False))]
    st = admin.get(f"/api/sandboxes/{sb['id']}/preview").json()
    assert st == {"url": "http://box.tailnet.ts.net:8200/", "port": 8000, "host_port": 8200, "enabled": True,
                  "listening": "any", "static_server": False, "has_index": True, "lists_directories": True,
                  "branch": "main"}
    assert admin.post(f"/api/sandboxes/{sb['id']}/preview/static", headers=CSRF).status_code == 200
    started = [" ".join(c) for c, _ in backend.exec_calls if "setsid -f" in " ".join(c)]
    assert started and "setsid -f python3 -I /tmp/kiln-preview.py 8000" in started[0]
    assert admin.delete(f"/api/sandboxes/{sb['id']}/preview/static", headers=CSRF).status_code == 200
    assert {a["action"] for a in admin.get("/api/audit").json()} >= {"preview.start", "preview.stop"}


# ---------------------------------------------------------------------------
# Disk, memory, prune
# ---------------------------------------------------------------------------

def test_runs_refused_when_disk_is_low(admin, host):
    sb = admin.post("/api/sandboxes", json={"name": "d", "template": "python"}, headers=CSRF).json()
    host.free_disk = 5 * GIB
    r = admin.post(f"/api/sandboxes/{sb['id']}/runs", json={"prompt": "x"}, headers=CSRF)
    assert r.status_code == 409 and "disk" in r.json()["reasons"][0]


def test_monitor_reports_memory_and_workspace_size(admin, svc, backend):
    import app.main as main

    sb = admin.post("/api/sandboxes", json={"name": "m", "template": "python"}, headers=CSRF).json()
    backend.exec_replies = [("du -sxb", ExecResult(0, "123456\n", False, False))]
    mon = Monitor(svc)
    main.app.state.monitor = mon
    mon.tick()
    got = admin.get(f"/api/sandboxes/{sb['id']}").json()
    assert got["memory_usage"] == 300 * 2**20 and got["workspace_bytes"] == 123456
    assert admin.post(f"/api/sandboxes/{sb['id']}/measure", headers=CSRF).json() == {"workspace_bytes": 123456}


def test_low_disk_stops_running_sandboxes_and_notifies(admin, svc, host):
    a = admin.post("/api/sandboxes", json={"name": "a", "template": "python", "memory": "512m"}, headers=CSRF).json()
    problems = []
    svc.on_problem = lambda sb, why: problems.append((sb.id, why))
    mon = Monitor(svc)
    assert mon.guard_disk() == []  # plenty of disk: nothing happens
    host.free_disk = 3 * GIB       # below DISK_EMERGENCY_FREE (5 GB)
    assert mon.guard_disk() == [a["id"]]
    got = admin.get(f"/api/sandboxes/{a['id']}").json()
    assert got["state"] == "stopped" and "disk" in got["error"]
    assert problems and problems[0][0] == a["id"] and "3.0 GB" in problems[0][1]
    assert any(e["action"] == "disk.emergency_stop" for e in admin.get("/api/audit").json())


def test_prune_never_touches_live_sandboxes_or_foreign_containers(admin, backend):
    keep = admin.post("/api/sandboxes", json={"name": "keep", "template": "python", "memory": "512m"}, headers=CSRF).json()
    stopped = admin.post("/api/sandboxes", json={"name": "stopped", "template": "python", "memory": "512m"},
                         headers=CSRF).json()
    admin.post(f"/api/sandboxes/{stopped['id']}/stop", headers=CSRF)
    backend.containers["orphan"] = {"status": "exited", "labels": {MANAGED_LABEL: "true", SANDBOX_LABEL: "gone"}}
    backend.add_foreign_container("foreign")
    r = admin.post("/api/system/prune", headers=CSRF)
    assert r.status_code == 200 and r.json()["containers"] == ["orphan"]
    assert "foreign" in backend.containers and len(backend.containers) == 3
    assert admin.get(f"/api/sandboxes/{keep['id']}").json()["state"] == "running"
    assert admin.get(f"/api/sandboxes/{stopped['id']}").json()["state"] == "stopped"
    assert any(a["action"] == "system.prune" for a in admin.get("/api/audit").json())
