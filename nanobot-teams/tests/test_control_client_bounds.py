"""R4: control-protocol client bounds — no proxies, no compression, bounded reads.

Regression lock for the 2026-11-09 audit findings F14 and F15: the bot-side
client never traverses an environment proxy with its bearer, rejects
compressed responses before decoding, and parses only bounded raw JSON with
unique keys and a depth limit.
"""

from __future__ import annotations

import gzip
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nanobot_teams.coordinator.client import MAX_RESPONSE_BYTES, BotRunClient

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _client_for(app: web.Application) -> tuple[BotRunClient, TestServer]:
    server = TestServer(app)
    await server.start_server()
    client = BotRunClient(str(server.make_url("")).rstrip("/"), "synthetic-control-key")
    return client, server


async def test_compressed_response_is_rejected_before_decoding():
    """F15: a gzip body is refused from the headers; no bomb is decoded."""
    big = {"protocol": 1, "instance_id": "inst_a", "extra": "x" * (1024 * 1024)}

    async def caps(request: web.Request) -> web.Response:
        return web.Response(
            body=gzip.compress(json.dumps(big).encode()),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    client, server = await _client_for(_app_with(caps))
    try:
        response = await client.capabilities()
        assert response.status == 200
        assert response.payload is None, "compressed body must not be decoded"
    finally:
        await client.aclose()
        await server.close()


async def test_oversize_response_is_rejected():
    """F15: raw bodies beyond the 256 KiB protocol bound are refused."""
    async def caps(request: web.Request) -> web.Response:
        payload = {"protocol": 1, "extra": "x" * (MAX_RESPONSE_BYTES + 1024)}
        return web.json_response(payload)

    client, server = await _client_for(_app_with(caps))
    try:
        response = await client.capabilities()
        assert response.payload is None
    finally:
        await client.aclose()
        await server.close()


async def test_duplicate_keys_and_overdeep_json_are_rejected():
    """F15: strict JSON — no duplicate keys, bounded depth."""
    async def dups(request: web.Request) -> web.Response:
        return web.Response(
            body='{"protocol":1,"protocol":1}', content_type="application/json"
        )

    client, server = await _client_for(_app_with(dups))
    try:
        assert (await client.capabilities()).payload is None
    finally:
        await client.aclose()
        await server.close()

    async def deep(request: web.Request) -> web.Response:
        value: object = 1
        for _ in range(64):
            value = [value]
        return web.json_response({"protocol": 1, "nested": value})

    client2, server2 = await _client_for(_app_with(deep))
    try:
        assert (await client2.capabilities()).payload is None
    finally:
        await client2.aclose()
        await server2.close()


async def test_bearer_never_traverses_an_environment_proxy():
    """F14: with HTTP_PROXY set, the client still dials the registered target."""
    proxy_hits: list[str] = []
    target_auth: list[str] = []

    async def proxy(request: web.Request) -> web.Response:
        proxy_hits.append(request.headers.get("Authorization", ""))
        return web.json_response({"protocol": 1, "instance_id": "proxy"})

    async def caps(request: web.Request) -> web.Response:
        target_auth.append(request.headers.get("Authorization", ""))
        return web.json_response({"protocol": 1, "instance_id": "inst_a", "execution": True})

    proxy_server = TestServer(_app_with(proxy))
    target_server = TestServer(_app_with(caps))
    await proxy_server.start_server()
    await target_server.start_server()
    client = BotRunClient(str(target_server.make_url("")).rstrip("/"), "synthetic-bot-credential")
    saved = {n: os.environ.get(n) for n in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    try:
        for name in saved:
            os.environ.pop(name, None)
        os.environ["HTTP_PROXY"] = str(proxy_server.make_url("/"))
        os.environ["NO_PROXY"] = ""
        response = await client.capabilities()
        assert response.status == 200
        assert response.payload is not None and response.payload["instance_id"] == "inst_a"
        assert proxy_hits == [], "the proxy must never see control traffic"
        assert target_auth == ["Bearer synthetic-bot-credential"]
    finally:
        await client.aclose()
        await proxy_server.close()
        await target_server.close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _app_with(handler) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/agent-capabilities", handler)
    return app
