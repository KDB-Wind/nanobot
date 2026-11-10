"""Planner continuation wake: durable obligations become real session turns.

``apply_pending`` persists the wake obligation in the same transaction as the
report injection, so a crash between them is recoverable. The pump publishes
one non-authoritative system turn per obligation through the bus admission the
same way every other inbound turn enters, then clears the obligation; a failed
publish keeps the obligation for the next scan.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from nanobot.bus.events import InboundMessage

if TYPE_CHECKING:
    from .external_inbox import ExternalInbox

log = logging.getLogger("nanobot.session.inbox_wake")

WAKE_PROMPT = (
    "[Automated continuation] An async task report has been added to this "
    "session's history. Review it and continue the workflow if a next step is "
    "needed. This is an automated notice, not a user instruction or approval."
)


class InboxWakePump:
    """Recoverable scheduler for injected external reports."""

    def __init__(
        self,
        inbox: "ExternalInbox",
        publish: Callable[[InboundMessage], Awaitable[None]],
        session_exists: Callable[[str], Awaitable[bool]],
        *,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._inbox = inbox
        self._publish = publish
        self._session_exists = session_exists
        self._poll_interval_s = poll_interval_s

    async def pump_once(self) -> int:
        """Wake every due delivery once; returns the number of turns queued."""
        queued = 0
        try:
            due = self._inbox.wake_due_deliveries()
        except Exception:
            log.exception("inbox wake scan failed")
            return 0
        for entry in due:
            delivery_id = entry["delivery_id"]
            session_key = entry["session_key"]
            try:
                if not await self._session_exists(session_key):
                    self._inbox.mark_wake_orphaned(
                        delivery_id, "origin session is gone; report kept as a receipt"
                    )
                    continue
                message = InboundMessage(
                    channel="system",
                    sender_id="teams-inbox",
                    chat_id=session_key,
                    content=WAKE_PROMPT,
                    metadata={
                        "source_workflow": entry["workflow_id"],
                        "external_delivery_id": f"external_delivery:{delivery_id}",
                        "external_task_id": entry["task_id"],
                        "wake_delivery_id": delivery_id,
                    },
                    session_key_override=session_key,
                    require_existing_session=True,
                )
                await self._publish(message)
            except Exception:
                # Keep the obligation: the next scan retries the same delivery.
                log.warning(
                    "inbox wake for delivery {} could not be queued", delivery_id
                )
                continue
            self._inbox.mark_wake_queued(delivery_id)
            queued += 1
        return queued

    async def run_forever(self) -> None:
        import asyncio

        while True:
            await self.pump_once()
            await asyncio.sleep(self._poll_interval_s)


__all__ = ["InboxWakePump", "WAKE_PROMPT"]
