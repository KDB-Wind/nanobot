"""Cancelling setup must not misreport an already committed authorization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from nanobot.channels.linear import connect as lc
from nanobot.channels.linear.client import LinearClient
from nanobot.channels.linear.oauth import OAUTH_FLOWS
from nanobot.channels.linear.server import LinearServerLease
from nanobot.channels.linear.state import LinearStateStore
from nanobot.channels.linear.tests.test_linear import _config, _installation


@pytest.fixture
async def connector(tmp_path, monkeypatch):
    state = LinearStateStore(tmp_path / "linear.sqlite3")
    monkeypatch.setattr(lc, "_load_linear_config", _config)
    monkeypatch.setattr(lc, "LinearStateStore", lambda: state)
    monkeypatch.setattr(lc, "acquire_http_server", lambda *_args: Mock(spec=LinearServerLease))
    store = lc.LinearConnectStore()
    yield store, state
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_flight", [False, True])
async def test_cancel_replays_completed_authorization_until_expiry(connector, monkeypatch, in_flight):
    store, state = connector
    started = await store.handle("start", {})
    session_id = started["session_id"]
    query = {"session_id": [session_id]}
    flow = store._sessions[session_id].flow
    assert OAUTH_FLOWS.complete(flow.state, code="code", error=None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if request.url.path == "/oauth/token":
            entered.set()
            await release.wait()
            return httpx.Response(200, json={
                "access_token": "access", "refresh_token": "refresh", "expires_in": 86400,
                "scope": "read write app:mentionable app:assignable",
            })
        return httpx.Response(200, json={"data": {"viewer": {
            "id": "app-user-1", "organization": {"id": "org-1", "name": "Example"},
        }}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(lc, "LinearClient", lambda config, state: LinearClient(config, state, http))
        poll = asyncio.create_task(store.handle("poll", query))
        cancel = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if in_flight:
                cancel = asyncio.create_task(store.handle("cancel", query))
                await asyncio.sleep(0)
            release.set()
            succeeded = await poll
            cancelled = await cancel if cancel is not None else await store.handle("cancel", query)
            assert succeeded["status"] == "succeeded"
            assert cancelled == succeeded
            assert state.has_installations("client-id")
            cancelled["status"] = "mutated by caller"
            assert await store.handle("cancel", query) == succeeded
            assert await store.handle("poll", query) == succeeded
            flow.deadline = 0
            assert (await store.handle("poll", query))["status"] == "expired"
            assert not store._sessions
            assert state.has_installations("client-id")
        finally:
            release.set()
            await asyncio.gather(poll, *([cancel] if cancel else []), return_exceptions=True)
            flow.deadline = 0
            await store.handle("cancel", query)


@pytest.mark.asyncio
async def test_pending_cancel_remains_idempotent(connector):
    store, state = connector
    started = await store.handle("start", {})
    query = {"session_id": [started["session_id"]]}
    flow = store._sessions[started["session_id"]].flow
    first = await store.handle("cancel", query)
    assert first["status"] == "cancelled"
    assert await store.handle("cancel", query) == first
    assert not OAUTH_FLOWS.complete(flow.state, code="late", error=None)
    assert not state.has_installations("client-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["expiry", "shutdown"])
async def test_lifecycle_cleanup_removes_committed_success(connector, monkeypatch, cleanup):
    store, state = connector
    expiry_gate = asyncio.Event()
    expire_session = store._expire_session

    async def expire_after_signal(session):
        await expiry_gate.wait()
        await expire_session(session)

    monkeypatch.setattr(store, "_expire_session", expire_after_signal)
    started = await store.start()
    session_id = started["session_id"]
    session = store._sessions[session_id]
    assert OAUTH_FLOWS.complete(session.flow.state, code="code", error=None)

    async def exchange(*_args):
        installation = _installation()
        state.save_installation(installation)
        return installation

    client = SimpleNamespace(exchange_code=exchange, close=AsyncMock())
    monkeypatch.setattr(lc, "LinearClient", lambda *_args: client)
    succeeded = await store.poll(session_id)
    assert succeeded["status"] == "succeeded"
    assert await store.cancel(session_id) == succeeded
    if cleanup == "expiry":
        session.flow.deadline = 0
        expiry_gate.set()
        await asyncio.wait_for(session.expiry_task, 3)
    else:
        await store.close()
    assert (await store.poll(session_id))["status"] == "expired"
    assert not store._sessions
    assert state.has_installations("client-id")


@pytest.mark.asyncio
async def test_cancel_finishes_listener_release_after_committed_authorization(connector, monkeypatch):
    store, state = connector
    started = await store.start()
    session_id = started["session_id"]
    session = store._sessions[session_id]
    lease = session.server
    lease.close.side_effect = [RuntimeError("listener close failed"), None]
    assert OAUTH_FLOWS.complete(session.flow.state, code="code", error=None)

    async def exchange(*_args):
        installation = _installation()
        state.save_installation(installation)
        return installation

    client = SimpleNamespace(exchange_code=exchange, close=AsyncMock())
    monkeypatch.setattr(lc, "LinearClient", lambda *_args: client)
    with pytest.raises(RuntimeError, match="listener close failed"):
        await store.poll(session_id)
    assert (await store.cancel(session_id))["status"] == "succeeded"
    assert state.has_installations("client-id")
    assert not store.has_pending_work
    assert lease.close.call_count == 2
