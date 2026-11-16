from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app import config
from app.db import Run
from app import gateway
from app.runners import (LAUNCH_SCRIPT, ClaudeCodeRunner, Outcome, RunSpec, ScriptRunner, follow_argv, kill_argv,
                         launch_argv)
from tests.conftest import CSRF, GIB

FINAL = {"succeeded", "failed", "cancelled", "timed_out"}
ORIGIN = {"origin": "http://testserver"}
TOKEN = "sk-ant-oat01-" + "Q" * 40
GH_TOKEN = "github_pat_" + "Z" * 60


def make_sandbox(admin, **kw):
    body = {"name": "runs", "template": "python", "memory": "1g", **kw}
    r = admin.post("/api/sandboxes", json=body, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


def start(admin, sid, prompt="add a README section", **kw):
    return admin.post(f"/api/sandboxes/{sid}/runs", json={"prompt": prompt, **kw}, headers=CSRF)


def wait_final(admin, rid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = admin.get(f"/api/runs/{rid}").json()
        if run["state"] in FINAL:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {rid} still {run['state']}")


def events(admin, rid):
    return admin.get(f"/api/runs/{rid}/log").json()


def launch_call(backend):
    """(argv, env) of the exec that started the agent."""
    calls = [(c, e) for c, e in backend.exec_calls if isinstance(c, list) and c[:3] == ["bash", "-c", LAUNCH_SCRIPT]]
    assert calls, "the agent was never launched"
    return calls[-1]


# ---------------------------------------------------------------------------
# Lifecycle through the API
# ---------------------------------------------------------------------------

def test_successful_run_commits_to_agent_branch(admin, svc):
    sb = make_sandbox(admin)
    r = start(admin, sb["id"])
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["state"] == "queued" and run["branch"] == f"agent/{run['id']}"
    assert run["timeout"] == 30 * 60 and run["max_turns"] == 50
    done = wait_final(admin, run["id"])
    assert done["state"] == "succeeded", done
    assert done["commit_sha"] and done["cost_usd"] == pytest.approx(0.0123) and done["num_turns"] == 4
    evs = events(admin, run["id"])
    assert [e["seq"] for e in evs] == list(range(len(evs)))
    states = [e["state"] for e in evs if e["type"] == "status"]
    assert states == ["queued", "running"]
    assert evs[-1]["type"] == "done" and evs[-1]["state"] == "succeeded" and evs[-1]["commit"]
    assert any(e.get("kind") == "tool" for e in evs)
    acts = [(a["action"], a["ok"]) for a in svc.db.list_audit()]
    assert ("run.start", True) in acts and ("run.finish", True) in acts
    sb_after = admin.get(f"/api/sandboxes/{sb['id']}").json()
    assert sb_after["last_run"]["state"] == "succeeded" and sb_after["base_branch"] == "main"


def test_one_active_run_per_sandbox(admin, runs):
    runs.runners["claude-code"].scale = 1.0  # slow enough to still be running
    sb = make_sandbox(admin)
    first = start(admin, sb["id"]).json()
    second = start(admin, sb["id"])
    assert second.status_code == 409 and first["id"] in second.json()["detail"]
    admin.post(f"/api/runs/{first['id']}/cancel", headers=CSRF)
    wait_final(admin, first["id"])
    assert start(admin, sb["id"]).status_code == 201  # free again


def test_cancel_keeps_the_work(admin, runs):
    runs.runners["claude-code"].scale = 1.0
    sb = make_sandbox(admin)
    run = start(admin, sb["id"]).json()
    time.sleep(0.6)
    r = admin.post(f"/api/runs/{run['id']}/cancel", headers=CSRF)
    assert r.status_code == 200
    done = wait_final(admin, run["id"])
    assert done["state"] == "cancelled" and done["commit_sha"]
    assert done["finished_at"] - done["started_at"] < 3


def test_requires_running_sandbox_and_known_runner(admin):
    sb = make_sandbox(admin)
    assert start(admin, sb["id"], runner="nope").status_code == 400
    admin.post(f"/api/sandboxes/{sb['id']}/stop", headers=CSRF)
    assert start(admin, sb["id"]).status_code == 409
    assert start(admin, "missing").status_code == 404


def test_requires_login_and_csrf(client, admin):
    sb = make_sandbox(admin)
    assert client.post(f"/api/sandboxes/{sb['id']}/runs", json={"prompt": "x"}).status_code == 403
    client.cookies.clear()
    assert client.post(f"/api/sandboxes/{sb['id']}/runs", json={"prompt": "x"}, headers=CSRF).status_code == 401


def test_memory_reserve_blocks_new_runs(admin, host):
    sb = make_sandbox(admin)
    host.mem_available = 2 * GIB
    r = start(admin, sb["id"])
    assert r.status_code == 409 and "free" in r.json()["reasons"][0]


def test_missing_claude_token_is_explained(admin, runs, backend):
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    sb = make_sandbox(admin)
    r = start(admin, sb["id"])
    assert r.status_code == 400 and "set_secret claude" in r.json()["detail"]


def test_agent_failure_is_reported(admin):
    sb = make_sandbox(admin)
    done = wait_final(admin, start(admin, sb["id"], prompt="fail").json()["id"])
    assert done["state"] == "failed" and "Simulated failure" in done["error"]


def test_workspace_failure_fails_the_run(admin, workspace):
    workspace.fail_prepare = "/workspace has uncommitted changes on main."
    sb = make_sandbox(admin)
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["state"] == "failed" and "uncommitted" in done["error"] and done["commit_sha"] is None


def test_push_only_when_allowed_and_successful(admin, workspace):
    sb = make_sandbox(admin, repo_url="https://github.com/example/playground")
    wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert workspace.pushed == []
    r = admin.patch(f"/api/sandboxes/{sb['id']}", json={"allow_push": True}, headers=CSRF)
    assert r.status_code == 200 and r.json()["allow_push"] is True
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["pushed"] is True and workspace.pushed == [done["branch"]]
    failed = wait_final(admin, start(admin, sb["id"], prompt="fail").json()["id"])
    assert failed["pushed"] is False and len(workspace.pushed) == 1


def test_stopping_the_sandbox_cancels_its_run(admin, runs):
    runs.runners["claude-code"].scale = 1.0
    sb = make_sandbox(admin)
    run = start(admin, sb["id"]).json()
    time.sleep(0.3)
    assert admin.post(f"/api/sandboxes/{sb['id']}/stop", headers=CSRF).status_code == 200
    assert admin.get(f"/api/runs/{run['id']}").json()["state"] == "cancelled"


def test_reconcile_marks_orphaned_runs_failed(svc, runs):
    run = Run(id="0123456789ab", sandbox_id="gone", runner="claude-code", prompt="x", timeout=60,
              max_turns=5, state="running")
    svc.db.insert_run(run)
    runs.reconcile_on_startup()
    assert svc.db.get_run(run.id).state == "failed"


# ---------------------------------------------------------------------------
# Exec-based runner: timeout, cancel, redaction (fake Docker stream)
# ---------------------------------------------------------------------------

def test_timeout_kills_the_agent(admin, svc, runs, backend):
    sb = make_sandbox(admin)
    backend.stream_script = [(0.05, 1, b"working\n"), (30, 1, b"never\n")]
    run = runs.create(sandbox_id=sb["id"], prompt="sleep 600", runner="script", timeout=1, max_turns=1, ip="t")
    asyncio.run(runs._execute(run))
    done = svc.db.get_run(run.id)
    assert done.state == "timed_out" and done.commit_sha
    assert any(cmd == kill_argv(run.id) for cmd, _ in backend.exec_calls)
    texts = [e.get("text") for e in svc.db.run_events(run.id)]
    assert "working" in texts and any("Time limit" in (t or "") for t in texts)


def test_secrets_are_redacted_and_only_in_the_agent_env(admin, svc, runs, backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token=TOKEN, github_token=GH_TOKEN))
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    sb = make_sandbox(admin)
    leak = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"found {TOKEN} and {GH_TOKEN} and Authorization: Bearer abcdefghijkl"}]}})
    backend.stream_script = [(0, 1, leak.encode() + b"\n"), (0, 2, f"stderr {TOKEN}\n".encode()),
                             (0, 1, json.dumps({"type": "result", "is_error": False, "result": "ok",
                                                "total_cost_usd": 0.5, "num_turns": 2}).encode() + b"\n")]
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["state"] == "succeeded"
    stored = json.dumps(events(admin, done["id"]))
    assert TOKEN not in stored and GH_TOKEN not in stored and "abcdefghijkl" not in stored
    assert stored.count("[redacted]") >= 4
    argv, env = launch_call(backend)
    # The real token never enters the sandbox: the agent gets a per-run key for the gateway.
    assert TOKEN not in json.dumps(env) and "GITHUB_TOKEN" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://kiln-gateway:8080"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-kiln-")
    assert TOKEN not in " ".join(argv) and GH_TOKEN not in " ".join(argv)
    assert all(TOKEN not in json.dumps(e or {}) and GH_TOKEN not in json.dumps(e or {}) for _, e in backend.exec_calls)
    assert TOKEN not in json.dumps(admin.get("/api/system").json())
    # The gateway holds the token; the run's key was registered and is gone now the run is over.
    assert backend.gateway["spec"]["environment"]["KILN_UPSTREAM_TOKEN"] == TOKEN
    assert "kiln-sb-" + sb["id"] in backend.gateway["networks"]
    assert backend.gateway["keys"] == set()
    added = [c for c in backend.gateway_calls if c[:2] == ["sh", "-c"] and "touch" in c[2]]
    assert len(added) == 1 and added[0][-1] == gateway.key_digest(env["CLAUDE_CODE_OAUTH_TOKEN"])


