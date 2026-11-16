"""Integration tests against the real rootless Docker daemon. Run as the agenthub user:

    sudo -u agenthub env XDG_RUNTIME_DIR=/run/user/$(id -u agenthub) \\
        DOCKER_HOST=unix:///run/user/$(id -u agenthub)/docker.sock \\
        /opt/agent-hub/.venv/bin/python -m pytest -m docker -p no:cacheprovider
"""

from __future__ import annotations

import subprocess

import pytest

from app.capacity import HostState
from app.db import Database
from app.docker_backend import MANAGED_LABEL, DockerBackend
from app.sandboxes import SandboxService

pytestmark = pytest.mark.docker


def host_ipv4s() -> list[str]:
    out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=True).stdout
    return [ip for ip in out.split() if ":" not in ip]


@pytest.fixture
def real(settings, tmp_path):
    backend = DockerBackend()
    if not backend.ping():
        pytest.skip("Docker daemon not reachable")
    backend.ensure_network()
    svc = SandboxService(Database(tmp_path / "it.db"), backend, host_state=HostState.read)
    created = []
    yield svc, created
    for sid in created:
        sb = svc.db.get_sandbox(sid)
        if sb and sb.state != "destroyed":
            svc.destroy(sid, "test")


def test_lifecycle_limits_and_egress(real):
    svc, created = real
    sb = svc.create(name="itest", template="python", repo_url=None, base_branch=None,
                    memory=1 * 2**30, cpus=1, ip="test")
    created.append(sb.id)
    assert sb.state == "running", sb.error

    r = svc.exec(sb.id, "python3 --version && id -un && id -u", None, "test")
    assert r.exit_code == 0 and r.output.split() == ["Python", r.output.split()[1], "agent", "1000"]

    attrs = svc.backend.client.containers.get(sb.container_id).attrs
    hc = attrs["HostConfig"]
    assert hc["Memory"] == hc["MemorySwap"] == 2**30
    assert hc["NanoCpus"] == 10**9 and hc["PidsLimit"] == 512
    assert hc["CapDrop"] == ["ALL"] and "no-new-privileges:true" in hc["SecurityOpt"]
    assert hc["Privileged"] is False and attrs["Config"]["User"] == "agent"
    assert [(m["Type"], m["Name"], m["Destination"]) for m in attrs["Mounts"]] == [("volume", sb.volume, "/workspace")]
    assert attrs["Config"]["Labels"][MANAGED_LABEL] == "true"

    r = svc.exec(sb.id, "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://pypi.org/simple/", 30, "test")
    assert r.output.strip() == "200", r.output

    targets = [f"{ip}:{p}" for ip in host_ipv4s() for p in (22, 8080)]
    targets += ["10.0.2.2:25575", "169.254.169.254:80"]
    for t in targets:
        host, port = t.rsplit(":", 1)
        r = svc.exec(sb.id, ["nc", "-z", "-w", "3", host, port], 15, "test")
        assert r.exit_code != 0, f"sandbox reached {t}; egress filter is not working"
    # No global IPv6 address in the sandbox (the egress filter only knows the host's own v6
    # addresses, so v6 to other LAN devices must not be possible at all).
    r = svc.exec(sb.id, ["sh", "-c", "awk '$4 == \"00\" {print $1}' /proc/net/if_inet6"], 15, "test")
    assert r.output.strip() == "", f"sandbox has a global IPv6 address: {r.output}"

    svc.destroy(sb.id, "test")
    assert svc.backend.status(sb.container_id) == "missing"
    assert not svc.backend.client.volumes.list(filters={"name": sb.volume})
    assert svc.backend._network(f"kiln-sb-{sb.id}") is None  # its network went with it


