"""Domain behavior of the persistent run service: idempotency, races, cancellation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from nanobot.runs.service import (
    EventCursorExpiredError,
    IdempotencyConflictError,
    InputTooLargeError,
    InvalidSubmitError,
    RunService,
    truncate_text,
)
from nanobot.runs.store import OperationKeyRetiredError, RunStore
from nanobot.runs.types import (
    ExecutionResult,
    InstanceIsolatedError,
    OutcomeCode,
    Run,
    RunState,
    RunSubmit,
    utc_now,
)


def replay_request(run: Run) -> RunSubmit:
    """Rebuild the exact original request body for idempotent reconciliation."""
    return RunSubmit(
        task_id=run.task_id,
        workflow_id=run.workflow_id,
        input_text=run.input_text,
        deadline_at=run.deadline_at,
        operation_key=run.operation_key,
        principal_id=run.principal_id,
    )


class FakeExecutor:
    """Controllable executor: gate holds execution, resist_cancel swallows cancellation."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.result = ExecutionResult(code=OutcomeCode.SUCCEEDED, text="ok")
        self.resist_cancel = False
        self.after_cancel_gate = asyncio.Event()

    async def execute(self, run) -> ExecutionResult:
        self.calls.append(run.run_id)
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            if not self.resist_cancel:
                raise
            await self.after_cancel_gate.wait()
            return ExecutionResult(code=OutcomeCode.CANCELLED)
        return self.result


@pytest.fixture
async def make(tmp_path):
    """Build services that are always closed, even when an assertion fails."""
    made: list[RunService] = []

    def _make(executor=None, **kwargs):
        store = RunStore(tmp_path / f"runs-{len(made)}.sqlite3")
        store.open()
        fake = executor if executor is not None else FakeExecutor()
        service = RunService(store, fake, **kwargs)
        made.append(service)
        return service, fake

    yield _make
    for service in made:
        try:
            await service.aclose()
        except Exception:
            pass
        service._store.close()


def submit_req(
    *,
    key: str | None = None,
    text: str = "do the thing",
    deadline_s: float = 600.0,
    task_id: str = "task-1",
) -> RunSubmit:
    return RunSubmit(
        task_id=task_id,
        workflow_id="wf-1",
        input_text=text,
        deadline_at=utc_now() + timedelta(seconds=deadline_s),
        operation_key=key or str(uuid.uuid4()),
        principal_id="planner-1",
    )


async def wait_for_state(service: RunService, run_id: str, state: RunState, timeout: float = 5.0):
    for _ in range(int(timeout / 0.01)):
        run = await service.get(run_id)
        if run.state is state:
            return run
        await asyncio.sleep(0.01)
    run = await service.get(run_id)
    pytest.fail(f"run {run_id} never reached {state.value}, now {run.state.value}")


async def test_same_key_same_body_returns_one_run_and_executes_once(make):
    service, fake = make()
    await service.start()
    request = submit_req()
    first = await service.submit(request)
    second = await service.submit(request)
    assert first.created and not second.created
    assert first.run.run_id == second.run.run_id
    run = await wait_for_state(service, first.run.run_id, RunState.SUCCEEDED)
    assert run.outcome is not None and run.outcome.code is OutcomeCode.SUCCEEDED
    assert fake.calls == [first.run.run_id]


async def test_concurrent_same_key_submissions_execute_once(make):
    service, fake = make()
    fake.gate = asyncio.Event()
    await service.start()
    request = submit_req()
    results = await asyncio.gather(service.submit(request), service.submit(request))
    assert {r.created for r in results} == {True, False}
    assert {r.run.run_id for r in results} == {results[0].run.run_id}
    fake.gate.set()
    run = await wait_for_state(service, results[0].run.run_id, RunState.SUCCEEDED)
    assert fake.calls == [run.run_id]


async def test_same_key_different_body_conflicts(make):
    service, _ = make()
    await service.start()
    key = str(uuid.uuid4())
    accepted = await service.submit(submit_req(key=key, text="original body"))
    with pytest.raises(IdempotencyConflictError):
        await service.submit(submit_req(key=key, text="changed body"))
    await wait_for_state(service, accepted.run.run_id, RunState.SUCCEEDED)


async def test_expired_deadline_same_key_still_returns_original_run(make):
    """After the fixed deadline passes, the same key and body still reconcile."""
    service, fake = make()
    fake.gate = asyncio.Event()  # hold the slot so a second run stays queued
    await service.start()
    holder = await service.submit(submit_req(key=str(uuid.uuid4()), task_id="holder"))
    await wait_for_state(service, holder.run.run_id, RunState.RUNNING)
    request = submit_req(deadline_s=0.3)
    accepted = await service.submit(request)
    run = await wait_for_state(service, accepted.run.run_id, RunState.FAILED)
    assert run.outcome is not None
    assert run.outcome.code is OutcomeCode.DEADLINE_BEFORE_START
    # The deadline in the replayed body is now in the past; reconciliation wins.
    replay = await service.submit(request)
    assert not replay.created
    assert replay.run.run_id == accepted.run.run_id
    assert replay.run.state is RunState.FAILED
    fake.gate.set()


