"""SQLite persistence for the coordinator: catalog, tasks, outbox, events, quarantine.

One exclusive file lock admits one writer process. Every state change and its
event commit in one ``BEGIN IMMEDIATE`` transaction; admission failure means the
HTTP worker never sees the task.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..file_lock import FileLock
from .models import (
    TERMINAL_RUN_STATES,
    Bot,
    CancelState,
    DeliveryMode,
    DeliveryState,
    DispatchEntry,
    DispatchState,
    Event,
    Project,
    Quarantine,
    Task,
    new_task_id,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS bots (
    bot_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    secret_env TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    instance_id TEXT,
    capabilities TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    caller_principal TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    input_text TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    delivery_mode TEXT NOT NULL,
    dispatch_state TEXT NOT NULL,
    remote_run_id TEXT,
    remote_instance_id TEXT,
    remote_revision INTEGER NOT NULL DEFAULT 0,
    run_state TEXT NOT NULL DEFAULT 'unobserved',
    stop_confirmed INTEGER NOT NULL DEFAULT 0,
    effects_uncertain INTEGER NOT NULL DEFAULT 0,
    outcome_code TEXT,
    outcome_message TEXT NOT NULL DEFAULT '',
    result_text TEXT NOT NULL DEFAULT '',
    result_truncated INTEGER NOT NULL DEFAULT 0,
    result_available INTEGER NOT NULL DEFAULT 0,
    result_fetched INTEGER NOT NULL DEFAULT 0,
    cancel_state TEXT NOT NULL DEFAULT 'none',
    delivery_state TEXT NOT NULL DEFAULT 'not_ready',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    reply_capability TEXT NOT NULL DEFAULT '',
    migrated_usage INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_operation_key
    ON tasks(caller_principal, operation_key);
CREATE INDEX IF NOT EXISTS tasks_workflow ON tasks(workflow_id);
CREATE INDEX IF NOT EXISTS tasks_bot_state ON tasks(bot_id, dispatch_state);
CREATE TABLE IF NOT EXISTS dispatch_outbox (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS dispatch_due ON dispatch_outbox(state, next_attempt_at);
CREATE TABLE IF NOT EXISTS delivery_outbox (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS remote_observations (
    obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_state TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    task_id TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS instance_quarantine (
    instance_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT NOT NULL DEFAULT '',
    resolution_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS migration_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    at TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caller_quotas (
    caller_principal TEXT PRIMARY KEY,
    delegations_used INTEGER NOT NULL DEFAULT 0,
    delegations_limit INTEGER NOT NULL DEFAULT 0,
    result_bytes_used INTEGER NOT NULL DEFAULT 0,
    result_bytes_limit INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    confirmed_by TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT
);
"""

TASK_COLUMNS = (
    "task_id, project_id, workflow_id, caller_principal, bot_id, operation_key, "
    "request_digest, input_text, deadline_at, delivery_mode, dispatch_state, remote_run_id, "
    "remote_instance_id, remote_revision, run_state, stop_confirmed, effects_uncertain, "
    "outcome_code, outcome_message, result_text, result_truncated, result_available, "
    "result_fetched, cancel_state, delivery_state, created_at, finished_at, last_error, "
    "reply_capability, migrated_usage"
)


class StoreError(RuntimeError):
    """The coordinator control state cannot be safely read or committed."""


class AdmissionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class IdempotencyConflictError(AdmissionError):
    def __init__(self, detail: str) -> None:
        super().__init__("operation_key_conflict", detail)


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def task_digest(
    *,
    caller_principal: str,
    project_id: str,
    bot_id: str,
    input_text: str,
    deadline_at: datetime | None,
    delivery_mode: str,
    workflow_id: str,
) -> str:
    body = json.dumps(
        {
            "principal": caller_principal,
            "project": project_id,
            "bot": bot_id,
            "input": input_text,
            "deadline": iso(deadline_at) if deadline_at is not None else "unset",
            "delivery": delivery_mode,
            "workflow": workflow_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


DEFAULT_QUOTAS = {
    "parallel_bots": 4,
    "workflow_tasks": 20,
    "principal_open": 32,
    "global_open": 128,
    "bot_queue": 16,
    "workflow_result_bytes": 640 * 1024,
}

# Every admitted-but-unsettled task reserves the run protocol's result cap; a
# task that has not returned its body yet still owns that quota headroom.
RESULT_RESERVATION_BYTES = 32 * 1024

# Dispatch states that may still produce remote work: pending was never sent,
# accepted has a live run, indeterminate means the remote state is unknown.
_UNKNOWN_DISPATCH_STATES = "('pending','accepted','indeterminate')"
_TERMINAL_RUN_STATES_SQL = "('succeeded','failed','cancelled','interrupted')"


class CoordinatorStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock: FileLock | None = None
        self._conn: sqlite3.Connection | None = None

    def open(self, *, lock_wait_s: float = 0.0) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = Path(str(self._path) + ".writer.lock")
            deadline = time.monotonic() + lock_wait_s
            while True:
                try:
                    self._lock = FileLock(lock_path)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
            self._conn = sqlite3.connect(self._path, isolation_level=None, timeout=0.5)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
            if "reply_capability" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN reply_capability TEXT NOT NULL DEFAULT ''"
                )
            if "result_fetched" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN result_fetched INTEGER NOT NULL DEFAULT 0"
                )
        except (OSError, sqlite3.Error, StoreError) as exc:
            self.close()
            raise StoreError("Cannot open the coordinator store (another writer?)") from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    @contextmanager
    def _transaction(self):
        if self._conn is None:
            raise StoreError("The coordinator store is not open.")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StoreError("Coordinator transaction failed.") from exc

    # ---- events ----

    def _emit(self, event_type: str, task_id: str | None, data: dict[str, str]) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO events(at, type, task_id, data) VALUES (?,?,?,?)",
            (iso(utc_now()), event_type, task_id, json.dumps(data, ensure_ascii=False)),
        )

    def events_after(self, after_seq: int, limit: int = 100) -> list[Event]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT seq, at, type, task_id, data FROM events WHERE seq>? ORDER BY seq LIMIT ?",
            (after_seq, limit),
        ).fetchall()
        return [
            Event(
                seq=int(r[0]),
                at=datetime.fromisoformat(r[1]),
                type=r[2],
                task_id=r[3],
                data=json.loads(r[4]),
            )
            for r in rows
        ]

    # ---- projects & bots ----

    def create_project(self, project_id: str, display_name: str) -> Project:
        with self._transaction():
            assert self._conn is not None
            if self._conn.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone():
                raise AdmissionError("project_exists", "That project id already exists.")
            now = utc_now()
            self._conn.execute(
                "INSERT INTO projects(project_id, display_name, created_at) VALUES (?,?,?)",
                (project_id, display_name, iso(now)),
            )
            self._emit("project_created", None, {"project": project_id})
            return Project(project_id, display_name, now)

    def list_projects(self) -> list[Project]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT project_id, display_name, created_at, archived_at FROM projects "
            "ORDER BY created_at"
        ).fetchall()
        return [
            Project(r[0], r[1], datetime.fromisoformat(r[2]),
                    None if r[3] is None else datetime.fromisoformat(r[3]))
            for r in rows
        ]

    def upsert_bot(self, bot: Bot) -> None:
        with self._transaction():
            assert self._conn is not None
            existing = self._conn.execute(
                "SELECT instance_id FROM bots WHERE bot_id=?", (bot.bot_id,)
            ).fetchone()
            if existing is not None and existing[0] and bot.instance_id not in (None, existing[0]):
                raise AdmissionError(
                    "instance_rebind",
                    "Rebinding a bot to a different instance requires operator action.",
                )
            now = iso(utc_now())
            self._conn.execute(
                "INSERT INTO bots(bot_id, display_name, role, endpoint, secret_env, enabled, "
                "instance_id, capabilities, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_id) DO UPDATE SET display_name=excluded.display_name, "
                "role=excluded.role, endpoint=excluded.endpoint, secret_env=excluded.secret_env, "
                "enabled=excluded.enabled, instance_id=COALESCE(bots.instance_id, "
                "excluded.instance_id), capabilities=excluded.capabilities, "
                "updated_at=excluded.updated_at",
                (
                    bot.bot_id, bot.display_name, bot.role, bot.endpoint, bot.secret_env,
                    int(bot.enabled), bot.instance_id,
                    json.dumps(list(bot.capabilities)), now, now,
                ),
            )
            self._emit("bot_upserted", None, {"bot": bot.bot_id})

    def bind_instance(self, bot_id: str, instance_id: str) -> bool:
        """Fix the instance identity; returns False when it conflicts."""
        with self._transaction():
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT instance_id FROM bots WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if row is None:
                raise AdmissionError("bot_not_found", "No bot with that id.")
            if row[0] is None:
                self._conn.execute(
                    "UPDATE bots SET instance_id=?, updated_at=? WHERE bot_id=?",
                    (instance_id, iso(utc_now()), bot_id),
                )
                self._emit("bot_instance_bound", None, {"bot": bot_id})
                return True
            return row[0] == instance_id

    def force_rebind_instance(
        self, bot_id: str, instance_id: str, *, actor: str, note: str
    ) -> None:
        """Operator-only explicit rebind after an identity change, with audit."""
        if not actor.strip() or not note.strip():
            raise AdmissionError("rebind_requires_reason", "Actor and note are required.")
        with self._transaction():
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT instance_id FROM bots WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if row is None:
                raise AdmissionError("bot_not_found", "No bot with that id.")
            old = row[0] or ""
            self._conn.execute(
                "UPDATE bots SET instance_id=?, updated_at=? WHERE bot_id=?",
                (instance_id, iso(utc_now()), bot_id),
            )
            self._emit(
                "bot_instance_rebound",
                None,
                {"bot": bot_id, "from": old, "to": instance_id, "actor": actor, "note": note},
            )

    def get_bot(self, bot_id: str) -> Bot | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT bot_id, display_name, role, endpoint, secret_env, enabled, instance_id, "
            "capabilities, created_at, updated_at, archived_at FROM bots WHERE bot_id=? "
            "AND archived_at IS NULL",
            (bot_id,),
        ).fetchone()
        if row is None:
            return None
        return Bot(
            bot_id=row[0], display_name=row[1], role=row[2], endpoint=row[3], secret_env=row[4],
            enabled=bool(row[5]), instance_id=row[6],
            capabilities=tuple(json.loads(row[7])), created_at=datetime.fromisoformat(row[8]),
            updated_at=datetime.fromisoformat(row[9]),
            archived_at=None if row[10] is None else datetime.fromisoformat(row[10]),
        )

    def list_bots(self) -> list[Bot]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT bot_id FROM bots WHERE archived_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [self.get_bot(r[0]) for r in rows if self.get_bot(r[0]) is not None]  # type: ignore[misc]

    def bot_has_open_tasks(self, bot_id: str) -> bool:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE bot_id=? AND dispatch_state IN ('pending','accepted') "
            "AND (run_state NOT IN ('succeeded','failed','cancelled','interrupted') "
            "OR run_state='unobserved') LIMIT 1",
            (bot_id,),
        ).fetchone()
        return row is not None

    # ---- quarantine ----

    def quarantine_instance(self, instance_id: str, reason: str) -> None:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO instance_quarantine(instance_id, reason, created_at) VALUES (?,?,?) "
                "ON CONFLICT(instance_id) DO UPDATE SET reason=excluded.reason",
                (instance_id, reason, iso(utc_now())),
            )
            self._emit("instance_quarantined", None, {"instance": instance_id, "reason": reason})

    def resolve_quarantine(self, instance_id: str, actor: str, note: str) -> None:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE instance_quarantine SET resolved_at=?, resolved_by=?, resolution_note=? "
                "WHERE instance_id=? AND resolved_at IS NULL",
                (iso(utc_now()), actor, note, instance_id),
            )
            self._emit("quarantine_resolved", None, {"instance": instance_id, "actor": actor})

    def active_quarantines(self) -> dict[str, Quarantine]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT instance_id, reason, created_at, resolved_at, resolved_by, resolution_note "
            "FROM instance_quarantine WHERE resolved_at IS NULL"
        ).fetchall()
        return {
            r[0]: Quarantine(
                instance_id=r[0], reason=r[1], created_at=datetime.fromisoformat(r[2]),
                resolved_at=None, resolved_by=r[4] if r[4] else "",
                resolution_note=r[5] if r[5] else "",
            )
            for r in rows
        }

    # ---- admission ----

    def quota_snapshot(
        self,
        *,
        workflow_id: str,
        caller_principal: str,
        bot_id: str,
        quotas: dict[str, int] | None = None,
    ) -> dict[str, int]:
        assert self._conn is not None
        limits = quotas or DEFAULT_QUOTAS
        workflow_tasks = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE workflow_id=?", (workflow_id,)
            ).fetchone()[0]
        )
        unknown_open = (
            f"dispatch_state IN {_UNKNOWN_DISPATCH_STATES} AND run_state NOT IN "
            f"{_TERMINAL_RUN_STATES_SQL}"
        )
        principal_open = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE caller_principal=? AND {unknown_open}",
                (caller_principal,),
            ).fetchone()[0]
        )
        global_open = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {unknown_open}"
            ).fetchone()[0]
        )
        # Queue quotas attach to the instance identity, not the bot row: alias
        # endpoints registered against the same instance share one queue.
        bot_row = self._conn.execute(
            "SELECT instance_id FROM bots WHERE bot_id=?", (bot_id,)
        ).fetchone()
        queue_key = (bot_row[0] if bot_row and bot_row[0] else bot_id) if bot_row else bot_id
        bot_queue = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM tasks JOIN bots ON bots.bot_id=tasks.bot_id "
                "WHERE COALESCE(bots.instance_id, bots.bot_id)=? AND "
                f"tasks.dispatch_state IN {_UNKNOWN_DISPATCH_STATES} AND tasks.run_state "
                f"NOT IN {_TERMINAL_RUN_STATES_SQL}",
                (queue_key,),
            ).fetchone()[0]
        )
        # Admission reserves the protocol result cap per unsettled task; only a
        # terminal task's actual stored body replaces its reservation.
        workflow_result_bytes = int(
            self._conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN dispatch_state IN "
                f"{_UNKNOWN_DISPATCH_STATES} AND run_state NOT IN {_TERMINAL_RUN_STATES_SQL} "
                "THEN ? ELSE LENGTH(CAST(result_text AS BLOB)) END),0) FROM tasks "
                "WHERE workflow_id=?",
                (RESULT_RESERVATION_BYTES, workflow_id),
            ).fetchone()[0]
        )
        return {
            "workflow_tasks": workflow_tasks,
            "principal_open": principal_open,
            "global_open": global_open,
            "bot_queue": bot_queue,
            "workflow_result_bytes": workflow_result_bytes,
            **{f"limit_{k}": v for k, v in limits.items()},
        }

    def admit_task(
        self,
        *,
        caller_principal: str,
        project_id: str,
        workflow_id: str,
        bot_id: str,
        operation_key: str,
        input_text: str,
        deadline_at: datetime | None,
        delivery_mode: DeliveryMode,
        bot: Bot,
        quotas: dict[str, int] | None = None,
        reply_capability: str = "",
    ) -> tuple[Task, bool]:
        """Validate, reserve, and persist one task plus its dispatch outbox entry.

        The idempotency digest covers only caller-provided semantics: a default
        deadline (when the caller omitted one) is fixed after the key check, so a
        stateless replay with the same arguments always reconciles.
        Returns (task, created). Raises AdmissionError with zero side effects.
        """
        limits = quotas or DEFAULT_QUOTAS
        digest = task_digest(
            caller_principal=caller_principal,
            project_id=project_id,
            bot_id=bot_id,
            input_text=input_text,
            deadline_at=deadline_at,
            delivery_mode=delivery_mode.value,
            workflow_id=workflow_id,
        )
        with self._transaction():
            assert self._conn is not None
            existing = self._conn.execute(
                "SELECT task_id, request_digest, deadline_at FROM tasks WHERE caller_principal=? AND "
                "operation_key=?",
                (caller_principal, operation_key),
            ).fetchone()
            if existing is not None:
                if existing[1] != digest:
                    raise IdempotencyConflictError(
                        "This operation key was already used with different semantics."
                    )
                return self._load_task(existing[0]), False
            effective_deadline = deadline_at or utc_now() + timedelta(minutes=30)
            if self._conn.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND archived_at IS NULL",
                (project_id,),
            ).fetchone() is None:
                raise AdmissionError("project_not_found", "No active project with that id.")
            if bot.bot_id != bot_id:
                raise AdmissionError("bot_unavailable", "Bot is missing or disabled.")
            live_bot = self.get_bot(bot_id)
            if live_bot is None or not live_bot.enabled:
                raise AdmissionError("bot_unavailable", "Bot is missing or disabled.")
            quarantines = self.active_quarantines()
            if bot.instance_id and bot.instance_id in quarantines:
                raise AdmissionError(
                    "bot_quarantined",
                    f"The bot instance is quarantined: {quarantines[bot.instance_id].reason}",
                )
            # Imported 0.4 usage is a real guard: a migrated caller cannot admit
            # new work until an operator confirms fresh quota for it.
            quota_row = self._conn.execute(
                "SELECT delegations_used, delegations_limit, imported_at, confirmed "
                "FROM caller_quotas WHERE caller_principal=?",
                (caller_principal,),
            ).fetchone()
            if quota_row is not None:
                if not quota_row[3]:
                    raise AdmissionError(
                        "migrated_quota_unconfirmed",
                        "Caller usage was imported from 0.4; an operator must confirm "
                        "fresh quota before new tasks are admitted.",
                    )
                admitted_since = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE caller_principal=? AND created_at>=?",
                        (caller_principal, quota_row[2]),
                    ).fetchone()[0]
                )
                if quota_row[0] + admitted_since >= quota_row[1]:
                    raise AdmissionError(
                        "migrated_quota_exhausted",
                        "The confirmed delegation budget for this caller is exhausted.",
                    )
            snapshot = self.quota_snapshot(
                workflow_id=workflow_id,
                caller_principal=caller_principal,
                bot_id=bot_id,
                quotas=limits,
            )
            if snapshot["workflow_tasks"] >= limits["workflow_tasks"]:
                raise AdmissionError(
                    "workflow_task_limit",
                    f"Workflow reached the {limits['workflow_tasks']}-task limit.",
                )
            if snapshot["principal_open"] >= limits["principal_open"]:
                raise AdmissionError(
                    "principal_open_limit",
                    f"Caller reached {limits['principal_open']} open tasks.",
                )
            if snapshot["global_open"] >= limits["global_open"]:
                raise AdmissionError("global_open_limit", "Too many open tasks globally.")
            if snapshot["bot_queue"] >= limits["bot_queue"]:
                raise AdmissionError(
                    "bot_queue_limit", f"Bot queue is full at {limits['bot_queue']}."
                )
            if snapshot["workflow_result_bytes"] >= limits["workflow_result_bytes"]:
                raise AdmissionError(
                    "workflow_result_limit",
                    "Workflow result reservation is exhausted.",
                )
            task_id = new_task_id()
            now = utc_now()
            self._conn.execute(
                f"INSERT INTO tasks ({TASK_COLUMNS}) VALUES "
                f"({','.join('?' for _ in TASK_COLUMNS.split(','))})",
                (
                    task_id, project_id, workflow_id, caller_principal, bot_id, operation_key,
                    digest, input_text, iso(effective_deadline), delivery_mode.value,
                    DispatchState.PENDING.value, None, None, 0, "unobserved", 0, 0, None, "",
                    "", 0, 0, 0, CancelState.NONE.value, DeliveryState.NOT_READY.value,
                    iso(now), None, "", reply_capability, 0,
                ),
            )
            self._conn.execute(
                "INSERT INTO dispatch_outbox(task_id, state, attempts, next_attempt_at) "
                "VALUES (?, 'pending', 0, ?)",
                (task_id, iso(now)),
            )
            self._emit(
                "task_created", task_id, {"bot": bot_id, "workflow": workflow_id}
            )
            return self._load_task(task_id), True

    # ---- task lifecycle ----

    def get_task(self, task_id: str) -> Task | None:
        return self._load_task(task_id, none_on_missing=True)

    def lookup_task(self, caller_principal: str, operation_key: str) -> Task | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT task_id FROM tasks WHERE caller_principal=? AND operation_key=?",
            (caller_principal, operation_key),
        ).fetchone()
        return None if row is None else self._load_task(row[0])

    def list_tasks(
        self,
        *,
        caller_principal: str | None = None,
        project_id: str | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
    ) -> list[Task]:
        assert self._conn is not None
        clauses, params = [], []
        if caller_principal:
            clauses.append("caller_principal=?")
            params.append(caller_principal)
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if workflow_id:
            clauses.append("workflow_id=?")
            params.append(workflow_id)
        query = "SELECT task_id FROM tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))
        rows = self._conn.execute(query, params).fetchall()
        return [self._load_task(r[0]) for r in rows]

    def _load_task(self, task_id: str, none_on_missing: bool = False) -> Task | None:
        assert self._conn is not None
        row = self._conn.execute(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            if none_on_missing:
                return None
            raise StoreError("No task with that id.")
        pairs = dict(zip(TASK_COLUMNS.split(", "), row, strict=True))
        return _row_to_task(pairs)

    def update_after_submit(
        self,
        task_id: str,
        *,
        remote_run_id: str,
        remote_instance_id: str,
    ) -> Task:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE tasks SET dispatch_state=?, remote_run_id=?, remote_instance_id=? "
                "WHERE task_id=?",
                (DispatchState.ACCEPTED.value, remote_run_id, remote_instance_id, task_id),
            )
            self._conn.execute(
                "UPDATE dispatch_outbox SET state='confirmed' WHERE task_id=?", (task_id,)
            )
            self._emit("task_accepted", task_id, {"run": remote_run_id})
            return self._load_task(task_id)

    def mark_dispatch_failure(
        self, task_id: str, *, error: str, retry_at: datetime | None = None, attempts: int = 0
    ) -> None:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE dispatch_outbox SET last_error=?, next_attempt_at=COALESCE(?, "
                "next_attempt_at), attempts=? WHERE task_id=? AND state='pending'",
                (error, iso(retry_at) if retry_at else None, attempts, task_id),
            )

    def record_observation(
        self,
        task_id: str,
        *,
        revision: int,
        run_state: str,
        stop_confirmed: bool | None = None,
        effects_uncertain: bool | None = None,
        outcome_code: str | None = None,
        outcome_message: str | None = None,
        result_text: str | None = None,
        result_truncated: bool | None = None,
        result_advertised: bool = False,
    ) -> Task | None:
        """Apply one remote observation; only newer revisions are recorded.

        ``result_available`` records that the remote advertises a terminal
        result body; ``result_fetched`` only settles once that body is
        persisted here, so a failed fetch keeps the task in the fetch queue.
        """
        with self._transaction():
            assert self._conn is not None
            current = self._load_task(task_id, none_on_missing=True)
            if current is None:
                return None
            if current.remote_revision >= revision:
                return current
            now = iso(utc_now())
            terminal = run_state in ("succeeded", "failed", "cancelled", "interrupted")
            assignments = ["remote_revision=?", "run_state=?"]
            params: list[object] = [revision, run_state]
            if stop_confirmed is not None:
                assignments.append("stop_confirmed=?")
                params.append(int(stop_confirmed))
            if effects_uncertain is not None:
                assignments.append("effects_uncertain=?")
                params.append(int(effects_uncertain))
            if outcome_code is not None:
                assignments.append("outcome_code=?")
                params.append(outcome_code)
            if outcome_message is not None:
                assignments.append("outcome_message=?")
                params.append(outcome_message)
            if result_text is not None:
                body = result_text.encode("utf-8")
                if len(body) > RESULT_RESERVATION_BYTES:
                    # Same bound as every other result surface; an oversized
                    # remote body keeps its prefix and the truncation marker.
                    result_text = body[:RESULT_RESERVATION_BYTES].decode(
                        "utf-8", errors="ignore"
                    )
                    result_truncated = True
                assignments.extend(["result_text=?", "result_available=?", "result_fetched=?"])
                params.extend([result_text, 1, 1])
            elif terminal and result_advertised:
                assignments.extend(["result_available=?", "result_fetched=?"])
                params.extend([1, 0])
            if result_truncated is not None:
                assignments.append("result_truncated=?")
                params.append(int(result_truncated))
            if terminal:
                new_cancel = current.cancel_state
                if current.cancel_state in (
                    CancelState.REQUESTED,
                    CancelState.UNCONFIRMED,
                ):
                    new_cancel = (
                        CancelState.CONFIRMED
                        if stop_confirmed
                        else CancelState.UNCONFIRMED
                    )
                assignments.append("cancel_state=?")
                params.append(new_cancel.value)
                assignments.append("finished_at=?")
                params.append(now)
            params.append(task_id)
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id=?", params
            )
            self._conn.execute(
                "INSERT INTO remote_observations(task_id, revision, run_state, observed_at) "
                "VALUES (?,?,?,?)",
                (task_id, revision, run_state, now),
            )
            event = "task_run_terminal" if terminal else "task_run_observed"
            self._emit(event, task_id, {"run_state": run_state, "revision": str(revision)})
            return self._load_task(task_id)

    def dispatch_entry_for_task(self, task_id: str) -> DispatchEntry | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT entry_id, task_id, state, attempts, next_attempt_at, last_error FROM "
            "dispatch_outbox WHERE task_id=? ORDER BY entry_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return DispatchEntry(
            entry_id=int(row[0]), task_id=row[1], state=row[2], attempts=int(row[3]),
            next_attempt_at=datetime.fromisoformat(row[4]), last_error=row[5],
        )

    def request_cancel(self, task_id: str) -> Task | None:
        with self._transaction():
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT dispatch_state, cancel_state, run_state FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            dispatch, cancel, run_state = row
            if cancel != CancelState.NONE.value:
                return self._load_task(task_id)
            if run_state in TERMINAL_RUN_STATES:
                # Terminal observation already settles the cancel; only the
                # state name is unified here.
                self._conn.execute(
                    "UPDATE tasks SET cancel_state=? WHERE task_id=?",
                    (CancelState.CONFIRMED.value, task_id),
                )
                self._emit("task_cancel_requested", task_id, {})
                return self._load_task(task_id)
            if dispatch == DispatchState.ACCEPTED.value:
                new_cancel = CancelState.REQUESTED.value
            elif dispatch == DispatchState.REJECTED.value:
                # Deterministic refusal: the remote never created work.
                new_cancel = CancelState.CONFIRMED.value
                self._conn.execute(
                    "UPDATE dispatch_outbox SET state='failed', last_error=? "
                    "WHERE task_id=? AND state='pending'",
                    ("cancelled before dispatch", task_id),
                )
            elif dispatch == DispatchState.INDETERMINATE.value:
                # The remote state is unknown; a submit may or may not exist.
                # Reconciliation decides; nothing new may be submitted.
                new_cancel = CancelState.UNCONFIRMED.value
            else:
                entry = self._conn.execute(
                    "SELECT attempts FROM dispatch_outbox WHERE task_id=? AND "
                    "state='pending' ORDER BY entry_id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if entry is not None and int(entry[0]) == 0:
                    # Definitely never sent: close the task and its outbox in
                    # this same transaction instead of confirming a phantom.
                    self._conn.execute(
                        "UPDATE tasks SET cancel_state=?, run_state='cancelled', "
                        "outcome_code='cancelled', outcome_message='Cancelled before "
                        "dispatch.', stop_confirmed=1, finished_at=? WHERE task_id=?",
                        (
                            CancelState.CONFIRMED.value,
                            iso(utc_now()),
                            task_id,
                        ),
                    )
                    self._conn.execute(
                        "UPDATE dispatch_outbox SET state='failed', last_error=? "
                        "WHERE task_id=? AND state='pending'",
                        ("cancelled before dispatch", task_id),
                    )
                    self._emit("task_cancelled_before_dispatch", task_id, {})
                    return self._load_task(task_id)
                # Attempts > 0: the submit may have reached the remote; keep the
                # unknown until the dispatch worker reconciles by the key.
                new_cancel = CancelState.UNCONFIRMED.value
            self._conn.execute(
                "UPDATE tasks SET cancel_state=? WHERE task_id=?", (new_cancel, task_id)
            )
            self._emit("task_cancel_requested", task_id, {})
            return self._load_task(task_id)

    def expire_task_before_dispatch(self, task_id: str, reason: str) -> Task:
        """Atomically end a task whose deadline passed before its first dispatch."""
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE tasks SET dispatch_state='rejected', run_state='failed', "
                "outcome_code='deadline_before_start', outcome_message=?, finished_at=? "
                "WHERE task_id=? AND dispatch_state='pending'",
                (reason, iso(utc_now()), task_id),
            )
            self._conn.execute(
                "UPDATE dispatch_outbox SET state='failed', last_error=? WHERE task_id=? "
                "AND state='pending'",
                (reason, task_id),
            )
            self._emit("task_expired_before_dispatch", task_id, {"reason": reason})
            return self._load_task(task_id)

    def close_cancelled_unknown(self, task_id: str, reason: str) -> Task | None:
        """Finish a never-created task whose unconfirmed cancel reconciled to nothing.

        The dispatch worker calls this after a lookup by the original key proves
        no remote run exists: no work was ever created, so the cancel confirms.
        """
        with self._transaction():
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT dispatch_state, run_state, remote_run_id FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if row[0] != DispatchState.PENDING.value or row[2] is not None:
                # An accepted or bound task owns remote work; cancel stays
                # unconfirmed and the normal cancel path handles it.
                return self._load_task(task_id)
            self._conn.execute(
                "UPDATE tasks SET cancel_state=?, run_state='cancelled', "
                "outcome_code='cancelled', outcome_message=?, stop_confirmed=1, "
                "dispatch_state='rejected', finished_at=? WHERE task_id=?",
                (CancelState.CONFIRMED.value, reason, iso(utc_now()), task_id),
            )
            self._conn.execute(
                "UPDATE dispatch_outbox SET state='failed', last_error=? WHERE task_id=? "
                "AND state='pending'",
                (reason, task_id),
            )
            self._emit("task_cancelled_before_dispatch", task_id, {})
            return self._load_task(task_id)

    def defer_dispatch(self, entry_id: int, next_at: datetime, error: str) -> None:
        """Push one outbox entry back without counting an attempt."""
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE dispatch_outbox SET next_attempt_at=?, last_error=? WHERE entry_id=?",
                (iso(next_at), error, entry_id),
            )

    def mark_indeterminate(
        self, task_id: str, reason: str, *, quarantine_instance: str | None = None
    ) -> Task:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE tasks SET dispatch_state=?, last_error=? WHERE task_id=?",
                (DispatchState.INDETERMINATE.value, reason, task_id),
            )
            # An indeterminate task never dispatches again; the operator owns
            # any manual retry decision.
            self._conn.execute(
                "UPDATE dispatch_outbox SET state='failed', last_error=? WHERE task_id=? "
                "AND state='pending'",
                (reason, task_id),
            )
            if quarantine_instance:
                self._conn.execute(
                    "INSERT INTO instance_quarantine(instance_id, reason, created_at) "
                    "VALUES (?,?,?) ON CONFLICT(instance_id) DO UPDATE SET "
                    "reason=excluded.reason",
                    (quarantine_instance, reason, iso(utc_now())),
                )
                self._emit(
                    "instance_quarantined", None,
                    {"instance": quarantine_instance, "reason": reason},
                )
            self._emit("task_indeterminate", task_id, {"reason": reason})
            return self._load_task(task_id)

    def auto_resume_ready_tasks(self) -> list[Task]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT task_id FROM tasks WHERE delivery_mode='auto_resume' AND "
            "run_state IN ('succeeded','failed','cancelled','interrupted') AND "
            "delivery_state IN ('not_ready','pending','persisted') AND "
            # An advertised-but-unfetched body must never be delivered as an
            # empty success report (F07): the fetch queue settles it first.
            "NOT (result_available=1 AND result_fetched=0)"
        ).fetchall()
        return [self._load_task(r[0]) for r in rows]

    def persist_result_body(
        self, task_id: str, *, result_text: str, result_truncated: bool
    ) -> None:
        """Settle one result fetch: persist the body after the terminal state.

        The stored body is bounded like every other result surface (the inbox
        delivery path uses the same cap); an oversized remote body keeps its
        bounded prefix and the truncation marker, so the artifact never
        silently claims completeness.
        """
        body = result_text.encode("utf-8")
        if len(body) > RESULT_RESERVATION_BYTES:
            body = body[:RESULT_RESERVATION_BYTES]
            result_text = body.decode("utf-8", errors="ignore")
            result_truncated = True
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE tasks SET result_text=?, result_truncated=?, result_available=1, "
                "result_fetched=1 WHERE task_id=?",
                (result_text, int(result_truncated), task_id),
            )
            self._emit("task_result_persisted", task_id, {})

    def set_delivery_state(self, task_id: str, state: DeliveryState) -> None:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE tasks SET delivery_state=? WHERE task_id=?", (state.value, task_id)
            )
            self._emit(f"delivery_{state.value}", task_id, {})

    def pending_dispatch(self, now: datetime, limit: int = 10) -> list[DispatchEntry]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT entry_id, task_id, state, attempts, next_attempt_at, last_error FROM "
            "dispatch_outbox WHERE state='pending' AND next_attempt_at<=? ORDER BY entry_id "
            "LIMIT ?",
            (iso(now), limit),
        ).fetchall()
        return [
            DispatchEntry(
                entry_id=int(r[0]), task_id=r[1], state=r[2], attempts=int(r[3]),
                next_attempt_at=datetime.fromisoformat(r[4]), last_error=r[5],
            )
            for r in rows
        ]

    def bump_dispatch_attempt(self, entry_id: int, next_at: datetime, error: str) -> None:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "UPDATE dispatch_outbox SET attempts=attempts+1, next_attempt_at=?, last_error=? "
                "WHERE entry_id=?",
                (iso(next_at), error, entry_id),
            )

    def tasks_to_observe(self, limit: int = 20) -> list[Task]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT task_id FROM tasks WHERE dispatch_state='accepted' AND run_state NOT IN "
            "('succeeded','failed','cancelled','interrupted') ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._load_task(r[0]) for r in rows]

    def tasks_with_unfetched_results(self, limit: int = 20) -> list[Task]:
        """Terminal tasks whose advertised result body was never persisted.

        A result read is not settled until the body lands in the control store;
        these tasks keep being polled no matter their run state.
        """
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT task_id FROM tasks WHERE dispatch_state='accepted' AND "
            "run_state IN ('succeeded','failed','cancelled','interrupted') AND "
            "result_available=1 AND result_fetched=0 ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._load_task(r[0]) for r in rows]

    def record_migration(self, source: str, fingerprint: str, details: dict) -> None:
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO migration_records(source, fingerprint, at, details) VALUES (?,?,?,?)",
                (source, fingerprint, iso(utc_now()), json.dumps(details, ensure_ascii=False)),
            )
            self._emit("migration_recorded", None, {"source": source})

    def migration_fingerprints(self, source: str) -> set[str]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT fingerprint FROM migration_records WHERE source=?", (source,)
        ).fetchall()
        return {r[0] for r in rows}

    def record_caller_usage(
        self, caller_principal: str, *, delegations_used: int, result_bytes_used: int
    ) -> None:
        """Persist imported 0.4 usage as an unconfirmed quota guard for the caller."""
        with self._transaction():
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO caller_quotas(caller_principal, delegations_used, "
                "result_bytes_used, imported_at, confirmed) VALUES (?,?,?,?,0) "
                "ON CONFLICT(caller_principal) DO UPDATE SET delegations_used=excluded."
                "delegations_used, result_bytes_used=excluded.result_bytes_used, "
                "confirmed=0, confirmed_by='', confirmed_at=NULL",
                (
                    caller_principal,
                    delegations_used,
                    result_bytes_used,
                    iso(utc_now()),
                ),
            )
            self._emit("caller_usage_imported", None, {"caller": caller_principal})

    def confirm_caller_quota(
        self,
        caller_principal: str,
        *,
        delegations_limit: int,
        result_bytes_limit: int,
        actor: str,
    ) -> bool:
        """Operator confirmation that unlocks new admissions for a migrated caller.

        Returns False when the caller has no imported usage record. Confirmation
        sets the fresh limits; it does not erase the imported history.
        """
        if not actor.strip():
            raise AdmissionError("confirm_requires_actor", "An actor is required.")
        with self._transaction():
            assert self._conn is not None
            cursor = self._conn.execute(
                "UPDATE caller_quotas SET confirmed=1, delegations_limit=?, "
                "result_bytes_limit=?, confirmed_by=?, confirmed_at=? "
                "WHERE caller_principal=?",
                (
                    delegations_limit,
                    result_bytes_limit,
                    actor,
                    iso(utc_now()),
                    caller_principal,
                ),
            )
            if cursor.rowcount == 0:
                return False
            self._emit("caller_quota_confirmed", None, {"caller": caller_principal})
            return True

    def caller_quota(self, caller_principal: str) -> dict | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT caller_principal, delegations_used, delegations_limit, "
            "result_bytes_used, result_bytes_limit, imported_at, confirmed, "
            "confirmed_by, confirmed_at FROM caller_quotas WHERE caller_principal=?",
            (caller_principal,),
        ).fetchone()
        if row is None:
            return None
        return {
            "caller_principal": row[0],
            "delegations_used": int(row[1]),
            "delegations_limit": int(row[2]),
            "result_bytes_used": int(row[3]),
            "result_bytes_limit": int(row[4]),
            "imported_at": row[5],
            "confirmed": bool(row[6]),
            "confirmed_by": row[7],
            "confirmed_at": row[8],
        }


