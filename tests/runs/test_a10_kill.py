"""A10: kill the planner between receipt persistence and history injection."""

from __future__ import annotations

from nanobot.session.external_inbox import (
    ExternalInbox,
    InboxDelivery,
    delivery_body_digest,
)
from nanobot.session.manager import SessionManager


def make_delivery(generation: str, delivery_id="del_kill") -> InboxDelivery:
    return InboxDelivery(
        delivery_id=delivery_id,
        session_key="websocket:webui-default",
        session_generation=generation,
        workflow_id="wf-kill",
        task_id="task_kill",
        bot_id="research",
        instance_id="inst_r",
        run_id="run_kill",
        terminal_revision=2,
        outcome_code="succeeded",
        result_text="the late answer",
        result_truncated=False,
        body_digest=delivery_body_digest(
            task_id="task_kill", bot_id="research", instance_id="inst_r",
            run_id="run_kill", terminal_revision=2, outcome_code="succeeded",
            result_text="the late answer",
        ),
    )


async def test_crash_after_receipt_before_injection_recovers_exactly_once(tmp_path):
    """Receive persists, the process dies, restart injects exactly one report."""
    manager = SessionManager(tmp_path / "sessions-root")
    state = manager.state
    inbox = ExternalInbox(state)
    session_key = "websocket:webui-default"
    await state._execute(
        session_key,
        lambda: state._change(
            session_key, lambda s: s.add_message("user", "go"), create_if_missing=True
        ),
        operation_name="seed",
    )
    session = await state.read(session_key)
    assert session is not None
    inbox.mint_capability(
        session_key=session_key, session_generation=session.generation,
        workflow_id="wf-kill", operation_key="*", bot_id="*", instance_id="*",
        principal_id="teams-coordinator",
    )
    delivery = make_delivery(session.generation)
    receipt = inbox.receive(delivery)
    assert receipt.state == "received"
    # "Crash": reopen the store from a fresh SessionManager over the same files.
    manager2 = SessionManager(tmp_path / "sessions-root")
    inbox2 = ExternalInbox(manager2.state)
    pending = inbox2.pending_deliveries()
    assert delivery.delivery_id in pending, "the receipt must survive the crash"
    outcome = await inbox2.apply_pending(delivery.delivery_id)
    assert outcome == "injected"
    # A second recovery replay must not duplicate the history row.
    outcome = await inbox2.apply_pending(delivery.delivery_id)
    assert outcome in ("injected", "already_handled")
    final = await manager2.state.read(session_key)
    assert final is not None
    rows = [
        m for m in final.messages
        if m.get("external_delivery_id") == "external_delivery:del_kill"
    ]
    assert len(rows) == 1