def test_sandboxes_are_isolated_and_reach_claude_only_through_the_gateway(real):
    """Each sandbox has its own network: it can't reach another sandbox, and it reaches the
    Claude gateway only while it has a key, which stops working once revoked."""
    from app import config
    from app.gateway import Gateway

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    a, b = _sandbox(svc, created, "iso-a"), _sandbox(svc, created, "iso-b")
    svc.exec(b.id, ["sh", "-c", "nohup python3 -m http.server 8000 >/dev/null 2>&1 &"], 10, "test")
    nets = svc.backend.client.containers.get(b.container_id).attrs["NetworkSettings"]["Networks"]
    assert list(nets) == [f"kiln-sb-{b.id}"]
    b_ip = nets[f"kiln-sb-{b.id}"]["IPAddress"]
    r = svc.exec(a.id, ["nc", "-z", "-w", "3", b_ip, "8000"], 15, "test")
    assert r.exit_code != 0, "sandbox a reached sandbox b"

    gw = Gateway(svc.backend)
    key = gw.issue(a.id, "itestgateway")
    probe = ('curl -sS --max-time 30 -H "Authorization: Bearer $K" -H "content-type: application/json" '
             '-H "anthropic-version: 2023-06-01" -d "{}" http://kiln-gateway:8080/v1/messages')
    try:
        r = svc.exec(a.id, ["sh", "-c", probe.replace("$K", key)], 60, "test")
        assert "kiln_gateway" not in r.output and "invalid_request_error" in r.output, r.output  # reached the API
        r = svc.exec(a.id, ["sh", "-c", probe.replace("$K", "sk-ant-oat01-kiln-guess")], 60, "test")
        assert "kiln_gateway" in r.output, r.output
        r = svc.exec(b.id, ["sh", "-c", probe.replace("$K", key)], 60, "test")
        assert r.exit_code != 0 and "Could not resolve" in r.output, r.output  # not on b's network
    finally:
        gw.revoke("itestgateway")
    r = svc.exec(a.id, ["sh", "-c", probe.replace("$K", key)], 60, "test")
    assert "kiln_gateway" in r.output, r.output  # revoked: refused by the gateway
    env = svc.exec(a.id, ["sh", "-c", "cat /proc/1/environ; env"], 15, "test").output
    assert config.settings.claude_code_oauth_token not in env


def test_stop_and_start_keep_workspace(real):
    svc, created = real
    sb = svc.create(name="persist", template="static", repo_url=None, base_branch=None,
                    memory=512 * 2**20, cpus=1, ip="test")
    created.append(sb.id)
    svc.exec(sb.id, "echo kept > /workspace/marker", None, "test")
    svc.stop(sb.id, "test")
    svc.start(sb.id, "test")
    assert svc.exec(sb.id, "cat /workspace/marker", None, "test").output.strip() == "kept"


def test_terminal_shell_runs_as_agent_and_is_killed_on_close(real):
    import asyncio

    svc, created = real
    sb = svc.create(name="term", template="python", repo_url=None, base_branch=None,
                    memory=512 * 2**20, cpus=1, ip="test")
    created.append(sb.id)

    async def session() -> tuple[bytes, str]:
        term = await svc.backend.open_terminal(sb.container_id, 100, 30)
        await term.write(b"python3 --version; echo \"who=$(id -un) size=$(stty size)\"; sleep 600 &\r")
        buf = b""
        async with asyncio.timeout(20):
            while b"who=agent" not in buf or not buf.rstrip().endswith(b"$"):
                chunk = await term.read()
                assert chunk, f"shell ended early: {buf!r}"
                buf += chunk
        await term.close()
        return buf, term.tag

    out, tag = asyncio.run(session())
    assert b"Python 3." in out and b"who=agent size=30 100" in out, out
    # Only the shell (tagged argv[0]) and the job it started; the container's own
    # `sleep infinity` keeps running, as it should.
    left = svc.exec(sb.id, ["sh", "-c", f"pgrep -f '^({tag}|sleep 600)' || true"], 15, "test")
    procs = svc.exec(sb.id, ["ps", "-eo", "pid,args"], 15, "test").output
    assert left.output.strip() == "", f"shell or its children survived close():\n{procs}"
    assert "sleep infinity" in procs  # the sandbox itself is untouched


# ---------------------------------------------------------------------------
# Agent runs (phase 3)
# ---------------------------------------------------------------------------

def _manager(svc):
    from app.runners import ClaudeCodeRunner, ScriptRunner
    from app.runs import RunManager
    from app.workspace import GitWorkspace

    return RunManager(svc.db, svc, {"script": ScriptRunner(svc.backend),
                                    "claude-code": ClaudeCodeRunner(svc.backend)}, GitWorkspace(svc.backend))


def _sandbox(svc, created, name):
    sb = svc.create(name=name, template="python", repo_url=None, base_branch=None,
                    memory=1 * 2**30, cpus=1, ip="test")
    created.append(sb.id)
    assert sb.state == "running", sb.error
    return sb