def test_direct_token_mode_is_still_available(admin, runs, backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token=TOKEN, claude_token_mode="direct"))
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    sb = make_sandbox(admin)
    backend.stream_script = [(0, 1, json.dumps({"type": "result", "is_error": False, "result": "ok"}).encode() + b"\n")]
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["state"] == "succeeded"
    env = launch_call(backend)[1]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "ANTHROPIC_BASE_URL" not in env
    assert backend.gateway is None


# ---------------------------------------------------------------------------
# Claude Code runner: flags and stream-json parsing
# ---------------------------------------------------------------------------

def spec(prompt="do it"):
    return RunSpec(run_id="abcdef012345", container_id="c", prompt=prompt, timeout=600, max_turns=7,
                   branch="agent/abcdef012345")


def test_claude_argv(backend):
    r = ClaudeCodeRunner(backend)
    argv = r.argv(spec("--help me"))
    assert argv[:2] == ["claude", "-p"] and "--help me" not in argv  # the prompt goes in on stdin
    for flag in ("--input-format", "--output-format", "stream-json", "--verbose", "--replay-user-messages",
                 "--max-turns", "7", "--permission-mode", "bypassPermissions", "--append-system-prompt"):
        assert flag in argv
    first = json.loads(r.first_message(spec("--help me")))
    assert first["type"] == "user" and first["message"]["content"][0]["text"] == "--help me"
    full = launch_argv("abcdef012345", 600, argv, interactive=True)
    assert full[:3] == ["bash", "-c", LAUNCH_SCRIPT] and full[4:7] == ["/tmp/kiln-runs/abcdef012345", "660", "1"]
    assert full[7:] == argv
    assert follow_argv("abcdef012345", 42)[-2:] == ["/tmp/kiln-runs/abcdef012345", "42"]


