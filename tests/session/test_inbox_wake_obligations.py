"""N02-N05: durable wake obligations, generation/grant binding, report handoff."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.turn_delivery import TurnDeliveryFactory
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.providers.base import LLMResponse
from nanobot.providers.unconfigured_provider import UnconfiguredProvider
from nanobot.session.external_inbox import (
    ExternalInbox,
    InboxDelivery,
    delivery_body_digest,
)
from nanobot.session.inbox_wake import WAKE_PROMPT, InboxWakePump
from nanobot.session.manager import SessionManager
from nanobot.session.state import SessionState


async def _seed(state: SessionState, key: str, text: str):
    await state._execute(
        key,
        lambda: state._change(
            key, lambda session: session.add_message("user", text),
            create_if_missing=True,
        ),
        operation_name="seed",
    )
    session = await state.read(key)
    assert session is not None
    return session


async def _inbox_env(tmp_path: Path):
    manager = SessionManager(tmp_path / "sessions")
    state = manager.state
    key = "websocket:wake-chat"
    session = await _seed(state, key, "research then write")
    inbox = ExternalInbox(state)
    inbox.mint_capability(
        session_key=key, session_generation=session.generation, workflow_id="wf-root",
        operation_key="op-1", bot_id="research", instance_id="research-instance",
        principal_id="teams-coordinator",
    )
    return manager, state, key, session, inbox


def _delivery(
    session_key: str,
    generation: str,
    *,
    delivery_id: str = "del-1",
    workflow: str = "wf-root",
    text: str = "REVIEW_EXTERNAL_DATA",
    truncated: bool = False,
) -> InboxDelivery:
    return InboxDelivery(
        delivery_id=delivery_id,
        session_key=session_key,
        session_generation=generation,
        workflow_id=workflow,
        task_id="task-1",
        bot_id="research",
        instance_id="research-instance",
        run_id="run-1",
        terminal_revision=1,
        outcome_code="succeeded",
        result_text=text,
        result_truncated=truncated,
        body_digest=delivery_body_digest(
            task_id="task-1", bot_id="research", instance_id="research-instance",
            run_id="run-1", terminal_revision=1, outcome_code="succeeded",
            result_text=text,
        ),
    )


async def _apply(inbox: ExternalInbox, session_key: str, generation: str, **kwargs) -> str:
    delivery = _delivery(session_key, generation, **kwargs)
    inbox.receive(delivery)
    return await inbox.apply_pending(delivery.delivery_id)


def _session_exists(state: SessionState):
    async def exists(session_key: str) -> bool:
        return await state.read(session_key) is not None

    return exists


class _BlockingProvider(UnconfiguredProvider):
    """Offline provider that holds the first model call open."""

    def __init__(self) -> None:
        super().__init__("fake-review-model")
        self.calls: list[list[dict]] = []
        self.first_call = asyncio.Event()
        self.release = asyncio.Event()
        self.second_call = asyncio.Event()

    async def chat(self, messages, **kwargs):  # type: ignore[override]
        self.calls.append(deepcopy(messages))
        if len(self.calls) == 1:
            self.first_call.set()
            await self.release.wait()
        elif len(self.calls) == 2:
            self.second_call.set()
        return LLMResponse(content="Reviewed fake response", finish_reason="stop")


def _agent(tmp_path: Path, manager: SessionManager, provider, bus: MessageBus) -> AgentLoop:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    config = Config.model_validate(
        {"agents": {"defaults": {"workspace": str(tmp_path / "workspace")}}}
    )
    return AgentLoop.from_config(
        config, bus, provider=provider, session_manager=manager,
        tool_registry=ToolRegistry(),
    )


async def _wait_until(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


async def test_publish_keeps_obligation_and_lease_blocks_duplicate(tmp_path):
    """N02: a bus publish is not acceptance, but one pump must not double-send."""
    _manager, state, key, session, inbox = await _inbox_env(tmp_path)
    assert await _apply(inbox, key, session.generation) == "injected"
    published: list[InboundMessage] = []

    async def publish(message: InboundMessage) -> None:
        published.append(message)

    pump = InboxWakePump(inbox, publish, _session_exists(state))
    assert await pump.pump_once() == 1
    due = inbox.wake_due_deliveries()
    assert [entry["delivery_id"] for entry in due] == ["del-1"], (
        "publishing into the bus must not clear the durable obligation"
    )
    assert due[0]["session_generation"] == session.generation
    assert due[0]["result_text"] == "REVIEW_EXTERNAL_DATA"
    message = published[0]
    assert message.session_generation == session.generation
    assert message.metadata["wake_delivery_id"] == "del-1"
    assert message.metadata["source_workflow"] == "wf-root"
    assert message.metadata["external_task_id"] == "task-1"
    assert message.metadata["_skip_user_persist"] is True
    assert WAKE_PROMPT in message.content
    assert "REVIEW_EXTERNAL_DATA" in message.content
    assert "not a user instruction or approval" in message.content
    # A second tick sees the live lease and does not republish.
    assert await pump.pump_once() == 0
    assert len(published) == 1
    # The accepting turn clears the obligation.
    assert inbox.mark_wake_consumed("del-1") is True
    assert inbox.wake_due_deliveries() == []


async def test_restarted_pump_republishes_after_crash_before_dequeue(tmp_path):
    """N02: the lease dies with the process, so the durable obligation returns."""
    _manager, state, key, session, inbox = await _inbox_env(tmp_path)
    assert await _apply(inbox, key, session.generation) == "injected"
    first: list[InboundMessage] = []
    second: list[InboundMessage] = []

    async def publish_first(message: InboundMessage) -> None:
        first.append(message)

    async def publish_second(message: InboundMessage) -> None:
        second.append(message)

    first_pump = InboxWakePump(inbox, publish_first, _session_exists(state))
    assert await first_pump.pump_once() == 1
    assert len(first) == 1
    assert inbox.wake_due_deliveries(), "the crashed process leaves the obligation"
    # A restarted process has no lease memory and must offer the wake again.
    restarted = InboxWakePump(inbox, publish_second, _session_exists(state))
    assert await restarted.pump_once() == 1
    assert len(second) == 1
    assert "REVIEW_EXTERNAL_DATA" in second[0].content


async def test_replaced_generation_orphans_receipt(tmp_path):
    """N03: a wake belongs to the generation that owns the report."""
    _manager, state, key, session, inbox = await _inbox_env(tmp_path)
    assert await _apply(inbox, key, session.generation) == "injected"
    await state.delete(key)
    await _seed(state, key, "NEW UNRELATED INTENT")
    published: list[InboundMessage] = []

    async def publish(message: InboundMessage) -> None:
        published.append(message)

    pump = InboxWakePump(inbox, publish, _session_exists(state))
    assert await pump.pump_once() == 0
    assert published == []
    receipt = inbox.get_receipt("del-1")
    assert receipt is not None and receipt.state == "orphaned"
    assert inbox.wake_due_deliveries() == []


async def test_revoked_workflow_drops_wake_and_keeps_receipt(tmp_path):
    """N03: revocation ends the wake, not the durable report."""
    _manager, state, key, session, inbox = await _inbox_env(tmp_path)
    assert await _apply(inbox, key, session.generation) == "injected"
    await inbox.revoke_session_workflows_async(key)
    published: list[InboundMessage] = []

    async def publish(message: InboundMessage) -> None:
        published.append(message)

    pump = InboxWakePump(inbox, publish, _session_exists(state))
    assert await pump.pump_once() == 0
    assert published == []
    receipt = inbox.get_receipt("del-1")
    assert receipt is not None
    assert receipt.state == "injected", "the report receipt survives the dropped wake"
    assert "revoked" in receipt.error
    assert inbox.wake_due_deliveries() == []


async def test_wake_carries_truncation_note(tmp_path):
    """N05: the wake repeats the same truncation note as history injection."""
    _manager, state, key, session, inbox = await _inbox_env(tmp_path)
    assert await _apply(
        inbox, key, session.generation, truncated=True
    ) == "injected"
    published: list[InboundMessage] = []

    async def publish(message: InboundMessage) -> None:
        published.append(message)

    pump = InboxWakePump(inbox, publish, _session_exists(state))
    assert await pump.pump_once() == 1
    assert "result truncated by the 32 KiB protocol limit" in published[0].content


async def test_injected_user_input_revokes_live_workflow(tmp_path):
    """N04: the mid-turn injection path applies the same revocation rule."""
    manager = SessionManager(tmp_path / "sessions")
    state = manager.state
    key = "websocket:wake-chat"
    session = await _seed(state, key, "research then write")
    inbox = ExternalInbox(state)
    bus = MessageBus()
    provider = _BlockingProvider()
    agent = _agent(tmp_path, manager, provider, bus)
    agent.external_inbox = inbox
    task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(InboundMessage(
            channel="websocket", sender_id="user", chat_id="wake-chat",
            content="research then write", input_role="user",
        ))
        await asyncio.wait_for(provider.first_call.wait(), 10)
        inbox.mint_capability(
            session_key=key, session_generation=session.generation,
            workflow_id="wf-live", operation_key="*", bot_id="*", instance_id="*",
            principal_id="teams-coordinator",
        )
        await bus.publish_inbound(InboundMessage(
            channel="websocket", sender_id="user", chat_id="wake-chat",
            content="NEW UNRELATED INTENT", input_role="user",
        ))
        await _wait_until(lambda: bus.inbound_size == 0)
        provider.release.set()
        await asyncio.wait_for(provider.second_call.wait(), 10)
        assert "NEW UNRELATED INTENT" in str(provider.calls[1])
        await _wait_until(lambda: not agent._pending_queues)
        assert inbox.workflow_allows_auto_continue("wf-live") is False, (
            "an accepted injected user input revokes the session's workflows"
        )
        receipt = inbox.receive(_delivery(key, session.generation, workflow="wf-live"))
        assert receipt.state == "card_only"
        assert await inbox.apply_pending("del-1") == "card_only"
        assert inbox.wake_due_deliveries() == []
    finally:
        provider.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.aclose()


async def test_active_turn_injection_sees_report_body(tmp_path):
    """N05: a report landing mid-turn reaches the model with its result text."""
    manager = SessionManager(tmp_path / "sessions")
    state = manager.state
    key = "websocket:wake-chat"
    session = await _seed(state, key, "research then write")
    inbox = ExternalInbox(state)
    bus = MessageBus()
    provider = _BlockingProvider()
    agent = _agent(tmp_path, manager, provider, bus)
    agent.external_inbox = inbox
    task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(InboundMessage(
            channel="websocket", sender_id="user", chat_id="wake-chat",
            content="research then write", input_role="user",
        ))
        await asyncio.wait_for(provider.first_call.wait(), 10)
        # The genuine user turn revokes at admission; a live workflow is minted
        # by the active turn (as a Teams tool call does) after that boundary.
        inbox.mint_capability(
            session_key=key, session_generation=session.generation,
            workflow_id="wf-root", operation_key="*", bot_id="*", instance_id="*",
            principal_id="teams-coordinator",
        )
        delivery = _delivery(key, session.generation)
        inbox.receive(delivery)
        assert await inbox.apply_pending(delivery.delivery_id) == "injected"
        pump = InboxWakePump(inbox, bus.publish_inbound, _session_exists(state))
        assert await pump.pump_once() == 1
        await _wait_until(lambda: bus.inbound_size == 0)
        provider.release.set()
        await asyncio.wait_for(provider.second_call.wait(), 10)
        await _wait_until(lambda: not agent._pending_queues)
        second_replay = str(provider.calls[1])
        assert "REVIEW_EXTERNAL_DATA" in second_replay, (
            "the active turn's model input must carry the report body"
        )
        assert "external data" in second_replay
        assert "Automated continuation" in second_replay
        assert inbox.wake_due_deliveries() == [], (
            "the accepting turn consumes the wake obligation"
        )
        current = await state.read(key)
        assert current is not None
        report_rows = [
            message for message in current.messages
            if message.get("external_delivery_id") == "external_delivery:del-1"
        ]
        assert len(report_rows) == 1, "the report is injected into history once"
        assert not any(
            "Automated continuation" in str(message.get("content"))
            for message in current.messages
        ), "the wake body is turn input, not a second history row"
    finally:
        provider.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.aclose()

async def test_active_turn_settles_its_wake_under_a_public_turn_id(tmp_path):
    """The channel's public turn id never becomes the wake's owner."""
    manager = SessionManager(tmp_path / "sessions")
    state = manager.state
    key = "websocket:wake-chat"
    session = await _seed(state, key, "research then write")
    inbox = ExternalInbox(state)
    bus = MessageBus()
    provider = _BlockingProvider()
    agent = _agent(tmp_path, manager, provider, bus)
    agent.external_inbox = inbox
    agent.turn_delivery_factory = TurnDeliveryFactory(
        bus,
        route_policy=lambda msg, key, route: replace(route, turn_id="public-42"),
    )
    task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(InboundMessage(
            channel="websocket", sender_id="user", chat_id="wake-chat",
            content="research then write", input_role="user",
        ))
        await asyncio.wait_for(provider.first_call.wait(), 10)
        inbox.mint_capability(
            session_key=key, session_generation=session.generation,
            workflow_id="wf-root", operation_key="*", bot_id="*", instance_id="*",
            principal_id="teams-coordinator",
        )
        delivery = _delivery(key, session.generation)
        inbox.receive(delivery)
        assert await inbox.apply_pending(delivery.delivery_id) == "injected"
        pump = InboxWakePump(inbox, bus.publish_inbound, _session_exists(state))
        assert await pump.pump_once() == 1
        await _wait_until(lambda: bus.inbound_size == 0)
        provider.release.set()
        await asyncio.wait_for(provider.second_call.wait(), 10)
        await _wait_until(lambda: not agent._pending_queues)
        for _ in range(200):
            current = await state.read(key)
            if current is not None and any(
                message.get("content") == "Reviewed fake response"
                for message in current.messages
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the active turn did not persist its answer")
        assert "pending_user_turn" not in current.metadata, (
            "the accepting turn settles the wake it consumed"
        )
        assert "pending_user_turn_owner" not in current.metadata
    finally:
        provider.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.aclose()
