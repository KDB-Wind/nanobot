from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
KEY = "stdio-secret"


class Target:
    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []


class TargetServer(ThreadingHTTPServer):
    target: Target


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        target = cast(TargetServer, self.server).target
        target.requests.append((dict(self.headers), body))
        status = target.statuses.pop(0) if target.statuses else 200
        if status == 200:
            payload = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "stdio answer"}}]}
            ).encode()
        else:
            payload = b'{"error":"boom"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def target() -> Iterator[tuple[Target, int]]:
    server = TargetServer(("127.0.0.1", 0), RequestHandler)
    server.target = Target()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.target, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def write_config(tmp_path: Path, port: int) -> Path:
    data = {
        "schema_version": 1,
        "team_id": "personal-research",
        "caller_bot_id": "planner",
        "thread_key": "main",
        "allowed_targets": ["research"],
        "bots": [
            {"id": "planner", "display_name": "Planner", "role": "planning"},
            {
                "id": "research",
                "display_name": "Research",
                "role": "research",
                "endpoint": f"http://127.0.0.1:{port}/v1/chat/completions",
                "api_key_env": "TEAMS_RESEARCH_API_KEY",
            },
        ],
    }
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


async def test_stdio_server_speaks_mcp(tmp_path: Path, target: tuple[Target, int]) -> None:
    server, port = target
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nanobot_teams", "--config", str(write_config(tmp_path, port))],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": KEY, "PYTHONPATH": str(SRC)},
        cwd=str(ROOT),
    )
    async with asyncio.timeout(120):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                by_name = {tool.name: tool for tool in tools}

                assert set(by_name) == {"list_bots", "delegate_task"}
                assert by_name["list_bots"].annotations.readOnlyHint is True
                assert by_name["list_bots"].annotations.idempotentHint is True
                assert by_name["list_bots"].annotations.openWorldHint is False
                assert by_name["delegate_task"].annotations.readOnlyHint is False
                assert by_name["delegate_task"].annotations.idempotentHint is False
                assert by_name["delegate_task"].annotations.destructiveHint is False
                assert by_name["delegate_task"].annotations.openWorldHint is True

                assert by_name["list_bots"].inputSchema["properties"] == {}
                assert by_name["list_bots"].inputSchema["additionalProperties"] is False
                delegate_schema = by_name["delegate_task"].inputSchema
                assert set(delegate_schema["properties"]) == {
                    "bot_id",
                    "task",
                    "parent_task_id",
                    "root_task_id",
                    "hop",
                    "deadline_remaining_s",
                    "chain",
                }
                assert set(delegate_schema["required"]) == {"bot_id", "task"}
                assert delegate_schema["additionalProperties"] is False

                unknown_tool = await session.call_tool("no_such_tool", {})
                assert unknown_tool.isError is True
                assert json.loads(unknown_tool.content[0].text)["error_code"] == "unknown_tool"

                listed = await session.call_tool("list_bots", {})
                assert listed.isError is False
                listed_text = listed.content[0].text
                assert json.loads(listed_text) == {
                    "bots": [
                        {
                            "id": "research",
                            "display_name": "Research",
                            "role": "research",
                            "capabilities": [],
                        }
                    ]
                }
                assert "endpoint" not in listed_text

                completed = await session.call_tool(
                    "delegate_task", {"bot_id": "research", "task": "find x"}
                )
                assert completed.isError is False
                payload = json.loads(completed.content[0].text)
                assert payload["status"] == "completed"
                assert payload["semantic_status"] == "unverified"
                assert payload["content"] == "stdio answer"
                assert payload["bot_id"] == "research"

                headers, body = server.requests[0]
                assert headers["Authorization"] == f"Bearer {KEY}"
                assert "model" not in body

                server.statuses.append(500)
                unknown = await session.call_tool(
                    "delegate_task", {"bot_id": "research", "task": "find y"}
                )
                assert unknown.isError is True
                assert json.loads(unknown.content[0].text)["status"] == "outcome_unknown"

                blocked = await session.call_tool(
                    "delegate_task", {"bot_id": "research", "task": "find z"}
                )
                assert blocked.isError is True
                assert json.loads(blocked.content[0].text)["error_code"] == "blocked"
                assert len(server.requests) == 2
