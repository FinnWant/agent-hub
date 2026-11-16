"""Ledger, checkpoints and rewind, loop breaker, handoff report, full log (fake Docker)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from app import config
from app.hooks import HookCall
from app.ledger import BEGIN, END, PATH, after_compaction
from app.loops import LoopWatch
from app.report import sections
from app.runners import ClaudeCodeRunner
from tests.conftest import CSRF
from tests.test_runs import make_sandbox, start, wait_final

TOKEN = "sk-ant-oat01-" + "Q" * 40


def jl(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_ledger_is_kept_across_runs(admin, svc, backend):
    sb = make_sandbox(admin)
    cid = svc.get(sb["id"]).container_id
    first = wait_final(admin, start(admin, sb["id"], prompt="Build the login page").json()["id"])
    text = backend.files[(cid, PATH)].decode()
    assert "## Plan" in text and BEGIN in text and END in text
    assert "Build the login page" in text and f"run {first['id']}" in text and "finished" in text
    # The agent's notes are kept; Kiln's block is replaced, not repeated.
    backend.files[(cid, PATH)] = text.replace("## Plan\n", "## Plan\n- use sessions, not JWT\n").encode()
    second = wait_final(admin, start(admin, sb["id"], prompt="Add tests").json()["id"])
    text = backend.files[(cid, PATH)].decode()
    assert "- use sessions, not JWT" in text and text.count(BEGIN) == 1
    assert "## Latest request\n\nAdd tests" in text and f"run {second['id']}" in text and f"run {first['id']}" in text

    got = admin.get(f"/api/sandboxes/{sb['id']}/ledger").json()
    assert got["exists"] and "sessions, not JWT" in got["text"]
    r = admin.put(f"/api/sandboxes/{sb['id']}/ledger", json={"text": "# mine"}, headers=CSRF)
    assert r.status_code == 200 and backend.files[(cid, PATH)] == b"# mine"


def test_ledger_comes_back_after_compaction(admin, svc, runs, backend):
    sb = make_sandbox(admin)
    cid = svc.get(sb["id"]).container_id
    backend.files[(cid, PATH)] = b"# Ledger\n## Plan\n- step 2 of 5"
    run = svc.db.list_runs(sandbox_id=sb["id"])  # none yet; any run of the sandbox will do
    from app.db import Run

    r = Run(id="abcabcabcabc", sandbox_id=sb["id"], runner="claude-code", prompt="x", timeout=60, max_turns=5)
    reply = asyncio.run(after_compaction(runs, r, HookCall("SessionStart", "1-1", {"source": "compact"})))
    ctx = reply.stdout["hookSpecificOutput"]["additionalContext"]
    assert reply.stdout["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "step 2 of 5" in ctx
    assert asyncio.run(after_compaction(runs, r, HookCall("SessionStart", "1-2", {"source": "startup"}))) is None
    assert run == []
    spec = runs._spec(r, svc.get(sb["id"]))
    assert spec.hooks["SessionStart"] == 20 and spec.hook_matchers["SessionStart"] == "compact"


# ---------------------------------------------------------------------------
# Checkpoints and rewind
# ---------------------------------------------------------------------------

@pytest.fixture
def quick(settings, monkeypatch):
    s = dataclasses.replace(settings, checkpoint_interval=0.05, checkpoint_debounce=0)
    monkeypatch.setattr(config, "settings", s)
    return s


def test_checkpoints_during_a_run_and_rewind(admin, runs, workspace, quick):
    sb = make_sandbox(admin)
    runs.runners["claude-code"].scale = 0.5
    live = start(admin, sb["id"]).json()
    time.sleep(0.4)
    assert admin.post(f"/api/runs/{live['id']}/rewind", json={"n": 1}, headers=CSRF).status_code == 409
    done = wait_final(admin, live["id"])
    cps = admin.get(f"/api/runs/{done['id']}/checkpoints").json()
    assert len(cps) >= 2 and [c["n"] for c in cps] == list(range(1, len(cps) + 1))
    log = admin.get(f"/api/runs/{done['id']}/log").json()
    assert [e["n"] for e in log if e.get("kind") == "checkpoint"] == [c["n"] for c in cps]
    r = admin.post(f"/api/runs/{done['id']}/rewind", json={"n": 1}, headers=CSRF)
    assert r.status_code == 200 and r.json()["rewound_to"] == 1 and r.json()["commit_sha"] == cps[0]["sha"]
    assert workspace.rewound == cps[0]["sha"]
    assert admin.post(f"/api/runs/{done['id']}/rewind", json={"n": 999}, headers=CSRF).status_code == 404
    assert done["report"]["facts"]["checkpoints"] == len(cps)


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------

def test_loop_watch():
    w = LoopWatch()
    for cmd in ("npm install", "npm ci", "yarn"):  # different calls
        w.tool("Bash", cmd)
        assert w.result(True) is None
    w = LoopWatch()
    for i in range(4):  # failing, but the agent edits a file in between: progress
        w.tool("Bash", "pytest -q")
        assert w.result(True) is None
        w.tool("Edit", "app.py")
        assert w.result(False) is None
    w = LoopWatch()
    hits = []
    for _ in range(3):
        w.tool("Bash", "npm install --registry http://10.0.0.1:4873")
        hits.append(w.result(True))
    assert hits[:2] == [None, None] and "3 times" in hits[2]
    w.tool("Bash", "npm install --registry http://10.0.0.1:4873")
    assert w.result(True) is None  # tells once per call
    w2 = LoopWatch()
    for i in range(3):
        w2.tool("Bash", f"curl http://host:{8000 + i}/x")  # numbers normalised: the same call
        last = w2.result(True)
    assert last is not None
    w3 = LoopWatch()
    for i in range(10):
        w3.tool("Bash", "pytest -q")
        assert w3.result(i % 2 == 0) is None  # passes in between: not stuck
    w4 = LoopWatch()
    results = []
    for i in range(w4.calls.maxlen):
        w4.tool("Read", ["a.py", "b.py"][i % 2])
        results.append(w4.result(False))
    assert results[-1] and "without changing any file" in results[-1]


def tool_pair(i, cmd="npm install", error=True):
    return [(0, 1, jl({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})),
            (0.02, 1, jl({"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": error, "content": f"E: network unreachable {i}"}]}}))]


@pytest.fixture
def claude(runs, backend, settings, monkeypatch):
    s = dataclasses.replace(settings, claude_code_oauth_token=TOKEN, budget_wait=30)
    monkeypatch.setattr(config, "settings", s)
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)


def test_loop_breaker_nudges_then_asks_the_owner(admin, backend, claude):
    sb = make_sandbox(admin)
    script = [p for i in range(3) for p in tool_pair(i)]
    script += [p for i in range(3) for p in tool_pair(i, "pip install requests")]
    script += [(3, 1, b"thinking\n"), (0, 1, jl({"type": "result", "is_error": False, "result": "done"}))]
    backend.stream_script = script
    rid = start(admin, sb["id"]).json()["id"]
    for _ in range(200):
        pending = admin.get("/api/requests", params={"run": rid, "state": "pending"}).json()
        if pending:
            break
        time.sleep(0.02)
    [req] = pending
    assert req["kind"] == "stuck" and req["options"] == ["Continue", "Stop"]
    nudges = [json.loads(e["AH_MSG"])["message"]["content"][0]["text"] for c, e in backend.exec_calls
              if isinstance(c, list) and c[3:4] == ["ah-send"]]
    assert len(nudges) == 1 and nudges[0].startswith("[Kiln] You ran 'npm install' 3 times")
    hold = [k for k in backend.gateway["keys"] if k.endswith(".hold")]
    assert len(hold) == 1
    r = admin.post(f"/api/requests/{req['id']}/answer", json={"choice": "continue", "text": "Use the offline mirror"},
                   headers=CSRF)
    assert r.status_code == 200
    assert not [k for k in backend.gateway["keys"] if k.endswith(".hold")]
    sent = [json.loads(e["AH_MSG"])["message"]["content"][0]["text"] for c, e in backend.exec_calls
            if isinstance(c, list) and c[3:4] == ["ah-send"]]
    assert sent[-1] == "[Owner] Use the offline mirror"
    done = wait_final(admin, rid)
    log = admin.get(f"/api/runs/{rid}/log").json()
    assert sum(1 for e in log if e.get("kind") == "loop") == 2
    assert done["report"]["facts"]["loops"] == 2


def test_stuck_agent_stopped_by_the_owner(admin, backend, claude):
    sb = make_sandbox(admin)
    backend.stream_script = [p for i in range(3) for p in tool_pair(i)] + \
        [p for i in range(3) for p in tool_pair(i, "make")] + [(5, 1, b"x\n")]
    rid = start(admin, sb["id"]).json()["id"]
    for _ in range(200):
        if pending := admin.get("/api/requests", params={"run": rid, "state": "pending"}).json():
            break
        time.sleep(0.02)
    admin.post(f"/api/requests/{pending[0]['id']}/answer", json={"choice": "stop"}, headers=CSRF)
    assert wait_final(admin, rid)["state"] == "cancelled"


# ---------------------------------------------------------------------------
# Report and full log
# ---------------------------------------------------------------------------

def test_summary_sections():
    s = sections("## Done\n- login page\n\n**Not done:**\n- tests\n\nAssumptions\nPostgres\n\n## How to verify\nnpm test")
    assert s == {"Done": "- login page", "Not done": "- tests", "Assumptions": "Postgres", "How to verify": "npm test"}
    assert sections("Just did it.") == {}
    # Inline, as agents often write it (seen from Claude Code in a real run)
    s = sections("Done: `a.txt` contains `a`. Both files are in /workspace.\nNot done: nothing\n"
                 "- **Risks**: none known")
    assert s == {"Done": "`a.txt` contains `a`. Both files are in /workspace.", "Not done": "nothing",
                 "Risks": "none known"}
    assert sections("Done with the refactor, then tested it.") == {}  # running text, not a heading


def test_report_facts(admin):
    sb = make_sandbox(admin)
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    rep = done["report"]
    assert rep["summary"].startswith("Added an") and rep["sections"] == {}
    f = rep["facts"]
    assert f["state"] == "succeeded" and f["files"] == 2 and f["insertions"] == 30 and f["file_names"]
    assert f["commit"] == done["commit_sha"] and f["questions"] == 0 and f["loops"] == 0


def test_full_log_keeps_what_the_stored_log_drops(admin, settings, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, run_max_events=5))
    sb = make_sandbox(admin)
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    stored = admin.get(f"/api/runs/{done['id']}/log").json()
    r = admin.get(f"/api/runs/{done['id']}/log/full")
    full = [json.loads(x) for x in r.text.splitlines()]
    assert r.headers["content-disposition"].startswith("attachment")
    assert len(full) > len(stored) and any("Log limit reached" in (e.get("text") or "") for e in stored)
    assert any(e.get("kind") == "result" for e in full)
