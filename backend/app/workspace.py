"""The git side of a run: get the repo into the sandbox, branch, commit, and (optionally) push.

The GitHub token never enters a sandbox. Cloning and pushing happen on the host, in a fresh
temporary repository with an isolated git config, and code moves in and out of the sandbox as
git bundles:

    clone:  host `git clone --bare` (token) -> bundle -> copied in -> `git clone` from the bundle
    push:   `git bundle` of agent/<run-id> in the sandbox -> copied out -> fetched into a fresh
            host repo -> `git push` (token) of exactly that branch, never forced
    review: the same export, and the diff is computed on the host from it (files.run_diff), so
            what you review is exactly what gets pushed, whatever the agent did to git in the sandbox

So nothing the agent writes (hooks, `.git/config`, credential helpers, `insteadOf` rewrites)
is ever executed or read by a git process that holds the token.
"""

from __future__ import annotations

import os
import re
import secrets
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
MAX_BUNDLE = 2**30
HOST_GIT_TIMEOUT = 600
IN_BUNDLE = "/tmp/ah-repo.bundle"

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
    committed_now: bool  # Kiln made a commit of leftover changes


class Workspace(Protocol):
    def prepare(self, sb: Sandbox, run: Run, log, start_from: str | None = None) -> str: ...
    def sync(self, sb: Sandbox, base: str | None, log) -> str: ...
    def finalize(self, sb: Sandbox, run: Run, log) -> Finalized: ...
    def push(self, sb: Sandbox, run: Run, log, explicit: bool = False) -> bool: ...


def parse_stats(out: str) -> dict:
    first, _, names = out.strip("\n").partition("\n")
    nums = {k: int(v) for v, k in re.findall(r"(\d+) (file|insertion|deletion)", first)}
    return {"files": nums.get("file", 0), "insertions": nums.get("insertion", 0),
            "deletions": nums.get("deletion", 0), "names": [n for n in names.splitlines() if n.strip()]}


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
mkdir -p .git/info
grep -qxF '/.kiln/' .git/info/exclude 2>/dev/null || echo '/.kiln/' >> .git/info/exclude
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
if [ -n "$AH_FROM" ]; then
  git rev-parse -q --verify "refs/heads/$AH_FROM" >/dev/null || { echo "NOFROM"; exit 0; }
  git checkout -q -b "$AH_BRANCH" "$AH_FROM"
else
  git checkout -q -b "$AH_BRANCH" "$AH_BASE"
fi
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

# A checkpoint: the whole working tree (untracked files too, ignored ones not) as a commit on top
# of HEAD, via a temporary index so the agent's own index and branch are untouched. Kept as
# refs/kiln/<run>/<n> (never pushed); skipped when nothing changed since the last one.
CHECKPOINT_SCRIPT = """set -e
[ -d .git ] || { echo NOREPO; exit 0; }
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
export GIT_INDEX_FILE="$tmp"
git read-tree HEAD 2>/dev/null || true
git add -A
tree=$(git write-tree)
prev=$(git rev-parse -q --verify "refs/kiln/$AH_RUN/last" 2>/dev/null || git rev-parse HEAD)
if [ "$tree" = "$(git rev-parse "$prev^{tree}")" ]; then echo SAME; exit 0; fi
c=$(git commit-tree "$tree" -p "$(git rev-parse HEAD)" -m "Kiln checkpoint $AH_N of run $AH_RUN")
git update-ref "refs/kiln/$AH_RUN/$AH_N" "$c"
git update-ref "refs/kiln/$AH_RUN/last" "$c"
echo "SNAP $c"
git diff --name-only "$prev" "$c" | head -n 50
"""

# Put the run's branch back to a checkpoint (the run has ended; ignored files such as the
# ledger stay).
REWIND_SCRIPT = """set -e
git rev-parse -q --verify "$AH_SHA^{commit}" >/dev/null || { echo NOSHA; exit 0; }
git checkout -q -f "$AH_BRANCH"
git reset -q --hard "$AH_SHA"
git clean -q -fd
echo OK
"""

STATS_SCRIPT = """git diff --shortstat "$AH_BASE...$AH_BRANCH" 2>/dev/null; echo; git diff --name-only "$AH_BASE...$AH_BRANCH" 2>/dev/null | head -n 200"""

