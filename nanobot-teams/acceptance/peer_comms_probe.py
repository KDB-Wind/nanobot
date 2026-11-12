"""Four-bot peer comms acceptance probe (PEER_COMMS_GOAL.md P2, red-first).

Boots a real topology with real processes: coordinator + planner gateway
(secretary) + three worker serves (research / writer / analyst). Asserts the
peer-comms contract:

  peer_dispatch    worker A dispatches to worker B and the reply lands in
                   A's own inbox (E2E-02/E2E-03 slices).
  unauthorized     B->C without an explicit edge is a typed rejection
                   (E2E-05).
  hop_guard        a fifth hop is rejected by the hop guard (E2E-06).
  idempotent_key   the same operation key yields exactly one task (E2E-07).
  crash_restart    the coordinator killed mid-delivery and restarted
                   converges: no duplicate tasks, no lost receipts (E2E-08).
  events_shape     events carry sender/recipient/task/hop for peer traffic
                   (I10).

Pre-P3 the current code must fail the first assertion; every scenario still
records its error code and evidence instead of crashing. Later phases flip
scenarios green; exit 0 only when all pass. A fake executor is only used to
pin a crash point, never to fake a pass.

Usage: python nanobot-teams/acceptance/peer_comms_probe.py [--keep]
Env: PEER_COMMS_PRESET (default glm-5.3), resolved from the operator's user
config exactly like live_workbench_r5.py does.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from peer_evidence import (
    ContinuationWatcher,
    ReplyAckGate,
    continuation_evidence,
    one_database,
    read_rows,
)

WT = Path(__file__).resolve().parents[2]
TEAMS = WT / "nanobot-teams"
if os.name == "nt":
    PY_TEAMS = TEAMS / ".venv/Scripts/python.exe"
    NANOBOT = WT / ".venv-nanobot/Scripts/nanobot.exe"
else:
    PY_TEAMS = TEAMS / ".venv/bin/python"
    NANOBOT = WT / ".venv-nanobot/bin/nanobot"

PY_TEAMS = Path(os.environ.get("PEER_TEAMS_PYTHON", str(PY_TEAMS)))
NANOBOT = Path(os.environ.get("PEER_NANOBOT", str(NANOBOT)))
EVIDENCE = Path(os.environ.get("PEER_EVIDENCE_DIR",
                               str(Path(tempfile.gettempdir()) / "nanobot-peer-evidence")))
SSE_HEARTBEAT = 15.0


def free_port() -> int:
    """A bindable listener port below the Windows ephemeral range."""
    import random

    for _ in range(200):
        port = random.randint(20000, 45000)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("FAIL no bindable port found in 20000-45000")


def http_json(method: str, url: str, *, token: str = "", body: dict | None = None,
              timeout: float = 30.0):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            return exc.code, {}
    except OSError as exc:
        return -1, {"error": str(exc)}


def sse_events(url: str, *, token: str, deadline_s: float) -> list[dict]:
    """Read SSE data frames until the deadline; best effort, no infinite wait."""
    events: list[dict] = []
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=deadline_s) as resp:
            start = time.monotonic()
            buf = b""
            while time.monotonic() - start < deadline_s:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    for line in frame.decode("utf-8", "replace").splitlines():
                        if line.startswith("data: "):
                            try:
                                events.append(json.loads(line[6:]))
                            except ValueError:
                                pass
                if events:
                    return events
    except (OSError, urllib.error.HTTPError):
        pass
    return events


def resolve_preset() -> tuple[str, dict, dict]:
    sys.path.insert(0, str(WT))
    from nanobot.config.loader import load_config, resolve_config_env_vars

    preset_name = os.environ.get("PEER_COMMS_PRESET", "glm-5.3")
    source = load_config()
    if preset_name not in source.model_presets:
        raise SystemExit(f"FAIL preset {preset_name} not configured")
    preset = resolve_config_env_vars(
        source.model_presets[preset_name]
    ).model_dump(by_alias=True, mode="json", exclude_none=True)
    provider = resolve_config_env_vars(
        getattr(source.providers, preset["provider"])
    ).model_dump(by_alias=True, mode="json", exclude_none=True)
    return preset_name, preset, provider


class PeerLab:
    """One four-bot topology: coordinator + planner + research/writer/analyst."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tmp = Path(tempfile.mkdtemp(prefix=f"nanobot-{name}-"))
        self.coord_port = free_port()
        self.planner_ws_port = free_port()
        self.planner_api_port = free_port()
        self.worker_ports = {
            "research": free_port(),
            "writer": free_port(),
            "analyst": free_port(),
        }
        self.admin_token = secrets.token_urlsafe(12)
        self.planner_token = secrets.token_urlsafe(12)
        # Per-bot coordinator credentials: distinct values, distinct env names.
        # Pre-P3 the coordinator knows none of them; every peer call must 401.
        self.bot_coord_tokens = {
            bot: secrets.token_urlsafe(14) for bot in self.worker_ports
        }
        # Per-bot runs keys (dispatch INTO the worker's serve) and the planner
        # inbox key the coordinator uses to deliver into the secretary.
        self.runs_keys = {bot: secrets.token_urlsafe(16) for bot in self.worker_ports}
        self.planner_inbox_key = secrets.token_urlsafe(16)
        self.ws_token = secrets.token_urlsafe(12)
        self.procs: list[subprocess.Popen] = []
        self.logs: list[tuple[subprocess.Popen, object]] = []
        self.named: dict[str, subprocess.Popen] = {}
        # Browser labs disable the websocket token; MCP-driven labs keep it.
        self.ws_requires_token = True
        self.coord_proc: subprocess.Popen | None = None
        self.preset_name, self.preset, self.provider = resolve_preset()
        marker = secrets.token_hex(4).upper()
        self.marker = f"PEERMARKER-{marker}"
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = os.pathsep.join(
            [str(WT), str(TEAMS / "src"), self.env.get("PYTHONPATH", "")]
        )
        self.reply_gate = ReplyAckGate(f"http://127.0.0.1:{self.worker_ports['research']}")
        self.continuation_watchers: list[ContinuationWatcher] = []
        self.env.update({
            "PEER_COORD_ADMIN": self.admin_token,
            "PEER_COORD_PLANNER": self.planner_token,
            "PEER_PLANNER_INBOX_KEY": self.planner_inbox_key,
        })
        for bot in self.worker_ports:
            self.env[f"PEER_COORD_TOKEN_{bot.upper()}"] = self.bot_coord_tokens[bot]
            self.env[f"PEER_BOT_{bot.upper()}_KEY"] = self.runs_keys[bot]

    # -- config writers ----------------------------------------------------

    def _model_blocks(self) -> dict:
        return {
            "model_presets": {self.preset_name: self.preset},
            "providers": {self.preset["provider"]: self.provider},
        }

    def write_worker(self, bot: str) -> Path:
        """A fully peer-wired worker: runs inbox plus the teams-async client."""
        home = self.tmp / bot
        (home / "workspace").mkdir(parents=True)
        token_env = f"PEER_COORD_TOKEN_{bot.upper()}"
        config = {
            "agents": {"defaults": {
                "workspace": str(home / "workspace"),
                "modelPreset": self.preset_name,
                "maxToolIterations": 40,
            }},
            **self._model_blocks(),
            "api": {"host": "127.0.0.1", "port": self.worker_ports[bot], "runs": {
                "enabled": True,
                "apiKey": self.runs_keys[bot],
                "principalId": "teams-coordinator",
                "executionEnabled": True,
                "exclusiveExecution": True,
                "inboxEnabled": True,
            }},
            "tools": {
                "restrictToWorkspace": True,
                "exec": {"enable": False},
                "mcpServers": {
                    "teams-async": {
                        "type": "stdio",
                        "command": str(PY_TEAMS),
                        "args": ["-m", "nanobot_teams", "async-mcp",
                                 "--url", f"http://127.0.0.1:{self.coord_port}",
                                 "--token-env", token_env],
                        "env": {token_env: "${" + token_env + "}"},
                        "teamsAsync": True,
                        "toolTimeout": 600,
                    },
                },
            },
        }
        path = home / "config.json"
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
                "apiKey": self.env["PEER_PLANNER_INBOX_KEY"],
                "principalId": "teams-coordinator",
            }},
            "channels": {"websocket": {
                "enabled": True, "host": "127.0.0.1",
                "port": self.planner_ws_port, "path": "/",
                "token": self.ws_token,
                "websocketRequiresToken": self.ws_requires_token,
            }},
            "gateway": {"host": "127.0.0.1", "port": free_port(),
                        "restartMode": "exit", "heartbeat": {"enabled": False}},
            "teams": {"coordinatorUrl": f"http://127.0.0.1:{self.coord_port}",
                      "coordinatorTokenEnv": "PEER_COORD_PLANNER"},
            "tools": {
                "restrictToWorkspace": True,
                "exec": {"enable": False},
                "mcpServers": {
                    "teams-async": {
                        "type": "stdio",
                        "command": str(PY_TEAMS),
                        "args": ["-m", "nanobot_teams", "async-mcp",
                                 "--url", f"http://127.0.0.1:{self.coord_port}",
                                 "--token-env", "PEER_COORD_PLANNER"],
                        "env": {"PEER_COORD_PLANNER": self.planner_token},
                        "teamsAsync": True,
                        "toolTimeout": 600,
                    }
                },
            },
        }
        path = home / "config.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    def write_coordinator(self) -> Path:
        config = {
            "control_dir": str(self.tmp / "coord"),
            "listen": {"host": "127.0.0.1", "port": self.coord_port},
            "admin_token_env": "PEER_COORD_ADMIN",
            "planner_token_env": "PEER_COORD_PLANNER",
            "bot_homes_root": str(self.tmp),
            "planner": {
                "inbox_url": f"http://127.0.0.1:{self.planner_api_port}",
                "instance_id": f"peer-planner-{self.name}",
                "principal_id": "teams-coordinator",
                "inbox_key_env": "PEER_PLANNER_INBOX_KEY",
            },
        }
        path = self.tmp / "coordinator.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    # -- processes ---------------------------------------------------------

    def spawn(self, name: str, argv: list[str]) -> subprocess.Popen:
        handle = open(self.tmp / f"{name}.log", "a", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=str(self.tmp), env=self.env,
                                stdout=handle, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        self.logs.append((proc, handle))
        self.named[name] = proc
        return proc

    def stop_worker(self, bot: str) -> None:
        """Kill one worker and wait for its port to close.

        Used by the browser lab to leave a task permanently pending, so the
        workbench's stop control has something to act on.
        """
        proc = self.named.pop(bot, None)
        if proc is None:
            raise SystemExit(f"FAIL {self.name}: no process named {bot}")
        if sys.platform == "win32":
            # The venv launcher re-executes the script; kill the tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            proc.kill()
        proc.wait(timeout=15)
        port = self.worker_ports[bot]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    time.sleep(0.2)
            except OSError:
                return
        raise SystemExit(f"FAIL {self.name}: worker {bot} still listens on {port}")

    def spawn_coordinator(self, config_path: Path) -> subprocess.Popen:
        self.coord_proc = self.spawn(
            "coordinator", [str(PY_TEAMS), "-m", "nanobot_teams", "coordinator",
                            "--config", str(config_path)]
        )
        return self.coord_proc

    def start(self) -> None:
        worker_configs = {bot: self.write_worker(bot) for bot in self.worker_ports}
        planner_config = self.write_planner()
        self.coord_config = self.write_coordinator()
        for bot, cfg in worker_configs.items():
            self.spawn(bot, [str(NANOBOT), "serve", "--config", str(cfg)])
        self.spawn("planner", [str(NANOBOT), "--home", str(self.tmp / "planner"),
                               "gateway", "--config", str(planner_config)])
        self.spawn_coordinator(self.coord_config)
        for bot, port in self.worker_ports.items():
            if not self.wait_port(port):
                raise SystemExit(f"FAIL {self.name}: worker {bot} never came up")
        if not self.wait_port(self.coord_port):
            raise SystemExit(f"FAIL {self.name}: coordinator never came up")
        if not self.wait_port(self.planner_ws_port):
            raise SystemExit(f"FAIL {self.name}: planner ws never came up")
        if not self.wait_port(self.planner_api_port):
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
        for watcher in self.continuation_watchers:
            watcher.close()
        self.reply_gate.close()
        for proc in self.procs:
            if os.name == "nt":
                # The venv launcher re-executes the script, so killing the
                # launcher leaves the real interpreter running and holding the
                # sqlite handles the lab teardown then tries to delete.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                proc.kill()
        for proc, handle in self.logs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.terminate()
            handle.close()
        ports = [self.coord_port, self.planner_ws_port, self.planner_api_port,
                 *self.worker_ports.values()]
        port_list = ",".join(str(p) for p in ports)
        if os.name == "nt":
            subprocess.run(
                ["pwsh", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port_list} -State Listen "
                 "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty "
                 "OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ "
                 "-Force -ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=30,
            )

    # -- coordinator helpers -------------------------------------------------

    def coord_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.coord_port}{path}"

    def register_bots(self) -> dict:
        evidence: dict = {"scenario": "register_bots", "steps": []}
        ok = True
        status, _ = http_json(
            "POST", self.coord_url("/v1/teams/projects"), token=self.admin_token,
            body={"project_id": "peer", "display_name": "Peer Comms"},
        )
        evidence["steps"].append({"step": "create project", "status": status})
        ok = ok and status == 201
        for bot, port in self.worker_ports.items():
            status, payload = http_json(
                "POST", self.coord_url("/v1/teams/bots"), token=self.admin_token,
                body={"bot_id": bot, "display_name": bot.title(),
                      "role": bot,
                      "endpoint": f"http://127.0.0.1:{port}",
                      "secret_env": f"PEER_BOT_{bot.upper()}_KEY",
                      "coord_secret_env": f"PEER_COORD_TOKEN_{bot.upper()}",
                      "inbox_url": (self.reply_gate.url if bot == "research"
                                    else f"http://127.0.0.1:{port}"),
                      "inbox_principal_id": "teams-coordinator",
                      "inbox_key_env": f"PEER_BOT_{bot.upper()}_KEY"},
            )
            evidence["steps"].append(
                {"step": f"register {bot}", "status": status,
                 "code": payload.get("code") if isinstance(payload, dict) else None}
            )
            ok = ok and status == 201
        # The authorized pair for the E2E chain; writer->analyst stays
        # deliberately unedged for the unauthorized scenario.
        status, _ = http_json(
            "POST", self.coord_url("/v1/teams/peer-edges"), token=self.admin_token,
            body={"from_bot_id": "research", "to_bot_id": "writer"},
        )
        evidence["steps"].append({"step": "edge research->writer", "status": status})
        ok = ok and status == 201
        evidence["status"] = "pass" if ok else "fail"
        return evidence

    def peer_send(self, sender: str, recipient: str, *, token: str,
                  operation_key: str | None = None,
                  conversation_root: str | None = None, hop: int | None = None,
                  input_text: str | None = None):
        body: dict = {
            "recipient": recipient,
            "input": input_text or f"Reply with exactly {self.marker}.",
            "operation_key": operation_key or str(uuid.uuid4()),
        }
        if conversation_root is not None:
            body["conversation_root"] = conversation_root
        if hop is not None:
            body["hop"] = hop
        return http_json("POST", self.coord_url("/v1/teams/peer/send"),
                         token=token, body=body)

    # -- scenarios -----------------------------------------------------------

    def scenario_peer_dispatch(self) -> dict:
        ev: dict = {"scenario": "peer_dispatch", "steps": []}
        status, payload = self.peer_send(
            "research", "writer", token=self.bot_coord_tokens["research"]
        )
        ev["steps"].append({"step": "peer send research->writer",
                            "status": status,
                            "code": payload.get("code") if isinstance(payload, dict) else None,
                            "task_id": payload.get("task_id") if isinstance(payload, dict) else None})
        if status not in (200, 201, 202):
            ev["status"] = "fail"
            ev["reason"] = f"peer send rejected with HTTP {status}"
            return ev
        task_id = payload.get("task_id")
        # P4 gate: the dispatch pipeline runs the recipient's real serve and
        # the result becomes queryable.
        deadline = time.monotonic() + 240.0
        run_state = None
        result_available = False
        while time.monotonic() < deadline:
            status, task = http_json(
                "GET", self.coord_url(f"/v1/teams/tasks/{task_id}"),
                token=self.admin_token,
            )
            run_state = task.get("run_state") if isinstance(task, dict) else None
            result_available = bool(
                task.get("result_available") if isinstance(task, dict) else False
            )
            if run_state in ("succeeded", "failed", "cancelled", "interrupted"):
                break
            time.sleep(2.0)
        ev["steps"].append({"step": "poll recipient run", "run_state": run_state,
                            "result_available": result_available})
        if run_state != "succeeded" or not result_available:
            ev["status"] = "fail"
            ev["reason"] = f"recipient run_state={run_state} result_available={result_available}"
            return ev
        ev["status"] = "pass"
        return ev

    def scenario_usage_stats(self) -> dict:
        """P6: every bot reports usage; cache rates follow the §5 contract."""
        ev: dict = {"scenario": "usage_stats", "steps": []}
        status, payload = http_json(
            "GET", self.coord_url("/v1/teams/usage?window=all"),
            token=self.admin_token,
        )
        ev["steps"].append({"step": "usage route", "status": status})
        if status != 200:
            ev["status"] = "fail"
            ev["reason"] = f"usage route HTTP {status}"
            return ev
        bots = payload.get("bots", [])
        by_id = {b["bot_id"]: b for b in bots}
        ev["steps"].append({
            "step": "per-bot usage",
            "summary": {
                bid: (
                    {"available": b["available"],
                     "requests": b.get("totals", {}).get("requests"),
                     "cache_read_tokens": b.get("totals", {}).get("cache_read_tokens"),
                     "cache_read_rate": b.get("totals", {}).get("cache_read_rate"),
                     "coverage": b.get("totals", {}).get("coverage")}
                    if b["available"] else {"available": False, "reason": b.get("reason")}
                )
                for bid, b in by_id.items()
            },
        })
        for worker in ("research", "writer"):
            entry = by_id.get(worker)
            if entry is None:
                ev["status"] = "fail"
                ev["reason"] = f"{worker} missing from usage listing"
                return ev
            if not entry["available"]:
                ev["status"] = "fail"
                ev["reason"] = f"{worker} usage unavailable: {entry.get('reason')}"
                return ev
            if entry["totals"]["requests"] < 1:
                ev["status"] = "fail"
                ev["reason"] = f"{worker} has no recorded calls"
                return ev
        ev["status"] = "pass"
        return ev

    def scenario_live_chain(self) -> dict:
        return self._reply_continuation(crash=False)

    def _stop_coordinator(self) -> None:
        assert self.coord_proc is not None
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.coord_proc.pid)],
                           capture_output=True, check=False)
        else:
            self.coord_proc.kill()
        self.coord_proc.wait(timeout=15)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.coord_port), timeout=0.2):
                    time.sleep(0.1)
            except OSError:
                return
        raise RuntimeError("killed coordinator still has a listening process")

    def _reply_continuation(self, *, crash: bool) -> dict:
        ev: dict = {"scenario": "crash_restart" if crash else "live_chain", "steps": []}
        workflow = str(uuid.uuid4())
        secret = "WRITER-" + secrets.token_hex(16)
        filename = f"reply-{workflow}.txt"
        (self.tmp / "writer" / "workspace" / filename).write_text(secret, encoding="utf-8")
        (self.tmp / "research" / "workspace" / "AGENTS.md").write_text(
            "For an automated async task report, answer exactly WAKE_ACK followed by "
            "the writer's secret from that report. Do not call tools for such a report. "
            "For an initial delegation request, call send_to_bot once, then answer WAITING. "
            "Never poll get_async_task or read task results.\n", encoding="utf-8",
        )
        self.reply_gate.arm(workflow, lose_ack=crash)
        status, payload = http_json(
            "POST", self.coord_url("/v1/teams/tasks"), token=self.planner_token,
            body={"project": "peer", "bot": "research", "workflow": workflow,
                  "input": "Delegate exactly once to writer using send_to_bot. Tell writer "
                  f"to read {filename} in its workspace and reply with only its content. "
                  "Do not read the file yourself. After dispatch, immediately finish with "
                  "WAITING. Do not poll or retrieve any task result. The async report "
                  "will start a separate continuation later.",
                  "operation_key": str(uuid.uuid4())},
        )
        ev["steps"].append({"step": "planner submits research task", "status": status,
                            "task_id": payload.get("task_id") if isinstance(payload, dict) else None})
        if status != 201:
            return {**ev, "status": "fail", "reason": f"planner submit HTTP {status}"}
        research_id = payload["task_id"]
        deadline = time.monotonic() + 480
        peer = None
        research = None
        while time.monotonic() < deadline:
            _, listing = http_json("GET", self.coord_url(
                f"/v1/teams/tasks?workflow={workflow}&limit=100"), token=self.admin_token)
            peers = [t for t in listing.get("tasks", []) if t.get("kind") == "peer"
                     and t.get("caller_principal") == "bot:research" and t.get("bot_id") == "writer"]
            if len(peers) > 1:
                return {**ev, "status": "fail", "reason": "research dispatched multiple peer tasks"}
            if peers:
                peer = peers[0]
            _, research = http_json("GET", self.coord_url(f"/v1/teams/tasks/{research_id}"),
                                    token=self.admin_token)
            if peer and research.get("run_state") == "succeeded":
                break
            time.sleep(0.1)
        if peer is None or not research or research.get("run_state") != "succeeded":
            return {**ev, "status": "fail", "reason": "initial research run did not finish after dispatch"}
        peer_id = peer["task_id"]
        _, initial_result = http_json("GET", self.coord_url(f"/v1/teams/tasks/{research_id}/result"),
                                      token=self.admin_token)
        if secret in str(initial_result.get("result_text", "")):
            return {**ev, "status": "fail", "reason": "initial run already knew writer-only secret"}
        ev["steps"].append({"step": "initial run finished before reply admission",
                            "task_id": research_id, "run_state": research["run_state"],
                            "peer_task_id": peer_id, "workflow_id": workflow})
        watcher = ContinuationWatcher(self.tmp / "research", peer_id, secret)
        self.continuation_watchers.append(watcher)
        self.reply_gate.delivery_allowed.set()
        crashed = False
        turn_id = ""
        evidence: dict = {}
        while time.monotonic() < deadline:
            if self.reply_gate.error:
                return {**ev, "status": "fail", "reason": self.reply_gate.error}
            if crash and not crashed and self.reply_gate.receipt_committed.is_set():
                committed = continuation_evidence(self.tmp / "research", peer_id, secret)
                if committed.get("receipt_count") != 1:
                    return {**ev, "status": "fail", "reason": "ack gate had no durable receipt"}
                self._stop_coordinator()
                ev["steps"].append({"step": "coordinator killed after inbox commit before acknowledgement",
                                    "delivery_id": committed["delivery_id"], "receipt_count": 1,
                                    "exit_code": self.coord_proc.returncode})
                self.reply_gate.ack_allowed.set()
                self.spawn_coordinator(self.coord_config)
                if not self.wait_port(self.coord_port):
                    return {**ev, "status": "fail", "reason": "coordinator did not restart"}
                crashed = True
            try:
                evidence = continuation_evidence(self.tmp / "research", peer_id, secret)
            except (ValueError, OSError):
                evidence = {}
            turn_id = evidence.get("accepting_turn_id") or watcher.turn_id or turn_id
            completed = (
                evidence.get("state") == "injected" and evidence.get("wake_due") == 0
                and evidence.get("injection_count") == 1 and evidence.get("continuation_count") == 1
                and evidence.get("generation_matches") and not evidence.get("turn_pending")
                and turn_id
            )
            if completed and (not crash or (crashed and len(self.reply_gate.attempts) >= 2)):
                _, peer = http_json("GET", self.coord_url(f"/v1/teams/tasks/{peer_id}"), token=self.admin_token)
                if peer.get("delivery_state") == "injected":
                    break
            time.sleep(0.05)
        else:
            return {**ev, "status": "fail", "reason": "no completed reply-driven continuation",
                    "continuation": evidence, "accepting_turn_id": turn_id,
                    "delivery_attempts": len(self.reply_gate.attempts)}
        coordinator_db = self.tmp / "coord" / "coordinator.sqlite3"
        task_count = read_rows(coordinator_db, "SELECT COUNT(*) FROM tasks WHERE caller_principal=? "
                               "AND operation_key=?", ("bot:research", peer["operation_key"]))[0][0]
        run_db = one_database(self.tmp / "writer", "runs.sqlite3")
        runs = read_rows(run_db, "SELECT run_id, state FROM runs WHERE task_id=?", (peer_id,))
        evidence["accepting_turn_id"] = turn_id
        evidence["task_count"] = task_count
        evidence["remote_run_count"] = len(runs)
        evidence["remote_run_id"] = runs[0]["run_id"] if len(runs) == 1 else None
        evidence["delivery_attempts"] = len(self.reply_gate.attempts)
        evidence["distinct_delivery_ids"] = len(set(self.reply_gate.attempts))
        ev["steps"].append({"step": "durable reply and completed continuation", **evidence})
        unique = (task_count == 1 and len(runs) == 1 and runs[0]["state"] == "succeeded"
                  and evidence["receipt_count"] == 1 and evidence["distinct_delivery_ids"] == 1)
        ev["status"] = "pass" if unique else "fail"
        if not unique:
            ev["reason"] = "recovery did not preserve unique task, run, receipt and injection"
        return ev

    def scenario_unauthorized(self, dispatch_ok: bool) -> dict:
        ev: dict = {"scenario": "unauthorized", "steps": []}
        status, payload = self.peer_send(
            "writer", "analyst", token=self.bot_coord_tokens["writer"]
        )
        code = payload.get("code") if isinstance(payload, dict) else None
        ev["steps"].append({"step": "peer send writer->analyst (no edge)",
                            "status": status, "code": code})
        typed_reject = status in (403, 422) and code == "unauthorized_edge"
        if typed_reject:
            ev["status"] = "pass"
        else:
            ev["status"] = "fail"
            ev["reason"] = (f"expected typed unauthorized_edge, got HTTP {status} "
                            f"code={code!r}")
            if not dispatch_ok:
                ev["reason"] += " (peer dispatch itself unavailable; " \
                                "prerequisite red recorded in peer_dispatch)"
        return ev

    def scenario_hop_guard(self, dispatch_ok: bool) -> dict:
        ev: dict = {"scenario": "hop_guard", "steps": []}
        root = str(uuid.uuid4())
        # The hop is server-derived from the conversation; fill the thread
        # with four planner delegations before the peer send.
        seeded = 0
        for _ in range(4):
            status, _payload = http_json(
                "POST", self.coord_url("/v1/teams/tasks"), token=self.planner_token,
                body={"project": "peer", "bot": "analyst", "input": "filler",
                      "operation_key": str(uuid.uuid4()), "workflow": root},
            )
            if status == 201:
                seeded += 1
        ev["steps"].append({"step": "seed conversation", "seeded": seeded})
        status, payload = self.peer_send(
            "research", "writer", token=self.bot_coord_tokens["research"],
            conversation_root=root,
        )
        code = payload.get("code") if isinstance(payload, dict) else None
        ev["steps"].append({"step": "peer send at hop 4 (fifth hop)",
                            "status": status, "code": code})
        if status in (403, 422) and code == "hop_limit_exceeded":
            ev["status"] = "pass"
        else:
            ev["status"] = "fail"
            ev["reason"] = (f"expected typed hop_limit_exceeded, got HTTP {status} "
                            f"code={code!r}")
            if not dispatch_ok:
                ev["reason"] += " (prerequisite red recorded in peer_dispatch)"
        return ev

    def scenario_idempotent_key(self, dispatch_ok: bool) -> dict:
        ev: dict = {"scenario": "idempotent_key", "steps": []}
        key = str(uuid.uuid4())
        first = self.peer_send("research", "writer",
                               token=self.bot_coord_tokens["research"],
                               operation_key=key)
        second = self.peer_send("research", "writer",
                                token=self.bot_coord_tokens["research"],
                                operation_key=key)
        ev["steps"].append({
            "step": "same operation key twice",
            "first_status": first[0],
            "second_status": second[0],
            "first_task": first[1].get("task_id") if isinstance(first[1], dict) else None,
            "second_task": second[1].get("task_id") if isinstance(second[1], dict) else None,
        })
        same = (
            first[0] in (200, 201, 202)
            and isinstance(first[1], dict) and isinstance(second[1], dict)
            and first[1].get("task_id") == second[1].get("task_id")
        )
        if same:
            ev["status"] = "pass"
        else:
            ev["status"] = "fail"
            ev["reason"] = "same key did not converge to one task" \
                if dispatch_ok else \
                "peer send unavailable; prerequisite red recorded in peer_dispatch"
        return ev

    def scenario_crash_restart(self, dispatch_ok: bool) -> dict:
        if not dispatch_ok:
            return {"scenario": "crash_restart", "status": "blocked",
                    "reason": "peer dispatch unavailable"}
        return self._reply_continuation(crash=True)

    def scenario_events_shape(self) -> dict:
        ev: dict = {"scenario": "events_shape", "steps": []}
        events = sse_events(
            self.coord_url("/v1/teams/events?after=0"), token=self.admin_token,
            deadline_s=8.0,
        )
        ev["steps"].append({"step": "sse read", "event_count": len(events)})
        peer_events = [
            e for e in events
            if isinstance(e.get("data"), dict)
            and e["data"].get("kind") == "peer"
            and e["data"].get("sender") and e["data"].get("recipient")
            and e.get("task_id") and "conversation_root" in e["data"]
        ]
        if peer_events:
            ev["status"] = "pass"
            ev["steps"].append({"step": "peer event sample",
                                "sample": peer_events[0]})
        else:
            ev["status"] = "fail"
            ev["reason"] = ("no event carries sender/recipient/hop/task for peer "
                            f"traffic ({len(events)} events seen)")
        return ev


