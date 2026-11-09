"""Durable external inbox: reply capabilities, workflows, and delivery receipts.

The planner host owns this state. The Coordinator never reads it directly; it
only delivers fixed-field payloads and observes receipt states through the
inbox HTTP protocol.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from .state import SessionConflictError, SessionState

CAPABILITY_TTL = timedelta(days=30)

_T = TypeVar("_T")


class InboxError(ValueError):
    """Protocol-level rejection carrying a stable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReplyCapability:
    capability_id: str
    token: str
    digest: str
    session_key: str
    session_generation: str
    workflow_id: str
    operation_key: str
    bot_id: str
    instance_id: str
    principal_id: str
    expires_at: datetime


@dataclass(frozen=True)
class InboxDelivery:
    delivery_id: str
    session_key: str
    session_generation: str
    workflow_id: str
    task_id: str
    bot_id: str
    instance_id: str
    run_id: str
    terminal_revision: int
    outcome_code: str
    result_text: str
    result_truncated: bool
    body_digest: str


@dataclass(frozen=True)
class InboxReceipt:
    delivery_id: str
    state: str
    received_at: str
    injected_at: str | None
    error: str


def delivery_body_digest(
    *,
    task_id: str,
    bot_id: str,
    instance_id: str,
    run_id: str,
    terminal_revision: int,
    outcome_code: str,
    result_text: str,
) -> str:
    body = json.dumps(
        {
            "task_id": task_id,
            "bot_id": bot_id,
            "instance_id": instance_id,
            "run_id": run_id,
            "terminal_revision": terminal_revision,
            "outcome_code": outcome_code,
            "result_text": result_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ExternalInbox:
    """Owns receipt persistence and atomic history injection for one host."""

    def __init__(self, state: SessionState) -> None:
        self._state = state
        self._store = state._store

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._store.run_write(lambda: operation(self._store._local.connection))

    def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._store.run_read(lambda: operation(self._store._local.connection))

    # ---- capabilities ----

    def mint_capability(
        self,
        *,
        session_key: str,
        session_generation: str,
        workflow_id: str,
        operation_key: str,
        bot_id: str,
        instance_id: str,
        principal_id: str,
    ) -> ReplyCapability:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        capability = ReplyCapability(
            capability_id=f"cap_{secrets.token_hex(8)}",
            token=token,
            digest=digest,
            session_key=session_key,
            session_generation=session_generation,
            workflow_id=workflow_id,
            operation_key=operation_key,
            bot_id=bot_id,
            instance_id=instance_id,
            principal_id=principal_id,
            expires_at=datetime.now(tz=timezone.utc) + CAPABILITY_TTL,
        )
        now = datetime.now(tz=timezone.utc).isoformat()

        def persist(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO reply_capabilities (capability_id, digest, session_key, "
                "session_generation, workflow_id, operation_key, bot_id, instance_id, "
                "principal_id, auto_continue, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    capability.capability_id, digest, session_key, session_generation,
                    workflow_id, operation_key, bot_id, instance_id, principal_id,
                    now, capability.expires_at.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO external_workflows (workflow_id, session_key, session_generation, "
                "created_at, auto_continue) VALUES (?,?,?,?,1) "
                "ON CONFLICT(workflow_id) DO UPDATE SET auto_continue=1",
                (workflow_id, session_key, session_generation, now),
            )

        self._write(persist)
        return capability

    def validate_capability(
        self,
        *,
        token: str,
        workflow_id: str,
        operation_key: str,
        bot_id: str,
        instance_id: str,
        principal_id: str,
    ) -> ReplyCapability:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()

        def query(conn: sqlite3.Connection) -> tuple | None:
            return conn.execute(
                "SELECT capability_id, session_key, session_generation, workflow_id, "
                "operation_key, bot_id, instance_id, principal_id, expires_at, auto_continue "
                "FROM reply_capabilities WHERE digest=?",
                (digest,),
            ).fetchone()

        def session_generation(conn: sqlite3.Connection) -> str | None:
            found = conn.execute(
                "SELECT generation FROM sessions WHERE key=?", (row[1],)
            ).fetchone() if row is not None else None
            return None if found is None else str(found[0])

        row = self._read(query)
        if row is None:
            raise InboxError("origin_required", "Unknown reply capability.")
        live_generation = self._read(session_generation)
        if live_generation != row[2]:
            raise InboxError("stale_origin", "The origin session is gone or was replaced.")
        expires = datetime.fromisoformat(row[8])
        if datetime.now(tz=timezone.utc) > expires:
            raise InboxError("capability_expired", "The reply capability expired.")
        if not row[9]:
            raise InboxError("auto_continue_revoked", "The workflow revoked auto-continue.")
        if row[7] != principal_id:
            raise InboxError("capability_binding_mismatch", "Capability bindings do not match.")
        bound = self._bind_first_use(
            digest, row[3], row[4], row[5], row[6],
            workflow_id, operation_key, bot_id, instance_id,
        )
        if bound is None:
            raise InboxError(
                "capability_binding_mismatch",
                "Capability already bound to a different workflow, key, or bot.",
            )
        row = (
            row[0], row[1], row[2], bound[0], bound[1], bound[2], bound[3],
            row[7], row[8], row[9],
        )
        return ReplyCapability(
            capability_id=row[0],
            token=token,
            digest=digest,
            session_key=row[1],
            session_generation=row[2],
            workflow_id=row[3],
            operation_key=row[4],
            bot_id=row[5],
            instance_id=row[6],
            principal_id=row[7],
            expires_at=expires,
        )

    def _bind_first_use(
        self,
        digest: str,
        stored_workflow: str,
        stored_operation: str,
        stored_bot: str,
        stored_instance: str,
        workflow_id: str,
        operation_key: str,
        bot_id: str,
        instance_id: str,
    ) -> tuple[str, str, str, str] | None:
        """Bind wildcard fields on first use; reject any later mismatch."""

        def resolve(conn: sqlite3.Connection) -> tuple[str, str, str, str] | None:
            if stored_workflow not in ("*", "") and stored_workflow != workflow_id:
                return None
            if stored_operation not in ("*", "") and stored_operation != operation_key:
                return None
            if stored_bot not in ("*", "") and stored_bot != bot_id:
                return None
            if stored_instance not in ("*", "") and instance_id and stored_instance != instance_id:
                return None
            new_operation = stored_operation if stored_operation not in ("*", "") else operation_key
            new_bot = stored_bot if stored_bot not in ("*", "") else bot_id
            new_instance = (
                stored_instance
                if stored_instance not in ("*", "") or not instance_id
                else instance_id
            )
            if (
                new_operation != stored_operation
                or new_bot != stored_bot
                or new_instance != stored_instance
            ):
                conn.execute(
                    "UPDATE reply_capabilities SET operation_key=?, bot_id=?, instance_id=? "
                    "WHERE digest=?",
                    (new_operation, new_bot, new_instance, digest),
                )
            return workflow_id, new_operation, new_bot, new_instance

        return self._write(resolve)

    # ---- workflows ----

    def revoke_session_workflows(self, session_key: str) -> int:
        """Synchronous revoke; call only from the store's writer context."""

        def update(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE external_workflows SET auto_continue=0 "
                "WHERE session_key=? AND auto_continue=1",
                (session_key,),
            )
            conn.execute(
                "UPDATE reply_capabilities SET auto_continue=0 "
                "WHERE session_key=? AND auto_continue=1",
                (session_key,),
            )
            return cursor.rowcount or 0

        return self._write(update)

    async def revoke_session_workflows_async(self, session_key: str) -> int:
        """Revoke through the session owner so the write uses its writer thread."""
        result = await self._state._execute(
            session_key,
            lambda: self._state._change(
                session_key,
                lambda session: self.revoke_session_workflows(session_key),
                history=False,
            ),
            operation_name="revoke_external_workflows",
        )
        return int(result) if isinstance(result, int) else 0

    def workflow_allows_auto_continue(self, workflow_id: str) -> bool:
        def query(conn: sqlite3.Connection) -> tuple | None:
            return conn.execute(
                "SELECT auto_continue FROM external_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()

        row = self._read(query)
        return bool(row and row[0])

    # ---- receipts ----

    def receive(self, delivery: InboxDelivery) -> InboxReceipt:
        """Persist the receipt durably; injection happens later, atomically."""
        now = datetime.now(tz=timezone.utc).isoformat()

        def insert(conn: sqlite3.Connection) -> InboxReceipt:
            existing = conn.execute(
                "SELECT body_digest, state, received_at, injected_at, last_error "
                "FROM external_inbox WHERE delivery_id=?",
                (delivery.delivery_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != delivery.body_digest:
                    raise InboxError(
                        "delivery_conflict", "Same delivery id with a different body."
                    )
                return InboxReceipt(
                    delivery_id=delivery.delivery_id, state=existing[1],
                    received_at=existing[2], injected_at=existing[3], error=existing[4],
                )
            allows = conn.execute(
                "SELECT auto_continue FROM external_workflows WHERE workflow_id=?",
                (delivery.workflow_id,),
            ).fetchone()
            state = "received" if allows and allows[0] else "card_only"
            conn.execute(
                "INSERT INTO external_inbox (delivery_id, session_key, session_generation, "
                "workflow_id, task_id, bot_id, instance_id, run_id, terminal_revision, "
                "outcome_code, result_text, result_truncated, body_digest, state, received_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delivery.delivery_id, delivery.session_key, delivery.session_generation,
                    delivery.workflow_id, delivery.task_id, delivery.bot_id,
                    delivery.instance_id, delivery.run_id, delivery.terminal_revision,
                    delivery.outcome_code, delivery.result_text,
                    int(delivery.result_truncated), delivery.body_digest, state, now,
                ),
            )
            return InboxReceipt(
                delivery_id=delivery.delivery_id, state=state, received_at=now,
                injected_at=None, error="",
            )

        return self._write(insert)

    def get_receipt(self, delivery_id: str) -> InboxReceipt | None:
        def query(conn: sqlite3.Connection) -> InboxReceipt | None:
            row = conn.execute(
                "SELECT state, received_at, injected_at, last_error FROM external_inbox "
                "WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return None
            return InboxReceipt(
                delivery_id=delivery_id, state=row[0], received_at=row[1],
                injected_at=row[2], error=row[3],
            )

        return self._read(query)

    def pending_deliveries(self, session_key: str | None = None) -> list[str]:
        def query(conn: sqlite3.Connection) -> list[str]:
            if session_key is None:
                rows = conn.execute(
                    "SELECT delivery_id FROM external_inbox WHERE state='received'"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT delivery_id FROM external_inbox "
                    "WHERE state='received' AND session_key=?",
                    (session_key,),
                ).fetchall()
            return [row[0] for row in rows]

        return self._read(query)

    def _inbox_row(self, conn: sqlite3.Connection, delivery_id: str) -> dict | None:
        row = conn.execute(
            "SELECT delivery_id, session_key, session_generation, workflow_id, task_id, "
            "bot_id, instance_id, run_id, terminal_revision, outcome_code, result_text, "
            "result_truncated FROM external_inbox WHERE delivery_id=? AND state='received'",
            (delivery_id,),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "delivery_id", "session_key", "session_generation", "workflow_id", "task_id",
            "bot_id", "instance_id", "run_id", "terminal_revision", "outcome_code",
            "result_text", "result_truncated",
        )
        return dict(zip(keys, row, strict=True))

    def _delivery_or_none(self, delivery_id: str) -> dict | None:
        return self._read(lambda conn: self._inbox_row(conn, delivery_id))

    async def apply_pending(self, delivery_id: str) -> str:
        """Atomically inject one received delivery into history; idempotent."""

        def change(session):  # type: ignore[no-untyped-def]
            conn = self._store._local.connection
            row = self._inbox_row(conn, delivery_id)
            if row is None:
                return "already_handled"
            if session.key != row["session_key"]:
                raise SessionConflictError("delivery routed to the wrong session")
            if session.generation != row["session_generation"]:
                self._mark_state(conn, delivery_id, "orphaned", "session generation replaced")
                return "orphaned"
            marker = f"external_delivery:{delivery_id}"
            for message in session.messages:
                if message.get("external_delivery_id") == marker:
                    self._mark_state(conn, delivery_id, "injected", "")
                    return "injected"
            truncated_note = (
                "\n[result truncated by the 32 KiB protocol limit]"
                if row["result_truncated"]
                else ""
            )
            text = (
                f"Async task {row['task_id']} (bot {row['bot_id']}) finished with "
                f"{row['outcome_code']}.\n\n{row['result_text']}{truncated_note}"
            )
            session.add_message(
                "assistant",
                text,
                external_delivery_id=marker,
                injected_event="external_delivery",
                source_workflow=row["workflow_id"],
                external_task_id=row["task_id"],
            )
            self._mark_state(conn, delivery_id, "injected", "")
            return "injected"

        row = self._delivery_or_none(delivery_id)
        if row is None:
            return "already_handled"
        session_key = str(row["session_key"])
        try:
            return await self._state._execute(
                session_key,
                lambda: self._state._change(session_key, change),
                operation_name="apply_external_delivery",
            )
        except SessionConflictError:
            def orphan(conn: sqlite3.Connection) -> None:
                self._mark_state(conn, delivery_id, "orphaned", "session missing or replaced")

            self._write(orphan)
            return "orphaned"

    @staticmethod
    def _mark_state(conn: sqlite3.Connection, delivery_id: str, state: str, error: str) -> None:
        conn.execute(
            "UPDATE external_inbox SET state=?, last_error=?, injected_at="
            "CASE WHEN ?='injected' THEN ? ELSE injected_at END WHERE delivery_id=?",
            (
                state,
                error,
                state,
                datetime.now(tz=timezone.utc).isoformat(),
                delivery_id,
            ),
        )


__all__ = [
    "CAPABILITY_TTL",
    "ExternalInbox",
    "InboxDelivery",
    "InboxError",
    "InboxReceipt",
    "ReplyCapability",
    "delivery_body_digest",
]
