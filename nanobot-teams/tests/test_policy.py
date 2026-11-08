from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from nanobot_teams.config import ConfigError, load_teams_config
from nanobot_teams.delegation import Delegator
from nanobot_teams.ledger import Ledger
from nanobot_teams.reporting import render_audit, render_budget, render_graph
from nanobot_teams.transport import HttpTransport, OutcomeKind

ENDPOINT = "http://127.0.0.1:8901/v1/chat/completions"
SECRET = "sekret"


def registry_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 2,
        "team_id": "policy",
        "thread_key": "main",
        "bots": [
            {"id": "planner", "display_name": "Planner", "role": "planning"},
            {
                "id": "research",
                "display_name": "Research",
                "role": "research",
                "capabilities": ["search"],
                "endpoint": ENDPOINT,
                "api_key_env": "TEAMS_RESEARCH_API_KEY",
                "allowed_callers": ["planner"],
            },
        ],
    }
    data.update(overrides)
    return data


def load(tmp_path: Path, data: dict[str, Any]):
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_teams_config(path)


def test_budgets_parse(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        registry_data(
            budgets={"planner": {"delegations": 5, "result_bytes": 4096, "chain_seconds": 60.0}}
        ),
    )
    budget = config.budgets["planner"]
    assert budget.delegations == 5
    assert budget.result_bytes == 4096
    assert budget.chain_seconds == 60.0
    assert "research" not in config.budgets


def test_budgets_unknown_bot_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown bot"):
        load(tmp_path, registry_data(budgets={"ghost": {"delegations": 5}}))


def test_budgets_bad_value_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be positive"):
        load(tmp_path, registry_data(budgets={"planner": {"delegations": 0}}))
    with pytest.raises(ConfigError, match="must be an integer"):
        load(tmp_path, registry_data(budgets={"planner": {"delegations": 1.5}}))
    with pytest.raises(ConfigError, match="unknown key"):
        load(tmp_path, registry_data(budgets={"planner": {"dollars": 5}}))


async def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "abcdefgh"}}]}
    )


def build(tmp_path: Path, budgets: dict | None):
    data = registry_data()
    if budgets is not None:
        data["budgets"] = budgets
    config = load(tmp_path, data)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    transport = HttpTransport(config.limits, transport=httpx.MockTransport(_ok))
    delegator = Delegator(config, transport, {"research": SECRET}, "planner", ledger=ledger)
    return delegator, transport, ledger


async def test_delegation_budget_rejects_after_count(tmp_path: Path) -> None:
    delegator, transport, ledger = build(tmp_path, {"planner": {"delegations": 2}})
    try:
        first = await delegator.delegate_task("research", "one")
        second = await delegator.delegate_task("research", "two")
        third = await delegator.delegate_task("research", "three")
    finally:
        await transport.aclose()
        ledger.close()

    assert first.status is OutcomeKind.completed
    assert second.status is OutcomeKind.completed
    assert third.status is OutcomeKind.failed
    assert third.error_code == "budget_exceeded"
    assert "Delegation budget spent: 2 of 2" in (third.detail or "")


async def test_result_byte_budget_rejects(tmp_path: Path) -> None:
    delegator, transport, ledger = build(tmp_path, {"planner": {"result_bytes": 8}})
    try:
        first = await delegator.delegate_task("research", "one")
        second = await delegator.delegate_task("research", "two")
    finally:
        await transport.aclose()
        ledger.close()

    assert first.status is OutcomeKind.completed
    assert second.error_code == "budget_exceeded"
    assert "Result byte budget spent: 8 of 8" in (second.detail or "")


async def test_budget_without_ledger_never_delegates(tmp_path: Path) -> None:
    data = registry_data(budgets={"planner": {"delegations": 5}})
    config = load(tmp_path, data)
    transport = HttpTransport(config.limits, transport=httpx.MockTransport(_ok))
    delegator = Delegator(config, transport, {"research": SECRET}, "planner")
    try:
        result = await delegator.delegate_task("research", "one")
    finally:
        await transport.aclose()

    assert result.error_code == "budget_exceeded"
    assert "--ledger" in (result.detail or "")


def test_render_graph(tmp_path: Path) -> None:
    config = load(tmp_path, registry_data())
    text = render_graph(config)
    assert "-> research" in text
    assert "flowchart LR" in text
    assert "planner --> research" in text


def test_render_budget_and_audit(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        registry_data(budgets={"planner": {"delegations": 2}}),
    )
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    delegator_transport = HttpTransport(
        config.limits, transport=httpx.MockTransport(_ok)
    )
    delegator = Delegator(
        config, delegator_transport, {"research": SECRET}, "planner", ledger=ledger
    )
    try:
        import asyncio

        asyncio.run(delegator.delegate_task("research", "one"))
    finally:
        asyncio.run(delegator_transport.aclose())

    budget_text = render_budget(config, [tmp_path / "ledger.sqlite3"])
    assert "1/2 ok" in budget_text
    assert "planner" in budget_text

    audit_text = render_audit([tmp_path / "ledger.sqlite3"])
    assert "planner -> research" in audit_text
    assert "completed=1" in audit_text
    ledger.close()


async def test_budget_fails_closed_when_ledger_unreadable(tmp_path: Path) -> None:
    import sqlite3

    delegator, transport, ledger = build(tmp_path, {"planner": {"delegations": 5}})
    try:
        first = await delegator.delegate_task("research", "one")
        assert first.status is OutcomeKind.completed

        class BrokenConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        original = ledger._conn
        ledger._conn = BrokenConn()
        second = await delegator.delegate_task("research", "two")
        ledger._conn = original
    finally:
        await transport.aclose()
        ledger.close()

    assert second.status is OutcomeKind.failed
    assert second.error_code == "budget_exceeded"
    assert "unreadable" in (second.detail or "")


def test_usage_for_signals_unreadable_ledger() -> None:
    import sqlite3

    class BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("locked")

    ledger = Ledger(None)
    assert ledger.usage_for("planner") == (0, 0, 0.0)
    ledger._conn = BrokenConn()
    assert ledger.usage_for("planner") is None
