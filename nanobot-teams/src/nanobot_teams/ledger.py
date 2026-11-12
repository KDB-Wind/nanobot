"""Durable admission, reservations, target quarantine, and content-free observations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .config import Budget
from .file_lock import FileLock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, root_task_id TEXT, parent_task_id TEXT,
    caller TEXT NOT NULL, target TEXT NOT NULL, hop INTEGER,
    status TEXT NOT NULL, error_code TEXT, truncated INTEGER NOT NULL DEFAULT 0,
    http_sent INTEGER NOT NULL DEFAULT 0, request_bytes INTEGER NOT NULL DEFAULT 0,
    response_bytes INTEGER NOT NULL DEFAULT 0, result_bytes INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started_at DESC);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL, event TEXT NOT NULL,
    at TEXT NOT NULL, actor TEXT, reason TEXT
);
"""
_EXTRA_COLUMNS = {
    "team_id": "TEXT NOT NULL DEFAULT ''",
    "endpoint": "TEXT NOT NULL DEFAULT ''",
    "thread_key": "TEXT NOT NULL DEFAULT ''",
    "session_id": "TEXT NOT NULL DEFAULT ''",
    "dispatch_state": "TEXT NOT NULL DEFAULT 'observed'",
    "blocked": "INTEGER NOT NULL DEFAULT 0",
    "charged_bytes": "INTEGER NOT NULL DEFAULT 0",
    "charged_seconds": "REAL NOT NULL DEFAULT 0",
}
_BASE_COLUMNS = (
    "task_id,root_task_id,parent_task_id,caller,target,hop,status,error_code,truncated,"
    "http_sent,request_bytes,response_bytes,result_bytes,started_at,finished_at,duration_ms"
)
_COLUMNS = _BASE_COLUMNS + "," + ",".join(_EXTRA_COLUMNS)


class LedgerError(RuntimeError):
    """Control state cannot be safely read or committed."""


class AdmissionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class Reservation:
    result_max_bytes: int
    timeout_s: float


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    root_task_id: str | None
    parent_task_id: str | None
    caller: str
    target: str
    hop: int | None
    status: str
    error_code: str | None
    truncated: bool
    http_sent: bool
    request_bytes: int
    response_bytes: int
    result_bytes: int
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    team_id: str = ""
    endpoint: str = ""
    thread_key: str = ""
    session_id: str = ""
    dispatch_state: Literal["submitted", "observed", "unknown"] = "observed"
    blocked: bool = False
    charged_bytes: int = 0
    charged_seconds: float = 0.0

    def to_json(self) -> str:
        data = asdict(self)
        data["submission_may_have_started"] = self.http_sent
        data["observation"] = "local view; status is not proof of remote success"
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str | Path | None, *, read_only: bool = False) -> None:
        self._path = None if path is None else Path(path).resolve()
        self._conn: sqlite3.Connection | None = None
        self._lock: FileLock | None = None
        self._scope: tuple[str, str] | None = None
        self._read_only = read_only
        if self._path is None:
            return
        try:
            if read_only:
                if not self._path.exists():
                    return
                self._conn = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True,
                                             timeout=0.1)
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = FileLock(Path(str(self._path) + ".lock"))
            self._conn = sqlite3.connect(self._path, isolation_level=None, timeout=0.1)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
        except (OSError, sqlite3.Error, LedgerError) as exc:
            self.close()
            raise LedgerError("Cannot open ledger or another writer owns it.") from exc

    def _migrate(self) -> None:
        assert self._conn is not None
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
        with self._transaction():
            for name, definition in _EXTRA_COLUMNS.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            if "charged_bytes" not in columns:
                self._conn.execute(
                    "UPDATE tasks SET charged_bytes=result_bytes, "
                    "charged_seconds=COALESCE(duration_ms,0)/1000.0, "
                    "blocked=(status IN ('outcome_unknown','timed_out')), "
                    "dispatch_state=CASE WHEN status IN ('outcome_unknown','timed_out') "
                    "THEN 'unknown' ELSE 'observed' END"
                )

    @contextmanager
    def _transaction(self):
        if self._conn is None or self._read_only:
            raise LedgerError("A writable persistent ledger is required.")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise LedgerError("Ledger transaction failed; dispatch is unsafe.") from exc

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def bind(self, team_id: str, caller: str) -> None:
        if self._scope == (team_id, caller):
            return
        with self._transaction():
            assert self._conn is not None
            scope = json.dumps([team_id, caller])
            old = self._conn.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
            if old is not None and old[0] != scope:
                raise LedgerError("Ledger belongs to another team or caller; use a separate ledger.")
            if old is None:
                if self._conn.execute("SELECT 1 FROM tasks WHERE caller != ? LIMIT 1",
                                      (caller,)).fetchone():
                    raise LedgerError("Legacy ledger contains other callers; migrate it separately.")
                self._conn.execute("INSERT INTO metadata VALUES ('scope',?)", (scope,))
                self._conn.execute("UPDATE tasks SET team_id=? WHERE team_id=''", (team_id,))
            pending = self._conn.execute(
                "SELECT task_id FROM tasks WHERE dispatch_state='submitted'"
            ).fetchall()
            for (task_id,) in pending:
                self._conn.execute(
                    "UPDATE tasks SET dispatch_state='unknown', error_code='interrupted', "
                    "finished_at=? WHERE task_id=?", (utc_now(), task_id)
                )
                self._event(task_id, "recovered_unknown")
        self._scope = (team_id, caller)

    def _event(self, task_id: str, event: str, actor: str | None = None,
               reason: str | None = None) -> None:
        assert self._conn is not None
        self._conn.execute("INSERT INTO events(task_id,event,at,actor,reason) VALUES (?,?,?,?,?)",
                           (task_id, event, utc_now(), actor, reason))

    def admit(self, entry: TaskRecord, budget: Budget | None, max_bytes: int,
              timeout_s: float, *, continuing: bool) -> Reservation:
        self.bind(entry.team_id, entry.caller)
        with self._transaction():
            assert self._conn is not None
            blocked = self._conn.execute(
                "SELECT task_id FROM tasks WHERE blocked=1 AND "
                "(endpoint=? OR endpoint='') LIMIT 1",
                (entry.endpoint,),
            ).fetchone()
            if blocked:
                raise AdmissionError("blocked", f"Unknown outcome for task {blocked[0]}; verify target first.")
            if continuing and not self._conn.execute(
                "SELECT 1 FROM tasks WHERE team_id=? AND caller=? AND target=? AND endpoint=? "
                "AND thread_key=? AND status='completed' LIMIT 1",
                (entry.team_id, entry.caller, entry.target, entry.endpoint, entry.thread_key),
            ).fetchone():
                raise AdmissionError("invalid_thread", "Continue only a thread returned for this caller and target.")
            used_count, used_bytes, used_seconds = self._totals(entry.caller)
            if budget is not None:
                if budget.delegations is not None and used_count >= budget.delegations:
                    raise AdmissionError("budget_exceeded", f"Delegation budget spent: {used_count} of {budget.delegations}.")
                if budget.result_bytes is not None:
                    max_bytes = min(max_bytes, budget.result_bytes - used_bytes)
                    if max_bytes <= 0:
                        raise AdmissionError("budget_exceeded", f"Result byte budget spent: {used_bytes} of {budget.result_bytes}.")
                if budget.chain_seconds is not None:
                    timeout_s = min(timeout_s, budget.chain_seconds - used_seconds)
                    if timeout_s <= 0:
                        raise AdmissionError("budget_exceeded", "Time budget spent.")
            values = asdict(entry)
            values.update(status="outcome_unknown", error_code="execution_pending", http_sent=True,
                          dispatch_state="submitted", blocked=True,
                          charged_bytes=max_bytes, charged_seconds=timeout_s)
            self._insert(TaskRecord(**values))
            self._event(entry.task_id, "admitted")
        return Reservation(max_bytes, timeout_s)

    def _insert(self, entry: TaskRecord) -> None:
        assert self._conn is not None
        values = asdict(entry)
        names = _COLUMNS.split(",")
        self._conn.execute(
            f"INSERT INTO tasks ({_COLUMNS}) VALUES ({','.join('?' for _ in names)})",
            tuple(values[name] for name in names),
        )

    def finish(self, entry: TaskRecord, *, unknown: bool) -> None:
        with self._transaction():
            assert self._conn is not None
            pending = self._conn.execute(
                "SELECT charged_bytes,charged_seconds FROM tasks "
                "WHERE task_id=? AND dispatch_state='submitted'", (entry.task_id,),
            ).fetchone()
            if pending is None:
                raise LedgerError("No pending admission to settle.")
            charged_bytes = pending[0] if unknown else entry.result_bytes
            charged_seconds = pending[1] if unknown else min((entry.duration_ms or 0)/1000, pending[1])
            self._conn.execute(
                "UPDATE tasks SET status=?,error_code=?,truncated=?,http_sent=?,request_bytes=?,"
                "response_bytes=?,result_bytes=?,finished_at=?,duration_ms=?,dispatch_state=?,"
                "blocked=?,charged_bytes=?,charged_seconds=? WHERE task_id=?",
                (entry.status, entry.error_code, entry.truncated, entry.http_sent, entry.request_bytes,
                 entry.response_bytes, entry.result_bytes, entry.finished_at, entry.duration_ms,
                 "unknown" if unknown else "observed", unknown, charged_bytes, charged_seconds,
                 entry.task_id),
            )
            self._event(entry.task_id, "unknown" if unknown else "observed")

    def acknowledge(self, task_id: str, *, actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip() or len(actor) > 128 or len(reason) > 1000:
            raise ValueError("actor and reason are required and must be bounded.")
        if any(ord(c) < 32 for c in actor + reason):
            raise ValueError("actor and reason must be one line.")
        with self._transaction():
            assert self._conn is not None
            row = self._conn.execute("SELECT blocked FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or not row[0]:
                raise ValueError("No quarantined task with that id.")
            self._conn.execute("UPDATE tasks SET blocked=0, dispatch_state='unknown' WHERE task_id=?",
                               (task_id,))
            self._event(task_id, "acknowledged_duplicate_risk", actor, reason)

    def _totals(self, caller: str) -> tuple[int, int, float]:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(charged_bytes),0),COALESCE(SUM(charged_seconds),0) "
            "FROM tasks WHERE caller=?", (caller,),
        ).fetchone()
        return int(row[0]), int(row[1]), float(row[2])

    def usage_for(self, caller: str) -> tuple[int, int, float] | None:
        if self._conn is None:
            return None
        try:
            return self._totals(caller)
        except sqlite3.Error:
            return None

    def identity(self) -> tuple[str, str] | None:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
            value = json.loads(row[0]) if row else None
            if not isinstance(value, list) or len(value) != 2 or any(not isinstance(item, str) for item in value):
                raise LedgerError("Ledger scope is unverified.")
            return value[0], value[1]
        except (sqlite3.Error, ValueError) as exc:
            raise LedgerError("Cannot verify ledger scope.") from exc

    def callers(self) -> set[str]:
        if self._conn is None:
            return set()
        try:
            return {row[0] for row in self._conn.execute("SELECT DISTINCT caller FROM tasks")}
        except sqlite3.Error as exc:
            raise LedgerError("Cannot read ledger callers.") from exc

    def list_tasks(self, limit: int = 50, target: str | None = None,
                   status: str | None = None) -> list[TaskRecord]:
        if self._conn is None:
            return []
        clauses, params = [], []
        if target:
            clauses.append("target=?")
            params.append(target)
        if status:
            clauses.append("status=?")
            params.append(status)
        query = f"SELECT {_COLUMNS} FROM tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at DESC,task_id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        try:
            return [_row_to_record(row) for row in self._conn.execute(query, params).fetchall()]
        except sqlite3.Error as exc:
            raise LedgerError("Cannot read ledger tasks.") from exc

    def get_task(self, task_id: str) -> TaskRecord | None:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return None if row is None else _row_to_record(row)
        except sqlite3.Error as exc:
            raise LedgerError("Cannot read ledger task.") from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None


def _row_to_record(row: tuple) -> TaskRecord:
    values = dict(zip(_COLUMNS.split(","), row, strict=True))
    for key in ("truncated", "http_sent", "blocked"):
        values[key] = bool(values[key])
    return TaskRecord(**values)