def test_claude_code_is_installed(real):
    svc, created = real
    sb = _sandbox(svc, created, "cli")
    r = svc.exec(sb.id, "claude --version && stat -c '%U %a' /usr/local/bin/claude", 60, "test")
    assert r.exit_code == 0 and "2.1.315" in r.output and "root 755" in r.output, r.output


def test_script_run_streams_and_commits(real):
    import asyncio

    svc, created = real
    sb = _sandbox(svc, created, "run-ok")
    runs = _manager(svc)
    run = runs.create(sandbox_id=sb.id, prompt="for i in 1 2 3; do echo step $i; sleep 0.2; done; "
                      "echo hello > hello.txt; echo oops >&2", runner="script", timeout=60, max_turns=1, ip="t")
    asyncio.run(runs._execute(run))
    done = svc.db.get_run(run.id)
    assert done.state == "succeeded" and done.exit_code == 0 and done.commit_sha, done
    texts = [(e.get("kind"), e.get("text")) for e in svc.db.run_events(run.id)]
    assert ("stdout", "step 3") in texts and ("stderr", "oops") in texts
    r = svc.exec(sb.id, f"git log -1 --format=%s {done.branch} && git show --stat {done.branch} | grep hello.txt",
                 30, "t")
    assert r.exit_code == 0 and f"Agent run {run.id}" in r.output, r.output


def test_cancel_and_timeout_leave_nothing_running(real):
    import asyncio

    svc, created = real
    sb = _sandbox(svc, created, "run-stop")
    runs = _manager(svc)
    script = "echo started; sleep 600 & sleep 600"

    async def cancel_after_start():
        run = runs.create(sandbox_id=sb.id, prompt=script, runner="script", timeout=600, max_turns=1, ip="t")
        task = runs.launch(run)
        for _ in range(100):
            if any(e.get("text") == "started" for e in svc.db.run_events(run.id)):
                break
            await asyncio.sleep(0.1)
        await runs.cancel(run.id, "t")
        await asyncio.wait_for(task, 30)
        return run.id

    rid = asyncio.run(cancel_after_start())
    assert svc.db.get_run(rid).state == "cancelled"
    left = svc.exec(sb.id, ["sh", "-c", "pgrep -af '^sleep 600' || true"], 15, "t").output.strip()
    assert left == "", f"cancel left processes: {left}"

    run = runs.create(sandbox_id=sb.id, prompt=script, runner="script", timeout=3, max_turns=1, ip="t")
    asyncio.run(runs._execute(run))
    assert svc.db.get_run(run.id).state == "timed_out"
    left = svc.exec(sb.id, ["sh", "-c", "pgrep -af '^sleep 600' || true"], 15, "t").output.strip()
    assert left == "", f"timeout left processes: {left}"


def test_real_claude_code_run(real):
    """One small real run with your Claude token (skipped if none is configured)."""
    import asyncio
    import json

    from app import config

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    sb = _sandbox(svc, created, "run-claude")
    runs = _manager(svc)
    run = runs.create(sandbox_id=sb.id, prompt="Create a file named hello.txt containing exactly the word hi. "
                      "Do nothing else.", runner="claude-code", timeout=300, max_turns=5, ip="t")
    asyncio.run(runs._execute(run))
    done = svc.db.get_run(run.id)
    evs = svc.db.run_events(run.id)
    assert done.state == "succeeded", (done.error, evs[-10:])
    assert done.commit_sha and done.num_turns
    assert config.settings.claude_code_oauth_token not in json.dumps(evs)
    r = svc.exec(sb.id, f"git show {done.branch}:hello.txt", 30, "t")
    assert r.output.strip() == "hi", r.output


