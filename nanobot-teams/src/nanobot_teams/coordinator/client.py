"""HTTP client for the public agent-run protocol v1 (bot side)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class BotUnreachableError(Exception):
    """The outcome of the request is unknown (network/timeout/5xx-class transport)."""


@dataclass(frozen=True)
class BotResponse:
    status: int
    payload: dict[str, Any] | None


class BotRunClient:
    def __init__(self, endpoint: str, api_key: str, *, timeout_s: float = 10.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"Authorization": f"Bearer {api_key}"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> BotResponse:
        try:
            response = await self._client.request(method, f"{self._endpoint}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise BotUnreachableError(f"{type(exc).__name__}: {exc}") from exc
        payload: dict[str, Any] | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if payload is not None and not isinstance(payload, dict):
            payload = None
        return BotResponse(status=response.status_code, payload=payload)

    async def capabilities(self) -> BotResponse:
        return await self._request("GET", "/v1/agent-capabilities")

    async def submit(
        self, *, operation_key: str, protocol_body: dict[str, Any]
    ) -> BotResponse:
        return await self._request(
            "POST",
            "/v1/agent-runs",
            json=protocol_body,
            headers={"Idempotency-Key": operation_key},
        )

    async def lookup(self, operation_key: str) -> BotResponse:
        return await self._request(
            "GET", "/v1/agent-runs/lookup", params={"operation_key": operation_key}
        )

    async def get_run(self, run_id: str) -> BotResponse:
        return await self._request("GET", f"/v1/agent-runs/{run_id}")

    async def get_result(self, run_id: str) -> BotResponse:
        return await self._request("GET", f"/v1/agent-runs/{run_id}/result")

    async def cancel(self, run_id: str) -> BotResponse:
        return await self._request("POST", f"/v1/agent-runs/{run_id}/cancel")
