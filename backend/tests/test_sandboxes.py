from __future__ import annotations

import pytest

from app.config import parse_size
from app.docker_backend import MANAGED_LABEL, NotManaged
from app.sandboxes import TRANSITIONS, InvalidTransition, check_transition
from app.templates import TEMPLATES
from tests.conftest import CSRF, GIB


def create(admin, **kw):
    body = {"name": "demo", "template": "python", **kw}
    return admin.post("/api/sandboxes", json=body, headers=CSRF)


def test_create_list_get_destroy(admin, backend):
    r = create(admin, repo_url="https://github.com/example/project", base_branch="main")
    assert r.status_code == 201, r.text
    sb = r.json()
    assert sb["state"] == "running" and sb["memory"] == 2 * GIB and sb["cpus"] == 2
    assert "container_id" not in sb and "volume" not in sb  # internals stay server-side
    assert [s["id"] for s in admin.get("/api/sandboxes").json()] == [sb["id"]]
    assert admin.get(f"/api/sandboxes/{sb['id']}").json()["name"] == "demo"

    assert admin.delete(f"/api/sandboxes/{sb['id']}", headers=CSRF).status_code == 204
    assert backend.containers == {} and backend.volumes == {}
    assert admin.get("/api/sandboxes").json() == []
    assert admin.get(f"/api/sandboxes/{sb['id']}").status_code == 404


def test_stop_start_cycle(admin):
    sid = create(admin).json()["id"]
    assert admin.post(f"/api/sandboxes/{sid}/stop", headers=CSRF).json()["state"] == "stopped"
    assert admin.post(f"/api/sandboxes/{sid}/start", headers=CSRF).json()["state"] == "running"


