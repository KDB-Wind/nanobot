from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from nanobot_teams.config import ConfigError, load_teams_config
from nanobot_teams.delegation import Delegator
from nanobot_teams.envelope import parse_preamble
from nanobot_teams.ledger import Ledger, LedgerError
from nanobot_teams.transport import HttpTransport


def registry() -> dict:
    return {
        "schema_version": 2, "team_id": "repair", "thread_key": "main",
        "bots": [
            {"id": "planner", "display_name": "Planner", "role": "plans"},
            {"id": "research", "display_name": "Research", "role": "research",
             "endpoint": "http://127.0.0.1:18901/v1/chat/completions",
             "api_key_env": "RESEARCH_KEY", "allowed_callers": ["planner"]},
            {"id": "writer", "display_name": "Writer", "role": "writes",
             "endpoint": "http://127.0.0.1:18902/v1/chat/completions",
             "api_key_env": "WRITER_KEY", "allowed_callers": ["planner"]},
        ],
    }


def build(tmp_path: Path, handler, *, data: dict | None = None, book: Ledger | None = None):
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data or registry()), encoding="utf-8")
    config = load_teams_config(path)
    ledger = book if book is not None else Ledger(tmp_path / "state.sqlite3")
    transport = HttpTransport(config.limits, httpx.MockTransport(handler))
    delegator = Delegator(config, transport, {"research": "fake", "writer": "fake"},
                          "planner", ledger=ledger)
    return delegator, transport, ledger


