"""The scripts that run an agent detached in its sandbox, executed for real with this host's bash
and coreutils (the sandbox images are Debian too). A tiny fake agent speaks Claude Code's
stream-json protocol: it echoes each message it reads (like --replay-user-messages) and answers
with a `result`, and exits when its stdin closes."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time

import pytest

from app import runners
from app.runners import (close_argv, follow_argv, kill_argv, launch_argv, parse_status, send_argv,
                         status_argv)

pytestmark = pytest.mark.skipif(not (sys.platform.startswith("linux") and shutil.which("setsid")
                                     and shutil.which("pkill")), reason="needs Linux with setsid and pkill")

AGENT = r"""
import json, sys, time
for line in sys.stdin:
    text = json.loads(line)["message"]["content"][0]["text"]
    print(json.dumps({"type": "user", "isReplay": True,
                      "message": {"role": "user", "content": [{"type": "text", "text": text}]}}), flush=True)
    if text == "slow":
        time.sleep(60)
    print(json.dumps({"type": "result", "is_error": False, "result": "ok:" + text}), flush=True)
print("agent stderr: bye", file=sys.stderr, flush=True)
"""


def msg(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


@pytest.fixture
def rid(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "RUN_ROOT", str(tmp_path / "runs"))
    (tmp_path / "agent.py").write_text(AGENT)
    run_id = "0123456789ab"
    yield run_id
    subprocess.run(kill_argv(run_id), capture_output=True, timeout=10)  # never leave an agent behind


def sh(argv, env=None, timeout=10):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})


def launch(rid, tmp_path, first: str, interactive=True):
    argv = launch_argv(rid, 120, [sys.executable, "-u", str(tmp_path / "agent.py")], interactive)
    t0 = time.monotonic()
    r = sh(argv, {"AH_MSG": msg(first)})
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 < 5  # returns as soon as the agent runs
    return r


class Follower:
    def __init__(self, rid, offset=0):
        self.p = subprocess.Popen(follow_argv(rid, offset), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.read = 0

    def line(self, timeout=10.0) -> dict:
        deadline = time.monotonic() + timeout
        buf = b""
        while not buf.endswith(b"\n"):
            left = deadline - time.monotonic()
            assert left > 0, f"no output line (got {buf!r})"
            ready, _, _ = select.select([self.p.stdout], [], [], left)
            if ready:
                ch = os.read(self.p.stdout.fileno(), 1)
                assert ch, f"follower ended (got {buf!r})"
                buf += ch
        self.read += len(buf)
        return json.loads(buf)

    def finish(self, timeout=10.0) -> tuple[int, str]:
        _, err = self.p.communicate(timeout=timeout)
        return self.p.returncode, err.decode()


def status(rid) -> str | None:
    return parse_status(sh(status_argv(rid)).stdout)


def test_messages_then_close_input(rid, tmp_path):
    launch(rid, tmp_path, "one")
    f = Follower(rid)
    assert f.line()["isReplay"] and f.line()["result"] == "ok:one"
    assert status(rid) == "ALIVE"  # idle, waiting for more input

    assert sh(send_argv(rid), {"AH_MSG": msg("two")}).returncode == 0
    assert f.line()["message"]["content"][0]["text"] == "two"
    assert f.line()["result"] == "ok:two"

    assert sh(close_argv(rid)).returncode == 0  # EOF: the agent exits, the follower with it
    code, err = f.finish()
    assert code == 0 and "agent stderr: bye" in err
    assert status(rid) == "DONE"
    assert sh(send_argv(rid), {"AH_MSG": msg("late")}).returncode == 3  # refused, doesn't hang


def test_follow_again_from_an_offset_after_a_restart(rid, tmp_path):
    launch(rid, tmp_path, "one")
    f = Follower(rid)
    f.line(), f.line()
    offset = f.read
    f.p.kill()  # Kiln goes away; the agent doesn't
    f.p.wait()

    assert sh(send_argv(rid), {"AH_MSG": msg("while kiln was down")}).returncode == 0
    time.sleep(0.5)
    g = Follower(rid, offset)
    assert g.line()["message"]["content"][0]["text"] == "while kiln was down"  # nothing repeated
    assert g.line()["result"] == "ok:while kiln was down"
    sh(close_argv(rid))
    assert g.finish()[0] == 0

    # Following a finished agent replays the rest and its exit status.
    h = Follower(rid, offset)
    assert h.line()["isReplay"] and h.line()["result"].startswith("ok:")
    assert h.finish()[0] == 0


def test_kill_ends_the_whole_session(rid, tmp_path):
    launch(rid, tmp_path, "slow")
    d = tmp_path / "runs" / rid
    for _ in range(100):
        if (d / "out").read_text():
            break
        time.sleep(0.05)
    holder = int((d / "holder").read_text())
    assert status(rid) == "ALIVE"

    assert sh(kill_argv(rid)).returncode == 0
    time.sleep(0.3)
    assert status(rid) == "GONE"
    with pytest.raises(ProcessLookupError):
        os.kill(holder, 0)
    f = Follower(rid)
    assert f.line()["isReplay"]
    assert f.finish()[0] == 125  # no exit status: the agent didn't finish


def test_non_interactive_agents_get_no_input(rid, tmp_path):
    argv = launch_argv(rid, 120, ["bash", "-c", "echo out; echo err >&2; exit 7"], interactive=False)
    assert sh(argv).returncode == 0
    f = Follower(rid)
    code, err = f.finish()
    assert code == 7 and "err" in err
    assert not (tmp_path / "runs" / rid / "in").exists()


def test_a_run_directory_is_never_reused(rid, tmp_path):
    launch(rid, tmp_path, "one")
    r = sh(launch_argv(rid, 120, ["true"], True), {"AH_MSG": msg("again")})
    assert r.returncode != 0 and "run directory" in r.stderr
