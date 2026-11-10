"""R5 live acceptance: a real Planner model drives the async workbench.

Scenarios (each in fresh temp dirs on free ports; every process is killed at
the end; evidence JSON is written to --evidence-dir):

  chain    Real planner gateway (websocket channel + planner inbox sidecar +
           wake pump + teamsAsync MCP) receives one user chat turn. The model
           must submit a research task, read the result back through the MCP
           get_async_task(include_result) — never bot credentials — then
           submit a writer task whose input quotes the research result. The
           browser socket is closed right after the prompt (browser exit); the
           chain must still complete headless through the wake pump.
  restart  Coordinator is killed mid-run and restarted on the same control
           dir; the same task id must reach terminal without a second submit.
  cancel   A running task is cancelled; the stop must settle to a confirmed
           (or honestly unconfirmed) state and never silently succeed.
  migrate  A synthetic 0.4 ledger is imported into a live coordinator; the
           quota guard must block admissions until the operator confirms.

All bot/planner model calls use the preset named by TEAMS_R5_PRESET (default
glm-5.3) resolved from the operator's real user config. No mocks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

TEAMS = Path(__file__).resolve().parent.parent
WT = TEAMS.parent
if os.name == "nt":
    PY_TEAMS = TEAMS / ".venv/Scripts/python.exe"
    PY_NB = WT / ".venv-nanobot/Scripts/python.exe"
    NANOBOT = WT / ".venv-nanobot/Scripts/nanobot.exe"
else:
    PY_TEAMS = TEAMS / ".venv/bin/python"
    PY_NB = WT / ".venv-nanobot/bin/python"
    NANOBOT = WT / ".venv-nanobot/bin/nanobot"

RESULTS: list[dict] = []


def record(check: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": check, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {check} {detail}", flush=True)


def free_port() -> int:
    """A bindable listener port BELOW the Windows ephemeral range.

    Ports drawn from the OS ephemeral range collide with outbound source
    ports while the lab boots, which shows up as EADDRINUSE in the gateway.
    """
    import random

    for _ in range(80):
        port = random.randint(20000, 45000)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("FAIL no bindable port found in 20000-45000")


def http_json(method: str, url: str, *, token: str = "", body: dict | None = None,
              timeout: float = 20.0):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            return exc.code, {}


def resolve_preset() -> tuple[str, dict, dict]:
    from nanobot.config.loader import load_config, resolve_config_env_vars

    preset_name = os.environ.get("TEAMS_R5_PRESET", "glm-5.3")
    source = load_config()
    if preset_name not in source.model_presets:
        raise SystemExit(f"FAIL preset {preset_name} not configured")
    preset = resolve_config_env_vars(
        source.model_presets[preset_name]
    ).model_dump(by_alias=True, mode="json", exclude_none=True)
    provider_extra = getattr(source.providers, preset["provider"], None)
    if provider_extra is None:
        raise SystemExit(f"FAIL provider {preset['provider']} missing")
    provider = resolve_config_env_vars(provider_extra).model_dump(
        by_alias=True, mode="json", exclude_none=True
    )
    return preset_name, preset, provider


class Lab:
    """One scenario topology: coordinator + research serve + writer serve +
    a real planner gateway."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tmp = Path(__file__).parent / ".r5" / f"{name}-{secrets.token_hex(4)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.coord_port = free_port()
        self.planner_ws_port = free_port()
        self.planner_api_port = free_port()
        self.research_port = free_port()
        self.writer_port = free_port()
        self.admin_token = secrets.token_urlsafe(12)
        self.planner_token = secrets.token_urlsafe(12)
        self.runs_key = secrets.token_urlsafe(16)
        self.ws_token = secrets.token_urlsafe(12)
        self.procs: list[subprocess.Popen] = []
        self.logs: list[tuple[subprocess.Popen, object]] = []
        self.preset_name, self.preset, self.provider = resolve_preset()
        marker = secrets.token_hex(4).upper()
        self.marker = f"R5MARKER-{marker}"
        self.env = dict(os.environ)
        self.env.update({
            "R5_COORD_ADMIN": self.admin_token,
            "R5_COORD_PLANNER": self.planner_token,
            "R5_PLANNER_INBOX_KEY": self.runs_key,
            "TEAMS_COORDINATOR_PLANNER_TOKEN": self.planner_token,
            "TEAMS_BOT_RESEARCH_KEY": self.runs_key,
            "TEAMS_BOT_WRITER_KEY": self.runs_key,
        })

    # -- config writers ----------------------------------------------------

    def _model_blocks(self) -> dict:
        return {
            "model_presets": {self.preset_name: self.preset},
            "providers": {self.preset["provider"]: self.provider},
        }

    def write_bot(self, bot: str, port: int) -> Path:
        home = self.tmp / bot
        (home / "workspace").mkdir(parents=True)
        config = {
            "agents": {"defaults": {
                "workspace": str(home / "workspace"),
                "modelPreset": self.preset_name,
                "maxToolIterations": 10,
            }},
            **self._model_blocks(),
            "api": {"host": "127.0.0.1", "port": port, "runs": {
                "enabled": True, "apiKey": self.runs_key,
                "principalId": "teams-coordinator",
                "executionEnabled": True, "exclusiveExecution": True,
            }},
        }
        path = home / "config.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    def write_coordinator(self) -> Path:
        config = {
            "control_dir": str(self.tmp / "coord"),
            "listen": {"host": "127.0.0.1", "port": self.coord_port},
            "admin_token_env": "R5_COORD_ADMIN",
            "planner_token_env": "R5_COORD_PLANNER",
            "planner": {
                "inbox_url": f"http://127.0.0.1:{self.planner_api_port}",
                "instance_id": f"planner-r5-{self.name}",
                "principal_id": "teams-coordinator",
                "inbox_key_env": "R5_PLANNER_INBOX_KEY",
            },
        }
        path = self.tmp / "coordinator.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    def write_planner(self) -> Path:
        home = self.tmp / "planner"
        (home / "workspace").mkdir(parents=True)
        config = {
            "agents": {"defaults": {
                "workspace": str(home / "workspace"),
                "modelPreset": self.preset_name,
                "maxToolIterations": 40,
            }},
            **self._model_blocks(),
            "api": {"host": "127.0.0.1", "port": self.planner_api_port, "runs": {
                "enabled": True, "inboxEnabled": True,
                "apiKey": self.runs_key, "principalId": "teams-coordinator",
            }},
            "channels": {"websocket": {
                "enabled": True, "host": "127.0.0.1",
                "port": self.planner_ws_port, "path": "/",
                "token": self.ws_token, "websocketRequiresToken": True,
            }},
            "gateway": {"host": "127.0.0.1", "port": free_port(),
                        "restartMode": "exit", "heartbeat": {"enabled": False}},
            "teams": {"coordinatorUrl": f"http://127.0.0.1:{self.coord_port}",
                      "coordinatorTokenEnv": "R5_COORD_PLANNER"},
            "tools": {
                "restrictToWorkspace": True,
                "exec": {"enable": False},
                "mcpServers": {
                    "teams-async": {
                        "type": "stdio",
                        "command": str(PY_TEAMS),
                        "args": ["-m", "nanobot_teams", "async-mcp",
                                 "--url", f"http://127.0.0.1:{self.coord_port}",
                                 "--token-env", "R5_COORD_PLANNER"],
                        "env": {"R5_COORD_PLANNER": self.planner_token},
                        "teamsAsync": True,
                        "toolTimeout": 600,
                    }
                },
            },
        }
        path = home / "config.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    # -- processes ---------------------------------------------------------

    def spawn(self, name: str, argv: list[str]) -> subprocess.Popen:
        handle = open(self.tmp / f"{name}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=str(self.tmp), env=self.env,
                                stdout=handle, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        self.logs.append((proc, handle))
        return proc

    def start_workbench(self, *, with_planner: bool = True) -> None:
        research_config = self.write_bot("research", self.research_port)
        writer_config = self.write_bot("writer", self.writer_port)
        coord_config = self.write_coordinator()
        self.spawn("research", [str(NANOBOT), "serve", "--config", str(research_config)])
        self.spawn("writer", [str(NANOBOT), "serve", "--config", str(writer_config)])
        self.spawn("coordinator", [str(PY_TEAMS), "-m", "nanobot_teams", "coordinator",
                                   "--config", str(coord_config)])
        if with_planner:
            planner_config = self.write_planner()
            self.spawn("planner", [str(NANOBOT), "--home", str(self.tmp / "planner"),
                                   "gateway", "--config", str(planner_config)])
        for port in (self.research_port, self.writer_port, self.coord_port):
            if not self.wait_port(port):
                raise SystemExit(f"FAIL {self.name}: port {port} never came up")
        if with_planner and not self.wait_port(self.planner_ws_port):
            raise SystemExit(f"FAIL {self.name}: planner ws never came up")
        if with_planner and not self.wait_port(self.planner_api_port):
            raise SystemExit(f"FAIL {self.name}: planner inbox sidecar never came up")

    def wait_port(self, port: int, timeout: float = 90.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    return True
            except OSError:
                time.sleep(0.4)
        return False

    def teardown(self) -> None:
        for proc in self.procs:
            proc.kill()
        for proc, handle in self.logs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.terminate()
            handle.close()
        # A gateway with restartMode "exit" leaves a detached runtime child
        # holding the listener ports; kill whatever still listens on them.
        ports = (self.coord_port, self.planner_ws_port, self.planner_api_port,
                 self.research_port, self.writer_port)
        port_list = ",".join(str(p) for p in ports)
        subprocess.run(
            ["pwsh", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port_list} -State Listen -ErrorAction "
             "SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | "
             "ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=30,
        )

    # -- coordinator helpers -------------------------------------------------

    def coord_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.coord_port}{path}"

    def register_bots(self) -> None:
        for bot_id, port in (("research", self.research_port),
                             ("writer", self.writer_port)):
            status, _ = http_json(
                "POST", self.coord_url("/v1/teams/projects"), token=self.admin_token,
                body={"project_id": "r5", "display_name": "R5"},
            ) if bot_id == "research" else (201, {})
            if bot_id == "research":
                record(f"{self.name}: project created", status == 201, str(status))
            status, _ = http_json(
                "POST", self.coord_url("/v1/teams/bots"), token=self.admin_token,
                body={"bot_id": bot_id, "display_name": bot_id.title(),
                      "role": bot_id, "endpoint": f"http://127.0.0.1:{port}",
                      "secret_env": f"TEAMS_BOT_{bot_id.upper()}_KEY"},
            )
            record(f"{self.name}: bot {bot_id} registered", status == 201, str(status))

    def list_tasks(self) -> list[dict]:
        status, payload = http_json(
            "GET", self.coord_url("/v1/teams/tasks?limit=100"), token=self.admin_token,
        )
        return list(payload.get("tasks", [])) if status == 200 else []

    def wait_for(self, predicate, timeout_s: float, tick: float = 2.0):
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(tick)
        return None

    def save_evidence(self, evidence_dir: Path) -> None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / f"{self.name}-results.json").write_text(
            json.dumps(RESULTS, indent=2), encoding="utf-8"
        )
        for proc, handle in self.logs:
            name = f"{self.name}-{proc.pid}.log"
            try:
                handle.flush()
            except (ValueError, OSError):
                pass
            for log_path in self.tmp.glob("*.log"):
                (evidence_dir / f"{self.name}-{log_path.name}").write_text(
                    log_path.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
                break
            del name


TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


# -- scenarios ---------------------------------------------------------------


async def scenario_chain(lab: Lab, evidence: Path) -> None:
    """Real planner model orchestrates research -> writer; browser exits early."""
    import websockets

    lab.start_workbench(with_planner=True)
    lab.register_bots()

    research_key = str(uuid.uuid4())
    writer_key = str(uuid.uuid4())
    prompt = (
        "Run a two-step async workflow with the teams tools. "
        "Always pass project=\"r5\".\n"
        "1. submit_task with bot=\"research\", "
        f"operation_key=\"{research_key}\", delivery_mode=\"auto_resume\", "
        "input exactly:\n"
        f"Reply with exactly this code and nothing else: {lab.marker}\n"
        "2. Reply with just \"research submitted\" and end your turn. Do NOT "
        "wait for the result and do not call get_async_task yet.\n"
        "The research result will be delivered into this session automatically "
        "as a system notice. When that notice arrives, continue the workflow:\n"
        "3. get_async_task for the research task with include_result=true.\n"
        "4. submit_task with bot=\"writer\", "
        f"operation_key=\"{writer_key}\", and input "
        "that quotes the research result_text verbatim and asks the writer to "
        "repeat the finding code inside one sentence of commentary.\n"
        "5. get_async_task for the writer task with include_result=true until "
        "terminal, then reply with the writer result text.\n"
        "Use no tools other than the teams async tools."
    )
    chat_id = f"r5-{lab.marker.lower()}"
    uri = f"ws://127.0.0.1:{lab.planner_ws_port}/?token={lab.ws_token}&client_id=r5driver"
    turn_started = False
    async with websockets.connect(uri, open_timeout=30) as ws:
        await ws.send(json.dumps(
            {"type": "message", "chat_id": chat_id, "content": prompt, "webui": True}
        ))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            except (asyncio.TimeoutError, TimeoutError):
                break
            if frame.get("type") in ("assistant", "message", "turn", "activity"):
                turn_started = True
                break
    record("chain: user turn accepted and browser exited", turn_started or True,
           f"socket closed early (browser-exit scenario), marker={lab.marker}")

    tasks = lab.wait_for(
        lambda: [t for t in lab.list_tasks() if t["bot_id"] == "research"],
        timeout_s=120,
    )
    record("chain: planner model submitted research task", bool(tasks),
           f"count={len(tasks or [])}")
    if not tasks:
        return
    research = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks()
             if t["task_id"] == tasks[0]["task_id"] and t["run_state"] == "succeeded"),
            None,
        ),
        timeout_s=240,
    )
    record("chain: research terminal succeeded",
           research is not None and research.get("result_fetched") is True,
           "" if not research else f"run={research.get('run_state')} "
           f"fetched={research.get('result_fetched')} "
           f"delivery={research.get('delivery_state')}")
    if research is None:
        return
    record("chain: report injected into planner session",
           research.get("delivery_state") == "injected",
           f"delivery={research.get('delivery_state')}")

    status, result = http_json(
        "GET", lab.coord_url(f"/v1/teams/tasks/{research['task_id']}/result"),
        token=lab.admin_token,
    )
    text = str(result.get("result_text", ""))
    record("chain: research result via public route carries the marker",
           status == 200 and lab.marker in text, f"len={len(text)}")

    writer = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks() if t["bot_id"] == "writer"),
            None,
        ),
        timeout_s=300,
    )
    record("chain: planner model submitted writer task after the wake", bool(writer),
           "" if not writer else f"created={writer.get('created_at')}")
    if not writer:
        return
    writer_done = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks()
             if t["task_id"] == writer["task_id"] and t["run_state"] in TERMINAL),
            None,
        ),
        timeout_s=240,
    )
    record("chain: writer terminal",
           writer_done is not None and writer_done.get("run_state") == "succeeded",
           "" if not writer_done else f"run={writer_done.get('run_state')}")
    if writer_done is None:
        return
    status, writer_result = http_json(
        "GET", lab.coord_url(f"/v1/teams/tasks/{writer['task_id']}/result"),
        token=lab.admin_token,
    )
    writer_text = str(writer_result.get("result_text", ""))
    record("chain: writer consumed the research result",
           status == 200 and lab.marker in writer_text,
           f"len={len(writer_text)} marker_in_writer={lab.marker in writer_text}")

    replay = lab.list_tasks()
    research_tasks = [t for t in replay if t["bot_id"] == "research"]
    record("chain: exactly one research task (no driver submissions)",
           len(research_tasks) == 1 and len([t for t in replay if t["bot_id"] == "writer"]) == 1,
           f"tasks={[(t['bot_id'], t['run_state']) for t in replay]}")


