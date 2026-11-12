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

# Same bounded read contract as the bot-side control client (F15/M04): the
# coordinator control client must not decode compression, must cap the raw
# body at the protocol limit, and must reject non-JSON through its typed error.
MAX_RESPONSE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 16

_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recipient": {"type": "string", "description": "Target bot id from list_bots."},
        "input": {"type": "string", "description": "Self-contained task and expected output."},
        "operation_key": {
            "type": "string",
            "description": "Stable UUID for this send; reuse it verbatim on retry.",
        },
        "conversation_root": {
            "type": "string",
            "description": "Conversation UUID when continuing one; omit to start fresh.",
        },
        "hop": {"type": "integer", "description": "Hop index of this send; 0 when fresh."},
        "project": {"type": "string", "description": "Optional project id; defaults to peer."},
        "deadline_at": {"type": "string", "description": "Optional RFC 3339 UTC deadline."},
    },
    "required": ["recipient", "input", "operation_key"],
    "additionalProperties": False,
}

def _normalize_operation_key(raw: str) -> str:
    """Accept any stable model-chosen key, forward a protocol UUID.

    The runs protocol hard-requires UUID Idempotency-Keys; models sometimes
    invent readable keys instead. A non-UUID key maps deterministically to
    uuid5 so retries with the same model string stay one business send.
    """
    import uuid as _uuid

    candidate = raw.strip()
    try:
        return str(_uuid.UUID(candidate))
    except (ValueError, AttributeError):
        return str(
            _uuid.uuid5(_uuid.NAMESPACE_URL, f"nanobot-teams/peer/{candidate}")
        )


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
    "properties": {
        "task_id": {"type": "string"},
        "include_result": {
            "type": "boolean",
            "description": (
                "Also fetch the terminal result body through the coordinator's "
                "public result route (no bot credentials involved)."
            ),
        },
    },
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


def _strict_json_object(raw: bytes) -> dict[str, Any] | None:
    """Parse a bounded JSON object; duplicate keys and over-deep nests fail."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(text, object_pairs_hook=pairs_hook)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict) or _depth(parsed) > MAX_JSON_DEPTH:
        return None
    return parsed


def _depth(value: object, level: int = 0) -> int:
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level


async def _bounded_payload(response: httpx.Response) -> dict[str, Any]:
    """Read a coordinator reply within the same bounds the bot client uses.

    Compression is rejected from the headers before decoding, the declared
    length is checked, and the raw stream stops at the 256 KiB cap (M04).
    """
    status = response.status_code
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise CoordinatorHttpError(
            status, "response_compressed", "Compressed replies are not accepted."
        )
    declared = response.headers.get("Content-Length", "")
    if declared:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise CoordinatorHttpError(
                    status, "response_too_large", "Reply exceeds the 256 KiB protocol limit."
                )
        except ValueError:
            raise CoordinatorHttpError(status, "invalid_response", "Invalid Content-Length.")
    raw = b""
    async for chunk in response.aiter_raw():
        raw += chunk
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CoordinatorHttpError(
                status, "response_too_large", "Reply exceeds the 256 KiB protocol limit."
            )
    payload = _strict_json_object(raw)
    if payload is None:
        raise CoordinatorHttpError(status, "invalid_response", "Non-JSON reply.")
    return payload


class CoordinatorClient:
    def __init__(self, base_url: str, token: str, *, timeout_s: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            follow_redirects=False,
            # F14: the planner token never traverses an environment proxy.
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        request = self._client.build_request(method, path, **kwargs)
        response = await self._client.send(request, stream=True)
        try:
            payload = await _bounded_payload(response)
        finally:
            await response.aclose()
        if response.status_code >= 400:
            code = str(payload.get("code", "error"))
            message = str(payload.get("message", ""))
            raise CoordinatorHttpError(response.status_code, code, message)
        return payload

    async def list_bots(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/v1/teams/bots")
        return list(payload.get("bots", []))

    async def submit_task(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/teams/tasks", json=body)

    async def send_to_bot(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/teams/peer/send", json=body)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/teams/tasks/{task_id}")

    async def get_task_result(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/teams/tasks/{task_id}/result")

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
                name="send_to_bot",
                description=(
                    "Dispatch one async task to a peer bot along an authorized "
                    "edge and return its Task id immediately. The reply returns "
                    "to this bot's own inbox; poll get_async_task meanwhile."
                ),
                inputSchema=_SEND_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    idempotentHint=True,
                    destructiveHint=False,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="get_async_task",
                description=(
                    "Fetch one async task by id: run, cancel, and delivery states. "
                    "Pass include_result=true to also read the terminal result body."
                ),
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
                    if meta.get("reply_workflow"):
                        # Workflow root continuity (F11): an auto-resume chain
                        # stays under the session's original workflow root;
                        # the model cannot fragment it into a fresh root.
                        body["workflow"] = meta["reply_workflow"]
                task = await client.submit_task(body)
                return _text(task)
            if name == "send_to_bot":
                meta = _trusted_meta()
                forged = sorted(set(arguments) & {
                    "reply_capability", "reply_workflow", "coordinator_principal",
                    "sender",
                })
                if forged:
                    return _text(
                        {"error_code": "forged_routing_fields",
                         "detail": f"Model arguments must not carry: {', '.join(forged)}"},
                        is_error=True,
                    )
                body = {
                    "recipient": arguments.get("recipient"),
                    "input": arguments.get("input"),
                    "operation_key": _normalize_operation_key(
                        str(arguments.get("operation_key", ""))
                    ),
                }
                for optional in ("conversation_root", "hop", "project", "deadline_at"):
                    if arguments.get(optional) is not None:
                        body[optional] = arguments[optional]
                if meta.get("reply_capability"):
                    # Peer auto-return: the sender host minted this capability
                    # for its own session; the reply routes back here and the
                    # workflow must match the capability binding.
                    body["reply_capability"] = meta["reply_capability"]
                    if meta.get("reply_workflow"):
                        body["workflow"] = meta["reply_workflow"]
                return _text(await client.send_to_bot(body))
            if name == "get_async_task":
                task_id = str(arguments.get("task_id", ""))
                payload = await client.get_task(task_id)
                if arguments.get("include_result"):
                    # Public result route through the coordinator; the model
                    # never touches bot service credentials.
                    try:
                        result = await client.get_task_result(task_id)
                    except CoordinatorHttpError as exc:
                        if exc.code == "result_fetch_pending":
                            payload["result_pending"] = True
                        else:
                            raise
                    else:
                        payload["result_text"] = result.get("result_text", "")
                        payload["result_truncated"] = result.get(
                            "result_truncated", False
                        )
                return _text(payload)
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
