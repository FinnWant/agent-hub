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

    svc.destroy(sb.id, "test")
    assert svc.backend.status(sb.container_id) == "missing"
    assert not svc.backend.client.volumes.list(filters={"name": sb.volume})


def test_stop_and_start_keep_workspace(real):
    svc, created = real
    sb = svc.create(name="persist", template="static", repo_url=None, base_branch=None,
                    memory=512 * 2**20, cpus=1, ip="test")
    created.append(sb.id)
    svc.exec(sb.id, "echo kept > /workspace/marker", None, "test")
    svc.stop(sb.id, "test")
    svc.start(sb.id, "test")
    assert svc.exec(sb.id, "cat /workspace/marker", None, "test").output.strip() == "kept"