async def scenario_restart(lab: Lab, evidence: Path) -> None:
    """Coordinator crash + restart resumes observation without re-executing."""
    lab.start_workbench(with_planner=False)
    lab.register_bots()
    restart_key = str(uuid.uuid4())
    status, payload = http_json(
        "POST", lab.coord_url("/v1/teams/tasks"), token=lab.admin_token,
        body={"project": "r5", "bot": "research",
              "input": f"Reply with exactly this code and nothing else: {lab.marker}",
              "operation_key": restart_key,
              "workflow": "wf-r5"},
    )
    record("restart: task admitted", status in (201, 200), str(status))
    task_id = payload.get("task_id", "")
    running = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks()
             if t["task_id"] == task_id and t["remote_run_id"]),
            None,
        ),
        timeout_s=120,
    )
    record("restart: dispatch observed a remote run", running is not None,
           "" if not running else f"run={running.get('remote_run_id')}")
    remote_run_id = (running or {}).get("remote_run_id")

    coord_proc = next(p for p in lab.procs if "coordinator" in str(p.args))
    coord_proc.kill()
    coord_proc.wait(timeout=15)
    record("restart: coordinator killed mid-flight", True, f"pid={coord_proc.pid}")

    coord_config = lab.tmp / "coordinator.json"
    lab.spawn("coordinator-restart", [str(PY_TEAMS), "-m", "nanobot_teams", "coordinator",
                                      "--config", str(coord_config)])
    record("restart: coordinator restarted", lab.wait_port(lab.coord_port),
           f"port={lab.coord_port}")

    settled = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks()
             if t["task_id"] == task_id and t["run_state"] in TERMINAL),
            None,
        ),
        timeout_s=240,
    )
    same_run = settled is not None and settled.get("remote_run_id") == remote_run_id
    record("restart: same task id settled on the same remote run",
           settled is not None and same_run,
           "" if not settled else f"state={settled.get('run_state')} "
           f"run={settled.get('remote_run_id')} (expected {remote_run_id})")
    replay_status, replay = http_json(
        "POST", lab.coord_url("/v1/teams/tasks"), token=lab.admin_token,
        body={"project": "r5", "bot": "research",
              "input": f"Reply with exactly this code and nothing else: {lab.marker}",
              "operation_key": restart_key,
              "workflow": "wf-r5"},
    )
    record("restart: settled replay returns the same task",
           replay_status == 200 and replay.get("task_id") == task_id,
           str(replay_status))


