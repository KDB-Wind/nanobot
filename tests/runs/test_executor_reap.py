"""Real exec-session cleanup on the executor's exception exit path (N06)."""

from __future__ import annotations

import sys

from nanobot.agent.hook import AgentHook
from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.types import OutcomeCode, Run, RunState, utc_now


class _ExecThenFailHook(AgentHook):
    """Snapshot the run's live exec sessions, then fail the run."""

    def __init__(self) -> None:
        super().__init__(reraise=True)
        self.manager = None
        self.captured: list = []

    async def after_run(self, context) -> None:
        if self.manager is not None:
            self.captured = [
                (session.session_id, session.process)
                for session in list(self.manager._sessions.values())
            ]
        raise RuntimeError("integration hook failed after exec")


class _ExecProvider(LLMProvider):
    """First turn starts a long exec session; later turns answer."""

    def __init__(self) -> None:
        super().__init__(provider_name="offline-reap")
        self.calls = 0

    def get_default_model(self) -> str:
        return "offline-reap"

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "sleep",
                        "exec",
                        {
                            "command": (
                                f'& "{sys.executable}" -c "import time; time.sleep(60)"'
                            ),
                            "shell": "pwsh",
                            "login": False,
                            "yield_time_ms": 0,
                        },
                    )
                ],
            )
        return LLMResponse(content="Done")

    async def chat_stream(self, messages, tools=None, **kwargs) -> LLMResponse:
        return await self.chat(messages, tools=tools, **kwargs)


def _run(run_id: str) -> Run:
    return Run(
        run_id=run_id,
        instance_id="inst",
        principal_id="p",
        operation_key="00000000-0000-4000-8000-000000000000",
        task_id="t",
        workflow_id="w",
        input_text="start a long job",
        request_digest="d",
        deadline_at=utc_now(),
        state=RunState.RUNNING,
        revision=1,
    )


async def test_exception_after_exec_kills_the_child_process(tmp_path):
    """N06: process_direct raising must not skip the reap of a live child."""
    provider = _ExecProvider()
    hook = _ExecThenFailHook()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path / "ws",
        max_iterations=3,
        hooks=[hook],
    )
    hook.manager = loop._exec_session_manager
    try:
        result = await AgentLoopRunExecutor(loop).execute(_run("run_reap_exception"))
        assert result.code is OutcomeCode.EXECUTION_ERROR
        assert hook.captured, "the run must have owned a live exec session"
        assert all(process.returncode is not None for _sid, process in hook.captured), (
            "the exception exit path must terminate the spawned process"
        )
        assert result.stop_confirmed is True
        assert result.effects_uncertain is False
        assert await loop._exec_session_manager.list(
            owner_session_key="run:run_reap_exception"
        ) == []
    finally:
        await loop.aclose()
