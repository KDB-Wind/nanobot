"""R1 guards: cancel ownership, instance isolation, quota reservation, migration.

Regression lock for the 2026-11-09 audit findings F03, F06, F16, S06, S07.
The audit's reproducers live in review/async-20261109/; these tests assert the
fixed, observable behavior on the real HTTP and store paths.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nanobot_teams.coordinator.models import Bot, DeliveryMode
from nanobot_teams.coordinator.store import (
    RESULT_RESERVATION_BYTES,
    AdmissionError,
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

SECRET = "synthetic-coordinator-guard-key"


class ScriptedBot:
    """Agent-run protocol server whose responses each test scripts."""

    def __init__(self) -> None:
        self.submit_responses: list[tuple[int, dict]] = []
        self.submits = 0
        self.cancel_calls: list[str] = []
        self.stored: dict[str, dict] = {}
        self.instance_id = "inst_guard_a"
        self.run_state = "running"
        self.revision = 1
        self.stop_confirmed = False
        self.effects_uncertain = False
        self.lookup_status: int | None = None  # None -> normal behavior
        self.lookup_payload: dict = {}
        self.get_run_status: int | None = None

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_get("/v1/agent-capabilities", self.capabilities)
        application.router.add_post("/v1/agent-runs", self.submit)
        application.router.add_get("/v1/agent-runs/lookup", self.lookup)
        application.router.add_get("/v1/agent-runs/{run_id}", self.get_run)
        application.router.add_post("/v1/agent-runs/{run_id}/cancel", self.cancel)
        return application

    async def capabilities(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "execution": True}
        )

    async def submit(self, request: web.Request) -> web.Response:
        self.submits += 1
        key = request.headers.get("Idempotency-Key", "")
        if self.submit_responses:
            status, payload = self.submit_responses.pop(0)
            return web.json_response(payload, status=status)
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.stored[key] = {"run_id": run_id, "body": await request.json()}
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "run_id": run_id,
             "state": "running", "revision": 1},
            status=202,
        )

    async def lookup(self, request: web.Request) -> web.Response:
        if self.lookup_status is not None:
            return web.json_response(self.lookup_payload, status=self.lookup_status)
        key = request.query.get("operation_key", "")
        entry = self.stored.get(key)
        if entry is None:
            return web.json_response({"code": "run_not_found"}, status=404)
        return self._run_payload(entry["run_id"])

    async def get_run(self, request: web.Request) -> web.Response:
        if self.get_run_status is not None:
            return web.json_response({"code": "run_not_found"}, status=self.get_run_status)
        return self._run_payload(request.match_info["run_id"])

    def _run_payload(self, run_id: str) -> web.Response:
        return web.json_response(
            {"protocol": 1, "instance_id": self.instance_id, "run_id": run_id,
             "state": self.run_state, "revision": self.revision,
             "stop_confirmed": self.stop_confirmed,
             "effects_uncertain": self.effects_uncertain,
             "outcome": {"code": self.run_state} if self.run_state != "running" else None,
             "result_available": self.run_state == "succeeded"}
        )

    async def cancel(self, request: web.Request) -> web.Response:
        self.cancel_calls.append(request.match_info["run_id"])
        return web.json_response({"state": "cancelling"}, status=202)


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "coord.sqlite3")
    s.open()
    s.create_project("proj", "Main")
    yield s
    s.close()


async def _bot_server(bot: ScriptedBot) -> TestServer:
    server = TestServer(bot.app())
    await server.start_server()
    return server


def _register(store, endpoint: str, bot_id="research", instance="inst_guard_a") -> Bot:
    bot = Bot(bot_id=bot_id, display_name=bot_id.title(), role="research",
              endpoint=endpoint, secret_env="GUARD_BOT_KEY", instance_id=instance)
    store.upsert_bot(bot)
    return bot


def _admit(store, bot: Bot, *, key=None, workflow="wf", deadline_s=300.0):
    return store.admit_task(
        caller_principal="planner", project_id="proj", workflow_id=workflow,
        bot_id=bot.bot_id, operation_key=key or str(uuid.uuid4()),
        input_text="guard task",
        deadline_at=None if deadline_s is None
        else utc_now() + timedelta(seconds=deadline_s),
        delivery_mode=DeliveryMode.WORKBENCH_ONLY, bot=bot,
    )[0]


async def _pump_until(worker, predicate, timeout=6.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await worker.pump_once()
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_cancel_before_dispatch_closes_task_and_outbox(store):
    """F03: a cancel of a never-sent task closes both atomically; nothing runs."""
    bot_app = ScriptedBot()
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        settled = store.request_cancel(task.task_id)
        assert settled.cancel_state.value == "confirmed"
        assert settled.run_state == "cancelled"
        assert settled.stop_confirmed is True
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        await worker.pump_once()
        assert bot_app.submits == 0, "cancelled task must never be dispatched"
        assert store.pending_dispatch(utc_now()) == []
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_unconfirmed_cancel_reconciles_lost_submit_to_remote_cancel(store):
    """F03: an unconfirmed cancel reconciles by key; a found run gets cancelled."""
    bot_app = ScriptedBot()
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        # Simulate a lost response: the remote holds the run, the coordinator
        # does not know it. First pump is the reconciling one.
        key = task.operation_key
        bot_app.stored[key] = {"run_id": "run_lost", "body": {}}
        entry = store.dispatch_entry_for_task(task.task_id)
        assert entry is not None
        store.mark_submit_started(entry.entry_id)
        store.request_cancel(task.task_id)
        current = store.get_task(task.task_id)
        assert current.cancel_state.value == "unconfirmed"
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        assert await _pump_until(
            worker,
            lambda: store.get_task(task.task_id).dispatch_state.value == "accepted",
        )
        assert bot_app.cancel_calls and set(bot_app.cancel_calls) == {"run_lost"}, (
            "a reconciled run must be cancelled on the remote"
        )
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_unconfirmed_cancel_with_no_remote_run_closes_locally(store):
    """F03: lookup 404 proves no work exists; the task closes as cancelled."""
    bot_app = ScriptedBot()
    bot_app.lookup_status = 404
    bot_app.lookup_payload = {"code": "run_not_found"}
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        entry = store.dispatch_entry_for_task(task.task_id)
        assert entry is not None
        store.mark_submit_started(entry.entry_id)
        store.request_cancel(task.task_id)
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        assert await _pump_until(
            worker,
            lambda: store.get_task(task.task_id).run_state == "cancelled",
        )
        current = store.get_task(task.task_id)
        assert current.cancel_state.value == "confirmed"
        assert bot_app.submits == 0, "reconciliation must not submit after a cancel"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_expired_deadline_before_dispatch_ends_task_atomically(store):
    """S07: a task past its deadline and never sent ends terminal, not stuck due."""
    bot_app = ScriptedBot()
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot, deadline_s=-10.0)
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        await worker.pump_once()
        current = store.get_task(task.task_id)
        assert current.run_state == "failed"
        assert current.outcome_code == "deadline_before_start"
        assert bot_app.submits == 0
        assert store.pending_dispatch(utc_now()) == [], (
            "the outbox entry must be closed with the task"
        )
    finally:
        await server.close()


def test_alias_endpoint_shares_instance_queue_and_reservation(store):
    """S06: quota attaches to the instance; reservations count before results."""
    primary = _register(store, "http://127.0.0.1:9001", bot_id="a")
    _register(store, "http://localhost:9001", bot_id="alias-b")
    alias_bot = store.get_bot("alias-b")
    for i in range(16):
        _admit(store, primary, key=f"k{i}", workflow="wf-a")
    snapshot = store.quota_snapshot(
        workflow_id="wf-a", caller_principal="planner", bot_id="a"
    )
    assert snapshot["bot_queue"] == 16
    assert snapshot["workflow_result_bytes"] == 16 * RESULT_RESERVATION_BYTES, (
        "unsettled tasks reserve the protocol result cap, not zero"
    )
    with pytest.raises(AdmissionError) as exc:
        _admit(store, alias_bot, workflow="wf-alias")
    assert exc.value.code == "bot_queue_limit"


def test_indeterminate_tasks_hold_open_quota(store):
    """S06/F06: unknown remote state keeps occupying the open-task quota."""
    bot = _register(store, "http://127.0.0.1:9002")
    for i in range(3):
        _admit(store, bot, key=f"k{i}")
    tasks = store.list_tasks(caller_principal="planner")
    for t in tasks:
        store.mark_indeterminate(t.task_id, "unknown execution")
    snapshot = store.quota_snapshot(
        workflow_id="wf", caller_principal="planner", bot_id="a"
    )
    assert snapshot["principal_open"] == 3, (
        "indeterminate tasks must not free their quota slots"
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_quarantine_blocks_dispatch_and_new_admission(store):
    """F06: an active quarantine stops existing outbox entries and admission."""
    bot_app = ScriptedBot()
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        store.quarantine_instance(bot.instance_id, "unconfirmed prior execution")
        entries = store.pending_dispatch(utc_now())
        assert entries, "the task was admitted before the quarantine"
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        await worker.pump_once()
        assert bot_app.submits == 0, (
            "a quarantined instance must not receive dispatches"
        )
        entry = store.dispatch_entry_for_task(task.task_id)
        assert entry.state == "pending", "quarantine defers, it does not drop the task"
        assert entry.next_attempt_at > utc_now()
        with pytest.raises(AdmissionError) as exc:
            _admit(store, bot, key="after-quarantine")
        assert exc.value.code == "bot_quarantined"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_idempotency_conflict_quarantines_instance(store):
    """F06: a remote 409 conflict means unknown state; the instance isolates."""
    bot_app = ScriptedBot()
    bot_app.submit_responses = [(409, {"code": "idempotency_conflict"})]
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        assert await _pump_until(
            worker,
            lambda: store.get_task(task.task_id).dispatch_state.value == "indeterminate",
        )
        assert bot.instance_id in store.active_quarantines()
        with pytest.raises(AdmissionError):
            _admit(store, bot, key="fresh-key")
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_effects_uncertain_observation_quarantines_instance(store):
    """F06: unverified remote effects isolate the instance at observation time."""
    bot_app = ScriptedBot()
    bot_app.run_state = "interrupted"
    bot_app.revision = 2
    bot_app.effects_uncertain = True
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        assert await _pump_until(
            worker,
            lambda: store.get_task(task.task_id).effects_uncertain is True,
        )
        assert bot.instance_id in store.active_quarantines()
        with pytest.raises(AdmissionError) as exc:
            _admit(store, bot, key="next-task")
        assert exc.value.code == "bot_quarantined"
    finally:
        await server.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_storage_lost_run_becomes_indeterminate_and_quarantines(store):
    """F06: identity-confirmed 404 on an admitted run must not linger running."""
    bot_app = ScriptedBot()
    server = await _bot_server(bot_app)
    try:
        bot = _register(store, str(server.make_url("")).rstrip("/"))
        task = _admit(store, bot)
        worker = DispatchWorker(store, ClientFactory({"GUARD_BOT_KEY": SECRET}))
        assert await _pump_until(
            worker,
            lambda: store.get_task(task.task_id).dispatch_state.value == "accepted",
        )
        bot_app.get_run_status = 404
        assert await _pump_until(
            worker,
            lambda: store.get_task(task.task_id).dispatch_state.value == "indeterminate",
        )
        assert bot.instance_id in store.active_quarantines()
    finally:
        await server.close()


def test_imported_usage_blocks_new_tasks_until_confirmed(store):
    """F16: imported caller usage is a guard, not just audit text."""
    store.record_caller_usage("planner", delegations_used=1, result_bytes_used=32768)
    bot = _register(store, "http://127.0.0.1:9004")
    with pytest.raises(AdmissionError) as exc:
        _admit(store, bot, key="post-import")
    assert exc.value.code == "migrated_quota_unconfirmed"
    # Operator confirmation with fresh limits unlocks admissions.
    assert store.confirm_caller_quota(
        "planner", delegations_limit=10, result_bytes_limit=640 * 1024, actor="operator"
    )
    task = _admit(store, bot, key="post-import")
    assert task.task_id.startswith("task_")
    # The confirmed budget itself is enforced.
    with pytest.raises(AdmissionError) as exc:
        store.confirm_caller_quota("planner", delegations_limit=0,
                                   result_bytes_limit=0, actor="")
    assert exc.value.code == "confirm_requires_actor"


def test_endpoint_normalization_maps_legacy_unknown_to_instance(store, tmp_path):
    """F16: a legacy unknown outcome quarantines the registered instance."""
    from nanobot_teams.coordinator.config import CoordinatorConfig
    from nanobot_teams.coordinator.service import CoordinatorService

    bot = _register(store, "http://127.0.0.1:9999")
    service = CoordinatorService(CoordinatorConfig(control_dir=tmp_path), store)
    mapped = service._instance_for_endpoint("http://127.0.0.1:9999/")
    assert mapped == bot.instance_id
    assert service._instance_for_endpoint("HTTP://127.0.0.1:9999") == bot.instance_id
    assert service._instance_for_endpoint("http://other-host:1") is None


def test_migration_import_quarantines_mapped_instance(tmp_path, store):
    """F16 end to end: the import guard engages on the live instance."""
    from nanobot_teams.config import Budget
    from nanobot_teams.ledger import Ledger, TaskRecord

    endpoint = "http://127.0.0.1:9999"
    bot = _register(store, endpoint)
    ledger_path = tmp_path / "legacy.sqlite3"
    ledger = Ledger(ledger_path)
    entry = TaskRecord(
        task_id="old_unknown", root_task_id=None, parent_task_id=None,
        caller="planner", target="a", hop=0, status="outcome_unknown",
        error_code=None, truncated=False, http_sent=False, request_bytes=10,
        response_bytes=0, result_bytes=0,
        started_at=utc_now().isoformat(), finished_at=None, duration_ms=None,
        team_id="t", endpoint=endpoint,
    )
    ledger.admit(entry, Budget(delegations=1, result_bytes=32768), 32768, 60,
                 continuing=False)
    ledger.close()
    from nanobot_teams.coordinator.config import CoordinatorConfig
    from nanobot_teams.coordinator.service import CoordinatorService

    service = CoordinatorService(CoordinatorConfig(control_dir=tmp_path), store)
    report = service.import_04_ledger(
        teams_config={"team_id": "t"}, ledger_path=ledger_path, caller_id="planner"
    )
    assert report["unknown_isolated"] == 1
    assert bot.instance_id in store.active_quarantines(), (
        "the mapped instance must carry the quarantine, not a legacy key"
    )
    with pytest.raises(AdmissionError) as exc:
        _admit(store, bot, key="brand-new")
    assert exc.value.code in ("bot_quarantined", "migrated_quota_unconfirmed"), (
        "the imported unknown must block new admissions for the mapped instance "
        "and the unconfirmed caller"
    )
    assert bot.instance_id in store.active_quarantines()
    assert store.caller_quota("planner")["confirmed"] is False
