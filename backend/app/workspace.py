"""The git side of a run: get the repo into the sandbox, branch, commit, and (optionally) push.

The GitHub token never enters a sandbox. Cloning and pushing happen on the host, in a fresh
temporary repository with an isolated git config, and code moves in and out of the sandbox as
git bundles:

    clone:  host `git clone --bare` (token) -> bundle -> copied in -> `git clone` from the bundle
    push:   `git bundle` of agent/<run-id> in the sandbox -> copied out -> fetched into a fresh
            host repo -> `git push` (token) of exactly that branch, never forced

So nothing the agent writes (hooks, `.git/config`, credential helpers, `insteadOf` rewrites)
is ever executed or read by a git process that holds the token.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from . import config
from .db import Run, Sandbox
from .docker_backend import Backend, BackendError

BRANCH_RE = re.compile(r"^agent/[0-9a-f]{12}$")
MAX_BUNDLE = 2 * 2**30
HOST_GIT_TIMEOUT = 600
IN_BUNDLE = "/tmp/ah-repo.bundle"
OUT_BUNDLE = "/tmp/ah-push.bundle"

# Credential helper for host git: answers only for github.com, with the token from the env.
CRED_HELPER = ('!f() { test "$1" = get || exit 0; ok=; while read -r l; do '
               'case "$l" in host=github.com) ok=1;; esac; done; '
               '[ -n "$ok" ] && printf "username=x-access-token\\npassword=%s\\n" "$AH_GITHUB_TOKEN"; }; f')


class WorkspaceError(Exception):
    pass


@dataclass
class Finalized:
    commit_sha: str | None
    commits: int        # commits on the run branch beyond the base
    committed_now: bool  # Agent Hub made a commit of leftover changes


class Workspace(Protocol):
    def prepare(self, sb: Sandbox, run: Run, log) -> str: ...
    def finalize(self, sb: Sandbox, run: Run, log) -> Finalized: ...
    def push(self, sb: Sandbox, run: Run, log) -> bool: ...


def github_repo(url: str) -> bool:
    return urlsplit(url).hostname == "github.com"


def commit_message(run: Run) -> str:
    first = (run.prompt.strip().splitlines() or [""])[0][:72]
    return f"Agent run {run.id}: {first}\n\nPrompt:\n{run.prompt.strip()[:4000]}\n\nRun-Id: {run.id}\n"


# Shell steps run inside the sandbox as `agent`, in /workspace.
STATE_SCRIPT = 'if [ -d .git ]; then echo git; elif [ -z "$(ls -A)" ]; then echo empty; else echo files; fi'

CLONE_SCRIPT = """set -e
git clone -q {bundle} .
git remote set-url origin "$AH_REPO_URL"
rm -f {bundle}
git symbolic-ref --short HEAD
"""

INIT_SCRIPT = """set -e
git init -q -b main
git add -A
git -c core.hooksPath=/dev/null commit -q --allow-empty -m "Initial commit"
echo main
"""

BRANCH_SCRIPT = """set -e
git config user.name "$AH_GIT_NAME"
git config user.email "$AH_GIT_EMAIL"
if [ -n "$(git status --porcelain)" ]; then
  echo "DIRTY $(git symbolic-ref --short -q HEAD || git rev-parse --short HEAD)"; exit 0
fi
if ! git rev-parse -q --verify "refs/heads/$AH_BASE" >/dev/null; then
  if git rev-parse -q --verify "refs/remotes/origin/$AH_BASE" >/dev/null; then
    git branch -q --track "$AH_BASE" "origin/$AH_BASE"
  else
    echo "NOBASE"; exit 0
  fi
fi
git checkout -q -b "$AH_BRANCH" "$AH_BASE"
echo OK
"""

FINALIZE_SCRIPT = """set -e
cur=$(git symbolic-ref --short -q HEAD || echo DETACHED)
if [ "$cur" != "$AH_BRANCH" ]; then git checkout -q "$AH_BRANCH" 2>/dev/null || true; fi
git add -A
made=0
if ! git diff --cached --quiet; then
  git -c core.hooksPath=/dev/null commit -q -m "$AH_MSG"; made=1