async def test_input_size_limit_rejected_before_admission(make):
    service, fake = make()
    await service.start()
    with pytest.raises(InputTooLargeError):
        await service.submit(submit_req(text="x" * (16 * 1024 + 1)))
    with pytest.raises(InvalidSubmitError):
        await service.submit(submit_req(text="   "))
    with pytest.raises(InvalidSubmitError):
        await service.submit(submit_req(key="not-a-uuid"))
    assert fake.calls == []


async def test_queued_cancel_confirms_without_execution(make):
    service, fake = make()
    fake.gate = asyncio.Event()  # hold so the first run keeps the slot busy
    await service.start()
    first = await service.submit(submit_req(task_id="first"))
    await wait_for_state(service, first.run.run_id, RunState.RUNNING)
    second = await service.submit(submit_req(key=str(uuid.uuid4()), task_id="second"))
    assert (await service.get(second.run.run_id)).state is RunState.QUEUED
    cancelled = await service.cancel(second.run.run_id)
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.stop_confirmed is True
    assert cancelled.outcome is not None
    assert cancelled.outcome.code is OutcomeCode.CANCELLED
    assert second.run.run_id not in fake.calls
    fake.gate.set()
    done = await wait_for_state(service, first.run.run_id, RunState.SUCCEEDED)
    assert fake.calls == [done.run_id]


async def test_cancel_after_completion_wins_records_success(make):
    """Completion already committed: cancel must not rewrite it to cancelled."""
    service, _ = make()
    await service.start()
    accepted = await service.submit(submit_req())
    await wait_for_state(service, accepted.run.run_id, RunState.SUCCEEDED)
    after = await service.cancel(accepted.run.run_id)
    assert after.state is RunState.SUCCEEDED
    assert after.outcome is not None and after.outcome.code is OutcomeCode.SUCCEEDED


async def test_cancel_running_cancels_and_confirms_stop(make):
    service, fake = make()
    fake.gate = asyncio.Event()  # hold execution until cancelled
    await service.start()
    accepted = await service.submit(submit_req())
    await wait_for_state(service, accepted.run.run_id, RunState.RUNNING)
    cancelled = await service.cancel(accepted.run.run_id)
    assert cancelled.state is RunState.CANCELLED
    assert cancelled.stop_confirmed is True


async def test_cancel_resistant_run_keeps_slot_and_stays_unconfirmed(make):
    service, fake = make()
    fake.gate = asyncio.Event()
    fake.resist_cancel = True
    await service.start()
    stubborn = await service.submit(submit_req(task_id="stubborn"))
    await wait_for_state(service, stubborn.run.run_id, RunState.RUNNING)
    # The executor swallows cancellation and keeps working past the gate.
    result = await service.cancel(stubborn.run.run_id)
    assert result.state is RunState.CANCELLING
    assert result.stop_confirmed is False
    # Same instance must not start another run while the slot is held.
    queued = await service.submit(submit_req(key=str(uuid.uuid4()), task_id="second"))
    await asyncio.sleep(0.3)
    assert (await service.get(queued.run.run_id)).state is RunState.QUEUED
    # When the stubborn run finally stops, the slot frees and cancel confirms.
    fake.after_cancel_gate.set()
    finished = await wait_for_state(service, stubborn.run.run_id, RunState.CANCELLED, timeout=5)
    assert finished.stop_confirmed is True
    assert finished.outcome is not None and finished.outcome.code is OutcomeCode.CANCELLED
    await wait_for_state(service, queued.run.run_id, RunState.RUNNING)


async def test_typed_failures_provider_error_and_iteration_limit(make):
    service, fake = make()
    fake.result = ExecutionResult(code=OutcomeCode.PROVIDER_ERROR, text="half", message="provider 502")
    await service.start()
    accepted = await service.submit(submit_req())
    run = await wait_for_state(service, accepted.run.run_id, RunState.FAILED)
    assert run.outcome is not None
    assert run.outcome.code is OutcomeCode.PROVIDER_ERROR
    assert run.outcome.partial == "half"

    fake.result = ExecutionResult(code=OutcomeCode.ITERATION_LIMIT, text="", message="max iterations")
    second = await service.submit(submit_req(key=str(uuid.uuid4()), task_id="task-2"))
    run2 = await wait_for_state(service, second.run.run_id, RunState.FAILED)
    assert run2.outcome is not None and run2.outcome.code is OutcomeCode.ITERATION_LIMIT


