"""Planner inbox HTTP protocol: capability validation and durable deliveries."""

from __future__ import annotations

import hmac
import json
from typing import Any

from aiohttp import web

from nanobot.api.agent_runs import parse_protocol_json
from nanobot.config.schema import ApiRunsConfig
from nanobot.session.external_inbox import (
    ExternalInbox,
    InboxDelivery,
    InboxError,
    delivery_body_digest,
)

_INBOX_KEY = web.AppKey[ExternalInbox]("agent_inbox")
_INBOX_CONFIG_KEY = web.AppKey[ApiRunsConfig]("agent_inbox_config")

INBOX_FIELDS = frozenset(
    {
        "delivery_id",
        "reply_capability",
        "operation_key",
        "bot_id",
        "instance_id",
        "task_id",
        "run_id",
        "terminal_revision",
        "outcome",
        "result_text",
        "result_truncated",
        "workflow_id",
    }
)


def _json(payload: Any, status: int = 200) -> web.Response:
    import json as _json_module

    return web.Response(
        text=_json_module.dumps(payload, ensure_ascii=False),
        content_type="application/json",
        status=status,
    )


def _error(status: int, code: str, message: str) -> web.Response:
    return _json({"code": code, "message": message}, status=status)



def _error_from_protocol(exc: web.HTTPException) -> web.Response:
    """Convert a protocol_error response into an inbox _error response."""
    try:
        parsed_detail: Any = json.loads(exc.text or "{}")
    except ValueError:
        parsed_detail = {}
    # json.loads is an Any boundary; the isinstance guard is the runtime check.
    detail: dict[str, Any] = (
        dict(parsed_detail)  # pyright: ignore[reportUnknownArgumentType]
        if isinstance(parsed_detail, dict)
        else {}
    )
    return _error(
        int(exc.status or 400),
        str(detail.get("code", "invalid_json")),
        str(detail.get("message", "Body rejected.")),
    )

def _bounded_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("terminal_revision must be an integer")
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError("terminal_revision must be an integer") from None
    if parsed < 0:
        raise ValueError("terminal_revision must be >= 0")
    return parsed


def _require_inbox_auth(request: web.Request) -> web.Response | None:
    config: ApiRunsConfig = request.app[_INBOX_CONFIG_KEY]
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return _error(401, "missing_bearer", "Use the dedicated runs/inbox key.")
    if not hmac.compare_digest(auth[len("Bearer "):], config.api_key):
        return _error(401, "invalid_bearer", "Invalid key.")
    return None


async def handle_capability_validate(request: web.Request) -> web.Response:
    denied = _require_inbox_auth(request)
    if denied is not None:
        return denied
    inbox: ExternalInbox = request.app[_INBOX_KEY]
    config: ApiRunsConfig = request.app[_INBOX_CONFIG_KEY]
    try:
        body = await parse_protocol_json(request)
    except web.HTTPException as exc:
        return _error_from_protocol(exc)
    required = {"capability", "workflow", "operation_key", "bot_id", "instance_id"}
    missing = required - set(body)
    if missing:
        return _error(400, "invalid_field", f"Missing fields: {sorted(missing)}")
    try:
        inbox.validate_capability(
            token=str(body["capability"]),
            workflow_id=str(body["workflow"]),
            operation_key=str(body["operation_key"]),
            bot_id=str(body["bot_id"]),
            instance_id=str(body["instance_id"]),
            principal_id=config.principal_id,
        )
    except InboxError as exc:
        return _error(422, exc.code, exc.detail)
    # Short-lived metadata answer only; never leaks the session address.
    return _json({"valid": True})


