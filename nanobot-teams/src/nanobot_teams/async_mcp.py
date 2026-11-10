"""Stdio MCP adapter for the async collaboration protocol (short-lived client).

The MCP process owns no durable state: every tool call is one authenticated
HTTP request to the resident Coordinator. Closing stdio never stops remote
execution; reopening queries the same Task ids.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__

_SERVER_NAME = "nanobot-teams-async"

_LIST_BOTS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "description": "Project id from the workbench."},
        "bot": {"type": "string", "description": "Bot id reported by list_bots."},
        "input": {"type": "string", "description": "Self-contained task and expected output."},
        "operation_key": {
            "type": "string",
            "description": "Stable UUID for this submission; reuse it verbatim on retry.",
        },
        "workflow": {"type": "string", "description": "Workflow grouping for related tasks."},
        "deadline_at": {"type": "string", "description": "Optional RFC 3339 UTC deadline."},
        "delivery_mode": {
            "type": "string",
            "description": "workbench_only (default) or auto_resume.",
        },
    },
    "required": ["project", "bot", "input", "operation_key"],
    "additionalProperties": False,
}

_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
    "additionalProperties": False,
}

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "workflow": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "additionalProperties": False,
}

_LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"operation_key": {"type": "string"}},
    "required": ["operation_key"],
    "additionalProperties": False,
}

_CANCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
    "additionalProperties": False,
}


class CoordinatorHttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class CoordinatorClient:
    def __init__(self, base_url: str, token: str, *, timeout_s: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._client.request(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            raise CoordinatorHttpError(response.status_code, "invalid_response", "Non-JSON reply.")
        if response.status_code >= 400:
            code = str(payload.get("code", "error")) if isinstance(payload, dict) else "error"
            message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            raise CoordinatorHttpError(response.status_code, code, message)
        if not isinstance(payload, dict):
            raise CoordinatorHttpError(response.status_code, "invalid_response", "Expected an object.")
        return payload

    async def list_bots(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/v1/teams/bots")
        return list(payload.get("bots", []))

    async def submit_task(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/teams/tasks", json=body)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/teams/tasks/{task_id}")

    async def list_tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", "/v1/teams/tasks", params=params)

    async def lookup_task(self, operation_key: str) -> dict[str, Any] | None:
        try:
            return await self._request(
                "GET", "/v1/teams/tasks/lookup", params={"operation_key": operation_key}
            )
        except CoordinatorHttpError as exc:
            if exc.status == 404:
                return None
            raise

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/teams/tasks/{task_id}/cancel")


def build_async_server(client: CoordinatorClient) -> Server:
    server: Server = Server(_SERVER_NAME)
    log = logging.getLogger("nanobot_teams.async_mcp")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        read_only = types.ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        )
        return [
            types.Tool(
                name="list_bots",
                description="List bots registered for async collaboration.",
                inputSchema=_LIST_BOTS_SCHEMA,
                annotations=read_only,
            ),
            types.Tool(
                name="submit_task",
                description=(
                    "Submit one async task and return its Task id immediately. "
                    "The bot runs in the background; poll get_async_task."
                ),
                inputSchema=_SUBMIT_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    idempotentHint=True,
                    destructiveHint=False,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="get_async_task",
                description="Fetch one async task by id: run, cancel, and delivery states.",
                inputSchema=_GET_SCHEMA,
                annotations=read_only,
            ),
            types.Tool(
                name="list_async_tasks",
                description="List recent async tasks, optionally filtered.",
                inputSchema=_LIST_SCHEMA,
                annotations=read_only,
            ),
            types.Tool(
                name="cancel_task",
                description="Request cancellation of one async task. Actual stop is confirmed later.",
                inputSchema=_CANCEL_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=False, idempotentHint=True, destructiveHint=True,
                    openWorldHint=False,
                ),
            ),
        ]

    def _text(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            isError=is_error,
        )

    def _trusted_meta() -> dict[str, str]:
        """Read host-minted routing meta; never accepted from model arguments.

        The SDK exposes the request's ``_meta`` as ``RequestContext.meta`` (a
        ``RequestParams.Meta`` model with extra fields allowed). Older SDKs put
        it on ``request.params.meta``; both shapes are normalized here, and a
        bare dict meta is tolerated for transport-level callers.
        """
        try:
            context = server.request_context
        except LookupError:
            return {}
        raw: Any = getattr(context, "meta", None)
        if raw is None:
            params = getattr(getattr(context, "request", None), "params", None)
            raw = getattr(params, "meta", None)
        if raw is None:
            return {}
        if isinstance(raw, dict):
            entries = raw.items()
        elif hasattr(raw, "model_dump"):
            dumped = raw.model_dump(exclude_none=True)
            entries = dumped.items() if isinstance(dumped, dict) else []
        else:
            return {}
        return {
            str(k): str(v)
            for k, v in entries
            if k in ("reply_capability", "reply_workflow", "coordinator_principal")
        }

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if name == "list_bots":
                return _text({"bots": await client.list_bots()})
            if name == "submit_task":
                meta = _trusted_meta()
                forged = sorted(set(arguments) & {
                    "reply_capability", "reply_workflow", "coordinator_principal"
                })
                if forged:
                    return _text(
                        {"error_code": "forged_routing_fields",
                         "detail": f"Model arguments must not carry: {', '.join(forged)}"},
                        is_error=True,
                    )
                body = {
                    "project": arguments.get("project"),
                    "bot": arguments.get("bot"),
                    "input": arguments.get("input"),
                    "operation_key": arguments.get("operation_key"),
                }
                for optional in ("workflow", "deadline_at", "delivery_mode"):
                    if arguments.get(optional) is not None:
                        body[optional] = arguments[optional]
                if not body.get("workflow") and meta.get("reply_workflow"):
                    body["workflow"] = meta["reply_workflow"]
                if arguments.get("delivery_mode") == "auto_resume":
                    if not meta.get("reply_capability"):
                        return _text(
                            {"error_code": "origin_required",
                             "detail": "auto_resume requires a host-minted reply capability."},
                            is_error=True,
                        )
                    body["reply_capability"] = meta["reply_capability"]
                task = await client.submit_task(body)
                return _text(task)
            if name == "get_async_task":
                return _text(await client.get_task(str(arguments.get("task_id", ""))))
            if name == "list_async_tasks":
                params = {
                    k: arguments[k]
                    for k in ("project", "workflow", "limit")
                    if arguments.get(k) is not None
                }
                return _text(await client.list_tasks(params))
            if name == "cancel_task":
                return _text(await client.cancel_task(str(arguments.get("task_id", ""))))
            return _text({"error_code": "unknown_tool"}, is_error=True)
        except CoordinatorHttpError as exc:
            log.info("coordinator rejected call: %s %s", exc.code, exc.message)
            return _text({"error_code": exc.code, "detail": exc.message}, is_error=True)

    return server


async def serve_async(base_url: str, token_env: str) -> None:
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise SystemExit(f"async-mcp: environment {token_env} must carry the planner token")
    client = CoordinatorClient(base_url, token)
    try:
        server = build_async_server(client)
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
        await client.aclose()


__all__ = [
    "CoordinatorClient",
    "CoordinatorHttpError",
    "build_async_server",
    "serve_async",
]