def test_claude_stream_json_parsing(backend):
    r, o = ClaudeCodeRunner(backend), Outcome()
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x", "claude_code_version": "2.1.315"},
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"},
                                                      {"type": "text", "text": "Reading files"},
                                                      {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "a\nb"}],
                                                  "is_error": False}]}},
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 7, "total_cost_usd": 0.2,
         "errors": []},
    ]
    evs = [e for line in lines for e in r.parse(1, json.dumps(line), o)]
    kinds = [(e["type"], e.get("kind")) for e in evs]
    assert kinds == [("log", "system"), ("log", "text"), ("log", "tool"), ("log", "tool_result"),
                     ("log", "error"), ("cost", None)]
    assert evs[2]["name"] == "Bash" and evs[2]["text"] == "ls" and evs[3]["text"] == "a\nb"
    assert o.is_error and "turn limit" in o.error and o.cost_usd == 0.2 and o.extra["session_id"] == "s1"
    assert r.parse(1, "not json", o) == [{"type": "log", "kind": "stdout", "text": "not json"}]
    assert r.parse(2, "warning", o)[0]["kind"] == "stderr"


def test_script_runner_is_not_offered_to_claude_users(backend):
    assert ScriptRunner(backend).argv(spec("echo hi")) == ["bash", "-c", "echo hi"]


