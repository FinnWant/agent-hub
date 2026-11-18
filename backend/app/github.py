"""Opening a pull request for a run's branch, with the GitHub token held on the server.

Only the REST call to create (or find) the pull request lives here; the branch itself is pushed by
workspace.push, which never touches anything but `agent/<run-id>`.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

from . import config
from .db import Run

API = "https://api.github.com"
TIMEOUT = 30


class GitHubError(Exception):
    pass


def repo_slug(url: str) -> tuple[str, str]:
    """("owner", "repo") of a https://github.com/owner/repo[.git] URL."""
    parts = urlsplit(url)
    m = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", parts.path)
    if parts.hostname != "github.com" or not m:
        raise GitHubError("Pull requests can only be opened for github.com repositories")
    return m.group(1), m.group(2)


def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One GitHub REST call. Kept as a module function so tests can replace it."""
    token = config.settings.github_token
    if not token:
        raise GitHubError("No GitHub token is set on the server (python -m app.set_secret github)")
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kiln",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310  (fixed https host)
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b"{}")
        except ValueError:
            data = {}
        detail = "; ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x)
                           for x in data.get("errors", [])) or data.get("message") or e.reason
        raise GitHubError(f"GitHub said {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise GitHubError(f"Couldn't reach GitHub: {e}") from e


def pr_title(run: Run) -> str:
    first = (run.prompt.strip().splitlines() or [""])[0].strip()
    return (first[:100] + ("…" if len(first) > 100 else "")) or f"Agent run {run.id}"


def pr_body(run: Run) -> str:
    report: dict[str, Any] = {}
    try:
        report = json.loads(run.report) if run.report else {}
    except ValueError:
        pass
    out = ["**Task**", "", run.prompt.strip()[:3000], ""]
    sections = report.get("sections") or {}
    if sections:
        for name, text in sections.items():
            out += [f"**{name}**", "", text.strip()[:3000], ""]
    elif report.get("summary"):
        out += ["**Agent's summary**", "", report["summary"].strip()[:6000], ""]
    facts = []
    if run.verify:
        facts.append(f"Checks (`{run.verify_cmd}`): {run.verify}")
    stats = report.get("facts") or {}
    if stats.get("files"):
        facts.append(f"{stats['files']} files changed, +{stats.get('insertions', 0)} −{stats.get('deletions', 0)}")
    if run.cost_est:
        facts.append(f"Estimated cost ${run.cost_est:.2f}")
    if facts:
        out += ["**Measured by Kiln**", "", *[f"- {f}" for f in facts], ""]
    out += ["---", f"Opened from Kiln run `{run.id}`, branch `{run.branch}`."]
    return "\n".join(out)[:60000]


def open_pull_request(repo_url: str, run: Run, draft: bool = False) -> str:
    """Open a pull request of the run's branch into its base branch; if one is already open for
    that branch, return it instead. Returns the pull request's URL."""
    owner, repo = repo_slug(repo_url)
    if not (run.branch and run.base_branch):
        raise GitHubError("This run has no branch to open a pull request for")
    base = f"/repos/{quote(owner)}/{quote(repo)}/pulls"
    try:
        pr = request("POST", base, {"title": pr_title(run), "head": run.branch, "base": run.base_branch,
                                    "body": pr_body(run), "draft": draft})
    except GitHubError as e:
        if "already exists" not in str(e):
            raise
        found = request("GET", f"{base}?state=open&head={quote(owner)}:{quote(run.branch, safe='')}")
        if not found:
            raise
        pr = found[0]
    url = pr.get("html_url") if isinstance(pr, dict) else None
    if not url or not str(url).startswith("https://github.com/"):
        raise GitHubError("GitHub didn't return the pull request's address")
    return str(url)
