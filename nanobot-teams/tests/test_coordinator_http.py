"""Coordinator HTTP API: role auth, admission, catalog writes, snapshot, SSE."""

from __future__ import annotations

import json
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.store import CoordinatorStore

try:
    import aiohttp  # noqa: F401

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

ADMIN = "admin-secret-token"
PLANNER = "planner-secret-token"


def make_config(tmp_path) -> CoordinatorConfig:
    return CoordinatorConfig(
        control_dir=tmp_path / "control",
        planner=PlannerBinding(
            inbox_url="http://127.0.0.1:8765", instance_id="planner-inst", principal_id="planner"
        ),
    )


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "coord.sqlite3")
    s.open()
    yield s
    s.close()


@pytest.fixture
async def client(tmp_path, store):
    app = build_app(store, make_config(tmp_path), {"operator": ADMIN, "planner": PLANNER})
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client, store
    await test_client.close()


def planner_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {PLANNER}"}


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN}"}


async def seed_catalog(client):
    test_client, store = client
    await test_client.post(
        "/v1/teams/projects",
        headers=admin_headers(),
        json={"project_id": "proj", "display_name": "Main"},
    )
    store.upsert_bot(
        __import__("nanobot_teams.coordinator.models", fromlist=["Bot"]).Bot(
            bot_id="research",
            display_name="Research",
            role="research",
            endpoint="http://127.0.0.1:8921",
            secret_env="TEAMS_BOT_KEY",
        )
    )


