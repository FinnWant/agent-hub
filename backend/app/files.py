"""Read-only views into a sandbox's /workspace: directory listings, file contents and the diff
of a run's branch. Everything runs as the sandbox user inside the container; paths are confined
to /workspace and sizes are capped. Content is returned as data for the UI to render as text.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from .db import Run, Sandbox
from .docker_backend import Backend, BackendError, WORKDIR
from .sandboxes import SandboxError

MAX_FILE = 512 * 1024
MAX_DIFF = 4 * 2**20
MAX_ENTRIES = 2000
REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")
TYPES = {"d": "dir", "f": "file", "l": "link"}

# Git in the sandbox, with everything that could run repo-provided programs switched off.
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.quotepath=off",
       "--no-pager"]


class PathError(SandboxError):
    status_code = 400


def resolve(path: str) -> str:
    full = posixpath.normpath(posixpath.join(WORKDIR, (path or "").lstrip("/")))
    if full != WORKDIR and not full.startswith(WORKDIR + "/"):
        raise PathError("Path is outside /workspace")
    return full


def relative(full: str) -> str:
    return posixpath.relpath(full, WORKDIR) if full != WORKDIR else ""


def _exec(backend: Backend, sb: Sandbox, argv: list[str], env: dict[str, str] | None = None) -> str:
    r = backend.exec(sb.container_id or "", argv, 60, env)
    if r.exit_code != 0:
        raise SandboxError(r.output.strip()[-300:] or "command failed")
    return r.output


def _stat(backend: Backend, sb: Sandbox, full: str) -> tuple[str, int, str]:
    try:
        out = _exec(backend, sb, ["find", full, "-maxdepth", "0", "-printf", "%y\t%s\t%l"])
    except SandboxError as e:
        if "No such file" in str(e):
            raise PathError("No such file or directory") from e
        raise
    kind, size, target = (out.split("\t") + ["", "", ""])[:3]
    return TYPES.get(kind, "other"), int(size or 0), target


def browse(backend: Backend, sb: Sandbox, path: str) -> dict[str, Any]:
    full = resolve(path)
    kind, size, target = _stat(backend, sb, full)
    base = {"path": relative(full), "kind": kind, "size": size}
    if kind == "dir":
        script = ('find "$1" -mindepth 1 -maxdepth 1 -printf "%y\\t%s\\t%l\\t%f\\0" | head -z -n ' +
                  str(MAX_ENTRIES + 1))
        out = _exec(backend, sb, ["bash", "-c", script, "list", full])
        entries = []
        for rec in out.split("\0"):
            parts = rec.split("\t", 3)
            if len(parts) != 4:
                continue
            t, sz, link, name = parts
            entry = {"name": name, "type": TYPES.get(t, "other"), "size": int(sz or 0)}
            if t == "l":
                entry["target"] = link
            entries.append(entry)
        order = {"dir": 0, "link": 1, "file": 1, "other": 2}
        entries.sort(key=lambda e: (order[e["type"]], e["name"].lower()))
        return {**base, "entries": entries[:MAX_ENTRIES], "truncated": len(entries) > MAX_ENTRIES}
    if kind == "link":
        return {**base, "target": target}
    if kind != "file":
        return base
    if size > MAX_FILE:
        return {**base, "too_large": True, "limit": MAX_FILE}
    data = backend.get_file(sb.container_id or "", full, MAX_FILE)
    if b"\0" in data[:8192]:
        return {**base, "binary": True}
    return {**base, "content": data.decode("utf-8", errors="replace")}


GIT_SH = " ".join(GIT)
NUMSTAT_SCRIPT = GIT_SH + ' diff --no-ext-diff --no-textconv -M --numstat "$AH_RANGE" --'
DIFF_SCRIPT = (f'{GIT_SH} diff --no-color --no-ext-diff --no-textconv -M "$AH_RANGE" -- > "$AH_OUT" && '
               'wc -c < "$AH_OUT"')


def run_diff(backend: Backend, sb: Sandbox, run: Run) -> dict[str, Any]:
    """The run's branch against the point where it left its base branch."""
    if not (run.branch and run.base_branch):
        raise SandboxError("This run didn't get as far as creating its branch")
    if not (REF_RE.fullmatch(run.branch) and REF_RE.fullmatch(run.base_branch)):
        raise SandboxError("Unexpected branch name")
    env = {"AH_RANGE": f"{run.base_branch}...{run.branch}", "AH_OUT": f"/tmp/ah-diff-{run.id}.patch"}
    try:
        stat = _exec(backend, sb, ["bash", "-c", NUMSTAT_SCRIPT], env)
    except SandboxError as e:
        if "unknown revision" in str(e) or "bad revision" in str(e):
            raise SandboxError(f"Branch {run.branch} no longer exists in this sandbox") from e
        raise
    files = []
    for line in stat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            add, rem, name = parts
            files.append({"path": name, "added": None if add == "-" else int(add),
                          "removed": None if rem == "-" else int(rem)})
    size = int(_exec(backend, sb, ["bash", "-c", DIFF_SCRIPT], env).strip() or 0)
    result: dict[str, Any] = {"run": run.id, "base": run.base_branch, "branch": run.branch,
                              "files": files, "size": size}
    if size > MAX_DIFF:
        result["too_large"] = True
    else:
        try:
            patch = backend.get_file(sb.container_id or "", env["AH_OUT"], MAX_DIFF) if size else b""
        except BackendError as e:
            raise SandboxError(str(e)) from e
        result["patch"] = patch.decode("utf-8", errors="replace")
    _exec(backend, sb, ["rm", "-f", env["AH_OUT"]])
    return result