# ---------------------------------------------------------------------------
# Live events over WebSocket
# ---------------------------------------------------------------------------

def collect(ws):
    got = []
    while True:
        try:
            got += ws.receive_json()["events"]
        except WebSocketDisconnect as e:
            return got, e.code


def test_events_websocket_backlog_and_live(admin, runs):
    sb = make_sandbox(admin)
    finished = start(admin, sb["id"]).json()
    wait_final(admin, finished["id"])
    with admin.websocket_connect(f"/api/runs/{finished['id']}/events", headers=ORIGIN) as ws:
        got, code = collect(ws)
    assert code == 1000 and got[-1]["type"] == "done" and [e["seq"] for e in got] == list(range(len(got)))

    runs.runners["claude-code"].scale = 0.2
    live = start(admin, sb["id"]).json()
    with admin.websocket_connect(f"/api/runs/{live['id']}/events", headers=ORIGIN) as ws:
        got, code = collect(ws)
    assert code == 1000 and got[-1]["type"] == "done" and got[-1]["state"] == "succeeded"
    assert [e["seq"] for e in got] == list(range(len(got)))  # no gaps, no duplicates


def test_events_websocket_requires_session_and_origin(client, admin):
    sb = make_sandbox(admin)
    run = start(admin, sb["id"]).json()
    with pytest.raises(WebSocketDisconnect) as e:
        with admin.websocket_connect(f"/api/runs/{run['id']}/events", headers={"origin": "http://evil.example"}):
            pass
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as e:
        with admin.websocket_connect("/api/runs/nope/events", headers=ORIGIN) as ws:
            ws.receive_json()
    assert e.value.code == 4404


def test_old_image_without_cli_is_explained(admin, runs, backend):
    runs.runners["claude-code"] = ScriptRunner(backend)
    backend.stream_script = [(0, 2, b"bash: claude: command not found\n")]
    backend.stream_exit_code = 127
    sb = make_sandbox(admin)
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["state"] == "failed" and "older image" in done["error"]


def test_redaction_catches_base64_of_the_bare_token():
    import base64

    from app.redact import Redactor

    r = Redactor([TOKEN])
    for prefix in (b"", b"a", b"ab", b"{\"t\":\""):
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            leaked = enc(prefix + TOKEN.encode() + b"\"}").decode()
            assert "[redacted]" in r.text(f"out: {leaked}"), (prefix, enc)
    assert r.text("nothing secret here") == "nothing secret here"


# ---------------------------------------------------------------------------
# Messages to a running agent, and surviving a Kiln restart
# ---------------------------------------------------------------------------

