"""Coordinator HTTP API (v1): role tokens, task admission, catalog, snapshot, SSE."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .config import CoordinatorConfig, PlannerBinding
from .models import (
    TERMINAL_RUN_STATES,
    Bot,
    CancelState,
    DeliveryMode,
    DispatchState,
)
from .store import (
    AdmissionError,
    CoordinatorStore,
    IdempotencyConflictError,
    task_digest,
)
from .worker import task_public_json

PAGE_MAX = 100
SSE_HEARTBEAT_SECONDS = 15.0
WORKFLOW_STOP_TASK_LIMIT = 500
_STORE_KEY = web.AppKey[CoordinatorStore]("coordinator_store")
_CONFIG_KEY = web.AppKey[CoordinatorConfig]("coordinator_config")
_ROLE_KEY = web.RequestKey[str]("coordinator_role")


def _json_response(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def api_error(status: int, code: str, message: str) -> web.Response:
    return _json_response({"code": code, "message": message}, status=status)


def _validate_scaffold_config(
    config_path, required_env: set[str], api_key: str, secret_env: str
) -> None:
    """Prove the generated config loads before the scaffold succeeds (F12).

    Uses the real Nanobot loader when the host has it: every ``${ENV}``
    reference resolves (missing names get a placeholder just for this
    check), and the runs key must round-trip to the generated value. The
    environment is restored afterwards.
    """
    import os

    try:
        from nanobot.config.loader import load_config, resolve_config_env_vars
    except ImportError:
        # No loader on this host: the JSON round-trip already proves the file
        # is real JSON; schema validation happens at the bot's first start.
        json.loads(config_path.read_text(encoding="utf-8"))
        return
    added: dict[str, str] = {}
    try:
        for name in required_env:
            if not os.environ.get(name):
                placeholder = f"scaffold-placeholder-{name.lower()}"
                os.environ[name] = placeholder
                added[name] = placeholder
        os.environ[secret_env] = api_key
        added[secret_env] = api_key
        # The same two-step resolve the serve command performs.
        loaded = resolve_config_env_vars(load_config(config_path), config_path=config_path)
        if loaded.api.runs.api_key != api_key:
            raise ValueError(
                "the runs api key env reference did not resolve to the generated key"
            )
    finally:
        for name in added:
            os.environ.pop(name, None)


@dataclass(frozen=True)
class Roles:
    operator: str
    planner: str

    def identify(self, request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):]
        for role, secret in (("operator", self.operator), ("planner", self.planner)):
            if hmac.compare_digest(token, secret):
                return role
        return None


def _is_loopback_http_endpoint(endpoint: str) -> bool:
    """Exact loopback host, no userinfo, http only — startswith is not enough."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    if parts.scheme != "http" or parts.username is not None or parts.password is not None:
        return False
    return parts.hostname in ("127.0.0.1", "localhost", "::1")


async def _revoke_planner_workflow(planner: PlannerBinding, workflow_id: str) -> bool:
    """Revoke one workflow grant at the planner inbox, within protocol bounds.

    Any missing key, transport failure, or non-conforming answer reports
    False: the operator stop still cancels the workflow's tasks, it just
    cannot claim the planner grant was withdrawn.
    """
    import os
    from urllib.parse import quote as urlquote

    import httpx

    from .client import _bounded_json_payload  # pyright: ignore[reportPrivateUsage]

    inbox_key = os.environ.get(planner.inbox_key_env, "").strip()
    if not inbox_key:
        return False
    url = (
        planner.inbox_url.rstrip("/")
        + "/v1/agent-inbox/workflows/"
        + urlquote(workflow_id, safe="")
        + "/revoke"
    )
    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=False, trust_env=False
    ) as client:
        request = client.build_request(
            "POST", url, headers={"Authorization": f"Bearer {inbox_key}"}
        )
        response = await client.send(request, stream=True)
        try:
            payload = await _bounded_json_payload(  # pyright: ignore[reportPrivateUsage]
                response
            )
        finally:
            await response.aclose()
    if response.status_code != 200 or payload is None:
        return False
    return bool(payload.get("revoked", False))