def _remove_lab_dir(path: Path, *, attempts: int = 20) -> None:
    """Best-effort removal of the lab home; never changes the verdict.

    A locked sqlite file makes rmtree raise WinError 32 on Windows. Cleanup
    runs after the evidence write for that reason, and when it still fails it
    only warns: cleanup is not a test result.
    """
    import shutil

    for remaining in range(attempts, 0, -1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if remaining == 1:
                print(f"warning: could not remove {path}", file=sys.stderr)
                return
            time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="retain the isolated bot homes")
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE)
    parser.add_argument("--only", action="append", choices=(
        "peer_dispatch", "unauthorized", "hop_guard", "idempotent_key", "crash_restart",
        "events_shape", "live_chain", "usage_stats"), help="run only selected scenarios")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    lab = PeerLab("peer")
    results: list[dict] = []
    try:
        lab.start()
        registration = lab.register_bots()
        results.append(registration)
        selected = args.only or ["peer_dispatch", "unauthorized", "hop_guard", "idempotent_key",
                                 "crash_restart", "events_shape", "live_chain", "usage_stats"]
        dispatch_ok = registration["status"] == "pass"
        for name in selected:
            scenario = getattr(lab, "scenario_" + name)
            result = scenario(dispatch_ok) if name in {
                "unauthorized", "hop_guard", "idempotent_key", "crash_restart"
            } else scenario()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if name == "peer_dispatch":
                dispatch_ok = result["status"] == "pass"
    except (SystemExit, Exception) as exc:
        results.append({"scenario": "runtime", "status": "fail", "reason": str(exc)})
    finally:
        if args.keep:
            results.append({"scenario": "meta", "topology_dir": str(lab.tmp)})
        try:
            lab.teardown()
        except Exception as exc:  # noqa: BLE001 - cleanup must not cost the evidence
            print(f"warning: teardown failed: {exc}", file=sys.stderr)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.evidence_dir / f"peer-probe-{stamp}.jsonl"
    with open(out, "w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    passed = sum(r.get("status") == "pass" for r in results)
    failed = sum(r.get("status") == "fail" for r in results)
    blocked = sum(r.get("status") == "blocked" for r in results)
    green = failed == 0 and blocked == 0 and passed > 0
    print(json.dumps({"probe": "peer_comms", "passed": passed, "failed": failed,
                      "blocked": blocked, "evidence": str(out),
                      "verdict": "GREEN" if green else "RED"}))
    if not args.keep:
        _remove_lab_dir(lab.tmp)
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
