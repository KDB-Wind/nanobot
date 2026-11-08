"""One HTTP request per delegation against the public chat completions API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from .config import Limits


class OutcomeKind(str, Enum):
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"
    outcome_unknown = "outcome_unknown"


@dataclass(frozen=True)
class TransportOutcome:
    kind: OutcomeKind
    content: str = ""
    truncated: bool = False
    error_code: str | None = None
    detail: str | None = None
    blocks_future: bool = False


async def read_bounded(chunks: AsyncIterator[bytes], max_bytes: int) -> bytes | None:
    """Join a byte stream, or return None as soon as it passes the cap."""
    buffer = bytearray()
    async for chunk in chunks:
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            return None
    return bytes(buffer)


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut to a UTF-8 boundary, dropping an incomplete trailing sequence."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


class HttpTransport:
    def __init__(
        self,
        limits: Limits,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._limits = limits
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=limits.connect_timeout_s,
                read=limits.request_timeout_s,
                write=limits.request_timeout_s,
                pool=limits.request_timeout_s,
            ),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def send(
        self,
        *,
        endpoint: str,
        api_key: str,
        task: str,
        session_id: str,
        deadline_s: float | None = None,
    ) -> TransportOutcome:
        body = {
            "messages": [{"role": "user", "content": task}],
            "session_id": session_id,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        budget = min(self._limits.total_timeout_s, deadline_s or self._limits.total_timeout_s)
        try:
            async with asyncio.timeout(budget):
                async with self._client.stream(
                    "POST", endpoint, json=body, headers=headers
                ) as response:
                    return await self._classify(response)
        except TimeoutError:
            return TransportOutcome(
                OutcomeKind.timed_out,
                error_code="deadline_exceeded",
                detail="The local deadline was exceeded.",
                blocks_future=True,
            )
        except (httpx.ConnectTimeout, httpx.ConnectError):
            return TransportOutcome(
                OutcomeKind.failed,
                error_code="connect_failed",
                detail="The target could not be reached.",
            )
        except httpx.RequestError:
            return TransportOutcome(
                OutcomeKind.outcome_unknown,
                error_code="transport_error",
                detail="The request failed after it may have been sent.",
                blocks_future=True,
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _classify(self, response: httpx.Response) -> TransportOutcome:
        status = response.status_code
        if status == 200:
            return await self._parse_completion(response)
        mapping = {
            503: TransportOutcome(
                OutcomeKind.failed,
                error_code="provider_unavailable",
                detail="The target provider is unavailable.",
            ),
            504: TransportOutcome(
                OutcomeKind.timed_out,
                error_code="target_timeout",
                detail="The target reported a timeout.",
                blocks_future=True,
            ),
        }
        if status in mapping:
            return mapping[status]
        if status in (401, 403):
            return TransportOutcome(
                OutcomeKind.failed,
                error_code="auth_failed",
                detail="The target rejected the credentials.",
            )
        if 400 <= status < 500:
            return TransportOutcome(
                OutcomeKind.failed,
                error_code="request_rejected",
                detail="The target rejected the request.",
            )
        if 500 <= status < 600:
            return TransportOutcome(
                OutcomeKind.outcome_unknown,
                error_code="target_error",
                detail="The target returned a server error.",
                blocks_future=True,
            )
        return TransportOutcome(
            OutcomeKind.outcome_unknown,
            error_code="unexpected_status",
            detail="The target returned an unexpected status.",
            blocks_future=True,
        )

    async def _parse_completion(self, response: httpx.Response) -> TransportOutcome:
        raw = await read_bounded(response.aiter_bytes(), self._limits.response_max_bytes)
        if raw is None:
            return TransportOutcome(
                OutcomeKind.outcome_unknown,
                error_code="response_too_large",
                detail="The target response exceeded the size limit.",
                blocks_future=True,
            )
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            return _invalid("The target response was not valid JSON.")
        content = _completion_content(payload)
        if content is None:
            return _invalid("The target response did not match the completion shape.")
        text, truncated = truncate_utf8(content, self._limits.result_max_bytes)
        return TransportOutcome(OutcomeKind.completed, content=text, truncated=truncated)


def _completion_content(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _invalid(detail: str) -> TransportOutcome:
    return TransportOutcome(
        OutcomeKind.outcome_unknown,
        error_code="response_invalid",
        detail=detail,
        blocks_future=True,
    )
