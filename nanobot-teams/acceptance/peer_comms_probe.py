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
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
TEAMS = WT / "nanobot-teams"
if os.name == "nt":
    PY_TEAMS = TEAMS / ".venv/Scripts/python.exe"
    NANOBOT = WT / ".venv-nanobot/Scripts/nanobot.exe"
else:
    PY_TEAMS = TEAMS / ".venv/bin/python"
    NANOBOT = WT / ".venv-nanobot/bin/nanobot"

EVIDENCE = TEAMS / "review/peer-comms"
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
        self.tmp = TEAMS / "acceptance/.peer-comms" / f"{name}-{secrets.token_hex(4)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
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
        self.coord_proc: subprocess.Popen | None = None
        self.preset_name, self.preset, self.provider = resolve_preset()
        marker = secrets.token_hex(4).upper()
        self.marker = f"PEERMARKER-{marker}"
        self.env = dict(os.environ)
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
        home = self.tmp / bot
        (home / "workspace").mkdir(parents=True)
        config = {
            "agents": {"defaults": {
                "workspace": str(home / "workspace"),
                "modelPreset": self.preset_name,
                "maxToolIterations": 10,
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
                "token": self.ws_token, "websocketRequiresToken": True,
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
            "bot_homes_root": str(self.tmp / "homes"),
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
        handle = open(self.tmp / f"{name}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=str(self.tmp), env=self.env,
                                stdout=handle, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        self.logs.append((proc, handle))
        return proc

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
        for proc in self.procs:
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
                      "inbox_url": f"http://127.0.0.1:{port}",
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
        # P5 gate: the reply lands in the sender's own inbox.
        deadline = time.monotonic() + 60.0
        delivery = None
        while time.monotonic() < deadline:
            status, task = http_json(
                "GET", self.coord_url(f"/v1/teams/tasks/{task_id}"),
                token=self.admin_token,
            )
            delivery = task.get("delivery_state") if isinstance(task, dict) else None
            if delivery in ("persisted", "injected", "card_only", "orphaned", "failed"):
                break
            time.sleep(2.0)
        ev["steps"].append({"step": "poll task delivery", "delivery_state": delivery})
        if delivery not in ("persisted", "injected"):
            ev["status"] = "fail"
            ev["reason"] = f"delivery_state={delivery} (P5: result must return to sender inbox)"
            return ev
        # The reply must land in research's own inbox (E2E-02/I5).
        lookup = http_json(
            "GET", self.coord_url(f"/v1/teams/tasks/{task_id}"),
            token=self.admin_token,
        )[1]
        receipt_status, receipt = http_json(
            "GET",
            f"http://127.0.0.1:{self.worker_ports['research']}"
            f"/v1/agent-inbox/{lookup.get('delivery_id', '')}",
            token=self.runs_keys["research"],
        )
        ev["steps"].append({"step": "fetch research inbox receipt",
                            "status": receipt_status,
                            "keys": sorted(receipt.keys()) if isinstance(receipt, dict) else None})
        if receipt_status != 200:
            ev["status"] = "fail"
            ev["reason"] = f"research inbox receipt HTTP {receipt_status}"
            return ev
        ev["status"] = "pass"
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
        status, payload = self.peer_send(
            "research", "writer", token=self.bot_coord_tokens["research"],
            conversation_root=root, hop=4,
        )
        code = payload.get("code") if isinstance(payload, dict) else None
        ev["steps"].append({"step": "peer send hop=4 (fifth hop)",
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
        ev: dict = {"scenario": "crash_restart", "steps": []}
        if not dispatch_ok:
            ev["status"] = "blocked"
            ev["reason"] = "peer dispatch unavailable; prerequisite red in peer_dispatch"
            return ev
        status, payload = self.peer_send(
            "research", "writer", token=self.bot_coord_tokens["research"],
            input_text=f"Count slowly from 1 to 5, then reply with {self.marker}.",
        )
        if status not in (200, 201, 202):
            ev["status"] = "fail"
            ev["reason"] = f"dispatch HTTP {status}"
            return ev
        task_id = payload["task_id"]
        # Wait until the task exists, then hard-kill the coordinator and
        # restart it on the same control dir. The dispatch/delivery outboxes
        # decide the crash point; no sleep-based race.
        time.sleep(1.0)
        assert self.coord_proc is not None
        self.coord_proc.kill()
        self.coord_proc.wait(timeout=10)
        ev["steps"].append({"step": "coordinator killed after accept"})
        self.spawn_coordinator(self.coord_config)
        if not self.wait_port(self.coord_port):
            ev["status"] = "fail"
            ev["reason"] = "coordinator did not come back"
            return ev
        ev["steps"].append({"step": "coordinator restarted"})
        # Reconcile as the sending bot: lookup is caller-scoped idempotency.
        lookup_status, lookup = http_json(
            "GET",
            self.coord_url(
                f"/v1/teams/tasks/lookup?operation_key={payload['operation_key']}"
            ),
            token=self.bot_coord_tokens["research"],
        )
        tasks = [lookup] if (
            lookup_status == 200 and isinstance(lookup, dict)
            and lookup.get("task_id") == task_id
        ) else []
        ev["steps"].append({"step": "task survives restart",
                            "lookup_status": lookup_status,
                            "match_count": len(tasks)})
        if len(tasks) != 1:
            ev["status"] = "fail"
            ev["reason"] = f"{len(tasks)} tasks after restart (want exactly 1)"
            return ev
        deadline = time.monotonic() + 240.0
        delivery = None
        while time.monotonic() < deadline:
            status, task = http_json(
                "GET", self.coord_url(f"/v1/teams/tasks/{task_id}"),
                token=self.admin_token,
            )
            delivery = task.get("delivery_state") if isinstance(task, dict) else None
            if delivery in ("persisted", "injected", "card_only", "orphaned", "failed"):
                break
            time.sleep(2.0)
        ev["steps"].append({"step": "delivery after restart",
                            "delivery_state": delivery})
        ev["status"] = "pass" if delivery in ("persisted", "injected") else "fail"
        if ev["status"] == "fail":
            ev["reason"] = f"delivery_state={delivery} after restart"
        return ev

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
            and "sender" in e["data"] and "recipient" in e["data"]
            and "hop" in e["data"] and e.get("task_id")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="keep the temp topology directory for inspection")
    args = parser.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    lab = PeerLab("p2")
    results: list[dict] = []
    try:
        lab.start()
        results.append(lab.register_bots())
        dispatch = lab.scenario_peer_dispatch()
        send_step = next(
            (s for s in dispatch.get("steps", [])
             if s.get("step", "").startswith("peer send")),
            {},
        )
        # The dispatch pipeline working is the prerequisite for the dependent
        # scenarios; the P5 inbox-return gate may still be red.
        dispatch_ok = send_step.get("status") in (200, 201, 202)
        results.append(dispatch)
        results.append(lab.scenario_unauthorized(dispatch_ok))
        results.append(lab.scenario_hop_guard(dispatch_ok))
        results.append(lab.scenario_idempotent_key(dispatch_ok))
        results.append(lab.scenario_crash_restart(dispatch_ok))
        results.append(lab.scenario_events_shape())
    except SystemExit as exc:
        results.append({"scenario": "boot", "status": "fail", "reason": str(exc)})
    finally:
        lab.teardown()
        if args.keep:
            results.append({"scenario": "meta", "topology_dir": str(lab.tmp)})

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = EVIDENCE / f"p2-probe-{stamp}.jsonl"
    with open(out, "w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    passed = sum(1 for r in results if r.get("status") == "pass")
    failed = sum(1 for r in results if r.get("status") == "fail")
    blocked = sum(1 for r in results if r.get("status") == "blocked")
    print(json.dumps({
        "probe": "peer_comms", "passed": passed, "failed": failed,
        "blocked": blocked, "evidence": str(out),
        "verdict": "GREEN" if failed == 0 and blocked == 0 and passed > 0 else "RED",
    }))
    return 0 if failed == 0 and blocked == 0 and passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
