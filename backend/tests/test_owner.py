"""Metering, budgets, approvals and questions (usage.py, owner.py), with fake Docker; plus the
kiln-ask script under bash."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time

import pytest

from app import config, gateway
from app.db import Run
from app.hooks import ASK_SCRIPT, HOOK_SCRIPT
from app.owner import match_rule, parse_rules, rules_matcher
from app.runners import ClaudeCodeRunner, run_dir
from app.usage import RunUsage, call_cost, price
from tests.conftest import CSRF
from tests.test_runs import make_sandbox, wait_final

TOKEN = "sk-ant-oat01-" + "Q" * 40


# ---------------------------------------------------------------------------
# Prices and rules
# ---------------------------------------------------------------------------

def test_prices():
    assert price("claude-opus-5-5")[0] == "claude-opus-5-5"
    assert price("claude-opus-5")[0] == "claude-opus-5"  # not confused with opus-5-5
    assert price("claude-sonnet-5-5-20261101")[0] == "claude-sonnet-5-5"
    assert price("gpt-9") is None and call_cost({"m": "gpt-9", "in": 10}) is None
    # Opus 5.5: $4 in, $20 out, $0.20 cache read; cache writes 1.25x (5 min) / 2x (1 h)
    c = call_cost({"m": "claude-opus-5-5", "in": 1_000_000, "out": 1_000_000, "cr": 1_000_000,
                   "cw": 2_000_000, "cw1h": 1_000_000})
    assert c == pytest.approx(4 + 20 + 0.20 + 5 + 8)
    # Haiku 5.5 bills prompts over 100K tokens at its long-context rate
    assert call_cost({"m": "claude-haiku-5-5", "in": 50_000}) == pytest.approx(0.005)
    assert call_cost({"m": "claude-haiku-5-5", "in": 200_000}) == pytest.approx(0.1)


def test_run_usage_totals_and_main_model():
    u = RunUsage()
    u.add({"m": "claude-opus-5-5", "in": 10, "cr": 5000, "cw": 300, "out": 800})
    u.add({"m": "claude-haiku-5-5", "in": 900, "out": 20})          # a small side call
    u.add({"m": "mystery", "in": 1, "out": 1})
    assert (u.requests, u.input, u.cache_read, u.cache_write, u.output) == (3, 911, 5000, 300, 821)
    assert u.model == "claude-opus-5-5" and u.context == 5310 and u.unpriced == 1
    again = RunUsage.load(u.dump())
    assert again == u and "offset" not in again.public()


def test_approval_rules():
    rules = parse_rules("# comments are skipped\nrm -rf\nBash: git push\nEdit: .github/workflows/\nWrite: .env\n\n")
    assert rules == [("Bash", "rm -rf"), ("Bash", "git push"), ("Edit", ".github/workflows/"), ("Write", ".env")]
    assert rules_matcher(rules) == "Bash|Edit|MultiEdit|NotebookEdit|Write"
    bash = {"tool_name": "Bash", "tool_input": {"command": "cd x && rm -rf build"}}
    assert match_rule(rules, bash)[:2] == ("Bash", "rm -rf")
    edit = {"tool_name": "MultiEdit", "tool_input": {"file_path": "/workspace/.github/workflows/ci.yml"}}
    assert match_rule(rules, edit)[1] == ".github/workflows/"
    assert match_rule(rules, {"tool_name": "Read", "tool_input": {"file_path": ".env"}}) is None
    assert match_rule(rules, {"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


# ---------------------------------------------------------------------------
# A Claude Code run through the gateway (fake Docker)
# ---------------------------------------------------------------------------

@pytest.fixture
def live(admin, svc, runs, backend, settings, monkeypatch):
    """A Claude run that stays up while the test drives it. Returns helpers."""
    s = dataclasses.replace(settings, claude_code_oauth_token=TOKEN, meter_interval=0.05, approval_timeout=30,
                            budget_wait=30, ask_wait=10)
    monkeypatch.setattr(config, "settings", s)
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    sent = []
    runs.owner.notify = sent.append

    class Live:
        notified = sent

        def start(self, script, **sb_settings):
            sb = make_sandbox(admin)
            if sb_settings:
                assert admin.patch(f"/api/sandboxes/{sb['id']}", json=sb_settings, headers=CSRF).status_code == 200
            self.cid = svc.get(sb["id"]).container_id
            backend.stream_script = script
            r = admin.post(f"/api/sandboxes/{sb['id']}/runs", json={"prompt": "go"}, headers=CSRF)
            assert r.status_code == 201, r.text
            self.rid = r.json()["id"]
            for _ in range(200):
                if backend.gateway and backend.gateway["keys"]:
                    break
                time.sleep(0.01)
            self.digest = next(k for k in backend.gateway["keys"] if len(k) == 64)
            return self.rid

        def usage(self, *recs):
            usage = backend.gateway.setdefault("usage", {})
            usage[self.digest] = usage.get(self.digest, b"") + b"".join(json.dumps(r).encode() + b"\n" for r in recs)

        def payload(self, hook_id, data):
            backend.files[(self.cid, f"{run_dir(self.rid)}/hook-{hook_id}.json")] = (
                data if isinstance(data, bytes) else json.dumps(data).encode())

        def reply(self, hook_id, timeout=5.0):
            key = (self.cid, f"/var/lib/kiln/replies/{self.rid}/{hook_id}")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if key in backend.files:
                    return backend.files[key].decode()
                time.sleep(0.02)
            return None

        def pending(self):
            return admin.get("/api/requests", params={"state": "pending", "run": self.rid}).json()

        def wait_pending(self, n=1):
            for _ in range(250):
                if len(p := self.pending()) >= n:
                    return p
                time.sleep(0.02)
            raise AssertionError("no request")

        def answer(self, req_id, **body):
            return admin.post(f"/api/requests/{req_id}/answer", json=body, headers=CSRF)

    return Live()


def jl(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


RESULT = (0, 1, jl({"type": "result", "is_error": False, "result": "done"}))


def test_usage_is_metered_onto_the_run(admin, live):
    rid = live.start([(0.6, 1, b"working\n"), RESULT])
    live.usage({"m": "claude-opus-5-5", "in": 1000, "cr": 0, "cw": 0, "cw1h": 0, "out": 500, "s": 200})
    done = wait_final(admin, rid)
    assert done["usage"]["requests"] == 1 and done["usage"]["output"] == 500
    assert done["cost_est"] == pytest.approx((1000 * 4 + 500 * 20) / 1e6, abs=1e-4)
    assert done["usage"]["model"] == "claude-opus-5-5" and "offset" not in done["usage"]


def test_over_budget_holds_the_agent_until_extended(admin, live, backend):
    rid = live.start([(3, 1, b"working\n"), RESULT], budget_usd=0.01)
    assert admin.get(f"/api/runs/{rid}").json()["budget_usd"] == 0.01
    live.usage({"m": "claude-opus-5-5", "in": 1000, "out": 1000})  # ~$0.024
    [req] = live.wait_pending()
    assert req["kind"] == "budget" and req["options"] == ["Extend", "Stop"]
    assert f"{live.digest}.hold" in backend.gateway["keys"]
    assert any(n["kind"] == "run.needs_you" for n in live.notified)
    assert live.answer(req["id"], choice="extend").status_code == 409  # needs an amount
    r = live.answer(req["id"], choice="extend", amount=1)
    assert r.status_code == 200 and r.json()["state"] == "answered"
    assert f"{live.digest}.hold" not in backend.gateway["keys"]
    done = wait_final(admin, rid)
    assert done["state"] == "succeeded" and done["budget_usd"] == pytest.approx(1.01)
    assert done["paused_seconds"] > 0  # waiting on the owner doesn't count against the time limit
    texts = [e.get("text") or "" for e in admin.get(f"/api/runs/{rid}/log").json()]
    assert any("Budget raised to $1.01" in t for t in texts)


def test_over_budget_and_stopped(admin, live):
    rid = live.start([(5, 1, b"working\n"), RESULT], budget_usd=0.01)
    live.usage({"m": "claude-opus-5-5", "in": 1000, "out": 1000})
    [req] = live.wait_pending()
    assert live.answer(req["id"], choice="stop").status_code == 200
    assert wait_final(admin, rid)["state"] == "cancelled"
    assert live.answer(req["id"], choice="stop").status_code == 409  # already answered


def test_risky_tool_call_waits_for_approval(admin, live, svc):
    rules = "rm -rf\nEdit: .github/workflows/"
    rid = live.start([(0.2, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "11-1"})),
                      (0, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "22-2"})),
                      (0, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "33-3"})),
                      (2.5, 1, b"working\n"), RESULT], approval_rules=rules)
    live.payload("11-1", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /workspace/build"}})
    live.payload("22-2", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    live.payload("33-3", {"tool_name": "Edit", "tool_input": {"file_path": "/workspace/.github/workflows/ci.yml"}})
    assert live.reply("22-2") == "0\nout\n\n#end\n"  # no rule matched: no decision
    reqs = live.wait_pending(2)
    by_title = {r["title"]: r for r in reqs}
    bash = by_title["Bash: rm -rf /workspace/build"]
    edit = next(r for r in reqs if r["title"].startswith("Edit:"))
    assert bash["kind"] == "approval" and bash["options"] == ["Allow", "Deny"] and "rm -rf" in bash["body"]
    assert live.answer(bash["id"], choice="deny", text="not that folder").status_code == 200
    deny = live.reply("11-1")
    assert deny.startswith("2\nerr\n") and "not that folder" in deny and "another way" in deny
    assert live.answer(edit["id"], choice="allow").status_code == 200
    assert live.reply("33-3") == "0\nout\n\n#end\n"
    done = wait_final(admin, rid)
    assert done["state"] == "succeeded"
    installed = json.loads(svc.backend.files[(live.cid, "/etc/claude-code/managed-settings.json")])
    pre = installed["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Bash|Edit|MultiEdit|NotebookEdit"
    log = admin.get(f"/api/runs/{rid}/log").json()
    assert [e["request_kind"] for e in log if e.get("kind") == "request"] == ["approval", "approval"]
    assert any(e.get("kind") == "answer" and "not that folder" in e["text"] for e in log)


def test_unanswered_approval_is_denied(admin, live, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, approval_timeout=1))
    rid = live.start([(0.2, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "44-4"})),
                      (2.5, 1, b"working\n"), RESULT], approval_rules="git push")
    live.payload("44-4", {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})
    reply = live.reply("44-4", timeout=8)
    assert reply.startswith("2\nerr\n") and "didn't answer" in reply
    wait_final(admin, rid)
    states = [r["state"] for r in admin.get("/api/requests", params={"run": rid}).json()]
    assert states == ["expired"]


def test_kiln_ask_answered_in_time(admin, live):
    rid = live.start([(0.2, 1, jl({"type": "kiln_hook", "event": "KilnAsk", "id": "55-5"})),
                      (2, 1, b"working\n"), RESULT])
    live.payload("55-5", b"Which database?\nIt matters for the schema.\n\x1e\nPostgres\nSQLite\n")
    [req] = live.wait_pending()
    assert req["kind"] == "question" and req["title"] == "Which database?" and req["options"] == ["Postgres", "SQLite"]
    assert live.answer(req["id"], choice="postgres").status_code == 200
    assert live.reply("55-5") == "0\nout\nThe owner answered: postgres\n#end\n"
    wait_final(admin, rid)


def test_kiln_ask_answered_late_arrives_as_a_message(admin, live, backend, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, ask_wait=6))  # waits 1 s
    rid = live.start([(0.2, 1, jl({"type": "kiln_hook", "event": "KilnAsk", "id": "66-6"})),
                      (4, 1, b"working\n"), RESULT])
    live.payload("66-6", b"Keep the old API?\n\x1e\n")
    reply = live.reply("66-6", timeout=6)
    assert reply.startswith("0\nout\nNo answer yet")
    [req] = live.pending()  # still open
    assert live.answer(req["id"], text="Yes, keep it for a release").status_code == 200
    sent = [e["AH_MSG"] for c, e in backend.exec_calls if isinstance(c, list) and c[3:4] == ["ah-send"]]
    assert sent and "Yes, keep it for a release" in json.loads(sent[-1])["message"]["content"][0]["text"]
    wait_final(admin, rid)


def test_requests_close_when_the_run_ends_and_after_a_restart(admin, live, runs, svc):
    rid = live.start([(0.2, 1, jl({"type": "kiln_hook", "event": "KilnAsk", "id": "77-7"})),
                      (0.3, 1, b"working\n"), RESULT])
    live.payload("77-7", b"Anything?\n\x1e\n")
    live.wait_pending()
    wait_final(admin, rid)
    assert [r["state"] for r in admin.get("/api/requests", params={"run": rid}).json()] == ["closed"]
    # A request left open by a Kiln that stopped, for a run that isn't followed again: closed.
    svc.db.insert_request({"id": "aaaaaaaaaaaa", "run_id": "nope", "sandbox_id": "x", "kind": "question",
                           "state": "pending", "title": "t", "body": "b", "options": "[]", "answer": None,
                           "created_at": time.time(), "answered_at": None, "deadline": None, "hook_id": None,
                           "data": "{}"})
    runs.owner.restore(set())
    assert svc.db.get_request("aaaaaaaaaaaa")["state"] == "closed"


def test_owner_settings_api(admin):
    sb = make_sandbox(admin)
    r = admin.patch(f"/api/sandboxes/{sb['id']}", json={"budget_usd": 2.5, "approval_rules": " rm -rf \n"},
                    headers=CSRF).json()
    assert r["budget_usd"] == 2.5 and r["approval_rules"] == "rm -rf"
    r = admin.patch(f"/api/sandboxes/{sb['id']}", json={"budget_usd": 0, "approval_rules": ""}, headers=CSRF).json()
    assert r["budget_usd"] is None and r["approval_rules"] is None
    assert admin.patch(f"/api/sandboxes/{sb['id']}", json={"budget_usd": -1}, headers=CSRF).status_code == 422
    assert admin.post("/api/requests/nope/answer", json={"choice": "allow"}, headers=CSRF).status_code == 404


# ---------------------------------------------------------------------------
# kiln-ask under bash
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs bash")
def test_kiln_ask_script(tmp_path):
    hook = tmp_path / "kiln-hook"
    hook.write_text(HOOK_SCRIPT)
    hook.chmod(0o755)
    ask = tmp_path / "kiln-ask"
    ask.write_text(ASK_SCRIPT.replace("/usr/local/lib/kiln/kiln-hook", str(hook)))
    ask.chmod(0o755)
    d, r = tmp_path / "run", tmp_path / "replies"
    d.mkdir()
    r.mkdir()
    (d / "out").write_text("")
    env = {**os.environ, "KILN_RUN_DIR": str(d), "KILN_REPLY_DIR": str(r), "KILN_ASK_WAIT": "1"}
    p = subprocess.run([str(ask), "Which one?", "A b", "C"], env=env, capture_output=True, text=True, timeout=10)
    assert p.returncode == 0
    marker = json.loads((d / "out").read_text())
    assert marker["event"] == "KilnAsk"
    assert (d / f"hook-{marker['id']}.json").read_bytes() == b"Which one?\n\x1e\nA b\nC\n"
    p = subprocess.run([str(ask)], env=env, capture_output=True, text=True, timeout=10)
    assert p.returncode == 64 and "usage" in p.stderr
    p = subprocess.run([str(ask), "q"], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=10)
    assert p.returncode == 69
