"""P4 chain: coordinator admission with capability, delivery, planner inbox injection.

Covers the A09/A10/A11/A18 protocol slices over real HTTP endpoints in one
process: a planner inbox sidecar (real SessionManager), the coordinator HTTP
API, and a fake bot.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app as build_coordinator_app
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.store import CoordinatorStore
from nanobot_teams.coordinator.worker import DispatchWorker

from nanobot.api.agent_inbox import register_inbox_routes
from nanobot.config.schema import ApiRunsConfig
from nanobot.session.external_inbox import ExternalInbox
from nanobot.session.manager import SessionManager

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

RUNS_KEY = "planner-runs-key"


class FakeBot:
    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.keyed: dict[str, str] = {}

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_get("/v1/agent-capabilities", self._caps)
        application.router.add_post("/v1/agent-runs", self._submit)
        application.router.add_get("/v1/agent-runs/{run_id}", self._get)
        application.router.add_get("/v1/agent-runs/{run_id}/result", self._result)
        return application

    async def _caps(self, _request):
        return web.json_response(
            {"protocol": 1, "instance_id": "inst_research", "execution": True}
        )

    async def _submit(self, request):
        key = request.headers.get("Idempotency-Key", "")
        if key not in self.keyed:
            run_id = f"run_{uuid.uuid4().hex[:8]}"
            self.keyed[key] = run_id
            self.runs[run_id] = {"state": "running", "revision": 1}
        return web.json_response(
            {
                "protocol": 1,
                "instance_id": "inst_research",
                "run_id": self.keyed[key],
                "state": "running",
                "revision": 1,
            },
            status=202,
        )

    async def _get(self, request):
        run = self.runs.get(request.match_info["run_id"])
        if run is None:
            return web.json_response({}, status=404)
        return web.json_response(
            {
                "protocol": 1,
                "instance_id": "inst_research",
                "run_id": request.match_info["run_id"],
                "state": run["state"],
                "revision": run["revision"],
                "stop_confirmed": True,
                "effects_uncertain": False,
                "outcome": {"code": "succeeded"},
                "result_available": run["state"] == "succeeded",
            }
        )

    async def _result(self, request):
        return web.json_response(
            {"outcome": {"code": "succeeded", "text": "research findings summary"}}
        )

    def finish_all(self):
        for run in self.runs.values():
            run["state"] = "succeeded"
            run["revision"] += 1


@pytest.fixture
async def chain(tmp_path, monkeypatch):
    # Planner side: session manager + inbox sidecar HTTP app.
    manager = SessionManager(tmp_path / "sessions-root")
    state = manager.state
    inbox = ExternalInbox(state)
    runs_config = ApiRunsConfig(
        enabled=True, api_key=RUNS_KEY, principal_id="teams-coordinator",
        inbox_enabled=True,
    )
    inbox_app = web.Application()
    register_inbox_routes(inbox_app, inbox, runs_config)
    inbox_server = TestServer(inbox_app)
    await inbox_server.start_server()

    # Seed a planner session and mint a capability like the gateway meta hook.
    session_key = "planner-chat"
    await state._execute(
        session_key,
        lambda: state._change(
            session_key, lambda s: s.add_message("user", "research and report"),
            create_if_missing=True,
        ),
        operation_name="seed",
    )
    session = await state.read(session_key)
    assert session is not None

    # Coordinator side.
    store = CoordinatorStore(tmp_path / "coord" / "c.sqlite3")
    store.open()
    store.create_project("proj", "Main")
    bot_port = inbox_server.port  # placeholder, replaced below
    fake_bot = FakeBot()
    bot_server = TestServer(fake_bot.app())
    await bot_server.start_server()
    store.upsert_bot(
        Bot(
            bot_id="research", display_name="Research", role="research",
            endpoint=str(bot_server.make_url("")), secret_env="TEAMS_P4_BOT_KEY",
        )
    )
    planner = PlannerBinding(
        inbox_url=str(inbox_server.make_url("")),
        instance_id="planner-inst",
        principal_id="teams-coordinator",
        inbox_key_env="TEAMS_P4_PLANNER_INBOX_KEY",
    )
    monkeypatch.setenv("TEAMS_P4_PLANNER_INBOX_KEY", RUNS_KEY)
    monkeypatch.setenv("TEAMS_P4_BOT_KEY", "bot-secret")
    coord_config = CoordinatorConfig(
        control_dir=tmp_path / "coord",
        host="127.0.0.1",
        port=0,
        planner=planner,
    )
    coord_app = build_coordinator_app(
        store, coord_config, {"operator": "admin-x", "planner": "planner-x"}
    )
    coord_server = TestServer(coord_app)
    await coord_server.start_server()

    worker = DispatchWorker(
        store,
        __import__("nanobot_teams.coordinator.worker", fromlist=["ClientFactory"])
        .ClientFactory(),
        planner=planner,
    )

    yield {
        "manager": manager,
        "state": state,
        "inbox": inbox,
        "session_key": session_key,
        "generation": session.generation,
        "inbox_client": TestClient(inbox_server),
        "coord_client": TestClient(coord_server),
        "store": store,
        "worker": worker,
        "fake_bot": fake_bot,
    }

    await inbox_server.close()
    await bot_server.close()
    await coord_server.close()
    store.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_auto_resume_full_chain_delivers_and_injects(chain):
    inbox = chain["inbox"]
    state = chain["state"]
    coord = chain["coord_client"]
    store = chain["store"]
    worker = chain["worker"]
    fake_bot = chain["fake_bot"]

    capability = inbox.mint_capability(
        session_key=chain["session_key"],
        session_generation=chain["generation"],
        workflow_id="wf_turn1",
        operation_key="*",
        bot_id="*",
        instance_id="*",
        principal_id="teams-coordinator",
    )
    operation_key = str(uuid.uuid4())
    submitted = await coord.post(
        "/v1/teams/tasks",
        headers={"Authorization": "Bearer planner-x"},
        json={
            "project": "proj",
            "bot": "research",
            "input": "go research",
            "operation_key": operation_key,
            "workflow": "wf_turn1",
            "delivery_mode": "auto_resume",
            "reply_capability": capability.token,
        },
    )
    assert submitted.status == 201, await submitted.text()
    task_id = (await submitted.json())["task_id"]

    accepted = False
    for _ in range(200):
        await worker.pump_once()
        task = store.get_task(task_id)
        if task.dispatch_state.value == "accepted":
            accepted = True
            break
        await asyncio.sleep(0.05)
    assert accepted
    fake_bot.finish_all()
    injected = False
    for _ in range(200):
        await worker.pump_once()
        task = store.get_task(task_id)
        if task.delivery_state.value == "injected":
            injected = True
            break
        await asyncio.sleep(0.05)
    assert injected, f"delivery_state={store.get_task(task_id).delivery_state}"
    session = await state.read(chain["session_key"])
    assert session is not None
    reports = [
        m for m in session.messages
        if m.get("injected_event") == "external_delivery"
    ]
    assert len(reports) == 1
    assert "research findings summary" in reports[0]["content"]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_forged_capability_or_revoked_workflow_fails_admission(chain):
    inbox = chain["inbox"]
    coord = chain["coord_client"]
    inbox_client = chain["inbox_client"]

    # Forged token: the planner inbox rejects validation.
    submitted = await coord.post(
        "/v1/teams/tasks",
        headers={"Authorization": "Bearer planner-x"},
        json={
            "project": "proj",
            "bot": "research",
            "input": "x",
            "operation_key": str(uuid.uuid4()),
            "workflow": "wf_forged",
            "delivery_mode": "auto_resume",
            "reply_capability": "forged-token",
        },
    )
    assert submitted.status == 422
    assert (await submitted.json())["code"] == "origin_required"

    # Real capability, but the workflow's auto-continue was revoked first.
    capability = inbox.mint_capability(
        session_key=chain["session_key"],
        session_generation=chain["generation"],
        workflow_id="wf_revoked",
        operation_key="*",
        bot_id="*",
        instance_id="*",
        principal_id="teams-coordinator",
    )
    inbox.revoke_session_workflows(chain["session_key"])
    submitted = await coord.post(
        "/v1/teams/tasks",
        headers={"Authorization": "Bearer planner-x"},
        json={
            "project": "proj",
            "bot": "research",
            "input": "x",
            "operation_key": str(uuid.uuid4()),
            "workflow": "wf_revoked",
            "delivery_mode": "auto_resume",
            "reply_capability": capability.token,
        },
    )
    assert submitted.status == 422
    assert (await submitted.json())["code"] == "auto_continue_revoked"

    # Auto_resume without any capability is refused client-side.
    submitted = await coord.post(
        "/v1/teams/tasks",
        headers={"Authorization": "Bearer planner-x"},
        json={
            "project": "proj",
            "bot": "research",
            "input": "x",
            "operation_key": str(uuid.uuid4()),
            "delivery_mode": "auto_resume",
        },
    )
    assert submitted.status == 422
    assert (await submitted.json())["code"] == "origin_required"

    # The inbox never accepts plain chat credentials.
    denied = await inbox_client.post(
        "/v1/agent-inbox",
        headers={"Authorization": "Bearer some-other-key"},
        json={},
    )
    assert denied.status == 401


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_session_reset_routes_delivery_orphaned(chain):
    inbox = chain["inbox"]
    state = chain["state"]
    worker = chain["worker"]
    store = chain["store"]

    capability = inbox.mint_capability(
        session_key=chain["session_key"],
        session_generation=chain["generation"],
        workflow_id="wf_stale",
        operation_key="*",
        bot_id="*",
        instance_id="*",
        principal_id="teams-coordinator",
    )
    # The user resets the chat before the result arrives: new generation.
    await state.reset(chain["session_key"])
    inbox.revoke_session_workflows(chain["session_key"])

    # Simulate the coordinator delivering straight to the inbox (410 path).
    inbox_client = chain["inbox_client"]
    delivery = {
        "delivery_id": "del_stale_1",
        "reply_capability": capability.token,
        "operation_key": "op_stale",
        "workflow_id": "wf_stale",
        "bot_id": "research",
        "instance_id": "inst_research",
        "task_id": "task_stale",
        "run_id": "run_stale",
        "terminal_revision": 2,
        "outcome": {"code": "succeeded"},
        "result_text": "late result",
        "result_truncated": False,
    }
    response = await inbox_client.post(
        "/v1/agent-inbox",
        headers={"Authorization": f"Bearer {RUNS_KEY}"},
        json=delivery,
    )
    assert response.status in (409, 410, 422), await response.text()
    # A11: the new session history never contains the late result.
    session = await state.read(chain["session_key"])
    assert session is not None
    assert not any(m.get("injected_event") == "external_delivery" for m in session.messages)
