from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from nanobot_teams.config import Limits
from nanobot_teams.transport import (
    HttpTransport,
    OutcomeKind,
    TransportOutcome,
    read_bounded,
    truncate_utf8,
)

ENDPOINT = "http://127.0.0.1:8901/v1/chat/completions"
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class CompletionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = json.dumps(completion("direct")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def local_target() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


async def send(
    handler: Handler, limits: Limits | None = None, task: str = "find x"
) -> TransportOutcome:
    transport = HttpTransport(limits or Limits(), transport=httpx.MockTransport(handler))
    try:
        return await transport.send(
            endpoint=ENDPOINT, api_key="secret-key", task=task, session_id="teams-v1-abc"
        )
    finally:
        await transport.aclose()


async def respond(response: httpx.Response) -> TransportOutcome:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    return await send(handler)


async def test_completed_200() -> None:
    outcome = await respond(httpx.Response(200, json=completion("answer")))

    assert outcome.kind is OutcomeKind.completed
    assert outcome.content == "answer"
    assert outcome.truncated is False
    assert outcome.error_code is None
    assert outcome.detail is None
    assert outcome.blocks_future is False


async def test_request_shape_and_headers() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion("ok"))

    await send(handler, task="read the docs")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(await request.aread())
    assert body == {
        "messages": [{"role": "user", "content": "read the docs"}],
        "session_id": "teams-v1-abc",
        "stream": False,
    }
    assert "model" not in body


async def test_truncates_after_full_parse() -> None:
    content = "a" * 32767 + "é" + "b"
    outcome = await respond(httpx.Response(200, json=completion(content)))

    assert outcome.kind is OutcomeKind.completed
    assert outcome.truncated is True
    assert outcome.content == "a" * 32767
    assert outcome.blocks_future is False


async def test_content_at_limit_is_not_truncated() -> None:
    outcome = await respond(httpx.Response(200, json=completion("a" * 32768)))

    assert outcome.truncated is False
    assert len(outcome.content) == 32768


@pytest.mark.parametrize(
    ("status", "kind", "error_code", "blocks"),
    [
        (400, OutcomeKind.failed, "request_rejected", False),
        (401, OutcomeKind.failed, "auth_failed", False),
        (403, OutcomeKind.failed, "auth_failed", False),
        (404, OutcomeKind.failed, "request_rejected", False),
        (413, OutcomeKind.failed, "request_rejected", False),
        (418, OutcomeKind.failed, "request_rejected", False),
        (302, OutcomeKind.outcome_unknown, "unexpected_status", True),
        (500, OutcomeKind.outcome_unknown, "target_error", True),
        (502, OutcomeKind.outcome_unknown, "target_error", True),
        (503, OutcomeKind.failed, "provider_unavailable", False),
        (504, OutcomeKind.timed_out, "target_timeout", True),
    ],
)
async def test_status_mapping(
    status: int, kind: OutcomeKind, error_code: str, blocks: bool
) -> None:
    outcome = await respond(httpx.Response(status, text="provider detail"))

    assert outcome.kind is kind
    assert outcome.error_code == error_code
    assert outcome.blocks_future is blocks
    assert outcome.content == ""
    assert "provider detail" not in (outcome.detail or "")


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"choices": []}',
        b'{"choices": [3]}',
        b'{"choices": [{"message": "hi"}]}',
        b'{"choices": [{"message": {"content": 3}}]}',
        b'{"choices": [{"message": {"content": null}}]}',
    ],
)
async def test_invalid_response_shape(payload: bytes) -> None:
    outcome = await respond(
        httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})
    )

    assert outcome.kind is OutcomeKind.outcome_unknown
    assert outcome.error_code == "response_invalid"
    assert outcome.blocks_future is True


async def test_response_too_large() -> None:
    payload = json.dumps(completion("x" * 500)).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    outcome = await send(handler, Limits(response_max_bytes=64))

    assert outcome.kind is OutcomeKind.outcome_unknown
    assert outcome.error_code == "response_too_large"
    assert outcome.blocks_future is True


async def test_connect_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    outcome = await send(handler)

    assert outcome.kind is OutcomeKind.failed
    assert outcome.error_code == "connect_failed"
    assert outcome.blocks_future is False


async def test_connect_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    outcome = await send(handler)

    assert outcome.kind is OutcomeKind.failed
    assert outcome.error_code == "connect_failed"


async def test_read_timeout_is_unknown() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    outcome = await send(handler)

    assert outcome.kind is OutcomeKind.outcome_unknown
    assert outcome.error_code == "transport_error"
    assert outcome.blocks_future is True


async def test_total_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        import asyncio

        await asyncio.sleep(5)
        return httpx.Response(200, json=completion("late"))

    limits = Limits(total_timeout_s=0.1, request_timeout_s=10.0)
    outcome = await send(handler, limits)

    assert outcome.kind is OutcomeKind.timed_out
    assert outcome.error_code == "deadline_exceeded"
    assert outcome.blocks_future is True


async def test_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch, local_target: int
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)

    transport = HttpTransport(Limits())
    try:
        outcome = await transport.send(
            endpoint=f"http://127.0.0.1:{local_target}/v1/chat/completions",
            api_key="secret-key",
            task="find x",
            session_id="teams-v1-abc",
        )
    finally:
        await transport.aclose()

    assert outcome.kind is OutcomeKind.completed
    assert outcome.content == "direct"


async def test_read_bounded_under_limit() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"def"

    assert await read_bounded(chunks(), 6) == b"abcdef"


async def test_read_bounded_over_limit() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"def"

    assert await read_bounded(chunks(), 5) is None


def test_truncate_utf8_boundary() -> None:
    assert truncate_utf8("abc", 10) == ("abc", False)
    assert truncate_utf8("aé", 2) == ("a", True)
    assert truncate_utf8("aé", 3) == ("aé", False)
