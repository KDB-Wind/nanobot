"""P4 HTTP surface: POST /v1/teams/peer/send and the peer-edge routes.

The route is the coordinator-side backend of the send_to_bot MCP tool: bot
principals only, typed rejections with audit events, idempotent replays.
"""

from __future__ import annotations

import json
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.store import CoordinatorStore

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

ADMIN = "send-admin-token"
PLANNER = "send-planner-token"
RESEARCH_KEY = "send-research-coord-key"
WRITER_KEY = "send-writer-coord-key"


def make_config(tmp_path) -> CoordinatorConfig:
    return CoordinatorConfig(
        control_dir=tmp_path / "control",
        planner=PlannerBinding(
            inbox_url="http://127.0.0.1:8765",
            instance_id="planner-inst",
            principal_id="planner",
        ),
    )


def make_bot(bot_id: str, key_env: str) -> Bot:
    return Bot(
        bot_id=bot_id,
        display_name=bot_id.title(),
        role=bot_id,
        endpoint=f"http://127.0.0.1:910{len(bot_id)}",
        secret_env=f"RUNS_{bot_id.upper()}",
        enabled=True,
        capabilities=("execute",),
        coord_secret_env=key_env,
    )


@pytest.fixture
async def lab(tmp_path, monkeypatch):
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    monkeypatch.setenv("RESEARCH_COORD_TOKEN", RESEARCH_KEY)
    monkeypatch.setenv("WRITER_COORD_TOKEN", WRITER_KEY)
    store = CoordinatorStore(tmp_path / "coord.sqlite3")
    store.open()
    store.create_project("peer", "Peer comms")
    store.upsert_bot(make_bot("research", "RESEARCH_COORD_TOKEN"))
    store.upsert_bot(make_bot("writer", "WRITER_COORD_TOKEN"))
    store.upsert_peer_edge("research", "writer")
    app = build_app(store, make_config(tmp_path), {"operator": ADMIN, "planner": PLANNER})
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, store
    finally:
        await client.close()
        store.close()


def bot_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def send_body(**overrides) -> dict:
    body = {
        "recipient": "writer",
        "input": "reply with the marker",
        "operation_key": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


async def test_peer_send_creates_a_peer_task(lab):
    client, store = lab
    resp = await client.post(
        "/v1/teams/peer/send", headers=bot_headers(RESEARCH_KEY), json=send_body()
    )
    assert resp.status == 201
    payload = await resp.json()
    assert payload["kind"] == "peer"
    assert payload["bot_id"] == "writer"
    assert payload["caller_principal"] == "bot:research"
    assert payload["conversation_root"]
    # The hop is server-derived; a fresh conversation starts at 0.
    assert payload["hop"] == 0


async def test_peer_send_same_key_replays_the_task(lab):
    client, store = lab
    body = send_body()
    first = await client.post(
        "/v1/teams/peer/send", headers=bot_headers(RESEARCH_KEY), json=body
    )
    second = await client.post(
        "/v1/teams/peer/send", headers=bot_headers(RESEARCH_KEY), json=body
    )
    assert first.status == 201
    assert second.status == 200
    assert (await first.json())["task_id"] == (await second.json())["task_id"]


async def test_peer_send_without_edge_is_a_typed_rejection_with_audit(lab):
    client, store = lab
    resp = await client.post(
        "/v1/teams/peer/send",
        headers=bot_headers(WRITER_KEY),
        json=send_body(recipient="research"),
    )
    assert resp.status == 403
    payload = await resp.json()
    assert payload["code"] == "unauthorized_edge"
    events = [
        e for e in store.events_after(0)
        if e.type == "peer_send_rejected"
        and e.data.get("sender") == "writer"
        and e.data.get("recipient") == "research"
        and e.data.get("code") == "unauthorized_edge"
        and e.data.get("conversation_root")
    ]
    assert events, "the rejection must be audited with routing facts"


async def test_peer_send_fifth_hop_is_typed_rejected(lab):
    client, store = lab
    # Seed four tasks into the conversation via the planner route, which now
    # threads the workflow as the conversation root.
    root = str(uuid.uuid4())
    for _ in range(4):
        seeded = await client.post(
            "/v1/teams/tasks",
            headers={"Authorization": f"Bearer {PLANNER}"},
            json={"project": "peer", "bot": "research",
                  "input": "x", "operation_key": str(uuid.uuid4()),
                  "workflow": root},
        )
        assert seeded.status == 201, await seeded.text()
    resp = await client.post(
        "/v1/teams/peer/send",
        headers=bot_headers(RESEARCH_KEY),
        json=send_body(conversation_root=root),
    )
    assert resp.status == 422
    assert (await resp.json())["code"] == "hop_limit_exceeded"


async def test_peer_send_rejects_role_tokens(lab):
    client, store = lab
    resp = await client.post(
        "/v1/teams/peer/send",
        headers={"Authorization": f"Bearer {PLANNER}"},
        json=send_body(),
    )
    assert resp.status == 403
    assert (await resp.json())["code"] == "peer_forbidden"


async def test_peer_send_rejects_oversized_input(lab):
    client, store = lab
    resp = await client.post(
        "/v1/teams/peer/send",
        headers=bot_headers(RESEARCH_KEY),
        json=send_body(input="x" * (16 * 1024 + 1)),
    )
    assert resp.status == 400
    assert (await resp.json())["code"] == "input_too_large"


async def test_peer_edge_management_roundtrip(lab):
    client, store = lab
    listing = await client.get(
        "/v1/teams/peer-edges", headers={"Authorization": f"Bearer {PLANNER}"}
    )
    assert listing.status == 200
    assert (await listing.json())["edges"] == [
        {"from_bot_id": "research", "to_bot_id": "writer"}
    ]
    # Creating an edge is operator-only.
    denied = await client.post(
        "/v1/teams/peer-edges",
        headers={"Authorization": f"Bearer {PLANNER}"},
        json={"from_bot_id": "research", "to_bot_id": "writer"},
    )
    assert denied.status == 403
    created = await client.post(
        "/v1/teams/peer-edges",
        headers={"Authorization": f"Bearer {ADMIN}"},
        json={"from_bot_id": "writer", "to_bot_id": "research"},
    )
    assert created.status == 201
    removed = await client.delete(
        "/v1/teams/peer-edges/writer/research",
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert removed.status == 200
    missing = await client.delete(
        "/v1/teams/peer-edges/writer/research",
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert missing.status == 404


async def test_peer_send_events_carry_routing_facts(lab):
    client, store = lab
    resp = await client.post(
        "/v1/teams/peer/send", headers=bot_headers(RESEARCH_KEY), json=send_body()
    )
    assert resp.status == 201
    created = [
        e for e in store.events_after(0)
        if e.type == "task_created" and e.data.get("kind") == "peer"
    ]
    assert created
    data = created[0].data
    assert data["sender"] == "research"
    assert data["recipient"] == "writer"
    assert "conversation_root" in data and "sender" in data


async def test_peer_task_json_has_no_secret_material(lab):
    client, store = lab
    resp = await client.post(
        "/v1/teams/peer/send", headers=bot_headers(RESEARCH_KEY), json=send_body()
    )
    payload = await resp.json()
    raw = json.dumps(payload)
    assert RESEARCH_KEY not in raw and WRITER_KEY not in raw and ADMIN not in raw
