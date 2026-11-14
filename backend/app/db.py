"""SQLite storage: sandboxes, agent runs (with their event logs) and the audit log."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    template      TEXT NOT NULL,
    repo_url      TEXT,
    base_branch   TEXT,
    state         TEXT NOT NULL,
    error         TEXT,
    container_id  TEXT,
    volume        TEXT NOT NULL,
    memory        INTEGER NOT NULL,
    cpus          REAL NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sandboxes_state ON sandboxes(state);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    ip      TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    ok      INTEGER NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    sandbox_id    TEXT NOT NULL,
    runner        TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    state         TEXT NOT NULL,
    branch        TEXT,
    base_branch   TEXT,
    commit_sha    TEXT,
    pushed        INTEGER NOT NULL DEFAULT 0,
    exit_code     INTEGER,
    error         TEXT,
    timeout       INTEGER NOT NULL,
    max_turns     INTEGER NOT NULL,
    cost_usd      REAL,
    num_turns     INTEGER,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS runs_sandbox ON runs(sandbox_id, created_at);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state);

CREATE TABLE IF NOT EXISTS run_events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = [
    ("sandboxes", "allow_push", "INTEGER NOT NULL DEFAULT 0"),
]

RUN_STATES = ("queued", "running", "succeeded", "failed", "cancelled", "timed_out")
ACTIVE_RUN_STATES = ("queued", "running")


@dataclass
class Sandbox:
    id: str
    name: str
    template: str
    volume: str
    memory: int
    cpus: float
    state: str = "creating"
    repo_url: str | None = None
    base_branch: str | None = None
    error: str | None = None
    container_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    allow_push: bool = False

    def __post_init__(self) -> None:
        self.allow_push = bool(self.allow_push)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Run:
    id: str
    sandbox_id: str
    runner: str
    prompt: str
    timeout: int
    max_turns: int
    state: str = "queued"
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    pushed: bool = False
    exit_code: int | None = None
    error: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        self.pushed = bool(self.pushed)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_RUN_STATES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ---- sandboxes ---------------------------------------------------------

    def insert_sandbox(self, sb: Sandbox) -> None:
        d = sb.to_dict()
        cols = ", ".join(d)
        with self._lock:
            self._conn.execute(f"INSERT INTO sandboxes ({cols}) VALUES ({', '.join('?' * len(d))})",
                               tuple(d.values()))

    def update_sandbox(self, sb: Sandbox, **changes: Any) -> Sandbox:
        changes["updated_at"] = time.time()
        for k, v in changes.items():
            setattr(sb, k, v)
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self._lock:
            self._conn.execute(f"UPDATE sandboxes SET {sets} WHERE id = ?", (*changes.values(), sb.id))
        return sb

    def get_sandbox(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)).fetchone()
        return Sandbox(**dict(row)) if row else None

    def list_sandboxes(self, include_destroyed: bool = False) -> list[Sandbox]:
        q = "SELECT * FROM sandboxes"
        if not include_destroyed:
            q += " WHERE state != 'destroyed'"
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY created_at DESC").fetchall()
        return [Sandbox(**dict(r)) for r in rows]

    # ---- runs --------------------------------------------------------------

    def insert_run(self, run: Run) -> None:
        d = run.to_dict()
        with self._lock:
            self._conn.execute(f"INSERT INTO runs ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                               tuple(d.values()))

    def update_run(self, run: Run, **changes: Any) -> Run:
        for k, v in changes.items():
            setattr(run, k, v)
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*changes.values(), run.id))
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run(**dict(row)) if row else None

    def list_runs(self, sandbox_id: str | None = None, states: tuple[str, ...] | None = None,
                  limit: int = 50) -> list[Run]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if sandbox_id:
            q += " AND sandbox_id = ?"
            args.append(sandbox_id)
        if states:
            q += f" AND state IN ({', '.join('?' * len(states))})"
            args.extend(states)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [Run(**dict(r)) for r in rows]

    def add_run_event(self, run_id: str, seq: int, ts: float, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO run_events (run_id, seq, ts, data) VALUES (?, ?, ?, ?)",
                               (run_id, seq, ts, json.dumps(data)))

    def run_events(self, run_id: str, after_seq: int = -1, limit: int = 100_000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, data FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], **json.loads(r["data"])} for r in rows]

    # ---- audit -------------------------------------------------------------

    def audit(self, ip: str, action: str, target: str | None = None, ok: bool = True,
              detail: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (ts, ip, action, target, ok, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), ip, action, target, int(ok), json.dumps(detail) if detail else None))

    def list_audit(self, limit: int = 100, before_id: int | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM audit", []
        if before_id:
            q += " WHERE id < ?"
            args.append(before_id)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ok"] = bool(d["ok"])
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out