def test_run_survives_a_kiln_restart(real):
    """Kiln dies mid-run (simulated: the first manager is dropped without any clean-up); a new
    one re-attaches to the agent still running in the sandbox and stores nothing twice."""
    import asyncio
    import json
    import time

    svc, created = real
    sb = _sandbox(svc, created, "run-restart")
    first = _manager(svc)
    run = first.create(sandbox_id=sb.id, prompt="echo one; sleep 4; echo two; echo done > out.txt",
                       runner="script", timeout=120, max_turns=1, ip="t")

    async def crash_after_first_line():
        # What RunManager._execute does, by hand, so it can stop without cleaning up.
        first._status(run, "running", started_at=time.time())
        base = await asyncio.to_thread(first.workspace.prepare, sb, run, lambda t: None)
        svc.db.update_run(run, base_branch=base)
        proc = await first.runners["script"].start(first._spec(run, sb))
        svc.db.update_run(run, runner_state=json.dumps(proc.state()))
        async for ev in proc.events():
            progress = ev.pop("_progress", False)
            first.emit(run, ev, proc.state() if progress else None)
            if ev.get("text") == "one":
                proc.stream.close()  # Kiln is gone; the agent isn't
                return

    asyncio.run(crash_after_first_line())
    second = _manager(svc)
    resumable = second.reconcile_on_startup()
    assert [r.id for r, _ in resumable] == [run.id]
    asyncio.run(second._execute(svc.db.get_run(run.id), resume=resumable[0][1]))
    done = svc.db.get_run(run.id)
    assert done.state == "succeeded" and done.commit_sha, done
    texts = [e.get("text") for e in svc.db.run_events(run.id)]
    assert texts.count("one") == 1 and texts.count("two") == 1, texts
    r = svc.exec(sb.id, f"git show {done.branch}:out.txt; ls /tmp/kiln-runs", 30, "t")
    assert r.output.split() == ["done"], r.output  # committed, and the run directory is cleaned up


def test_real_claude_code_takes_a_message_mid_run(real):
    """With your Claude token (skipped if none): a message sent while the agent works is read,
    acted on, and the run still ends by itself."""
    import asyncio

    from app import config

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    sb = _sandbox(svc, created, "run-message")
    runs = _manager(svc)
    run = runs.create(sandbox_id=sb.id, prompt="Run the shell command `sleep 15`, then create a file named a.txt "
                      "containing exactly the word a. Do nothing else.", runner="claude-code", timeout=300,
                      max_turns=10, ip="t")

    async def go():
        task = runs.launch(run)
        for _ in range(300):
            if any(e.get("kind") == "tool" for e in svc.db.run_events(run.id)):
                break
            await asyncio.sleep(0.1)
        await runs.send(run.id, "Also create a file named b.txt containing exactly the word b.", "t")
        await asyncio.wait_for(task, 300)

    asyncio.run(go())
    done = svc.db.get_run(run.id)
    evs = svc.db.run_events(run.id)
    assert done.state == "succeeded", (done.error, evs[-10:])
    assert any(e.get("kind") == "user" for e in evs), "the message never showed as read"
    for name, word in (("a.txt", "a"), ("b.txt", "b")):  # "exactly the word": maybe no newline
        r = svc.exec(sb.id, f"git show {done.branch}:{name}", 30, "t")
        assert r.output.strip() == word, (name, r.output)


def test_real_claude_code_obeys_kiln_hooks(real):
    """With your Claude token (skipped if none): Claude Code loads the root-owned managed
    settings Kiln installs, and a Kiln hook blocks a Bash command."""
    import asyncio

    from app import config
    from app.hooks import HookReply
    from app.runs import HookHandler

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    sb = _sandbox(svc, created, "run-hooks")
    runs = _manager(svc)
    asked: list[str] = []

    async def guard(manager, run, call):
        command = call.payload.get("tool_input", {}).get("command", "")
        asked.append(command)
        if "kiln-forbidden" in command:
            return HookReply(code=2, stderr="Kiln policy: this command is not allowed here.")
        return None

    runs.hooks.append(HookHandler("PreToolUse", guard, wait=30, matcher="Bash"))
    run = runs.create(sandbox_id=sb.id, prompt="Use the Bash tool to run exactly `echo kiln-forbidden > x.txt`. "
                      "If that is blocked, do not try another way: create a file named blocked.txt containing "
                      "exactly the word yes (with the Write tool) and stop.", runner="claude-code", timeout=300,
                      max_turns=8, ip="t")
    asyncio.run(runs._execute(run))
    done = svc.db.get_run(run.id)
    evs = svc.db.run_events(run.id)
    assert done.state == "succeeded", (done.error, evs[-10:])
    assert any("kiln-forbidden" in c for c in asked), asked
    assert any(e.get("kind") == "tool_result" and "Kiln policy" in (e.get("text") or "") for e in evs), evs[-10:]
    r = svc.exec(sb.id, f"git show {done.branch}:blocked.txt", 30, "t")
    assert r.output.strip() == "yes", r.output  # may have no trailing newline
    r = svc.exec(sb.id, f"git cat-file -e {done.branch}:x.txt 2>/dev/null && echo EXISTS; ls x.txt 2>/dev/null; true", 30, "t")
    assert r.output.strip() == "", r.output  # the blocked command never ran
    # The agent can read the hooks but not change them.
    r = svc.exec(sb.id, "stat -c '%U %a' /etc/claude-code/managed-settings.json /usr/local/lib/kiln/kiln-hook; "
                 "(echo x >> /etc/claude-code/managed-settings.json) 2>/dev/null && echo WRITABLE; true", 30, "t")
    assert r.output.split() == ["root", "644", "root", "755"], r.output  # and no WRITABLE


