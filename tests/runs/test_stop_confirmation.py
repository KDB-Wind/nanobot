"""R1 guards on the run layer: writer lock, stop truth, retention, reap.

Regression lock for the 2026-11-09 audit findings F04, F05, F06 (nanobot side),
and S01, S02. The audit reproducers live in nanobot-teams/review/async-20261109/.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from nanobot.agent.stop_report import SessionStopReport
from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore
from nanobot.runs.types import (
    ExecutionResult,
    OutcomeCode,
    RunState,
    RunSubmit,
    StoreUnavailableError,
    utc_now,
)
from runs.test_run_service import FakeExecutor


def _req(key: str | None = None) -> RunSubmit:
    return RunSubmit(
        task_id="t",
        workflow_id="w",
        input_text="body",
        deadline_at=utc_now() + timedelta(seconds=300),
        operation_key=key or str(uuid.uuid4()),
        principal_id="p",
    )


async def _wait_state(service: RunService, run_id: str, state: RunState):
    for _ in range(500):
        run = await service.get(run_id)
        if run.state is state:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {state}, got {run.state}")


async def test_second_writer_process_fails_before_reading_state(tmp_path):
    """F05: a second RunStore on the same database cannot open at all."""
    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    store.instance_id()
    competitor = RunStore(tmp_path / "runs.sqlite3")
    try:
        with pytest.raises(StoreUnavailableError):
            competitor.open()
        # Sequential owners are fine: close releases the OS lock.
        store.close()
        competitor.open()
        competitor.instance_id()
    finally:
        competitor.close()
        store.close()


async def test_recovery_isolates_and_blocks_queued_until_cleared(tmp_path):
    """F06: a restart finding an active run isolates the instance."""
    from nanobot.runs.store import canonical_request_digest

    db = tmp_path / "runs.sqlite3"
    store = RunStore(db)
    store.open()
    instance = store.instance_id()
    request = _req(str(uuid.uuid4()))
    store.submit(
        request, instance_id=instance, run_id="run_crash",
        digest=canonical_request_digest(request),
    )
    store.transition("run_crash", RunState.QUEUED, RunState.RUNNING)
    # A queued run predates the crash; admission is blocked once isolated, so
    # it must exist before the restart.
    queued_request = _req(str(uuid.uuid4()))
    store.submit(
        queued_request, instance_id=instance, run_id="run_queued",
        digest=canonical_request_digest(queued_request),
    )
    store.close()

    # The restart finds the running record and must isolate.
    store2 = RunStore(db)
    store2.open()
    executor = FakeExecutor()
    recovered = RunService(store2, executor)
    await recovered.start()
    isolated, reason = store2.isolation()
    assert isolated, "recovery must isolate after interrupting active runs"
    prior = await recovered.get("run_crash")
    assert prior.state is RunState.INTERRUPTED
    assert prior.effects_uncertain is True
    for _ in range(60):
        await asyncio.sleep(0.01)
        if (await recovered.get("run_queued")).state is not RunState.QUEUED:
            break
    assert (await recovered.get("run_queued")).state is RunState.QUEUED, (
        "queued work must wait for the operator while isolated"
    )
    assert executor.calls == []
    store2.clear_isolation(actor="operator", reason="effects reviewed")
    for _ in range(500):
        if (await recovered.get("run_queued")).state is RunState.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    assert (await recovered.get("run_queued")).state is RunState.SUCCEEDED
    await recovered.aclose()
    store2.close()


async def test_unconfirmed_cancel_stays_unconfirmed_and_isolates(tmp_path):
    """F04: an executor-reported unconfirmed stop is stored, not overwritten."""
    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    executor = FakeExecutor()
    executor.result = ExecutionResult(
        code=OutcomeCode.CANCELLED, text="partial", stop_confirmed=False
    )
    service = RunService(store, executor)
    await service.start()
    run = (await service.submit(_req())).run
    final = await _wait_state(service, run.run_id, RunState.CANCELLED)
    assert final.stop_confirmed is False
    isolated, reason = store.isolation()
    assert isolated, "an unproven stop must not present the instance as clean"
    await service.aclose()
    store.close()


async def test_retention_keeps_unconfirmed_stops(tmp_path):
    """S01: cleanup never deletes unverified evidence, regardless of age."""
    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    service = RunService(store, FakeExecutor())
    run = (await service.submit(_req())).run
    store.transition(run.run_id, RunState.QUEUED, RunState.RUNNING)
    store.recover()  # interrupted + effects_uncertain, no confirmed stop
    counts = store.cleanup(now=utc_now() + timedelta(days=100))
    kept = store.get(run.run_id)
    assert kept is not None, "an unconfirmed run must survive retention cleanup"
    assert counts["runs"] == 0
    assert counts["result_bodies"] == 0
    assert counts["events"] == 0, "the investigation log leaves only with its row"
    assert store.events_after(0) != [], "an unconfirmed run keeps its events"
    await service.aclose()
    store.close()


async def test_retention_still_deletes_confirmed_runs(tmp_path):
    """S01's guard must not freeze retention for ordinary confirmed runs."""
    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    service = RunService(store, FakeExecutor())
    await service.start()
    run = (await service.submit(_req())).run
    await _wait_state(service, run.run_id, RunState.SUCCEEDED)
    counts = store.cleanup(now=utc_now() + timedelta(days=100))
    assert store.get(run.run_id) is None
    assert counts["runs"] == 1
    await service.aclose()
    store.close()


