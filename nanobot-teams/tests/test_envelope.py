from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanobot_teams.config import load_teams_config
from nanobot_teams.delegation import Delegator, session_id_for
from nanobot_teams.envelope import (
    EnvelopeError,
    envelope_for_root,
    parse_preamble,
    preamble_envelope,
    task_with_preamble,
    validate_continuation,
)
from nanobot_teams.transport import HttpTransport, OutcomeKind

ENDPOINT = "http://127.0.0.1:8901/v1/chat/completions"
SECRET = "sekret-value"

V2_DATA: dict[str, Any] = {
    "schema_version": 2,
    "team_id": "chain",
    "thread_key": "main",
    "bots": [
        {"id": "planner", "display_name": "Planner", "role": "planning"},
        {
            "id": "mid",
            "display_name": "Mid",
            "role": "middle",
            "endpoint": "http://127.0.0.1:8904/v1/chat/completions",
            "api_key_env": "TEAMS_MID_API_KEY",
            "allowed_callers": ["planner"],
        },
        {
            "id": "research",
            "display_name": "Research",
            "role": "research",
            "endpoint": ENDPOINT,
            "api_key_env": "TEAMS_RESEARCH_API_KEY",
            "allowed_callers": ["planner", "mid"],
        },
    ],
}


def load(tmp_path: Path, data: dict[str, Any] | None = None):
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data or V2_DATA), encoding="utf-8")
    return load_teams_config(path)


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def build(tmp_path: Path, handler, caller: str = "planner"):
    config = load(tmp_path)
    transport = HttpTransport(config.limits, transport=httpx.MockTransport(handler))
    keys = {bot.id: SECRET for bot in config.targets_for(caller)}
    return config, Delegator(config, transport, keys, caller), transport


def continuation(**overrides):
    base = {
        "parent_task_id": str(uuid.uuid4()),
        "root_task_id": str(uuid.uuid4()),
        "hop": 2,
        "deadline_remaining_s": 60.0,
        "chain": ["planner", "mid"],
    }
    base.update(overrides)
    return base


async def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request expected")


async def test_root_delegation_embeds_preamble(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=completion("ok"))

    _, delegator, transport = build(tmp_path, handler)
    try:
        result = await delegator.delegate_task("research", "find x")
    finally:
        await transport.aclose()

    assert result.status is OutcomeKind.completed
    assert result.root_task_id == result.task_id
    assert result.hop == 1

    sent = bodies[0]["messages"][0]["content"]
    fields = parse_preamble(sent)
    assert fields["root_task_id"] == result.task_id
    assert fields["task_id"] == result.task_id
    assert fields["parent_task_id"] == result.task_id
    assert fields["hop"] == 2
    assert fields["chain"] == ["planner", "research"]
    assert fields["deadline_remaining_s"] == pytest.approx(145.0 * 0.9)
    assert sent.endswith("find x")


async def test_continuation_flows_envelope(tmp_path: Path) -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=completion("ok"))

    _, delegator, transport = build(tmp_path, handler, caller="mid")
    args = continuation()
    try:
        result = await delegator.delegate_task("research", "dig deeper", **args)
    finally:
        await transport.aclose()

    assert result.status is OutcomeKind.completed
    assert result.root_task_id == args["root_task_id"]
    assert result.hop == 2

    fields = parse_preamble(bodies[0]["messages"][0]["content"])
    assert fields["root_task_id"] == args["root_task_id"]
    assert fields["parent_task_id"] == result.task_id
    assert fields["hop"] == 3
    assert fields["chain"] == ["planner", "mid", "research"]
    assert fields["deadline_remaining_s"] == pytest.approx(54.0)


async def test_loop_back_to_ancestor_refused(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("ok"))

    data = json.loads(json.dumps(V2_DATA))
    data["bots"][0]["endpoint"] = ENDPOINT
    data["bots"][0]["api_key_env"] = "TEAMS_PLANNER_API_KEY"
    data["bots"][0]["allowed_callers"] = ["mid"]
    config = load(tmp_path, data)
    transport = HttpTransport(config.limits, transport=httpx.MockTransport(handler))
    keys = {bot.id: SECRET for bot in config.targets_for("mid")}
    delegator = Delegator(config, transport, keys, "mid")
    args = continuation()
    try:
        result = await delegator.delegate_task("planner", "back to the root", **args)
    finally:
        await transport.aclose()

    assert result.error_code == "loop_detected"
    assert requests == []