def bot_public_json(bot: Bot, *, include_secret_env: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "bot_id": bot.bot_id,
        "display_name": bot.display_name,
        "role": bot.role,
        "endpoint": bot.endpoint,
        "enabled": bot.enabled,
        "instance_id": bot.instance_id,
        "capabilities": list(bot.capabilities),
        "inbox_url": bot.inbox_url,
        "inbox_principal_id": bot.inbox_principal_id,
        "inbox_bound": all((bot.inbox_url, bot.inbox_principal_id, bot.inbox_key_env)),
        "has_open_tasks": False,
    }
    if include_secret_env:
        payload["secret_env"] = bot.secret_env
    return payload


def build_app(
    store: CoordinatorStore,
    config: CoordinatorConfig,
    tokens: dict[str, str],
) -> web.Application:
    roles = Roles(operator=tokens["operator"], planner=tokens["planner"])
    app = web.Application(client_max_size=256 * 1024)
    app[_STORE_KEY] = store
    app[_CONFIG_KEY] = config

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        role = roles.identify(request)
        if role is None:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                role = _identify_bot_principal(auth[len("Bearer "):])
        request[_ROLE_KEY] = role
        if role is None:
            return api_error(401, "unauthorized", "A coordinator role bearer token is required.")
        return await handler(request)

    def _identify_bot_principal(token: str) -> str | None:
        """Per-bot coordinator credentials identify their own bot principal.

        The stored value is only an env-var name; a bot row whose env is unset
        carries no credential at all.
        """
        try:
            bots = store.list_bots()
        except Exception:
            return None
        for bot in bots:
            if not bot.coord_secret_env:
                continue
            secret = os.environ.get(bot.coord_secret_env, "")
            if secret and hmac.compare_digest(token, secret):
                return f"bot:{bot.bot_id}"
        return None

    def require_role(request: web.Request, allowed: tuple[str, ...]) -> web.Response | None:
        if request[_ROLE_KEY] not in allowed:
            return api_error(403, "forbidden", "That role cannot perform this operation.")
        return None

    def principal_for(request: web.Request) -> str:
        # Identity comes from token verification: the planner and operator
        # roles are fixed principals; each bot credential is its own principal.
        role = request[_ROLE_KEY]
        return role if role is not None else "operator"

    async def capabilities(request: web.Request) -> web.Response:
        return _json_response(
            {
                "protocol": 1,
                "service": "nanobot-teams-coordinator",
                "roles": ["operator", "planner"],
                "auto_resume": config.planner is not None,
            }
        )

    async def create_task(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return api_error(400, "invalid_json", "Body must be JSON.")
        if not isinstance(body, dict):
            return api_error(400, "invalid_json", "Body must be an object.")
        required = {"project", "bot", "input", "operation_key"}
        missing = required - set(body)
        if missing:
            return api_error(400, "invalid_field", f"Missing fields: {sorted(missing)}")
        delivery_mode = DeliveryMode(str(body.get("delivery_mode", "workbench_only")))
        if delivery_mode not in (DeliveryMode.WORKBENCH_ONLY, DeliveryMode.AUTO_RESUME):
            return api_error(400, "invalid_field", "delivery_mode must be workbench_only or auto_resume.")
        if delivery_mode is DeliveryMode.AUTO_RESUME and request[_ROLE_KEY] != "planner":
            return api_error(
                403, "operator_workbench_only",
                "Only the planner principal may request auto-resume delivery.",
            )
        deadline_raw = body.get("deadline_at")
        deadline = None
        if deadline_raw is not None:
            from datetime import datetime

            try:
                deadline = datetime.fromisoformat(str(deadline_raw))
            except ValueError:
                return api_error(400, "invalid_field", "deadline_at must be RFC 3339.")
            if deadline.tzinfo is None:
                return api_error(
                    400, "invalid_field", "deadline_at must carry a UTC timezone."
                )

        # Idempotent reconciliation wins over fresh validation: a retry of an
        # accepted task must return the original even while the planner is down.
        existing = store.lookup_task(principal_for(request), str(body["operation_key"]))
        if existing is not None:
            digest = task_digest(
                caller_principal=principal_for(request),
                project_id=str(body["project"]),
                bot_id=str(body["bot"]),
                input_text=str(body["input"]),
                deadline_at=deadline,
                delivery_mode=delivery_mode.value,
                workflow_id=str(body.get("workflow", "wf-default")),
            )
            if existing.request_digest == digest:
                return _json_response(task_public_json(existing), status=200)
            return api_error(409, "operation_key_conflict",
                             "This operation key was used with different semantics.")
        if delivery_mode is DeliveryMode.AUTO_RESUME:
            planner = config.planner
            capability = str(body.get("reply_capability", "")).strip()
            if planner is None or not capability:
                return api_error(
                    422,
                    "origin_required",
                    "auto_resume requires a planner binding and a reply capability.",
                )
            import os

            import httpx

            inbox_key = os.environ.get(planner.inbox_key_env, "").strip()
            if not inbox_key:
                return api_error(
                    503, "planner_key_missing",
                    f"{planner.inbox_key_env} is not set in the coordinator environment.",
                )
            validate_body = {
                "capability": capability,
                "workflow": str(body.get("workflow", "wf-default")),
                "operation_key": str(body["operation_key"]),
                "bot_id": str(body["bot"]),
                "instance_id": str(body.get("instance_id", "")),
            }
            try:
                async with httpx.AsyncClient(
                    timeout=10.0, follow_redirects=False, trust_env=False
                ) as client:
                    validation = await client.post(
                        planner.inbox_url.rstrip("/") + "/v1/agent-inbox/capabilities/validate",
                        headers={"Authorization": f"Bearer {inbox_key}"},
                        json=validate_body,
                    )
            except httpx.HTTPError:
                return api_error(
                    503, "planner_unavailable",
                    "The planner inbox did not answer; new remote tasks are refused.",
                )
            if validation.status_code != 200:
                detail = ""
                try:
                    detail = str(validation.json().get("code", ""))
                except ValueError:
                    pass
                return api_error(
                    422, detail or "capability_invalid",
                    "The planner rejected the reply capability.",
                )
        bot = store.get_bot(str(body["bot"]))
        if bot is None:
            return api_error(400, "bot_unavailable", "No registered bot with that id.")
        reply_capability = str(body.get("reply_capability", "")).strip()
        try:
            task, created = store.admit_task(
                caller_principal=principal_for(request),
                project_id=str(body["project"]),
                workflow_id=str(body.get("workflow", "wf-default")),
                bot_id=str(body["bot"]),
                operation_key=str(body["operation_key"]),
                input_text=str(body["input"]),
                deadline_at=deadline,
                delivery_mode=delivery_mode,
                bot=bot,
                reply_capability=reply_capability,
            )
        except IdempotencyConflictError as exc:
            return api_error(409, exc.code, exc.detail)
        except AdmissionError as exc:
            # A quarantined bot is a state conflict, not a malformed request:
            # the same body becomes admissible once the operator resolves it.
            status = 409 if exc.code == "bot_quarantined" else 422
            return api_error(status, exc.code, exc.detail)
        return _json_response(task_public_json(task), status=201 if created else 200)

    async def list_tasks(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        limit_raw = request.query.get("limit", "50")
        try:
            limit = max(1, min(int(limit_raw), PAGE_MAX))
        except ValueError:
            return api_error(400, "invalid_field", "limit must be an integer.")
        principal = principal_for(request) if request[_ROLE_KEY] == "planner" else None
        tasks = store.list_tasks(
            caller_principal=principal,
            project_id=request.query.get("project"),
            workflow_id=request.query.get("workflow"),
            limit=limit,
        )
        return _json_response({"tasks": [task_public_json(t) for t in tasks]})

    async def lookup_task(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        key = request.query.get("operation_key", "")
        if not key:
            return api_error(400, "operation_key_required", "operation_key is required.")
        task = store.lookup_task(principal_for(request), key)
        if task is None and request[_ROLE_KEY] == "operator":
            task = store.lookup_task("planner", key)
        if task is None:
            return api_error(404, "task_not_found", "No task for that key.")
        return _json_response(task_public_json(task))

    async def peer_send(request: web.Request) -> web.Response:
        """Bot-principal task dispatch along an explicit peer edge (I3-I6)."""
        role = request[_ROLE_KEY]
        if role is None or not role.startswith("bot:"):
            return api_error(
                403, "peer_forbidden", "Peer sends require a bot principal."
            )
        sender_id = role[len("bot:"):]
        try:
            body = await request.json()
        except Exception:
            return api_error(400, "invalid_json", "Body must be JSON.")
        if not isinstance(body, dict):
            return api_error(400, "invalid_json", "Body must be an object.")
        required = {"recipient", "input", "operation_key"}
        missing = required - set(body)
        if missing:
            return api_error(400, "invalid_field", f"Missing fields: {sorted(missing)}")
        input_text = str(body["input"])
        if len(input_text.encode("utf-8")) > 16 * 1024:
            return api_error(
                400, "input_too_large", "Peer input exceeds the 16 KiB protocol cap."
            )
        try:
            hop = int(body.get("hop", 0))
        except (TypeError, ValueError):
            return api_error(400, "invalid_field", "hop must be an integer.")
        conversation_root = str(body.get("conversation_root", "")).strip()             or str(uuid.uuid4())
        deadline_raw = body.get("deadline_at")
        deadline = None
        if deadline_raw is not None:
            from datetime import datetime

            try:
                deadline = datetime.fromisoformat(str(deadline_raw))
            except ValueError:
                return api_error(400, "invalid_field", "deadline_at must be RFC 3339.")
            if deadline.tzinfo is None:
                return api_error(
                    400, "invalid_field", "deadline_at must carry a UTC timezone."
                )
        sender = store.get_bot(sender_id)
        if sender is None or not sender.enabled:
            return api_error(
                403, "unknown_sender", "The sending bot is not registered or enabled."
            )
        recipient = store.get_bot(str(body["recipient"]))
        if recipient is None or not recipient.enabled:
            return api_error(400, "bot_unavailable", "No registered bot with that id.")
        project_id = str(body.get("project", "peer")).strip() or "peer"
        try:
            store.create_project(project_id, "Peer comms")
        except AdmissionError:
            pass  # already present on every send but the first
        # Idempotent reconciliation wins over fresh budget checks, exactly
        # like planner submissions.
        principal = principal_for(request)
        existing = store.lookup_task(principal, str(body["operation_key"]))
        if existing is not None:
            # A replay that omits the envelope keeps the original semantics:
            # the caller did not change what the operation key means.
            replay_root = (
                existing.conversation_root
                if not str(body.get("conversation_root", "")).strip()
                else conversation_root
            )
            digest = task_digest(
                caller_principal=principal,
                project_id=project_id,
                bot_id=recipient.bot_id,
                input_text=input_text,
                deadline_at=deadline,
                delivery_mode=DeliveryMode.WORKBENCH_ONLY.value,
                workflow_id=replay_root,
            )
            if existing.request_digest == digest:
                return _json_response(task_public_json(existing), status=200)
            return api_error(
                409, "operation_key_conflict",
                "This operation key was used with different semantics.",
            )
        try:
            task, created = store.admit_task(
                caller_principal=principal,
                project_id=project_id,
                workflow_id=conversation_root,
                bot_id=recipient.bot_id,
                operation_key=str(body["operation_key"]),
                input_text=input_text,
                deadline_at=deadline,
                delivery_mode=DeliveryMode.WORKBENCH_ONLY,
                bot=recipient,
                kind="peer",
                conversation_root=conversation_root,
                hop=hop,
                sender_bot_id=sender_id,
            )
        except IdempotencyConflictError as exc:
            return api_error(409, exc.code, exc.detail)
        except AdmissionError as exc:
            status = {
                "unauthorized_edge": 403,
                "conversation_cycle": 403,
                "hop_limit_exceeded": 422,
                "pair_rate_limited": 429,
                "bot_quarantined": 409,
            }.get(exc.code, 422)
            store.record_peer_rejection(
                sender_bot_id=sender_id,
                recipient_bot_id=recipient.bot_id,
                code=exc.code,
                conversation_root=conversation_root,
                hop=hop,
            )
            return api_error(status, exc.code, exc.detail)
        return _json_response(task_public_json(task), status=201 if created else 200)

    async def upsert_peer_edge(request: web.Request) -> web.Response:
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return api_error(400, "invalid_json", "Body must be JSON.")
        from_bot = str(body.get("from_bot_id", "")).strip()
        to_bot = str(body.get("to_bot_id", "")).strip()
        if not from_bot or not to_bot:
            return api_error(
                400, "invalid_field", "from_bot_id and to_bot_id are required."
            )
        try:
            store.upsert_peer_edge(from_bot, to_bot)
        except AdmissionError as exc:
            return api_error(409, exc.code, exc.detail)
        return _json_response(
            {"edge": {"from_bot_id": from_bot, "to_bot_id": to_bot}}, status=201
        )

    async def list_peer_edges(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        return _json_response(
            {"edges": [
                {"from_bot_id": f, "to_bot_id": t}
                for f, t in store.list_peer_edges()
            ]}
        )

    async def delete_peer_edge(request: web.Request) -> web.Response:
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        removed = store.delete_peer_edge(
            request.match_info["from_bot_id"], request.match_info["to_bot_id"]
        )
        if not removed:
            return api_error(404, "edge_not_found", "No such peer edge.")
        return _json_response({"removed": True})

    async def get_task(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        task = store.get_task(request.match_info["task_id"])
        if task is None:
            return api_error(404, "task_not_found", "No task with that id.")
        if request[_ROLE_KEY] == "planner" and task.caller_principal != "planner":
            return api_error(403, "forbidden", "Not your task.")
        return _json_response(task_public_json(task))

    async def get_task_result(request: web.Request) -> web.Response:
        """Public result read path (F08): no bot credentials reach the caller.

        Same ownership rules as the detail route. A terminal run whose body is
        still being fetched answers 409 result_fetch_pending instead of a
        silent empty success.
        """
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        task = store.get_task(request.match_info["task_id"])
        if task is None:
            return api_error(404, "task_not_found", "No task with that id.")
        if request[_ROLE_KEY] == "planner" and task.caller_principal != "planner":
            return api_error(403, "forbidden", "Not your task.")
        if not task.result_available or not task.result_fetched:
            return api_error(
                409,
                "result_fetch_pending",
                "The terminal result body is not persisted yet; retry shortly.",
            )
        return _json_response(
            {
                "task_id": task.task_id,
                "run_state": task.run_state,
                "outcome_code": task.outcome_code,
                "result_text": task.result_text,
                "result_truncated": task.result_truncated,
            }
        )

    async def cancel_task(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        task = store.get_task(request.match_info["task_id"])
        if task is None:
            return api_error(404, "task_not_found", "No task with that id.")
        if request[_ROLE_KEY] == "planner" and task.caller_principal != "planner":
            return api_error(403, "forbidden", "Not your task.")
        updated = store.request_cancel(task.task_id)
        return _json_response(task_public_json(updated), status=202)

    async def snapshot(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        quarantines = {
            k: {"reason": v.reason, "created_at": v.created_at.isoformat()}
            for k, v in store.active_quarantines().items()
        }
        bots = []
        for bot in store.list_bots():
            payload = bot_public_json(bot, include_secret_env=False)
            payload["has_open_tasks"] = store.bot_has_open_tasks(bot.bot_id)
            bots.append(payload)
        return _json_response(
            {
                "projects": [
                    {
                        "project_id": p.project_id,
                        "display_name": p.display_name,
                        "created_at": p.created_at.isoformat(),
                    }
                    for p in store.list_projects()
                ],
                "bots": bots,
                "tasks": [
                    task_public_json(t)
                    for t in store.list_tasks(
                        caller_principal=(
                            "planner" if request[_ROLE_KEY] == "planner" else None
                        ),
                        limit=PAGE_MAX,
                    )
                ],
                "quarantines": quarantines,
                "protocol": 1,
            }
        )

    async def events(request: web.Request) -> web.StreamResponse:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        after_raw = request.query.get("after", "0")
        try:
            after = int(after_raw)
        except ValueError:
            return api_error(400, "invalid_cursor", "after must be an integer seq.")
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await resp.prepare(request)
        loop = asyncio.get_event_loop()
        heartbeat_at = loop.time() + SSE_HEARTBEAT_SECONDS
        try:
            while True:
                for event in store.events_after(after, limit=PAGE_MAX):
                    payload = _dumps(
                        {
                            "seq": event.seq,
                            "at": event.at.isoformat(),
                            "type": event.type,
                            "task_id": event.task_id,
                            "data": event.data,
                        }
                    )
                    await resp.write(
                        f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n".encode()
                    )
                    after = event.seq
                now = loop.time()
                if now >= heartbeat_at:
                    await resp.write(b": heartbeat\n\n")
                    heartbeat_at = now + SSE_HEARTBEAT_SECONDS
                await asyncio.sleep(0.4)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await resp.write_eof()
        return resp

    async def create_project(request: web.Request) -> web.Response:
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        body = await request.json()
        project_id = str(body.get("project_id", "")).strip()
        if not project_id or len(project_id) > 64:
            return api_error(400, "invalid_field", "project_id must be 1-64 characters.")
        try:
            store.create_project(project_id, str(body.get("display_name", project_id)))
        except AdmissionError as exc:
            return api_error(409, exc.code, exc.detail)
        return _json_response({"project_id": project_id}, status=201)

    async def list_projects(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        return _json_response(
            {"projects": [p.project_id for p in store.list_projects()]}
        )

    async def upsert_bot(request: web.Request) -> web.Response:
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        body = await request.json()
        bot_id = str(body.get("bot_id", "")).strip()
        endpoint = str(body.get("endpoint", "")).strip()
        secret_env = str(body.get("secret_env", "")).strip()
        if not bot_id or not _is_loopback_http_endpoint(endpoint):
            return api_error(
                400, "invalid_field", "v1 accepts loopback bot endpoints only."
            )
        if not secret_env:
            return api_error(400, "invalid_field", "secret_env naming the service env is required.")
        if "secret" in json.dumps(body).lower() and str(body.get("api_key", "")):
            return api_error(400, "secret_rejected", "Raw keys are never accepted here.")
        if store.get_bot(bot_id) is not None and store.bot_has_open_tasks(bot_id):
            return api_error(
                409, "bot_has_open_tasks", "Settle or cancel open tasks before editing this bot."
            )
        inbox_url = str(body.get("inbox_url", "")).strip() or None
        inbox_principal_id = str(body.get("inbox_principal_id", "")).strip() or None
        inbox_key_env = str(body.get("inbox_key_env", "")).strip() or None
        coord_secret_env = str(body.get("coord_secret_env", "")).strip() or None
        if inbox_url is not None and not _is_loopback_http_endpoint(inbox_url):
            return api_error(
                400, "invalid_field", "inbox_url must be a loopback http endpoint."
            )
        # I2: the receive binding is all-or-nothing.
        inbox_fields = (inbox_url, inbox_principal_id, inbox_key_env)
        if any(inbox_fields) and not all(inbox_fields):
            return api_error(
                400, "invalid_field",
                "A receive binding requires inbox_url, inbox_principal_id, and "
                "inbox_key_env together.",
            )
        bot = Bot(
            bot_id=bot_id,
            display_name=str(body.get("display_name", bot_id)),
            role=str(body.get("role", "")),
            endpoint=endpoint,
            secret_env=secret_env,
            enabled=bool(body.get("enabled", True)),
            capabilities=tuple(str(c) for c in body.get("capabilities", [])),
            inbox_url=inbox_url,
            inbox_principal_id=inbox_principal_id,
            inbox_key_env=inbox_key_env,
            coord_secret_env=coord_secret_env,
        )
        try:
            store.upsert_bot(bot)
        except AdmissionError as exc:
            return api_error(409, exc.code, exc.detail)
        return _json_response(bot_public_json(store.get_bot(bot_id)), status=201)

    async def list_bots_route(request: web.Request) -> web.Response:
        denied = require_role(request, ("planner", "operator"))
        if denied is not None:
            return denied
        return _json_response(
            {
                "bots": [
                    bot_public_json(b, include_secret_env=False)
                    for b in store.list_bots()
                ]
            }
        )

    app.middlewares.append(auth_middleware)
    async def scaffold_bot(request: web.Request) -> web.Response:
        """Generate a fresh managed-bot home; never start a process.

        F12/F13: the generated ``config.json`` is real JSON the Nanobot loader
        accepts (validated here before the 201), credentials live only in
        ``${ENV}`` references, and the raw service key is written to an
        operator-local ``service-key.env`` — it never reaches the browser
        response or the config file.
        """
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        body = await request.json()
        bot_id = str(body.get("bot_id", "")).strip()
        if not bot_id or len(bot_id) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", bot_id):
            return api_error(400, "invalid_field", "bot_id must be a bounded slug.")
        homes_root = config.bot_homes_root
        if homes_root is None:
            return api_error(422, "homes_root_missing",
                             "The coordinator config has no bot_homes_root.")
        preset = config.scaffold_model_preset.strip()
        if not preset:
            return api_error(
                422, "preset_required",
                "Set scaffold_model_preset in the coordinator config before "
                "scaffolding managed bots.",
            )
        import secrets as pysecrets
        import shutil as pyshutil
        import socket as pysocket

        from ..lifecycle import ConfigError, managed_bot_config

        home = (homes_root / bot_id).resolve()
        if not home.is_relative_to(homes_root.resolve()):
            return api_error(400, "path_escape", "Resolved home escapes bot_homes_root.")
        if home.exists():
            return api_error(409, "home_exists", "That home directory already exists.")
        with pysocket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        api_key = pysecrets.token_urlsafe(24)
        secret_env = f"TEAMS_BOT_{re.sub(r'[^A-Z0-9]', '_', bot_id.upper())}_KEY"
        coord_token = pysecrets.token_urlsafe(24)
        coord_env = f"TEAMS_BOT_{re.sub(r'[^A-Z0-9]', '_', bot_id.upper())}_COORD_TOKEN"
        coordinator_url = f"http://{config.host}:{config.port}"
        try:
            bot_config, required_env = managed_bot_config(
                bot_id=bot_id, home=home, port=port, preset=preset,
                runs_api_key_env=secret_env,
                coordinator_url=coordinator_url,
                coord_token_env=coord_env,
                user_config=config.scaffold_user_config,
            )
        except ConfigError as exc:
            return api_error(422, "invalid_preset", str(exc))
        home.mkdir(parents=True)
        (home / "workspace").mkdir()
        try:
            (home / "config.json").write_text(
                json.dumps(bot_config, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            key_file = home / "service-key.env"
            key_file.write_text(
                "# Managed bot service keys generated by the teams coordinator.\n"
                "# Set this value (source the file or copy the line) in the\n"
                "# environment of BOTH the bot process and the coordinator\n"
                "# process, then delete this file once both are configured.\n"
                f"{secret_env}={api_key}\n"
                f"{coord_env}={coord_token}\n",
                encoding="utf-8",
            )
            _validate_scaffold_config(home / "config.json", required_env, api_key, secret_env)
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            pyshutil.rmtree(home, ignore_errors=True)
            return api_error(
                422, "scaffold_validation_failed",
                f"The generated config did not validate; no home was kept: {exc}",
            )
        store.upsert_bot(
            Bot(
                bot_id=bot_id,
                display_name=str(body.get("display_name", bot_id)),
                role=str(body.get("role", "")),
                endpoint=f"http://127.0.0.1:{port}",
                secret_env=secret_env,
                coord_secret_env=coord_env,
                inbox_url=f"http://127.0.0.1:{port}",
                inbox_principal_id="teams-coordinator",
                inbox_key_env=secret_env,
            )
        )
        start_command = f'nanobot serve --config "{home / "config.json"}"'
        return _json_response(
            {
                "bot_id": bot_id,
                "home": str(home),
                "endpoint": f"http://127.0.0.1:{port}",
                "secret_env": secret_env,
                "key_file": str(home / "service-key.env"),
                "required_env": sorted(required_env),
                "start_command": start_command,
                "started": False,
            },
            status=201,
        )

    async def caller_quota_route(request: web.Request) -> web.Response:
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        quota = store.caller_quota(request.match_info["caller"])
        if quota is None:
            return api_error(404, "quota_not_found", "No imported usage for that caller.")
        return _json_response(quota)

    async def confirm_caller_quota(request: web.Request) -> web.Response:
        """Operator confirmation that unlocks admissions for a migrated caller."""
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except ValueError:
            return api_error(400, "invalid_json", "A JSON body is required.")
        actor = request[_ROLE_KEY]
        try:
            confirmed = store.confirm_caller_quota(
                request.match_info["caller"],
                delegations_limit=int(body.get("delegations", 0)),
                result_bytes_limit=int(body.get("result_bytes", 0)),
                actor=actor,
            )
        except AdmissionError as exc:
            return api_error(422, exc.code, exc.detail)
        if not confirmed:
            return api_error(404, "quota_not_found", "No imported usage for that caller.")
        return _json_response({"confirmed": True})

    async def stop_workflow(request: web.Request) -> web.Response:
        """Stop a whole workflow: revoke its grant, then cancel its open tasks."""
        denied = require_role(request, ("operator", "planner"))
        if denied is not None:
            return denied
        workflow_id = request.match_info["workflow_id"].strip()
        if not workflow_id:
            return api_error(400, "invalid_field", "workflow_id is required.")
        # Authorize the workflow before any grant revocation or task write. A
        # planner owns only its own tasks; an operator owns the whole workflow.
        planner_scope = "planner" if request[_ROLE_KEY] == "planner" else None
        tasks = store.list_tasks(
            workflow_id=workflow_id,
            caller_principal=planner_scope,
            limit=WORKFLOW_STOP_TASK_LIMIT,
        )
        if planner_scope is not None and not tasks:
            return api_error(403, "forbidden", "Not your workflow.")
        revoked = False
        if config.planner is not None:
            try:
                revoked = await _revoke_planner_workflow(config.planner, workflow_id)
            except Exception:  # noqa: BLE001 - the cancel pass must still run
                revoked = False
        cancelled: list[str] = []
        unconfirmed: list[str] = []
        for task in tasks:
            if task.cancel_state is not CancelState.NONE:
                continue
            if task.dispatch_state not in (
                DispatchState.ACCEPTED,
                DispatchState.PENDING,
            ):
                continue
            if (
                task.run_state in TERMINAL_RUN_STATES
                and task.stop_confirmed
                and not task.effects_uncertain
            ):
                # Already finished with a confirmed stop: no cancel to send.
                continue
            updated = store.request_cancel(task.task_id)
            if updated is None:
                continue
            if updated.cancel_state is CancelState.CONFIRMED:
                cancelled.append(updated.task_id)
            else:
                # Requested or unconfirmed: the stop is not proven yet, so the
                # operator must not read it as cancelled.
                unconfirmed.append(updated.task_id)
        return _json_response(
            {
                "workflow_id": workflow_id,
                "revoked": revoked,
                "cancelled": cancelled,
                "unconfirmed": unconfirmed,
                "total": len(tasks),
            }
        )

    async def resolve_quarantine(request: web.Request) -> web.Response:
        """Operator resolution for one quarantine key (instance or endpoint)."""
        denied = require_role(request, ("operator",))
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except ValueError:
            return api_error(400, "invalid_json", "A JSON body is required.")
        if not isinstance(body, dict):
            return api_error(400, "invalid_json", "Body must be an object.")
        actor = body.get("actor")
        note = body.get("note")
        if not isinstance(actor, str) or not actor.strip():
            return api_error(400, "invalid_field", "actor must be a non-empty string.")
        if not isinstance(note, str) or not note.strip():
            return api_error(400, "invalid_field", "note must be a non-empty string.")
        from urllib.parse import unquote

        key = unquote(request.match_info["key"])
        store.resolve_quarantine(key, actor.strip(), note.strip())
        return _json_response({"key": key, "resolved": True})

    app.router.add_get("/v1/teams/capabilities", capabilities)
    app.router.add_post("/v1/teams/tasks", create_task)
    app.router.add_post("/v1/teams/peer/send", peer_send)
    app.router.add_post("/v1/teams/peer-edges", upsert_peer_edge)
    app.router.add_get("/v1/teams/peer-edges", list_peer_edges)
    app.router.add_delete(
        "/v1/teams/peer-edges/{from_bot_id}/{to_bot_id}", delete_peer_edge
    )
    app.router.add_get("/v1/teams/tasks", list_tasks)
    app.router.add_get("/v1/teams/tasks/lookup", lookup_task)
    app.router.add_get("/v1/teams/tasks/{task_id}", get_task)
    app.router.add_get("/v1/teams/tasks/{task_id}/result", get_task_result)
    app.router.add_post("/v1/teams/tasks/{task_id}/cancel", cancel_task)
    app.router.add_get("/v1/teams/snapshot", snapshot)
    app.router.add_get("/v1/teams/events", events)
    app.router.add_post("/v1/teams/projects", create_project)
    app.router.add_get("/v1/teams/projects", list_projects)
    app.router.add_post("/v1/teams/bots/scaffold", scaffold_bot)
    app.router.add_post("/v1/teams/bots", upsert_bot)
    app.router.add_get("/v1/teams/bots", list_bots_route)
    app.router.add_get("/v1/teams/quota/{caller}", caller_quota_route)
    app.router.add_post("/v1/teams/quota/{caller}/confirm", confirm_caller_quota)
    app.router.add_post("/v1/teams/workflows/{workflow_id}/stop", stop_workflow)
    app.router.add_post("/v1/teams/quarantine/{key}/resolve", resolve_quarantine)
    return app


__all__ = ["Roles", "build_app", "bot_public_json", "task_public_json"]
