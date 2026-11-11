"""Deterministic teams-task cron jobs: stable trigger id, stable submit key."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .service import CronJob, CronRunResult


async def run_teams_cron_job(job: CronJob, teams_proxy: Any | None) -> CronRunResult:
    """Submit one coordinator task per cron fire; replays reuse the same key.

    The trigger id is derived from the job id plus the scheduled fire time, so
    a duplicated delivery of the same fire reconciles to one task instead of
    creating a second one. Cron-originated tasks are workbench_only: the cron
    path has no interactive origin session to auto-resume.
    """
    run_id = f"cronrun_{uuid.uuid4().hex[:12]}"
    payload = job.payload
    details: dict[str, Any] = {"kind": "teams_task", "run_id": run_id}
    if teams_proxy is None or not getattr(teams_proxy, "enabled", False):
        raise RuntimeError("teams cron job requires a configured coordinator")
    # The fire's identity is the slot it came due for, not the wall clock at
    # callback time. If the state save is lost the scheduler delivers the same
    # slot again, and that redelivery has to reconcile to the same task.
    slot = job.state.next_run_at_ms or job.state.last_run_at_ms or _now_ms()
    trigger_id = f"{job.id}:{slot}"
    operation_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"teams-cron:{trigger_id}"))
    body = {
        "project": payload.origin_metadata.get("teams_project", "cron"),
        "bot": payload.origin_metadata.get("teams_bot", ""),
        "input": payload.message,
        "operation_key": operation_key,
        "workflow": f"cron-{job.id}",
        "delivery_mode": "workbench_only",
    }
    if not body["bot"]:
        raise RuntimeError("teams cron job payload missing teams_bot")
    result = await teams_proxy.forward("POST", "/api/teams/tasks", body)
    details["status"] = result.status
    if result.status not in (200, 201):
        details["error"] = str(result.payload)[:200]
    return CronRunResult(run_id=run_id, response=details["status"] in (200, 201) and "ok" or "error")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


__all__ = ["run_teams_cron_job"]
