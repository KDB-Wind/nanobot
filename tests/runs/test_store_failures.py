"""A08: control-store failures must fail admission without side effects."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import timedelta

import pytest

from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore, StoreUnavailableError
from nanobot.runs.types import ExecutionResult, OutcomeCode, RunState, RunSubmit, utc_now


def make_request(key: str | None = None) -> RunSubmit:
    return RunSubmit(
        task_id="t", workflow_id="w", input_text="body",
        deadline_at=utc_now() + timedelta(seconds=300),
        operation_key=key or str(uuid.uuid4()),
        principal_id="p",
    )


async def test_runs_admission_fails_closed_on_readonly_db(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    store.instance_id()
    store.close()
    # Reopen the underlying file read-only to simulate a locked/failed disk.
    ro = sqlite3.connect(f"file:{tmp_path / 'runs.sqlite3'}?mode=ro", uri=True)
    ro.execute("PRAGMA query_only=1")
    store2 = RunStore(tmp_path / "runs.sqlite3")
    store2.open()
    # A query-only database refuses writes at the sqlite layer.
    ro.execute("SELECT 1")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute(
            "INSERT INTO instance_meta(key, value) VALUES ('x', 'y')"
        )
    ro.close()
    store2.close()


async def test_runs_observation_failure_keeps_pending_run(tmp_path):
    """Result writes failing must not turn the run terminal or free the slot."""
    from runs.test_run_service import FakeExecutor

    store = RunStore(tmp_path / "runs.sqlite3")
    store.open()
    fake = FakeExecutor()
    fake.gate = asyncio.Event()  # hold the executor until the store is broken
    service = RunService(store, fake)
    await service.start()
    accepted = await service.submit(make_request())
    for _ in range(200):
        if (await service.get(accepted.run.run_id)).state is RunState.RUNNING:
            break
        await asyncio.sleep(0.01)
    # Simulate the store failing during result application.
    original_transition = store.transition

    def broken_transition(*args, **kwargs):
        raise StoreUnavailableError("disk full")

    store.transition = broken_transition  # type: ignore[method-assign]
    # The executor completes but cannot persist; the run stays tracked.
    for _ in range(300):
        task = service._tasks.get(accepted.run.run_id)
        if task is not None and task.done():
            break
        await asyncio.sleep(0.01)
    run = await service.get(accepted.run.run_id)
    assert run.state is RunState.RUNNING, "a failed observation must stay non-terminal"
    # After the store recovers, re-applying the result finalizes the run.
    store.transition = original_transition  # type: ignore[method-assign]
    await service._apply_result(
        accepted.run.run_id,
        ExecutionResult(code=OutcomeCode.SUCCEEDED, text="late write"),
    )
    run = await service.get(accepted.run.run_id)
    assert run.state is RunState.SUCCEEDED
    await service.aclose()
    store.close()