def jl(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def replay(text):
    return jl({"type": "user", "isReplay": True, "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


def test_claude_replays_drive_the_close_rule(backend):
    r, o = ClaudeCodeRunner(backend), Outcome(extra={"sent": 1, "acked": 0})
    assert r.parse(1, replay("the prompt").decode(), o) == []  # the prompt is shown with the run already
    assert not r.should_close(o)
    r.parse(1, jl({"type": "result", "is_error": False, "result": "done"}).decode(), o)
    assert r.should_close(o)  # idle and nothing unread
    o.extra["sent"] = 2  # a message went in just as the agent finished
    assert not r.should_close(o)
    evs = r.parse(1, replay("also update the docs").decode(), o)
    assert evs == [{"type": "log", "kind": "user", "text": "also update the docs"}] and not o.extra["idle"]
    r.parse(1, jl({"type": "result", "is_error": False, "result": "done"}).decode(), o)
    assert r.should_close(o)


def test_messages_reach_a_running_agent(admin, runs, backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token=TOKEN, claude_token_mode="direct"))
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    sb = make_sandbox(admin)
    backend.stream_script = [
        (0, 1, jl({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "m"})),
        (0, 1, replay("add a README section")),
        (0, 1, jl({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})),
        (0.6, 1, replay("also add a licence")),
        (0, 1, jl({"type": "result", "is_error": False, "result": "done", "num_turns": 3})),
    ]
    rid = start(admin, sb["id"]).json()["id"]
    wait_for(lambda: admin.get(f"/api/runs/{rid}").json()["can_message"])
    r = admin.post(f"/api/runs/{rid}/messages", json={"text": "also add a licence"}, headers=CSRF)
    assert r.status_code == 202, r.text
    sent = [e for c, e in backend.exec_calls if isinstance(c, list) and c[3:4] == ["ah-send"]]
    assert len(sent) == 1 and json.loads(sent[0]["AH_MSG"])["message"]["content"][0]["text"] == "also add a licence"

    done = wait_final(admin, rid)
    assert done["state"] == "succeeded" and done["session_id"] == "sess-1"
    assert "runner_state" not in done and done["can_message"] is False
    log = events(admin, rid)
    assert [e["text"] for e in log if e.get("kind") == "user"] == ["also add a licence"]
    closes = [i for i, (c, _) in enumerate(backend.exec_calls) if isinstance(c, list) and c[3:4] == ["ah-close"]]
    assert len(closes) == 1  # the input closed once, after the last result
    late = admin.post(f"/api/runs/{rid}/messages", json={"text": "too late"}, headers=CSRF)
    assert late.status_code == 409


def test_messages_are_validated(admin, runs):
    sb = make_sandbox(admin)
    runs.runners["claude-code"].scale = 5  # slow demo run
    rid = start(admin, sb["id"]).json()["id"]
    assert admin.post(f"/api/runs/{rid}/messages", json={"text": ""}, headers=CSRF).status_code == 422
    assert admin.post(f"/api/runs/{rid}/messages", json={"text": "x" * 4001}, headers=CSRF).status_code == 422
    assert admin.post(f"/api/runs/{rid}/messages", json={"text": "hi"}).status_code == 403  # CSRF
    assert admin.post("/api/runs/nope/messages", json={"text": "hi"}, headers=CSRF).status_code == 404
    admin.post(f"/api/runs/{rid}/cancel", headers=CSRF)
    wait_final(admin, rid)


def running_run(svc, sb, **kw):
    run = Run(id="feedc0ffee01", sandbox_id=sb["id"], runner="script", prompt="x", timeout=600, max_turns=5,
              state="running", branch="agent/feedc0ffee01", base_branch="main", started_at=time.time() - 30,
              runner_state=json.dumps({"offset": 7}), **kw)
    svc.db.insert_run(run)
    for seq in range(2):
        svc.db.add_run_event(run.id, seq, time.time(), {"type": "log", "kind": "stdout", "text": f"before {seq}"})
    return run


def status_reply(word):
    from app.docker_backend import ExecResult

    return [("ah-status", ExecResult(exit_code=0, output=word + "\n", truncated=False, timed_out=False))]


def test_restart_reattaches_where_the_log_ends(admin, svc, runs, backend):
    sb = make_sandbox(admin)
    run = running_run(svc, sb)
    backend.exec_replies = status_reply("ALIVE")
    resumable = runs.reconcile_on_startup()
    assert [(r.id, st) for r, st in resumable] == [(run.id, {"offset": 7})]
    assert not any(c == kill_argv(run.id) for c, _ in backend.exec_calls)

    backend.exec_replies = status_reply("DONE")
    backend.stream_script = [(0, 1, b"after restart\n")]
    asyncio.run(runs._execute(svc.db.get_run(run.id), resume=resumable[0][1]))
    assert backend.stream_calls[-1][0] == follow_argv(run.id, 7)  # nothing read twice
    done = svc.db.get_run(run.id)
    assert done.state == "succeeded" and done.commit_sha
    assert json.loads(done.runner_state)["offset"] == 7 + len(b"after restart\n")
    log = svc.db.run_events(run.id)
    assert [e["seq"] for e in log] == list(range(len(log)))  # numbering continues
    texts = [e.get("text") for e in log]
    assert texts[:2] == ["before 0", "before 1"] and "after restart" in texts
    assert any("Kiln restarted" in (t or "") for t in texts)


def test_restart_fails_runs_whose_agent_is_gone(admin, svc, runs, backend):
    sb = make_sandbox(admin)
    run = running_run(svc, sb)
    backend.exec_replies = status_reply("GONE")
    assert runs.reconcile_on_startup() == []
    assert svc.db.get_run(run.id).state == "failed"
    assert any(c == kill_argv(run.id) for c, _ in backend.exec_calls)


def test_startup_resumes_runs_and_keeps_their_gateway_keys(svc, runs, backend, settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token=TOKEN, db_path=tmp_path / "t.db"))
    claude = ClaudeCodeRunner(backend)
    runs.runners["claude-code"] = claude
    sb = svc.create(name="restart", template="python", repo_url=None, base_branch=None, memory=GIB, cpus=None, ip="t")
    keep = claude.gateway.issue(sb.id, "feedc0ffee01")
    claude.gateway.issue(sb.id, "000000000000")  # a finished run's key, left over
    run = running_run(svc, {"id": sb.id})
    svc.db.update_run(run, runner="claude-code", runner_state=json.dumps(
        {"offset": 0, "sent": 1, "acked": 1, "key_digest": gateway.key_digest(keep)}))
    claude.gateway._keys.clear()  # a new Kiln process knows nothing
    monkeypatch.setattr(claude, "status", lambda spec: "ALIVE")
    backend.exec_replies = status_reply("DONE")
    backend.stream_script = [(0.3, 1, jl({"type": "result", "is_error": False, "result": "finished"}))]

    app.state.service, app.state.runs = svc, runs
    try:
        with TestClient(app):
            assert backend.gateway["keys"] == {gateway.key_digest(keep)}  # the other key is gone
            wait_for(lambda: svc.db.get_run(run.id).state == "succeeded")
    finally:
        for attr in ("service", "runs", "notifier", "monitor"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)
    assert backend.gateway["keys"] == set()  # released when the run ended
    assert any(c[3:4] == ["ah-close"] for c, _ in backend.exec_calls if isinstance(c, list))


def test_gateway_restore_registers_the_key_again(backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(settings, claude_code_oauth_token=TOKEN))
    from app.docker_backend import sandbox_network

    backend.networks.add(sandbox_network("sb1"))
    gw = gateway.Gateway(backend)
    digest = gateway.key_digest(gw.issue("sb1", "run1"))
    backend.gateway = None  # e.g. replaced after an upgrade
    gw2 = gateway.Gateway(backend)
    gw2.restore("sb1", "run1", digest)
    assert backend.gateway["keys"] == {digest} and gw2.digest("run1") == digest
    with pytest.raises(Exception):
        gw2.restore("sb1", "run2", "not-a-digest")
