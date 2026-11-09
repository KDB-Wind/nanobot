from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from nanobot_teams.config import load_teams_config
from nanobot_teams.delegation import Delegator
from nanobot_teams.ledger import Ledger
from nanobot_teams.transport import HttpTransport, OutcomeKind

ENDPOINT = "http://127.0.0.1:8901/v1/chat/completions"
SECRET = "sekret-value"

REGISTRY: dict[str, Any] = {
    "schema_version": 2,
    "team_id": "ledger-team",
    "thread_key": "main",
    "bots": [
        {"id": "planner", "display_name": "Planner", "role": "planning"},
        {
            "id": "research",
            "display_name": "Research",
            "role": "research",
            "endpoint": ENDPOINT,
            "api_key_env": "TEAMS_RESEARCH_API_KEY",
            "allowed_callers": ["planner"],
        },
    ],
}


def build(tmp_path: Path, handler, ledger: Ledger | None):
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(REGISTRY), encoding="utf-8")
    config = load_teams_config(path)
    transport = HttpTransport(config.limits, transport=httpx.MockTransport(handler))
    keys = {"research": SECRET}
    delegator = Delegator(config, transport, keys, "planner", ledger=ledger)
    return delegator, transport


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


async def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=completion("the visible answer"))


async def test_delegation_recorded_with_bytes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    delegator, transport = build(tmp_path, _ok, ledger)
    try:
        result = await delegator.delegate_task("research", "task text nonce-alpha")
    finally:
        await transport.aclose()

    rows = ledger.list_tasks()
    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == result.task_id
    assert row.caller == "planner"
    assert row.target == "research"
    assert row.status == "completed"
    assert row.hop == 1
    assert row.http_sent is True
    assert row.request_bytes > 0
    assert row.response_bytes > 0
    assert row.result_bytes == len("the visible answer".encode())
    assert row.duration_ms is not None
    ledger.close()


async def test_ledger_stores_no_task_or_answer_text(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(db_path)
    delegator, transport = build(tmp_path, _ok, ledger)
    try:
        await delegator.delegate_task("research", "task text nonce-alpha")
    finally:
        await transport.aclose()
    ledger.close()

    blob = db_path.read_bytes()
    assert b"nonce-alpha" not in blob
    assert b"the visible answer" not in blob
    assert SECRET.encode() not in blob


async def test_refusals_are_not_recorded(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    delegator, transport = build(tmp_path, _ok, ledger)
    try:
        await delegator.delegate_task("ghost", "x")
        await delegator.delegate_task("research", "")
    finally:
        await transport.aclose()

    assert ledger.list_tasks() == []
    ledger.close()


async def test_filters_and_get_task(tmp_path: Path) -> None:
    calls = {"count": 0}

    async def alternating(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json=completion("fine"))
        return httpx.Response(500, text="boom")

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    delegator, transport = build(tmp_path, alternating, ledger)
    try:
        good = await delegator.delegate_task("research", "one")
        unknown = await delegator.delegate_task("research", "two")
    finally:
        await transport.aclose()

    assert good.status is OutcomeKind.completed
    assert unknown.status is OutcomeKind.outcome_unknown
    statuses = {row.status for row in ledger.list_tasks()}
    assert statuses == {"completed", "outcome_unknown"}
    assert [row.status for row in ledger.list_tasks(status="completed")] == ["completed"]
    fetched = ledger.get_task(good.task_id)
    assert fetched is not None and fetched.status == "completed"
    assert ledger.get_task("no-such-id") is None
    ledger.close()


async def test_ledger_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    first = Ledger(db_path)
    delegator, transport = build(tmp_path, _ok, first)
    try:
        result = await delegator.delegate_task("research", "recorded once")
    finally:
        await transport.aclose()
    first.close()

    second = Ledger(db_path)
    rows = second.list_tasks()
    assert [row.task_id for row in rows] == [result.task_id]
    second.close()


def test_disabled_ledger_is_a_noop() -> None:
    ledger = Ledger(None)
    assert ledger.enabled is False
    assert ledger.list_tasks() == []
    assert ledger.get_task("x") is None
    ledger.close()


async def test_json_row_carries_observation_note(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    delegator, transport = build(tmp_path, _ok, ledger)
    try:
        result = await delegator.delegate_task("research", "note check")
    finally:
        await transport.aclose()

    row = ledger.get_task(result.task_id)
    assert row is not None
    payload = json.loads(row.to_json())
    assert payload["observation"] == "local view; status is not proof of remote success"
    ledger.close()