def test_real_claude_code_is_sent_back_by_failing_checks(real):
    """With your Claude token (skipped if none): the sandbox's checks need something the prompt
    doesn't mention. The agent's first stop is blocked with the check output, it fixes the
    problem, and the run ends with passing checks."""
    import asyncio

    from app import config

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    sb = _sandbox(svc, created, "run-checks")
    svc.update_settings(sb.id, "t", verify_cmd='[ "$(cat VERSION 2>/dev/null)" = "1.0" ] '
                        '|| { echo "check failed: a file named VERSION must contain exactly 1.0"; exit 1; }')
    runs = _manager(svc)
    run = runs.create(sandbox_id=sb.id, prompt="Create a file named hello.txt containing exactly the word hi, "
                      "then finish.", runner="claude-code", timeout=300, max_turns=12, ip="t")
    asyncio.run(runs._execute(run))
    done = svc.db.get_run(run.id)
    evs = svc.db.run_events(run.id)
    assert done.state == "succeeded", (done.error, evs[-10:])
    checks = [e for e in evs if e.get("kind") == "check"]
    assert checks and not checks[0]["passed"] and checks[0]["phase"] == "stop", checks
    assert done.verify == "passed" and done.verify_attempts >= 1, (done.verify, done.verify_attempts)
    assert checks[-1]["phase"] == "final" and checks[-1]["passed"]
    r = svc.exec(sb.id, f"git show {done.branch}:VERSION", 30, "t")
    assert r.output.strip() == "1.0", r.output


def test_real_claude_code_continues_a_run(real):
    """With your Claude token (skipped if none): a follow-up run resumes the first run's agent
    session (it remembers a word that was never written down) and builds on its branch."""
    import asyncio

    from app import config

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    sb = _sandbox(svc, created, "run-continue")
    runs = _manager(svc)
    first = runs.create(sandbox_id=sb.id, prompt="Remember this code word for later, but don't write it to any "
                        "file: PELICAN. Now create a file named a.txt containing exactly the word a.",
                        runner="claude-code", timeout=300, max_turns=6, ip="t")
    asyncio.run(runs._execute(first))
    assert svc.db.get_run(first.id).state == "succeeded"
    second = runs.create(sandbox_id=sb.id, prompt="Create a file named b.txt containing exactly the code word "
                         "I gave you earlier.", runner="claude-code", timeout=300, max_turns=6, ip="t",
                         continue_from=first.id)
    asyncio.run(runs._execute(second))
    done = svc.db.get_run(second.id)
    evs = svc.db.run_events(second.id)
    assert done.state == "succeeded", (done.error, evs[-10:])
    assert any("in the same agent session" in (e.get("text") or "") for e in evs), evs[:8]
    for name, word in (("a.txt", "a"), ("b.txt", "PELICAN")):  # a.txt comes from the first run
        r = svc.exec(sb.id, f"git show {done.branch}:{name}", 30, "t")
        assert r.output.strip() == word, (name, r.output)


