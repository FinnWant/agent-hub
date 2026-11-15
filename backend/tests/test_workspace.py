"""GitWorkspace against real git: the sandbox scripts run in a temp directory (standing in
for /workspace) and a local bare repository stands in for GitHub."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from app import config, workspace as ws_mod
from app.db import Run, Sandbox
from app.docker_backend import ExecResult
from app.workspace import GitWorkspace, WorkspaceError, commit_message

GH_TOKEN = "github_pat_" + "T" * 60


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


class LocalBackend:
    """Runs the 'container' commands in a local directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ws = root / "workspace"
        self.ws.mkdir(parents=True)
        self.calls: list[tuple[list[str], dict]] = []

    def exec(self, container_id, cmd, timeout, env=None):
        self.calls.append((cmd, env or {}))
        # Like the sandbox user: no git identity to fall back on (git must not guess one).
        e = {"PATH": os.environ["PATH"], "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "user.useConfigOnly", "GIT_CONFIG_VALUE_0": "true", **(env or {})}
        r = subprocess.run(cmd, cwd=self.ws, env=e, capture_output=True, text=True, timeout=timeout)
        return ExecResult(exit_code=r.returncode, output=r.stdout + r.stderr, truncated=False, timed_out=False)

    def put_file(self, container_id, directory, name, data):
        Path(directory, name).write_bytes(data)

    def get_file(self, container_id, path, max_size):
        return Path(path).read_bytes()


@pytest.fixture
def origin(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "trunk", cwd=seed)
    (seed / "README.md").write_text("# Playground\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "first", cwd=seed)
    bare = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def env(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(settings, github_token=GH_TOKEN))
    backend = LocalBackend(tmp_path / "box")
    (tmp_path / "box").mkdir(exist_ok=True)
    w = GitWorkspace(backend, in_bundle=str(tmp_path / "box" / "in.bundle"),
                     out_bundle=str(tmp_path / "box" / "out.bundle"))
    return w, backend


def sandbox(url=None, base=None, allow_push=False):
    return Sandbox(id="s1", name="s", template="python", volume="v", memory=1, cpus=1, state="running",
                   container_id="c1", repo_url=url, base_branch=base, allow_push=allow_push)


def run(rid="0123456789ab"):
    return Run(id=rid, sandbox_id="s1", runner="claude-code", prompt="Add a README section\nwith detail",
               timeout=60, max_turns=5, state="running", branch=f"agent/{rid}")


def test_clone_branch_commit_push(env, origin, monkeypatch):
    w, b = env
    sb, r, logs = sandbox(url=f"file://{origin}", allow_push=True), run(), []
    r.base_branch = w.prepare(sb, r, logs.append)
    assert r.base_branch == "trunk"  # the repo's default branch
    assert git("symbolic-ref", "--short", "HEAD", cwd=b.ws) == r.branch
    assert git("remote", "get-url", "origin", cwd=b.ws) == f"file://{origin}"
    assert any("Cloning" in line for line in logs)

    (b.ws / "README.md").write_text("# Playground\n\n## About\nA test.\n")
    fin = w.finalize(sb, r, logs.append)
    assert fin.committed_now and fin.commits == 1
    assert git("log", "-1", "--format=%an <%ae>%n%s", cwd=b.ws).splitlines() == [
        "Kiln <agent-hub@users.noreply.github.com>", "Agent run 0123456789ab: Add a README section"]

    # The agent plants traps in its repo; none of them may matter for the push.
    marker = b.root / "hook-ran"
    hook = b.ws / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git("config", "url.file:///nonexistent/.insteadOf", f"file://{origin}", cwd=b.ws)
    git("config", "credential.helper", f"!touch {marker}", cwd=b.ws)

    monkeypatch.setattr(ws_mod, "github_repo", lambda url: True)
    assert w.push(sb, r, logs.append) is True
    assert git("rev-parse", r.branch, cwd=origin) == fin.commit_sha
    assert git("rev-parse", "trunk", cwd=origin) != fin.commit_sha  # main branch untouched
    assert not marker.exists()
    assert all(GH_TOKEN not in str(env_) for _, env_ in b.calls)  # token never sent into the sandbox


def test_push_rules(env, origin, monkeypatch):
    w, b = env
    sb, r = sandbox(url=f"file://{origin}", allow_push=True), run()
    r.base_branch = w.prepare(sb, r, lambda _: None)
    (b.ws / "x").write_text("x")
    w.finalize(sb, r, lambda _: None)
    logs = []
    assert w.push(sb, r, logs.append) is False and "github.com" in logs[-1]  # not a GitHub URL
    monkeypatch.setattr(ws_mod, "github_repo", lambda url: True)
    assert w.push(dataclasses.replace(sb, allow_push=False), r, logs.append) is False
    bad = dataclasses.replace(r, branch="main")
    with pytest.raises(WorkspaceError, match="Refusing"):
        w.push(sb, bad, logs.append)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, github_token=""))
    assert w.push(sb, r, logs.append) is False and "no GitHub token" in logs[-1]


def test_no_repo_initialises_one(env):
    w, b = env
    (b.ws / "notes.txt").write_text("hi")
    sb, r = sandbox(), run()
    r.base_branch = w.prepare(sb, r, lambda _: None)
    assert r.base_branch == "main"
    assert "notes.txt" in git("show", "--stat", "main", cwd=b.ws)
    fin = w.finalize(sb, r, lambda _: None)
    assert fin.commits == 0 and not fin.committed_now  # agent changed nothing


def test_second_run_starts_from_base_and_dirty_tree_is_refused(env, origin):
    w, b = env
    sb = sandbox(url=f"file://{origin}")
    r1 = run("aaaaaaaaaaaa")
    r1.base_branch = sb.base_branch = w.prepare(sb, r1, lambda _: None)
    (b.ws / "a.txt").write_text("a")
    w.finalize(sb, r1, lambda _: None)
    r2 = run("bbbbbbbbbbbb")
    r2.base_branch = w.prepare(sb, r2, lambda _: None)
    assert not (b.ws / "a.txt").exists()  # branched from trunk, not from the previous run
    (b.ws / "dirty.txt").write_text("d")
    with pytest.raises(WorkspaceError, match="uncommitted"):
        w.prepare(sb, run("cccccccccccc"), lambda _: None)


def test_files_without_repo_block_clone(env, origin):
    w, b = env
    (b.ws / "stuff").write_text("x")
    with pytest.raises(WorkspaceError, match="no git repository"):
        w.prepare(sandbox(url=f"file://{origin}"), run(), lambda _: None)


def test_missing_base_branch(env, origin):
    w, _ = env
    with pytest.raises(WorkspaceError, match="doesn't exist"):
        w.prepare(sandbox(url=f"file://{origin}", base="nope"), run(), lambda _: None)


def test_commit_message_is_bounded():
    r = run()
    r.prompt = "x" * 10_000
    msg = commit_message(r)
    assert len(msg.splitlines()[0]) <= 100 and len(msg) < 4300 and "Run-Id: 0123456789ab" in msg
