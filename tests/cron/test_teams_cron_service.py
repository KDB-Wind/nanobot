"""A24: CronService executes teams_task jobs; duplicate fires submit once."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.cron.service import CronJobSkippedError, CronService
from nanobot.cron.teams_jobs import run_teams_cron_job
from nanobot.cron.types import CronJob, CronPayload, CronSchedule


class FakeProxy:
    def __init__(self):
        self.submissions = {}
        self.enabled = True

    async def forward(self, method, path, body=None):
        body = body or {}
        if body["operation_key"] in self.submissions:
            return _Result(200, self.submissions[body["operation_key"]])
        record = {"task_id": f"task_{len(self.submissions) + 1}", **body}
        self.submissions[body["operation_key"]] = record
        return _Result(201, record)


class _Result:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload


def make_job(job_id="cron-1"):
    payload = CronPayload(
        kind="teams_task",
        message="nightly digest",
        origin_metadata={"teams_project": "ops", "teams_bot": "research"},
    )
    return CronJob(id=job_id, name="digest",
                   schedule=CronSchedule(kind="every"), payload=payload)


async def test_cron_service_runs_teams_job_and_dedupes_fire(tmp_path):
    proxy = FakeProxy()
    service = CronService(tmp_path / "cron" / "jobs.json")

    async def on_job(job):
        return await run_teams_cron_job(job, proxy)

    service.on_job = on_job
    job = make_job()
    await service._execute_job(job)
    assert job.state.last_status == "ok"
    first_run_id = job.state.run_history[-1].run_id

    # The same fire redelivered (e.g. timer raced): same job state, one task.
    job.state.last_run_at_ms = job.state.run_history[-1].run_at_ms
    await service._execute_job(job)
    assert job.state.last_status == "ok"
    assert len(proxy.submissions) == 1
    assert first_run_id != job.state.run_history[-1].run_id


async def test_teams_job_without_proxy_marks_error(tmp_path):
    service = CronService(tmp_path / "cron" / "jobs.json")

    async def on_job(job):
        return await run_teams_cron_job(job, None)

    service.on_job = on_job
    job = make_job()
    await service._execute_job(job)
    assert job.state.last_status == "error"
    assert "coordinator" in (job.state.last_error or "")
