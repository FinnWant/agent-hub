"""FastAPI app: auth, system info, templates, sandboxes, exec, terminal, audit log, web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__, auth, config
from .capacity import disk_usage, mem_available, mem_total
from .config import format_size, parse_size
from .db import Database, Sandbox
from . import demo, files as files_mod, preview as preview_mod
from .db import Run
from .monitor import Monitor
from .notify import Notifier, push_endpoint_ok
from .docker_backend import Backend, BackendError, DockerBackend, FakeBackend, NotManaged
from .runners import default_runners
from .runs import RunManager
from .sandboxes import InvalidTransition, SandboxError, SandboxService
from .templates import TEMPLATES
from .terminal import TerminalSession
from .workspace import default_workspace

log = logging.getLogger("agent_hub")


def build_service(backend: Backend | None = None, db: Database | None = None) -> SandboxService:
    s = config.settings
    if backend is None:
        backend = FakeBackend() if s.demo_mode else DockerBackend()
    return SandboxService(db or Database(s.db_path), backend)


def build_runs(svc: SandboxService) -> RunManager:
    return RunManager(svc.db, svc, default_runners(svc.backend), default_workspace(svc.backend))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "service"):
        app.state.service = build_service()
    svc: SandboxService = app.state.service
    if not hasattr(app.state, "runs"):
        app.state.runs = build_runs(svc)
    if not hasattr(app.state, "notifier"):
        app.state.notifier = Notifier(svc.db)
    notifier: Notifier = app.state.notifier
    notifier.bind_loop(asyncio.get_running_loop())
    svc.on_problem = notifier.sandbox_problem
    app.state.runs.on_finished = notifier.run_finished
    svc.reconcile_on_startup()
    app.state.runs.reconcile_on_startup()
    try:
        svc.backend.ensure_network()
    except Exception as e:  # keep serving so /api/health can report the problem
        log.error("Docker unavailable at startup: %s", e)
    if config.settings.demo_mode:
        seed_demo(svc)
    app.state.monitor = Monitor(svc)
    task = asyncio.create_task(app.state.monitor.run()) if config.settings.stats_interval > 0 else None
    yield
    if task:
        task.cancel()


def seed_demo(svc: SandboxService) -> None:
    """Give DEMO_MODE something to show on first start."""
    if svc.db.list_sandboxes():
        return
    for name, template, memory in (("api-server", "python", 2 * 2**30), ("landing-page", "static", 512 * 2**20)):
        try:
            svc.create(name=name, template=template, repo_url=None, base_branch=None,
                       memory=memory, cpus=None, ip="demo")
        except SandboxError as e:
            log.info("demo seed skipped %s: %s", name, e)


app = FastAPI(title="Kiln", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
Admin = Annotated[None, Depends(auth.require_admin)]

# xterm.js and React set inline styles; everything else is same-origin only.
# ipc: / http://ipc.localhost let the Windows app's bridge (Tauri) talk to its shell; browsers
# never use them. worker-src for the service worker that shows push notifications.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self' ipc: http://ipc.localhost; worker-src 'self'; "
       "manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    if config.settings.https:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def service(request: Request) -> SandboxService:
    return request.app.state.service


Svc = Annotated[SandboxService, Depends(service)]


def run_manager(request: Request) -> RunManager:
    return request.app.state.runs


Runs = Annotated[RunManager, Depends(run_manager)]


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


@app.post("/api/auth/login", dependencies=[Depends(auth.require_not_preview_host), Depends(auth.require_configured),
                                           Depends(auth.require_csrf)])
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
    response.set_cookie(auth.COOKIE, token, max_age=max_age, httponly=True, samesite="strict", path="/",
                        secure=config.settings.https)
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
        "preview": {"enabled": s.previews_enabled, "host": s.preview_host.rstrip(".") or None,
                    "ports": s.preview_port_range},
        # Only whether credentials exist, never their values.
        "agent": {"claude_token": bool(s.claude_code_oauth_token) or s.demo_mode,
                  "github_token": bool(s.github_token) or s.demo_mode,
                  "model": s.agent_model or None,
                  "run_timeout_default": s.run_timeout_default,
                  "run_timeout_max": s.run_timeout_max,
                  "run_max_turns_default": s.run_max_turns_default},
    }


@app.get("/api/templates")
def templates(_: Admin, svc: Svc) -> list[dict]:
    return [{"name": t.name, "description": t.description, "image": t.image,
             "preview_port": t.preview_port, "built": svc.backend.image_exists(t.image)}
            for t in TEMPLATES.values()]


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------

def sandbox_out(sb: Sandbox, runs: RunManager | None = None, monitor: Monitor | None = None) -> dict:
    d = sb.to_dict()
    d["memory_human"] = format_size(sb.memory)
    d["preview_url"] = preview_mod.preview_url(sb)
    d["memory_usage"] = monitor.memory.get(sb.id) if monitor and sb.state == "running" else None
    d.pop("volume")
    d.pop("container_id")
    if runs is not None:
        last = runs.db.list_runs(sandbox_id=sb.id, limit=1)
        d["last_run"] = ({"id": last[0].id, "state": last[0].state, "created_at": last[0].created_at,
                          "finished_at": last[0].finished_at} if last else None)
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


def monitor_of(request: Request) -> Monitor | None:
    return getattr(request.app.state, "monitor", None)


Mon = Annotated[Monitor | None, Depends(monitor_of)]


@app.get("/api/sandboxes")
def list_sandboxes(_: Admin, svc: Svc, runs: Runs, mon: Mon) -> list[dict]:
    return [sandbox_out(sb, runs, mon) for sb in svc.list()]


@app.post("/api/sandboxes", status_code=201)
def create_sandbox(body: CreateSandbox, _: Admin, request: Request, svc: Svc) -> dict:
    sb = svc.create(name=body.name, template=body.template, repo_url=body.repo_url,
                    base_branch=body.base_branch,
                    memory=parse_size(body.memory) if body.memory else None,
                    cpus=body.cpus, ip=auth.client_ip(request))
    return sandbox_out(sb)


@app.get("/api/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str, _: Admin, svc: Svc, runs: Runs, mon: Mon) -> dict:
    return sandbox_out(svc.get(sandbox_id), runs, mon)


class SandboxSettings(BaseModel):
    allow_push: bool | None = None


@app.patch("/api/sandboxes/{sandbox_id}")
def update_sandbox(sandbox_id: str, body: SandboxSettings, _: Admin, request: Request, svc: Svc,
                   runs: Runs) -> dict:
    sb = svc.update_settings(sandbox_id, auth.client_ip(request), allow_push=body.allow_push)
    return sandbox_out(sb, runs)


@app.post("/api/sandboxes/{sandbox_id}/start")
def start_sandbox(sandbox_id: str, _: Admin, request: Request, svc: Svc) -> dict:
    return sandbox_out(svc.start(sandbox_id, auth.client_ip(request)))


@app.post("/api/sandboxes/{sandbox_id}/stop")
async def stop_sandbox(sandbox_id: str, _: Admin, request: Request, svc: Svc, runs: Runs) -> dict:
    ip = auth.client_ip(request)
    await runs.cancel_for_sandbox(sandbox_id, ip)  # a run can't survive its container
    return sandbox_out(await asyncio.to_thread(svc.stop, sandbox_id, ip))


@app.delete("/api/sandboxes/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str, _: Admin, request: Request, svc: Svc, runs: Runs) -> Response:
    ip = auth.client_ip(request)
    await runs.cancel_for_sandbox(sandbox_id, ip)
    await asyncio.to_thread(svc.destroy, sandbox_id, ip)
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
# Files, diff, preview, workspace size, cleanup
# ---------------------------------------------------------------------------

def _running_sandbox(svc: SandboxService, sandbox_id: str) -> Sandbox:
    sb = svc.get(sandbox_id)
    if sb.state != "running" or not sb.container_id:
        raise InvalidTransition("Start the sandbox to look inside it")
    return sb


@app.get("/api/sandboxes/{sandbox_id}/files")
def browse_files(sandbox_id: str, _: Admin, svc: Svc, path: str = Query("", max_length=4096)) -> dict:
    if config.settings.demo_mode:
        _running_sandbox(svc, sandbox_id)
        return demo.browse(path)
    return files_mod.browse(svc.backend, _running_sandbox(svc, sandbox_id), path)


@app.get("/api/runs/{run_id}/diff")
def run_diff(run_id: str, _: Admin, svc: Svc, runs: Runs) -> dict:
    run = runs.get(run_id)
    if config.settings.demo_mode:
        _running_sandbox(svc, run.sandbox_id)
        return demo.diff(run)
    return files_mod.run_diff(svc.backend, _running_sandbox(svc, run.sandbox_id), run)


@app.get("/api/sandboxes/{sandbox_id}/preview")
def preview_status(sandbox_id: str, _: Admin, svc: Svc) -> dict:
    if config.settings.demo_mode:
        return demo.preview(svc.get(sandbox_id))
    return preview_mod.status(svc.backend, svc.get(sandbox_id))


@app.post("/api/sandboxes/{sandbox_id}/preview/static")
def preview_start(sandbox_id: str, _: Admin, request: Request, svc: Svc) -> dict:
    sb = svc.get(sandbox_id)
    preview_mod.start_static(svc.backend, sb)
    svc.db.audit(auth.client_ip(request), "preview.start", sb.id)
    return preview_mod.status(svc.backend, sb)


@app.delete("/api/sandboxes/{sandbox_id}/preview/static")
def preview_stop(sandbox_id: str, _: Admin, request: Request, svc: Svc) -> dict:
    sb = svc.get(sandbox_id)
    preview_mod.stop_static(svc.backend, sb)
    svc.db.audit(auth.client_ip(request), "preview.stop", sb.id)
    return preview_mod.status(svc.backend, sb)


@app.post("/api/sandboxes/{sandbox_id}/measure")
def measure_workspace(sandbox_id: str, _: Admin, svc: Svc, mon: Mon) -> dict:
    sb = _running_sandbox(svc, sandbox_id)
    size = mon.measure_workspace(sb.id) if mon else None
    return {"workspace_bytes": size}


@app.post("/api/system/prune")
def prune(_: Admin, request: Request, svc: Svc) -> dict:
    keep = {sb.id for sb in svc.db.list_sandboxes()}  # every sandbox you haven't destroyed
    current = {t.image for t in TEMPLATES.values()}
    result = svc.backend.prune(keep, current)
    svc.db.audit(auth.client_ip(request), "system.prune", detail={
        "bytes": result["bytes"], "containers": len(result["containers"]),
        "volumes": len(result["volumes"]), "images": result["images"][:20]})
    return result


# ---------------------------------------------------------------------------
# Notifications: live event stream and Web Push
# ---------------------------------------------------------------------------

def notifier_of(request: Request) -> Notifier:
    return request.app.state.notifier


Notif = Annotated[Notifier, Depends(notifier_of)]


@app.websocket("/api/events")
async def events(ws: WebSocket) -> None:
    """Notification events for every open window: {"events": [...]}; keep-alive every 25 s."""
    notifier: Notifier = ws.app.state.notifier
    if not auth.configured() or not auth.ws_allowed(ws):
        await ws.close(code=4401)
        return
    queue = notifier.subscribe()
    try:
        await ws.accept()
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=25)
                await ws.send_json({"events": [ev]})
            except TimeoutError:
                await ws.send_json({"events": []})
    except Exception:  # noqa: BLE001  (client went away)
        pass
    finally:
        notifier.unsubscribe(queue)


@app.get("/api/push")
def push_info(_: Admin, notifier: Notif) -> dict:
    """Whether Web Push is available here, the key browsers subscribe with, and the devices."""
    s = config.settings
    devices = [{"label": d["label"], "endpoint_host": urlsplit(d["endpoint"]).hostname,
                "created_at": d["created_at"], "last_ok_at": d["last_ok_at"], "endpoint": d["endpoint"]}
               for d in notifier.db.list_push_subscriptions()]
    return {"available": s.https, "public_key": notifier.vapid.public_key if s.https else None,
            "devices": devices}


class PushSubscription(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: dict[str, str]
    label: str | None = Field(default=None, max_length=60)


@app.post("/api/push/subscribe", status_code=201)
def push_subscribe(body: PushSubscription, _: Admin, request: Request, notifier: Notif) -> dict:
    if not config.settings.https:
        raise HTTPException(400, "Push notifications need Kiln on HTTPS (PUBLIC_URL)")
    if not push_endpoint_ok(body.endpoint):
        raise HTTPException(400, "Not a recognised push service")
    p256dh, key = body.keys.get("p256dh", ""), body.keys.get("auth", "")
    if not (20 <= len(p256dh) <= 200 and 8 <= len(key) <= 100):
        raise HTTPException(400, "Invalid subscription keys")
    notifier.db.save_push_subscription(body.endpoint, p256dh, key, body.label)
    notifier.db.audit(auth.client_ip(request), "push.subscribe", detail={"label": body.label,
                                                                         "service": urlsplit(body.endpoint).hostname})
    return {"ok": True}


class PushUnsubscribe(BaseModel):
    endpoint: str = Field(max_length=1000)


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: PushUnsubscribe, _: Admin, request: Request, notifier: Notif) -> dict:
    removed = notifier.db.delete_push_subscription(body.endpoint)
    if removed:
        notifier.db.audit(auth.client_ip(request), "push.unsubscribe")
    return {"removed": removed}


@app.post("/api/push/test")
async def push_test(_: Admin, notifier: Notif) -> dict:
    ev = {"kind": "test", "title": "Kiln", "body": "Notifications work on this device.", "url": "/"}
    notifier.publish(ev, push=False)  # open windows and the Windows app; push is sent below
    if not config.settings.https:
        return {"sent": 0, "removed": 0, "failed": 0}
    return await asyncio.to_thread(notifier.push_all, ev)


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

def run_out(run: Run) -> dict:
    return run.to_dict()


class StartRun(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    runner: str = Field(default="claude-code", max_length=40)
    timeout_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    max_turns: int | None = Field(default=None, ge=1, le=10_000)


@app.get("/api/sandboxes/{sandbox_id}/runs")
def list_runs(sandbox_id: str, _: Admin, svc: Svc, runs: Runs, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    svc.get(sandbox_id)
    return [run_out(r) for r in runs.db.list_runs(sandbox_id=sandbox_id, limit=limit)]


@app.post("/api/sandboxes/{sandbox_id}/runs", status_code=201)
async def start_run(sandbox_id: str, body: StartRun, _: Admin, request: Request, runs: Runs) -> dict:
    run = await asyncio.to_thread(
        runs.create, sandbox_id=sandbox_id, prompt=body.prompt.strip(), runner=body.runner,
        timeout=body.timeout_minutes * 60 if body.timeout_minutes else None, max_turns=body.max_turns,
        ip=auth.client_ip(request))
    runs.launch(run)
    return run_out(run)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, _: Admin, runs: Runs) -> dict:
    return run_out(runs.get(run_id))


@app.get("/api/runs/{run_id}/log")
def run_log(run_id: str, _: Admin, runs: Runs, after: int = Query(-1, ge=-1)) -> list[dict]:
    runs.get(run_id)
    return runs.db.run_events(run_id, after_seq=after, limit=5000)


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, _: Admin, request: Request, runs: Runs) -> dict:
    return run_out(await runs.cancel(run_id, auth.client_ip(request)))


EVENT_BATCH_SECONDS = 0.15
EVENT_BATCH_MAX = 500


@app.websocket("/api/runs/{run_id}/events")
async def run_events(ws: WebSocket, run_id: str) -> None:
    """Backlog on connect, then live events batched every 150 ms as {"events": [...]}.
    Closes with 1000 after the run's "done" event; 4401 not logged in; 4404 no such run."""
    runs: RunManager = ws.app.state.runs
    if not auth.configured() or not auth.ws_allowed(ws):
        await ws.close(code=4401)
        return
    if runs.db.get_run(run_id) is None:
        await ws.accept()
        await ws.close(code=4404, reason="No such run")
        return
    queue = runs.subscribe(run_id)  # before reading the backlog, so nothing falls in between
    try:
        await ws.accept()
        backlog = runs.db.run_events(run_id)
        last = backlog[-1]["seq"] if backlog else -1
        for i in range(0, len(backlog), EVENT_BATCH_MAX):
            await ws.send_json({"events": backlog[i:i + EVENT_BATCH_MAX]})
        done = any(e["type"] == "done" for e in backlog)
        while not done:
            try:
                first = await asyncio.wait_for(queue.get(), timeout=25)
            except TimeoutError:
                await ws.send_json({"events": []})  # keep-alive; also detects a closed socket
                continue
            await asyncio.sleep(EVENT_BATCH_SECONDS)
            batch = [first]
            while not queue.empty() and len(batch) < EVENT_BATCH_MAX:
                batch.append(queue.get_nowait())
            batch = [e for e in batch if e["seq"] > last]
            if batch:
                last = batch[-1]["seq"]
                await ws.send_json({"events": batch})
                done = any(e["type"] == "done" for e in batch)
        await ws.close(code=1000)
    except Exception:  # noqa: BLE001  (client went away)
        pass
    finally:
        runs.unsubscribe(run_id, queue)


