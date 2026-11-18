"""The task queue (taskqueue.py), pull requests (github.py) and syncing a sandbox with its remote."""

from __future__ import annotations

import dataclasses
import json
import time

from app import config, github
from tests.conftest import CSRF
from tests.test_runs import make_sandbox, start, wait_final


def queue(admin, sid):
    return admin.get(f"/api/sandboxes/{sid}/queue").json()


def enqueue(admin, sid, prompt, **kw):
    r = admin.post(f"/api/sandboxes/{sid}/queue", json={"prompt": prompt, **kw}, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


def runs_of(admin, sid):
    return admin.get(f"/api/sandboxes/{sid}/runs").json()


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (v := cond()):
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_queued_tasks_run_one_after_another(admin, runs):
    runs.runners["claude-code"].scale = 0.3
    sb = make_sandbox(admin)
    first = start(admin, sb["id"], prompt="first").json()
    q = enqueue(admin, sb["id"], "second")
    assert q["started"] is None and [t["prompt"] for t in q["items"]] == ["second"]
    enqueue(admin, sb["id"], "third", continue_previous=True)
    wait_final(admin, first["id"])
    wait_for(lambda: len(runs_of(admin, sb["id"])) == 3 and all(
        r["state"] == "succeeded" for r in runs_of(admin, sb["id"])))
    newest, second, _ = runs_of(admin, sb["id"])
    assert second["prompt"] == "second" and second["parent_run_id"] is None
    assert newest["prompt"] == "third" and newest["parent_run_id"] == second["id"]  # continued it
    assert queue(admin, sb["id"])["items"] == []


def test_a_free_sandbox_starts_a_queued_task_at_once(admin):
    sb = make_sandbox(admin)
    q = enqueue(admin, sb["id"], "now", timeout_minutes=5, max_turns=7)
    assert q["started"]["prompt"] == "now" and q["items"] == []
    done = wait_final(admin, q["started"]["id"])
    assert done["timeout"] == 300 and done["max_turns"] == 7


def test_a_failed_run_pauses_the_queue_until_resumed(admin, runs):
    runs.runners["claude-code"].scale = 0.3
    sb = make_sandbox(admin)
    first = start(admin, sb["id"], prompt="first").json()
    enqueue(admin, sb["id"], "fail")
    enqueue(admin, sb["id"], "after")
    wait_final(admin, first["id"])
    q = wait_for(lambda: (q := queue(admin, sb["id"]))["paused"] and q)
    bad = runs_of(admin, sb["id"])[0]
    assert bad["prompt"] == "fail" and bad["state"] == "failed"
    assert f"run {bad['id']} failed" in q["note"] and [t["prompt"] for t in q["items"]] == ["after"]
    assert len(runs_of(admin, sb["id"])) == 2  # "after" didn't start

    r = admin.patch(f"/api/sandboxes/{sb['id']}/queue", json={"paused": False}, headers=CSRF).json()
    assert r["paused"] is False and r["note"] is None and r["started"]["prompt"] == "after"


def test_reorder_and_remove(admin, runs):
    runs.runners["claude-code"].scale = 1.0
    sb = make_sandbox(admin)
    busy = start(admin, sb["id"], prompt="busy").json()
    for p in ("a", "b", "c"):
        enqueue(admin, sb["id"], p)
    ids = {t["prompt"]: t["id"] for t in queue(admin, sb["id"])["items"]}
    url = f"/api/sandboxes/{sb['id']}/queue"
    r = admin.post(f"{url}/{ids['c']}/move", json={"offset": -2}, headers=CSRF).json()
    assert [t["prompt"] for t in r["items"]] == ["c", "a", "b"]
    r = admin.delete(f"{url}/{ids['a']}", headers=CSRF).json()
    assert [t["prompt"] for t in r["items"]] == ["c", "b"]
    assert admin.delete(f"{url}/{ids['a']}", headers=CSRF).status_code == 404
    admin.patch(url, json={"paused": True}, headers=CSRF)
    admin.post(f"/api/runs/{busy['id']}/cancel", headers=CSRF)
    wait_final(admin, busy["id"])
    time.sleep(0.2)
    assert len(runs_of(admin, sb["id"])) == 1 and len(queue(admin, sb["id"])["items"]) == 2  # paused: waits


def test_open_pull_request_pushes_then_opens(admin, workspace, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, github_token="ghp_x"))
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return {"html_url": "https://github.com/example/playground/pull/7"}

    monkeypatch.setattr(github, "request", fake)
    sb = make_sandbox(admin, repo_url="https://github.com/example/playground")
    done = wait_final(admin, start(admin, sb["id"], prompt="Add a README section\nmore").json()["id"])
    assert done["pushed"] is False  # automatic push is off
    r = admin.post(f"/api/runs/{done['id']}/pr", json={}, headers=CSRF)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["pr_url"] == "https://github.com/example/playground/pull/7" and out["pushed"] is True
    assert workspace.pushed == [done["branch"]]
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/repos/example/playground/pulls")
    assert body["head"] == done["branch"] and body["base"] == "main" and body["title"] == "Add a README section"
    assert f"run `{done['id']}`" in body["body"]
    # Asking again returns the same pull request without another call.
    assert admin.post(f"/api/runs/{done['id']}/pr", json={}, headers=CSRF).json()["pr_url"] == out["pr_url"]
    assert len(calls) == 1


def test_open_pull_request_finds_an_existing_one(monkeypatch):
    from app.db import Run

    def fake(method, path, body=None):
        if method == "POST":
            raise github.GitHubError("GitHub said 422: A pull request already exists for example:agent/x.")
        assert "head=example:agent%2F0123456789ab" in path
        return [{"html_url": "https://github.com/example/playground/pull/3"}]

    monkeypatch.setattr(github, "request", fake)
    run = Run(id="0123456789ab", sandbox_id="s", runner="claude-code", prompt="x", timeout=1, max_turns=1,
              branch="agent/0123456789ab", base_branch="main",
              report=json.dumps({"summary": "did it", "sections": {"Done": "the thing"}, "facts": {"files": 2}}))
    assert github.open_pull_request("https://github.com/example/playground.git", run).endswith("/pull/3")
    assert "**Done**" in github.pr_body(run) and "2 files changed" in github.pr_body(run)


def test_pull_requests_need_github(admin):
    sb = make_sandbox(admin, repo_url="https://gitlab.com/example/playground")
    done = wait_final(admin, start(admin, sb["id"]).json()["id"])
    r = admin.post(f"/api/runs/{done['id']}/pr", json={}, headers=CSRF)
    assert r.status_code == 400 and "github.com" in r.json()["detail"]


def test_sync_endpoint(admin, runs):
    sb = make_sandbox(admin, repo_url="https://github.com/example/playground")
    r = admin.post(f"/api/sandboxes/{sb['id']}/sync", headers=CSRF)
    assert r.status_code == 200 and "up to date" in r.json()["message"]
    plain = make_sandbox(admin, name="plain")
    r = admin.post(f"/api/sandboxes/{plain['id']}/sync", headers=CSRF)
    assert r.status_code == 400 and "no repository" in r.json()["detail"]