# Keeping the base branch up to date with the remote. The sandbox never talks to the remote
# itself: the host fetches into its own cache (sync_cache) and only the new commits come in, as a
# bundle. First the commits the sandbox already has, so the bundle can leave them out:
SYNC_HAVE_SCRIPT = """git rev-parse -q --verify "refs/remotes/origin/$AH_BASE^{commit}" || echo -
git rev-parse -q --verify "refs/heads/$AH_BASE^{commit}" || echo -
"""

# Then bring the remote's branch in as origin/<base> and fast-forward the local base branch to it.
# A local base branch with commits of its own is left alone (AHEAD / DIVERGED).
SYNC_SCRIPT = """set -e
if [ -n "$AH_BUNDLE" ]; then
  git fetch -q --no-tags "$AH_BUNDLE" "+refs/heads/$AH_BASE:refs/remotes/origin/$AH_BASE"
  rm -f "$AH_BUNDLE"
fi
git update-ref "refs/remotes/origin/$AH_BASE" "$AH_TIP"
remote=$(git rev-parse "refs/remotes/origin/$AH_BASE")
local=$(git rev-parse -q --verify "refs/heads/$AH_BASE") || { echo NEW; exit 0; }
[ "$local" = "$remote" ] && { echo SAME; exit 0; }
if git merge-base --is-ancestor "$local" "$remote"; then
  if [ "$(git symbolic-ref --short -q HEAD)" = "$AH_BASE" ]; then
    [ -z "$(git status --porcelain)" ] || { echo DIRTY; exit 0; }
    git -c core.hooksPath=/dev/null merge -q --ff-only "$remote"
  else
    git update-ref "refs/heads/$AH_BASE" "$remote" "$local"
  fi
  echo "FF $(git rev-list --count "$local..$remote")"; exit 0
fi
if git merge-base --is-ancestor "$remote" "$local"; then echo AHEAD; else echo DIVERGED; fi
"""

DEFAULT_BRANCH_SCRIPT = """b=$(git symbolic-ref --short -q refs/remotes/origin/HEAD) && echo "${b#origin/}" && exit 0
git symbolic-ref --short HEAD"""

# The run branch, plus its base branch if asked for and still there. Run with the system git
# (Kiln's commands use a PATH of system directories only) and without replace objects.
EXPORT_SCRIPT = """set -e
rm -f "$AH_OUT"
git rev-parse -q --verify "refs/heads/$AH_BRANCH" >/dev/null || { echo NOBRANCH; exit 3; }
refs="refs/heads/$AH_BRANCH"
if [ -n "$AH_BASE" ] && git rev-parse -q --verify "refs/heads/$AH_BASE" >/dev/null; then
  refs="$refs refs/heads/$AH_BASE"
fi
git bundle create -q "$AH_OUT" $refs
"""


