"""Typed stop report: only observed live state confirms a session stop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.agent.stop_report import SessionStopReport
from nanobot.bus.queue import MessageBus


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.aclose = AsyncMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096, temperature=0.1, reasoning_effort=None
    )
    return provider


def _loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=4096,
    )


def test_confirmed_requires_no_live_subagent_and_no_live_exec_session():
    assert SessionStopReport(cancelled=0, live_subagents=0, live_exec_sessions=()).confirmed
    assert not SessionStopReport(cancelled=1, live_subagents=1, live_exec_sessions=()).confirmed
    assert not SessionStopReport(cancelled=1, live_subagents=0, live_exec_sessions=("e",)).confirmed


async def test_stop_session_work_reports_live_state_and_keeps_the_count_api(tmp_path):
    loop = _loop(tmp_path)
    try:
        loop._cancel_active_tasks = AsyncMock(return_value=3)  # type: ignore[method-assign]
        loop.subagents.get_running_count_by_session = lambda key: 1  # type: ignore[method-assign]
        loop._exec_session_manager.list = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                SimpleNamespace(session_id="exec_live", returncode=None),
                SimpleNamespace(session_id="exec_done", returncode=0),
            ]
        )
        report = await loop.stop_session_work("run:x")
        assert report.cancelled == 3
        assert report.live_subagents == 1
        assert report.live_exec_sessions == ("exec_live",)
        assert report.confirmed is False
        # Existing callers keep the integer contract.
        assert await loop.cancel_session_work("run:x") == 3
        assert loop._cancel_active_tasks.await_count == 2
    finally:
        await loop.aclose()


async def test_stop_session_work_is_confirmed_when_nothing_is_alive(tmp_path):
    loop = _loop(tmp_path)
    try:
        loop._cancel_active_tasks = AsyncMock(return_value=0)  # type: ignore[method-assign]
        loop.subagents.get_running_count_by_session = lambda key: 0  # type: ignore[method-assign]
        loop._exec_session_manager.list = AsyncMock(  # type: ignore[method-assign]
            return_value=[SimpleNamespace(session_id="exec_done", returncode=0)]
        )
        report = await loop.stop_session_work("run:x")
        assert report.confirmed is True
        assert report.live_exec_sessions == ()
    finally:
        await loop.aclose()
