"""Teams cron jobs: deterministic keys per fire, one task per duplicate delivery."""

from __future__ import annotations

import pytest

from nanobot.cron.service import CronJob, CronJobState
from nanobot.cron.teams_jobs import run_teams_cron_job
from nanobot.cron.types import CronPayload, CronSchedule


class FakeProxy:
    def __init__(self):
        self.submissions: dict[str, dict] = {}
        self.enabled = True

    async def forward(self, method: str, path: str, body: dict | None = None):
        assert path == "/api/teams/tasks"
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


def make_job(job_id="job-1", last_run_ms=1000):
    payload = CronPayload(
        kind="teams_task",
        message="daily research brief",
        origin_metadata={"teams_project": "ops", "teams_bot": "research"},
    )
    job = CronJob(
        id=job_id,
        name="daily brief",
        schedule=CronSchedule(kind="every"),
        payload=payload,
    )
    job.state = CronJobState(last_run_at_ms=last_run_ms)
    return job


async def test_same_fire_duplicate_delivery_submits_one_task():
    proxy = FakeProxy()
    job = make_job(last_run_ms=1000)
    first = await run_teams_cron_job(job, proxy)
    second = await run_teams_cron_job(job, proxy)
    assert len(proxy.submissions) == 1
    assert first.response == "ok"
    assert second.response == "ok"  # replay reconciles


async def test_distinct_fires_use_distinct_keys():
    proxy = FakeProxy()
    await run_teams_cron_job(make_job(last_run_ms=1000), proxy)
    await run_teams_cron_job(make_job(last_run_ms=60000), proxy)
    assert len(proxy.submissions) == 2


async def test_missing_bot_or_proxy_fails_the_run():
    job = make_job()
    job.payload.origin_metadata.pop("teams_bot")
    with pytest.raises(RuntimeError):
        await run_teams_cron_job(job, FakeProxy())
    with pytest.raises(RuntimeError):
        await run_teams_cron_job(make_job(), None)
