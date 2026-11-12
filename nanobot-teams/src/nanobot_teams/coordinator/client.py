"""HTTP client for the public agent-run protocol v1 (bot side)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 256 * 1024
"""Protocol raw-read bound (F15): compressed and oversize bodies are rejected
before parsing, matching the server-side 256 KiB body limit."""

MAX_JSON_DEPTH = 16


class BotUnreachableError(Exception):
    """The outcome of the request is unknown (network/timeout/5xx-class transport)."""


@dataclass(frozen=True)
class BotResponse:
    status: int
    payload: dict[str, Any] | None


def _strict_json_dict(raw: bytes) -> dict[str, Any] | None:
    """Parse a bounded JSON object; duplicate keys and over-deep nests fail."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(text, object_pairs_hook=pairs_hook)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict) or _depth(parsed) > MAX_JSON_DEPTH:
        return None
    return parsed


def _depth(value: object, level: int = 0) -> int:
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level


async def _bounded_json_payload(response: httpx.Response) -> dict[str, Any] | None:
    """Read the response body within protocol bounds (F15).

    Compression is rejected from the headers before any byte is decoded, the
    declared length is checked, and the raw stream is read with a hard cap.
    Any violation yields ``None`` (same shape as a non-JSON body) so callers
    keep their existing reconciliation behavior.
    """
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        return None
    declared = response.headers.get("Content-Length", "")
    if declared:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                return None
        except ValueError:
            return None
    raw = b""
    async for chunk in response.aiter_bytes():
        raw += chunk
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
    return _strict_json_dict(raw)


class BotRunClient:
    def __init__(self, endpoint: str, api_key: str, *, timeout_s: float = 10.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"Authorization": f"Bearer {api_key}"},
            follow_redirects=False,
            # F14: authenticated control traffic never traverses an
            # environment proxy; targets are the registered loopback endpoints.
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> BotResponse:
        request = self._client.build_request(method, f"{self._endpoint}{path}", **kwargs)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise BotUnreachableError(f"{type(exc).__name__}: {exc}") from exc
        try:
            payload = await _bounded_json_payload(response)
        finally:
            await response.aclose()
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