async def test_hop_over_limit_refused(tmp_path: Path) -> None:
    _, delegator, transport = build(tmp_path, _unreachable, caller="mid")
    args = continuation(hop=3, chain=["planner", "mid", "research"])
    try:
        result = await delegator.delegate_task("research", "x", **args)
    finally:
        await transport.aclose()

    assert result.error_code == "hop_limit"


async def test_partial_envelope_refused(tmp_path: Path) -> None:
    _, delegator, transport = build(tmp_path, _unreachable, caller="mid")
    args = continuation()
    del args["chain"]
    try:
        result = await delegator.delegate_task("research", "x", **args)
    finally:
        await transport.aclose()

    assert result.error_code == "envelope_invalid"
    assert "chain" in (result.detail or "")


async def test_chain_shape_errors(tmp_path: Path) -> None:
    _, delegator, transport = build(tmp_path, _unreachable, caller="mid")
    cases = [
        (continuation(hop=1), "envelope_invalid"),
        (continuation(chain=["mid"]), "envelope_invalid"),
        (continuation(chain=["planner", "mid", "research"]), "envelope_invalid"),
        (continuation(chain=["planner", "ghost"]), "envelope_invalid"),
        (continuation(chain=["mid", "mid"]), "envelope_invalid"),
        (continuation(parent_task_id="not-a-uuid"), "envelope_invalid"),
        (continuation(deadline_remaining_s=0), "budget_exhausted"),
        (continuation(deadline_remaining_s=-5), "budget_exhausted"),
        (continuation(deadline_remaining_s="soon"), "envelope_invalid"),
    ]
    try:
        for args, expected in cases:
            result = await delegator.delegate_task("research", "x", **args)
            assert result.error_code == expected, (result.error_code, result.detail)
    finally:
        await transport.aclose()


async def test_deadline_clamps_wait_and_reports_timeout(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=completion("late"))

    _, delegator, transport = build(tmp_path, handler, caller="mid")
    args = continuation(deadline_remaining_s=0.5)
    try:
        result = await asyncio.wait_for(
            delegator.delegate_task("research", "x", **args), 10
        )
    finally:
        release.set()
        await transport.aclose()

    assert result.status is OutcomeKind.timed_out
    assert result.error_code == "deadline_exceeded"
    assert result.hop == 2


async def test_budget_exhausted_does_not_block(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion("ok"))

    _, delegator, transport = build(tmp_path, handler, caller="mid")
    try:
        first = await delegator.delegate_task(
            "research", "x", **continuation(deadline_remaining_s=0)
        )
        assert first.error_code == "budget_exhausted"
        assert first.status is OutcomeKind.failed
        second = await delegator.delegate_task("research", "y", **continuation())
        assert second.status is OutcomeKind.completed
    finally:
        await transport.aclose()


def test_validate_continuation_unit() -> None:
    envelope = validate_continuation(
        caller_id="mid",
        target_id="research",
        parent_task_id=str(uuid.uuid4()),
        root_task_id=str(uuid.uuid4()),
        hop=2,
        deadline_remaining_s=30,
        chain=["planner", "mid"],
        max_hops=2,
        known_bots={"planner", "mid", "research"},
    )
    assert envelope.hop == 2

    with pytest.raises(EnvelopeError) as exc:
        validate_continuation(
            caller_id="mid",
            target_id="research",
            parent_task_id=str(uuid.uuid4()),
            root_task_id=str(uuid.uuid4()),
            hop=5,
            deadline_remaining_s=30,
            chain=["planner", "mid"],
            max_hops=2,
            known_bots={"planner", "mid", "research"},
        )
    assert exc.value.error_code == "hop_limit"


def test_preamble_round_trip() -> None:
    envelope = envelope_for_root(str(uuid.uuid4()), "planner", 100.0)
    child = preamble_envelope(envelope, str(uuid.uuid4()), "research")
    text = task_with_preamble("do the thing", child, str(uuid.uuid4()))
    fields = parse_preamble(text)
    assert fields["chain"] == ["planner", "research"]
    assert fields["deadline_remaining_s"] == pytest.approx(90.0)
    assert text.endswith("do the thing")


def test_session_id_spans_callers(tmp_path: Path) -> None:
    config = load(tmp_path)
    assert session_id_for(config, "planner", "research") != session_id_for(
        config, "mid", "research"
    )


async def test_nan_and_infinite_deadline_refused(tmp_path: Path) -> None:
    _, delegator, transport = build(tmp_path, _unreachable, caller="mid")
    try:
        for bad in (float("nan"), float("inf")):
            result = await delegator.delegate_task(
                "research", "x", **continuation(deadline_remaining_s=bad)
            )
            assert result.error_code == "envelope_invalid", (bad, result.error_code)
    finally:
        await transport.aclose()
