"""Public agent-run protocol v1 HTTP routes (mounted by serve and the gateway sidecar).

All routes authenticate against the dedicated ``api.runs.api_key``; the plain
``api.api_key`` never grants access here. Errors use
``{code, message, retryable, request_id}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import math
import uuid
from typing import Any, cast

from aiohttp import web

from nanobot.config.schema import ApiRunsConfig
from nanobot.runs.service import (
    EventCursorExpiredError,
    IdempotencyConflictError,
    InputTooLargeError,
    InvalidSubmitError,
    RunService,
)
from nanobot.runs.store import OperationKeyRetiredError
from nanobot.runs.types import (
    EVENT_MAX_BYTES,
    INPUT_MAX_BYTES,
    PROTOCOL_VERSION,
    RESULT_MAX_BYTES,
    Run,
    RunSubmit,
    parse_rfc3339,
    rfc3339,
)

RAW_BODY_MAX_BYTES = 256 * 1024
JSON_MAX_DEPTH = 16
SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_SECONDS = 0.4

_RUNS_SERVICE_KEY = web.AppKey[RunService]("agent_runs_service")
_RUNS_CONFIG_KEY = web.AppKey[ApiRunsConfig]("agent_runs_config")

RUNS_ROUTE_PREFIX = "/v1/agent-"


_STATUS_EXCEPTIONS: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    410: web.HTTPGone,
    413: web.HTTPRequestEntityTooLarge,
}


def protocol_error(status: int, code: str, message: str, *, retryable: bool = False) -> web.HTTPException:
    """Build a protocol-shaped error response: {code, message, retryable, request_id}."""
    payload = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": uuid.uuid4().hex[:16],
    }
    exc_cls = _STATUS_EXCEPTIONS.get(status, web.HTTPBadRequest)
    if exc_cls is web.HTTPRequestEntityTooLarge:
        return exc_cls(
            max_size=RAW_BODY_MAX_BYTES, text=_dumps(payload), content_type="application/json"
        )
    return exc_cls(text=_dumps(payload), content_type="application/json")


def _protocol_json_response(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def parse_protocol_json(request: web.Request) -> dict[str, Any]:
    """Read one bounded JSON body: no compression, no duplicate keys, finite numbers."""
    encoding = (request.headers.get("Content-Encoding") or "").lower()
    if encoding and encoding != "identity":
        raise protocol_error(400, "compressed_body_rejected", "Compressed bodies are not accepted.")
    raw = await request.content.read(RAW_BODY_MAX_BYTES + 1)
    if len(raw) > RAW_BODY_MAX_BYTES:
        raise protocol_error(413, "body_too_large", "Body exceeds the 256 KiB protocol limit.")

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise protocol_error(400, "duplicate_key", f"Duplicate object key: {key}")
            result[key] = value
        return result

    def _constant(name: str) -> float:
        raise protocol_error(400, "non_finite_number", f"{name} is not a finite number.")

    try:
        parsed: Any = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except protocol_error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise protocol_error(400, "invalid_json", "Body is not valid JSON.")
    if not isinstance(parsed, dict):
        raise protocol_error(400, "invalid_json", "Body must be a JSON object.")
    body = cast("dict[str, Any]", parsed)
    _check_depth(body, 0)
    return body


def _check_depth(value: Any, depth: int) -> None:
    if depth > JSON_MAX_DEPTH:
        raise protocol_error(400, "json_too_deep", "JSON nesting exceeds 16 levels.")
    if isinstance(value, dict):
        items: list[Any] = list(cast("dict[str, Any]", value).values())
    elif isinstance(value, list):
        items = cast("list[Any]", value)
    else:
        if isinstance(value, float) and not math.isfinite(value):
            raise protocol_error(400, "non_finite_number", "Numbers must be finite.")
        return
    for item in items:
        _check_depth(item, depth + 1)


def _run_status(run: Run, *, result_available: bool) -> dict[str, Any]:
    outcome: dict[str, Any] | None = None
    if run.outcome is not None:
        outcome = {
            "code": run.outcome.code.value,
            "message": run.outcome.message,
            "truncated": run.outcome.truncated,
        }
    return {
        "protocol": PROTOCOL_VERSION,
        "instance_id": run.instance_id,
        "run_id": run.run_id,
        "task_id": run.task_id,
        "workflow_id": run.workflow_id,
        "state": run.state.value,
        "revision": run.revision,
        "phase": run.phase,
        "deadline_at": rfc3339(run.deadline_at),
        "stop_confirmed": run.stop_confirmed,
        "effects_uncertain": run.effects_uncertain,
        "outcome": outcome,
        "result_available": result_available,
    }


def _require_runs_auth(request: web.Request) -> None:
    config: ApiRunsConfig = request.app[_RUNS_CONFIG_KEY]
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise protocol_error(401, "missing_bearer", "Use: Bearer <api.runs.api_key>.")
    if not hmac.compare_digest(auth[len("Bearer "):], config.api_key):
        raise protocol_error(401, "invalid_bearer", "Invalid run-protocol key.")


def _require_execution(request: web.Request) -> None:
    config: ApiRunsConfig = request.app[_RUNS_CONFIG_KEY]
    if not config.execution_enabled:
        raise protocol_error(
            403, "execution_disabled", "This instance does not accept agent runs."
        )


async def handle_capabilities(request: web.Request) -> web.Response:
    _require_runs_auth(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    config: ApiRunsConfig = request.app[_RUNS_CONFIG_KEY]
    return _protocol_json_response(
        {
            "protocol": PROTOCOL_VERSION,
            "instance_id": service.instance_id,
            "execution": config.execution_enabled,
            "inbox": config.inbox_enabled,
            "exclusive_execution": config.exclusive_execution,
            "limits": {
                "input_max_bytes": INPUT_MAX_BYTES,
                "result_max_bytes": RESULT_MAX_BYTES,
                "event_max_bytes": EVENT_MAX_BYTES,
            },
        }
    )


async def handle_submit(request: web.Request) -> web.Response:
    _require_runs_auth(request)
    _require_execution(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    config: ApiRunsConfig = request.app[_RUNS_CONFIG_KEY]
    operation_key = request.headers.get("Idempotency-Key", "").strip()
    if not operation_key:
        raise protocol_error(400, "idempotency_key_required", "The Idempotency-Key header is required.")
    body = await parse_protocol_json(request)
    known = {"protocol", "task_id", "workflow_id", "input_text", "deadline_at"}
    unknown = set(body) - known
    if unknown:
        raise protocol_error(
            400, "unknown_fields", f"Unknown control fields rejected: {sorted(unknown)}"
        )
    if body.get("protocol") != PROTOCOL_VERSION:
        raise protocol_error(400, "protocol_mismatch", "protocol must be the integer 1.")
    for field in ("task_id", "workflow_id", "input_text"):
        if not isinstance(body.get(field), str):
            raise protocol_error(400, "invalid_field", f"{field} must be a string.")
    deadline_raw = body.get("deadline_at")
    if not isinstance(deadline_raw, str):
        raise protocol_error(400, "invalid_field", "deadline_at must be an RFC 3339 string.")
    try:
        deadline = parse_rfc3339(deadline_raw)
    except ValueError:
        raise protocol_error(400, "invalid_field", "deadline_at must be RFC 3339 with timezone.")
    submit = RunSubmit(
        task_id=body["task_id"],
        workflow_id=body["workflow_id"],
        input_text=body["input_text"],
        deadline_at=deadline,
        operation_key=operation_key,
        principal_id=config.principal_id,
    )
    try:
        acceptance = await service.submit(submit)
    except IdempotencyConflictError:
        raise protocol_error(409, "idempotency_conflict", "Same key with a different body.")
    except OperationKeyRetiredError:
        raise protocol_error(410, "operation_key_retired", "This key was cleaned up; never reused.")
    except InputTooLargeError as exc:
        raise protocol_error(413, "input_too_large", exc.detail)
    except (InvalidSubmitError,) as exc:
        raise protocol_error(400, "invalid_submit", exc.detail)
    run = acceptance.run
    status = 200 if run.terminal else 202
    return _protocol_json_response(_run_status(run, result_available=run.terminal), status=status)


async def handle_lookup(request: web.Request) -> web.Response:
    _require_runs_auth(request)
    _require_execution(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    config: ApiRunsConfig = request.app[_RUNS_CONFIG_KEY]
    operation_key = request.query.get("operation_key", "")
    if not operation_key:
        raise protocol_error(400, "operation_key_required", "operation_key is required.")
    run = await service.lookup(config.principal_id, operation_key)
    if run is None:
        raise protocol_error(
            404, "run_not_found", "No run for that key. Reconciliation does not authorize a new key."
        )
    return _protocol_json_response(_run_status(run, result_available=run.terminal))


async def handle_get_run(request: web.Request) -> web.Response:
    _require_runs_auth(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    try:
        run = await service.get(request.match_info["run_id"])
    except Exception:
        raise protocol_error(404, "run_not_found", "No run with that id.")
    return _protocol_json_response(_run_status(run, result_available=run.terminal))


async def handle_get_result(request: web.Request) -> web.Response:
    _require_runs_auth(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    try:
        run = await service.get(request.match_info["run_id"])
    except Exception:
        raise protocol_error(404, "run_not_found", "No run with that id.")
    if not run.terminal:
        raise protocol_error(409, "not_finished", "The result exists only in a terminal state.")
    if run.result_cleaned_at is not None:
        raise protocol_error(410, "result_cleaned", "The result text was cleaned by retention.")
    outcome = run.outcome
    return _protocol_json_response(
        {
            "protocol": PROTOCOL_VERSION,
            "run_id": run.run_id,
            "state": run.state.value,
            "outcome": None
            if outcome is None
            else {
                "code": outcome.code.value,
                "message": outcome.message,
                "text": outcome.partial,
                "truncated": outcome.truncated,
                "original_bytes": outcome.partial_bytes,
            },
        }
    )


async def handle_cancel(request: web.Request) -> web.Response:
    _require_runs_auth(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    try:
        run = await service.cancel(request.match_info["run_id"])
    except Exception:
        raise protocol_error(404, "run_not_found", "No run with that id.")
    return _protocol_json_response(
        _run_status(run, result_available=run.terminal), status=202
    )


async def handle_events(request: web.Request) -> web.StreamResponse:
    _require_runs_auth(request)
    service: RunService = request.app[_RUNS_SERVICE_KEY]
    last_event_id = request.headers.get("Last-Event-ID")
    after_raw = request.query.get("after")
    if last_event_id is not None:
        after = _parse_seq(last_event_id)
    elif after_raw is not None:
        after = _parse_seq(after_raw)
    else:
        after = 0
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)
    heartbeat_at = asyncio.get_event_loop().time() + SSE_HEARTBEAT_SECONDS
    try:
        while True:
            try:
                events = await service.events(after)
            except EventCursorExpiredError:
                await resp.write(
                    b"event: cursor_expired\ndata: {\"code\":\"event_cursor_expired\"}\n\n"
                )
                return resp
            for event in events:
                payload = _dumps(
                    {
                        "seq": event.seq,
                        "run_id": event.run_id,
                        "type": event.type,
                        "at": rfc3339(event.at),
                        "data": event.data,
                    }
                ).encode("utf-8")
                if len(payload) > EVENT_MAX_BYTES:
                    continue
                await resp.write(
                    f"id: {event.seq}\nevent: {event.type}\ndata: {payload.decode('utf-8')}\n\n".encode()
                )
                after = event.seq
            now = asyncio.get_event_loop().time()
            if now >= heartbeat_at:
                await resp.write(b": heartbeat\n\n")
                heartbeat_at = now + SSE_HEARTBEAT_SECONDS
            await asyncio.sleep(SSE_POLL_SECONDS)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await resp.write_eof()
    return resp


def _parse_seq(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise protocol_error(400, "invalid_cursor", "Event cursor must be an integer seq.")
    if parsed < 0:
        raise protocol_error(400, "invalid_cursor", "Event cursor must be >= 0.")
    return parsed


def register_runs_routes(app: web.Application, service: RunService, config: ApiRunsConfig) -> None:
    """Mount the agent-run routes and their dedicated auth."""
    app[_RUNS_SERVICE_KEY] = service
    app[_RUNS_CONFIG_KEY] = config
    app.router.add_get("/v1/agent-capabilities", handle_capabilities)
    app.router.add_post("/v1/agent-runs", handle_submit)
    app.router.add_get("/v1/agent-runs/lookup", handle_lookup)
    app.router.add_get("/v1/agent-runs/events", handle_events)
    app.router.add_get("/v1/agent-runs/{run_id}", handle_get_run)
    app.router.add_get("/v1/agent-runs/{run_id}/result", handle_get_result)
    app.router.add_post("/v1/agent-runs/{run_id}/cancel", handle_cancel)


__all__ = [
    "protocol_error",
    "RAW_BODY_MAX_BYTES",
    "RUNS_ROUTE_PREFIX",
    "handle_capabilities",
    "handle_cancel",
    "handle_events",
    "handle_get_result",
    "handle_get_run",
    "handle_lookup",
    "handle_submit",
    "parse_protocol_json",
    "register_runs_routes",
]
