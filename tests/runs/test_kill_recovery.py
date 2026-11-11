"""Real hard-kill recovery: kill a live child process at committed states (A02)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore
from nanobot.runs.types import ExecutionResult, OutcomeCode, RunState

CHILD = Path(__file__).parent / "kill_child.py"


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, run) -> ExecutionResult:
        self.calls.append(run.run_id)
        return ExecutionResult(code=OutcomeCode.SUCCEEDED, text="recovered")


def _wait_for(path: Path, timeout: float = 20.0) -> str:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    pytest.fail(f"marker {path.name} never appeared")


def _kill_child(proc: subprocess.Popen) -> None:
    proc.kill()  # TerminateProcess on Windows: no cleanup handlers run.
    proc.wait(timeout=10)


@pytest.mark.parametrize(
    "mode",
    ["running", "preresult", "queued"],
)
async def test_hard_kill_recovery(tmp_path, mode):
    db_path = tmp_path / "runs.sqlite3"
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            str(CHILD),
            str(db_path),
            str(marker_dir),
            mode,
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        victim_run_id = _wait_for(marker_dir / "child-ready")
        if mode == "running":
            _wait_for(marker_dir / f"exec-entered-{victim_run_id}")
        elif mode == "preresult":
            _wait_for(marker_dir / f"preresult-{victim_run_id}")
        else:
            entered = list(marker_dir.glob("exec-entered-*"))
            assert entered, "hold run must have entered the executor before the kill"
        _kill_child(child)
    finally:
        if child.poll() is None:  # pragma: no cover - safety net
            child.kill()

    # A new process owns the same store and runs startup recovery. The bounded
    # wait absorbs the moment Windows takes to release the killed process's
    # byte-range lock (the same semantics serve uses).
    store = RunStore(db_path)
    store.open(lock_wait_s=60.0)
    executor = RecordingExecutor()
    service = RunService(store, executor)
    interrupted = service._store.recover()
    recovered_ids = [run.run_id for run in interrupted]

    if mode in ("running", "preresult"):
        victim = store.get(victim_run_id)
        assert victim is not None
        assert victim.state is RunState.INTERRUPTED
        assert victim.effects_uncertain is True
        assert victim.outcome is not None
        assert victim.outcome.code is OutcomeCode.INTERRUPTED
        assert victim_run_id in recovered_ids
        # The crashed run is never handed to an executor again.
        await service.start()
        assert victim_run_id not in executor.calls
    else:
        # queued: the queued run was never started. Recovery interrupts the
        # hold run that was active at kill time and isolates the instance, so
        # the queued run must wait for an operator, not resume by itself.
        victim = store.get(victim_run_id)
        assert victim is not None
        assert victim.state is RunState.QUEUED or victim.state is RunState.SUCCEEDED
        await service.start()
        isolated, reason = store.isolation()
        assert isolated, "recovery must isolate after interrupting active runs"
        deadline_hit = 0.0
        while victim_run_id not in executor.calls and deadline_hit < 1.0:
            await _sleep()
            deadline_hit += 0.05
        assert victim_run_id not in executor.calls, (
            "queued work must not start while the instance is isolated"
        )
        # The hold run that was executing at kill time is interrupted, not re-run.
        hold_rows = [r for r in store.list_state(RunState.INTERRUPTED, limit=10)]
        assert hold_rows, "the hold run active at kill time must be interrupted"
        store.clear_isolation(actor="operator", reason="effects reviewed")
        deadline_hit = 0.0
        while victim_run_id not in executor.calls and deadline_hit < 10.0:
            await _sleep()
            deadline_hit += 0.05
        assert victim_run_id in executor.calls, (
            "queued run resumes after the operator clears isolation"
        )
    await service.aclose()
    store.close()


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.05)
