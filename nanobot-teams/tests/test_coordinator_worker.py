"""Dispatch worker against a real local bot HTTP server (A03/A07/A13 slices)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nanobot_teams.coordinator.models import Bot, DeliveryMode
from nanobot_teams.coordinator.store import CoordinatorStore, utc_now
from nanobot_teams.coordinator.worker import ClientFactory, DispatchWorker

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

SECRET = "bot-secret"


class FakeBotApp:
    """Minimal agent-run protocol server with scriptable failures."""

    def __init__(self, *, instance_id="inst_bot_a", drop_first_submit=False):
        self.instance_id = instance_id
        self.drop_first_submit = drop_first_submit
        self.submissions: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.submit_count = 0
        self.cancel_calls: list[str] = []

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_get("/v1/agent-capabilities", self.capabilities)
        application.router.add_post("/v1/agent-runs", self.submit)
        application.router.add_get("/v1/agent-runs/lookup", self.lookup)
        application.router.add_get("/v1/agent-runs/{run_id}", self.get_run)
        application.router.add_get("/v1/agent-runs/{run_id}/result", self.get_result)
        application.router.add_post("/v1/agent-runs/{run_id}/cancel", self.cancel)
        return application

    def _authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {SECRET}"

    async def capabilities(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"code": "invalid_bearer"}, status=401)
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "execution": True}
        )

    async def submit(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"code": "invalid_bearer"}, status=401)
        key = request.headers.get("Idempotency-Key", "")
        body = await request.json()
        self.submit_count += 1
        if self.drop_first_submit and self.submit_count == 1:
            # Persist the submission, then kill the TCP connection so the
            # client sees an unknown outcome (A03's lost acceptance response).
            self._persist(key, body)
            self.drop_first_submit = False
            if request.transport is not None:
                request.transport.abort()
            return web.Response(status=599)
        run_id = self._persist(key, body)
        run = self.runs[run_id]
        return web.json_response(self._status(run), status=202)

    def _persist(self, key: str, body: dict) -> str:
        existing = self.submissions.get(key)
        if existing is not None:
            if existing["body"] != body:
                return "CONFLICT"
            return existing["run_id"]
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.submissions[key] = {"body": body, "run_id": run_id}
        self.runs[run_id] = {
            "run_id": run_id,
            "state": "queued",
            "revision": 1,
            "instance_id": self.instance_id,
            "stop_confirmed": False,
            "effects_uncertain": False,
            "outcome": None,
            "result_available": False,
        }
        return run_id

    async def lookup(self, request: web.Request) -> web.Response:
        key = request.query.get("operation_key", "")
        entry = self.submissions.get(key)
        if entry is None:
            return web.json_response({"code": "run_not_found"}, status=404)
        return web.json_response(self._status(self.runs[entry["run_id"]]))

    async def get_run(self, request: web.Request) -> web.Response:
        run = self.runs.get(request.match_info["run_id"])
        if run is None:
            return web.json_response({"code": "run_not_found"}, status=404)
        return web.json_response(self._status(run))

    async def get_result(self, request: web.Request) -> web.Response:
        run = self.runs.get(request.match_info["run_id"])
        if run is None or not run.get("result_available"):
            return web.json_response({"code": "not_finished"}, status=409)
        return web.json_response(
            {"run_id": run["run_id"], "outcome": {"code": "succeeded", "text": "final answer"}}
        )

    async def cancel(self, request: web.Request) -> web.Response:
        self.cancel_calls.append(request.match_info["run_id"])
        return web.json_response({"state": "cancelling"}, status=202)

    def _status(self, run: dict) -> dict:
        return {
            "protocol": 1,
            "instance_id": run["instance_id"],
            "run_id": run["run_id"],
            "state": run["state"],
            "revision": run["revision"],
            "stop_confirmed": run["stop_confirmed"],
            "effects_uncertain": run["effects_uncertain"],
            "outcome": run["outcome"],
            "result_available": run["result_available"],
        }

    def finish_run(self, key: str, *, state="succeeded") -> None:
        entry = self.submissions[key]
        run = self.runs[entry["run_id"]]
        run["state"] = state
        run["revision"] += 1
        run["stop_confirmed"] = True
        run["outcome"] = {"code": state}
        run["result_available"] = state == "succeeded"


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "coord.sqlite3")
    s.open()
    yield s
    s.close()


async def make_bot_server(bot_app: FakeBotApp) -> TestServer:
    server = TestServer(bot_app.app())
    await server.start_server()
    return server


def register_bot(store, endpoint, bot_id="research", instance=None) -> None:
    store.upsert_bot(
        Bot(
            bot_id=bot_id,
            display_name=bot_id.title(),
            role="research",
            endpoint=endpoint,
            secret_env="TEAMS_TEST_BOT_KEY",
            instance_id=instance,
        )
    )


def admit(store, *, key=None, bot_id="research", workflow="wf-1"):
    return store.admit_task(
        caller_principal="planner",
        project_id="proj",
        workflow_id=workflow,
        bot_id=bot_id,
        operation_key=key or str(uuid.uuid4()),
        input_text="please research",
        deadline_at=utc_now() + timedelta(seconds=300),
        delivery_mode=DeliveryMode.WORKBENCH_ONLY,
        bot=store.get_bot(bot_id),
    )


def factory() -> ClientFactory:
    return ClientFactory({"TEAMS_TEST_BOT_KEY": SECRET})


async def pump_until(worker, predicate, timeout=8.0, interval=0.05):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await worker.pump_once()
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_dispatch_accepts_and_observes_terminal_result(store):
    store.create_project("proj", "Main")
    bot_app = FakeBotApp()
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")))
        task, _ = admit(store)
        worker = DispatchWorker(store, factory())
        assert await pump_until(
            worker, lambda: store.get_task(task.task_id).dispatch_state.value == "accepted"
        )
        bot_app.finish_run(task.operation_key)
        assert await pump_until(
            worker,
            lambda: store.get_task(task.task_id).run_state == "succeeded"
            and store.get_task(task.task_id).result_available,
        )
        final = store.get_task(task.task_id)
        assert final.result_text == "final answer"
        assert final.outcome_code == "succeeded"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_lost_acceptance_response_reconciles_same_run(store):
    """A03: response dropped after the remote persisted; same key reconciles, no re-run."""
    store.create_project("proj", "Main")
    bot_app = FakeBotApp(drop_first_submit=True)
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")))
        task, _ = admit(store)
        worker = DispatchWorker(store, factory())
        assert await pump_until(
            worker, lambda: store.get_task(task.task_id).dispatch_state.value == "accepted"
        ), store.get_task(task.task_id).last_error
        final = store.get_task(task.task_id)
        assert final.remote_run_id in bot_app.runs
        assert len(bot_app.runs) == 1, "no duplicate remote run may be created"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_identity_change_goes_indeterminate_and_quarantines(store):
    """A13: the endpoint now answers with a different instance id."""
    store.create_project("proj", "Main")
    bot_app = FakeBotApp(instance_id="inst_original")
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")), instance="inst_original")
        task, _ = admit(store)
        worker = DispatchWorker(store, factory())
        assert await pump_until(
            worker, lambda: store.get_task(task.task_id).dispatch_state.value == "accepted"
        )
        # The bot restarts with fresh storage: new identity, run rows gone.
        bot_app.instance_id = "inst_reborn"
        bot_app.submissions.clear()
        bot_app.runs.clear()
        assert await pump_until(
            worker, lambda: store.get_task(task.task_id).dispatch_state.value == "indeterminate"
        )
        assert "inst_reborn" in store.active_quarantines()
        # A new task to the same endpoint is never executed on the reborn
        # instance: the pre-submit identity check sends it indeterminate too.
        submits_before = bot_app.submit_count
        second, _ = admit(store, key=str(uuid.uuid4()))
        assert await pump_until(
            worker,
            lambda: store.get_task(second.task_id).dispatch_state.value == "indeterminate",
        )
        assert bot_app.submit_count == submits_before, "no run may be created on a changed instance"
        # After the operator re-binds the bot explicitly, dispatch works again.
        store.resolve_quarantine("inst_reborn", "operator", "storage rebuilt deliberately")
        store.force_rebind_instance(
            "research", "inst_reborn", actor="operator", note="storage rebuilt deliberately"
        )
        third, _ = admit(store, key=str(uuid.uuid4()))
        assert await pump_until(
            worker, lambda: store.get_task(third.task_id).dispatch_state.value == "accepted"
        )
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_unreachable_bot_does_not_block_the_other(store):
    """A07: one bot down; the other bot's task proceeds."""
    store.create_project("proj", "Main")
    bot_app = FakeBotApp()
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")), bot_id="writer")
        # A dead endpoint on a loopback port with no listener.
        register_bot(store, "http://127.0.0.1:1/", bot_id="research")
        dead_task, _ = admit(store, bot_id="research")
        live_task, _ = admit(store, bot_id="writer")
        worker = DispatchWorker(store, factory())
        assert await pump_until(
            worker, lambda: store.get_task(live_task.task_id).dispatch_state.value == "accepted"
        )
        assert store.get_task(dead_task.task_id).dispatch_state.value == "pending"
        assert store.get_task(live_task.task_id).dispatch_state.value == "accepted"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_cancel_request_drives_remote_cancel(store):
    store.create_project("proj", "Main")
    bot_app = FakeBotApp()
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")))
        task, _ = admit(store)
        worker = DispatchWorker(store, factory())
        assert await pump_until(
            worker, lambda: store.get_task(task.task_id).dispatch_state.value == "accepted"
        )
        store.request_cancel(task.task_id)
        assert await pump_until(worker, lambda: len(bot_app.cancel_calls) > 0)
        assert bot_app.cancel_calls == [store.get_task(task.task_id).remote_run_id]
    finally:
        await server.close()


