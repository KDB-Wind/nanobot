"""Domain types for persistent agent runs (public run protocol v1)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROTOCOL_VERSION = 1

INPUT_MAX_BYTES = 16 * 1024
RESULT_MAX_BYTES = 32 * 1024
EVENT_MAX_BYTES = 16 * 1024
DEFAULT_DEADLINE_SECONDS = 30 * 60
MAX_DEADLINE_SECONDS = 24 * 60 * 60
CANCEL_CONFIRM_WAIT_SECONDS = 5.0

RESULT_RETENTION_DAYS = 14
EVENT_RETENTION_DAYS = 30
METADATA_RETENTION_DAYS = 90


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("deadline must carry a UTC timezone")
    return parsed.astimezone(timezone.utc)


class RunState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def active(self) -> bool:
        """The run still holds an execution slot on its instance."""
        return self in (RunState.RUNNING, RunState.CANCELLING)


_TERMINAL_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    }
)


class OutcomeCode(enum.StrEnum):
    SUCCEEDED = "succeeded"
    PROVIDER_ERROR = "provider_error"
    ITERATION_LIMIT = "iteration_limit"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DEADLINE_BEFORE_START = "deadline_before_start"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


_TERMINAL_OUTCOME_FOR_STATE = {
    RunState.SUCCEEDED: frozenset({OutcomeCode.SUCCEEDED}),
    RunState.FAILED: frozenset(
        {
            OutcomeCode.PROVIDER_ERROR,
            OutcomeCode.ITERATION_LIMIT,
            OutcomeCode.DEADLINE_EXCEEDED,
            OutcomeCode.DEADLINE_BEFORE_START,
            OutcomeCode.EXECUTION_ERROR,
        }
    ),
    RunState.CANCELLED: frozenset({OutcomeCode.CANCELLED, OutcomeCode.DEADLINE_EXCEEDED}),
    RunState.INTERRUPTED: frozenset({OutcomeCode.INTERRUPTED}),
}


def outcome_allowed_for_state(state: RunState, code: OutcomeCode) -> bool:
    return code in _TERMINAL_OUTCOME_FOR_STATE.get(state, frozenset())


@dataclass(frozen=True)
class RunOutcome:
    code: OutcomeCode
    message: str = ""
    partial: str = ""
    truncated: bool = False
    partial_bytes: int = 0


@dataclass(frozen=True)
class RunSubmit:
    task_id: str
    workflow_id: str
    input_text: str
    deadline_at: datetime
    operation_key: str
    principal_id: str


@dataclass(frozen=True)
class Run:
    run_id: str
    instance_id: str
    principal_id: str
    operation_key: str
    task_id: str
    workflow_id: str
    input_text: str
    request_digest: str
    deadline_at: datetime
    state: RunState
    revision: int
    phase: str = ""
    stop_confirmed: bool = False
    effects_uncertain: bool = False
    outcome: RunOutcome | None = None
    cancel_requested_at: datetime | None = None
    cancel_reason: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.state.terminal


@dataclass(frozen=True)
class RunEvent:
    seq: int
    instance_id: str
    run_id: str
    type: str
    at: datetime
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    """Typed executor completion; string model output is never parsed for success."""

    code: OutcomeCode
    text: str = ""
    message: str = ""
    truncated: bool = False
    partial_bytes: int = 0
    stop_confirmed: bool = True
    effects_uncertain: bool = False


@dataclass(frozen=True)
class SubmitAcceptance:
    run: Run
    created: bool


class RunsError(RuntimeError):
    """Base error carrying a protocol error code."""

    code = "runs_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class IdempotencyConflictError(RunsError):
    code = "idempotency_conflict"


class UnknownRunError(RunsError):
    code = "run_not_found"


class StoreUnavailableError(RunsError):
    code = "runs_store_unavailable"


class EventTooLargeError(RunsError):
    code = "event_too_large"


class InstanceIsolatedError(RunsError):
    code = "instance_isolated"
