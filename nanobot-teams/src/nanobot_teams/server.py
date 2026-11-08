"""The two MCP tools, in text JSON with isError on every non-completed result."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__
from .config import TeamConfig
from .delegation import Delegator
from .transport import HttpTransport, OutcomeKind

_SERVER_NAME = "nanobot-teams"

_LIST_BOTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_DELEGATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bot_id": {"type": "string", "description": "Bot id reported by list_bots."},
        "task": {"type": "string", "description": "Self-contained task and expected output."},
    },
    "required": ["bot_id", "task"],
    "additionalProperties": False,
}


def build_server(delegator: Delegator) -> Server:
    server: Server = Server(_SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="list_bots",
                description="List the bots this caller may delegate a task to.",
                inputSchema=_LIST_BOTS_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="delegate_task",
                description="Send one task to another bot and wait for its answer.",
                inputSchema=_DELEGATE_TASK_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    idempotentHint=False,
                    destructiveHint=False,
                    openWorldHint=True,
                ),
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name == "list_bots":
            text = json.dumps(
                {"bots": delegator.list_bots()}, ensure_ascii=False, separators=(",", ":")
            )
            return _text_result(text, is_error=False)
        if name == "delegate_task":
            bot_id = arguments.get("bot_id")
            task = arguments.get("task")
            if not isinstance(bot_id, str) or not isinstance(task, str):
                return _error_result("invalid_request", "bot_id and task must be strings.")
            result = await delegator.delegate_task(bot_id, task)
            is_error = result.status is not OutcomeKind.completed
            return _text_result(result.to_json(), is_error=is_error)
        return _error_result("unknown_tool", "Unknown tool.")

    return server


async def serve(config: TeamConfig, api_keys: Mapping[str, str]) -> None:
    transport = HttpTransport(config.limits)
    try:
        server = build_server(Delegator(config, transport, api_keys))
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=_SERVER_NAME,
                    server_version=__version__,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await transport.aclose()


def _text_result(text: str, *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _error_result(error_code: str, detail: str) -> types.CallToolResult:
    text = json.dumps({"error_code": error_code, "detail": detail}, separators=(",", ":"))
    return _text_result(text, is_error=True)