# ---------------------------------------------------------------------------
# Terminal (WebSocket)
#   client -> server: binary frames = keystrokes; text frames = {"type": "resize", "cols", "rows"}
#   server -> client: binary frames = terminal output
#   close codes: 4401 not logged in, 4404 no such sandbox, 4409 not running / too many terminals,
#                4408 idle timeout, 4502 Docker error, 1000 shell exited
# ---------------------------------------------------------------------------

MAX_INPUT_FRAME = 64 * 1024


def _dims(cols: object, rows: object) -> tuple[int, int]:
    def clamp(v: object, default: int, hi: int) -> int:
        try:
            return max(2, min(int(v), hi))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return clamp(cols, 80, 500), clamp(rows, 24, 200)


async def _bridge(ws: WebSocket, term: TerminalSession) -> str:
    idle = config.settings.terminal_idle_timeout

    async def output() -> str:
        while data := await term.read():
            await ws.send_bytes(data)
        return "shell exited"

    async def keyboard() -> str:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=idle)
            except TimeoutError:
                return "idle timeout"
            if msg["type"] == "websocket.disconnect":
                return "client closed"
            if (data := msg.get("bytes")) is not None:
                if len(data) <= MAX_INPUT_FRAME:
                    await term.write(data)
            elif text := msg.get("text"):
                try:
                    m = json.loads(text)
                except ValueError:
                    continue
                if isinstance(m, dict) and m.get("type") == "resize":
                    await term.resize(*_dims(m.get("cols"), m.get("rows")))

    tasks = [asyncio.create_task(output()), asyncio.create_task(keyboard())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    try:
        reason = done.pop().result()
    except Exception:  # the browser went away mid-send
        return "client closed"
    if reason != "client closed":
        try:
            await ws.close(code=4408 if reason == "idle timeout" else 1000, reason=reason)
        except Exception:
            pass
    return reason


@app.websocket("/api/sandboxes/{sandbox_id}/terminal")
async def terminal(ws: WebSocket, sandbox_id: str) -> None:
    svc: SandboxService = ws.app.state.service
    ip = auth.client_ip(ws)
    if not auth.configured() or not auth.ws_allowed(ws):
        await ws.close(code=4401)  # before accept(): the handshake itself is refused
        return
    cols, rows = _dims(ws.query_params.get("cols"), ws.query_params.get("rows"))
    try:
        sb = await asyncio.to_thread(svc.reserve_terminal, sandbox_id, ip)
    except SandboxError as e:
        await ws.accept()
        await ws.close(code=4000 + e.status_code, reason=str(e)[:120])
        return
    term: TerminalSession | None = None
    started = time.monotonic()
    reason = "error"
    try:
        try:
            term = await svc.backend.open_terminal(sb.container_id or "", cols, rows)
        except Exception as e:
            log.warning("terminal open failed for %s: %s", sb.id, e)
            svc.db.audit(ip, "terminal.open", sb.id, ok=False, detail={"error": str(e)[:200]})
            await ws.accept()
            await ws.close(code=4502, reason="Could not open a shell")
            return
        await ws.accept()
        svc.db.audit(ip, "terminal.open", sb.id, detail={"cols": cols, "rows": rows})
        reason = await _bridge(ws, term)
    finally:
        if term is not None:
            await term.close()
            svc.db.audit(ip, "terminal.close", sb.id,
                         detail={"seconds": round(time.monotonic() - started), "reason": reason})
        svc.release_terminal()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@app.get("/api/audit")
def audit(_: Admin, svc: Svc, limit: int = Query(100, ge=1, le=500),
          before_id: int | None = Query(None, ge=1)) -> list[dict]:
    return svc.db.list_audit(limit=limit, before_id=before_id)


# ---------------------------------------------------------------------------
# Web UI (frontend/dist). Registered last so it never shadows an API route.
# ---------------------------------------------------------------------------

@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def web_ui(path: str) -> Response:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "Not found")
    root = config.settings.frontend_dir.resolve()
    if path:
        f = (root / path).resolve()
        if f.is_relative_to(root) and f.is_file():
            immutable = f.is_relative_to(root / "assets")
            media = {".webmanifest": "application/manifest+json", ".js": "text/javascript"}.get(f.suffix)
            return FileResponse(f, media_type=media, headers={"Cache-Control": "public, max-age=31536000, immutable"
                                            if immutable else "no-cache"})
    index = root / "index.html"
    if not index.is_file():
        return PlainTextResponse("The web UI isn't built. Run deploy/install.sh (or npm run build in frontend/).",
                                 status_code=404)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
