"""P3 identity: per-bot bindings on the bots table, per-bot coordinator
credentials, and default-deny for bot principals on existing routes.

Contract (PEER_COMMS_GOAL.md I1/I2/I3, PEER_COMMS_DESIGN.md §2): identity
comes from token verification, a bot receives only when its binding is
complete, and worker credentials never widen into operator/planner powers.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.store import Bot, CoordinatorStore

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

ADMIN = "identity-admin-token"
PLANNER = "identity-planner-token"
BOT_KEY = "identity-bot-coordinator-key"

LEGACY_BOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    bot_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    secret_env TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    instance_id TEXT,
    capabilities TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
"""


def make_config(tmp_path) -> CoordinatorConfig:
    return CoordinatorConfig(
        control_dir=tmp_path / "control",
        planner=PlannerBinding(
            inbox_url="http://127.0.0.1:8765",
            instance_id="planner-inst",
            principal_id="planner",
        ),
    )


def make_bot(bot_id: str = "research", **overrides) -> Bot:
    values = {
        "bot_id": bot_id,
        "display_name": "Research",
        "role": "research",
        "endpoint": "http://127.0.0.1:9101",
        "secret_env": "PEER_RESEARCH_RUNS_KEY",
        "enabled": True,
        "capabilities": ("execute",),
        "inbox_url": "http://127.0.0.1:9101",
        "inbox_principal_id": "teams-coordinator",
        "inbox_key_env": "PEER_RESEARCH_INBOX_KEY",
        "coord_secret_env": "PEER_RESEARCH_COORD_TOKEN",
    }
    values.update(overrides)
    return Bot(**values)


def test_store_upgrades_legacy_bots_table_and_is_idempotent(tmp_path):
    db_path = tmp_path / "coord.sqlite3"
    raw = sqlite3.connect(db_path)
    raw.executescript(LEGACY_BOTS_SCHEMA)
    raw.execute(
        "INSERT INTO bots (bot_id, display_name, role, endpoint, secret_env,"
        " enabled, instance_id, capabilities, created_at, updated_at)"
        " VALUES ('legacy-bot', 'Legacy', 'worker', 'http://127.0.0.1:9000',"
        " 'OLD_ENV', 1, NULL, '[]', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    store = CoordinatorStore(db_path)
    store.open()
    try:
        legacy = store.get_bot("legacy-bot")
        assert legacy is not None
        assert legacy.secret_env == "OLD_ENV"
        assert legacy.inbox_url is None
        assert legacy.coord_secret_env is None
    finally:
        store.close()

    # Reopening must not rewrite or duplicate anything.
    again = CoordinatorStore(db_path)
    again.open()
    try:
        assert len(again.list_bots()) == 1
    finally:
        again.close()


def test_store_roundtrips_binding_fields(tmp_path):
    store = CoordinatorStore(tmp_path / "coord.sqlite3")
    store.open()
    try:
        store.upsert_bot(make_bot())
        bot = store.get_bot("research")
        assert bot is not None
        assert bot.inbox_url == "http://127.0.0.1:9101"
        assert bot.inbox_principal_id == "teams-coordinator"
        assert bot.inbox_key_env == "PEER_RESEARCH_INBOX_KEY"
        assert bot.coord_secret_env == "PEER_RESEARCH_COORD_TOKEN"
    finally:
        store.close()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    if not HAS_AIOHTTP:  # pragma: no cover
        pytest.skip("aiohttp not installed")
    monkeypatch.setenv("PEER_RESEARCH_COORD_TOKEN", BOT_KEY)
    store = CoordinatorStore(tmp_path / "coord.sqlite3")
    store.open()
    app = build_app(store, make_config(tmp_path), {"operator": ADMIN, "planner": PLANNER})
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        yield test_client, store
    finally:
        await test_client.close()
        store.close()


def bot_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOT_KEY}"}


async def register_research(client):
    test_client, store = client
    store.upsert_bot(make_bot())


async def test_bot_token_authenticates_on_capabilities(client):
    await register_research(client)
    test_client, _ = client
    resp = await test_client.get("/v1/teams/capabilities", headers=bot_headers())
    assert resp.status == 200


async def test_bot_token_is_default_denied_on_operator_routes(client):
    await register_research(client)
    test_client, _ = client
    resp = await test_client.post(
        "/v1/teams/projects",
        headers=bot_headers(),
        json={"project_id": "p", "display_name": "P"},
    )
    assert resp.status == 403


async def test_bot_token_is_default_denied_on_planner_routes(client):
    await register_research(client)
    test_client, _ = client
    resp = await test_client.post(
        "/v1/teams/tasks",
        headers=bot_headers(),
        json={
            "project_id": "p",
            "bot_id": "research",
            "input": "hello",
            "operation_key": str(__import__("uuid").uuid4()),
        },
    )
    assert resp.status == 403


async def test_unknown_token_is_rejected(client):
    await register_research(client)
    test_client, _ = client
    resp = await test_client.get(
        "/v1/teams/capabilities",
        headers={"Authorization": "Bearer no-such-token"},
    )
    assert resp.status == 401


async def test_bot_coord_secret_env_missing_is_not_a_credential(client, monkeypatch):
    await register_research(client)
    test_client, _ = client
    monkeypatch.delenv("PEER_RESEARCH_COORD_TOKEN", raising=False)
    resp = await test_client.get("/v1/teams/capabilities", headers=bot_headers())
    assert resp.status == 401


async def test_upsert_accepts_inbox_binding_and_echoes_public_fields(client):
    test_client, _ = client
    resp = await test_client.post(
        "/v1/teams/bots",
        headers={"Authorization": f"Bearer {ADMIN}"},
        json={
            "bot_id": "research",
            "display_name": "Research",
            "role": "research",
            "endpoint": "http://127.0.0.1:9101",
            "secret_env": "PEER_RESEARCH_RUNS_KEY",
            "coord_secret_env": "PEER_RESEARCH_COORD_TOKEN",
            "inbox_url": "http://127.0.0.1:9101",
            "inbox_principal_id": "teams-coordinator",
            "inbox_key_env": "PEER_RESEARCH_INBOX_KEY",
        },
    )
    assert resp.status == 201
    payload = await resp.json()
    assert payload["inbox_url"] == "http://127.0.0.1:9101"
    assert payload["inbox_principal_id"] == "teams-coordinator"
    # The credential value itself never shows up anywhere in the response.
    assert BOT_KEY not in json.dumps(payload)


async def test_upsert_rejects_non_loopback_inbox_url(client):
    test_client, _ = client
    resp = await test_client.post(
        "/v1/teams/bots",
        headers={"Authorization": f"Bearer {ADMIN}"},
        json={
            "bot_id": "evil",
            "endpoint": "http://127.0.0.1:9101",
            "secret_env": "E",
            "inbox_url": "http://10.0.0.5:9000",
        },
    )
    assert resp.status == 400
