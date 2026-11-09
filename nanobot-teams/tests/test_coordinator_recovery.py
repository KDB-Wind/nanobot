"""Coordinator resilience: real process kill/restart, MCP statelessness, 0.4 import."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.store import CoordinatorStore

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

ADMIN = "kill-test-admin-token"
PLANNER = "kill-test-planner-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_config(tmp_path: Path, port: int) -> Path:
    config_path = tmp_path / "coordinator.json"
    config_path.write_text(
        json.dumps(
            {
                "control_dir": str(tmp_path / "control"),
                "listen": {"host": "127.0.0.1", "port": port},
                "admin_token_env": "TEST_ADMIN_TOKEN",
                "planner_token_env": "TEST_PLANNER_TOKEN",
            }
        ),
        encoding="utf-8",
    )
    return config_path


def spawn_coordinator(config_path: Path, log_path: Path | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "TEST_ADMIN_TOKEN": ADMIN,
            "TEST_PLANNER_TOKEN": PLANNER,
            "TEST_BOT_KEY": "bot-run-secret",
        }
    )
    stderr = open(log_path, "w", encoding="utf-8") if log_path else subprocess.DEVNULL
    return subprocess.Popen(
        [sys.executable, "-m", "nanobot_teams", "coordinator", "--config", str(config_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


async def start_fake_bot(port: int):
    from aiohttp import web

    state: dict[str, dict] = {}

    async def capabilities(_request):
        return web.json_response({"protocol": 1, "instance_id": "inst_k", "execution": True})

    async def submit(request):
        key = request.headers.get("Idempotency-Key", "")
        body = await request.json()
        if key not in state:
            state[key] = {
                "run_id": f"run_{uuid.uuid4().hex[:10]}",
                "body": body,
                "state": "running",
                "revision": 1,
            }
        run = state[key]
        return web.json_response(
            {
                "protocol": 1,
                "instance_id": "inst_k",
                "run_id": run["run_id"],
                "state": run["state"],
                "revision": run["revision"],
            },
            status=202,
        )

    async def get_run(request):
        for run in state.values():
            if run["run_id"] == request.match_info["run_id"]:
                return web.json_response(
                    {
                        "protocol": 1,
                        "instance_id": "inst_k",
                        "run_id": run["run_id"],
                        "state": run["state"],
                        "revision": run["revision"],
                        "stop_confirmed": False,
                        "effects_uncertain": False,
                        "outcome": None,
                    }
                )
        return web.json_response({"code": "run_not_found"}, status=404)

    app = web.Application()
    app.router.add_get("/v1/agent-capabilities", capabilities)
    app.router.add_post("/v1/agent-runs", submit)
    app.router.add_get("/v1/agent-runs/{run_id}", get_run)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, state


@pytest.fixture
def seeded_store(tmp_path, monkeypatch):
    port = free_port()
    config_path = write_config(tmp_path, port)
    monkeypatch.setenv("TEST_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("TEST_PLANNER_TOKEN", PLANNER)
    from nanobot_teams.coordinator.config import load_coordinator_config

    config = load_coordinator_config(config_path)
    store = CoordinatorStore(config.control_dir / "coordinator.sqlite3")
    store.open()
    store.create_project("proj", "Main")
    store.upsert_bot(
        Bot(
            bot_id="research",
            display_name="Research",
            role="research",
            endpoint=f"http://127.0.0.1:{free_port()}",
            secret_env="TEST_BOT_KEY",
        )
    )
    store.close()
    return config_path


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_coordinator_hard_kill_recovers_pending_dispatch(tmp_path, seeded_store):
    """Kill the writer process; the committed outbox still dispatches after restart."""
    bot_port = free_port()
    coord_port = json.loads(Path(seeded_store).read_text(encoding="utf-8"))["listen"]["port"]
    # Point the bot at the (initially down) fake bot endpoint.
    control_dir = Path(seeded_store).parent / "control"
    store = CoordinatorStore(control_dir / "coordinator.sqlite3")
    store.open()
    from nanobot_teams.coordinator.models import Bot as BotModel

    store.upsert_bot(
        BotModel(
            bot_id="research", display_name="Research", role="research",
            endpoint=f"http://127.0.0.1:{bot_port}", secret_env="TEST_BOT_KEY",
        )
    )
    store.close()

    coordinator = spawn_coordinator(seeded_store)
    try:
        assert wait_port(coord_port), "coordinator did not start"
        headers = {"Authorization": f"Bearer {PLANNER}"}
        operation_key = str(uuid.uuid4())
        async with httpx.AsyncClient(timeout=10.0) as client:
            submitted = await client.post(
                f"http://127.0.0.1:{coord_port}/v1/teams/tasks",
                headers=headers,
                json={
                    "project": "proj",
                    "bot": "research",
                    "input": "survive the kill",
                    "operation_key": operation_key,
                    "workflow": "wf-kill",
                },
            )
            assert submitted.status_code == 201, submitted.text
            task_id = submitted.json()["task_id"]
            # The bot endpoint is down, so the task stays pending in the outbox.
            await asyncio_sleep(1.5)
            current = await client.get(
                f"http://127.0.0.1:{coord_port}/v1/teams/tasks/{task_id}", headers=headers
            )
            assert current.json()["dispatch_state"] == "pending"
        coordinator.kill()  # TerminateProcess: no cleanup handlers run.
        coordinator.wait(timeout=10)
    finally:
        if coordinator.poll() is None:  # pragma: no cover - safety net
            coordinator.kill()

    # Bring the bot up, restart the coordinator, and let the outbox drain.
    bot_runner, state = await start_fake_bot(bot_port)
    try:
        restart_log = tmp_path / "restart.log"
        restarted = spawn_coordinator(seeded_store, restart_log)
        try:
            assert wait_port(coord_port), "coordinator did not restart"
            headers = {"Authorization": f"Bearer {PLANNER}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                accepted = False
                for _ in range(100):
                    current = await client.get(
                        f"http://127.0.0.1:{coord_port}/v1/teams/tasks/{task_id}",
                        headers=headers,
                    )
                    if current.status_code == 200 and current.json()["dispatch_state"] == "accepted":
                        accepted = True
                        break
                    await asyncio_sleep(0.2)
                assert accepted, (
                    f"restart log: {restart_log.read_text(encoding='utf-8') if restart_log.exists() else 'none'}"
                    f" | last task: {current.text}"
                )
                assert len(state) == 1, "exactly one remote run after recovery"
                # A16 slice: a fresh MCP client sees the same task.
                from nanobot_teams.async_mcp import CoordinatorClient

                mcp_client = CoordinatorClient(
                    f"http://127.0.0.1:{coord_port}", PLANNER
                )
                try:
                    task = await mcp_client.get_task(task_id)
                    assert task["task_id"] == task_id
                    assert task["remote_run_id"] == next(iter(state.values()))["run_id"]
                finally:
                    await mcp_client.aclose()
        finally:
            if restarted.poll() is None:
                restarted.kill()
                restarted.wait(timeout=10)
    finally:
        await bot_runner.cleanup()


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def test_import_04_ledger_is_idempotent_and_preserves_unknown(tmp_path):
    """A20: usage counts and unknown isolation survive import; re-import is a no-op."""
    from nanobot_teams.coordinator.config import CoordinatorConfig
    from nanobot_teams.coordinator.service import CoordinatorService

    # Build a 0.4-style ledger through the real 0.4 Ledger class.
    from nanobot_teams.ledger import Ledger

    ledger_path = tmp_path / "caller.sqlite3"
    ledger = Ledger(ledger_path)
    ledger.bind("team-x", "planner")
    from datetime import datetime, timezone

    from nanobot_teams.ledger import TaskRecord

    base = dict(
        task_id="t1", root_task_id=None, parent_task_id=None, caller="planner",
        target="research", hop=None, status="completed", error_code=None, truncated=False,
        http_sent=True, request_bytes=10, response_bytes=20, result_bytes=30,
        started_at=datetime.now(tz=timezone.utc).isoformat(), finished_at=None,
        duration_ms=None, team_id="team-x", endpoint="http://127.0.0.1:8921",
        thread_key="", session_id="",
    )
    ledger.admit(TaskRecord(**base), None, 32768, 100.0, continuing=False)
    ledger.finish(TaskRecord(**{**base, "status": "completed"}), unknown=False)
    unknown = TaskRecord(**{**base, "task_id": "t2", "status": "outcome_unknown"})
    ledger.admit(unknown, None, 32768, 100.0, continuing=False)
    ledger.finish(unknown, unknown=True)
    ledger.close()

    config = CoordinatorConfig(control_dir=tmp_path / "control")
    service = CoordinatorService.__new__(CoordinatorService)
    service.config = config
    service.store = CoordinatorStore(tmp_path / "control" / "c.sqlite3")
    service.store.open()
    try:
        result = service.import_04_ledger(
            teams_config={"team_id": "team-x"},
            ledger_path=ledger_path,
            caller_id="planner",
            declared_budgets={"planner": {"delegations": 5, "result_bytes": 100}},
        )
        assert result["imported"] is True
        assert result["usage_delegations"] == 2
        assert result["unknown_isolated"] == 1
        quarantines = service.store.active_quarantines()
        assert any(q.startswith("legacy:") for q in quarantines)
        again = service.import_04_ledger(
            teams_config={"team_id": "team-x"},
            ledger_path=ledger_path,
            caller_id="planner",
        )
        assert again["imported"] is False
        assert again["reason"] == "already imported"
    finally:
        service.store.close()
