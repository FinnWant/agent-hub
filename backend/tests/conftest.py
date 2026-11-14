from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth, config
from app.capacity import HostState
from app.db import Database
from app.docker_backend import FakeBackend
from app.main import app
from app.sandboxes import SandboxService
from app.templates import TEMPLATES

PASSWORD = "correct horse battery"
CSRF = {auth.CSRF_HEADER: auth.CSRF_VALUE}
GIB = 2**30
_HASH = auth.hash_password(PASSWORD)


@pytest.fixture
def settings(monkeypatch):
    """Known caps and a configured password, independent of the developer's .env."""
    s = dataclasses.replace(
        config.Settings(),
        admin_password_hash=_HASH, session_secret="s" * 64, demo_mode=False,
        max_concurrent_sandboxes=2, default_sandbox_memory=2 * GIB, max_sandbox_memory=4 * GIB,
        total_sandbox_memory=6 * GIB, memory_reserve=3 * GIB, default_sandbox_cpus=2,
        max_sandbox_cpus=4, min_free_disk=15 * GIB, io_device="", container_runtime="",
    )
    monkeypatch.setattr(config, "settings", s)
    auth.login_limiter._fails.clear()
    return s


class Host:
    """Mutable fake host so tests can simulate low memory or a full disk."""

    def __init__(self) -> None:
        self.mem_available = 11 * GIB
        self.free_disk = 100 * GIB

    def __call__(self) -> HostState:
        return HostState(mem_available=self.mem_available, free_disk=self.free_disk)


@pytest.fixture
def host() -> Host:
    return Host()


@pytest.fixture
def backend(settings) -> FakeBackend:
    b = FakeBackend()
    b.images = {t.image for t in TEMPLATES.values()}
    return b


@pytest.fixture
def svc(backend, host) -> SandboxService:
    return SandboxService(Database(":memory:"), backend, host_state=host)


@pytest.fixture
def client(svc):
    app.state.service = svc
    with TestClient(app) as c:
        yield c
    del app.state.service


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200, r.text
    return client
