"""Relay coordinator SSE events to authenticated WebUI sockets as teams_event."""

from __future__ import annotations

import asyncio
from typing import Any


def start_teams_event_relay(proxy: Any, websocket_channel: Any) -> asyncio.Task[None]:
    """Subscribe the coordinator stream; forward each event as teams_event."""

    async def on_event(event: dict[str, Any]) -> None:
        router = websocket_channel._commands  # noqa: SLF001 - relay owns this wiring
        try:
            await router.broadcast_webui_event(
                "teams_event",
                seq=event.get("seq"),
                type=event.get("type"),
                task_id=event.get("task_id"),
                data=event.get("data", {}),
            )
        except Exception:
            pass

    async def run() -> None:
        await proxy.event_stream(on_event)

    return asyncio.create_task(run(), name="nanobot-teams-event-relay")


__all__ = ["start_teams_event_relay"]
