"""SQLite persistence for agent runs: idempotent admission, transitions, events, recovery.

One store file belongs to one Nanobot instance (one writer process). A process
exclusive writer lock on the control directory is part of :meth:`RunStore.open`,
so a second service fails before it can read, recover, or mutate anything. Every
state change and its event commit in a single ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .types import (
    EVENT_MAX_BYTES,
    EVENT_RETENTION_DAYS,
    METADATA_RETENTION_DAYS,
    RESULT_RETENTION_DAYS,
    IdempotencyConflictError,
    OutcomeCode,
    Run,
    RunEvent,
    RunOutcome,
    RunState,
    RunSubmit,
    StoreUnavailableError,
    outcome_allowed_for_state,
    rfc3339,
    utc_now,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instance_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    input_text TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    stop_confirmed INTEGER NOT NULL DEFAULT 0,
    effects_uncertain INTEGER NOT NULL DEFAULT 0,
    outcome_code TEXT,
    outcome_message TEXT,
    outcome_partial TEXT,
    outcome_truncated INTEGER NOT NULL DEFAULT 0,
    outcome_partial_bytes INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at TEXT,
    cancel_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_cleaned_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_operation_key
    ON runs(instance_id, principal_id, operation_key);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state, created_at);
CREATE TABLE IF NOT EXISTS run_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_events_instance_seq ON run_events(instance_id, seq);
CREATE TABLE IF NOT EXISTS operation_tombstones (
    instance_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    tombstoned_at TEXT NOT NULL,
    PRIMARY KEY (instance_id, principal_id, operation_key)
);
"""


