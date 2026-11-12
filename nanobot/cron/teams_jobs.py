"""Deterministic teams-task cron jobs: stable trigger id, stable submit key."""

from __future__ import annotations

import uuid
from typing import Any

from .types import CronInvocation, CronJob, CronRunResult


async def run_teams_cron_job(
    job: CronJob, teams_proxy: Any | None, *, invocation: CronInvocation
) -> CronRunResult:
    """Submit one coordinator task per cron fire; replays reuse the same key.

    The scheduler supplies a stable identity for a scheduled slot and a new
    identity for each manual request. Cron-originated tasks are workbench_only:
    the cron path has no interactive origin session to auto-resume.
    """
    run_id = f"cronrun_{uuid.uuid4().hex[:12]}"
    payload = job.payload
    details: dict[str, Any] = {"kind": "teams_task", "run_id": run_id}
    if teams_proxy is None or not getattr(teams_proxy, "enabled", False):
        raise RuntimeError("teams cron job requires a configured coordinator")
    operation_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"teams-cron:{invocation.trigger_id}"))
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


__all__ = ["run_teams_cron_job"]
