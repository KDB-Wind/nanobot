"""ADR-001: `serve` can host the runs inbox on its own API app.

A worker bot that both executes coordinator-dispatched runs and receives
peer results needs both route families in one process. Before this change
serve advertised ``inbox`` from the config while never mounting the route
(.audit/serve_inbox_probe.py, RED on db8e9ba56).
"""

from __future__ import annotations

import pytest

from nanobot.api.server import create_app
from nanobot.config.schema import ApiRunsConfig

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

RUNS_KEY = "serve-inbox-test-key"


def inbox_runs_config() -> ApiRunsConfig:
    return ApiRunsConfig(
        enabled=True,
        api_key=RUNS_KEY,
        principal_id="teams-coordinator",
        execution_enabled=True,
        inbox_enabled=True,
    )


@pytest.fixture
async def inbox_app(tmp_path):
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    from nanobot.bus.queue import MessageBus
    from nanobot.cli.serve_inbox import mount_serve_inbox
    from nanobot.runs.service import RunService
    from nanobot.runs.store import RunStore
    from nanobot.session.inbox_wake import InboxWakePump
    from nanobot.session.manager import SessionManager
    from tests.runs.test_run_service import FakeExecutor

    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    service = RunService(store, FakeExecutor())
    await service.start()
    session_manager = SessionManager(tmp_path / "home")
    app = create_app(
        object(),
        api_key="",
        runs_service=service,
        runs_config=inbox_runs_config(),
    )
    pump = mount_serve_inbox(
        app,
        session_manager=session_manager,
        bus=MessageBus(),
        runs_config=inbox_runs_config(),
    )
    assert isinstance(pump, InboxWakePump)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        try:
            await service.aclose()
        finally:
            store.close()


async def test_capabilities_report_inbox_truthfully(inbox_app):
    resp = await inbox_app.get(
        "/v1/agent-capabilities", headers={"Authorization": f"Bearer {RUNS_KEY}"}
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["inbox"] is True


async def test_inbox_route_receives_and_rejects_garbage(inbox_app):
    """The route exists and answers with a typed protocol rejection."""
    resp = await inbox_app.post(
        "/v1/agent-inbox",
        json={"protocol": 1, "delivery_id": "not-a-uuid"},
        headers={"Authorization": f"Bearer {RUNS_KEY}"},
    )
    assert resp.status != 404
    payload = await resp.json()
    assert "code" in payload


async def test_inbox_route_rejects_wrong_key(inbox_app):
    resp = await inbox_app.post(
        "/v1/agent-inbox",
        json={},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status == 401


async def test_plain_app_has_no_inbox_route(tmp_path):
    """Default-off: an app assembled without mount_serve_inbox has no route."""
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    app = create_app(object(), api_key="")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/v1/agent-inbox", json={})
        assert resp.status == 404
    finally:
        await client.close()
