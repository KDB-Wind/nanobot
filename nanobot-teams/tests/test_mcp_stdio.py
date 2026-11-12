from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nanobot_teams.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
KEY = "stdio-secret"


class Target:
    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.hold_first = False
        self.first_received = threading.Event()
        self.release_first = threading.Event()
        self.first_finished = threading.Event()


class TargetServer(ThreadingHTTPServer):
    target: Target


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, OSError):
            pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        target = cast(TargetServer, self.server).target
        target.requests.append((dict(self.headers), body))
        if target.hold_first and len(target.requests) == 1:
            target.first_received.set()
            target.release_first.wait(10)
            target.first_finished.set()
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
        try:
            self.wfile.write(payload)
        except OSError:
            pass

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
    ledger_path = tmp_path / "ledger.sqlite3"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "nanobot_teams",
            "--config",
            str(write_config(tmp_path, port)),
            "--ledger",
            str(ledger_path),
        ],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": KEY, "PYTHONPATH": str(SRC)},
        cwd=str(ROOT),
    )
    async with asyncio.timeout(120):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                by_name = {tool.name: tool for tool in tools}

                assert set(by_name) == {"list_bots", "delegate_task", "list_tasks", "get_task"}
                assert by_name["list_bots"].annotations.readOnlyHint is True
                assert by_name["list_bots"].annotations.idempotentHint is True
                assert by_name["list_bots"].annotations.openWorldHint is False
                assert by_name["delegate_task"].annotations.readOnlyHint is False
                assert by_name["delegate_task"].annotations.idempotentHint is False
                assert by_name["delegate_task"].annotations.destructiveHint is True
                assert by_name["delegate_task"].annotations.openWorldHint is True
                assert by_name["list_tasks"].annotations.readOnlyHint is True
                assert by_name["get_task"].annotations.readOnlyHint is True

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
                    "expires_at",
                    "thread_key",
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
                            "model_preset": None,
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

                listed_tasks = await session.call_tool("list_tasks", {})
                assert listed_tasks.isError is False
                task_rows = json.loads(listed_tasks.content[0].text)["tasks"]
                assert len(task_rows) == 1
                assert task_rows[0]["task_id"] == payload["task_id"]
                assert task_rows[0]["status"] == "completed"
                assert task_rows[0]["http_sent"] is True

                fetched = await session.call_tool(
                    "get_task", {"task_id": payload["task_id"]}
                )
                assert fetched.isError is False
                assert json.loads(fetched.content[0].text)["target"] == "research"

                missing = await session.call_tool("get_task", {"task_id": "no-such"})
                assert missing.isError is True
                assert json.loads(missing.content[0].text)["error_code"] == "not_found"

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


class RawMCP:
    def __init__(self, config: Path, ledger: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "nanobot_teams", "--config", str(config), "--ledger", str(ledger)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=ROOT,
            env={**os.environ, "TEAMS_RESEARCH_API_KEY": KEY, "PYTHONPATH": str(SRC)},
        )

    def send(self, method: str, params: dict, request_id: int | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    async def response(self, request_id: int) -> dict:
        async with asyncio.timeout(5):
            while True:
                line = await asyncio.to_thread(self.process.stdout.readline)
                assert line, "MCP process closed before returning the response"
                message = json.loads(line)
                if message.get("id") == request_id:
                    return message

    async def initialize(self) -> None:
        self.send("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                 "clientInfo": {"name": "recovery-regression", "version": "1"}}, 0)
        assert "result" in await self.response(0)
        self.send("notifications/initialized", {})

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def close(self) -> None:
        self.kill()
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()


@pytest.mark.parametrize("interrupt", ["cancel_notification", "kill_process"])
async def test_stdio_interruption_retains_unknown_target(tmp_path: Path, target, interrupt: str) -> None:
    remote, port = target
    remote.hold_first = True
    config = write_config(tmp_path, port)
    path = tmp_path / "state.sqlite3"
    client = RawMCP(config, path)
    fresh = None
    try:
        await client.initialize()
        client.send("tools/call", {"name": "delegate_task", "arguments": {"bot_id": "research", "task": "first"}}, 1)
        assert await asyncio.to_thread(remote.first_received.wait, 5)
        observer = Ledger(path, read_only=True)
        try:
            admitted = observer.list_tasks()
            assert len(admitted) == 1 and admitted[0].blocked
        finally:
            observer.close()

        if interrupt == "cancel_notification":
            client.send("notifications/cancelled", {"requestId": 1, "reason": "caller stopped waiting"})
            active = client
        else:
            client.kill()
            fresh = RawMCP(config, path)
            await fresh.initialize()
            active = fresh
        active.send("tools/call", {"name": "delegate_task", "arguments": {"bot_id": "research", "task": "second"}}, 2)
        response = await active.response(2)
        payload = json.loads(response["result"]["content"][0]["text"])
        assert payload["error_code"] == "blocked"
        assert len(remote.requests) == 1
        observer = Ledger(path, read_only=True)
        try:
            row = observer.list_tasks()[0]
            assert row.status == "outcome_unknown" and row.dispatch_state == "unknown"
            assert row.error_code == ("cancelled" if interrupt == "cancel_notification" else "interrupted")
        finally:
            observer.close()
    finally:
        remote.release_first.set()
        client.close()
        if fresh is not None:
            fresh.close()


async def test_stdio_second_writer_exits_before_serving(tmp_path: Path, target) -> None:
    _, port = target
    config = write_config(tmp_path, port)
    path = tmp_path / "state.sqlite3"
    client = RawMCP(config, path)
    try:
        await client.initialize()
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "nanobot_teams", "--config", str(config), "--ledger", str(path)],
            input="", capture_output=True, text=True, timeout=5,
            env={**os.environ, "TEAMS_RESEARCH_API_KEY": KEY, "PYTHONPATH": str(SRC)})
        assert result.returncode == 2
        assert "another writer" in result.stderr
    finally:
        client.close()
