"""Checks (verify.py): the Stop-hook gate that sends the agent back, the final check after a
run, what it means for pushing and notifications, and the settings API."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from app import config
from app.db import Run
from app.docker_backend import ExecResult
from app.hooks import HookCall
from app.notify import run_message
from app.verify import check_before_stop
from tests.conftest import CSRF
from tests.test_runs import make_sandbox, start, wait_final


def reply(code: int, output: str = "") -> ExecResult:
    return ExecResult(exit_code=code, output=output, truncated=False, timed_out=False)


def set_checks(admin, sid, cmd):
    r = admin.patch(f"/api/sandboxes/{sid}", json={"verify_cmd": cmd}, headers=CSRF)
    assert r.status_code == 200, r.text
    return r.json()


def test_settings_api_sets_and_clears_checks(admin):
    sb = make_sandbox(admin)
    assert sb["verify_cmd"] is None
    assert set_checks(admin, sb["id"], "  npm test  ")["verify_cmd"] == "npm test"
    assert set_checks(admin, sb["id"], "")["verify_cmd"] is None
    too_long = admin.patch(f"/api/sandboxes/{sb['id']}", json={"verify_cmd": "x" * 1001}, headers=CSRF)
    assert too_long.status_code == 422
    hints = {t["name"]: t["check_hint"] for t in admin.get("/api/templates").json()}
    assert hints["python"] == "python -m pytest -q" and hints["static"] == ""


def test_final_check_passes_and_the_run_is_pushed(admin, svc, backend, workspace):
    sb = make_sandbox(admin, repo_url="https://github.com/example/playground")
    admin.patch(f"/api/sandboxes/{sb['id']}", json={"allow_push": True, "verify_cmd": "make test"}, headers=CSRF)
    backend.exec_replies = [("make test", reply(0, "12 passed"))]
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["state"] == "succeeded" and done["verify"] == "passed" and done["verify_cmd"] == "make test"
    assert workspace.pushed == [done["branch"]]
    checks = [e for e in admin.get(f"/api/runs/{done['id']}/log").json() if e.get("kind") == "check"]
    assert len(checks) == 1 and checks[0]["phase"] == "final" and checks[0]["passed"] and "12 passed" in checks[0]["text"]


def test_failing_final_check_is_recorded_and_never_pushed(admin, svc, backend, workspace):
    sb = make_sandbox(admin, repo_url="https://github.com/example/playground")
    admin.patch(f"/api/sandboxes/{sb['id']}", json={"allow_push": True, "verify_cmd": "make test"}, headers=CSRF)
    backend.exec_replies = [("make test", reply(1, "FAILED test_x\n" + "x" * 5000))]
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["state"] == "succeeded" and done["verify"] == "failed" and done["commit_sha"]
    assert workspace.pushed == []
    log = admin.get(f"/api/runs/{done['id']}/log").json()
    [check] = [e for e in log if e.get("kind") == "check"]
    assert not check["passed"] and check["exit_code"] == 1 and len(check["text"]) <= 3001
    assert any(e.get("text") == "Not pushed: the checks fail" for e in log)
    msg = run_message(svc.db.get_run(done["id"]), "demo")
    assert msg["kind"] == "run.failed" and "checks fail" in msg["title"] and "not pushed" in msg["body"]


def test_runs_without_checks_are_unchanged(admin, svc, backend):
    sb = make_sandbox(admin)
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    assert done["verify"] is None and done["verify_cmd"] is None
    assert run_message(svc.db.get_run(done["id"]), "demo")["title"] == "demo: run finished"
    assert not any(e.get("kind") == "check" for e in admin.get(f"/api/runs/{done['id']}/log").json())


def test_checks_are_not_run_after_a_failed_or_cancelled_run(admin, svc, backend):
    sb = make_sandbox(admin)
    set_checks(admin, sb["id"], "make test")
    done = wait_final(admin, start(admin, sb["id"], prompt="fail").json()["id"])
    assert done["state"] == "failed" and done["verify"] is None
    assert not any(c == ["bash", "-c", "make test"] for c, _ in backend.exec_calls)


# ---------------------------------------------------------------------------
# The Stop-hook gate
# ---------------------------------------------------------------------------

@pytest.fixture
def gated(admin, svc, runs, backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(settings, verify_max_repairs=2))
    sb = make_sandbox(admin)
    run = Run(id="c0ffee000001", sandbox_id=sb["id"], runner="claude-code", prompt="x", timeout=60, max_turns=5,
              state="running", verify_cmd="make test")
    svc.db.insert_run(run)
    return run


def stop(runs, run):
    return asyncio.run(check_before_stop(runs, run, HookCall("Stop", "1-1", {"stop_hook_active": False})))


def test_stop_gate_sends_the_agent_back_with_the_output(runs, backend, svc, gated):
    backend.exec_replies = [("make test", reply(2, "AssertionError: expected 3, got 4"))]
    r = stop(runs, gated)
    assert r.code == 2 and "make test" in r.stderr and "expected 3, got 4" in r.stderr
    assert "don't weaken or skip" in r.stderr and "last round" not in r.stderr
    assert svc.db.get_run(gated.id).verify_attempts == 1

    r = stop(runs, gated)  # second and last round
    assert r.code == 2 and "last round" in r.stderr

    assert stop(runs, gated) is None  # limit reached: the agent may stop
    assert stop(runs, gated) is None
    texts = [e.get("text") for e in svc.db.run_events(gated.id)]
    assert texts.count("Checks still fail after 2 rounds of fixes; the agent may stop") == 1
    checks = [e for e in svc.db.run_events(gated.id) if e.get("kind") == "check"]
    assert [c["attempt"] for c in checks] == [1, 2] and all(c["phase"] == "stop" for c in checks)


def test_stop_gate_lets_a_passing_agent_stop(runs, backend, svc, gated):
    backend.exec_replies = [("make test", reply(0, "ok"))]
    assert stop(runs, gated) is None
    assert svc.db.get_run(gated.id).verify_attempts == 0


def test_stop_hook_is_installed_only_where_there_are_checks(runs, svc, gated):
    sb = svc.get(gated.sandbox_id)
    spec = runs._spec(gated, sb)
    assert spec.hooks["Stop"] == config.settings.verify_timeout + 60 and "PreCompact" in spec.hooks
    spec = runs._spec(dataclasses.replace(gated, verify_cmd=None), sb)
    assert "Stop" not in spec.hooks