async def test_execution_exception_maps_to_typed_execution_error(make):
    class ExplodingExecutor:
        async def execute(self, run):
            raise RuntimeError("boom")

    service, _ = make(executor=ExplodingExecutor())
    await service.start()
    accepted = await service.submit(submit_req())
    run = await wait_for_state(service, accepted.run.run_id, RunState.FAILED)
    assert run.outcome is not None
    assert run.outcome.code is OutcomeCode.EXECUTION_ERROR
    assert "RuntimeError" in run.outcome.message


async def test_execution_deadline_moves_to_cancelling_with_reason(make):
    service, fake = make()
    fake.gate = asyncio.Event()
    await service.start()
    accepted = await service.submit(submit_req(deadline_s=0.3))
    await wait_for_state(service, accepted.run.run_id, RunState.RUNNING)
    # The executor hangs; the deadline sweep must cancel with deadline_exceeded.
    run = await wait_for_state(service, accepted.run.run_id, RunState.CANCELLED, timeout=5)
    assert run.outcome is not None
    assert run.outcome.code is OutcomeCode.DEADLINE_EXCEEDED
    assert run.cancel_reason == "deadline_exceeded"


async def test_effects_uncertain_isolates_instance(make):
    service, fake = make()
    fake.result = ExecutionResult(code=OutcomeCode.SUCCEEDED, text="x", effects_uncertain=True)
    await service.start()
    accepted = await service.submit(submit_req())
    run = await wait_for_state(service, accepted.run.run_id, RunState.SUCCEEDED)
    assert run.effects_uncertain is True
    replay = await service.submit(replay_request(accepted.run))
    assert replay.created is False
    with pytest.raises(InstanceIsolatedError):
        await service.submit(submit_req(key=str(uuid.uuid4()), task_id="next"))
    service._store.clear_isolation("operator", "inspected, no stray processes")
    after = await service.submit(submit_req(key=str(uuid.uuid4()), task_id="next"))
    assert after.created


async def test_result_truncation_reports_original_bytes(make):
    text = "查" * 20000  # 3 bytes each: 60000 bytes, cap is 32768
    service, fake = make()
    fake.result = ExecutionResult(code=OutcomeCode.SUCCEEDED, text=text)
    await service.start()
    accepted = await service.submit(submit_req())
    run = await wait_for_state(service, accepted.run.run_id, RunState.SUCCEEDED)
    assert run.outcome is not None
    assert run.outcome.truncated is True
    assert run.outcome.partial_bytes == len(text.encode("utf-8"))
    assert len(run.outcome.partial.encode("utf-8")) <= 32768
    assert run.outcome.partial  # cut on a character boundary


async def test_events_stream_and_floor(tmp_path, make):
    service, _ = make()
    await service.start()
    accepted = await service.submit(submit_req())
    await wait_for_state(service, accepted.run.run_id, RunState.SUCCEEDED)
    events = await service.events(0)
    assert events, "expected at least creation events"
    assert events[-1].seq > events[0].seq
    fresh = await service.events(events[-1].seq)
    assert fresh == []
    # Backdate events past retention and run cleanup on the same store.
    service._store._conn.execute(
        "UPDATE run_events SET at = ?", ((utc_now() - timedelta(days=31)).isoformat(),)
    )
    service._store.cleanup()
    floor = service._store.events_floor()
    assert floor is not None and floor > 0
    with pytest.raises(EventCursorExpiredError):
        await service.events(0)


async def test_cleanup_tombstones_operation_keys_forever(tmp_path, make):
    service, _ = make()
    await service.start()
    accepted = await service.submit(submit_req())
    await wait_for_state(service, accepted.run.run_id, RunState.SUCCEEDED)
    store = service._store
    # Backdate past body retention first: bodies must be cleared, row kept.
    store._conn.execute(
        "UPDATE runs SET finished_at = ? WHERE run_id = ?",
        ((utc_now() - timedelta(days=15)).isoformat(), accepted.run.run_id),
    )
    counts = store.cleanup()
    assert counts["result_bodies"] == 1
    cleaned = store.get(accepted.run.run_id)
    assert cleaned is not None and cleaned.input_text == ""
    assert cleaned.outcome is not None and cleaned.outcome.partial == ""
    # Backdate past metadata retention: the row goes but the key is tombstoned.
    store._conn.execute(
        "UPDATE runs SET finished_at = ? WHERE run_id = ?",
        ((utc_now() - timedelta(days=91)).isoformat(), accepted.run.run_id),
    )
    counts = store.cleanup()
    assert counts["runs"] == 1
    assert store.get(accepted.run.run_id) is None
    with pytest.raises(OperationKeyRetiredError):
        store.submit(
            submit_req(key=accepted.run.operation_key),
            instance_id=store.instance_id_ro(),
            run_id="run_x",
            digest="d",
        )


