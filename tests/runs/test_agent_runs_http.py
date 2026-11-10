"""Public HTTP behavior of the agent-run protocol (A01-A06, A14, A17 slices)."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import timedelta

import pytest

from nanobot.api.server import create_app
from nanobot.config.schema import ApiRunsConfig
from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore
from nanobot.runs.types import ExecutionResult, OutcomeCode, utc_now
from tests.runs.test_run_service import FakeExecutor

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

RUNS_KEY = "dedicated-runs-secret"
PLAIN_API_KEY = "plain-api-secret"


def runs_config(**overrides) -> ApiRunsConfig:
    values = {
        "enabled": True,
        "api_key": RUNS_KEY,
        "principal_id": "coordinator-test",
        "execution_enabled": True,
    }
    values.update(overrides)
    return ApiRunsConfig(**values)


@pytest.fixture
async def client_factory(tmp_path):
    made: list[tuple[TestClient, RunService, RunStore]] = []

    async def _make(config: ApiRunsConfig | None = None, agent_loop=None, **service_kwargs):
        store = RunStore(tmp_path / f"runs-{len(made)}.sqlite3")
        store.open()
        fake = FakeExecutor()
        service = RunService(store, fake, **service_kwargs)
        await service.start()
        active = config or runs_config()
        runs_kwargs = (
            {"runs_service": service, "runs_config": active} if active.enabled else {}
        )
        app = create_app(
            agent_loop if agent_loop is not None else object(),
            api_key=PLAIN_API_KEY,
            **runs_kwargs,
        )
        server = TestServer(app)
        test_client = TestClient(server)
        await test_client.start_server()
        made.append((test_client, service, store))
        return test_client, service, fake

    yield _make
    for test_client, service, store in made:
        try:
            await test_client.close()
        finally:
            try:
                await service.aclose()
            except Exception:
                pass
            store.close()


def submit_body(text: str = "do the work", *, deadline_s: float = 600.0) -> dict:
    return {
        "protocol": 1,
        "task_id": "task-http-1",
        "workflow_id": "wf-http",
        "input_text": text,
        "deadline_at": (utc_now() + timedelta(seconds=deadline_s)).isoformat(),
    }


def runs_headers(key: str = RUNS_KEY, operation_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if operation_key:
        headers["Idempotency-Key"] = operation_key
    return headers


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_capabilities_reports_instance_identity(client_factory):
    client, service, _ = await client_factory()
    resp = await client.get("/v1/agent-capabilities", headers=runs_headers())
    assert resp.status == 200
    payload = await resp.json()
    assert payload["protocol"] == 1
    assert payload["instance_id"] == service.instance_id
    assert payload["execution"] is True
    assert payload["exclusive_execution"] is False


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_runs_routes_reject_missing_plain_and_wrong_keys(client_factory):
    client, _, _ = await client_factory()
    no_auth = await client.get("/v1/agent-capabilities")
    assert no_auth.status == 401
    body = await no_auth.json()
    assert body["code"] == "missing_bearer"
    plain_key = await client.get(
        "/v1/agent-capabilities", headers=runs_headers(key=PLAIN_API_KEY)
    )
    assert plain_key.status == 401
    assert (await plain_key.json())["code"] == "invalid_bearer"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_submit_idempotent_same_key_same_body(client_factory):
    client, _, fake = await client_factory()
    key = str(uuid.uuid4())
    body = submit_body()
    first = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=body
    )
    assert first.status == 202
    run = await first.json()
    assert run["state"] in ("queued", "running", "succeeded")
    second = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=body
    )
    assert second.status in (200, 202)
    replay = await second.json()
    assert replay["run_id"] == run["run_id"]
    for _ in range(300):
        status = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
        payload = await status.json()
        if payload["state"] == "succeeded":
            break
        await asyncio.sleep(0.02)
    assert payload["state"] == "succeeded"
    assert fake.calls == [run["run_id"]]
    changed = await client.post(
        "/v1/agent-runs",
        headers=runs_headers(operation_key=key),
        json=submit_body(text="different body"),
    )
    assert changed.status == 409
    assert (await changed.json())["code"] == "idempotency_conflict"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_submit_rejects_bad_protocol_bodies(client_factory):
    client, _, _ = await client_factory()
    missing_key = await client.post(
        "/v1/agent-runs", headers=runs_headers(), json=submit_body()
    )
    assert missing_key.status == 400
    assert (await missing_key.json())["code"] == "idempotency_key_required"
    unknown_field = await client.post(
        "/v1/agent-runs",
        headers=runs_headers(operation_key=str(uuid.uuid4())),
        json={**submit_body(), "model": "gpt"},
    )
    assert unknown_field.status == 400
    assert (await unknown_field.json())["code"] == "unknown_fields"
    oversized = await client.post(
        "/v1/agent-runs",
        headers=runs_headers(operation_key=str(uuid.uuid4())),
        json=submit_body(text="x" * (16 * 1024 + 1)),
    )
    assert oversized.status == 413
    assert (await oversized.json())["code"] == "input_too_large"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_malformed_bodies_answer_structured_4xx(client_factory):
    """S05: parser faults surface as protocol 400s, never a catching-class 500."""
    client, _, _ = await client_factory()
    malformed = await client.post(
        "/v1/agent-runs", data="{", headers=runs_headers(operation_key=str(uuid.uuid4()))
    )
    assert malformed.status == 400
    body = await malformed.json()
    assert body["code"] == "invalid_json"
    assert malformed.content_type == "application/json"
    duplicate = await client.post(
        "/v1/agent-runs",
        data='{"protocol":1,"protocol":1}',
        headers=runs_headers(operation_key=str(uuid.uuid4())),
    )
    assert duplicate.status == 400
    assert (await duplicate.json())["code"] == "duplicate_key"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_expired_cursor_answers_410_before_streaming(client_factory):
    """S09: an expired cursor is a protocol 410 before the stream prepares."""
    client, service, fake = await client_factory()
    fake.result = ExecutionResult(code=OutcomeCode.SUCCEEDED, text="done", message="")
    key = str(uuid.uuid4())
    submitted = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=submit_body()
    )
    run = await submitted.json()
    for _ in range(300):
        status = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
        payload = await status.json()
        if payload["state"] == "succeeded":
            break
        await asyncio.sleep(0.02)
    store: RunStore = service._store
    store._conn.execute(
        "UPDATE run_events SET at=?", ((utc_now() - timedelta(days=31)).isoformat(),)
    )
    store.cleanup()
    expired = await client.get("/v1/agent-runs/events?after=0", headers=runs_headers())
    assert expired.status == 410
    body = await expired.json()
    assert body["code"] == "event_cursor_expired"
    assert expired.content_type == "application/json"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_result_endpoint_lifecycle(client_factory):
    client, _, fake = await client_factory()
    fake.result = ExecutionResult(code=OutcomeCode.PROVIDER_ERROR, text="partial", message="502")
    fake.gate = asyncio.Event()  # hold execution so the 409 window is deterministic
    key = str(uuid.uuid4())
    submitted = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=submit_body()
    )
    run = await submitted.json()
    early = await client.get(f"/v1/agent-runs/{run['run_id']}/result", headers=runs_headers())
    assert early.status == 409
    assert (await early.json())["code"] == "not_finished"
    fake.gate.set()
    for _ in range(300):
        status = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
        payload = await status.json()
        if payload["state"] == "failed":
            break
        await asyncio.sleep(0.02)
    assert payload["state"] == "failed"
    assert payload["outcome"]["code"] == "provider_error"
    result = await client.get(f"/v1/agent-runs/{run['run_id']}/result", headers=runs_headers())
    assert result.status == 200
    body = await result.json()
    assert body["outcome"]["code"] == "provider_error"
    assert body["outcome"]["text"] == "partial"
    missing = await client.get("/v1/agent-runs/run_nope", headers=runs_headers())
    assert missing.status == 404


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_cleaned_result_stops_being_advertised(client_factory):
    """S10: availability checks the terminal state AND retention kept the body."""
    client, service, fake = await client_factory()
    fake.result = ExecutionResult(code=OutcomeCode.SUCCEEDED, text="kept body", message="")
    key = str(uuid.uuid4())
    submitted = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=submit_body()
    )
    run = await submitted.json()
    for _ in range(300):
        status = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
        payload = await status.json()
        if payload["state"] == "succeeded":
            break
        await asyncio.sleep(0.02)
    assert payload["state"] == "succeeded"
    assert payload["result_available"] is True
    store: RunStore = service._store
    store._conn.execute(
        "UPDATE runs SET finished_at=?", ((utc_now() - timedelta(days=15)).isoformat(),)
    )
    store.cleanup()
    after = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
    cleaned = await after.json()
    assert cleaned["result_available"] is False
    result = await client.get(f"/v1/agent-runs/{run['run_id']}/result", headers=runs_headers())
    assert result.status == 410
    assert (await result.json())["code"] == "result_cleaned"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_lookup_reconciles_without_new_runs(client_factory):
    client, service, fake = await client_factory()
    key = str(uuid.uuid4())
    submitted = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=submit_body()
    )
    run = await submitted.json()
    for _ in range(300):
        if (await service.get(run["run_id"])).terminal:
            break
        await asyncio.sleep(0.02)
    found = await client.get(
        f"/v1/agent-runs/lookup?operation_key={key}", headers=runs_headers()
    )
    assert found.status == 200
    assert (await found.json())["run_id"] == run["run_id"]
    absent = await client.get(
        f"/v1/agent-runs/lookup?operation_key={uuid.uuid4()}", headers=runs_headers()
    )
    assert absent.status == 404
    assert len(fake.calls) == 1


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_cancel_records_request_and_observes_stop(client_factory):
    client, _, fake = await client_factory()
    fake.gate = asyncio.Event()
    key = str(uuid.uuid4())
    submitted = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=key), json=submit_body()
    )
    run = await submitted.json()
    for _ in range(300):
        status = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
        payload = await status.json()
        if payload["state"] == "running":
            break
        await asyncio.sleep(0.02)
    cancel = await client.post(
        f"/v1/agent-runs/{run['run_id']}/cancel", headers=runs_headers()
    )
    assert cancel.status == 202
    payload = await cancel.json()
    assert payload["state"] in ("cancelling", "cancelled")
    for _ in range(300):
        status = await client.get(f"/v1/agent-runs/{run['run_id']}", headers=runs_headers())
        payload = await status.json()
        if payload["state"] == "cancelled":
            break
        await asyncio.sleep(0.02)
    assert payload["state"] == "cancelled"
    assert payload["stop_confirmed"] is True


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_events_stream_yields_run_lifecycle(client_factory):
    client, _, _ = await client_factory()
    submitted = await client.post(
        "/v1/agent-runs", headers=runs_headers(operation_key=str(uuid.uuid4())), json=submit_body()
    )
    run = await submitted.json()
    resp = await client.get("/v1/agent-runs/events?after=0", headers=runs_headers())
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"
    collected = b""
    run_finished = False
    while len(collected) < 64 * 1024:
        chunk = await resp.content.readany()
        if not chunk:
            break
        collected += chunk
        text = collected.decode("utf-8", errors="ignore")
        if "event: run_succeeded" in text or "run_failed" in text:
            run_finished = True
            break
    assert run_finished, collected.decode("utf-8", errors="ignore")
    assert "id: " in collected.decode("utf-8", errors="ignore")
    assert run["run_id"] in collected.decode("utf-8", errors="ignore")
    resp.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_execution_disabled_instance_rejects_submissions(client_factory):
    client, _, _ = await client_factory(config=runs_config(execution_enabled=False))
    capabilities = await client.get("/v1/agent-capabilities", headers=runs_headers())
    assert (await capabilities.json())["execution"] is False
    submitted = await client.post(
        "/v1/agent-runs",
        headers=runs_headers(operation_key=str(uuid.uuid4())),
        json=submit_body(),
    )
    assert submitted.status == 403
    assert (await submitted.json())["code"] == "execution_disabled"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_exclusive_mode_blocks_chat_endpoint(client_factory):
    client, _, _ = await client_factory(config=runs_config(exclusive_execution=True))
    chat = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLAIN_API_KEY}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert chat.status == 409
    assert (await chat.json())["error"]["type"] == "managed_execution_only"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_runs_disabled_keeps_plain_api_behavior(client_factory):
    client, _, _ = await client_factory(
        config=ApiRunsConfig(enabled=False, api_key=RUNS_KEY)
    )
    resp = await client.get("/v1/agent-capabilities", headers=runs_headers())
    assert resp.status == 404
    health = await client.get("/health")
    assert health.status == 200


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_compressed_and_duplicate_key_bodies_rejected(client_factory):
    client, _, _ = await client_factory()
    key = str(uuid.uuid4())
    compressed = await client.post(
        "/v1/agent-runs",
        headers={**runs_headers(operation_key=key), "Content-Encoding": "gzip"},
        json=submit_body(),
    )
    assert compressed.status == 400
    assert (await compressed.json())["code"] == "compressed_body_rejected"

@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp is not installed")
async def test_storage_fault_is_retryable_503_while_missing_run_stays_404(client_factory):
    """M01: a storage failure is a structured retryable 503, never 404."""
    client, service, _ = await client_factory()
    submitted = await client.post(
        "/v1/agent-runs",
        headers=runs_headers(operation_key=str(uuid.uuid4())),
        json=submit_body(),
    )
    run = await submitted.json()
    store: RunStore = service._store
    store._conn.set_authorizer(
        lambda action, *args: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT
        else sqlite3.SQLITE_OK
    )
    try:
        routes = (
            ("get", f"/v1/agent-runs/{run['run_id']}"),
            ("get", f"/v1/agent-runs/{run['run_id']}/result"),
            ("post", f"/v1/agent-runs/{run['run_id']}/cancel"),
        )
        for method, path in routes:
            call = client.get if method == "get" else client.post
            response = await call(path, headers=runs_headers())
            assert response.status == 503, (path, response.status, await response.text())
            assert response.content_type == "application/json"
            body = await response.json()
            assert body["code"] == "storage_unavailable"
            assert body["retryable"] is True
    finally:
        store._conn.set_authorizer(None)
    missing = await client.get("/v1/agent-runs/run_absent", headers=runs_headers())
    assert missing.status == 404
    assert (await missing.json())["code"] == "run_not_found"
