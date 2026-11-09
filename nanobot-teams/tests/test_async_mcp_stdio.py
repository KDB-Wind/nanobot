"""A16: the async MCP is a short-lived stdio client; tasks outlive the process."""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.store import CoordinatorStore

try:
    import aiohttp  # noqa: F401
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    HAS_MCP = True
except ImportError:  # pragma: no cover
    HAS_MCP = False

pytest_plugins = ("pytest_asyncio",)

PLANNER_TOKEN = "stdio-test-planner-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def coord_server(tmp_path):
    store = CoordinatorStore(tmp_path / "c.sqlite3")
    store.open()
    store.create_project("proj", "Main")
    bot_port = free_port()


    async def caps(_request):
        return web.json_response(
            {"protocol": 1, "instance_id": "inst_stdio", "execution": True}
        )

    async def submit(request):
        key = request.headers.get("Idempotency-Key", "")
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        return web.json_response(
            {"protocol": 1, "instance_id": "inst_stdio", "run_id": run_id,
             "state": "running", "revision": 1},
            status=202,
        )

    async def get_run(request):
        return web.json_response(
            {"protocol": 1, "instance_id": "inst_stdio",
             "run_id": request.match_info["run_id"], "state": "running",
             "revision": 1, "stop_confirmed": False, "effects_uncertain": False,
             "outcome": None}
        )

    async def result(_request):
        return web.json_response({"outcome": {"code": "succeeded", "text": "done"}})

    app = web.Application()
    app.router.add_get("/v1/agent-capabilities", caps)
    app.router.add_post("/v1/agent-runs", submit)
    app.router.add_get("/v1/agent-runs/{run_id}", get_run)
    app.router.add_get("/v1/agent-runs/{run_id}/result", result)
    store.upsert_bot(
        Bot(bot_id="research", display_name="Research", role="research",
            endpoint=f"http://127.0.0.1:{bot_port}", secret_env="X")
    )

    from nanobot_teams.coordinator.config import CoordinatorConfig
    from nanobot_teams.coordinator.http_api import build_app

    config = CoordinatorConfig(
        control_dir=tmp_path / "control", host="127.0.0.1", port=0,
    )
    application = build_app(store, config, {"operator": "a", "planner": PLANNER_TOKEN})
    return application, store


@pytest.mark.skipif(not HAS_MCP, reason="mcp or aiohttp is not installed")
async def test_stdio_mcp_roundtrip_and_reopen(coord_server, tmp_path, monkeypatch):
    application, store = coord_server
    server = TestServer(application)
    await server.start_server()
    try:
        monkeypatch.setenv("STDIO_TEST_TOKEN", PLANNER_TOKEN)
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "nanobot_teams", "async-mcp",
                "--url", str(server.make_url("")),
                "--token-env", "STDIO_TEST_TOKEN",
            ],
            env={**os.environ, "STDIO_TEST_TOKEN": PLANNER_TOKEN, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        operation_key = str(uuid.uuid4())

        # First MCP session: list tools, submit one task.
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"list_bots", "submit_task", "get_async_task",
                        "list_async_tasks", "cancel_task"} <= names
                result = await session.call_tool(
                    "submit_task",
                    {"project": "proj", "bot": "research", "input": "stdio task",
                     "operation_key": operation_key},
                )
                assert result.content and isinstance(
                    result.content[0], __import__("mcp").types.TextContent
                ), f"unexpected content: {result.content!r}"
                payload = json.loads(result.content[0].text)
                task_id = payload["task_id"]
                assert task_id.startswith("task_")

        # The MCP process is gone; the task and its dispatch stay in the store.
        assert store.get_task(task_id) is not None

        # Reopen a fresh MCP session: the same task is queryable (A16).
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                fetched = await session.call_tool(
                    "get_async_task", {"task_id": task_id}
                )
                again = json.loads(fetched.content[0].text)
                assert again["task_id"] == task_id
                # Same key reconciles through MCP as well.
                replay = await session.call_tool(
                    "submit_task",
                    {"project": "proj", "bot": "research", "input": "stdio task",
                     "operation_key": operation_key},
                )
                assert json.loads(replay.content[0].text)["task_id"] == task_id
    finally:
        await server.close()
        store.close()
