"""R3: result fetch settlement, public result reads, truncation metadata.

Regression lock for the 2026-11-09 audit findings F07, F08, S08 and the
delivery half of F07 (an unfetched body must never ship as an empty success
report). The audit's reproducers live in review/async-20261109/; these tests
assert the fixed, observable behavior on the real worker and HTTP paths.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.models import Bot, DeliveryMode
from nanobot_teams.coordinator.store import (
    RESULT_RESERVATION_BYTES,
    CoordinatorStore,
    utc_now,
)
from nanobot_teams.coordinator.worker import ClientFactory, DispatchWorker

try:
    import aiohttp.test_utils  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

SECRET = "synthetic-result-pipeline-key"


class ResultBot:
    """Agent-run protocol server scripted for result-fetch behavior."""

    def __init__(self) -> None:
        self.instance_id = "inst_result_a"
        self.run_state = "running"
        self.revision = 1
        # Fail the first N result reads; 0 means always succeed.
        self.fail_result_requests = 0
        self.truncated = False
        self.result_text = "retained answer"
        self.result_requests = 0

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_get("/v1/agent-capabilities", self.capabilities)
        application.router.add_post("/v1/agent-runs", self.submit)
        application.router.add_get("/v1/agent-runs/run_a", self.get_run)
        application.router.add_get("/v1/agent-runs/run_a/result", self.get_result)
        return application

    async def capabilities(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "execution": True}
        )

    async def submit(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "run_id": "run_a",
             "state": "running", "revision": 1},
            status=202,
        )

    async def get_run(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "run_id": "run_a",
             "state": self.run_state, "revision": self.revision,
             "stop_confirmed": self.run_state == "succeeded",
             "effects_uncertain": False,
             "outcome": {"code": self.run_state} if self.run_state != "running" else None,
             "result_available": self.run_state == "succeeded"}
        )

    async def get_result(self, request: web.Request) -> web.Response:
        self.result_requests += 1
        if self.result_requests <= self.fail_result_requests:
            return web.json_response({"code": "temporary_failure"}, status=503)
        return web.json_response(
            {"run_id": "run_a",
             "outcome": {"code": "succeeded", "text": self.result_text,
                         "truncated": self.truncated}}
        )


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "results.sqlite3")
    s.open()
    s.create_project("proj", "Main")
    yield s
    s.close()


def _register(store: CoordinatorStore, endpoint: str) -> Bot:
    bot = Bot(bot_id="research", display_name="Research", role="research",
              endpoint=endpoint, secret_env="RESULT_BOT_KEY",
              instance_id="inst_result_a")
    store.upsert_bot(bot)
    return bot


def _admit(store: CoordinatorStore, bot: Bot, *, caller: str = "planner") -> object:
    return store.admit_task(
        caller_principal=caller, project_id="proj", workflow_id="wf",
        bot_id=bot.bot_id, operation_key=str(uuid.uuid4()), input_text="result task",
        deadline_at=utc_now() + timedelta(seconds=300),
        delivery_mode=DeliveryMode.WORKBENCH_ONLY, bot=bot,
    )[0]


async def _pump_until(worker: DispatchWorker, predicate, timeout: float = 6.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await worker.pump_once()
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _running_task(store: CoordinatorStore) -> tuple[Bot, object, ResultBot, TestServer]:
    bot = ResultBot()
    server = TestServer(bot.app())
    await server.start_server()
    registered = _register(store, str(server.make_url("")).rstrip("/"))
    task = _admit(store, registered)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    await worker.pump_once()  # dispatch to run_a
    return registered, task, bot, server


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_failed_result_fetch_keeps_retrying_until_body_persists(store):
    """F07: a 503 on the first result read must not settle the fetch."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.fail_result_requests = 1
        assert await _pump_until(
            worker, lambda: store.get_task(task.task_id).result_fetched
        )
        settled = store.get_task(task.task_id)
        assert settled.run_state == "succeeded"
        assert settled.result_text == "retained answer"
        assert bot.result_requests >= 2, "the fetch was retried on the same remote run"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_observed_result_without_body_stays_in_fetch_queue(store):
    """Settlement rule: only a persisted body marks the result fetched."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.fail_result_requests = 2
        await worker.pump_once()
        current = store.get_task(task.task_id)
        assert current.result_available, "terminal remote advertised the body"
        assert not current.result_fetched, "the failed fetch must not count as settled"
        assert [t.task_id for t in store.tasks_with_unfetched_results()] == [
            task.task_id
        ]
        bot.fail_result_requests = 0
        store.persist_result_body(
            task.task_id, result_text="retained answer", result_truncated=False
        )
        assert store.get_task(task.task_id).result_fetched
        assert store.tasks_with_unfetched_results() == []
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_remote_truncation_metadata_is_preserved(store):
    """S08: a partial body keeps the remote's typed truncation marker."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.truncated = True
        assert await _pump_until(
            worker, lambda: store.get_task(task.task_id).result_fetched
        )
        settled = store.get_task(task.task_id)
        assert settled.result_truncated is True
        assert settled.result_text == "retained answer"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_oversized_remote_body_is_bounded_and_marked(store):
    """The stored body is bounded; the marker never claims completeness."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.truncated = False
        bot.result_text = "x" * (RESULT_RESERVATION_BYTES + 4096)
        assert await _pump_until(
            worker, lambda: store.get_task(task.task_id).result_fetched
        )
        settled = store.get_task(task.task_id)
        assert len(settled.result_text.encode("utf-8")) <= RESULT_RESERVATION_BYTES
        assert settled.result_truncated is True
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_unfetched_body_is_never_delivery_ready(store):
    """F07 delivery half: no empty success report leaves the coordinator."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.fail_result_requests = 2
        await worker.pump_once()
        current = store.get_task(task.task_id)
        assert current.result_available and not current.result_fetched
        ready_before = [t.task_id for t in store.auto_resume_ready_tasks()]
        assert task.task_id not in ready_before
        bot.fail_result_requests = 0
        assert await _pump_until(
            worker, lambda: store.get_task(task.task_id).result_fetched
        )
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_public_result_route_serves_bodies_without_bot_credentials(store, tmp_path):
    """F08: planner and operator read the body through the coordinator only."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.truncated = True
        assert await _pump_until(
            worker, lambda: store.get_task(task.task_id).result_fetched
        )
        foreign = _admit(store, registered, caller="someone-else")
        store.record_observation(
            foreign.task_id, revision=2, run_state="succeeded", stop_confirmed=True,
            result_text="secret draft", result_truncated=False,
        )
        app = build_app(
            store,
            CoordinatorConfig(control_dir=tmp_path),
            {"operator": "op-token", "planner": "plan-token"},
        )
        async with TestClient(TestServer(app)) as client:
            for role, token in (("planner", "plan-token"), ("operator", "op-token")):
                detail = await client.get(
                    f"/v1/teams/tasks/{task.task_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert detail.status == 200
                body = await detail.json()
                assert body["result_available"] is True
                assert "result_text" not in body, "detail stays metadata-only"
                result = await client.get(
                    f"/v1/teams/tasks/{task.task_id}/result",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert result.status == 200
                payload = await result.json()
                assert payload["result_text"] == "retained answer"
                assert payload["result_truncated"] is True
            # The planner never reads another principal's result body.
            denied = await client.get(
                f"/v1/teams/tasks/{foreign.task_id}/result",
                headers={"Authorization": "Bearer plan-token"},
            )
            assert denied.status == 403
            missing = await client.get(
                "/v1/teams/tasks/task_none/result",
                headers={"Authorization": "Bearer plan-token"},
            )
            assert missing.status == 404
            anonymous = await client.get(f"/v1/teams/tasks/{task.task_id}/result")
            assert anonymous.status == 401
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_result_route_reports_pending_fetch(store, tmp_path):
    """A terminal advertised body that is still fetching answers 409, not empty."""
    registered, task, bot, server = await _running_task(store)
    worker = DispatchWorker(store, ClientFactory({"RESULT_BOT_KEY": SECRET}))
    try:
        bot.run_state = "succeeded"
        bot.revision = 2
        bot.fail_result_requests = 2
        await worker.pump_once()
        app = build_app(
            store,
            CoordinatorConfig(control_dir=tmp_path),
            {"operator": "op-token", "planner": "plan-token"},
        )
        async with TestClient(TestServer(app)) as client:
            result = await client.get(
                f"/v1/teams/tasks/{task.task_id}/result",
                headers={"Authorization": "Bearer plan-token"},
            )
            assert result.status == 409
            assert (await result.json())["code"] == "result_fetch_pending"
    finally:
        await server.close()
