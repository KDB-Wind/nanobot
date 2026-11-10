"""Peer-capable runs bind a durable conversation context (P5).

A run keeps its ephemeral working session, but when the host has the runs
inbox enabled the executor binds a RequestContext for the durable
``peer:<workflow>`` conversation session so send_to_bot mints capabilities
that survive the run and can wake the conversation later.
"""

from __future__ import annotations

from nanobot.agent.tools.context import current_request_context
from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.types import Run, RunState
from nanobot.session.manager import Session

pytest_plugins = ("pytest_asyncio",)


class _Msg:
    content = "done"


class _StopReport:
    confirmed = True


class _State:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def get(self, key: str) -> Session:
        self.created.append(key)
        return Session(key=key)


class _Sessions:
    def __init__(self) -> None:
        self.state = _State()


class _Loop:
    def __init__(self) -> None:
        self.sessions = _Sessions()
        self.seen_contexts: list[object] = []
        self.calls: list[dict] = []

    async def process_direct(self, **kwargs):
        self.calls.append(kwargs)
        self.seen_contexts.append(current_request_context())
        return _Msg()

    async def stop_session_work(self, session_key: str) -> _StopReport:
        return _StopReport()


def make_run(workflow: str = "wf-conv") -> Run:
    from datetime import datetime, timezone

    return Run(
        run_id="run_x", instance_id="inst", principal_id="p",
        operation_key="k", task_id="t", workflow_id=workflow,
        input_text="do", request_digest="d",
        deadline_at=datetime.now(timezone.utc), state=RunState.SUCCEEDED,
        revision=1,
    )


async def test_durable_context_bound_during_execution():
    loop = _Loop()
    executor = AgentLoopRunExecutor(loop, durable_peer_sessions=True)
    result = await executor.execute(make_run("wf-conv"))
    assert result is not None
    assert loop.sessions.state.created == ["peer:wf-conv"]
    ctx = loop.seen_contexts[0]
    assert ctx is not None
    assert ctx.session_key == "peer:wf-conv"
    assert ctx.source_workflow == "wf-conv"
    # The run session itself stays ephemeral.
    assert loop.calls[0]["ephemeral"] is True


async def test_no_durable_context_without_the_flag():
    loop = _Loop()
    executor = AgentLoopRunExecutor(loop)
    await executor.execute(make_run("wf-conv"))
    assert loop.sessions.state.created == []
    assert loop.seen_contexts[0] is None


async def test_context_reset_after_execution():
    loop = _Loop()
    executor = AgentLoopRunExecutor(loop, durable_peer_sessions=True)
    await executor.execute(make_run())
    assert current_request_context() is None
