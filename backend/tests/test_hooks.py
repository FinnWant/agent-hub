"""Kiln-managed Claude Code hooks: the kiln-hook script (run for real with bash), the managed
settings, and how RunManager answers hooks (fake Docker)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from app import config, hooks
from app.hooks import HOOK_SCRIPT, HookCall, HookReply, managed_settings, provision_entries
from app.runners import ClaudeCodeRunner, run_dir
from app.runs import HookHandler
from tests.conftest import CSRF

TOKEN = "sk-ant-oat01-" + "Q" * 40

needs_linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs the bash hook script")


@pytest.fixture
def dirs(tmp_path):
    script = tmp_path / "kiln-hook"
    script.write_text(HOOK_SCRIPT)
    script.chmod(0o755)
    d, r = tmp_path / "run", tmp_path / "replies"
    d.mkdir()
    r.mkdir()
    (d / "out").write_text("")
    return script, d, r


def hook(dirs, event, wait, payload, timeout=20):
    script, d, r = dirs
    return subprocess.run([str(script), str(d), str(r), event, str(wait)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=timeout)


def markers(d):
    return [json.loads(line) for line in (d / "out").read_text().splitlines()]


@needs_linux
def test_hook_that_doesnt_wait_leaves_a_marker_and_its_input(dirs):
    _, d, _ = dirs
    r = hook(dirs, "PreCompact", 0, {"hook_event_name": "PreCompact", "trigger": "auto"})
    assert r.returncode == 0 and r.stdout == ""
    [m] = markers(d)
    assert m["type"] == "kiln_hook" and m["event"] == "PreCompact" and hooks.HOOK_ID.fullmatch(m["id"])
    saved = d / f"hook-{m['id']}.json"
    assert json.loads(saved.read_text())["trigger"] == "auto"
    assert oct(saved.stat().st_mode & 0o777) == "0o600"


def answer_when_asked(dirs, reply: bytes, delay=0.3):
    """Play Kiln: wait for the marker, then write the reply file (possibly in two halves)."""
    _, d, r = dirs

    def run():
        for _ in range(100):
            if (d / "out").read_text():
                break
            time.sleep(0.05)
        hook_id = markers(d)[-1]["id"]
        time.sleep(delay)
        (r / hook_id).write_bytes(reply)

    t = threading.Thread(target=run)
    t.start()
    return t


@needs_linux
def test_waiting_hook_returns_kilns_decision(dirs):
    deny = HookReply(stdout={"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                    "permissionDecisionReason": "not in this sandbox"}})
    t = answer_when_asked(dirs, deny.render())
    r = hook(dirs, "PreToolUse", 10, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
    t.join()
    assert r.returncode == 0 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@needs_linux
def test_exit_2_sends_the_reason_on_stderr(dirs):
    t = answer_when_asked(dirs, HookReply(code=2, stderr="Tests fail:\nline 2").render())
    r = hook(dirs, "Stop", 10, {"stop_hook_active": False})
    t.join()
    assert r.returncode == 2 and r.stderr == "Tests fail:\nline 2\n" and r.stdout == ""


@needs_linux
def test_half_written_reply_is_ignored_and_no_reply_means_carry_on(dirs):
    t = answer_when_asked(dirs, b"2\nerr\nno end marker yet", delay=0.1)
    t0 = time.monotonic()
    r = hook(dirs, "Stop", 2, {})
    t.join()
    assert r.returncode == 0 and r.stderr == "" and 0.9 < time.monotonic() - t0 < 6  # bash SECONDS: whole seconds


@needs_linux
def test_hook_outside_a_run_does_nothing(tmp_path, dirs):
    script, _, r = dirs
    p = subprocess.run([str(script), str(tmp_path / "nope"), str(r), "Stop", "5"], input="{}",
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and p.stdout == ""


def test_managed_settings_shape():
    s = managed_settings("/tmp/kiln-runs/abc", "abc", {"PreToolUse": 30, "PreCompact": 0}, {"PreToolUse": "Bash"})
    pre = s["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Bash" and pre["hooks"][0]["type"] == "command"
    assert pre["hooks"][0]["command"] == f"{hooks.HOOK_COMMAND} /tmp/kiln-runs/abc /var/lib/kiln/replies/abc PreToolUse 30"
    assert pre["hooks"][0]["timeout"] == 45  # outlives the wait, so Kiln's "no decision" wins
    assert "matcher" not in s["hooks"]["PreCompact"][0]
    assert "allowManagedHooksOnly" not in s and "disableAllHooks" not in s  # repos' own hooks still run
    with pytest.raises(ValueError):
        managed_settings("/d", "abc", {"Pre Tool; rm -rf /": 0})
    modes = {p: m for p, _, m in provision_entries("/d", "abc", {})}
    assert modes["etc/claude-code/managed-settings.json"] == 0o644 and modes["usr/local/lib/kiln/kiln-hook"] == 0o755


# ---------------------------------------------------------------------------
# RunManager: installing hooks and answering them
# ---------------------------------------------------------------------------

def jl(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def test_runs_install_hooks_and_answer_them(admin, svc, runs, backend, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        settings, claude_code_oauth_token=TOKEN, claude_token_mode="direct"))
    runs.runners["claude-code"] = ClaudeCodeRunner(backend)
    seen: list[HookCall] = []

    async def guard(manager, run, call):
        seen.append(call)
        if "rm -rf" in call.payload.get("tool_input", {}).get("command", ""):
            return HookReply(stdout={"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                            "permissionDecision": "deny",
                                                            "permissionDecisionReason": "no"}})
        return None

    runs.hooks.append(HookHandler("PreToolUse", guard, wait=30, matcher="Bash"))
    sb = admin.post("/api/sandboxes", json={"name": "hooks", "template": "python", "memory": "1g"},
                    headers=CSRF).json()
    cid = svc.get(sb["id"]).container_id
    backend.stream_script = [
        (0.05, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "111-1"})),
        (0, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "222-2"})),
        (0, 1, jl({"type": "kiln_hook", "event": "PreCompact", "id": "333-3"})),
        (0, 1, jl({"type": "kiln_hook", "event": "PreToolUse", "id": "../../etc/x"})),  # ignored
        (0.3, 1, jl({"type": "result", "is_error": False, "result": "done"})),
    ]
    rid_holder: dict[str, str] = {}

    real_put_tree = backend.put_tree

    def put_tree(container_id, entries):  # payloads appear once the run (and its id) exists
        real_put_tree(container_id, entries)
        if "etc/claude-code/managed-settings.json" in [p for p, _, _ in entries]:
            settings_json = json.loads(dict((p, d) for p, d, _ in entries)["etc/claude-code/managed-settings.json"])
            rid = settings_json["hooks"]["PreToolUse"][0]["hooks"][0]["command"].split()[1].rsplit("/", 1)[-1]
            rid_holder["rid"] = rid
            for hid, cmd in (("111-1", "rm -rf /workspace"), ("222-2", "ls")):
                backend.files[(cid, f"{run_dir(rid)}/hook-{hid}.json")] = json.dumps(
                    {"tool_name": "Bash", "tool_input": {"command": cmd}}).encode()
            backend.files[(cid, f"{run_dir(rid)}/hook-333-3.json")] = b'{"trigger": "auto"}'

    monkeypatch.setattr(backend, "put_tree", put_tree)
    rid = admin.post(f"/api/sandboxes/{sb['id']}/runs", json={"prompt": "go"}, headers=CSRF).json()["id"]
    for _ in range(300):
        if admin.get(f"/api/runs/{rid}").json()["state"] not in ("queued", "running"):
            break
        time.sleep(0.02)
    assert admin.get(f"/api/runs/{rid}").json()["state"] == "succeeded"
    assert rid_holder["rid"] == rid

    # Installed, root-owned, before the agent started
    s = json.loads(backend.files[(cid, "/etc/claude-code/managed-settings.json")])
    assert s["hooks"]["PreToolUse"][0]["matcher"] == "Bash" and "PreCompact" in s["hooks"]
    assert backend.file_modes[(cid, "/usr/local/lib/kiln/kiln-hook")] == 0o755

    # Both PreToolUse calls were answered: a deny, and "no decision" so the hook stops waiting
    for _ in range(100):
        if (cid, f"/var/lib/kiln/replies/{rid}/222-2") in backend.files:
            break
        time.sleep(0.02)
    deny = backend.files[(cid, f"/var/lib/kiln/replies/{rid}/111-1")].decode()
    assert deny.startswith("0\nout\n") and '"deny"' in deny and deny.endswith("#end\n")
    assert backend.files[(cid, f"/var/lib/kiln/replies/{rid}/222-2")] == b"0\nout\n\n#end\n"
    assert not any("333-3" in p for _, p in backend.files if p.startswith("/var/lib/kiln/replies"))  # doesn't wait
    assert sorted(c.id for c in seen) == ["111-1", "222-2"]

    log = admin.get(f"/api/runs/{rid}/log").json()
    assert not any(e["type"] == "hook" for e in log)
    assert any("compacted (automatically" in (e.get("text") or "") for e in log)
