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
    from runs.test_run_service import FakeExecutor

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


async def test_serve_inbox_continues_an_idle_peer_session(tmp_path, monkeypatch):
    import asyncio
    import json

    from nanobot.agent.loop import AgentLoop
    from nanobot.cli import commands
    from nanobot.config.schema import Config
    from nanobot.providers.base import LLMProvider, LLMResponse
    from nanobot.session.external_inbox import ExternalInbox

    class ReplyProvider(LLMProvider):
        def __init__(self):
            super().__init__(provider_name="offline-inbox")
            self.called = asyncio.Event()
            self.inputs = []

        def get_default_model(self):
            return "offline-inbox"

        async def chat(self, messages, tools=None, **kwargs):
            self.inputs.extend(messages)
            self.called.set()
            return LLMResponse(content="WAKE_ACK result-token")

        async def chat_stream(self, messages, tools=None, **kwargs):
            return await self.chat(messages, tools=tools, **kwargs)

    class NoMCP:
        async def connect(self):
            return None

        async def aclose(self):
            return None

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.api.runs = inbox_runs_config()
    (tmp_path / "runs").mkdir()
    provider = ReplyProvider()
    assembled = {}

    def make_loop(_config, bus, **kwargs):
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            session_manager=kwargs["session_manager"],
            tool_registry=kwargs["tool_registry"],
        )
        assembled["loop"] = loop
        return loop

    monkeypatch.setattr(commands, "_load_runtime_config", lambda *_args: config)
    monkeypatch.setattr(commands, "_provider_setup_error", lambda _config: None)
    monkeypatch.setattr(commands, "sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(commands.AgentLoop, "from_config", make_loop)
    monkeypatch.setattr(commands.MCPProvider, "from_config", lambda *_args: NoMCP())
    monkeypatch.setattr("nanobot.config.paths.get_runtime_subdir", lambda _name: tmp_path / "runs")
    monkeypatch.setattr("aiohttp.web.run_app", lambda app, **kwargs: assembled.update(app=app))
    commands.serve(host="127.0.0.1", port=8765, timeout=30, verbose=False,
                   workspace=None, config=None)
    loop = assembled["loop"]
    state = loop.sessions.state
    session = await state.get("peer:wf-idle")
    inbox = ExternalInbox(state)
    capability = inbox.mint_capability(
        session_key=session.key, session_generation=session.generation,
        workflow_id="wf-idle", operation_key="op-idle", bot_id="writer",
        instance_id="writer-instance", principal_id="teams-coordinator",
    )
    client = TestClient(TestServer(assembled["app"]))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/agent-inbox",
            headers={"Authorization": f"Bearer {RUNS_KEY}"},
            json={
                "delivery_id": "del_idle", "task_id": "task_idle",
                "bot_id": "writer", "instance_id": "writer-instance",
                "run_id": "run_idle", "terminal_revision": 1,
                "outcome": {"code": "succeeded"}, "result_text": "result-token",
                "result_truncated": False, "workflow_id": "wf-idle",
                "operation_key": "op-idle", "reply_capability": capability.token,
            },
        )
        assert response.status == 202, await response.text()
        await asyncio.wait_for(provider.called.wait(), timeout=4)
        assert "result-token" in json.dumps(provider.inputs)
        async with asyncio.timeout(4):
            while True:
                saved = await state.read(session.key)
                replies = [m for m in saved.messages if m.get("role") == "assistant"]
                if replies:
                    break
                await asyncio.sleep(0.01)
        assert [m["content"] for m in replies] == ["WAKE_ACK result-token"]
        assert len([m for m in saved.messages if m.get("external_delivery_id")]) == 1
        assert inbox.wake_due_deliveries() == []
        assert "pending_user_turn_owner" not in saved.metadata
    finally:
        await client.close()
