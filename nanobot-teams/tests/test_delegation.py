from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from nanobot_teams.config import Limits, TeamConfig, load_teams_config
from nanobot_teams.delegation import Delegator, session_id_for
from nanobot_teams.transport import HttpTransport, OutcomeKind

ENDPOINT = "http://127.0.0.1:8901/v1/chat/completions"
SECRET = "sekret-value"
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def config_data(thread_key: str = "main", allowed: list[str] | None = None) -> dict[str, Any]:
    targets = allowed or ["research"]
    bots: list[dict[str, Any]] = [{"id": "planner", "display_name": "Planner", "role": "planning"}]
    for index, bot_id in enumerate(targets):
        bots.append(
            {
                "id": bot_id,
                "display_name": bot_id.title(),
                "role": "research",
                "endpoint": ENDPOINT,
                "api_key_env": f"TEAMS_{bot_id.upper()}_API_KEY",
            }
        )
    return {
        "schema_version": 1,
        "team_id": "personal-research",
        "caller_bot_id": "planner",
        "thread_key": thread_key,
        "allowed_targets": targets,
        "bots": bots,
    }


def load(tmp_path: Path, data: dict[str, Any]) -> TeamConfig:
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_teams_config(path)


def build(
    tmp_path: Path,
    handler: Handler,
    *,
    data: dict[str, Any] | None = None,
) -> tuple[Delegator, HttpTransport]:
    config = load(tmp_path, data or config_data())
    transport = HttpTransport(config.limits, transport=httpx.MockTransport(handler))
    keys = {bot.id: SECRET for bot in config.targets_for("planner")}
    return Delegator(config, transport, keys, "planner"), transport


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


async def test_completed_delegation(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("the answer"))

    delegator, transport = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("research", "find x")
    finally:
        await transport.aclose()

    payload = json.loads(result.to_json())
    assert payload["status"] == "completed"
    assert payload["semantic_status"] == "unverified"
    assert payload["content"] == "the answer"
    assert payload["truncated"] is False
    assert payload["error_code"] is None
    assert payload["detail"] is None
    assert payload["bot_id"] == "research"
    assert len(payload["task_id"]) == 36

    body = json.loads(await requests[0].aread())
    assert "model" not in body
    assert body["messages"] == [{"role": "user", "content": "find x"}]
    assert body["stream"] is False
    assert body["session_id"].startswith("teams-v1-")

    assert SECRET not in result.to_json()


async def test_task_ids_change_and_session_is_stable(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=completion("ok"))

    delegator, transport = build(tmp_path, handler)
    try:
        first = await delegator.delegate_task("research", "one")
        second = await delegator.delegate_task("research", "two")
    finally:
        await transport.aclose()

    assert first.task_id != second.task_id
    assert bodies[0]["session_id"] == bodies[1]["session_id"]
    assert bodies[0]["session_id"].startswith("teams-v1-")


async def test_busy_second_call(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json=completion("done"))

    delegator, transport = build(tmp_path, handler)
    try:
        first = asyncio.create_task(delegator.delegate_task("research", "one"))
        await asyncio.wait_for(started.wait(), 5)
        second = await delegator.delegate_task("research", "two")
        release.set()
        completed = await asyncio.wait_for(first, 5)
    finally:
        await transport.aclose()

    assert second.status is OutcomeKind.failed
    assert second.error_code == "busy"
    assert completed.status is OutcomeKind.completed
    assert len(requests) == 1


async def test_blocked_after_outcome_unknown(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, text="boom")

    delegator, transport = build(tmp_path, handler)
    try:
        first = await delegator.delegate_task("research", "one")
        second = await delegator.delegate_task("research", "two")
    finally:
        await transport.aclose()

    assert first.status is OutcomeKind.outcome_unknown
    assert second.status is OutcomeKind.failed
    assert second.error_code == "blocked"
    assert len(requests) == 1


async def test_blocked_after_target_timeout(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504)

    delegator, transport = build(tmp_path, handler)
    try:
        first = await delegator.delegate_task("research", "one")
        second = await delegator.delegate_task("research", "two")
    finally:
        await transport.aclose()

    assert first.status is OutcomeKind.timed_out
    assert second.error_code == "blocked"


async def test_invalid_target_makes_no_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("ok"))

    delegator, transport = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("ghost", "one")
    finally:
        await transport.aclose()

    assert result.status is OutcomeKind.failed
    assert result.error_code == "invalid_target"
    assert requests == []


async def test_defined_but_unauthorized_bot_is_refused(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("ok"))

    data = config_data()
    data["bots"].append({"id": "analyst", "display_name": "Analyst", "role": "analysis"})
    delegator, transport = build(tmp_path, handler, data=data)
    try:
        result = await delegator.delegate_task("analyst", "one")
    finally:
        await transport.aclose()

    assert result.error_code == "invalid_target"
    assert requests == []


async def test_task_too_large(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("ok"))

    data = config_data()
    data["limits"] = {"task_max_bytes": 16}
    delegator, transport = build(tmp_path, handler, data=data)
    try:
        result = await delegator.delegate_task("research", "a" * 17)
    finally:
        await transport.aclose()

    assert result.error_code == "task_invalid"
    assert requests == []


async def test_empty_task_is_refused(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    delegator, transport = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("research", "")
    finally:
        await transport.aclose()

    assert result.error_code == "task_invalid"


async def test_list_bots_hides_target_details(tmp_path: Path) -> None:
    config = load(tmp_path, config_data())
    transport = HttpTransport(Limits(), transport=httpx.MockTransport(_unreachable))
    delegator = Delegator(config, transport, {}, "planner")

    try:
        listed = delegator.list_bots()
    finally:
        await transport.aclose()

    assert listed == [
        {"id": "research", "display_name": "Research", "role": "research", "capabilities": []}
    ]
    assert "endpoint" not in json.dumps(listed)


def test_session_id_is_deterministic(tmp_path: Path) -> None:
    config = load(tmp_path, config_data())
    again = load(tmp_path, config_data())

    assert session_id_for(config, "planner", "research") == session_id_for(again, "planner", "research")
    assert session_id_for(config, "planner", "research").startswith("teams-v1-")
    assert len(session_id_for(config, "planner", "research")) == 41


def test_session_id_differs_per_thread_and_target(tmp_path: Path) -> None:
    main = load(tmp_path, config_data())
    other_thread = load(tmp_path, config_data(thread_key="other"))
    two_targets = load(tmp_path, config_data(allowed=["research", "analyst"]))

    assert session_id_for(main, "planner", "research") != session_id_for(
        other_thread, "planner", "research"
    )
    assert session_id_for(two_targets, "planner", "research") != session_id_for(two_targets, "planner", "analyst")


async def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request expected")
