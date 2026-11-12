"""A18: a real loop user turn revokes an older workflow's grant (regression)."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.providers.unconfigured_provider import UnconfiguredProvider
from nanobot.session.external_inbox import ExternalInbox
from nanobot.session.manager import SessionManager


@pytest.fixture
async def harness(tmp_path):
    session_manager = SessionManager(tmp_path / "sessions-root")
    bus = MessageBus()
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
    provider = UnconfiguredProvider(default_model="test-model")
    loop = AgentLoop.from_config(
        config, bus,
        provider=provider,
        session_manager=session_manager,
        tool_registry=ToolRegistry(),
    )
    inbox = ExternalInbox(session_manager.state)
    loop.external_inbox = inbox  # what the gateway assembly does
    yield loop, inbox, session_manager
    await loop.aclose()


async def _user_turn(loop, content: str) -> None:
    message = InboundMessage(
        channel="websocket",
        chat_id="webui-default",
        sender_id="user",
        content=content,
        input_role="user",
    )
    await loop._dispatch_one(message, asyncio.Queue())


async def test_user_turn_revokes_minted_workflow(harness):
    loop, inbox, session_manager = harness
    session_key = "websocket:webui-default"
    await _user_turn(loop, "Say OK1 and nothing else.")
    inbox.mint_capability(
        session_key=session_key,
        session_generation=(await session_manager.state.read(session_key)).generation,
        workflow_id="wf-proc",
        operation_key="*",
        bot_id="*",
        instance_id="*",
        principal_id="teams-coordinator",
    )
    assert inbox.workflow_allows_auto_continue("wf-proc") is True
    await _user_turn(loop, "Say OK2 and nothing else.")
    assert inbox.workflow_allows_auto_continue("wf-proc") is False
