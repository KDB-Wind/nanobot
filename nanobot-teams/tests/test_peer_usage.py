"""P6: per-bot cache hit-rate reader and usage routes (goal §5).

The denominators are the input tokens of calls that REPORTED cache fields —
never total tokens, never request counts; missing data stays null rather than
0%. Sources are the bots' own llm_usage.sqlite3 files opened read-only.
"""

from __future__ import annotations

import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.store import CoordinatorStore
from nanobot_teams.coordinator.usage import read_bot_usage

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

ADMIN = "usage-admin"
PLANNER = "usage-planner"


def build_usage_db(path, rows, *, user_version=1):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=%d" % user_version)
    conn.executescript(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            started_at_ms INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            source TEXT NOT NULL,
            stream INTEGER NOT NULL,
            finish_reason TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reported_tokens INTEGER,
            estimated_tokens INTEGER,
            generation_ms INTEGER,
            measured_output_tokens INTEGER,
            ttft_ms INTEGER,
            timed_requests INTEGER,
            error_status_code INTEGER,
            error_kind TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO llm_calls (started_at_ms, duration_ms, provider, model,"
        " source, stream, finish_reason, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def row(started_ms, model, input_t, cache_read, *, provider="prov",
        output_t=10, cache_write=None):
    return (started_ms, 1000, provider, model, "agent", 0, "stop",
            input_t, output_t, cache_read, cache_write)


NOW = 1_800_000_000_000  # fixed epoch ms for deterministic windows
DAY = 86_400_000


@pytest.fixture
def homes(tmp_path):
    root = tmp_path / "homes"
    root.mkdir()
    return root


def test_exact_arithmetic_and_coverage(homes):
    home = homes / "research"
    home.mkdir()
    build_usage_db(home / "llm_usage.sqlite3", [
        row(NOW - DAY, "glm-5.3", 100, 60),
        row(NOW - DAY, "glm-5.3", 50, None),
    ])
    usage = read_bot_usage("research", "Research", homes, "30d", now_ms=NOW)
    assert usage.available is True
    assert usage.totals.requests == 2
    assert usage.totals.input_tokens == 150
    assert usage.totals.cache_read_tokens == 60
    assert usage.totals.observed_input_tokens == 100
    assert usage.totals.cache_read_rate == pytest.approx(0.6)
    assert usage.totals.coverage == pytest.approx(100 / 150)


def test_no_cache_reports_yield_null_not_zero(homes):
    home = homes / "writer"
    home.mkdir()
    build_usage_db(home / "llm_usage.sqlite3", [row(NOW, "m", 30, None)])
    usage = read_bot_usage("writer", "Writer", homes, "7d", now_ms=NOW)
    assert usage.available is True
    assert usage.totals.cache_read_rate is None
    # Coverage is honestly 0%: none of the input was cache-observable.
    assert usage.totals.coverage == 0.0
    assert usage.totals.observed_input_tokens == 0


def test_multi_model_split(homes):
    home = homes / "analyst"
    home.mkdir()
    build_usage_db(home / "llm_usage.sqlite3", [
        row(NOW, "glm-5.3", 100, 60),
        row(NOW, "step-5", 40, 20),
    ])
    usage = read_bot_usage("analyst", "Analyst", homes, "all", now_ms=NOW)
    by_model = {m.model: m for m in usage.models}
    assert set(by_model) == {"glm-5.3", "step-5"}
    assert by_model["glm-5.3"].cache_read_rate == pytest.approx(0.6)
    assert by_model["step-5"].cache_read_rate == pytest.approx(0.5)
    assert usage.totals.cache_read_tokens == 80
    assert usage.totals.observed_input_tokens == 140


def test_window_cutoff_is_exact(homes):
    home = homes / "cutter"
    home.mkdir()
    build_usage_db(home / "llm_usage.sqlite3", [
        row(NOW - 7 * DAY, "m", 100, 50),        # exactly at the 7d cutoff
        row(NOW - 7 * DAY - 1, "m", 1000, 900),  # one ms outside
        row(NOW, "m", 10, 5),
    ])
    usage = read_bot_usage("cutter", "Cutter", homes, "7d", now_ms=NOW)
    assert usage.totals.requests == 2
    assert usage.totals.input_tokens == 110
    every = read_bot_usage("cutter", "Cutter", homes, "all", now_ms=NOW)
    assert every.totals.requests == 3


def test_missing_database_is_unavailable(homes):
    usage = read_bot_usage("ghost", "Ghost", homes, "30d", now_ms=NOW)
    assert usage.available is False
    assert "missing" in usage.reason


def test_unknown_schema_version_is_unavailable(homes):
    home = homes / "futured"
    home.mkdir()
    build_usage_db(home / "llm_usage.sqlite3", [row(NOW, "m", 1, 1)],
                   user_version=7)
    usage = read_bot_usage("futured", "Future", homes, "30d", now_ms=NOW)
    assert usage.available is False
    assert "schema" in usage.reason


@pytest.fixture
async def usage_lab(tmp_path, monkeypatch):
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    homes = tmp_path / "homes"
    homes.mkdir()
    home = homes / "research"
    home.mkdir()
    build_usage_db(home / "llm_usage.sqlite3", [
        row(NOW - DAY, "glm-5.3", 100, 60),
        row(NOW - DAY, "glm-5.3", 50, None),
    ])
    store = CoordinatorStore(tmp_path / "coord.sqlite3")
    store.open()
    store.upsert_bot(Bot(
        bot_id="research", display_name="Research", role="research",
        endpoint="http://127.0.0.1:9401", secret_env="E", enabled=True,
    ))
    store.upsert_bot(Bot(
        bot_id="writer", display_name="Writer", role="writer",
        endpoint="http://127.0.0.1:9402", secret_env="E2", enabled=True,
    ))
    config = CoordinatorConfig(
        control_dir=tmp_path / "control",
        bot_homes_root=homes,
        planner=PlannerBinding(
            inbox_url="http://127.0.0.1:59998", instance_id="i",
            principal_id="planner",
        ),
    )
    app = build_app(store, config, {"operator": ADMIN, "planner": PLANNER})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, homes
    finally:
        await client.close()
        store.close()


async def test_usage_route_lists_all_bots(usage_lab):
    client, homes = usage_lab
    resp = await client.get(
        "/v1/teams/usage?window=30d",
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status == 200
    payload = await resp.json()
    by_bot = {b["bot_id"]: b for b in payload["bots"]}
    assert set(by_bot) == {"research", "writer"}
    research = by_bot["research"]
    assert research["available"] is True
    assert research["totals"]["cache_read_rate"] == pytest.approx(0.6)
    assert research["totals"]["coverage"] == pytest.approx(100 / 150)
    assert research["window"] == "30d"
    writer = by_bot["writer"]
    assert writer["available"] is False
    assert writer["reason"]
    raw = str(payload)
    assert "apiKey" not in raw and "Bearer" not in raw


async def test_single_bot_usage_route(usage_lab):
    client, homes = usage_lab
    resp = await client.get(
        "/v1/teams/bots/research/usage?window=7d",
        headers={"Authorization": f"Bearer {PLANNER}"},
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["bot_id"] == "research"
    assert payload["available"] is True
    assert payload["totals"]["requests"] == 2


async def test_usage_route_rejects_bad_window(usage_lab):
    client, homes = usage_lab
    resp = await client.get(
        "/v1/teams/usage?window=3y",
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status == 400


async def test_usage_route_denies_bots(usage_lab, monkeypatch):
    client, homes = usage_lab
    resp = await client.get(
        "/v1/teams/usage",
        headers={"Authorization": "Bearer not-a-known-token"},
    )
    assert resp.status == 401
