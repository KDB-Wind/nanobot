"""AgentLoop adapter contract: typed outcomes and real stop propagation."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.types import OutcomeCode, Run, RunState, utc_now


class StubLoop:
    """Minimal AgentLoop surface the executor relies on."""

    def __init__(self, *, stop_reason="completed", error=None, content="answer", raise_exc=None):
        self.stop_reason = stop_reason
        self.error = error
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.cancelled_sessions: list[str] = []
        self._cancel_on_cancel = asyncio.Event()

    async def process_direct(self, *, content, session_key, channel, chat_id, hooks, **kwargs):
        self.calls.append(
            {"content": content, "session_key": session_key, "ephemeral": kwargs.get("ephemeral")}
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

    async def cancel_session_work(self, session_key: str) -> int:
        self.cancelled_sessions.append(session_key)
        return 0


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
            await asyncio.Event().wait()  # hang until cancelled

    loop = CancellingLoop()
    with pytest.raises((asyncio.CancelledError, TimeoutError)):
        await asyncio.wait_for(AgentLoopRunExecutor(loop).execute(make_run()), timeout=0.5)
    assert loop.cancelled_sessions == ["run:run_x"]
