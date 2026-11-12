"""Child process for real hard-kill recovery tests (run via subprocess, not imported)."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nanobot.runs.service import RunService  # noqa: E402
from nanobot.runs.store import RunStore  # noqa: E402
from nanobot.runs.types import ExecutionResult, RunSubmit, utc_now  # noqa: E402


class GatedExecutor:
    """Signals milestones to the parent through marker files, then blocks forever."""

    def __init__(self, marker_dir: Path, hold_first: bool) -> None:
        self.marker_dir = marker_dir
        self.hold_first = hold_first

    async def execute(self, run) -> ExecutionResult:
        (self.marker_dir / f"exec-entered-{run.run_id}").write_text("1", encoding="utf-8")
        if self.hold_first and run.task_id == "hold":
            # Hold the single execution slot until the process is killed.
            await asyncio.Event().wait()
        (self.marker_dir / f"preresult-{run.run_id}").write_text("1", encoding="utf-8")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def req(task_id: str) -> RunSubmit:
    return RunSubmit(
        task_id=task_id,
        workflow_id="wf-kill",
        input_text=f"body for {task_id}",
        deadline_at=utc_now() + timedelta(seconds=120),
        operation_key=str(uuid.uuid5(uuid.NAMESPACE_URL, f"kill-test:{task_id}")),
        principal_id="planner-kill",
    )


async def main() -> None:
    db_path = Path(sys.argv[1])
    marker_dir = Path(sys.argv[2])
    mode = sys.argv[3]
    store = RunStore(db_path)
    store.open()
    hold_first = mode in ("running", "queued")
    service = RunService(store, GatedExecutor(marker_dir, hold_first))
    await service.start()
    if mode == "running":
        accepted = await service.submit(req("victim"))
        while not (marker_dir / f"exec-entered-{accepted.run.run_id}").exists():
            await asyncio.sleep(0.02)
    elif mode == "preresult":
        accepted = await service.submit(req("victim"))
        while not (marker_dir / f"preresult-{accepted.run.run_id}").exists():
            await asyncio.sleep(0.02)
    elif mode == "queued":
        await service.submit(req("hold"))
        while not any(marker_dir.glob("exec-entered-*")):
            await asyncio.sleep(0.02)
        accepted = await service.submit(req("queued-victim"))
    else:
        raise SystemExit(f"unknown mode {mode}")
    (marker_dir / "child-ready").write_text(accepted.run.run_id, encoding="utf-8")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
