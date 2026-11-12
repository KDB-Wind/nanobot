"""Four MCP tools, in text JSON with isError on every non-completed result."""

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
from .ledger import Ledger, LedgerError
from .transport import HttpTransport, OutcomeKind

_SERVER_NAME = "nanobot-teams"

_LIST_BOTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_LIST_TASKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "description": "Rows to return, 1 to 500."},
        "target": {"type": "string", "description": "Only rows for this bot id."},
        "status": {
            "type": "string",
            "description": "completed, failed, timed_out, or outcome_unknown.",
        },
    },
    "additionalProperties": False,
}

_GET_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Task id from a delegation result."}
    },
    "required": ["task_id"],
    "additionalProperties": False,
}

_DELEGATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bot_id": {"type": "string", "maxLength": 64, "description": "Bot id reported by list_bots."},
        "task": {"type": "string", "description": "Self-contained task and expected output."},
        "parent_task_id": {
            "type": "string",
            "description": "Task id from the envelope block you are serving, when continuing a chain.",
        },
        "root_task_id": {
            "type": "string",
            "description": "Root task id copied from the envelope block, when continuing a chain.",
        },
        "hop": {
            "type": "integer",
            "description": "Hop number copied from the envelope block, when continuing a chain.",
        },
        "deadline_remaining_s": {
            "type": "number",
            "description": "Remaining chain budget in seconds, copied from the envelope block.",
        },
        "thread_key": {"type": "string", "maxLength": 128,
                       "description": "Continue a thread_key returned for this target; omit for an isolated new task."},
        "expires_at": {"type": "number",
                       "description": "Unix expiry copied with a multi-hop envelope; advisory, not authenticated."},
        "chain": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Bot chain copied from the envelope block, ending with your own id.",
        },
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
                    destructiveHint=True,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="list_tasks",
                description=(
                    "List recent delegations from the local ledger. Local observation "
                    "only; completed is not proof of remote success. Backed by durable local control state."
                ),
                inputSchema=_LIST_TASKS_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="get_task",
                description="Fetch one ledger row by task id. Local observation only.",
                inputSchema=_GET_TASK_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
        ]

    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        if name == "list_bots":
            text = json.dumps(
                {"bots": delegator.list_bots()}, ensure_ascii=False, separators=(",", ":")
            )
            return _text_result(text, is_error=False)
        if name == "list_tasks":
            limit = arguments.get("limit", 50)
            target = arguments.get("target")
            status = arguments.get("status")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 500
                or (target is not None and not isinstance(target, str))
                or (status is not None and not isinstance(status, str))
            ):
                return _error_result("invalid_request", "limit, target, or status is malformed.")
            rows = delegator.list_tasks(limit=limit, target=target, status=status)
            text = json.dumps(
                {"tasks": [json.loads(row.to_json()) for row in rows]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return _text_result(text, is_error=False)
        if name == "get_task":
            task_id = arguments.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                return _error_result("invalid_request", "task_id must be a non-empty string.")
            row = delegator.get_task(task_id)
            if row is None:
                return _error_result("not_found", "No ledger row for that task id.")
            return _text_result(row.to_json(), is_error=False)
        if name == "delegate_task":
            bot_id = arguments.get("bot_id")
            task = arguments.get("task")
            if not isinstance(bot_id, str) or not isinstance(task, str):
                return _error_result("invalid_request", "bot_id and task must be strings.")
            result = await delegator.delegate_task(
                bot_id,
                task,
                parent_task_id=_optional_str(arguments.get("parent_task_id")),
                root_task_id=_optional_str(arguments.get("root_task_id")),
                hop=arguments.get("hop"),
                deadline_remaining_s=arguments.get("deadline_remaining_s"),
                chain=arguments.get("chain"),
                expires_at=arguments.get("expires_at"),
                thread_key=arguments.get("thread_key"),
            )
            is_error = result.status is not OutcomeKind.completed
            return _text_result(result.to_json(), is_error=is_error)
        return _error_result("unknown_tool", "Unknown tool.")

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            return await _call_tool(name, arguments)
        except LedgerError:
            return _error_result("ledger_unavailable", "Cannot read durable control state.")

    return server


async def serve(
    config: TeamConfig,
    api_keys: Mapping[str, str],
    caller_id: str,
    ledger: Ledger | None = None,
) -> None:
    book = ledger if ledger is not None else Ledger(None)
    if not book.enabled:
        raise LedgerError("A persistent ledger is required.")
    book.bind(config.team_id, caller_id)
    transport = HttpTransport(config.limits)
    try:
        server = build_server(Delegator(config, transport, api_keys, caller_id, ledger=book))
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
        book.close()


def _text_result(text: str, *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _error_result(error_code: str, detail: str) -> types.CallToolResult:
    text = json.dumps({"error_code": error_code, "detail": detail}, separators=(",", ":"))
    return _text_result(text, is_error=True)
