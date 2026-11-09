"""Gateway proxy to the resident Teams coordinator (browser workbench backend).

The browser only ever talks to its own gateway under /api/teams/*; service
credentials stay in the gateway process. Reads ride authenticated HTTP;
mutations arrive through the WebSocket webui_request action mapping.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class TeamsProxyResult:
    status: int
    payload: Any


class TeamsProxy:
    def __init__(self, coordinator_url: str, token_env: str) -> None:
        self.coordinator_url = coordinator_url.rstrip("/")
        self.token_env = token_env

    @property
    def enabled(self) -> bool:
        return bool(self.coordinator_url)

    def _token(self) -> str:
        return os.environ.get(self.token_env, "").strip()

    async def forward(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> TeamsProxyResult:
        """Forward one /api/teams/... request to the coordinator."""
        if not self.enabled:
            return TeamsProxyResult(503, {"code": "teams_disabled", "message": "No coordinator configured."})
        token = self._token()
        if not token:
            return TeamsProxyResult(
                503,
                {"code": "teams_token_missing", "message": f"{self.token_env} is not set."},
            )
        target = self.coordinator_url + path.replace("/api/teams", "/v1/teams", 1)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(
                    method,
                    target,
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            return TeamsProxyResult(
                503, {"code": "coordinator_unavailable", "message": str(exc)}
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:512]}
        return TeamsProxyResult(response.status_code, payload)

    async def event_stream(self, on_event: Any) -> None:
        """Subscribe the coordinator SSE stream and forward parsed events."""
        token = self._token()
        if not token:
            return
        backoff = 1.0
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "GET",
                        self.coordinator_url + "/v1/teams/events?after=0",
                        headers={"Authorization": f"Bearer {token}"},
                    ) as response:
                        response.raise_for_status()
                        backoff = 1.0
                        buffer = b""
                        async for chunk in response.aiter_bytes():
                            buffer += chunk
                            while b"\n\n" in buffer:
                                raw, buffer = buffer.split(b"\n\n", 1)
                                event = _parse_sse(raw)
                                if event is not None:
                                    await on_event(event)
            except (httpx.HTTPError, asyncio.CancelledError):
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _parse_sse(raw: bytes) -> dict[str, Any] | None:
    event_type = ""
    data = ""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
    if not data:
        return None
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    payload: dict[str, Any] = {**parsed, "type": event_type}
    return payload


__all__ = ["TeamsProxy", "TeamsProxyResult"]
