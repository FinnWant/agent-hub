"""FastAPI app: auth, system info, templates, sandboxes, exec, audit log."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__, auth, config
from .capacity import disk_usage, mem_available, mem_total
from .config import format_size, parse_size
from .db import Database, Sandbox
from .docker_backend import Backend, BackendError, DockerBackend, FakeBackend, NotManaged
from .sandboxes import SandboxError, SandboxService
from .templates import TEMPLATES

log = logging.getLogger("agent_hub")


def build_service(backend: Backend | None = None, db: Database | None = None) -> SandboxService:
    s = config.settings
    if backend is None:
        backend = FakeBackend() if s.demo_mode else DockerBackend()
    return SandboxService(db or Database(s.db_path), backend)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "service"):
        app.state.service = build_service()
    svc: SandboxService = app.state.service
    svc.reconcile_on_startup()
    try:
        svc.backend.ensure_network()
    except Exception as e:  # keep serving so /api/health can report the problem
        log.error("Docker unavailable at startup: %s", e)
    yield


app = FastAPI(title="Agent Hub", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
Admin = Annotated[None, Depends(auth.require_admin)]


def service(request: Request) -> SandboxService:
    return request.app.state.service


Svc = Annotated[SandboxService, Depends(service)]


@app.exception_handler(SandboxError)
async def sandbox_error(_: Request, exc: SandboxError) -> JSONResponse:
    body: dict = {"detail": str(exc)}
    if hasattr(exc, "reasons"):
        body["reasons"] = exc.reasons
    return JSONResponse(body, status_code=exc.status_code)


@app.exception_handler(NotManaged)
async def not_managed(_: Request, exc: NotManaged) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(BackendError)
async def backend_error(_: Request, exc: BackendError) -> JSONResponse:
    return JSONResponse({"detail": f"Docker error: {exc}"}, status_code=502)


# ---------------------------------------------------------------------------
# Health and auth
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(svc: Svc) -> dict:
    return {"status": "ok", "version": __version__, "docker": svc.backend.ping(),
            "demo": config.settings.demo_mode}


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


@app.post("/api/auth/login", dependencies=[Depends(auth.require_configured), Depends(auth.require_csrf)])
def login(body: LoginBody, request: Request, response: Response, svc: Svc) -> dict:
    ip = auth.client_ip(request)
    wait = auth.login_limiter.retry_after(ip)
    if wait:
        raise HTTPException(429, f"Too many failed logins. Try again in {wait // 60 + 1} min.",
                            headers={"Retry-After": str(wait)})
    if not auth.check_password(body.password):
        auth.login_limiter.fail(ip)
        svc.db.audit(ip, "auth.login", ok=False)
        raise HTTPException(401, "Wrong password")
    auth.login_limiter.reset(ip)
    token, max_age = auth.make_token()
    response.set_cookie(auth.COOKIE, token, max_age=max_age, httponly=True, samesite="strict", path="/")
    svc.db.audit(ip, "auth.login")
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(_: Admin, request: Request, response: Response, svc: Svc) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    svc.db.audit(auth.client_ip(request), "auth.logout")
    return {"ok": True}


@app.get("/api/auth/me")
def me(_: Admin) -> dict:
    return {"authenticated": True}


# ---------------------------------------------------------------------------
# System and templates
# ---------------------------------------------------------------------------

@app.get("/api/system")
def system(_: Admin, svc: Svc) -> dict:
    s = config.settings
    disk_total, disk_free = disk_usage(s.disk_path)
    running = svc.running()
    used = sum(sb.memory for sb in running)
    blockers = svc.blockers(s.default_sandbox_memory)
    load1, load5, load15 = os.getloadavg()
    return {
        "host": {"cpus": os.cpu_count(), "load": [load1, load5, load15],
                 "mem_total": mem_total(), "mem_available": mem_available(),
                 "disk_total": disk_total, "disk_free": disk_free},
        "caps": {"max_concurrent_sandboxes": s.max_concurrent_sandboxes,
                 "default_sandbox_memory": s.default_sandbox_memory,
                 "max_sandbox_memory": s.max_sandbox_memory,
                 "total_sandbox_memory": s.total_sandbox_memory,
                 "memory_reserve": s.memory_reserve,
                 "default_sandbox_cpus": s.default_sandbox_cpus,
                 "max_sandbox_cpus": s.max_sandbox_cpus,
                 "min_free_disk": s.min_free_disk},
        "sandboxes": {"running": len(running), "memory_used": used},
        "can_start_default": not blockers,
        "blockers": blockers,
    }


@app.get("/api/templates")
def templates(_: Admin, svc: Svc) -> list[dict]:
    return [{"name": t.name, "description": t.description, "image": t.image,
             "preview_port": t.preview_port, "built": svc.backend.image_exists(t.image)}
            for t in TEMPLATES.values()]


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------

def sandbox_out(sb: Sandbox) -> dict:
    d = sb.to_dict()
    d["memory_human"] = format_size(sb.memory)
    d.pop("volume")
    d.pop("container_id")
    return d


class CreateSandbox(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
    template: Literal["python", "node", "static"]
    # HTTPS only, no embedded credentials (tokens are injected per run, never stored in the URL).
    repo_url: str | None = Field(default=None, max_length=300,
                                 pattern=r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/-]+$")
    base_branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}$")
    memory: str | None = Field(default=None, description="e.g. 2g or 512m")
    cpus: float | None = Field(default=None, gt=0)

    @field_validator("memory")
    @classmethod
    def _memory(cls, v: str | None) -> str | None:
        if v is not None:
            n = parse_size(v)
            if n < 256 * 2**20:
                raise ValueError("At least 256m")
        return v


@app.get("/api/sandboxes")
def list_sandboxes(_: Admin, svc: Svc) -> list[dict]:
    return [sandbox_out(sb) for sb in svc.list()]


@app.post("/api/sandboxes", status_code=201)
def create_sandbox(body: CreateSandbox, _: Admin, request: Request, svc: Svc) -> dict:
    sb = svc.create(name=body.name, template=body.template, repo_url=body.repo_url,
                    base_branch=body.base_branch,
                    memory=parse_size(body.memory) if body.memory else None,
                    cpus=body.cpus, ip=auth.client_ip(request))
    return sandbox_out(sb)


@app.get("/api/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str, _: Admin, svc: Svc) -> dict:
    return sandbox_out(svc.get(sandbox_id))


@app.post("/api/sandboxes/{sandbox_id}/start")
def start_sandbox(sandbox_id: str, _: Admin, request: Request, svc: Svc) -> dict:
    return sandbox_out(svc.start(sandbox_id, auth.client_ip(request)))


@app.post("/api/sandboxes/{sandbox_id}/stop")
def stop_sandbox(sandbox_id: str, _: Admin, request: Request, svc: Svc) -> dict:
    return sandbox_out(svc.stop(sandbox_id, auth.client_ip(request)))


@app.delete("/api/sandboxes/{sandbox_id}", status_code=204)
def destroy_sandbox(sandbox_id: str, _: Admin, request: Request, svc: Svc) -> Response:
    svc.destroy(sandbox_id, auth.client_ip(request))
    return Response(status_code=204)


class ExecBody(BaseModel):
    cmd: str | list[str] = Field(description="Shell string (run with bash -lc) or argv list")
    timeout: int | None = Field(default=None, gt=0)


@app.post("/api/sandboxes/{sandbox_id}/exec")
def exec_in_sandbox(sandbox_id: str, body: ExecBody, _: Admin, request: Request, svc: Svc) -> dict:
    if not body.cmd:
        raise HTTPException(422, "cmd is empty")
    r = svc.exec(sandbox_id, body.cmd, body.timeout, auth.client_ip(request))
    return {"exit_code": r.exit_code, "output": r.output, "truncated": r.truncated, "timed_out": r.timed_out}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@app.get("/api/audit")
def audit(_: Admin, svc: Svc, limit: int = Query(100, ge=1, le=500),
          before_id: int | None = Query(None, ge=1)) -> list[dict]:
    return svc.db.list_audit(limit=limit, before_id=before_id)