def _row_to_task(values: dict) -> Task:
    return Task(
        task_id=values["task_id"],
        project_id=values["project_id"],
        workflow_id=values["workflow_id"],
        caller_principal=values["caller_principal"],
        bot_id=values["bot_id"],
        operation_key=values["operation_key"],
        request_digest=values["request_digest"],
        input_text=values["input_text"],
        deadline_at=datetime.fromisoformat(values["deadline_at"]),
        delivery_mode=DeliveryMode(values["delivery_mode"]),
        dispatch_state=DispatchState(values["dispatch_state"]),
        created_at=datetime.fromisoformat(values["created_at"]),
        remote_run_id=values["remote_run_id"],
        remote_instance_id=values["remote_instance_id"],
        remote_revision=int(values["remote_revision"]),
        run_state=values["run_state"],
        stop_confirmed=bool(values["stop_confirmed"]),
        effects_uncertain=bool(values["effects_uncertain"]),
        outcome_code=values["outcome_code"],
        outcome_message=values["outcome_message"] or "",
        result_text=values["result_text"] or "",
        result_truncated=bool(values["result_truncated"]),
        result_available=bool(values["result_available"]),
        result_fetched=bool(values.get("result_fetched")),
        cancel_state=CancelState(values["cancel_state"]),
        delivery_state=DeliveryState(values["delivery_state"]),
        finished_at=None if values["finished_at"] is None
        else datetime.fromisoformat(values["finished_at"]),
        last_error=values["last_error"] or "",
        reply_capability=values.get("reply_capability") or "",
    )