def test_real_claude_code_asks_the_owner_and_is_metered(real):
    """With your Claude token (skipped if none): the agent uses kiln-ask, gets the owner's answer
    and acts on it; the gateway meters the run's API calls from the real responses."""
    import asyncio

    from app import config

    if not config.settings.claude_code_oauth_token:
        pytest.skip("no CLAUDE_CODE_OAUTH_TOKEN configured")
    svc, created = real
    sb = _sandbox(svc, created, "run-ask")
    runs = _manager(svc)
    run = runs.create(sandbox_id=sb.id, prompt="Use the shell command kiln-ask to ask me which colour to use, with "
                      "the options red and blue. Then create a file named colour.txt containing exactly the colour "
                      "I chose.", runner="claude-code", timeout=300, max_turns=10, ip="t")

    async def go():
        task = runs.launch(run)
        for _ in range(1200):
            pending = svc.db.list_requests(state="pending", run_id=run.id)
            if pending:
                await runs.owner.answer(pending[0]["id"], "blue", "blue", None, "t")
                break
            await asyncio.sleep(0.1)
        await asyncio.wait_for(task, 300)

    asyncio.run(go())
    done = svc.db.get_run(run.id)
    evs = svc.db.run_events(run.id)
    assert done.state == "succeeded", (done.error, evs[-10:])
    [req] = svc.db.list_requests(run_id=run.id)
    assert req["kind"] == "question" and req["state"] == "answered", req
    r = svc.exec(sb.id, f"git show {done.branch}:colour.txt", 30, "t")
    assert r.output.strip() == "blue", r.output
    from app.usage import RunUsage

    u = RunUsage.load(done.usage)
    assert u.requests > 0 and u.output > 0 and u.cost > 0 and u.model.startswith("claude-"), done.usage


# ---------------------------------------------------------------------------
# Files, diff, preview, stats, prune (phase 4)
# ---------------------------------------------------------------------------

def test_files_and_run_diff(real):
    import asyncio

    from app import files

    svc, created = real
    sb = _sandbox(svc, created, "review")
    runs = _manager(svc)
    run = runs.create(sandbox_id=sb.id, prompt="printf '# Title\\n\\nNew section\\n' > README.md; mkdir -p src; "
                      "echo 'print(1)' > src/main.py", runner="script", timeout=60, max_turns=1, ip="t")
    asyncio.run(runs._execute(run))
    run = svc.db.get_run(run.id)
    assert run.state == "succeeded" and run.commit_sha
    sb = svc.get(sb.id)
    root = files.browse(svc.backend, sb, "")
    assert {"README.md", "src", ".git"} <= {e["name"] for e in root["entries"]}
    assert files.browse(svc.backend, sb, "src/main.py")["content"] == "print(1)\n"
    with pytest.raises(files.PathError):
        files.browse(svc.backend, sb, "../etc/passwd")
    d = files.run_diff(svc.backend, sb, run)
    assert {f["path"] for f in d["files"]} == {"README.md", "src/main.py"} and "+New section" in d["patch"]


def test_preview_is_served_on_tailscale_ip_only(real, monkeypatch):
    import dataclasses
    import time

    from app import config, preview

    env = config.Settings()  # the server's real .env, not the test defaults
    if not env.previews_enabled:
        pytest.skip("PREVIEW_BIND / PREVIEW_HOST not configured")
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, preview_bind=env.preview_bind, preview_host=env.preview_host,
        preview_port_range=env.preview_port_range))
    svc, created = real
    sb = _sandbox(svc, created, "preview")
    assert sb.preview_port in config.settings.preview_ports
    preview.start_static(svc.backend, sb)
    for _ in range(20):
        if preview.status(svc.backend, sb)["listening"] == "any":
            break
        time.sleep(0.25)
    assert preview.status(svc.backend, sb)["static_server"] is True
    # Published on the Tailscale IP only. (An HTTP check from here can't work: the egress filter
    # stops agenthub from reaching the host's own addresses. smoke-test.sh checks it as you.)
    bindings = svc.backend.client.containers.get(sb.container_id).attrs["HostConfig"]["PortBindings"]
    assert bindings == {"8000/tcp": [{"HostIp": env.preview_bind, "HostPort": str(sb.preview_port)}]}, bindings
    preview.stop_static(svc.backend, sb)
    assert preview.status(svc.backend, sb)["static_server"] is False


def test_memory_stats_and_prune_keep_sandboxes(real):
    from app.templates import TEMPLATES

    svc, created = real
    sb = _sandbox(svc, created, "stats")
    used = svc.backend.memory_usage(sb.container_id)
    assert used is not None and 0 < used < sb.memory
    keep = {s.id for s in svc.db.list_sandboxes()}
    svc.backend.prune(keep, {t.image for t in TEMPLATES.values()})
    assert svc.backend.status(sb.container_id) == "running"
