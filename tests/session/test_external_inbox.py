"""External inbox domain: capability binding, atomic injection, revocation (P4)."""

from __future__ import annotations

import pytest

from nanobot.session.external_inbox import (
    ExternalInbox,
    InboxDelivery,
    InboxError,
    delivery_body_digest,
)
from nanobot.session.manager import SessionManager


@pytest.fixture
async def harness(tmp_path):
    manager = SessionManager(tmp_path)
    state = manager.state
    inbox = ExternalInbox(state)
    session_key = "planner-chat"
    await state._execute(
        session_key,
        lambda: state._change(
            session_key, lambda s: s.add_message("user", "plan something"),
            create_if_missing=True,
        ),
        operation_name="seed",
    )
    session = await state.read(session_key)
    assert session is not None
    yield inbox, state, session_key, session.generation


def make_delivery(session_key, generation, *, delivery_id="del_1", workflow="wf-1",
                  task="task_1", text="the findings", revision=3,
                  outcome="succeeded") -> InboxDelivery:
    return InboxDelivery(
        delivery_id=delivery_id,
        session_key=session_key,
        session_generation=generation,
        workflow_id=workflow,
        task_id=task,
        bot_id="research",
        instance_id="inst_r",
        run_id="run_1",
        terminal_revision=revision,
        outcome_code=outcome,
        result_text=text,
        result_truncated=False,
        body_digest=delivery_body_digest(
            task_id=task, bot_id="research", instance_id="inst_r", run_id="run_1",
            terminal_revision=revision, outcome_code=outcome, result_text=text,
        ),
    )


async def test_capability_binds_origin_and_rejects_mismatches(harness):
    inbox, state, key, generation = harness
    capability = inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-1",
        operation_key="op-1", bot_id="research", instance_id="inst_r",
        principal_id="coordinator",
    )
    assert inbox.workflow_allows_auto_continue("wf-1")
    validated = inbox.validate_capability(
        token=capability.token, workflow_id="wf-1", operation_key="op-1",
        bot_id="research", instance_id="inst_r", principal_id="coordinator",
    )
    assert validated.session_key == key
    base = dict(
        token=capability.token, workflow_id="wf-1", operation_key="op-1",
        bot_id="research", instance_id="inst_r", principal_id="coordinator",
    )
    for field, bad in (
        ("workflow_id", "wf-2"),
        ("operation_key", "op-2"),
        ("bot_id", "writer"),
        ("principal_id", "someone-else"),
    ):
        with pytest.raises(InboxError) as exc:
            inbox.validate_capability(**{**base, field: bad})
        assert exc.value.code == "capability_binding_mismatch"
    with pytest.raises(InboxError) as exc:
        inbox.validate_capability(
            token="forged-token", workflow_id="wf-1", operation_key="op-1",
            bot_id="research", instance_id="inst_r", principal_id="coordinator",
        )
    assert exc.value.code == "origin_required"


async def test_receive_then_inject_once_and_idempotent_replay(harness):
    inbox, state, key, generation = harness
    inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-1",
        operation_key="op-1", bot_id="research", instance_id="inst_r",
        principal_id="coordinator",
    )
    delivery = make_delivery(key, generation)
    receipt = inbox.receive(delivery)
    assert receipt.state == "received"
    # Same delivery id + same body replays the same receipt.
    replay = inbox.receive(delivery)
    assert replay.state == "received" and replay.received_at == receipt.received_at
    # Same id, different body conflicts.
    with pytest.raises(InboxError) as exc:
        inbox.receive(make_delivery(key, generation, text="tampered"))
    assert exc.value.code == "delivery_conflict"

    outcome = await inbox.apply_pending(delivery.delivery_id)
    assert outcome == "injected"
    session = await state.read(key)
    assert session is not None
    injected = [
        m for m in session.messages
        if m.get("external_delivery_id") == "external_delivery:del_1"
    ]
    assert len(injected) == 1
    assert "the findings" in injected[0]["content"]
    # Crash-after-inject replay does not duplicate the report.
    inbox._write(
        lambda conn: conn.execute(
            "UPDATE external_inbox SET state='received' WHERE delivery_id=?",
            (delivery.delivery_id,),
        )
    )
    outcome = await inbox.apply_pending(delivery.delivery_id)
    assert outcome == "injected"
    session = await state.read(key)
    assert session is not None
    injected = [
        m for m in session.messages
        if m.get("external_delivery_id") == "external_delivery:del_1"
    ]
    assert len(injected) == 1