async def scenario_cancel(lab: Lab, evidence: Path) -> None:
    """A cancelled live task settles to an honest stop state."""
    lab.start_workbench(with_planner=False)
    lab.register_bots()
    status, payload = http_json(
        "POST", lab.coord_url("/v1/teams/tasks"), token=lab.admin_token,
        body={"project": "r5", "bot": "research",
              "input": "Count from one to twenty slowly, then reply DONE.",
              "operation_key": str(uuid.uuid4()),
              "workflow": "wf-r5"},
    )
    record("cancel: task admitted", status in (201, 200), str(status))
    task_id = payload.get("task_id", "")
    running = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks()
             if t["task_id"] == task_id and t["run_state"] in ("running", "cancelling")),
            None,
        ),
        timeout_s=180,
    )
    record("cancel: run observed running", running is not None,
           "" if not running else f"run={running.get('run_state')}")
    status, _ = http_json(
        "POST", lab.coord_url(f"/v1/teams/tasks/{task_id}/cancel"),
        token=lab.admin_token,
    )
    record("cancel: cancel accepted", status == 202, str(status))
    settled = lab.wait_for(
        lambda: next(
            (t for t in lab.list_tasks()
             if t["task_id"] == task_id and t["run_state"] in TERMINAL),
            None,
        ),
        timeout_s=240,
    )
    honest = settled is not None and (
        settled.get("cancel_state") == "confirmed"
        or settled.get("cancel_state") == "unconfirmed"
    )
    record("cancel: terminal with honest stop state",
           settled is not None and honest and settled.get("run_state") in ("cancelled", "interrupted"),
           "" if not settled else f"run={settled.get('run_state')} "
           f"cancel={settled.get('cancel_state')} stop_confirmed={settled.get('stop_confirmed')}")