def ok(text: str = "abcdefgh") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_cancelled_submission_stays_blocked_after_restart(tmp_path: Path) -> None:
    received = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            received.set()
            await asyncio.Event().wait()
        return ok()

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        task = asyncio.create_task(delegator.delegate_task("research", "one"))
        await asyncio.wait_for(received.wait(), 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        blocked = await delegator.delegate_task("research", "two")
        assert blocked.error_code == "blocked"
        assert ledger.list_tasks()[0].status == "outcome_unknown"
    finally:
        await transport.aclose()
        ledger.close()
    fresh, transport, ledger = build(tmp_path, handler)
    try:
        assert (await fresh.delegate_task("research", "three")).error_code == "blocked"
        assert (await fresh.delegate_task("writer", "independent")).status.value == "completed"
        assert calls == 2
    finally:
        await transport.aclose()
        ledger.close()


async def test_readable_but_unwritable_ledger_never_dispatches(tmp_path: Path) -> None:
    calls = []

    async def handler(request):
        calls.append(request)
        return ok()

    data = registry()
    data["budgets"] = {"planner": {"delegations": 1}}
    delegator, transport, ledger = build(tmp_path, handler, data=data)
    ledger._conn.execute("PRAGMA query_only=ON")
    try:
        result = await delegator.delegate_task("research", "one")
        assert result.error_code == "ledger_unavailable"
        assert calls == []
    finally:
        await transport.aclose()
        ledger.close()


async def test_surrogate_response_is_unknown_and_blocks(tmp_path: Path) -> None:
    async def handler(request):
        return httpx.Response(200, content=b'{"choices":[{"message":{"content":"\\ud800"}}]}')

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("research", "one")
        assert result.error_code == "response_invalid"
        assert (await delegator.delegate_task("research", "two")).error_code == "blocked"
    finally:
        await transport.aclose()
        ledger.close()


async def test_result_budget_clips_current_result(tmp_path: Path) -> None:
    async def handler(request):
        return ok()

    data = registry()
    data["budgets"] = {"planner": {"result_bytes": 1}}
    delegator, transport, ledger = build(tmp_path, handler, data=data)
    try:
        result = await delegator.delegate_task("research", "one")
        assert result.content == "a"
        assert result.truncated is True
        assert ledger.usage_for("planner")[2] > 0
        assert (await delegator.delegate_task("research", "two")).error_code == "budget_exceeded"
    finally:
        await transport.aclose()
        ledger.close()


async def test_explicit_thread_continues_only_its_target(tmp_path: Path) -> None:
    sessions = []

    async def handler(request):
        sessions.append(json.loads(request.content)["session_id"])
        return ok()

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        first = await delegator.delegate_task("research", "one")
        second = await delegator.delegate_task("research", "followup", thread_key=first.thread_key)
        isolated = await delegator.delegate_task("research", "other topic")
        cross_target = await delegator.delegate_task("writer", "steal thread", thread_key=first.thread_key)
        assert first.thread_key == second.thread_key
        assert isolated.thread_key != first.thread_key
        assert sessions[0] == sessions[1] != sessions[2]
        assert cross_target.error_code == "invalid_thread"
        assert len(sessions) == 3
    finally:
        await transport.aclose()
        ledger.close()


async def test_failed_terminal_commit_retains_admission(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.sqlite3")

    async def handler(request):
        ledger._conn.execute("PRAGMA query_only=ON")
        return ok()

    delegator, transport, ledger = build(tmp_path, handler, book=ledger)
    try:
        result = await delegator.delegate_task("research", "one")
        assert result.error_code == "ledger_unavailable"
        row = ledger.get_task(result.task_id)
        assert row.blocked and row.dispatch_state == "submitted"
        assert row.charged_bytes == 32768
    finally:
        await transport.aclose()
        ledger.close()

    async def unexpected(request):
        pytest.fail("unsettled request must remain quarantined after restart")

    fresh, transport, ledger = build(tmp_path, unexpected)
    try:
        assert (await fresh.delegate_task("research", "two")).error_code == "blocked"
        assert ledger.get_task(result.task_id).error_code == "interrupted"
    finally:
        await transport.aclose()
        ledger.close()


async def test_time_reservation_bounds_current_wait(tmp_path: Path) -> None:
    async def handler(request):
        await asyncio.Event().wait()

    data = registry()
    data["budgets"] = {"planner": {"chain_seconds": 0.02}}
    delegator, transport, ledger = build(tmp_path, handler, data=data)
    try:
        first = await asyncio.wait_for(delegator.delegate_task("research", "wait"), 2)
        assert first.error_code == "deadline_exceeded"
        assert ledger.usage_for("planner")[2] == pytest.approx(0.02)
        second = await delegator.delegate_task("writer", "other target")
        assert second.error_code == "budget_exceeded"
    finally:
        await transport.aclose()
        ledger.close()


async def test_acknowledgment_preserves_unknown_and_charges(tmp_path: Path) -> None:
    async def handler(request):
        return httpx.Response(500)

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("research", "one")
        before = ledger.usage_for("planner")
        ledger.acknowledge(result.task_id, actor="test-operator", reason="checked effects; accept duplicate risk")
        assert ledger.get_task(result.task_id).status == "outcome_unknown"
        assert not ledger.get_task(result.task_id).blocked
        assert ledger.usage_for("planner") == before
        assert ledger._conn.execute("SELECT actor,reason FROM events WHERE event='acknowledged_duplicate_risk'").fetchone() == (
            "test-operator", "checked effects; accept duplicate risk")
        assert (await delegator.delegate_task("research", "two")).error_code == "target_error"
    finally:
        await transport.aclose()
        ledger.close()


async def test_response_loss_records_possible_submission(tmp_path: Path) -> None:
    async def handler(request):
        raise httpx.ReadError("response lost", request=request)

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("research", "one")
        row = ledger.get_task(result.task_id)
        assert row.http_sent is True and row.request_bytes > 0
        assert row.blocked is True
    finally:
        await transport.aclose()
        ledger.close()


def test_second_writer_and_other_scope_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    try:
        first.bind("team-one", "planner")
        with pytest.raises(LedgerError, match="another writer"):
            Ledger(path)
        reader = Ledger(path, read_only=True)
        assert reader.list_tasks() == []
        reader.close()
    finally:
        first.close()
    second = Ledger(path)
    try:
        with pytest.raises(LedgerError, match="another team"):
            second.bind("team-two", "planner")
    finally:
        second.close()


async def test_default_multihop_and_intermediate_root_are_refused(tmp_path: Path) -> None:
    data = registry()
    data["bots"][2]["allowed_callers"] = ["research"]

    async def handler(request):
        pytest.fail("no request is authorized here")

    delegator, transport, ledger = build(tmp_path, handler, data=data)
    middle_book = Ledger(tmp_path / "middle.sqlite3")
    middle = Delegator(delegator._config, transport, {"writer": "fake"}, "research", ledger=middle_book)
    try:
        assert (await middle.delegate_task("writer", "new root")).error_code == "root_not_allowed"
        assert (await middle.delegate_task("writer", "continue", hop=2)).error_code == "multihop_disabled"
    finally:
        await transport.aclose()
        middle_book.close()
        ledger.close()


async def test_expiry_and_local_cap_are_used_in_child_envelope(tmp_path: Path) -> None:
    data = registry()
    data["allow_multihop"] = True
    data["bots"][2]["allowed_callers"] = ["research"]
    received = []

    async def handler(request):
        received.append(parse_preamble(json.loads(request.content)["messages"][0]["content"]))
        return ok()

    delegator, transport, ledger = build(tmp_path, handler, data=data)
    middle_book = Ledger(tmp_path / "middle.sqlite3")
    middle = Delegator(delegator._config, transport, {"writer": "fake"}, "research", ledger=middle_book)
    args = dict(root_task_id="11111111-1111-1111-1111-111111111111",
                parent_task_id="22222222-2222-2222-2222-222222222222", hop=2,
                deadline_remaining_s=86400, chain=["planner", "research"])
    try:
        expired = await middle.delegate_task("writer", "expired", expires_at=time.time()-1, **args)
        assert expired.error_code == "budget_exhausted"
        good = await middle.delegate_task("writer", "bound", expires_at=time.time()+86400, **args)
        assert good.status.value == "completed"
        assert 0 < received[0]["deadline_remaining_s"] <= 145 * 0.9
        assert received[0]["expires_at"] <= time.time() + 145
    finally:
        await transport.aclose()
        middle_book.close()
        ledger.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_limits_and_budgets_are_rejected(tmp_path: Path, value: float) -> None:
    path = tmp_path / "teams.json"
    for field in ("limits", "budgets"):
        data = registry()
        data[field] = {"total_timeout_s": value} if field == "limits" else {"planner": {"chain_seconds": value}}
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match="finite"):
            load_teams_config(path)


async def test_legacy_unknown_rows_remain_quarantined(tmp_path: Path) -> None:
    from nanobot_teams.ledger import _BASE_COLUMNS, _SCHEMA

    path = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(f"INSERT INTO tasks ({_BASE_COLUMNS}) VALUES ({','.join('?' for _ in range(16))})", (
        "old-task", "old-root", None, "planner", "research", 1,
        "outcome_unknown", "transport_error", 0, 0, 0, 0, 0, "2026-11-01", "2026-11-01", 12))
    conn.commit()
    conn.close()

    async def handler(request):
        pytest.fail("legacy unknown must not be forgotten")

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        assert (await delegator.delegate_task("research", "one")).error_code == "blocked"
        assert ledger.get_task("old-task").team_id == "repair"
    finally:
        await transport.aclose()
        ledger.close()


async def test_alias_target_cannot_bypass_endpoint_quarantine(tmp_path: Path) -> None:
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(500)

    data = registry()
    data["bots"][2]["endpoint"] = "http://127.0.0.1:018901/v1/chat/completions"
    delegator, transport, ledger = build(tmp_path, handler, data=data)
    try:
        assert (await delegator.delegate_task("research", "one")).error_code == "target_error"
        assert (await delegator.delegate_task("writer", "alias")).error_code == "blocked"
        assert len(calls) == 1
    finally:
        await transport.aclose()
        ledger.close()


async def test_invalid_target_does_not_inject_logs(tmp_path: Path, caplog) -> None:
    async def handler(request):
        pytest.fail("invalid target must not dispatch")

    delegator, transport, ledger = build(tmp_path, handler)
    try:
        with caplog.at_level("INFO"):
            result = await delegator.delegate_task("fake\nFORGED_LOG_LINE", "one")
        assert result.error_code == "invalid_target"
        assert "FORGED_LOG_LINE" not in caplog.text
    finally:
        await transport.aclose()
        ledger.close()