fi
echo "$made $(git rev-parse "$AH_BRANCH") $(git rev-list --count "$AH_BASE..$AH_BRANCH")"
"""

DEFAULT_BRANCH_SCRIPT = """b=$(git symbolic-ref --short -q refs/remotes/origin/HEAD) && echo "${b#origin/}" && exit 0
git symbolic-ref --short HEAD"""

BUNDLE_SCRIPT = 'set -e; rm -f {out}; git bundle create -q {out} "refs/heads/$AH_BRANCH"'


class GitWorkspace:
    def __init__(self, backend: Backend, in_bundle: str = IN_BUNDLE, out_bundle: str = OUT_BUNDLE) -> None:
        self.backend = backend
        self.in_bundle = in_bundle
        self.out_bundle = out_bundle

    # ---- in the sandbox ------------------------------------------------------

    def _sh(self, sb: Sandbox, script: str, env: dict[str, str] | None = None, timeout: int = 300) -> str:
        s = config.settings
        # Agent Hub's own commits (initial commit, saving the agent's work) always carry this
        # identity; the sandbox user has none for git to fall back on.
        ident = {"GIT_AUTHOR_NAME": s.git_author_name, "GIT_AUTHOR_EMAIL": s.git_author_email,
                 "GIT_COMMITTER_NAME": s.git_author_name, "GIT_COMMITTER_EMAIL": s.git_author_email}
        r = self.backend.exec(sb.container_id or "", ["bash", "-c", script], timeout, {**ident, **(env or {})})
        if r.exit_code != 0:
            raise WorkspaceError(r.output.strip()[-500:] or f"git step failed ({r.exit_code})")
        return r.output.strip()

    def prepare(self, sb: Sandbox, run: Run, log) -> str:
        s = config.settings
        state = self._sh(sb, STATE_SCRIPT).splitlines()[-1]
        base = sb.base_branch
        if state != "git":
            if sb.repo_url:
                if state == "files":
                    raise WorkspaceError("/workspace has files but no git repository, so the repo can't be "
                                         "cloned into it. Clear it in the terminal or create a new sandbox.")
                log(f"Cloning {sb.repo_url}")
                bundle = self._host_clone_bundle(sb.repo_url)
                p = Path(self.in_bundle)
                self.backend.put_file(sb.container_id or "", str(p.parent), p.name, bundle)
                head = self._sh(sb, CLONE_SCRIPT.format(bundle=self.in_bundle), {"AH_REPO_URL": sb.repo_url})
                base = base or head.splitlines()[-1]
            else:
                log("No repository: initialising one in /workspace")
                base = base or self._sh(sb, INIT_SCRIPT).splitlines()[-1]
        elif not base:
            base = self._sh(sb, DEFAULT_BRANCH_SCRIPT).splitlines()[-1]
        out = self._sh(sb, BRANCH_SCRIPT, {"AH_BASE": base, "AH_BRANCH": run.branch or "",
                                           "AH_GIT_NAME": s.git_author_name, "AH_GIT_EMAIL": s.git_author_email})
        last = out.splitlines()[-1]
        if last.startswith("DIRTY"):
            raise WorkspaceError(f"/workspace has uncommitted changes on {last[6:]}. Commit or discard "
                                 "them in the terminal first, so the agent starts from a clean tree.")
        if last == "NOBASE":
            raise WorkspaceError(f"Base branch {base!r} doesn't exist in the repository.")
        log(f"On branch {run.branch} from {base}")
        return base

    def finalize(self, sb: Sandbox, run: Run, log) -> Finalized:
        out = self._sh(sb, FINALIZE_SCRIPT, {"AH_BRANCH": run.branch or "", "AH_BASE": run.base_branch or "",
                                             "AH_MSG": commit_message(run)})
        made, sha, count = out.splitlines()[-1].split()
        if made == "1":
            log(f"Committed the agent's changes as {sha[:10]}")
        return Finalized(commit_sha=sha, commits=int(count), committed_now=made == "1")

    def push(self, sb: Sandbox, run: Run, log) -> bool:
        s = config.settings
        branch = run.branch or ""
        if not (sb.allow_push and sb.repo_url):
            return False
        if not s.github_token:
            log("Push is on, but no GitHub token is configured; skipped")
            return False
        if not github_repo(sb.repo_url):
            log("Push is only supported for github.com repositories; skipped")
            return False
        if not BRANCH_RE.fullmatch(branch):  # never main/master or anything else
            raise WorkspaceError(f"Refusing to push unexpected branch {branch!r}")
        self._sh(sb, BUNDLE_SCRIPT.format(out=self.out_bundle), {"AH_BRANCH": branch})
        bundle = self.backend.get_file(sb.container_id or "", self.out_bundle, MAX_BUNDLE)
        log(f"Pushing {branch} to {sb.repo_url}")
        self._host_push(sb.repo_url, branch, bundle)
        log(f"Pushed {branch}")
        return True

    # ---- on the host ----------------------------------------------------------

    def _git(self, args: list[str], home: str, cwd: str | None = None) -> str:
        env = {"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"}
        if config.settings.github_token:
            env["AH_GITHUB_TOKEN"] = config.settings.github_token
        cmd = ["git", "-c", "credential.helper=", "-c", f"credential.helper={CRED_HELPER}",
               "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
               "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=always", *args]
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=HOST_GIT_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise WorkspaceError(f"git {args[0]} timed out") from e
        if r.returncode != 0:
            raise WorkspaceError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout

    def _host_clone_bundle(self, url: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="ah-clone-") as tmp:
            self._git(["clone", "--quiet", "--bare", url, f"{tmp}/repo.git"], home=tmp)
            self._git(["bundle", "create", "--quiet", f"{tmp}/repo.bundle", "--all"], home=tmp, cwd=f"{tmp}/repo.git")
            path = Path(f"{tmp}/repo.bundle")
            if path.stat().st_size > MAX_BUNDLE:
                raise WorkspaceError("Repository is larger than 2 GB")
            return path.read_bytes()

    def _host_push(self, url: str, branch: str, bundle: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="ah-push-") as tmp:
            repo = f"{tmp}/repo.git"
            Path(f"{tmp}/out.bundle").write_bytes(bundle)
            self._git(["init", "--quiet", "--bare", repo], home=tmp)
            ref = f"refs/heads/{branch}"
            self._git(["fetch", "--quiet", f"{tmp}/out.bundle", f"{ref}:{ref}"], home=tmp, cwd=repo)
            # Exactly one branch, no force, explicit URL (no remotes or config from the sandbox).
            self._git(["push", "--quiet", "--no-verify", url, f"{ref}:{ref}"], home=tmp, cwd=repo)


class FakeWorkspace:
    """DEMO_MODE and unit tests: simulates the git steps."""

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.fail_prepare: str | None = None

    def prepare(self, sb: Sandbox, run: Run, log) -> str:
        if self.fail_prepare:
            raise WorkspaceError(self.fail_prepare)
        if sb.repo_url:
            log(f"Cloning {sb.repo_url}")
        base = sb.base_branch or "main"
        log(f"On branch {run.branch} from {base}")
        return base

    def finalize(self, sb: Sandbox, run: Run, log) -> Finalized:
        sha = (run.id * 4)[:40]
        log(f"Committed the agent's changes as {sha[:10]}")
        return Finalized(commit_sha=sha, commits=1, committed_now=True)

    def push(self, sb: Sandbox, run: Run, log) -> bool:
        if not (sb.allow_push and sb.repo_url):
            return False
        if not BRANCH_RE.fullmatch(run.branch or ""):
            raise WorkspaceError("bad branch")
        self.pushed.append(run.branch or "")
        log(f"Pushed {run.branch}")
        return True


def default_workspace(backend: Backend) -> Workspace:
    return FakeWorkspace() if config.settings.demo_mode else GitWorkspace(backend)


__all__ = ["BackendError", "BRANCH_RE", "Finalized", "GitWorkspace", "FakeWorkspace", "Workspace",
           "WorkspaceError", "commit_message", "default_workspace"]
