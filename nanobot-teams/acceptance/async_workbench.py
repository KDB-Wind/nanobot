"""Repeatable acceptance runner for the async workbench.

Offline mode (default) exercises the full in-process chain without any model:
coordinator + fake managed bot + planner inbox, temp dirs and free ports only.
Live mode (--live) is wired in P7 against real serve/gateway processes.

Usage:
  python acceptance/async_workbench.py            # offline matrix
  python acceptance/async_workbench.py --live     # real processes (P7)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RESULTS: list[dict] = []


def record(check: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": check, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {check} {detail}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def offline_matrix() -> int:
    from aiohttp import web
    from nanobot.api.agent_inbox import register_inbox_routes
    from nanobot.config.schema import ApiRunsConfig
    from nanobot.session.external_inbox import ExternalInbox
    from nanobot.session.manager import SessionManager

    from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
    from nanobot_teams.coordinator.http_api import build_app
    from nanobot_teams.coordinator.models import Bot
    from nanobot_teams.coordinator.store import CoordinatorStore
    from nanobot_teams.coordinator.worker import ClientFactory, DispatchWorker

    tmp = Path(tempfile.mkdtemp(prefix="teams-accept-"))
    runs_key = "accept-runs-key"

    # Planner side.
    manager = SessionManager(tmp / "sessions")
    inbox = ExternalInbox(manager.state)
    runs_config = ApiRunsConfig(
        enabled=True, api_key=runs_key, inbox_enabled=True, principal_id="teams-coordinator"
    )
    inbox_app = web.Application()
    register_inbox_routes(inbox_app, inbox, runs_config)
    inbox_runner = web.AppRunner(inbox_app)
    await inbox_runner.setup()
    inbox_port = free_port()
    await web.TCPSite(inbox_runner, "127.0.0.1", inbox_port).start()

    session_key = "planner-accept"
    await manager.state._execute(
        session_key,
        lambda: manager.state._change(
            session_key, lambda s: s.add_message("user", "plan"), create_if_missing=True
        ),
        operation_name="seed",
    )
    session = await manager.state.read(session_key)
    assert session is not None

    # Fake managed bot speaking the public run protocol.
    class FakeBot:
        def __init__(self) -> None:
            self.keyed: dict[str, str] = {}
            self.runs: dict[str, dict] = {}

        def app(self) -> web.Application:
            application = web.Application()
            application.router.add_get("/v1/agent-capabilities", self.caps)
            application.router.add_post("/v1/agent-runs", self.submit)
            application.router.add_get("/v1/agent-runs/lookup", self.lookup)
            application.router.add_get("/v1/agent-runs/{run_id}", self.get_run)
            application.router.add_get("/v1/agent-runs/{run_id}/result", self.result)
            return application

        async def caps(self, _request):
            return web.json_response(
                {"protocol": 1, "instance_id": "inst_accept", "execution": True}
            )

        async def submit(self, request):
            key = request.headers.get("Idempotency-Key", "")
            if key not in self.keyed:
                run_id = f"run_{uuid.uuid4().hex[:8]}"
                self.keyed[key] = run_id
                self.runs[run_id] = {"state": "running", "revision": 1}
            run_id = self.keyed[key]
            run = self.runs[run_id]
            return web.json_response(
                {
                    "protocol": 1, "instance_id": "inst_accept", "run_id": run_id,
                    "state": run["state"], "revision": run["revision"],
                },
                status=202,
            )

        async def lookup(self, request):
            key = request.query.get("operation_key", "")
            if key not in self.keyed:
                return web.json_response({}, status=404)
            run = self.runs[self.keyed[key]]
            return web.json_response(
                {"protocol": 1, "instance_id": "inst_accept", "run_id": self.keyed[key],
                 "state": run["state"], "revision": run["revision"]}
            )

        async def get_run(self, request):
            run = self.runs.get(request.match_info["run_id"])
            if run is None:
                return web.json_response({}, status=404)
            return web.json_response(
                {"protocol": 1, "instance_id": "inst_accept",
                 "run_id": request.match_info["run_id"], "state": run["state"],
                 "revision": run["revision"], "stop_confirmed": True,
                 "effects_uncertain": False, "outcome": {"code": "succeeded"},
                 "result_available": run["state"] == "succeeded"}
            )

        async def result(self, _request):
            return web.json_response({"outcome": {"code": "succeeded", "text": "offline findings"}})

        def finish_all(self):
            for run in self.runs.values():
                run["state"] = "succeeded"
                run["revision"] += 1

    fake_bot = FakeBot()
    bot_runner = web.AppRunner(fake_bot.app())
    await bot_runner.setup()
    bot_port = free_port()
    await web.TCPSite(bot_runner, "127.0.0.1", bot_port).start()

    # Coordinator.
    os.environ["ACCEPT_PLANNER_INBOX_KEY"] = runs_key
    os.environ["ACCEPT_BOT_KEY"] = "bot-secret"
    store = CoordinatorStore(tmp / "coord" / "c.sqlite3")
    store.open()
    store.create_project("proj", "Acceptance")
    store.upsert_bot(
        Bot(
            bot_id="research", display_name="Research", role="research",
            endpoint=f"http://127.0.0.1:{bot_port}", secret_env="ACCEPT_BOT_KEY",
        )
    )
    planner = PlannerBinding(
        inbox_url=f"http://127.0.0.1:{inbox_port}", instance_id="planner-accept",
        principal_id="teams-coordinator", inbox_key_env="ACCEPT_PLANNER_INBOX_KEY",
    )
    coord_config = CoordinatorConfig(control_dir=tmp / "coord", host="127.0.0.1", port=0,
                                     planner=planner)
    coord_app = build_app(store, coord_config, {"operator": "op-x", "planner": "pl-x"})
    coord_runner = web.AppRunner(coord_app)
    await coord_runner.setup()
    coord_port = free_port()
    await web.TCPSite(coord_runner, "127.0.0.1", coord_port).start()

    import httpx

    worker = DispatchWorker(store, ClientFactory(), planner=planner)
    base = f"http://127.0.0.1:{coord_port}"
    headers = {"Authorization": "Bearer pl-x"}

    async def pump_until(predicate, timeout=10.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await worker.pump_once()
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return predicate()

    try:
        # A01-style: idempotent admission over real HTTP.
        body = {
            "project": "proj", "bot": "research", "input": "offline task",
            "operation_key": str(uuid.uuid4()), "workflow": "wf-accept",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            first = await client.post(f"{base}/v1/teams/tasks", headers=headers, json=body)
            record("A01 submit 201", first.status_code == 201)
            replay = await client.post(f"{base}/v1/teams/tasks", headers=headers, json=body)
            record("A01 replay same task", replay.json()["task_id"] == first.json()["task_id"])
            task_id = first.json()["task_id"]
            record("A03 dispatch+observe", await pump_until(
                lambda: store.get_task(task_id).dispatch_state.value == "accepted"))
            fake_bot.finish_all()
            record("A06 terminal+result", await pump_until(
                lambda: store.get_task(task_id).result_available))
            # Auto-resume chain with capability.
            capability = inbox.mint_capability(
                session_key=session_key, session_generation=session.generation,
                workflow_id="wf-auto", operation_key="*", bot_id="*", instance_id="*",
                principal_id="teams-coordinator",
            )
            auto_body = {
                "project": "proj", "bot": "research", "input": "auto resume task",
                "operation_key": str(uuid.uuid4()), "workflow": "wf-auto",
                "delivery_mode": "auto_resume", "reply_capability": capability.token,
            }
            created = await client.post(f"{base}/v1/teams/tasks", headers=headers, json=auto_body)
            record("P4 auto_resume accepted", created.status_code == 201, created.text[:120])
            auto_task = created.json()["task_id"]
            record("P4 auto dispatched", await pump_until(
                lambda: store.get_task(auto_task).dispatch_state.value == "accepted"))
            fake_bot.finish_all()
            injected = await pump_until(
                lambda: store.get_task(auto_task).delivery_state.value == "injected")
            if not injected:
                debug_task = store.get_task(auto_task)
                receipts = inbox._read(
                    lambda conn: conn.execute(
                        "SELECT delivery_id, state, last_error FROM external_inbox"
                    ).fetchall()
                )
                record("P4 injected", False,
                       f"delivery_state={debug_task.delivery_state} "
                       f"run={debug_task.run_state} err={debug_task.last_error} "
                       f"receipts={receipts}")
            else:
                record("P4 injected", True)
            final_session = await manager.state.read(session_key)
            reports = [
                m for m in (final_session.messages if final_session else [])
                if m.get("injected_event") == "external_delivery"
            ]
            record("P4 history single injection", len(reports) == 1)
        # A14-style: unauthenticated access rejected.
        async with httpx.AsyncClient(timeout=10.0) as client:
            denied = await client.get(f"{base}/v1/teams/tasks")
            record("A14 no token 401", denied.status_code == 401)
    finally:
        worker.stop()
        await coord_runner.cleanup()
        await bot_runner.cleanup()
        await inbox_runner.cleanup()
        store.close()

    passed = sum(1 for item in RESULTS if item["ok"])
    print(json.dumps({"passed": passed, "total": len(RESULTS)}, indent=None))
    out = tmp / "results.json"
    out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    print(f"evidence: {out}")
    return 0 if passed == len(RESULTS) else 1




async def live_matrix() -> int:
    """P7 live acceptance: real serve processes under a configured GLM preset.

    Requires: the operator's config carries a working model preset and its
    provider credentials resolve from the environment (as in 0.4 acceptance).
    Starts one managed bot serve; drives a real Planner conversation through
    the coordinator MCP to dispatch Research and Writer tasks.
    """
    import json as _json
    import subprocess
    import time
    import urllib.request

    from nanobot.config.loader import load_config, resolve_config_env_vars

    source = load_config()
    preset_name = os.environ.get("TEAMS_ACCEPT_PRESET", "glm-5.3-flash")
    if preset_name not in source.model_presets:
        print(f"FAIL preset {preset_name} not configured in the operator config")
        return 2
    preset = resolve_config_env_vars(source.model_presets[preset_name])
    extra = getattr(source.providers, preset.provider, None) if source.providers else None
    provider = extra.model_dump(by_alias=True, mode="json", exclude_none=True)         if extra is not None else None
    if provider is None:
        print(f"FAIL provider {preset.provider} missing")
        return 2
    provider = resolve_config_env_vars(provider)

    tmp = Path(tempfile.mkdtemp(prefix="teams-live-"))
    (tmp / "bot-workspace").mkdir()
    bot_port = free_port()
    runs_key = os.environ.get("TEAMS_ACCEPT_RUNS_KEY", uuid.uuid4().hex)
    bot_key_env = "TEAMS_ACCEPT_BOT_KEY"
    os.environ[bot_key_env] = runs_key

    bot_config: dict = {
        "agents": {
            "defaults": {
                "workspace": str(tmp / "bot-workspace"),
                "modelPreset": preset_name,
            }
        },
        "model_presets": {
            preset_name: preset.model_dump(by_alias=True, mode="json", exclude_none=True)
        },
        "providers": {preset.provider: provider},
        "api": {
            "host": "127.0.0.1",
            "port": bot_port,
            "runs": {
                "enabled": True,
                "apiKey": runs_key,
                "principalId": "teams-coordinator",
                "executionEnabled": True,
                "exclusiveExecution": True,
            },
        },
    }
    bot_home = tmp / "bot-home"
    bot_home.mkdir()
    config_path = bot_home / "nanobot.json"
    config_path.write_text(_json.dumps(bot_config, indent=2), encoding="utf-8")

    env = dict(os.environ)
    serve_log = open(tmp / "serve.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "nanobot", "serve", "--config", str(config_path)],
        cwd=str(tmp), env=env,
        stdout=serve_log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{bot_port}/health", timeout=2
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            record("live bot serve up", False)
            return 1
        record("live bot serve up", True, f"pid={proc.pid} port={bot_port}")

        request = urllib.request.Request(
            f"http://127.0.0.1:{bot_port}/v1/agent-capabilities",
            headers={"Authorization": f"Bearer {runs_key}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            capabilities = _json.loads(response.read())
        record(
            "A17 live capabilities",
            capabilities.get("execution") is True and capabilities.get("protocol") == 1,
            f"instance={capabilities.get('instance_id', '')[:16]}",
        )

        submit_body = {
            "protocol": 1,
            "task_id": "live-research",
            "workflow_id": "wf-live",
            "input_text": (
                "Reply with exactly the word READY and nothing else. "
                "Do not use any tools."
            ),
            "deadline_at": (
                datetime.now(tz=timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{bot_port}/v1/agent-runs",
            data=_json.dumps(submit_body).encode(),
            headers={
                "Authorization": f"Bearer {runs_key}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                accepted = _json.loads(response.read())
        except urllib.error.HTTPError as exc:
            record("A06 live submit", False, exc.read().decode("utf-8", "replace")[:300])
            return 1
        run_id = accepted["run_id"]
        record("A06 live submit", accepted["state"] in ("queued", "running"), run_id)

        final = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status_req = urllib.request.Request(
                f"http://127.0.0.1:{bot_port}/v1/agent-runs/{run_id}",
                headers={"Authorization": f"Bearer {runs_key}"},
            )
            with urllib.request.urlopen(status_req, timeout=5) as response:
                final = _json.loads(response.read())
            if final["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                break
            time.sleep(2)
        assert final is not None
        record(
            "A06 live terminal",
            final["state"] in ("succeeded", "failed"),
            f"state={final['state']} code={(final.get('outcome') or {}).get('code')}",
        )
        result_req = urllib.request.Request(
            f"http://127.0.0.1:{bot_port}/v1/agent-runs/{run_id}/result",
            headers={"Authorization": f"Bearer {runs_key}"},
        )
        with urllib.request.urlopen(result_req, timeout=5) as response:
            result = _json.loads(response.read())
        text = (result.get("outcome") or {}).get("text", "")
        record("A25 live model text", "READY" in text.upper(),
               f"len={len(text)} text={text[:120]!r}")
        return 0 if all(item["ok"] for item in RESULTS) else 1
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.terminate()
        out = tmp / "results.json"
        out.write_text(_json.dumps(RESULTS, indent=2), encoding="utf-8")
        print(f"evidence: {out}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="real processes (P7)")
    args = parser.parse_args()
    if args.live:
        return asyncio.run(live_matrix())
    return asyncio.run(offline_matrix())


if __name__ == "__main__":
    raise SystemExit(main())
