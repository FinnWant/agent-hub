"""SQLite storage: sandboxes and the audit log."""

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
"""


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
