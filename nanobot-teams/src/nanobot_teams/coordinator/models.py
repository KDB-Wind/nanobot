"""Coordinator domain models: projects, bots, collaboration tasks, outbox entries."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


def new_task_id() -> str:
    import uuid

    return f"task_{uuid.uuid4()}"


class DispatchState(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class CancelState(enum.StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class DeliveryState(enum.StrEnum):
    NOT_READY = "not_ready"
    PENDING = "pending"
    PERSISTED = "persisted"
    INJECTED = "injected"
    CARD_ONLY = "card_only"
    ORPHANED = "orphaned"
    FAILED = "failed"


class DeliveryMode(enum.StrEnum):
    WORKBENCH_ONLY = "workbench_only"
    AUTO_RESUME = "auto_resume"


TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
ACTIVE_RUN_STATES = frozenset({"running", "cancelling"})


@dataclass(frozen=True)
class Project:
    project_id: str
    display_name: str
    created_at: datetime
    archived_at: datetime | None = None


@dataclass(frozen=True)
class Bot:
    bot_id: str
    display_name: str
    role: str
    endpoint: str
    secret_env: str
    enabled: bool = True
    instance_id: str | None = None
    capabilities: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True)
class Task:
    task_id: str
    project_id: str
    workflow_id: str
    caller_principal: str
    bot_id: str
    operation_key: str
    request_digest: str
    input_text: str
    deadline_at: datetime
    delivery_mode: DeliveryMode
    dispatch_state: DispatchState
    created_at: datetime
    remote_run_id: str | None = None
    remote_instance_id: str | None = None
    remote_revision: int = 0
    run_state: str = "unobserved"
    stop_confirmed: bool = False
    effects_uncertain: bool = False
    outcome_code: str | None = None
    outcome_message: str = ""
    result_text: str = ""
    result_truncated: bool = False
    result_available: bool = False
    cancel_state: CancelState = CancelState.NONE
    delivery_state: DeliveryState = DeliveryState.NOT_READY
    finished_at: datetime | None = None
    last_error: str = ""

    @property
    def open(self) -> bool:
        """The task has not reached a final observation."""
        return self.dispatch_state in (DispatchState.PENDING, DispatchState.ACCEPTED) and (
            self.run_state not in TERMINAL_RUN_STATES
        )


@dataclass(frozen=True)
class DispatchEntry:
    entry_id: int
    task_id: str
    state: str  # pending | confirmed | failed
    attempts: int
    next_attempt_at: datetime
    last_error: str = ""


@dataclass(frozen=True)
class Quarantine:
    instance_id: str
    reason: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str = ""
    resolution_note: str = ""


@dataclass(frozen=True)
class Event:
    seq: int
    at: datetime
    type: str
    task_id: str | None = None
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QuotaSnapshot:
    workflow_tasks: int
    principal_open: int
    global_open: int
    bot_queue: int
    workflow_result_bytes: int
