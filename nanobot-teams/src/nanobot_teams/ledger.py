"""Local task ledger: what this MCP process submitted and observed.

One SQLite file per installation, written only by the MCP server process that
runs the delegations. Rows carry ids, chain position, status, error codes,
byte counts, and timing. They never carry task text, answers, or credentials:
the ledger records observations, not content. A row says what the local
process saw, not what the remote bot eventually did.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("nanobot_teams.ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    root_task_id TEXT,
    parent_task_id TEXT,
    caller TEXT NOT NULL,
    target TEXT NOT NULL,
    hop INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    http_sent INTEGER NOT NULL DEFAULT 0,
    request_bytes INTEGER NOT NULL DEFAULT 0,
    response_bytes INTEGER NOT NULL DEFAULT 0,
    result_bytes INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started_at DESC);
"""

_COLUMNS = (
    "task_id, root_task_id, parent_task_id, caller, target, hop, status, "
    "error_code, truncated, http_sent, request_bytes, response_bytes, "
    "result_bytes, started_at, finished_at, duration_ms"
)


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

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "root_task_id": self.root_task_id,
                "parent_task_id": self.parent_task_id,
                "caller": self.caller,
                "target": self.target,
                "hop": self.hop,
                "status": self.status,
                "error_code": self.error_code,
                "truncated": self.truncated,
                "http_sent": self.http_sent,
                "request_bytes": self.request_bytes,
                "response_bytes": self.response_bytes,
                "result_bytes": self.result_bytes,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_ms": self.duration_ms,
                "observation": "local view; status is not proof of remote success",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


class Ledger:
    """Append-only observations. A write failure logs and never fails a delegation;
    rows are never updated once written."""

    def __init__(self, path: str | Path | None, *, read_only: bool = False) -> None:
        self._path = None if path is None else Path(path)
        self._conn: sqlite3.Connection | None = None
        if self._path is None:
            return
        if read_only:
            if not self._path.exists():
                return
            try:
                uri = self._path.resolve().as_uri() + "?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True)
            except sqlite3.Error:
                self._conn = None
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def record(self, entry: TaskRecord) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                f"INSERT INTO tasks ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.task_id,
                    entry.root_task_id,
                    entry.parent_task_id,
                    entry.caller,
                    entry.target,
                    entry.hop,
                    entry.status,
                    entry.error_code,
                    int(entry.truncated),
                    int(entry.http_sent),
                    entry.request_bytes,
                    entry.response_bytes,
                    entry.result_bytes,
                    entry.started_at,
                    entry.finished_at,
                    entry.duration_ms,
                ),
            )
        except sqlite3.Error as exc:
            _LOGGER.error("ledger write failed: %s", exc)
            return

    def list_tasks(self, limit: int = 50, target: str | None = None, status: str | None = None) -> list[TaskRecord]:
        if self._conn is None:
            return []
        query = f"SELECT {_COLUMNS} FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if target:
            clauses.append("target = ?")
            params.append(target)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        bounded = max(1, min(limit, 500))
        query += " ORDER BY started_at DESC, task_id DESC LIMIT ?"
        params.append(bounded)
        try:
            rows = self._conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        return [_row_to_record(row) for row in rows]

    def usage_for(self, caller: str) -> tuple[int, int, float] | None:
        """Usage totals for one caller, or None when the ledger is unreadable
        so budget checks can fail closed."""
        if self._conn is None:
            return (0, 0, 0.0)
        try:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(result_bytes), 0), "
                "COALESCE(SUM(duration_ms), 0) FROM tasks WHERE caller = ?",
                (caller,),
            ).fetchone()
        except sqlite3.Error:
            return None
        return (int(row[0]), int(row[1]), float(row[2]) / 1000.0)

    def get_task(self, task_id: str) -> TaskRecord | None:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return None if row is None else _row_to_record(row)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _row_to_record(row: tuple) -> TaskRecord:
    return TaskRecord(
        task_id=row[0],
        root_task_id=row[1],
        parent_task_id=row[2],
        caller=row[3],
        target=row[4],
        hop=row[5],
        status=row[6],
        error_code=row[7],
        truncated=bool(row[8]),
        http_sent=bool(row[9]),
        request_bytes=row[10],
        response_bytes=row[11],
        result_bytes=row[12],
        started_at=row[13],
        finished_at=row[14],
        duration_ms=row[15],
    )