async def test_session_replacement_routes_delivery_orphaned(harness):
    """A11: the origin session was reset; the result must not reach the new session."""
    inbox, state, key, generation = harness
    inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-1",
        operation_key="op-1", bot_id="research", instance_id="inst_r",
        principal_id="coordinator",
    )
    stale = make_delivery(key, "old-generation", delivery_id="del_stale")
    inbox.receive(stale)
    outcome = await inbox.apply_pending(stale.delivery_id)
    assert outcome == "orphaned"
    receipt = inbox.get_receipt(stale.delivery_id)
    assert receipt is not None and receipt.state == "orphaned"
    session = await state.read(key)
    assert session is not None
    assert not any(
        m.get("external_delivery_id") for m in session.messages
    )


async def test_new_user_turn_revokes_auto_continue(harness):
    """A18: after a real user turn, a late report becomes card-only."""
    inbox, state, key, generation = harness
    inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-old",
        operation_key="op-1", bot_id="research", instance_id="inst_r",
        principal_id="coordinator",
    )
    revoked = inbox.revoke_session_workflows(key)
    assert revoked == 1
    assert inbox.workflow_allows_auto_continue("wf-old") is False
    delivery = make_delivery(key, generation, workflow="wf-old")
    receipt = inbox.receive(delivery)
    assert receipt.state == "card_only"
    session = await state.read(key)
    assert session is not None
    assert not any(m.get("external_delivery_id") for m in session.messages)


async def test_q1a_b4_revoked_workflow_stays_revoked_after_new_mint(harness):
    """A later capability mint must not resurrect a revoked workflow."""
    inbox, state, key, generation = harness
    inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-r",
        operation_key="op-1", bot_id="research", instance_id="inst_r",
        principal_id="coordinator",
    )
    inbox.revoke_session_workflows(key)
    assert inbox.workflow_allows_auto_continue("wf-r") is False
    second = inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-r",
        operation_key="op-2", bot_id="research", instance_id="inst_r",
        principal_id="coordinator",
    )
    assert inbox.workflow_allows_auto_continue("wf-r") is False
    # New-task admission keeps requiring the live grant; only result
    # delivery validates without it (see test_inbox_continuation).
    with pytest.raises(InboxError) as exc:
        inbox.validate_capability(
            token=second.token, workflow_id="wf-r", operation_key="op-2",
            bot_id="research", instance_id="inst_r", principal_id="coordinator",
        )
    assert exc.value.code == "auto_continue_revoked"


async def test_q1a_b6_concurrent_first_use_binds_exactly_once(harness):
    """Two validates racing different keys on wildcards: one wins, one loses."""
    inbox, state, key, generation = harness
    capability = inbox.mint_capability(
        session_key=key, session_generation=generation, workflow_id="wf-race",
        operation_key="*", bot_id="*", instance_id="*", principal_id="coordinator",
    )
    first = inbox.validate_capability(
        token=capability.token, workflow_id="wf-race", operation_key="op-A",
        bot_id="research", instance_id="inst-A", principal_id="coordinator",
    )
    assert first.operation_key == "op-A"
    with pytest.raises(InboxError) as exc:
        inbox.validate_capability(
            token=capability.token, workflow_id="wf-race", operation_key="op-B",
            bot_id="research", instance_id="inst-A", principal_id="coordinator",
        )
    assert exc.value.code == "capability_binding_mismatch"
