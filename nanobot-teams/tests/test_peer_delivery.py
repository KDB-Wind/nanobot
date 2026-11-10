"""P5: peer results return to the SENDER's inbox with the sender's own
capability (I5), validated at admission against the sender binding.

The delivery target for a peer task is the sender bot's receive binding, not
the planner; the capability is minted by the sender's host, so its workflow is
the sender's session workflow (task.workflow_id) while conversation threading
rides the separate conversation_root column.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.models import Bot, DeliveryMode
from nanobot_teams.coordinator.store import CoordinatorStore
from nanobot_teams.coordinator.worker import DispatchWorker

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

ADMIN = "delivery-admin"
PLANNER = "delivery-planner"
RESEARCH_COORD = "delivery-research-coord"
RESEARCH_INBOX_KEY = "delivery-research-inbox-key"
SENDER_WORKFLOW = "wf-research-session"


def make_config(tmp_path) -> CoordinatorConfig:
    return CoordinatorConfig(
        control_dir=tmp_path / "control",
        planner=PlannerBinding(
            inbox_url="http://127.0.0.1:59999",
            instance_id="planner-inst",
            principal_id="planner",
        ),
    )


def peer_bot(bot_id: str, *, endpoint: str, coord_env: str) -> Bot:
    return Bot(
        bot_id=bot_id,
        display_name=bot_id.title(),
        role=bot_id,
        endpoint=endpoint,
        secret_env=f"RUNS_{bot_id.upper()}",
        enabled=True,
        capabilities=("execute",),
        coord_secret_env=coord_env,
        inbox_url=endpoint,
        inbox_principal_id="teams-coordinator",
        inbox_key_env="RESEARCH_INBOX_KEY_ENV",
    )


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "coord.sqlite3")
    s.open()
    s.create_project("peer", "Peer comms")
    yield s
    s.close()


def seed_pair(store, *, sender_endpoint="http://127.0.0.1:9301") -> None:
    store.upsert_bot(peer_bot("research", endpoint=sender_endpoint,
                              coord_env="RESEARCH_COORD_ENV"))
    store.upsert_bot(peer_bot("writer", endpoint="http://127.0.0.1:9302",
                              coord_env="WRITER_COORD_ENV"))
    store.upsert_peer_edge("research", "writer")


def admit_auto_peer(store, *, reply_capability="cap-1",
                    workflow=SENDER_WORKFLOW) -> object:
    seed_pair(store)
    task, _ = store.admit_task(
        caller_principal="bot:research",
        project_id="peer",
        workflow_id=workflow,
        bot_id="writer",
        operation_key=str(uuid.uuid4()),
        input_text="do it",
        deadline_at=None,
        delivery_mode=DeliveryMode.AUTO_RESUME,
        bot=store.get_bot("writer"),
        reply_capability=reply_capability,
        kind="peer",
        conversation_root=str(uuid.uuid4()),
        hop=0,
        sender_bot_id="research",
    )
    return task


def test_auto_peer_task_enters_delivery_queue(store):
    task = admit_auto_peer(store)
    store.record_observation(
        task.task_id, revision=2, run_state="succeeded", stop_confirmed=True,
        outcome_code="succeeded", result_text="the writer's answer",
        result_truncated=False, result_advertised=True,
    )
    ready = store.auto_resume_ready_tasks()
    assert any(t.task_id == task.task_id for t in ready)


def test_delivery_target_resolves_sender_binding(store):
    task = admit_auto_peer(store)
    worker = DispatchWorker(store, clients=None, planner=object())
    url, key_env = worker._delivery_target(task)
    assert url == "http://127.0.0.1:9301"
    assert key_env == "RESEARCH_INBOX_KEY_ENV"


def test_delivery_target_requires_complete_binding(store):
    task = admit_auto_peer(store)
    current = store.get_bot("research")
    assert current is not None
    store.upsert_bot(dataclasses.replace(current, inbox_key_env=None))
    worker = DispatchWorker(store, clients=None, planner=object())
    assert worker._delivery_target(task) is None


async def test_peer_send_with_capability_is_admitted_auto(tmp_path, store, monkeypatch):
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    monkeypatch.setenv("RESEARCH_COORD_ENV", RESEARCH_COORD)
    monkeypatch.setenv("RESEARCH_INBOX_KEY_ENV", RESEARCH_INBOX_KEY)

    validated: list[dict] = []

    async def validate(request: web.Request) -> web.Response:
        validated.append(await request.json())
        return web.json_response({"ok": True})

    inbox_app = web.Application()
    inbox_app.router.add_post("/v1/agent-inbox/capabilities/validate", validate)
    inbox_client = TestClient(TestServer(inbox_app))
    await inbox_client.start_server()
    try:
        sender = peer_bot("research",
                          endpoint=str(inbox_client.make_url("")).rstrip("/"),
                          coord_env="RESEARCH_COORD_ENV")
        store.upsert_bot(sender)
        store.upsert_bot(peer_bot("writer", endpoint="http://127.0.0.1:9302",
                                  coord_env="WRITER_COORD_ENV"))
        store.upsert_peer_edge("research", "writer")

        app = build_app(store, make_config(tmp_path),
                        {"operator": ADMIN, "planner": PLANNER})
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/v1/teams/peer/send",
                headers={"Authorization": f"Bearer {RESEARCH_COORD}"},
                json={
                    "recipient": "writer",
                    "input": "summarize",
                    "operation_key": str(uuid.uuid4()),
                    "workflow": SENDER_WORKFLOW,
                    "reply_capability": "cap-secret-value",
                },
            )
            assert resp.status == 201, await resp.text()
            payload = await resp.json()
            assert payload["delivery_mode"] == "auto_resume"
            assert validated, "the capability must be validated at the sender inbox"
            assert validated[0]["workflow"] == SENDER_WORKFLOW
            assert validated[0]["capability"] == "cap-secret-value"
            assert "cap-secret-value" not in json.dumps(payload)
        finally:
            await client.close()
    finally:
        await inbox_client.close()


async def test_peer_send_capability_rejected_by_sender_is_refused(
    tmp_path, store, monkeypatch
):
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    monkeypatch.setenv("RESEARCH_COORD_ENV", RESEARCH_COORD)
    monkeypatch.setenv("RESEARCH_INBOX_KEY_ENV", RESEARCH_INBOX_KEY)

    async def validate(request: web.Request) -> web.Response:
        return web.json_response(
            {"code": "capability_invalid", "message": "unknown capability"},
            status=403,
        )

    inbox_app = web.Application()
    inbox_app.router.add_post("/v1/agent-inbox/capabilities/validate", validate)
    inbox_client = TestServer(inbox_app)
    client = TestClient(inbox_client)
    await client.start_server()
    try:
        sender = peer_bot("research",
                          endpoint=str(client.make_url("")).rstrip("/"),
                          coord_env="RESEARCH_COORD_ENV")
        store.upsert_bot(sender)
        store.upsert_bot(peer_bot("writer", endpoint="http://127.0.0.1:9302",
                                  coord_env="WRITER_COORD_ENV"))
        store.upsert_peer_edge("research", "writer")
        app = build_app(store, make_config(tmp_path),
                        {"operator": ADMIN, "planner": PLANNER})
        coord_client = TestClient(TestServer(app))
        await coord_client.start_server()
        try:
            resp = await coord_client.post(
                "/v1/teams/peer/send",
                headers={"Authorization": f"Bearer {RESEARCH_COORD}"},
                json={
                    "recipient": "writer",
                    "input": "summarize",
                    "operation_key": str(uuid.uuid4()),
                    "workflow": SENDER_WORKFLOW,
                    "reply_capability": "cap-bogus",
                },
            )
            assert resp.status == 422
            assert (await resp.json())["code"] == "capability_invalid"
            assert store.list_tasks(caller_principal="bot:research") == []
        finally:
            await coord_client.close()
    finally:
        await client.close()
