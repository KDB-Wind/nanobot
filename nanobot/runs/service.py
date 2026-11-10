"""Run admission, execution scheduling, cancellation, and recovery.

The service is the single writer driver over one :class:`RunStore`. Admission
persists before any execution; a persisted ``running`` marker precedes every
executor call, so a crash between the marker and the call is treated as an
interruption, never as a reason to execute twice.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Protocol

from .store import (
    RunStore,
    StateTransitionError,
    StoreUnavailableError,
    canonical_request_digest,
)
from .types import (
    CANCEL_CONFIRM_WAIT_SECONDS,
    DEFAULT_DEADLINE_SECONDS,
    INPUT_MAX_BYTES,
    MAX_DEADLINE_SECONDS,
    RESULT_MAX_BYTES,
    ExecutionResult,
    IdempotencyConflictError,
    InstanceIsolatedError,
    OutcomeCode,
    Run,
    RunEvent,
    RunOutcome,
    RunsError,
    RunState,
    RunSubmit,
    SubmitAcceptance,
    UnknownRunError,
    parse_rfc3339,
    utc_now,
)

log = logging.getLogger("nanobot.runs.service")


class RunExecutor(Protocol):
    async def execute(self, run: Run) -> ExecutionResult: ...


class EventCursorExpiredError(RunsError):
    code = "event_cursor_expired"


class InputTooLargeError(RunsError):
    code = "input_too_large"


class InvalidSubmitError(RunsError):
    code = "invalid_submit"


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool, int]:
    """Cut UTF-8 text to max_bytes on a character boundary, never silently."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False, len(raw)
    cut = raw[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8"), True, len(raw)
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True, len(raw)


class RunService:
    def __init__(
        self,
        store: RunStore,
        executor: RunExecutor,
        *,
        max_active: int = 1,
        cancel_wait_s: float = CANCEL_CONFIRM_WAIT_SECONDS,
        poll_interval_s: float = 0.05,
    ) -> None:
        self._store = store
        self._executor = executor
        self._max_active = max(1, max_active)
        self._cancel_wait_s = cancel_wait_s
        self._poll_interval_s = poll_interval_s
        self._instance_id = store.instance_id()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Results whose store commit failed: the exact observation is kept so
        # the pump can retry it without re-running the executor.
        self._pending_results: dict[str, ExecutionResult] = {}
        self._worker: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._closing = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def isolation(self) -> tuple[bool, str]:
        return self._store.isolation()

    def clear_isolation(self, actor: str, reason: str) -> tuple[bool, str]:
        """Clear the instance quarantine; returns the previous (isolated, reason)."""
        previous = self._store.isolation()
        self._store.clear_isolation(actor, reason)
        return previous

    async def start(self) -> list[Run]:
        interrupted = self._store.recover()
        if interrupted:
            names = ", ".join(run.run_id for run in interrupted)
            log.warning("runs recovered after restart, interrupted: %s", names)
        self._worker = asyncio.create_task(self._worker_loop(), name="runs-worker")
        self._wakeup.set()
        return interrupted

    async def aclose(self) -> None:
        """Request cooperative cancel of tracked runs; keep their durable state."""
        self._closing = True
        self._wakeup.set()
        worker = self._worker
        if worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=self._cancel_wait_s)
            except (TimeoutError, asyncio.CancelledError):
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks.values()), timeout=self._cancel_wait_s)
        for run_id, task in self._tasks.items():
            if not task.done():
                log.warning("run %s did not confirm stop before close", run_id)

    async def submit(self, request: RunSubmit) -> SubmitAcceptance:
        _validate_ids(request)
        text_bytes = request.input_text.encode("utf-8", errors="strict")
        if not text_bytes.strip():
            raise InvalidSubmitError("input_text must not be empty.")
        if len(text_bytes) > INPUT_MAX_BYTES:
            raise InputTooLargeError(
                f"input_text exceeds {INPUT_MAX_BYTES} bytes before admission."
            )
        # Digest the caller-provided semantics first: a defaulted deadline is
        # fixed after the key check, so replaying the identical wire body
        # always reconciles to the original run.
        digest = canonical_request_digest(request)
        existing = self._store.lookup(request.principal_id, request.operation_key)
        if existing is not None:
            if existing.request_digest != digest:
                raise IdempotencyConflictError(
                    "This operation key was submitted with a different request body."
                )
            return SubmitAcceptance(run=existing, created=False)
        deadline = _deadline_or_default(request.deadline_at)
        _validate_deadline_bounds(deadline)
        isolated, reason = self._store.isolation()
        if isolated:
            raise InstanceIsolatedError(f"This instance is isolated: {reason}")
        normalized = RunSubmit(
            task_id=request.task_id,
            workflow_id=request.workflow_id,
            input_text=request.input_text,
            deadline_at=deadline,
            operation_key=request.operation_key,
            principal_id=request.principal_id,
        )
        run, created = self._store.submit(
            normalized,
            instance_id=self._instance_id,
            run_id=f"run_{uuid.uuid4()}",
            digest=digest,
        )
        if created:
            self._wakeup.set()
        return SubmitAcceptance(run=run, created=created)

    async def get(self, run_id: str) -> Run:
        run = self._store.get(run_id)
        if run is None:
            raise UnknownRunError("No run with that id.")
        return run

    async def lookup(self, principal_id: str, operation_key: str) -> Run | None:
        return self._store.lookup(principal_id, operation_key)

    async def cancel(self, run_id: str) -> Run:
        """Record a cancel request durably; actual stop is observed, not assumed."""
        run = await self.get(run_id)
        if run.terminal:
            return run
        # A worker pump may move the run between our read and the transition;
        # re-read on conflict so a cancel request is never silently dropped.
        inject_cancel = False
        for _ in range(3):
            run = await self.get(run_id)
            if run.terminal:
                return run
            if run.state is RunState.QUEUED:
                try:
                    self._store.transition(
                        run.run_id,
                        RunState.QUEUED,
                        RunState.CANCELLED,
                        outcome=RunOutcome(code=OutcomeCode.CANCELLED, message="Cancelled before start."),
                        stop_confirmed=True,
                        cancel_requested=True,
                        cancel_reason="cancel_requested",
                        event_data={"code": "cancelled"},
                    )
                    self._wakeup.set()
                    return await self.get(run_id)
                except StateTransitionError:
                    continue
            if run.state is RunState.CANCELLING:
                # A repeated cancel is idempotent: the first request already
                # owns the cancellation and its reap.
                break
            if run.state is RunState.RUNNING:
                try:
                    self._store.transition(
                        run.run_id,
                        RunState.RUNNING,
                        RunState.CANCELLING,
                        cancel_requested=True,
                        cancel_reason="cancel_requested",
                        event_type="cancel_requested",
                    )
                except StateTransitionError:
                    continue
                inject_cancel = True
                break
            break
        task = self._tasks.get(run_id)
        if inject_cancel and task is not None and not task.done():
            task.cancel()
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=self._cancel_wait_s)
        return await self.get(run_id)

    async def events(self, after_seq: int) -> list[RunEvent]:
        floor = self._store.events_floor()
        if floor is not None and after_seq < floor:
            raise EventCursorExpiredError(
                "The event cursor predates retained events; refetch the snapshot."
            )
        return self._store.events_after(after_seq)

    async def recover(self) -> list[Run]:
        return self._store.recover()

    async def _worker_loop(self) -> None:
        while not self._closing:
            try:
                progressed = await self._pump_once()
            except Exception:
                log.exception("runs worker iteration failed")
                progressed = False
            if not progressed:
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=1.0)
                except (asyncio.TimeoutError, TimeoutError):
                    pass

    async def _pump_once(self) -> bool:
        # A finished run with a held result owns a real outcome; committing it
        # comes before any sweep that could classify the row as an orphan or
        # rewrite it as cancelled.
        progressed = await self._retry_pending_results()
        progressed = self._sweep_deadlines() or progressed
        progressed = self._reap_orphan_cancelling() or progressed
        progressed = self._start_queued() or progressed
        return progressed

    async def _retry_pending_results(self) -> bool:
        """Commit held results in place; never re-run the executor for them."""
        if not self._pending_results:
            return False
        acted = False
        for run_id, result in list(self._pending_results.items()):
            try:
                await self._apply_result(run_id, result)
            except StoreUnavailableError:
                continue
            self._pending_results.pop(run_id, None)
            acted = True
        return acted

    def _reap_orphan_cancelling(self) -> bool:
        """Finalize cancelling runs whose executor task is gone.

        The executor coroutine exits when its result could not be persisted or
        the process is closing; the slot must not stay held forever. The stop
        is recorded conservatively: the coroutine is gone, but whether every
        background effect was reaped is unknown, so the instance isolates.
        """
        acted = False
        orphans = self._store.list_state(RunState.CANCELLING, limit=100) + self._store.list_state(
            RunState.RUNNING, limit=100
        )
        for run in orphans:
            if run.run_id in self._pending_results:
                # The executor finished and its result is held; this is not an
                # orphan and must never be rewritten to cancelled.
                continue
            task = self._tasks.get(run.run_id)
            if task is not None and not task.done():
                continue
            try:
                self._store.transition(
                    run.run_id,
                    run.state,
                    RunState.CANCELLED,
                    outcome=RunOutcome(
                        code=OutcomeCode.CANCELLED,
                        message="Executor exited before confirming the stop; "
                        "background effects are unverified.",
                    ),
                    stop_confirmed=False,
                    effects_uncertain=True,
                    event_type="run_cancelled_unverified",
                    isolation_reason=(
                        f"Run {run.run_id} stopped without confirmed effect cleanup."
                    ),
                )
                acted = True
            except StateTransitionError:
                continue
            except StoreUnavailableError:
                return acted
        return acted

    def _sweep_deadlines(self) -> bool:
        now = utc_now()
        acted = False
        for run in self._store.list_state(RunState.QUEUED, limit=100):
            if run.deadline_at <= now:
                self._store.expire_queued(now)
                acted = True
        for run in self._store.list_state(RunState.RUNNING, limit=100) + self._store.list_state(
            RunState.CANCELLING, limit=100
        ):
            if run.run_id in self._pending_results:
                # A completed result waiting for its commit must not be
                # cancelled just because its deadline passed while the store
                # was unavailable.
                continue
            if run.deadline_at > now or run.cancel_reason == "deadline_exceeded":
                continue
            was_running = run.state is RunState.RUNNING
            try:
                self._store.transition(
                    run.run_id,
                    run.state,
                    RunState.CANCELLING,
                    cancel_requested=True,
                    cancel_reason="deadline_exceeded",
                    event_type="deadline_exceeded",
                )
            except StateTransitionError:
                continue
            task = self._tasks.get(run.run_id)
            if was_running and task is not None and not task.done():
                task.cancel()
            acted = True
        return acted

    def _start_queued(self) -> bool:
        isolated, _reason = self._store.isolation()
        if isolated:
            return False
        started_any = False
        for _ in range(16):
            if self._store.count_active() >= self._max_active:
                break
            candidates = [
                run
                for run in self._store.list_state(RunState.QUEUED, limit=10)
                if run.run_id not in self._tasks
            ]
            if not candidates:
                break
            run = candidates[0]
            if run.deadline_at <= utc_now():
                self._store.expire_queued()
                continue
            try:
                started = self._store.transition(
                    run.run_id, RunState.QUEUED, RunState.RUNNING
                )
            except StateTransitionError:
                continue
            self._tasks[started.run_id] = asyncio.create_task(
                self._execute_run(started), name=f"run-{started.run_id}"
            )
            started_any = True
        return started_any

    async def _execute_run(self, run: Run) -> None:
        try:
            try:
                result = await self._executor.execute(run)
            except asyncio.CancelledError:
                unverified = bool(
                    getattr(self._executor, "consume_cancel_unverified", lambda: False)()
                )
                await self._finalize_cancelled(run.run_id, unverified=unverified)
                raise
            except Exception as exc:
                result = ExecutionResult(
                    code=OutcomeCode.EXECUTION_ERROR,
                    message=f"{type(exc).__name__}: {exc}"[:512],
                    stop_confirmed=True,
                )
            try:
                await self._apply_result(run.run_id, result)
            except StoreUnavailableError:
                # Keep the exact observation: the pump retries this same result
                # once the store recovers. The run stays non-terminal until it
                # lands, and a process exit leaves recovery to mark it
                # interrupted rather than cancelled.
                self._pending_results[run.run_id] = result
                log.warning(
                    "run %s result could not be persisted; retry pending",
                    run.run_id,
                )
        finally:
            self._tasks.pop(run.run_id, None)
            self._wakeup.set()

    async def _finalize_cancelled(self, run_id: str, *, unverified: bool = False) -> None:
        current = self._store.get(run_id)
        if current is None or current.terminal:
            return
        reason = current.cancel_reason or "cancel_requested"
        code = (
            OutcomeCode.DEADLINE_EXCEEDED
            if reason == "deadline_exceeded"
            else OutcomeCode.CANCELLED
        )
        try:
            self._store.transition(
                run_id,
                current.state,
                RunState.CANCELLED,
                outcome=RunOutcome(code=code, message=f"Executor exited after {reason}."),
                # The stop is only confirmed when the executor's typed report
                # proved nothing is still live.
                stop_confirmed=not unverified,
                effects_uncertain=True if unverified else None,
                isolation_reason=(
                    f"Run {run_id} cancelled before its background work confirmed cleanup."
                    if unverified
                    else None
                ),
            )
        except StateTransitionError:
            pass

    async def _apply_result(self, run_id: str, result: ExecutionResult) -> None:
        current = self._store.get(run_id)
        if current is None or current.terminal:
            return
        isolation_reason = None
        if result.effects_uncertain:
            isolation_reason = (
                f"Run {run_id} stopped with unconfirmed background effects."
            )
        elif not result.stop_confirmed:
            # The executor could not prove the run's background work stopped;
            # the slot must not be treated as clean until an operator checks.
            isolation_reason = (
                f"Run {run_id} finished without a confirmed stop of its "
                "background work."
            )
        if result.code is OutcomeCode.SUCCEEDED:
            text, truncated, original_bytes = truncate_text(result.text, RESULT_MAX_BYTES)
            outcome = RunOutcome(
                code=OutcomeCode.SUCCEEDED,
                message=result.message[:512],
                partial=text,
                truncated=truncated or result.truncated,
                partial_bytes=original_bytes if truncated else result.partial_bytes,
            )
            new_state = RunState.SUCCEEDED
        elif result.code is OutcomeCode.CANCELLED:
            text, truncated, original_bytes = truncate_text(result.text, RESULT_MAX_BYTES)
            outcome = RunOutcome(
                code=OutcomeCode.CANCELLED,
                message=result.message[:512],
                partial=text,
                truncated=truncated or result.truncated,
                partial_bytes=original_bytes if truncated else result.partial_bytes,
            )
            new_state = RunState.CANCELLED
        elif result.code is OutcomeCode.INTERRUPTED:
            outcome = RunOutcome(
                code=OutcomeCode.INTERRUPTED,
                message=result.message[:512],
            )
            new_state = RunState.INTERRUPTED
        else:
            text, truncated, original_bytes = truncate_text(result.text, RESULT_MAX_BYTES)
            outcome = RunOutcome(
                code=result.code,
                message=result.message[:512],
                partial=text,
                truncated=truncated or result.truncated,
                partial_bytes=original_bytes if truncated else result.partial_bytes,
            )
            new_state = RunState.FAILED
        try:
            self._store.transition(
                run_id,
                current.state,
                new_state,
                outcome=outcome,
                stop_confirmed=result.stop_confirmed,
                effects_uncertain=result.effects_uncertain or None,
                isolation_reason=isolation_reason,
            )
        except StateTransitionError:
            pass

    def cleanup(self) -> dict[str, int]:
        return self._store.cleanup()