class GatedBotApp(FakeBotApp):
    """Holds the submit handler open so a cancel can race the in-flight POST."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.submit_entered = asyncio.Event()
        self.release_submit = asyncio.Event()

    async def submit(self, request: web.Request) -> web.Response:
        key = request.headers.get("Idempotency-Key", "")
        body = await request.json()
        self.submit_entered.set()
        await self.release_submit.wait()
        self.submit_count += 1
        run_id = self._persist(key, body)
        return web.json_response(self._status(self.runs[run_id]), status=202)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_cancel_while_submit_in_flight_stays_unconfirmed_and_stops_run(store):
    """N01: a cancel during the first POST must not confirm a phantom stop."""
    store.create_project("proj", "Main")
    bot_app = GatedBotApp()
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")).rstrip("/"), instance="inst_bot_a")
        task, _ = admit(store)
        worker = DispatchWorker(store, factory())
        pump = asyncio.create_task(worker.pump_once())
        await asyncio.wait_for(bot_app.submit_entered.wait(), 3)
        in_flight = store.dispatch_entry_for_task(task.task_id)
        assert in_flight is not None and in_flight.submit_started_at is not None
        cancel = store.request_cancel(task.task_id)
        assert cancel is not None
        assert cancel.cancel_state.value == "unconfirmed"
        assert cancel.stop_confirmed is False
        bot_app.release_submit.set()
        await asyncio.wait_for(pump, 5)
        current = store.get_task(task.task_id)
        assert current.dispatch_state.value == "accepted"
        assert current.remote_run_id is not None
        assert current.remote_run_id in bot_app.cancel_calls, (
            "an accepted run must be stopped in the same pump"
        )
        assert len(store.tasks_to_observe()) == 1
    finally:
        await server.close()


class QuarantiningCapsApp(FakeBotApp):
    """Quarantines the bot instance while the capabilities preflight is awaited."""

    def __init__(self, store, **kwargs):
        super().__init__(**kwargs)
        self._store = store

    async def capabilities(self, request: web.Request) -> web.Response:
        response = await super().capabilities(request)
        self._store.quarantine_instance(self.instance_id, "incident during preflight")
        return response


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_quarantine_during_preflight_defers_before_any_send(store):
    """N01: isolation re-checked after awaited preflight stops the submit."""
    store.create_project("proj", "Main")
    bot_app = QuarantiningCapsApp(store)
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")).rstrip("/"), instance="inst_bot_a")
        task, _ = admit(store)
        worker = DispatchWorker(store, factory())
        await worker.pump_once()
        current = store.get_task(task.task_id)
        assert current.dispatch_state.value == "pending"
        entry = store.dispatch_entry_for_task(task.task_id)
        assert entry is not None and entry.state == "pending"
        assert "quarantined" in entry.last_error
        assert bot_app.submit_count == 0, "the submit must not leave the coordinator"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_terminal_unconfirmed_stop_still_drives_remote_cancel(store):
    """N01/N08: a terminal state is not stop evidence; unconfirmed keeps driving."""
    store.create_project("proj", "Main")
    bot_app = FakeBotApp()
    server = await make_bot_server(bot_app)
    try:
        register_bot(store, str(server.make_url("")).rstrip("/"), instance="inst_bot_a")
        unconfirmed, _ = admit(store)
        store.update_after_submit(
            unconfirmed.task_id, remote_run_id="run_stop_unconfirmed",
            remote_instance_id="inst_bot_a",
        )
        store.request_cancel(unconfirmed.task_id)
        observed = store.record_observation(
            unconfirmed.task_id, revision=2, run_state="interrupted", stop_confirmed=False,
        )
        assert observed is not None and observed.cancel_state.value == "unconfirmed"
        worker = DispatchWorker(store, factory())
        bot = store.get_bot("research")
        assert bot is not None
        client = factory().for_bot(bot.endpoint, bot.secret_env)
        try:
            assert await worker._drive_cancel(observed, client) is True
            assert bot_app.cancel_calls == ["run_stop_unconfirmed"]
            # A confirmed terminal stop is settled and must not be driven again.
            settled, _ = admit(store, key=str(uuid.uuid4()), workflow="wf-2")
            store.update_after_submit(
                settled.task_id, remote_run_id="run_stop_confirmed",
                remote_instance_id="inst_bot_a",
            )
            store.request_cancel(settled.task_id)
            done = store.record_observation(
                settled.task_id, revision=2, run_state="cancelled", stop_confirmed=True,
            )
            assert done is not None and done.cancel_state.value == "confirmed"
            assert await worker._drive_cancel(done, client) is False
        finally:
            await client.aclose()
        assert bot_app.cancel_calls == ["run_stop_unconfirmed"]
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_pump_drives_cancel_for_terminal_unconfirmed_run(store):
    """N08: observation stops at terminal; the cancel pump must keep driving."""
    store.create_project("proj", "Main")
    bot_app = FakeBotApp()
    # The run exists remotely and is terminal without a confirmed stop; a
    # missing run here would instead exercise the lost-storage path.
    bot_app.runs["run_terminal"] = {
        "run_id": "run_terminal",
        "state": "interrupted",
        "revision": 2,
        "instance_id": "inst_bot_a",
        "stop_confirmed": False,
        "effects_uncertain": False,
        "outcome": {"code": "interrupted"},
        "result_available": False,
    }
    server = await make_bot_server(bot_app)
    try:
        register_bot(
            store, str(server.make_url("")).rstrip("/"), instance="inst_bot_a"
        )
        task, _ = admit(store)
        store.update_after_submit(
            task.task_id, remote_run_id="run_terminal", remote_instance_id="inst_bot_a"
        )
        store.request_cancel(task.task_id)
        store.record_observation(
            task.task_id, revision=2, run_state="interrupted", stop_confirmed=False
        )
        worker = DispatchWorker(store, factory())
        await worker.pump_once()
        assert bot_app.cancel_calls == ["run_terminal"], (
            "a terminal unconfirmed run still needs a remote stop"
        )
    finally:
        await server.close()