def host_git(args: list[str], home: str, cwd: str | None = None, token: bool = False) -> str:
    """git on the host, with no system/global config, no hooks, and the GitHub token only when
    asked for (it's then offered to github.com only)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
           "LANG": "C.UTF-8"}
    if token and config.settings.github_token:
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


def export_branch(backend: Backend, sb: Sandbox, branch: str, base: str | None, tmp: str) -> str:
    """Copy `branch` (and `base`, if given and present) out of the sandbox as a git bundle,
    streamed to disk, and fetch it into a fresh bare repository under `tmp`. Returns its path."""
    out = f"/tmp/ah-export-{secrets.token_hex(6)}.bundle"
    env = {"AH_OUT": out, "AH_BRANCH": branch, "AH_BASE": base or "", "GIT_NO_REPLACE_OBJECTS": "1"}
    cid = sb.container_id or ""
    r = backend.exec(cid, ["bash", "-c", EXPORT_SCRIPT], 600, env)
    try:
        if r.exit_code != 0:
            if "NOBRANCH" in r.output:
                raise WorkspaceError(f"Branch {branch} no longer exists in this sandbox")
            raise WorkspaceError(r.output.strip()[-400:] or "git bundle failed")
        backend.get_file_to(cid, out, f"{tmp}/out.bundle", MAX_BUNDLE)
    finally:
        try:
            backend.exec(cid, ["rm", "-f", out], 30)
        except BackendError:
            pass
    repo = f"{tmp}/repo.git"
    host_git(["init", "--quiet", "--bare", repo], home=tmp)
    host_git(["fetch", "--quiet", "--no-tags", f"{tmp}/out.bundle", "refs/heads/*:refs/heads/*"], home=tmp, cwd=repo)
    return repo


BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$")


def valid_branch(name: str) -> bool:
    return bool(BRANCH_NAME_RE.fullmatch(name)) and ".." not in name and "//" not in name \
        and not name.endswith((".", "/", ".lock"))


class GitWorkspace:
    def __init__(self, backend: Backend, in_bundle: str = IN_BUNDLE, cache_dir: str | Path | None = None) -> None:
        self.backend = backend
        self.in_bundle = in_bundle
        self._cache_dir = Path(cache_dir) if cache_dir else None

    @property
    def cache_dir(self) -> Path:
        """Host-side mirrors of each sandbox's remote base branch (only what came from the remote;
        nothing from a sandbox is ever written here)."""
        return self._cache_dir or Path(config.settings.db_path).parent / "upstream"

    # ---- in the sandbox ------------------------------------------------------

    def _sh(self, sb: Sandbox, script: str, env: dict[str, str] | None = None, timeout: int = 300) -> str:
        s = config.settings
        # Kiln's own commits (initial commit, saving the agent's work) always carry this
        # identity; the sandbox user has none for git to fall back on.
        ident = {"GIT_AUTHOR_NAME": s.git_author_name, "GIT_AUTHOR_EMAIL": s.git_author_email,
                 "GIT_COMMITTER_NAME": s.git_author_name, "GIT_COMMITTER_EMAIL": s.git_author_email}
        r = self.backend.exec(sb.container_id or "", ["bash", "-c", script], timeout, {**ident, **(env or {})})
        if r.exit_code != 0:
            raise WorkspaceError(r.output.strip()[-500:] or f"git step failed ({r.exit_code})")
        return r.output.strip()

    def prepare(self, sb: Sandbox, run: Run, log, start_from: str | None = None) -> str:
        """Clone or init /workspace if needed and check out the run's branch: from the base
        branch, or from `start_from` (a previous run's branch, when continuing it). Returns the
        base branch, which the diff and push compare against either way."""
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
        if start_from and not BRANCH_RE.fullmatch(start_from):
            raise WorkspaceError(f"Can't continue from branch {start_from!r}")
        if state == "git" and sb.repo_url:
            # Cloned earlier: bring the base branch up to date with the remote first (or fetch
            # it, if it was made there since). Not reaching the remote isn't fatal.
            try:
                self.sync(sb, base, log)
            except WorkspaceError as e:
                log(f"Couldn't update {base} from {sb.repo_url}, so the run starts from the sandbox's copy: {e}")
        branch_env = {"AH_BASE": base, "AH_BRANCH": run.branch or "", "AH_FROM": start_from or "",
                      "AH_GIT_NAME": s.git_author_name, "AH_GIT_EMAIL": s.git_author_email}
        last = self._sh(sb, BRANCH_SCRIPT, branch_env).splitlines()[-1]
        if last.startswith("DIRTY"):
            raise WorkspaceError(f"/workspace has uncommitted changes on {last[6:]}. Commit or discard "
                                 "them in the terminal first, so the agent starts from a clean tree.")
        if last == "NOBASE":
            raise WorkspaceError(f"Base branch {base!r} doesn't exist in the sandbox"
                                 + (f" or in {sb.repo_url}. Push it there first, or change the sandbox's "
                                    "base branch in Settings." if sb.repo_url else "."))
        if last == "NOFROM":
            raise WorkspaceError(f"The previous run's branch {start_from} no longer exists in the sandbox.")
        log(f"On branch {run.branch} from {start_from or base}")
        return base

    def sync(self, sb: Sandbox, base: str | None, log) -> str:
        """Bring `base` (default: the sandbox's base branch) up to date with the remote. Returns a
        sentence saying what happened. Raises WorkspaceError if the remote can't be reached or
        doesn't have the branch."""
        if not sb.repo_url:
            raise WorkspaceError("This sandbox has no repository to sync with")
        base = base or sb.base_branch
        if not base:
            raise WorkspaceError("Run the agent once first, so the repository is cloned")
        if not valid_branch(base):
            raise WorkspaceError(f"Not a valid branch name: {base!r}")
        if self._sh(sb, STATE_SCRIPT).splitlines()[-1] != "git":
            raise WorkspaceError("/workspace has no repository yet; it's cloned at the first run")
        env = {"AH_BASE": base}
        have = [x for x in self._sh(sb, SYNC_HAVE_SCRIPT, env).splitlines() if re.fullmatch(r"[0-9a-f]{40}", x)]
        bundle, tip = self.sync_cache(sb, base, have)
        if bundle is not None:
            p = Path(self.in_bundle)
            self.backend.put_file(sb.container_id or "", str(p.parent), p.name, bundle)
        out = self._sh(sb, SYNC_SCRIPT, {**env, "AH_TIP": tip,
                                         "AH_BUNDLE": self.in_bundle if bundle is not None else ""})
        last = out.splitlines()[-1]
        msg = {
            "NEW": f"Fetched {base} from {sb.repo_url}",
            "SAME": f"{base} is up to date with {sb.repo_url}",
            "DIRTY": f"{base} has new commits on the remote, but /workspace has uncommitted changes on it; "
                     "not updated",
            "AHEAD": f"{base} in the sandbox has commits the remote doesn't; kept as it is",
            "DIVERGED": f"{base} in the sandbox and on the remote have both changed; kept the sandbox's copy. "
                        "Reset it in the terminal (git reset --hard origin/" + base + ") to use the remote's.",
        }.get(last)
        if msg is None and last.startswith("FF "):
            n = int(last[3:])
            msg = f"Updated {base} with {n} new commit{'s' if n != 1 else ''} from {sb.repo_url}"
        log(msg or f"Sync of {base} said: {last}")
        return msg or last

    def sync_cache(self, sb: Sandbox, base: str, have: list[str]) -> tuple[bytes | None, str]:
        """Fetch `base` from the remote into this sandbox's host cache. Returns a bundle of what the
        sandbox is missing (None: it has every commit already) and the remote's tip."""
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        repo = self.cache_dir / f"{sb.id}.git"
        with tempfile.TemporaryDirectory(prefix="ah-sync-") as tmp:
            if not (repo / "HEAD").exists():
                host_git(["init", "--quiet", "--bare", str(repo)], home=tmp)
            ref = f"refs/heads/{base}"
            try:
                self._git(["fetch", "--quiet", "--no-tags", "--", sb.repo_url or "", f"+{ref}:{ref}"],
                          home=tmp, cwd=str(repo))
            except WorkspaceError as e:
                if "couldn't find remote ref" in str(e):
                    raise WorkspaceError(f"{sb.repo_url} has no branch {base!r}") from e
                raise
            tip = host_git(["rev-parse", ref], home=tmp, cwd=str(repo)).strip()
            if tip in have:
                return None, tip
            known = []
            for sha in have:
                try:
                    host_git(["cat-file", "-e", f"{sha}^{{commit}}"], home=tmp, cwd=str(repo))
                    known.append(f"^{sha}")
                except WorkspaceError:
                    pass
            out = f"{tmp}/sync.bundle"
            try:
                host_git(["bundle", "create", "--quiet", out, ref, *known], home=tmp, cwd=str(repo))
            except WorkspaceError as e:
                if "empty bundle" not in str(e):
                    raise
                return None, tip  # the sandbox already has every commit (e.g. the remote went back)
            if Path(out).stat().st_size > MAX_BUNDLE:
                raise WorkspaceError("Update is larger than 1 GB")
            return Path(out).read_bytes(), tip

    def forget(self, sb: Sandbox) -> None:
        """The sandbox is gone: drop its host cache."""
        import shutil

        shutil.rmtree(self.cache_dir / f"{sb.id}.git", ignore_errors=True)

    def finalize(self, sb: Sandbox, run: Run, log) -> Finalized:
        out = self._sh(sb, FINALIZE_SCRIPT, {"AH_BRANCH": run.branch or "", "AH_BASE": run.base_branch or "",
                                             "AH_MSG": commit_message(run)})
        made, sha, count = out.splitlines()[-1].split()
        if made == "1":
            log(f"Committed the agent's changes as {sha[:10]}")
        return Finalized(commit_sha=sha, commits=int(count), committed_now=made == "1")

    def checkpoint(self, sb: Sandbox, run: Run, n: int) -> tuple[str, list[str]] | None:
        """(commit, files changed since the previous checkpoint), or None if nothing changed."""
        out = self._sh(sb, CHECKPOINT_SCRIPT, {"AH_RUN": run.id, "AH_N": str(int(n))}, timeout=120).splitlines()
        snap = next((i for i, line in enumerate(out) if line.startswith("SNAP ")), None)
        if snap is None:
            return None
        return out[snap].split()[1], [f for f in out[snap + 1:] if f]

    def rewind(self, sb: Sandbox, run: Run, sha: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not BRANCH_RE.fullmatch(run.branch or ""):
            raise WorkspaceError("Bad checkpoint")
        if self._sh(sb, REWIND_SCRIPT, {"AH_SHA": sha, "AH_BRANCH": run.branch or ""}).splitlines()[-1] != "OK":
            raise WorkspaceError("That checkpoint is no longer in the sandbox's repository")

    def stats(self, sb: Sandbox, run: Run) -> dict:
        """Files changed, insertions and deletions of the run's branch against its base."""
        out = self._sh(sb, STATS_SCRIPT, {"AH_BASE": run.base_branch or "", "AH_BRANCH": run.branch or ""})
        return parse_stats(out)

    def push(self, sb: Sandbox, run: Run, log, explicit: bool = False) -> bool:
        """`explicit`: the owner asked for this push (e.g. to open a pull request), so the
        sandbox's automatic-push setting doesn't apply."""
        s = config.settings
        branch = run.branch or ""
        if not ((sb.allow_push or explicit) and sb.repo_url):
            return False
        if not s.github_token:
            log("Push is on, but no GitHub token is configured; skipped")
            return False
        if not github_repo(sb.repo_url):
            log("Push is only supported for github.com repositories; skipped")
            return False
        if not BRANCH_RE.fullmatch(branch):  # never main/master or anything else
            raise WorkspaceError(f"Refusing to push unexpected branch {branch!r}")
        with tempfile.TemporaryDirectory(prefix="ah-push-") as tmp:
            try:
                repo = export_branch(self.backend, sb, branch, None, tmp)
            except BackendError as e:
                raise WorkspaceError(str(e)) from e
            log(f"Pushing {branch} to {sb.repo_url}")
            ref = f"refs/heads/{branch}"
            # Exactly one branch, no force, explicit URL (no remotes or config from the sandbox).
            self._git(["push", "--quiet", "--no-verify", sb.repo_url, f"{ref}:{ref}"], home=tmp, cwd=repo)
        log(f"Pushed {branch}")
        return True

    # ---- on the host ----------------------------------------------------------

    def _git(self, args: list[str], home: str, cwd: str | None = None) -> str:
        return host_git(args, home, cwd, token=True)

    def _host_clone_bundle(self, url: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="ah-clone-") as tmp:
            self._git(["clone", "--quiet", "--bare", url, f"{tmp}/repo.git"], home=tmp)
            self._git(["bundle", "create", "--quiet", f"{tmp}/repo.bundle", "--all"], home=tmp, cwd=f"{tmp}/repo.git")
            path = Path(f"{tmp}/repo.bundle")
            if path.stat().st_size > MAX_BUNDLE:
                raise WorkspaceError("Repository is larger than 1 GB")
            return path.read_bytes()


class FakeWorkspace:
    """DEMO_MODE and unit tests: simulates the git steps."""

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.fail_prepare: str | None = None
        self.started_from: str | None = None

    def prepare(self, sb: Sandbox, run: Run, log, start_from: str | None = None) -> str:
        if self.fail_prepare:
            raise WorkspaceError(self.fail_prepare)
        if sb.repo_url and not start_from:
            log(f"Cloning {sb.repo_url}")
        base = sb.base_branch or "main"
        self.started_from = start_from
        log(f"On branch {run.branch} from {start_from or base}")
        return base

    def sync(self, sb: Sandbox, base: str | None, log) -> str:
        if not sb.repo_url:
            raise WorkspaceError("This sandbox has no repository to sync with")
        msg = f"{base or sb.base_branch or 'main'} is up to date with {sb.repo_url}"
        log(msg)
        return msg

    def forget(self, sb: Sandbox) -> None:
        pass

    def checkpoint(self, sb: Sandbox, run: Run, n: int) -> tuple[str, list[str]] | None:
        self.checkpoints = getattr(self, "checkpoints", 0)
        if not getattr(self, "changes", True):
            return None
        self.checkpoints += 1
        return (f"{n:02d}" * 20)[:40], [f"src/file{n}.py"]

    def rewind(self, sb: Sandbox, run: Run, sha: str) -> None:
        self.rewound = sha

    def stats(self, sb: Sandbox, run: Run) -> dict:
        return {"files": 2, "insertions": 30, "deletions": 4, "names": ["README.md", "src/app.py"]}

    def finalize(self, sb: Sandbox, run: Run, log) -> Finalized:
        sha = (run.id * 4)[:40]
        log(f"Committed the agent's changes as {sha[:10]}")
        return Finalized(commit_sha=sha, commits=1, committed_now=True)

    def push(self, sb: Sandbox, run: Run, log, explicit: bool = False) -> bool:
        if not ((sb.allow_push or explicit) and sb.repo_url):
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