def _deadline_or_default(deadline: datetime | None) -> datetime:
    if deadline is None:
        return utc_now() + timedelta(seconds=DEFAULT_DEADLINE_SECONDS)
    if deadline.tzinfo is None:
        raise InvalidSubmitError("deadline_at must be an RFC 3339 timestamp with timezone.")
    return deadline


def _validate_deadline_bounds(deadline: datetime) -> None:
    now = utc_now()
    if deadline <= now:
        raise InvalidSubmitError("deadline_at must be in the future.")
    if deadline > now + timedelta(seconds=MAX_DEADLINE_SECONDS):
        raise InvalidSubmitError(
            f"deadline_at must not exceed {MAX_DEADLINE_SECONDS} seconds ahead."
        )


def _validate_ids(request: RunSubmit) -> None:
    try:
        uuid.UUID(request.operation_key)
    except ValueError as exc:
        raise InvalidSubmitError("operation_key must be a UUID.") from exc
    if len(request.task_id) > 128 or len(request.workflow_id) > 128:
        raise InvalidSubmitError("task_id and workflow_id must be at most 128 characters.")
    if not request.task_id or not request.workflow_id:
        raise InvalidSubmitError("task_id and workflow_id must be non-empty.")
    if not request.principal_id:
        raise InvalidSubmitError("principal_id must be a non-empty string.")


__all__ = [
    "EventCursorExpiredError",
    "InputTooLargeError",
    "InvalidSubmitError",
    "RunExecutor",
    "RunService",
    "truncate_text",
    "parse_rfc3339",
]