async def handle_inbox_receive(request: web.Request) -> web.Response:
    denied = _require_inbox_auth(request)
    if denied is not None:
        return denied
    inbox: ExternalInbox = request.app[_INBOX_KEY]
    config: ApiRunsConfig = request.app[_INBOX_CONFIG_KEY]
    try:
        body = await parse_protocol_json(request)
    except web.HTTPException as exc:
        return _error_from_protocol(exc)
    unknown = set(body) - INBOX_FIELDS
    if unknown:
        return _error(400, "unknown_fields", f"Unknown fields: {sorted(unknown)}")
    try:
        capability = inbox.validate_capability(
            token=str(body.get("reply_capability", "")),
            workflow_id=str(body.get("workflow_id", "")),
            operation_key=str(body.get("operation_key", "")),
            bot_id=str(body.get("bot_id", "")),
            instance_id=str(body.get("instance_id", "")),
            principal_id=config.principal_id,
            # Delivery admission: revoked workflows still deliver results, as
            # durable card-only receipts. New-task admission (the validate
            # route) keeps requiring the live grant.
            require_auto_continue=False,
        )
    except InboxError as exc:
        status = 410 if exc.code == "stale_origin" else 422
        return _error(status, exc.code, exc.detail)
    result_text = str(body.get("result_text", ""))
    if len(result_text.encode("utf-8")) > 32 * 1024:
        return _error(413, "result_too_large", "result_text exceeds 32 KiB.")
    try:
        terminal_revision = _bounded_int(body.get("terminal_revision", 0))
    except ValueError as exc:
        return _error(400, "invalid_field", str(exc))
    outcome = body.get("outcome")
    outcome_code = ""
    if isinstance(outcome, dict):
        # Same Any boundary as above; the key read is guarded at runtime.
        outcome_code = str(
            outcome.get("code", "")  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        )
    delivery = InboxDelivery(
        delivery_id=str(body.get("delivery_id", "")),
        session_key=capability.session_key,
        session_generation=capability.session_generation,
        workflow_id=capability.workflow_id,
        task_id=str(body.get("task_id", "")),
        bot_id=capability.bot_id,
        instance_id=capability.instance_id,
        run_id=str(body.get("run_id", "")),
        terminal_revision=terminal_revision,
        outcome_code=outcome_code,
        result_text=result_text,
        result_truncated=bool(body.get("result_truncated", False)),
        body_digest=delivery_body_digest(
            task_id=str(body.get("task_id", "")),
            bot_id=capability.bot_id,
            instance_id=capability.instance_id,
            run_id=str(body.get("run_id", "")),
            terminal_revision=terminal_revision,
            outcome_code=outcome_code,
            result_text=result_text,
        ),
    )
    try:
        receipt = inbox.receive(delivery, capability_digest=capability.digest)
    except InboxError as exc:
        return _error(409, exc.code, exc.detail)
    except ValueError as exc:
        return _error(400, "invalid_field", str(exc))
    status = 202 if receipt.state == "received" else 200
    response = _json(
        {
            "delivery_id": receipt.delivery_id,
            "state": receipt.state,
            "received_at": receipt.received_at,
        },
        status=status,
    )
    if receipt.state == "received":
        # Wake the planner turn after the durable receipt; failure only delays
        # injection to the recovery scan, it never loses the receipt.
        try:
            loop = __import__("asyncio").get_event_loop()
            loop.create_task(inbox.apply_pending(delivery.delivery_id))
        except Exception:
            pass
    return response


async def handle_inbox_receipt(request: web.Request) -> web.Response:
    denied = _require_inbox_auth(request)
    if denied is not None:
        return denied
    inbox: ExternalInbox = request.app[_INBOX_KEY]
    receipt = inbox.get_receipt(request.match_info["delivery_id"])
    if receipt is None:
        return _error(404, "receipt_not_found", "No delivery with that id.")
    return _json(
        {
            "delivery_id": receipt.delivery_id,
            "state": receipt.state,
            "received_at": receipt.received_at,
            "injected_at": receipt.injected_at,
        }
    )


def register_inbox_routes(
    app: web.Application, inbox: ExternalInbox, config: ApiRunsConfig
) -> None:
    app[_INBOX_KEY] = inbox
    app[_INBOX_CONFIG_KEY] = config
    app.router.add_post("/v1/agent-inbox/capabilities/validate", handle_capability_validate)
    app.router.add_post("/v1/agent-inbox", handle_inbox_receive)
    app.router.add_get("/v1/agent-inbox/{delivery_id}", handle_inbox_receipt)


__all__ = ["register_inbox_routes"]
