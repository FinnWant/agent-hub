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

CREATE TABLE IF NOT EXISTS revoked_sessions (
    sig         TEXT PRIMARY KEY,
    expires     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint    TEXT PRIMARY KEY,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    label       TEXT,
    created_at  REAL NOT NULL,
    last_ok_at  REAL,
    failures    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    sandbox_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- approval | question | budget
    state       TEXT NOT NULL,          -- pending | answered | expired | closed
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    options     TEXT NOT NULL,          -- JSON list
    answer      TEXT,                   -- JSON {"choice", "text", "amount"}
    created_at  REAL NOT NULL,
    answered_at REAL,
    deadline    REAL,
    hook_id     TEXT,
    data        TEXT NOT NULL           -- JSON
);
CREATE INDEX IF NOT EXISTS requests_state ON requests(state, created_at);
CREATE INDEX IF NOT EXISTS requests_run ON requests(run_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id  TEXT NOT NULL,
    n       INTEGER NOT NULL,
    sha     TEXT NOT NULL,
    ts      REAL NOT NULL,
    seq     INTEGER NOT NULL,       -- the run's last log event when it was taken
    files   TEXT NOT NULL,          -- JSON: files changed since the previous checkpoint
    PRIMARY KEY (run_id, n)
);

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
    ("sandboxes", "preview_port", "INTEGER"),
    ("sandboxes", "workspace_bytes", "INTEGER"),
    ("runs", "session_id", "TEXT"),
    ("runs", "runner_state", "TEXT"),   # JSON: where to re-attach after a Kiln restart
    ("sandboxes", "verify_cmd", "TEXT"),
    ("runs", "verify_cmd", "TEXT"),     # the sandbox's checks when the run started
    ("runs", "verify", "TEXT"),         # passed | failed | None (no checks, or not run)
    ("runs", "verify_attempts", "INTEGER NOT NULL DEFAULT 0"),  # times the agent was sent back
    ("runs", "parent_run_id", "TEXT"),  # the run this one continues (its branch and session)
    ("sandboxes", "budget_usd", "REAL"),        # per run, estimated (usage.py)
    ("sandboxes", "approval_rules", "TEXT"),    # see owner.py
    ("runs", "budget_usd", "REAL"),
    ("runs", "approval_rules", "TEXT"),
    ("runs", "usage", "TEXT"),                  # JSON, usage.RunUsage
    ("runs", "cost_est", "REAL"),
    ("runs", "paused_seconds", "REAL NOT NULL DEFAULT 0"),  # waiting on the owner; not counted
    ("runs", "report", "TEXT"),                 # JSON: the handoff report (report.py)
    ("runs", "rewound_to", "INTEGER"),          # checkpoint the branch was put back to
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
    preview_port: int | None = None      # host port (Tailscale IP) published for previews
    workspace_bytes: int | None = None   # last measured size of /workspace
    verify_cmd: str | None = None        # the project's checks, e.g. "npm test" (see verify.py)
    budget_usd: float | None = None      # per run, estimated at API prices (usage.py)
    approval_rules: str | None = None    # actions that need the owner's OK (owner.py)

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
    session_id: str | None = None        # the agent's own session (Claude Code), when it reports one
    runner_state: str | None = None      # internal; never sent to the browser
    verify_cmd: str | None = None
    verify: str | None = None
    verify_attempts: int = 0
    parent_run_id: str | None = None
    budget_usd: float | None = None
    approval_rules: str | None = None
    usage: str | None = None
    cost_est: float | None = None
    paused_seconds: float = 0.0
    report: str | None = None
    rewound_to: int | None = None

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

    def add_run_event(self, run_id: str, seq: int, ts: float, data: dict[str, Any] | None,
                      runner_state: dict[str, Any] | None = None) -> None:
        """Store an event (None: none to store) and, if given, the runner state reached with it,
        in one transaction, so a restart re-attaches exactly after the stored events."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if data is not None:
                    self._conn.execute("INSERT INTO run_events (run_id, seq, ts, data) VALUES (?, ?, ?, ?)",
                                       (run_id, seq, ts, json.dumps(data)))
                if runner_state is not None:
                    self._conn.execute("UPDATE runs SET runner_state = ? WHERE id = ?",
                                       (json.dumps(runner_state), run_id))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ---- owner requests ------------------------------------------------------

    def insert_request(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(f"INSERT INTO requests ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               tuple(row.values()))

    def update_request(self, req_id: str, **changes: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self._lock:
            self._conn.execute(f"UPDATE requests SET {sets} WHERE id = ?", (*changes.values(), req_id))

    def get_request(self, req_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
        return dict(row) if row else None

    def list_requests(self, state: str | None = None, run_id: str | None = None,
                      limit: int = 100) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM requests WHERE 1=1", []
        if state:
            q += " AND state = ?"
            args.append(state)
        if run_id:
            q += " AND run_id = ?"
            args.append(run_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def add_checkpoint(self, run_id: str, n: int, sha: str, seq: int, files: list[str]) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO checkpoints (run_id, n, sha, ts, seq, files) VALUES (?, ?, ?, ?, ?, ?)",
                               (run_id, n, sha, time.time(), seq, json.dumps(files)))

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM checkpoints WHERE run_id = ? ORDER BY n", (run_id,)).fetchall()
        return [{**dict(r), "files": json.loads(r["files"])} for r in rows]

    def last_run_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
        return -1 if row[0] is None else int(row[0])

    def run_events(self, run_id: str, after_seq: int = -1, limit: int = 100_000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, data FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], **json.loads(r["data"])} for r in rows]

    # ---- push subscriptions --------------------------------------------------

    def save_push_subscription(self, endpoint: str, p256dh: str, auth: str, label: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO push_subscriptions (endpoint, p256dh, auth, label, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
                "label = excluded.label, failures = 0",
                (endpoint, p256dh, auth, label, time.time()))

    def list_push_subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM push_subscriptions ORDER BY created_at")]

    def delete_push_subscription(self, endpoint: str) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).rowcount > 0

    def touch_push_subscription(self, endpoint: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE endpoint = ?",
                                   (time.time(), endpoint))
            else:
                self._conn.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE endpoint = ?",
                                   (endpoint,))
                # A device that has failed many times in a row is gone for good.
                self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND failures >= 20", (endpoint,))

    # ---- sessions ending at logout -------------------------------------------------

    def revoke_session(self, sig: str, expires: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM revoked_sessions WHERE expires < ?", (time.time(),))
            self._conn.execute("INSERT OR IGNORE INTO revoked_sessions (sig, expires) VALUES (?, ?)", (sig, expires))

    def session_revoked(self, sig: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM revoked_sessions WHERE sig = ?", (sig,)).fetchone() is not None

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