def task_body(**overrides) -> dict:
    body = {
        "project": "proj",
        "bot": "research",
        "input": "go research",
        "operation_key": str(uuid.uuid4()),
        "workflow": "wf-1",
    }
    body.update(overrides)
    return body


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_role_authentication(client):
    test_client, _ = client
    no_token = await test_client.get("/v1/teams/capabilities")
    assert no_token.status == 401
    wrong = await test_client.get(
        "/v1/teams/capabilities", headers={"Authorization": "Bearer nope"}
    )
    assert wrong.status == 401
    ok = await test_client.get("/v1/teams/capabilities", headers=planner_headers())
    assert ok.status == 200
    assert (await ok.json())["protocol"] == 1


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_planner_cannot_write_catalog_but_operator_can(client):
    test_client, _ = client
    denied = await test_client.post(
        "/v1/teams/projects", headers=planner_headers(), json={"project_id": "p2"}
    )
    assert denied.status == 403
    created = await test_client.post(
        "/v1/teams/projects", headers=admin_headers(), json={"project_id": "p2"}
    )
    assert created.status == 201
    bots_denied = await test_client.post(
        "/v1/teams/bots",
        headers=planner_headers(),
        json={"bot_id": "x", "endpoint": "http://127.0.0.1:1", "secret_env": "E"},
    )
    assert bots_denied.status == 403


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_bot_registration_rejects_raw_secrets_and_non_loopback(client):
    test_client, _ = client
    raw = await test_client.post(
        "/v1/teams/bots",
        headers=admin_headers(),
        json={
            "bot_id": "x",
            "endpoint": "http://127.0.0.1:8921",
            "secret_env": "E",
            "api_key": "super-secret",
        },
    )
    assert raw.status == 400
    assert (await raw.json())["code"] == "secret_rejected"
    remote = await test_client.post(
        "/v1/teams/bots",
        headers=admin_headers(),
        json={"bot_id": "x", "endpoint": "http://10.0.0.5:8921", "secret_env": "E"},
    )
    assert remote.status == 400
    good = await test_client.post(
        "/v1/teams/bots",
        headers=admin_headers(),
        json={"bot_id": "x", "endpoint": "http://127.0.0.1:8921", "secret_env": "E"},
    )
    assert good.status == 201


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_task_admission_idempotent_and_conflict(client):
    await seed_catalog(client)
    test_client, _ = client
    body = task_body()
    first = await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=body)
    assert first.status == 201
    task = await first.json()
    assert task["dispatch_state"] == "pending"
    replay = await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=body)
    assert replay.status == 200
    assert (await replay.json())["task_id"] == task["task_id"]
    conflict = await test_client.post(
        "/v1/teams/tasks", headers=planner_headers(), json=task_body(
            operation_key=body["operation_key"], input="different"
        )
    )
    assert conflict.status == 409


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_auto_resume_requires_capability_until_p4(client):
    await seed_catalog(client)
    test_client, _ = client
    response = await test_client.post(
        "/v1/teams/tasks",
        headers=planner_headers(),
        json=task_body(delivery_mode="auto_resume"),
    )
    assert response.status == 422
    assert (await response.json())["code"] == "origin_required"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_lookup_static_route_takes_precedence(client):
    await seed_catalog(client)
    test_client, _ = client
    body = task_body()
    created = await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=body)
    task_id = (await created.json())["task_id"]
    found = await test_client.get(
        f"/v1/teams/tasks/lookup?operation_key={body['operation_key']}",
        headers=planner_headers(),
    )
    assert found.status == 200
    assert (await found.json())["task_id"] == task_id
    missing = await test_client.get(
        f"/v1/teams/tasks/lookup?operation_key={uuid.uuid4()}",
        headers=planner_headers(),
    )
    assert missing.status == 404


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_cancel_and_states_flow_through_http(client):
    await seed_catalog(client)
    test_client, _ = client
    created = await test_client.post(
        "/v1/teams/tasks", headers=planner_headers(), json=task_body()
    )
    task_id = (await created.json())["task_id"]
    cancel = await test_client.post(
        f"/v1/teams/tasks/{task_id}/cancel", headers=planner_headers()
    )
    assert cancel.status == 202
    payload = await cancel.json()
    assert payload["cancel_state"] in ("requested", "confirmed")
    fetched = await test_client.get(f"/v1/teams/tasks/{task_id}", headers=planner_headers())
    assert fetched.status == 200
    forbidden = await test_client.get("/v1/teams/tasks/task_nope", headers=planner_headers())
    assert forbidden.status == 404


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_snapshot_lists_catalog_without_secrets(client):
    await seed_catalog(client)
    test_client, _ = client
    await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=task_body())
    snapshot = await test_client.get("/v1/teams/snapshot", headers=planner_headers())
    assert snapshot.status == 200
    payload = await snapshot.json()
    assert payload["projects"] and payload["bots"] and payload["tasks"]
    assert "secret_env" not in payload["bots"][0]
    text = json.dumps(payload)
    assert "Bearer" not in text and "secret" not in text.lower().replace("secret_env", "")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_events_stream_emits_task_lifecycle(client):
    await seed_catalog(client)
    test_client, _ = client
    created = await test_client.post(
        "/v1/teams/tasks", headers=planner_headers(), json=task_body()
    )
    task_id = (await created.json())["task_id"]
    resp = await test_client.get("/v1/teams/events?after=0", headers=planner_headers())
    assert resp.status == 200
    collected = b""
    seen = False
    while len(collected) < 65536:
        chunk = await resp.content.readany()
        if not chunk:
            break
        collected += chunk
        text = collected.decode("utf-8", errors="ignore")
        if "task_created" in text and task_id in text:
            seen = True
            break
    assert seen, collected.decode("utf-8", errors="ignore")
    resp.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_scaffold_creates_home_and_returns_manual_command(tmp_path, store):
    from nanobot_teams.coordinator.config import CoordinatorConfig

    config = CoordinatorConfig(
        control_dir=tmp_path / "control", bot_homes_root=tmp_path / "homes"
    )
    app = build_app(store, config, {"operator": ADMIN, "planner": PLANNER})
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        created = await test_client.post(
            "/v1/teams/bots/scaffold",
            headers=admin_headers(),
            json={"bot_id": "writer", "role": "writing"},
        )
        assert created.status == 201, await created.text()
        payload = await created.json()
        assert payload["started"] is False
        assert payload["start_command"].startswith("nanobot serve --config")
        assert payload["api_key"]
        home = tmp_path / "homes" / "writer"
        assert (home / "nanobot.toml").exists()
        assert (home / "workspace").is_dir()
        assert store.get_bot("writer") is not None
        # The same home is never overwritten.
        repeat = await test_client.post(
            "/v1/teams/bots/scaffold",
            headers=admin_headers(),
            json={"bot_id": "writer"},
        )
        assert repeat.status == 409
        escape = await test_client.post(
            "/v1/teams/bots/scaffold",
            headers=admin_headers(),
            json={"bot_id": "../evil"},
        )
        assert escape.status == 400
        # Planner cannot scaffold.
        denied = await test_client.post(
            "/v1/teams/bots/scaffold",
            headers=planner_headers(),
            json={"bot_id": "other"},
        )
        assert denied.status == 403
    finally:
        await test_client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_bot_with_open_tasks_rejects_edits(client):
    await seed_catalog(client)
    test_client, store = client
    await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=task_body())
    edit = await test_client.post(
        "/v1/teams/bots",
        headers=admin_headers(),
        json={
            "bot_id": "research",
            "endpoint": "http://127.0.0.1:9999",
            "secret_env": "TEAMS_BOT_KEY",
        },
    )
    assert edit.status == 409
    assert (await edit.json())["code"] == "bot_has_open_tasks"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_q1b_ssrf_endpoint_variants_rejected(client):
    test_client, _ = client
    for endpoint in (
        "http://127.0.0.1.evil.com",
        "http://127.0.0.1@attacker.host",
        "http://localhost:80@evil",
        "https://127.0.0.1",
    ):
        response = await test_client.post(
            "/v1/teams/bots",
            headers=admin_headers(),
            json={"bot_id": "s", "endpoint": endpoint, "secret_env": "E"},
        )
        assert response.status == 400, endpoint


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_q1b_replay_wins_over_capability_validation(client):
    await seed_catalog(client)
    test_client, store = client
    body = task_body()
    first = await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=body)
    assert first.status == 201
    # A workbench_only retry never touches the planner anyway; assert the
    # replay path returns 200 with the original task id.
    replay = await test_client.post("/v1/teams/tasks", headers=planner_headers(), json=body)
    assert replay.status == 200
    assert (await replay.json())["task_id"] == (await first.json())["task_id"]
    different = await test_client.post(
        "/v1/teams/tasks",
        headers=planner_headers(),
        json={**body, "input": "different"},
    )
    assert different.status == 409


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_q1b_planner_snapshot_is_scoped_and_operator_cannot_auto_resume(client):
    await seed_catalog(client)
    test_client, store = client
    # Operator-created task stays invisible to the planner snapshot.
    await test_client.post(
        "/v1/teams/tasks",
        headers=admin_headers(),
        json=task_body(operation_key=str(__import__("uuid").uuid4())),
    )
    snapshot = await test_client.get("/v1/teams/snapshot", headers=planner_headers())
    tasks = (await snapshot.json())["tasks"]
    assert all(t["caller_principal"] == "planner" for t in tasks)
    # Operator may not request auto-resume delivery.
    response = await test_client.post(
        "/v1/teams/tasks",
        headers=admin_headers(),
        json=task_body(delivery_mode="auto_resume"),
    )
    assert response.status == 403