def test_exec_is_audited(admin, svc):
    sid = create(admin).json()["id"]
    r = admin.post(f"/api/sandboxes/{sid}/exec", json={"cmd": "python3 --version"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["exit_code"] == 0
    actions = [e["action"] for e in svc.db.list_audit()]
    assert actions[:2] == ["sandbox.exec", "sandbox.create"]


def test_exec_requires_running(admin):
    sid = create(admin).json()["id"]
    admin.post(f"/api/sandboxes/{sid}/stop", headers=CSRF)
    r = admin.post(f"/api/sandboxes/{sid}/exec", json={"cmd": "true"}, headers=CSRF)
    assert r.status_code == 409


# ---- capacity -------------------------------------------------------------

def test_concurrency_cap(admin):
    assert create(admin, name="a", memory="1g").status_code == 201
    assert create(admin, name="b", memory="1g").status_code == 201
    r = create(admin, name="c", memory="1g")
    assert r.status_code == 409
    assert "already running" in r.json()["reasons"][0]


def test_total_memory_cap(admin):
    assert create(admin, name="a", memory="4g").status_code == 201
    r = create(admin, name="b", memory="3g")
    assert r.status_code == 409 and "total cap" in r.json()["detail"]


def test_host_memory_reserve(admin, host):
    host.mem_available = 4 * GIB  # 4 - 2 = 2 GiB left, under the 3 GiB reserve
    r = create(admin)
    assert r.status_code == 409 and "reserve" in r.json()["detail"]


def test_free_disk_floor(admin, host):
    host.free_disk = 10 * GIB
    r = create(admin)
    assert r.status_code == 409 and "free disk" in r.json()["detail"]


def test_restart_respects_caps(admin, host):
    sid = create(admin).json()["id"]
    admin.post(f"/api/sandboxes/{sid}/stop", headers=CSRF)
    host.mem_available = 3 * GIB
    assert admin.post(f"/api/sandboxes/{sid}/start", headers=CSRF).status_code == 409


def test_blocked_create_is_audited(admin, svc, host):
    host.free_disk = 1
    create(admin)
    entry = svc.db.list_audit()[0]
    assert entry["action"] == "sandbox.create" and entry["ok"] is False and entry["detail"]["blocked"]


def test_per_sandbox_limits(admin):
    assert create(admin, memory="5g").status_code == 400
    assert create(admin, cpus=8).status_code == 400
    assert create(admin, memory="100m").status_code == 422


@pytest.mark.parametrize("body", [
    {"repo_url": "https://user:token@github.com/x/y"},
    {"repo_url": "http://github.com/x/y"},
    {"repo_url": "file:///etc"},
    {"base_branch": "--upload-pack=evil"},
    {"name": "../etc"},
    {"template": "ubuntu"},
])
def test_input_validation(admin, body):
    assert create(admin, **body).status_code == 422


def test_system_reports_caps_and_blockers(admin, host):
    s = admin.get("/api/system").json()
    assert s["caps"]["total_sandbox_memory"] == 6 * GIB and s["can_start_default"]
    host.free_disk = 0
    s = admin.get("/api/system").json()
    assert not s["can_start_default"] and s["blockers"]


def test_templates_report_build_state(admin, backend):
    backend.images.discard(TEMPLATES["node"].image)
    built = {t["name"]: t["built"] for t in admin.get("/api/templates").json()}
    assert built == {"python": True, "node": False, "static": True}
    assert create(admin, template="node").status_code == 503


# ---- state machine and reconciliation ---------------------------------------

def test_transition_table():
    assert TRANSITIONS["destroyed"] == set()
    check_transition("running", "stopped")
    with pytest.raises(InvalidTransition):
        check_transition("destroyed", "running")
    with pytest.raises(InvalidTransition):
        check_transition("creating", "stopped")


def test_failed_start_leaves_error_row(admin, backend):
    backend.fail_next_start = "boom"
    sb = create(admin).json()
    assert sb["state"] == "error" and "boom" in sb["error"]
    # An errored sandbox doesn't count against capacity and can be destroyed.
    assert admin.delete(f"/api/sandboxes/{sb['id']}", headers=CSRF).status_code == 204


def test_sync_detects_exited_and_missing(admin, backend, svc):
    sid = create(admin).json()["id"]
    cid = svc.db.get_sandbox(sid).container_id
    backend.containers[cid]["status"] = "exited"  # e.g. OOM-killed
    assert admin.get(f"/api/sandboxes/{sid}").json()["state"] == "stopped"
    del backend.containers[cid]
    assert admin.get(f"/api/sandboxes/{sid}").json()["state"] == "error"


def test_interrupted_create_marked_error(svc):
    from app.db import Sandbox
    svc.db.insert_sandbox(Sandbox(id="x1", name="x", template="python", volume="ah-x1", memory=1, cpus=1))
    svc.reconcile_on_startup()
    assert svc.db.get_sandbox("x1").state == "error"


# ---- never touch unmanaged containers ----------------------------------------

def test_foreign_containers_are_never_touched(backend, svc):
    backend.add_foreign_container("minecraft")
    for op in (backend.status, backend.stop, backend.start):
        with pytest.raises(NotManaged):
            op("minecraft")
    with pytest.raises(NotManaged):
        backend.remove("minecraft", "ah-none")
    assert backend.containers["minecraft"]["status"] == "running"


def test_api_refuses_sandbox_pointing_at_foreign_container(admin, backend, svc):
    sid = create(admin).json()["id"]
    backend.add_foreign_container("foreign")
    svc.db.update_sandbox(svc.db.get_sandbox(sid), container_id="foreign")
    assert admin.post(f"/api/sandboxes/{sid}/stop", headers=CSRF).status_code == 409
    assert backend.containers["foreign"]["status"] == "running"


def test_all_created_resources_are_labelled(admin, backend):
    create(admin)
    spec = backend.created_specs[0]
    assert spec["labels"][MANAGED_LABEL] == "true"
    assert all(v[MANAGED_LABEL] == "true" for v in backend.volumes.values())


def test_parse_size():
    assert parse_size("2g") == 2 * GIB
    assert parse_size("512m") == 512 * 2**20
    assert parse_size("1.5G") == int(1.5 * GIB)
    with pytest.raises(ValueError):
        parse_size("lots")