class ReapingLoop:
    """Records session reaps; its report is the stop fact the executor trusts."""

    def __init__(
        self,
        *,
        reap_fails: bool = False,
        stop_reason: str = "completed",
        live_subagents: int = 0,
        live_exec_sessions: tuple[str, ...] = (),
    ) -> None:
        self.reap_fails = reap_fails
        self.stop_reason = stop_reason
        self.live_subagents = live_subagents
        self.live_exec_sessions = live_exec_sessions
        self.reaped: list[str] = []

    async def process_direct(self, *, content, session_key, hooks, **kwargs):
        class _Ctx:
            stop_reason = self.stop_reason
            error = None

        await hooks[0].after_run(_Ctx())

        class _Message:
            content = "answer"

        return _Message()

    async def stop_session_work(self, session_key: str) -> SessionStopReport:
        if self.reap_fails:
            raise RuntimeError("exec stop refused")
        self.reaped.append(session_key)
        return SessionStopReport(
            cancelled=0,
            live_subagents=self.live_subagents,
            live_exec_sessions=self.live_exec_sessions,
        )


def _run() -> object:
    from nanobot.runs.types import Run

    return Run(
        run_id="run_reap",
        instance_id="inst",
        principal_id="p",
        operation_key=str(uuid.uuid4()),
        task_id="t",
        workflow_id="w",
        input_text="job",
        request_digest="d",
        deadline_at=utc_now(),
        state=RunState.RUNNING,
        revision=1,
    )


async def test_success_reaps_session_background_work():
    """F04: a finished run stops its own tools and exec sessions."""
    loop = ReapingLoop()
    executor = AgentLoopRunExecutor(loop)
    result = await executor.execute(_run())
    assert result.code is OutcomeCode.SUCCEEDED
    assert result.stop_confirmed is True
    assert loop.reaped == ["run:run_reap"], (
        "every terminal path must reap the run's background work"
    )


async def test_failed_reap_reports_unverified_stop():
    """F04: cleanup failure is never reported as a clean finish."""
    loop = ReapingLoop(reap_fails=True)
    executor = AgentLoopRunExecutor(loop)
    result = await executor.execute(_run())
    assert result.stop_confirmed is False
    assert result.effects_uncertain is True


async def test_live_work_in_the_report_reports_unverified_stop():
    """N06: a bounded wait that left work alive is not a confirmed stop."""
    loop = ReapingLoop(live_subagents=1)
    executor = AgentLoopRunExecutor(loop)
    result = await executor.execute(_run())
    assert result.code is OutcomeCode.SUCCEEDED
    assert result.stop_confirmed is False
    assert result.effects_uncertain is True


async def test_empty_final_response_is_a_typed_failure():
    """S02: the fallback text is not a success."""
    loop = ReapingLoop(stop_reason="empty_final_response")
    executor = AgentLoopRunExecutor(loop)
    result = await executor.execute(_run())
    assert result.code is OutcomeCode.EXECUTION_ERROR
