"""Continuing a run: validation, branching from the previous run, and resuming the agent's
session (or a summary when the session is gone)."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest

from app import config
from app.db import Run
from app.docker_backend import ExecResult
from app.runners import LAUNCH_SCRIPT, ClaudeCodeRunner
from app.runs import outcome
from tests.conftest import CSRF
from tests.test_runs import make_sandbox, start, wait_final

TOKEN = "sk-ant-oat01-" + "Q" * 40
SID = "0b1c2d3e-4f50-4617-8899-aabbccddeeff"


def cont(admin, sid, parent, prompt="now add tests"):
    return admin.post(f"/api/sandboxes/{sid}/runs", json={"prompt": prompt, "continue_from": parent}, headers=CSRF)


def test_continue_validation(admin, svc, runs):
    sb, other = make_sandbox(admin), make_sandbox(admin, name="other")
    assert cont(admin, sb["id"], "000000000000").status_code == 404
    assert cont(admin, sb["id"], "not-an-id").status_code == 422
    elsewhere = wait_final(admin, start(admin, other["id"]).json()["id"])
    assert cont(admin, sb["id"], elsewhere["id"]).status_code == 404  # another sandbox's run

    runs.runners["claude-code"].scale = 5  # a slow demo run
    live = start(admin, sb["id"]).json()
    r = cont(admin, sb["id"], live["id"])
    assert r.status_code == 409 and "send it a message" in r.json()["detail"]
    admin.post(f"/api/runs/{live['id']}/cancel", headers=CSRF)
    wait_final(admin, live["id"])

    never = Run(id="0123456789ab", sandbox_id=sb["id"], runner="claude-code", prompt="x", timeout=60, max_turns=5,
                state="failed", branch="agent/0123456789ab")  # failed before its branch existed
    svc.db.insert_run(never)
    assert cont(admin, sb["id"], never.id).status_code == 409


def test_continuing_picks_up_the_previous_branch(admin, workspace):
    sb = make_sandbox(admin)
    first = wait_final(admin, start(admin, sb["id"]).json()["id"])
    r = cont(admin, sb["id"], first["id"])
    assert r.status_code == 201, r.text
    second = wait_final(admin, r.json()["id"])
    assert second["parent_run_id"] == first["id"] and second["state"] == "succeeded"
    assert second["base_branch"] == first["base_branch"]
    assert workspace.started_from == first["branch"]
    # A cancelled or timed-out run can be continued too: that's what continuing is for.
    third = wait_final(admin, cont(admin, sb["id"], second["id"]).json()["id"])
    assert third["parent_run_id"] == second["id"]


def test_outcome_wording():
    base = Run(id="x", sandbox_id="s", runner="claude-code", prompt="p", timeout=1800, max_turns=5)
    assert outcome(dataclasses.replace(base, state="succeeded", verify="passed", verify_cmd="t")) == \
        "finished, and its checks pass"
    assert "time limit (30 min)" in outcome(dataclasses.replace(base, state="timed_out"))
    assert "cancelled by the owner" in outcome(dataclasses.replace(base, state="cancelled"))
    assert outcome(dataclasses.replace(base, state="failed", error="boom\nmore")) == "failed (boom)"
    assert "checks (`npm test`) fail" in outcome(dataclasses.replace(base, state="succeeded", verify="failed",
                                                                     verify_cmd="npm test"))


@pytest.fixture
def claude(runs, backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token=TOKEN, claude_token_mode="direct"))
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    backend.stream_script = [(0, 1, json.dumps({"type": "result", "is_error": False, "result": "ok"}).encode() + b"\n")]


def parent_run(svc, sb, **kw):
    p = Run(id="aaaaaaaaaaaa", sandbox_id=sb["id"], runner="claude-code", prompt="Build the login page",
            timeout=1800, max_turns=5, state="timed_out", branch="agent/aaaaaaaaaaaa", base_branch="main",
            session_id=SID, finished_at=time.time(), **kw)
    svc.db.insert_run(p)
    svc.db.add_run_event(p.id, 0, time.time(), {"type": "log", "kind": "result", "text": "Login form done; no tests yet."})
    return p


def first_message(backend):
    [(argv, env)] = [(c, e) for c, e in backend.exec_calls if isinstance(c, list) and c[:3] == ["bash", "-c", LAUNCH_SCRIPT]]
    return argv, json.loads(env["AH_MSG"])["message"]["content"][0]["text"]


def test_resumes_the_agent_session_when_it_still_exists(admin, svc, backend, claude):
    sb = make_sandbox(admin)
    p = parent_run(svc, sb)
    backend.exec_replies = [(".claude/projects", ExecResult(0, f"/home/agent/.claude/projects/-workspace/{SID}.jsonl\n",
                                                             False, False))]
    done = wait_final(admin, cont(admin, sb["id"], p.id).json()["id"])
    argv, msg = first_message(backend)
    assert argv[argv.index("--resume") + 1] == SID
    assert msg.startswith("[Kiln] This continues an earlier run") and "time limit (30 min)" in msg
    assert msg.endswith("now add tests") and "It was asked" not in msg
    log = [e.get("text") for e in admin.get(f"/api/runs/{done['id']}/log").json()]
    assert f"Continuing run {p.id} in the same agent session" in log


def test_summary_instead_when_the_session_is_gone(admin, svc, backend, claude):
    sb = make_sandbox(admin)
    p = parent_run(svc, sb)
    backend.exec_replies = [(".claude/projects", ExecResult(0, "", False, False))]  # e.g. sandbox recreated
    done = wait_final(admin, cont(admin, sb["id"], p.id).json()["id"])
    argv, msg = first_message(backend)
    assert "--resume" not in argv
    assert "It was asked:\nBuild the login page" in msg and "Login form done; no tests yet." in msg
    assert msg.endswith("Now:\nnow add tests")
    log = [e.get("text") or "" for e in admin.get(f"/api/runs/{done['id']}/log").json()]
    assert any("gets a summary of it instead" in t for t in log)


def test_a_bad_session_id_is_never_passed_on(backend):
    from app.runners import RunSpec

    r = ClaudeCodeRunner(backend)
    spec = RunSpec(run_id="r", container_id="c", prompt="p", timeout=60, max_turns=5, branch="agent/r",
                   resume_session="--dangerous; rm -rf /")
    assert "--resume" not in r.argv(spec) and r.session_exists(spec) is False
