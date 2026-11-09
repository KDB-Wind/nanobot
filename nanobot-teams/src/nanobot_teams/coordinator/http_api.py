"""Coordinator HTTP API (v1): role tokens, task admission, catalog, snapshot, SSE."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .config import CoordinatorConfig
from .models import Bot, DeliveryMode
from .store import (
    AdmissionError,
    CoordinatorStore,
    IdempotencyConflictError,
)
from .worker import task_public_json

PAGE_MAX = 100
SSE_HEARTBEAT_SECONDS = 15.0
_STORE_KEY = web.AppKey[CoordinatorStore]("coordinator_store")
_CONFIG_KEY = web.AppKey[CoordinatorConfig]("coordinator_config")
_ROLE_KEY = web.RequestKey[str]("coordinator_role")


def _json_response(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def api_error(status: int, code: str, message: str) -> web.Response:
    return _json_response({"code": code, "message": message}, status=status)


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


def bot_public_json(bot: Bot, *, include_secret_env: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "bot_id": bot.bot_id,
        "display_name": bot.display_name,
        "role": bot.role,
        "endpoint": bot.endpoint,
        "enabled": bot.enabled,
        "instance_id": bot.instance_id,
        "capabilities": list(bot.capabilities),
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
        request[_ROLE_KEY] = role
        if role is None:
            return api_error(401, "unauthorized", "A coordinator role bearer token is required.")
        return await handler(request)

    def require_role(request: web.Request, allowed: tuple[str, ...]) -> web.Response | None:
        if request[_ROLE_KEY] not in allowed:
            return api_error(403, "forbidden", "That role cannot perform this operation.")
        return None

    def principal_for(request: web.Request) -> str:
        # The planner role is a single fixed principal; the operator acts as itself.
        role = request[_ROLE_KEY]
        return "planner" if role == "planner" else "operator"

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
                async with httpx.AsyncClient(timeout=10.0) as client:
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
        deadline_raw = body.get("deadline_at")
        deadline = None
        if deadline_raw is not None:
            from datetime import datetime

            try:
                deadline = datetime.fromisoformat(str(deadline_raw))
            except ValueError:
                return api_error(400, "invalid_field", "deadline_at must be RFC 3339.")
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
            return api_error(422, exc.code, exc.detail)
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
                "tasks": [task_public_json(t) for t in store.list_tasks(limit=PAGE_MAX)],
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
        if not bot_id or not endpoint.startswith(("http://127.0.0.1", "http://localhost")):
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
        bot = Bot(
            bot_id=bot_id,
            display_name=str(body.get("display_name", bot_id)),
            role=str(body.get("role", "")),
            endpoint=endpoint,
            secret_env=secret_env,
            enabled=bool(body.get("enabled", True)),
            capabilities=tuple(str(c) for c in body.get("capabilities", [])),
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
        """Generate a fresh managed-bot home; never start a process."""
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
        import secrets as pysecrets
        import socket as pysocket

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
        home.mkdir(parents=True)
        (home / "workspace").mkdir()
        (home / "nanobot.toml").write_text(
            f'''# Managed bot home generated by nanobot-teams; start with:
"
            f"#   nanobot serve --config \"{home / 'nanobot.toml'}\"
"
            f"api.host = \"127.0.0.1\"
"
            f"api.port = {port}
"
            f"[api.runs]
"
            f"enabled = true
"
            f"api_key = \"{api_key}\"
"
            f"principal_id = \"teams-coordinator\"
"
            f"execution_enabled = true
"
            f"exclusive_execution = true
"
            f"''',
            encoding="utf-8",
        )
        store.upsert_bot(
            Bot(
                bot_id=bot_id,
                display_name=str(body.get("display_name", bot_id)),
                role=str(body.get("role", "")),
                endpoint=f"http://127.0.0.1:{port}",
                secret_env=secret_env,
            )
        )
        return _json_response(
            {
                "bot_id": bot_id,
                "home": str(home),
                "endpoint": f"http://127.0.0.1:{port}",
                "secret_env": secret_env,
                "api_key": api_key,
                "start_command": f"nanobot serve --config {home / 'nanobot.toml'}",
                "started": False,
            },
            status=201,
        )

    app.router.add_get("/v1/teams/capabilities", capabilities)
    app.router.add_post("/v1/teams/tasks", create_task)
    app.router.add_get("/v1/teams/tasks", list_tasks)
    app.router.add_get("/v1/teams/tasks/lookup", lookup_task)
    app.router.add_get("/v1/teams/tasks/{task_id}", get_task)
    app.router.add_post("/v1/teams/tasks/{task_id}/cancel", cancel_task)
    app.router.add_get("/v1/teams/snapshot", snapshot)
    app.router.add_get("/v1/teams/events", events)
    app.router.add_post("/v1/teams/projects", create_project)
    app.router.add_get("/v1/teams/projects", list_projects)
    app.router.add_post("/v1/teams/bots/scaffold", scaffold_bot)
    app.router.add_post("/v1/teams/bots", upsert_bot)
    app.router.add_get("/v1/teams/bots", list_bots_route)
    return app


__all__ = ["Roles", "build_app", "bot_public_json", "task_public_json"]
