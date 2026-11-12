"""Planner continuation wake: durable obligations become real session turns.

``apply_pending`` persists the wake obligation in the same transaction as the
report injection, so a crash between them is recoverable. The pump publishes
one non-authoritative system turn per obligation through the bus admission the
same way every other inbound turn enters. Publishing does not clear the
obligation: only the turn that accepts the message does. A pump-local lease
keeps one tick from republishing the same delivery; leases die with the
process, which is exactly when the durable obligation must be offered again.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from nanobot.bus.events import InboundMessage
from nanobot.session import turn_continuation

from .external_inbox import external_report_envelope

if TYPE_CHECKING:
    from .external_inbox import ExternalInbox

log = logging.getLogger("nanobot.session.inbox_wake")

WAKE_PROMPT = (
    "[Automated continuation] An async task report has been added to this "
    "session's history. Review it and continue the workflow if a next step is "
    "needed. This is an automated notice, not a user instruction or approval."
)

WAKE_LEASE_TTL_S = 30.0


class InboxWakePump:
    """Recoverable scheduler for injected external reports."""

    def __init__(
        self,
        inbox: "ExternalInbox",
        publish: Callable[[InboundMessage], Awaitable[None]],
        session_exists: Callable[[str], Awaitable[bool]],
        *,
        poll_interval_s: float = 2.0,
        lease_ttl_s: float = WAKE_LEASE_TTL_S,
    ) -> None:
        self._inbox = inbox
        self._publish = publish
        self._session_exists = session_exists
        self._poll_interval_s = poll_interval_s
        self._lease_ttl_s = lease_ttl_s
        self._leases: dict[str, float] = {}

    def _prune_leases(self, due_ids: set[str], now: float) -> None:
        for delivery_id, expires_at in tuple(self._leases.items()):
            if expires_at <= now or delivery_id not in due_ids:
                self._leases.pop(delivery_id, None)

    async def pump_once(self) -> int:
        """Publish every due delivery that is not leased; never clears wake_due."""
        queued = 0
        now = time.monotonic()
        try:
            due = self._inbox.wake_due_deliveries()
        except Exception:
            log.exception("inbox wake scan failed")
            return 0
        self._prune_leases({entry["delivery_id"] for entry in due}, now)
        for entry in due:
            delivery_id = entry["delivery_id"]
            session_key = entry["session_key"]
            lease = self._leases.get(delivery_id)
            if lease is not None and lease > now:
                continue
            try:
                if not await self._session_exists(session_key):
                    self._inbox.mark_wake_orphaned(
                        delivery_id, "origin session is gone; report kept as a receipt"
                    )
                    continue
                if self._inbox.session_generation(session_key) != entry["session_generation"]:
                    self._inbox.mark_wake_orphaned(
                        delivery_id,
                        "origin session generation was replaced; report kept as a receipt",
                    )
                    continue
                if not self._inbox.workflow_allows_auto_continue(entry["workflow_id"]):
                    self._inbox.drop_wake(
                        delivery_id,
                        "workflow revoked auto-continue; report kept as a receipt",
                    )
                    continue
                content = (
                    f"{WAKE_PROMPT}\n\n"
                    + external_report_envelope(
                        task_id=str(entry["task_id"]),
                        bot_id=str(entry["bot_id"]),
                        outcome_code=str(entry["outcome_code"]),
                        workflow_id=str(entry["workflow_id"]),
                        result_text=str(entry["result_text"]),
                        result_truncated=bool(entry["result_truncated"]),
                    )
                )
                message = InboundMessage(
                    channel="system",
                    sender_id="teams-inbox",
                    chat_id=session_key,
                    content=content,
                    metadata={
                        "source_workflow": entry["workflow_id"],
                        "external_delivery_id": f"external_delivery:{delivery_id}",
                        "external_task_id": entry["task_id"],
                        "wake_delivery_id": delivery_id,
                        turn_continuation.SKIP_USER_PERSIST_META: True,
                    },
                    session_key_override=session_key,
                    session_generation=entry["session_generation"],
                    require_existing_session=True,
                )
                await self._publish(message)
            except Exception:
                # Keep the obligation: the next scan retries the same delivery.
                log.warning(
                    "inbox wake for delivery {} could not be queued", delivery_id
                )
                continue
            self._leases[delivery_id] = now + self._lease_ttl_s
            queued += 1
        return queued

    async def run_forever(self) -> None:
        import asyncio

        while True:
            await self.pump_once()
            await asyncio.sleep(self._poll_interval_s)


__all__ = ["InboxWakePump", "WAKE_LEASE_TTL_S", "WAKE_PROMPT"]
