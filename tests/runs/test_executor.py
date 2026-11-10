"""AgentLoop adapter contract: typed outcomes and real stop propagation."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.stop_report import SessionStopReport
from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.types import OutcomeCode, Run, RunState, utc_now


class StubLoop:
    """Minimal AgentLoop surface the executor relies on."""

    def __init__(
        self,
        *,
        stop_reason="completed",
        error=None,
        content="answer",
        raise_exc=None,
        live_subagents=0,
        live_exec_sessions=(),
    ):
        self.stop_reason = stop_reason
        self.error = error
        self.content = content
        self.raise_exc = raise_exc
        self.live_subagents = live_subagents
        self.live_exec_sessions = tuple(live_exec_sessions)
        self.calls: list[dict] = []
        self.cancelled_sessions: list[str] = []
        self.entered = asyncio.Event()
        self._cancel_on_cancel = asyncio.Event()

    async def process_direct(self, *, content, session_key, channel, chat_id, hooks, **kwargs):
        self.calls.append(
            {
                "content": content,
                "session_key": session_key,
                "ephemeral": kwargs.get("ephemeral"),
                "extra_hooks": kwargs.get("_run_extra_hooks_for_ephemeral"),
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        hook = hooks[0]

        class _Ctx:
            stop_reason = self.stop_reason
            error = self.error

        await hook.after_run(_Ctx())

        class _Message:
            content_ = self.content

            @property
            def content(self):
                return self.content_

        return _Message()

    async def stop_session_work(self, session_key: str) -> SessionStopReport:
        self.cancelled_sessions.append(session_key)
        return SessionStopReport(
            cancelled=0,
            live_subagents=self.live_subagents,
            live_exec_sessions=self.live_exec_sessions,
        )


def make_run() -> Run:
    return Run(
        run_id="run_x",
        instance_id="inst_x",
        principal_id="p",
        operation_key=str(__import__("uuid").uuid4()),
        task_id="t",
        workflow_id="w",
        input_text="job body",
        request_digest="d",
        deadline_at=utc_now(),
        state=RunState.RUNNING,
        revision=1,
    )


async def test_success_maps_to_succeeded_with_text():
    loop = StubLoop(stop_reason="completed", content="final answer")
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.SUCCEEDED
    assert result.text == "final answer"
    assert loop.calls[0]["session_key"] == "run:run_x"
    assert loop.calls[0]["ephemeral"] is True
    assert loop.calls[0]["extra_hooks"] is True
    assert loop.calls[0]["content"] == "job body"


async def test_provider_error_and_iteration_limit_map_typed():
    loop = StubLoop(stop_reason="error", error="provider 502", content="partial")
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.PROVIDER_ERROR
    assert result.message == "provider 502"
    assert result.text == "partial"

    loop = StubLoop(stop_reason="max_iterations", error=None, content="halfway")
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.ITERATION_LIMIT


async def test_unknown_stop_reason_maps_to_execution_error():
    loop = StubLoop(stop_reason="something_new")
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.EXECUTION_ERROR
    assert "something_new" in result.message


async def test_unexpected_exception_maps_to_execution_error():
    loop = StubLoop(raise_exc=RuntimeError("boom"))
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.EXECUTION_ERROR
    assert "RuntimeError" in result.message


async def test_cancellation_terminates_session_work_and_reraises():
    class CancellingLoop(StubLoop):
        async def process_direct(self, **kwargs):
            self.entered.set()
            await asyncio.Event().wait()  # hang until cancelled

    loop = CancellingLoop()
    executor = AgentLoopRunExecutor(loop)
    task = asyncio.create_task(executor.execute(make_run()))
    await asyncio.wait_for(loop.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.cancelled_sessions == ["run:run_x"]
    assert executor.consume_cancel_unverified() is False


async def test_live_work_in_stop_report_downgrades_a_clean_finish():
    """N06: the report, not the call returning, decides stop confirmation."""
    loop = StubLoop(content="answer", live_exec_sessions=("exec_still_running",))
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.SUCCEEDED
    assert result.text == "answer"
    assert result.stop_confirmed is False
    assert result.effects_uncertain is True


async def test_exception_path_reaps_and_reports_the_live_state():
    """N06: the exception exit path owns the tools the run already started."""
    loop = StubLoop(raise_exc=RuntimeError("boom"), live_subagents=1)
    result = await AgentLoopRunExecutor(loop).execute(make_run())
    assert result.code is OutcomeCode.EXECUTION_ERROR
    assert loop.cancelled_sessions == ["run:run_x"]
    assert result.stop_confirmed is False
    assert result.effects_uncertain is True


async def test_cancelled_run_sets_unverified_flag_from_the_report():
    """N06: a bounded wait that left work alive is an unverified stop."""

    class CancellingLoop(StubLoop):
        async def process_direct(self, **kwargs):
            self.entered.set()
            await asyncio.Event().wait()

    loop = CancellingLoop(live_subagents=1)
    executor = AgentLoopRunExecutor(loop)
    task = asyncio.create_task(executor.execute(make_run()))
    await asyncio.wait_for(loop.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert executor.consume_cancel_unverified() is True
    assert executor.consume_cancel_unverified() is False, "the flag is one-shot"