def test_truncate_text_boundary():
    text = "ab😀cd"
    cut, truncated, size = truncate_text(text, 4)
    assert cut == "ab" and truncated is True and size == 8
    same, truncated, size = truncate_text("abc", 100)
    assert same == "abc" and truncated is False


async def test_q1a_b1_defaulted_deadline_replay_reconciles(make):
    """Same wire body without a deadline replays after the default was fixed."""
    service, fake = make()
    fake.gate = asyncio.Event()
    await service.start()
    holder = await service.submit(submit_req(key=str(uuid.uuid4()), task_id="holder"))
    await wait_for_state(service, holder.run.run_id, RunState.RUNNING)
    request = RunSubmit(
        task_id="replay",
        workflow_id="wf",
        input_text="same body",
        deadline_at=None,
        operation_key=str(uuid.uuid4()),
        principal_id="planner-1",
    )
    first = await service.submit(request)
    assert (await service.get(first.run.run_id)).state in (RunState.QUEUED, RunState.RUNNING)
    replay = await service.submit(request)
    assert replay.created is False
    assert replay.run.run_id == first.run.run_id


async def test_q1a_b2_cancel_racing_the_worker_pump_is_honored(make):
    """Cancel landing exactly as the worker starts the run still cancels."""
    service, fake = make()
    fake.gate = asyncio.Event()
    await service.start()
    accepted = await service.submit(submit_req())
    results = await asyncio.gather(
        service.cancel(accepted.run.run_id),
        service.cancel(accepted.run.run_id),
    )
    final = await wait_for_state(service, accepted.run.run_id, RunState.CANCELLED)
    assert final.cancel_requested_at is not None
    for result in results:
        assert result.run_id == accepted.run.run_id


async def test_q1a_b3_failed_result_commit_is_retried_not_reaped(make):
    """N10: a held result is never cancelled as an orphan and never re-run."""
    service, fake = make()
    fake.gate = asyncio.Event()  # hold the run so the write can be broken first
    fake.result = ExecutionResult(code=OutcomeCode.SUCCEEDED, text="PERSIST_ME")
    await service.start()
    accepted = await service.submit(submit_req())
    await wait_for_state(service, accepted.run.run_id, RunState.RUNNING)
    original = service._store.transition

    def broken(*args, **kwargs):
        from nanobot.runs.store import StoreUnavailableError

        raise StoreUnavailableError("disk full")

    service._store.transition = broken  # type: ignore[method-assign]
    fake.gate.set()
    for _ in range(300):
        if service._pending_results:
            break
        await asyncio.sleep(0.01)
    assert service._pending_results, "the exact result must be held in memory"
    for _ in range(10):
        await service._pump_once()
    held = await service.get(accepted.run.run_id)
    assert held.state is RunState.RUNNING, "a run with a pending result is not an orphan"
    assert held.stop_confirmed is False
    assert service._store.isolation() == (False, "")
    service._store.transition = original  # type: ignore[method-assign]
    await service._pump_once()
    final = await service.get(accepted.run.run_id)
    assert final.state is RunState.SUCCEEDED
    assert final.outcome is not None and final.outcome.partial == "PERSIST_ME"
    assert final.effects_uncertain is False
    assert fake.calls == [accepted.run.run_id], "the held result is never re-executed"


async def test_executor_exception_result_is_held_while_the_store_is_down(make):
    """N10: the exception exit path's typed result is held the same way."""

    class GatedExplodingExecutor:
        def __init__(self) -> None:
            self.gate = asyncio.Event()

        async def execute(self, run):
            await self.gate.wait()
            raise RuntimeError("boom")

    executor = GatedExplodingExecutor()
    service, _ = make(executor=executor)
    await service.start()
    accepted = await service.submit(submit_req(task_id="boom"))
    await wait_for_state(service, accepted.run.run_id, RunState.RUNNING)
    original = service._store.transition

    def broken(*args, **kwargs):
        from nanobot.runs.store import StoreUnavailableError

        raise StoreUnavailableError("disk full")

    service._store.transition = broken  # type: ignore[method-assign]
    executor.gate.set()
    for _ in range(300):
        if service._pending_results:
            break
        await asyncio.sleep(0.01)
    assert service._pending_results
    await service._pump_once()
    assert (await service.get(accepted.run.run_id)).state is RunState.RUNNING
    service._store.transition = original  # type: ignore[method-assign]
    await service._pump_once()
    final = await service.get(accepted.run.run_id)
    assert final.state is RunState.FAILED
    assert final.outcome is not None
    assert final.outcome.code is OutcomeCode.EXECUTION_ERROR
    assert "RuntimeError" in final.outcome.message