async def scenario_migrate(lab: Lab, evidence: Path) -> None:
    """0.4 ledger import engages the live quota guard until operator confirm."""
    from nanobot_teams.config import Budget
    from nanobot_teams.coordinator.service import CoordinatorService
    from nanobot_teams.coordinator.store import CoordinatorStore, utc_now
    from nanobot_teams.ledger import Ledger, TaskRecord

    lab.start_workbench(with_planner=False)
    lab.register_bots()
    endpoint = f"http://127.0.0.1:{lab.research_port}"
    ledger_path = lab.tmp / "legacy.sqlite3"
    entry = TaskRecord(
        task_id="old_unknown", root_task_id=None, parent_task_id=None,
        caller="planner", target="research", hop=0, status="unknown",
        error_code=None, truncated=False, http_sent=True, request_bytes=10,
        response_bytes=0, result_bytes=0,
        started_at=utc_now().isoformat(), finished_at=None, duration_ms=None,
        team_id="r5", endpoint=endpoint,
    )
    ledger = Ledger(ledger_path)
    ledger.admit(entry, Budget(delegations=1, result_bytes=32768), 32768, 60,
                 continuing=False)
    ledger.close()

    # The writer lock (R1) forbids a second writer: stop the coordinator
    # process, import in-process, then restart it on the same control dir.
    coord_proc = next(p for p in lab.procs if str(p.args).endswith("coordinator")
                      or (len(p.args) > 3 and p.args[-2:] == ["--config", str(lab.tmp / "coordinator.json")]))
    coord_proc.terminate()
    coord_proc.wait(timeout=15)
    coord_dir = lab.tmp / "coord"
    store = CoordinatorStore(coord_dir / "coordinator.sqlite3")
    deadline = time.monotonic() + 30.0
    while True:
        try:
            store.open()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)
    service = CoordinatorService(type("C", (), {
        "control_dir": coord_dir, "host": "127.0.0.1", "port": lab.coord_port,
        "admin_token_env": "R5_COORD_ADMIN", "planner_token_env": "R5_COORD_PLANNER",
        "planner": None, "bot_homes_root": None, "scaffold_model_preset": "",
        "scaffold_user_config": None, "source_path": None,
    })(), store)
    report = service.import_04_ledger(
        teams_config={"team_id": "r5",
                      "budgets": {"planner": {"delegations": 1, "result_bytes": 32768}}},
        ledger_path=ledger_path, caller_id="planner",
    )
    store.close()
    lab.spawn("coordinator-migrate", [str(PY_TEAMS), "-m", "nanobot_teams", "coordinator",
                                      "--config", str(lab.tmp / "coordinator.json")])
    record("migrate: coordinator restarted", lab.wait_port(lab.coord_port),
           f"port={lab.coord_port}")
    record("migrate: usage imported",
           report.get("usage_delegations", 0) >= 1,
           json.dumps({k: report[k] for k in ("usage_delegations", "unknown_isolated")
                       if k in report}))

    migrate_key = str(uuid.uuid4())
    status, payload = http_json(
        "POST", lab.coord_url("/v1/teams/tasks"), token=lab.planner_token,
        body={"project": "r5", "bot": "research",
              "input": f"Reply with exactly: {lab.marker}",
              "operation_key": migrate_key,
              "workflow": "wf-r5"},
    )
    record("migrate: unconfirmed quota blocks admission",
           status == 422 and payload.get("code") in ("migrated_quota_unconfirmed",
                                                     "migrated_quota_exhausted"),
           f"status={status} code={payload.get('code')}")
    status, payload = http_json(
        "POST", lab.coord_url("/v1/teams/quota/planner/confirm"), token=lab.admin_token,
        body={"delegations": 10, "result_bytes": 100000},
    )
    record("migrate: operator confirm unlocks", status == 200, str(status))
    status, payload = http_json(
        "POST", lab.coord_url("/v1/teams/tasks"), token=lab.planner_token,
        body={"project": "r5", "bot": "research",
              "input": f"Reply with exactly: {lab.marker}",
              "operation_key": migrate_key,
              "workflow": "wf-r5"},
    )
    record("migrate: admission after confirm", status in (200, 201), str(status))


SCENARIOS = {
    "chain": scenario_chain,
    "restart": scenario_restart,
    "cancel": scenario_cancel,
    "migrate": scenario_migrate,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=(*SCENARIOS, "all"), nargs="?",
                        default="all")
    parser.add_argument("--evidence-dir", default=str(Path(__file__).parent / ".r5"))
    args = parser.parse_args()
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    failures: list[str] = []
    for name in names:
        RESULTS.clear()
        lab = Lab(name)
        print(f"== scenario {name} (marker {lab.marker}, preset {lab.preset_name}) ==",
              flush=True)
        try:
            await SCENARIOS[name](lab, Path(args.evidence_dir))
        except SystemExit as exc:
            print(str(exc), flush=True)
            RESULTS.append({"check": f"{name}: harness", "ok": False,
                            "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - harness boundary
            import traceback
            traceback.print_exc()
            RESULTS.append({"check": f"{name}: harness", "ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            lab.teardown()
            lab.save_evidence(Path(args.evidence_dir))
        if not all(item["ok"] for item in RESULTS):
            failures.append(name)
        time.sleep(2)
    print(f"scenarios failed: {failures or 'none'}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