def canonical_request_digest(submit: RunSubmit) -> str:
    """Digest of the caller-provided semantics; a defaulted deadline is unset
    here so a replay of the identical wire body reconciles (the concrete
    deadline is fixed after the key check, as on the coordinator side)."""
    body = json.dumps(
        {
            "protocol": 1,
            "task_id": submit.task_id,
            "workflow_id": submit.workflow_id,
            "input_text": submit.input_text,
            "deadline_at": rfc3339(submit.deadline_at) if submit.deadline_at is not None else "unset",
            "principal_id": submit.principal_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class OperationKeyRetiredError(StoreUnavailableError):
    """The operation key was cleaned up; the same key can never run again."""

    code = "operation_key_retired"
    terminal_state: str = ""

    def __init__(self, detail: str, terminal_state: str) -> None:
        super().__init__(detail)
        self.terminal_state = terminal_state


class StateTransitionError(StoreUnavailableError):
    code = "transition_conflict"


class _WriterLock:
    """Process exclusive ownership of one control database (non-blocking OS lock)."""

    def __init__(self, path: Path) -> None:
        self._fd: int | None = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(self._fd).st_size == 0:
                os.write(self._fd, b"0")
            os.lseek(self._fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    def __del__(self) -> None:
        self.close()


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock: _WriterLock | None = None

    def open(self, *, lock_wait_s: float = 0.0) -> None:
        """Acquire the writer lock before touching any state.

        A second writer fails here, before it can read runs, recover crashes, or
        mutate anything. ``lock_wait_s`` bounds the wait for a hard-killed
        predecessor whose OS lock may clear a moment after death.
        """
        lock_path = Path(str(self._path) + ".writer.lock")
        deadline = time.monotonic() + lock_wait_s
        while True:
            try:
                self._lock = _WriterLock(lock_path)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreUnavailableError(
                        "Another writer process holds the runs control store."
                    ) from None
                time.sleep(0.25)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, isolation_level=None, timeout=5.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise StoreUnavailableError("Cannot open the runs control database.") from exc

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
            raise StoreUnavailableError("The runs store is not open.")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StoreUnavailableError("Runs store transaction failed.") from exc

    def instance_id(self) -> str:
        assert self._conn is not None
        with self._transaction():
            row = self._conn.execute(
                "SELECT value FROM instance_meta WHERE key='instance_id'"
            ).fetchone()
            if row is not None:
                return row[0]
            created = f"inst_{secrets.token_hex(16)}"
            self._conn.execute(
                "INSERT INTO instance_meta(key, value) VALUES ('instance_id', ?)",
                (created,),
            )
            return created

    def isolation(self) -> tuple[bool, str]:
        assert self._conn is not None
        try:
            row = self._conn.execute(
                "SELECT value FROM instance_meta WHERE key='isolation'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError("Cannot read instance isolation state.") from exc
        if row is None:
            return False, ""
        payload = json.loads(row[0])
        return bool(payload.get("isolated")), str(payload.get("reason", ""))

    def set_isolation(self, reason: str) -> None:
        self._write_meta(
            "isolation",
            json.dumps({"isolated": True, "reason": reason, "at": rfc3339(utc_now())}),
        )

    def clear_isolation(self, actor: str, reason: str) -> None:
        self._write_meta(
            "isolation",
            json.dumps(
                {
                    "isolated": False,
                    "reason": reason,
                    "actor": actor,
                    "at": rfc3339(utc_now()),
                }
            ),
        )

    def _write_meta(self, key: str, value: str) -> None:
        assert self._conn is not None
        with self._transaction():
            self._write_meta_locked(key, value)

    def _write_meta_locked(self, key: str, value: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO instance_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def submit(
        self,
        submit: RunSubmit,
        *,
        instance_id: str,
        run_id: str,
        digest: str,
        now: datetime | None = None,
    ) -> tuple[Run, bool]:
        assert self._conn is not None
        moment = rfc3339(now or utc_now())
        with self._transaction():
            row = self._conn.execute(
                "SELECT run_id, request_digest FROM runs WHERE instance_id=? AND principal_id=? "
                "AND operation_key=?",
                (instance_id, submit.principal_id, submit.operation_key),
            ).fetchone()
            if row is not None:
                if row[1] != digest:
                    raise IdempotencyConflictError(
                        "This operation key was submitted with a different request body."
                    )
                return self._load_run(row[0]), False
            tombstone = self._conn.execute(
                "SELECT terminal_state FROM operation_tombstones "
                "WHERE instance_id=? AND principal_id=? AND operation_key=?",
                (instance_id, submit.principal_id, submit.operation_key),
            ).fetchone()
            if tombstone is not None:
                raise OperationKeyRetiredError(
                    "This operation key was retired after retention cleanup.",
                    tombstone[0],
                )
            self._conn.execute(
                "INSERT INTO runs (run_id, instance_id, principal_id, operation_key, task_id, "
                "workflow_id, input_text, request_digest, deadline_at, state, revision, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                (
                    run_id,
                    instance_id,
                    submit.principal_id,
                    submit.operation_key,
                    submit.task_id,
                    submit.workflow_id,
                    submit.input_text,
                    digest,
                    rfc3339(submit.deadline_at or utc_now()),
                    RunState.QUEUED.value,
                    moment,
                ),
            )
            self._append_event(instance_id, run_id, "run_created", moment, {})
            return self._load_run(run_id), True

    def get(self, run_id: str) -> Run | None:
        assert self._conn is not None
        try:
            row = self._conn.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError("Cannot read run.") from exc
        if row is None:
            return None
        return self._load_run(run_id)

    def lookup(self, principal_id: str, operation_key: str) -> Run | None:
        assert self._conn is not None
        try:
            row = self._conn.execute(
                "SELECT run_id FROM runs WHERE instance_id=? AND principal_id=? AND operation_key=?",
                (self.instance_id_ro(), principal_id, operation_key),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError("Cannot look up the operation key.") from exc
        return None if row is None else self._load_run(row[0])

    def instance_id_ro(self) -> str:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT value FROM instance_meta WHERE key='instance_id'"
        ).fetchone()
        if row is None:
            raise StoreUnavailableError("Instance identity is not initialized.")
        return row[0]

    def transition(
        self,
        run_id: str,
        expected: RunState,
        new_state: RunState,
        *,
        outcome: RunOutcome | None = None,
        phase: str | None = None,
        stop_confirmed: bool | None = None,
        effects_uncertain: bool | None = None,
        cancel_requested: bool = False,
        cancel_reason: str | None = None,
        event_type: str | None = None,
        event_data: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> Run:
        assert self._conn is not None
        moment = now or utc_now()
        with self._transaction():
            row = self._conn.execute(
                "SELECT state, revision FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateTransitionError("No run with that id.")
            current = RunState(row[0])
            if current is not expected:
                raise StateTransitionError(
                    f"Expected state {expected.value} but run is {current.value}."
                )
            if new_state.terminal and outcome is not None and not outcome_allowed_for_state(
                new_state, outcome.code
            ):
                raise StateTransitionError(
                    f"Outcome {outcome.code.value} is not valid for state {new_state.value}."
                )
            revision = int(row[1]) + 1
            assignments = ["state=?", "revision=?"]
            params: list[object] = [new_state.value, revision]
            if outcome is not None:
                assignments.extend(
                    [
                        "outcome_code=?",
                        "outcome_message=?",
                        "outcome_partial=?",
                        "outcome_truncated=?",
                        "outcome_partial_bytes=?",
                    ]
                )
                params.extend(
                    [
                        outcome.code.value,
                        outcome.message,
                        outcome.partial,
                        int(outcome.truncated),
                        outcome.partial_bytes,
                    ]
                )
            if phase is not None:
                assignments.append("phase=?")
                params.append(phase)
            if stop_confirmed is not None:
                assignments.append("stop_confirmed=?")
                params.append(int(stop_confirmed))
            if effects_uncertain is not None:
                assignments.append("effects_uncertain=?")
                params.append(int(effects_uncertain))
            if cancel_requested:
                assignments.append("cancel_requested_at=?")
                params.append(rfc3339(moment))
            if cancel_reason is not None:
                assignments.append("cancel_reason=?")
                params.append(cancel_reason)
            if new_state is RunState.RUNNING:
                assignments.append("started_at=?")
                params.append(rfc3339(moment))
            if new_state.terminal:
                assignments.append("finished_at=?")
                params.append(rfc3339(moment))
            params.append(run_id)
            self._conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE run_id=?", params
            )
            event = event_type or f"run_{new_state.value}"
            self._append_event(
                self.instance_id_ro(),
                run_id,
                event,
                rfc3339(moment),
                event_data or {},
            )
            return self._load_run(run_id)

    def record_phase(self, run_id: str, phase: str) -> Run:
        assert self._conn is not None
        moment = utc_now()
        with self._transaction():
            row = self._conn.execute(
                "SELECT revision FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateTransitionError("No run with that id.")
            revision = int(row[0]) + 1
            self._conn.execute(
                "UPDATE runs SET phase=?, revision=? WHERE run_id=?",
                (phase, revision, run_id),
            )
            self._append_event(
                self.instance_id_ro(), run_id, "run_phase", rfc3339(moment), {"phase": phase}
            )
            return self._load_run(run_id)

    def _append_event(
        self, instance_id: str, run_id: str, event_type: str, at: str, data: dict[str, str]
    ) -> int:
        assert self._conn is not None
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > EVENT_MAX_BYTES:
            raise StoreUnavailableError("Event payload exceeds the protocol limit.")
        cursor = self._conn.execute(
            "INSERT INTO run_events(instance_id, run_id, type, at, data) VALUES (?,?,?,?,?)",
            (instance_id, run_id, event_type, at, payload),
        )
        return int(cursor.lastrowid or 0)

    def append_event(self, run_id: str, event_type: str, data: dict[str, str]) -> int:
        assert self._conn is not None
        with self._transaction():
            return self._append_event(
                self.instance_id_ro(), run_id, event_type, rfc3339(utc_now()), data
            )

    def events_after(self, after_seq: int, limit: int = 100) -> list[RunEvent]:
        assert self._conn is not None
        try:
            rows = self._conn.execute(
                "SELECT seq, instance_id, run_id, type, at, data FROM run_events "
                "WHERE instance_id=? AND seq>? ORDER BY seq LIMIT ?",
                (self.instance_id_ro(), after_seq, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError("Cannot read run events.") from exc
        return [
            RunEvent(
                seq=int(row[0]),
                instance_id=row[1],
                run_id=row[2],
                type=row[3],
                at=datetime.fromisoformat(row[4]),
                data=json.loads(row[5]),
            )
            for row in rows
        ]

    def events_floor(self) -> int | None:
        """Lowest retained event seq, or None while no cleanup has happened."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT value FROM instance_meta WHERE key='events_floor'"
        ).fetchone()
        return None if row is None else int(row[0])

    def recover(self, now: datetime | None = None) -> list[Run]:
        """Mark crashed active runs as interrupted; expired queued runs fail.

        Interrupting active runs means their background effects are unverified,
        so the instance isolates itself in the same transaction: no queued work
        may start until an operator clears the isolation.
        """
        moment = now or utc_now()
        changed: list[Run] = []
        with self._transaction():
            assert self._conn is not None
            instance = self.instance_id_ro()
            active = self._conn.execute(
                "SELECT run_id FROM runs WHERE state IN ('running', 'cancelling')"
            ).fetchall()
            for (run_id,) in active:
                self._conn.execute(
                    "UPDATE runs SET state=?, revision=revision+1, finished_at=?, "
                    "outcome_code=?, outcome_message=?, effects_uncertain=1 WHERE run_id=?",
                    (
                        RunState.INTERRUPTED.value,
                        rfc3339(moment),
                        "interrupted",
                        "The process restarted while the run was active.",
                        run_id,
                    ),
                )
                self._append_event(
                    instance, run_id, "run_interrupted", rfc3339(moment), {}
                )
                changed.append(self._load_run(run_id))
            if changed:
                self._write_meta_locked(
                    "isolation",
                    json.dumps(
                        {
                            "isolated": True,
                            "reason": (
                                f"Restart interrupted {len(changed)} active run(s); "
                                "their background effects are unverified."
                            ),
                            "at": rfc3339(moment),
                        }
                    ),
                )
            self._expire_queued_locked(moment)
        return changed

    def expire_queued(self, now: datetime | None = None) -> list[Run]:
        moment = now or utc_now()
        with self._transaction():
            return self._expire_queued_locked(moment)

    def _expire_queued_locked(self, moment: datetime) -> list[Run]:
        assert self._conn is not None
        instance = self.instance_id_ro()
        expired: list[Run] = []
        rows = self._conn.execute(
            "SELECT run_id, deadline_at FROM runs WHERE state='queued'"
        ).fetchall()
        for run_id, deadline in rows:
            if datetime.fromisoformat(deadline) > moment:
                continue
            self._conn.execute(
                "UPDATE runs SET state=?, revision=revision+1, finished_at=?, outcome_code=?, "
                "outcome_message=? WHERE run_id=?",
                (
                    RunState.FAILED.value,
                    rfc3339(moment),
                    "deadline_before_start",
                    "The deadline expired before execution started.",
                    run_id,
                ),
            )
            self._append_event(
                instance, run_id, "run_failed", rfc3339(moment), {"code": "deadline_before_start"}
            )
            expired.append(self._load_run(run_id))
        return expired

    def list_state(self, state: RunState, limit: int = 50) -> list[Run]:
        assert self._conn is not None
        try:
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE state=? ORDER BY created_at LIMIT ?",
                (state.value, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError("Cannot list runs.") from exc
        return [self._load_run(row[0]) for row in rows]

    def count_active(self) -> int:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT COUNT(*) FROM runs WHERE state IN ('running', 'cancelling')"
        ).fetchone()
        return int(row[0])

    # Unverified outcome rows never age out: their input, partial body, and
    # events are the evidence an operator needs before clearing isolation.
    _UNCONFIRMED_GUARD = (
        "effects_uncertain=0 AND NOT (state IN ('cancelled','interrupted') "
        "AND stop_confirmed=0)"
    )

    def cleanup(self, now: datetime | None = None) -> dict[str, int]:
        """Apply retention: bodies 14d, events 30d, run rows 90d with tombstones.

        Runs whose stop was never confirmed or whose effects stayed uncertain
        are retained regardless of age.
        """
        moment = now or utc_now()
        counts = {"result_bodies": 0, "events": 0, "runs": 0}
        with self._transaction():
            assert self._conn is not None
            body_cutoff = rfc3339(moment - timedelta(days=RESULT_RETENTION_DAYS))
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE result_cleaned_at IS NULL AND finished_at IS NOT NULL "
                f"AND finished_at < ? AND {self._UNCONFIRMED_GUARD}",
                (body_cutoff,),
            ).fetchall()
            for (run_id,) in rows:
                self._conn.execute(
                    "UPDATE runs SET input_text='', outcome_partial='', result_cleaned_at=? "
                    "WHERE run_id=?",
                    (rfc3339(moment), run_id),
                )
                counts["result_bodies"] += 1
            event_cutoff = rfc3339(moment - timedelta(days=EVENT_RETENTION_DAYS))
            meta_cutoff = rfc3339(moment - timedelta(days=METADATA_RETENTION_DAYS))
            last_seq_row = self._conn.execute("SELECT MAX(seq) FROM run_events").fetchone()
            last_seq = 0 if last_seq_row[0] is None else int(last_seq_row[0])
            # Events go only with the run row they belong to. An unconfirmed
            # run keeps its row, body and investigation events together no
            # matter how old they are, so age alone must never delete them.
            cursor = self._conn.execute(
                "DELETE FROM run_events WHERE at < ? AND run_id NOT IN ("
                "SELECT run_id FROM runs WHERE finished_at IS NULL OR "
                f"NOT ({self._UNCONFIRMED_GUARD}))",
                (event_cutoff,),
            )
            deleted_events = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            counts["events"] = deleted_events
            if deleted_events:
                # The delete may leave holes, so the floor is the smallest seq
                # still retained, or the next seq when nothing remains.
                floor_row = self._conn.execute("SELECT MIN(seq) FROM run_events").fetchone()
                floor = (
                    last_seq + 1 if floor_row[0] is None else int(floor_row[0])
                )
                self._conn.execute(
                    "INSERT INTO instance_meta(key, value) VALUES ('events_floor', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=MAX(value, excluded.value)",
                    (str(floor),),
                )
            stale = self._conn.execute(
                "SELECT run_id, state FROM runs WHERE finished_at IS NOT NULL AND finished_at < ? "
                f"AND {self._UNCONFIRMED_GUARD}",
                (meta_cutoff,),
            ).fetchall()
            instance = self.instance_id_ro()
            for run_id, state in stale:
                digest_row = self._conn.execute(
                    "SELECT principal_id, operation_key, request_digest FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if digest_row is not None:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO operation_tombstones "
                        "(instance_id, principal_id, operation_key, request_digest, terminal_state, "
                        "tombstoned_at) VALUES (?,?,?,?,?,?)",
                        (instance, digest_row[0], digest_row[1], digest_row[2], state, rfc3339(moment)),
                    )
                self._conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                counts["runs"] += 1
        return counts

    def _load_run(self, run_id: str) -> Run:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT run_id, instance_id, principal_id, operation_key, task_id, workflow_id, "
            "input_text, request_digest, deadline_at, state, revision, phase, stop_confirmed, "
            "effects_uncertain, outcome_code, outcome_message, outcome_partial, outcome_truncated, "
            "outcome_partial_bytes, cancel_requested_at, cancel_reason, created_at, started_at, "
            "finished_at, result_cleaned_at FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StoreUnavailableError("No run with that id.")
        outcome = None
        if row[14] is not None:
            outcome = RunOutcome(
                code=OutcomeCode(row[14]),
                message=row[15] or "",
                partial=row[16] or "",
                truncated=bool(row[17]),
                partial_bytes=int(row[18]),
            )
        return Run(
            run_id=row[0],
            instance_id=row[1],
            principal_id=row[2],
            operation_key=row[3],
            task_id=row[4],
            workflow_id=row[5],
            input_text=row[6],
            request_digest=row[7],
            deadline_at=datetime.fromisoformat(row[8]),
            state=RunState(row[9]),
            revision=int(row[10]),
            phase=row[11] or "",
            stop_confirmed=bool(row[12]),
            effects_uncertain=bool(row[13]),
            outcome=outcome,
            cancel_requested_at=None if row[19] is None else datetime.fromisoformat(row[19]),
            cancel_reason=row[20] or "",
            created_at=None if row[21] is None else datetime.fromisoformat(row[21]),
            started_at=None if row[22] is None else datetime.fromisoformat(row[22]),
            finished_at=None if row[23] is None else datetime.fromisoformat(row[23]),
            result_cleaned_at=None if row[24] is None else datetime.fromisoformat(row[24]),
        )
