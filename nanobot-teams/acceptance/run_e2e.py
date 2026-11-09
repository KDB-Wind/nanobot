#!/usr/bin/env python
"""End-to-end acceptance for the nanobot-teams MVP.

Runs two real Nanobot instances on the step-5-preview preset and drives the
Teams MCP server through the Planner agent loop. Secrets stay in process
environment variables and are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
TEAMS = ROOT.parent
OUT = TEAMS / ".e2e"
WT = Path(os.environ.get("NANOBOT_TEAMS_WORKTREE") or TEAMS.parent)
if os.name == "nt":
    PY_TEAMS = TEAMS / ".venv/Scripts/python.exe"
    PY_NB = WT / ".venv-nanobot/Scripts/python.exe"
    NANOBOT = WT / ".venv-nanobot/Scripts/nanobot.exe"
else:
    PY_TEAMS = TEAMS / ".venv/bin/python"
    PY_NB = WT / ".venv-nanobot/bin/python"
    NANOBOT = WT / ".venv-nanobot/bin/nanobot"
USER_CONFIG = Path.home() / ".nanobot/config.json"

RESEARCH_PORT = 8901
STUB_PORT = 8906
PLANNER_PORT = 8902
WRITER_PORT = 8903
RESEARCH_KEY = secrets.token_urlsafe(24)
PLANNER_KEY = secrets.token_urlsafe(24)
WRITER_KEY = secrets.token_urlsafe(24)
PRESET = os.environ.get("NANOBOT_TEAMS_PRESET", "step-5-preview")


def _package_version() -> str:
    text = (TEAMS / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise RuntimeError("pyproject.toml has no version line")


VERSION = _package_version()


def log(message: str) -> None:
    print(message, flush=True)


def user_config() -> dict:
    return json.loads(USER_CONFIG.read_text(encoding="utf-8"))


def _provider_env_name(name: str) -> str:
    safe = "".join(char if char.isalnum() else "_" for char in name).upper()
    return f"{safe}_API_KEY"


def provider_config(preset: dict, source: dict) -> dict:
    provider = dict(source["providers"][preset["provider"]])
    override = os.environ.get("NANOBOT_TEAMS_API_BASE")
    if override:
        provider["apiBase"] = override
    key = provider.get("apiKey")
    if isinstance(key, str) and key and not (key.startswith("$" + "{") and key.endswith("}")):
        provider["apiKey"] = "$" + "{" + _provider_env_name(preset["provider"]) + "}"
    return provider


def base_config(workspace: Path, port: int, api_key_ref: str, *, restrict: bool) -> dict:
    source = user_config()
    preset = source["modelPresets"][PRESET]
    return {
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "modelPreset": PRESET,
                "model": preset["model"],
                "provider": "auto",
                "maxTokens": 8192,
                "contextWindowTokens": 200000,
                "temperature": 0.1,
                "maxToolIterations": 30,
                "maxConcurrentSubagents": 1,
                "maxToolResultChars": 16000,
                "timezone": "Asia/Shanghai",
                "unifiedSession": False,
                "dream": {"enabled": False},
            }
        },
        "providers": {preset["provider"]: provider_config(preset, source)},
        "modelPresets": {PRESET: preset},
        "api": {
            "host": "127.0.0.1",
            "port": port,
            "timeout": 120,
            "apiKey": api_key_ref,
        },
        "gateway": {"host": "127.0.0.1", "port": port + 1000, "restartMode": "exit"},
        "tools": {
            "restrictToWorkspace": restrict,
            "exec": {"enable": False},
            "mcpServers": {},
        },
        "channels": {},
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_teams(endpoint: str) -> Path:
    path = OUT / "teams.json"
    write_json(
        path,
        {
            "schema_version": 1,
            "team_id": "e2e",
            "caller_bot_id": "planner",
            "thread_key": "main",
            "allowed_targets": ["research"],
            "bots": [
                {"id": "planner", "display_name": "Planner", "role": "planning"},
                {
                    "id": "research",
                    "display_name": "Research",
                    "role": "research",
                    "endpoint": endpoint,
                    "api_key_env": "TEAMS_RESEARCH_API_KEY",
                },
            ],
        },
    )
    return path


def write_teams_v2(research_endpoint: str, writer_endpoint: str) -> Path:
    path = OUT / "teams.json"
    write_json(
        path,
        {
            "schema_version": 2,
            "team_id": "e2e",
            "thread_key": "main",
            "bots": [
                {
                    "id": "planner",
                    "display_name": "Planner",
                    "role": "planning",
                    "capabilities": ["plan"],
                },
                {
                    "id": "research",
                    "display_name": "Research",
                    "role": "research",
                    "capabilities": ["search"],
                    "endpoint": research_endpoint,
                    "api_key_env": "TEAMS_RESEARCH_API_KEY",
                    "allowed_callers": ["planner"],
                },
                {
                    "id": "writer",
                    "display_name": "Writer",
                    "role": "drafting",
                    "capabilities": ["write"],
                    "endpoint": writer_endpoint,
                    "api_key_env": "TEAMS_WRITER_API_KEY",
                    "allowed_callers": ["planner"],
                },
            ],
        },
    )
    return path


def process_env() -> dict:
    env = os.environ.copy()
    env["RESEARCH_API_KEY"] = RESEARCH_KEY
    env["PLANNER_API_KEY"] = PLANNER_KEY
    env["WRITER_API_KEY"] = WRITER_KEY
    env["TEAMS_RESEARCH_API_KEY"] = RESEARCH_KEY
    env["TEAMS_WRITER_API_KEY"] = WRITER_KEY
    for name, block in (user_config().get("providers") or {}).items():
        if isinstance(block, dict):
            key = block.get("apiKey")
            if isinstance(key, str) and key and not key.startswith("$" + "{"):
                env[_provider_env_name(name)] = key
    return env


def start(name: str, args: list[str], env: dict) -> subprocess.Popen:
    logs = OUT / "logs"
    logs.mkdir(exist_ok=True)
    handle = open(logs / f"{name}.log", "ab")
    handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)} ===\n".encode())
    handle.flush()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        args,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=str(WT),
        creationflags=flags,
    )


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            text=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def start_nanobot(name: str, home: Path, config: Path, env: dict) -> subprocess.Popen:
    return start(
        name,
        [str(NANOBOT), "--home", str(home), "serve", "--config", str(config)],
        env,
    )


def wait_health(port: int, proc: subprocess.Popen, name: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with {proc.returncode}. See .e2e/logs/{name}.log")
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not become healthy on port {port}")


def chat(port: int, key: str, session_id: str, text: str, timeout: float = 300.0) -> str:
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": text}],
            "session_id": session_id,
            "stream": False,
        }
    ).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


class StubHandler(BaseHTTPRequestHandler):
    delay = 0.0
    status = 200
    body = b"{}"
    hits = 0
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        with type(self).lock:
            type(self).hits += 1
        if type(self).delay:
            time.sleep(type(self).delay)
        payload = {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "model": "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": type(self).body.decode()},
                    "finish_reason": "stop",
                }
            ],
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args) -> None:
        return


def start_stub(delay: float, status: int, content: str, port: int = STUB_PORT) -> ThreadingHTTPServer:
    StubHandler.delay = delay
    StubHandler.status = status
    StubHandler.body = content.encode("utf-8")
    StubHandler.hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", port), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait_ports_bindable(timeout: float = 90.0) -> None:
    """Block until no e2e port still has a live listener.

    The probe binds with SO_REUSEADDR so short-lived TIME_WAIT sockets from a
    just-closed peer do not stall the run: they never blocked a real server,
    only a plain bind. A process that still holds the port does.
    """
    for port in (RESEARCH_PORT, PLANNER_PORT, WRITER_PORT, MID_PORT, WRITER2_PORT):
        deadline = time.time() + timeout
        while True:
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", port))
                probe.close()
                break
            except OSError:
                probe.close()
                if time.time() > deadline:
                    raise RuntimeError(f"port {port} stayed busy for {timeout}s")
                time.sleep(0.5)


def reset() -> None:
    _wait_ports_bindable()
    for name in ("research", "planner", "writer", "mid", "logs"):
        shutil.rmtree(OUT / name, ignore_errors=True)
    (OUT / "research/workspace").mkdir(parents=True, exist_ok=True)
    (OUT / "planner/workspace").mkdir(parents=True, exist_ok=True)
    (OUT / "writer/workspace").mkdir(parents=True, exist_ok=True)
    (OUT / "mid/workspace").mkdir(parents=True, exist_ok=True)


def research_config() -> Path:
    config = base_config(OUT / "research/workspace", RESEARCH_PORT, "${RESEARCH_API_KEY}", restrict=True)
    path = OUT / "research/config.json"
    write_json(path, config)
    return path


def planner_config(
    caller: str | None = None,
    writer_target: bool = False,
    extra_mcp_env: dict[str, str] | None = None,
) -> Path:
    config = base_config(OUT / "planner/workspace", PLANNER_PORT, "${PLANNER_API_KEY}", restrict=True)
    args = ["-m", "nanobot_teams", "--config", str(OUT / "teams.json")]
    if caller:
        args += ["--caller", caller]
    mcp_env = {"TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"}
    if writer_target:
        mcp_env["TEAMS_WRITER_API_KEY"] = "${TEAMS_WRITER_API_KEY}"
    mcp_env.update(extra_mcp_env or {})
    config["tools"]["mcpServers"] = {
        "teams": {
            "type": "stdio",
            "command": str(PY_TEAMS),
            "args": args,
            "cwd": str(ROOT),
            "env": mcp_env,
            "toolTimeout": 180,
            "enabledTools": ["list_bots", "delegate_task"],
        }
    }
    path = OUT / "planner/config.json"
    write_json(path, config)
    return path


def writer_config() -> Path:
    config = base_config(OUT / "writer/workspace", WRITER_PORT, "${WRITER_API_KEY}", restrict=True)
    path = OUT / "writer/config.json"
    write_json(path, config)
    return path


def delegation_prompt(task: str) -> str:
    return (
        "You must use the Teams tools. Call mcp_teams_list_bots once, then call "
        "mcp_teams_delegate_task with bot_id research and this exact task:\n\n"
        f"{task}\n\n"
        "Do not answer from your own knowledge. After the tool returns, reply with "
        "the returned content and nothing else."
    )


def scenario_research_direct() -> None:
    reset()
    config = research_config()
    env = process_env()
    proc = start_nanobot("research", OUT / "research", config, env)
    try:
        wait_health(RESEARCH_PORT, proc, "research")
        answer = chat(RESEARCH_PORT, RESEARCH_KEY, "direct-1", "Reply with the single word BANANA.")
        assert "BANANA" in answer, answer
        log("PASS research_direct")
    finally:
        stop(proc)


def scenario_delegate_basic() -> None:
    reset()
    nonce = f"SECRET-{secrets.token_hex(4).upper()}"
    (OUT / "research/workspace/secret.txt").write_text(nonce, encoding="utf-8")
    research = research_config()
    write_teams(f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions")
    planner = planner_config()
    env = process_env()
    research_proc = start_nanobot("research", OUT / "research", research, env)
    planner_proc = None
    try:
        wait_health(RESEARCH_PORT, research_proc, "research")
        planner_proc = start_nanobot("planner", OUT / "planner", planner, env)
        wait_health(PLANNER_PORT, planner_proc, "planner")
        answer = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-basic",
            delegation_prompt(
                "Read the file secret.txt in your workspace with your file tool. "
                "Reply with only its exact contents and nothing else."
            ),
        )
        assert nonce in answer, answer
        log("PASS delegate_basic")
    finally:
        stop(planner_proc)
        stop(research_proc)


def scenario_session_persistence() -> None:
    reset()
    code = f"CODE-{secrets.token_hex(4).upper()}"
    research = research_config()
    write_teams(f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions")
    planner = planner_config()
    env = process_env()
    research_proc = start_nanobot("research", OUT / "research", research, env)
    planner_proc = None
    try:
        wait_health(RESEARCH_PORT, research_proc, "research")
        planner_proc = start_nanobot("planner", OUT / "planner", planner, env)
        wait_health(PLANNER_PORT, planner_proc, "planner")
        first = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-a",
            delegation_prompt(
                f"Remember this code for later in this session: {code}. "
                "Reply with only OK."
            ),
        )
        assert "OK" in first.upper(), first
        stop(research_proc)
        research_proc = start_nanobot("research", OUT / "research", research, env)
        wait_health(RESEARCH_PORT, research_proc, "research")
        second = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-b",
            delegation_prompt(
                "What code did I ask you to remember earlier in this session? "
                "Reply with only the code."
            ),
        )
        assert code in second, second
        log("PASS session_persistence")
    finally:
        stop(planner_proc)
        stop(research_proc)


def scenario_long_timeout() -> None:
    reset()
    stub = start_stub(35.0, 200, "SLOW-OK")
    write_teams(f"http://127.0.0.1:{STUB_PORT}/v1/chat/completions")
    planner = planner_config()
    env = process_env()
    planner_proc = None
    try:
        planner_proc = start_nanobot("planner", OUT / "planner", planner, env)
        wait_health(PLANNER_PORT, planner_proc, "planner")
        started = time.time()
        answer = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-slow",
            delegation_prompt("Reply with only SLOW-OK."),
            timeout=300,
        )
        elapsed = time.time() - started
        assert "SLOW-OK" in answer, answer
        assert elapsed > 30, elapsed
        log(f"PASS long_timeout elapsed={elapsed:.1f}s")
    finally:
        stop(planner_proc)
        stub.shutdown()
        stub.server_close()


async def _mcp_calls(config: Path, count: int) -> list[tuple[bool, dict]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PY_TEAMS),
        args=["-m", "nanobot_teams", "--config", str(config)],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
        cwd=str(OUT),
    )
    results: list[tuple[bool, dict]] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"list_bots", "delegate_task", "list_tasks", "get_task"}, names
            for index in range(count):
                result = await session.call_tool(
                    "delegate_task",
                    {"bot_id": "research", "task": f"task {index}"},
                )
                results.append((result.isError, json.loads(result.content[0].text)))
    return results


def scenario_unknown_block() -> None:
    reset()
    stub = start_stub(0.0, 500, "boom")
    try:
        config = write_teams(f"http://127.0.0.1:{STUB_PORT}/v1/chat/completions")
        results = asyncio.run(_mcp_calls(config, 2))
        first_error, first = results[0]
        second_error, second = results[1]
        assert first_error and first["status"] == "outcome_unknown", first
        assert second_error and second["error_code"] == "blocked", second
        assert StubHandler.hits == 1, StubHandler.hits
        log("PASS unknown_block")
    finally:
        stub.shutdown()
        stub.server_close()


def scenario_multi_bot() -> None:
    reset()
    research_nonce = f"R-{secrets.token_hex(4).upper()}"
    writer_nonce = f"W-{secrets.token_hex(4).upper()}"
    (OUT / "research/workspace/secret.txt").write_text(research_nonce, encoding="utf-8")
    (OUT / "writer/workspace/secret.txt").write_text(writer_nonce, encoding="utf-8")
    write_teams_v2(
        f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions",
        f"http://127.0.0.1:{WRITER_PORT}/v1/chat/completions",
    )
    research = research_config()
    writer = writer_config()
    planner = planner_config(caller="planner", writer_target=True)
    env = process_env()
    research_proc = None
    writer_proc = None
    planner_proc = None
    try:
        research_proc = start_nanobot("research", OUT / "research", research, env)
        wait_health(RESEARCH_PORT, research_proc, "research")
        writer_proc = start_nanobot("writer", OUT / "writer", writer, env)
        wait_health(WRITER_PORT, writer_proc, "writer")
        planner_proc = start_nanobot("planner", OUT / "planner", planner, env)
        wait_health(PLANNER_PORT, planner_proc, "planner")
        answer = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-multi",
            "You must use the Teams tools. Call mcp_teams_list_bots once, then call "
            "mcp_teams_delegate_task twice: first with bot_id research, then with "
            "bot_id writer. Give both this exact task:\n\n"
            "Read the file secret.txt in your workspace with your file tool. "
            "Reply with only its exact contents and nothing else.\n\n"
            "Do not answer from your own knowledge. After both tools return, reply "
            "with only RESEARCH=<research answer> WRITER=<writer answer>.",
            timeout=420,
        )
        assert research_nonce in answer, answer
        assert writer_nonce in answer, answer
        log("PASS multi_bot")
    finally:
        stop(planner_proc)
        stop(writer_proc)
        stop(research_proc)


async def _unauthorized_mcp_calls(config: Path) -> tuple[dict, dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PY_TEAMS),
        args=["-m", "nanobot_teams", "--config", str(config), "--caller", "writer"],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
        cwd=str(OUT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.call_tool("list_bots", {})
            refused = await session.call_tool(
                "delegate_task", {"bot_id": "research", "task": "should never send"}
            )
            return json.loads(listed.content[0].text), json.loads(refused.content[0].text)


def scenario_unauthorized_pair() -> None:
    reset()
    stub = start_stub(0.0, 200, "unused")
    try:
        config = write_teams_v2(
            f"http://127.0.0.1:{STUB_PORT}/v1/chat/completions",
            f"http://127.0.0.1:{WRITER_PORT}/v1/chat/completions",
        )
        listed, refused = asyncio.run(_unauthorized_mcp_calls(config))
        assert listed == {"bots": []}, listed
        assert refused["error_code"] == "invalid_target", refused
        assert StubHandler.hits == 0, StubHandler.hits
        log("PASS unauthorized_pair")
    finally:
        stub.shutdown()
        stub.server_close()


def scenario_plugin_manifest() -> None:
    reset()
    target = OUT / "planner/workspace/plugins/nanobot-teams"
    shutil.copytree(TEAMS / "agent-plugin/nanobot-teams", target)
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from nanobot.agent.plugins import discover_agent_plugins\n"
        "from nanobot.agent.skill_metadata import parse_skill_metadata, valid_skill_metadata\n"
        "workspace = Path(sys.argv[1])\n"
        "plugins = discover_agent_plugins(workspace)\n"
        "skill = workspace / 'plugins/nanobot-teams/skills/teams/SKILL.md'\n"
        "meta = parse_skill_metadata(skill.read_text(encoding='utf-8'))\n"
        "print(json.dumps({\n"
        "  'plugins': [{'name': p.name, 'display': p.display_name, 'mcp': list(p.mcp_servers)} for p in plugins],\n"
        "  'skill_valid': bool(meta) and valid_skill_metadata(meta, 'teams'),\n"
        "}))\n"
    )
    result = subprocess.run(
        [str(PY_NB), "-c", code, str(OUT / "planner/workspace")],
        capture_output=True,
        text=True,
        cwd=str(WT),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["plugins"] == [
        {"name": "nanobot-teams", "display": "Nanobot Teams", "mcp": []}
    ], payload
    assert payload["skill_valid"], payload
    log("PASS plugin_manifest")


MID_PORT = 8904
MID_KEY = secrets.token_urlsafe(24)


def write_teams_chain(research_port: int = RESEARCH_PORT) -> Path:
    path = OUT / "teams.json"
    write_json(
        path,
        {
            "schema_version": 2,
            "team_id": "e2e-chain",
            "thread_key": "main",
            "bots": [
                {"id": "planner", "display_name": "Planner", "role": "planning"},
                {
                    "id": "mid",
                    "display_name": "Mid",
                    "role": "middle hop",
                    "endpoint": f"http://127.0.0.1:{MID_PORT}/v1/chat/completions",
                    "api_key_env": "TEAMS_MID_API_KEY",
                    "allowed_callers": ["planner"],
                },
                {
                    "id": "research",
                    "display_name": "Research",
                    "role": "research",
                    "endpoint": f"http://127.0.0.1:{research_port}/v1/chat/completions",
                    "api_key_env": "TEAMS_RESEARCH_API_KEY",
                    "allowed_callers": ["planner", "mid"],
                },
            ],
        },
    )
    return path


def mid_config() -> Path:
    config = base_config(OUT / "mid/workspace", MID_PORT, "${MID_API_KEY}", restrict=True)
    config["tools"]["mcpServers"] = {
        "teams": {
            "type": "stdio",
            "command": str(PY_TEAMS),
            "args": ["-m", "nanobot_teams", "--config", str(OUT / "teams.json"), "--caller", "mid"],
            "cwd": str(ROOT),
            "env": {"TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"},
            "toolTimeout": 180,
            "enabledTools": ["list_bots", "delegate_task"],
        }
    }
    path = OUT / "mid/config.json"
    write_json(path, config)
    return path


def chain_process_env() -> dict:
    env = process_env()
    env["MID_API_KEY"] = MID_KEY
    env["TEAMS_MID_API_KEY"] = MID_KEY
    return env


def scenario_chain_abc() -> None:
    reset()
    nonce = f"C-{secrets.token_hex(4).upper()}"
    (OUT / "research/workspace/secret.txt").write_text(nonce, encoding="utf-8")
    shutil.copytree(
        TEAMS / "agent-plugin/nanobot-teams",
        OUT / "mid/workspace/plugins/nanobot-teams",
    )
    write_teams_chain()
    research = research_config()
    mid = mid_config()
    planner = planner_config(
        caller="planner", extra_mcp_env={"TEAMS_MID_API_KEY": "${TEAMS_MID_API_KEY}"}
    )
    env = chain_process_env()
    research_proc = None
    mid_proc = None
    planner_proc = None
    try:
        research_proc = start_nanobot("research", OUT / "research", research, env)
        wait_health(RESEARCH_PORT, research_proc, "research")
        mid_proc = start_nanobot("mid", OUT / "mid", mid, env)
        wait_health(MID_PORT, mid_proc, "mid")
        planner_proc = start_nanobot("planner", OUT / "planner", planner, env)
        wait_health(PLANNER_PORT, planner_proc, "planner")
        answer = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-chain",
            "You must use the Teams tools. Call mcp_teams_delegate_task once with "
            "bot_id mid and exactly this task:\n\n"
            "Your task text starts with a [teams-envelope] block. Call "
            "mcp_teams_delegate_task with bot_id research and this task:\n"
            "Read the file secret.txt in your workspace with your file tool. "
            "Reply with only its exact contents and nothing else.\n"
            "Copy root_task_id, hop, deadline_remaining_s, and chain exactly from "
            "your [teams-envelope] block into the call, and pass parent_task_id "
            "with the block's task_id value. Do not change any value.\n"
            "After the tool returns, reply with only the returned content.\n\n"
            "Do not answer from your own knowledge. After the tool returns, reply "
            "with the returned content and nothing else.",
            timeout=420,
        )
        assert nonce in answer, answer
        log("PASS chain_abc")
    finally:
        stop(planner_proc)
        stop(mid_proc)
        stop(research_proc)


async def _chain_mcp_calls(config: Path, calls: list[dict]) -> list[tuple[bool, dict]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PY_TEAMS),
        args=["-m", "nanobot_teams", "--config", str(config), "--caller", "mid"],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
        cwd=str(OUT),
    )
    results: list[tuple[bool, dict]] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for call in calls:
                result = await session.call_tool("delegate_task", call)
                results.append((result.isError, json.loads(result.content[0].text)))
    return results


def _chain_call(**overrides) -> dict:
    call: dict = {
        "bot_id": "research",
        "task": "should not matter",
        "parent_task_id": str(uuid.uuid4()),
        "root_task_id": str(uuid.uuid4()),
        "hop": 2,
        "deadline_remaining_s": 60.0,
        "chain": ["planner", "mid"],
    }
    call.update(overrides)
    return call


def scenario_chain_loop() -> None:
    reset()
    stub = start_stub(0.0, 200, "unused")
    try:
        config = write_teams_chain(research_port=STUB_PORT)
        results = asyncio.run(_chain_mcp_calls(config, [
            _chain_call(chain=["research", "mid"]),
        ]))
        error, payload = results[0]
        assert error and payload["error_code"] == "loop_detected", payload
        assert StubHandler.hits == 0, StubHandler.hits
        log("PASS chain_loop")
    finally:
        stub.shutdown()
        stub.server_close()


def scenario_chain_budget() -> None:
    reset()
    stub = start_stub(8.0, 200, "unused")
    try:
        config = write_teams_chain(research_port=STUB_PORT)
        results = asyncio.run(_chain_mcp_calls(config, [
            _chain_call(deadline_remaining_s=0),
            _chain_call(deadline_remaining_s=5.0),
        ]))
        error, exhausted = results[0]
        assert error and exhausted["error_code"] == "budget_exhausted", exhausted
        error, timed_out = results[1]
        assert error and timed_out["status"] == "timed_out", timed_out
        assert timed_out["error_code"] == "deadline_exceeded", timed_out
        assert timed_out["status"] != "completed"
        assert StubHandler.hits == 1, StubHandler.hits
        log("PASS chain_budget")
    finally:
        stub.shutdown()
        stub.server_close()


WRITER2_PORT = 8905
WRITER2_KEY = secrets.token_urlsafe(24)


def scenario_bot_init_live() -> None:
    reset()
    nonce = f"GEN-{secrets.token_hex(4).upper()}"
    write_teams_v2(
        f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions",
        f"http://127.0.0.1:{WRITER2_PORT}/v1/chat/completions",
    )
    # shrink the registry to planner+research only, then let the CLI add writer2
    registry_path = OUT / "teams.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["bots"] = [bot for bot in registry["bots"] if bot["id"] in ("planner", "research")]
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    env = process_env()
    env["TEAMS_WRITER2_API_KEY"] = WRITER2_KEY
    env.pop("TEAMS_WRITER_API_KEY", None)
    home = OUT / "writer2"

    init = subprocess.run(
        [
            str(PY_TEAMS), "-m", "nanobot_teams", "bot", "init", "writer2",
            "--registry", str(registry_path),
            "--home", str(home),
            "--port", str(WRITER2_PORT),
            "--preset", PRESET,
            "--role", "generated writer",
            "--allow", "planner",
        ],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    assert init.returncode == 0, init.stdout + init.stderr
    assert (home / "config.json").exists()
    assert "TEAMS_WRITER2_API_KEY" in init.stdout

    registry_after = json.loads(registry_path.read_text(encoding="utf-8"))
    ids = [bot["id"] for bot in registry_after["bots"]]
    assert ids == ["planner", "research", "writer2"], ids

    (home / "workspace/secret.txt").write_text(nonce, encoding="utf-8")

    pre_check = subprocess.run(
        [str(PY_TEAMS), "-m", "nanobot_teams", "check", "--config", str(registry_path)],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    assert "endpoint_down" in pre_check.stdout, pre_check.stdout

    planner = planner_config(
        caller="planner",
        extra_mcp_env={"TEAMS_WRITER2_API_KEY": "${TEAMS_WRITER2_API_KEY}"},
    )
    research_proc = None
    writer2_proc = None
    planner_proc = None
    try:
        writer2_proc = start_nanobot("writer2", home, home / "config.json", env)
        wait_health(WRITER2_PORT, writer2_proc, "writer2")

        post_check = subprocess.run(
            [str(PY_TEAMS), "-m", "nanobot_teams", "check", "--config", str(registry_path)],
            capture_output=True, text=True, env=env, cwd=str(ROOT),
        )
        assert post_check.returncode == 0, post_check.stdout + post_check.stderr
        assert "writer2: something answers" in post_check.stdout, post_check.stdout

        planner_proc = start_nanobot("planner", OUT / "planner", planner, env)
        wait_health(PLANNER_PORT, planner_proc, "planner")
        answer = chat(
            PLANNER_PORT,
            PLANNER_KEY,
            "planner-init",
            "You must use the Teams tools. Call mcp_teams_delegate_task once with "
            "bot_id writer2 and exactly this task:\n\n"
            "Read the file secret.txt in your workspace with your file tool. "
            "Reply with only its exact contents and nothing else.\n\n"
            "Do not answer from your own knowledge. After the tool returns, reply "
            "with the returned content and nothing else.",
            timeout=420,
        )
        assert nonce in answer, answer
        log("PASS bot_init_live")
    finally:
        stop(planner_proc)
        stop(writer2_proc)
        stop(research_proc)


def scenario_check_errors() -> None:
    reset()
    registry_path = OUT / "teams.json"
    registry = {
        "schema_version": 2,
        "team_id": "e2e",
        "thread_key": "main",
        "bots": [
            {"id": "planner", "display_name": "Planner", "role": "planning"},
            {
                "id": "research",
                "display_name": "Research",
                "role": "research",
                "model_preset": "no-such-preset",
                "endpoint": f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions",
                "api_key_env": "TEAMS_RESEARCH_API_KEY",
                "allowed_callers": ["planner"],
            },
        ],
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    env = {k: v for k, v in process_env().items() if not k.startswith("TEAMS_")}
    result = subprocess.run(
        [str(PY_TEAMS), "-m", "nanobot_teams", "check", "--config", str(registry_path)],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    assert result.returncode == 1, result.stdout
    assert "missing_env" in result.stdout, result.stdout
    assert "unknown_preset" in result.stdout, result.stdout
    log("PASS check_errors")


async def _ledger_first_session(config: Path, ledger_path: Path) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PY_TEAMS),
        args=["-m", "nanobot_teams", "--config", str(config), "--ledger", str(ledger_path)],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
        cwd=str(OUT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "delegate_task", {"bot_id": "research", "task": "ledger-probe-task-alpha"}
            )
            payload = json.loads(result.content[0].text)
            assert payload["status"] == "completed", payload
            return payload["task_id"]


async def _ledger_second_session(config: Path, ledger_path: Path, task_id: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PY_TEAMS),
        args=["-m", "nanobot_teams", "--config", str(config), "--ledger", str(ledger_path)],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
        cwd=str(OUT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.call_tool("list_tasks", {"limit": 10})
            rows = json.loads(listed.content[0].text)["tasks"]
            assert len(rows) == 1, rows
            assert rows[0]["task_id"] == task_id
            assert rows[0]["status"] == "completed"
            fetched = await session.call_tool("get_task", {"task_id": task_id})
            return json.loads(fetched.content[0].text)


def scenario_ledger_restart() -> None:
    reset()
    (OUT / "ledger.sqlite3").unlink(missing_ok=True)
    stub = start_stub(0.0, 200, "ledger-probe-answer-alpha")
    try:
        config = write_teams(f"http://127.0.0.1:{STUB_PORT}/v1/chat/completions")
        ledger_path = OUT / "ledger.sqlite3"
        task_id = asyncio.run(_ledger_first_session(config, ledger_path))
        row = asyncio.run(_ledger_second_session(config, ledger_path, task_id))
        assert row["target"] == "research"
        assert row["observation"]
        blob = ledger_path.read_bytes()
        assert b"ledger-probe-task-alpha" not in blob
        assert b"ledger-probe-answer-alpha" not in blob
        assert RESEARCH_KEY.encode() not in blob
        log("PASS ledger_restart")
    finally:
        stub.shutdown()
        stub.server_close()


async def _budget_mcp_calls(config: Path, ledger_path: Path) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PY_TEAMS),
        args=[
            "-m",
            "nanobot_teams",
            "--config",
            str(config),
            "--caller",
            "planner",
            "--ledger",
            str(ledger_path),
        ],
        env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
        cwd=str(OUT),
    )
    results = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for index in range(3):
                result = await session.call_tool(
                    "delegate_task", {"bot_id": "research", "task": f"budget probe {index}"}
                )
                results.append(json.loads(result.content[0].text))
    return results


def scenario_budget_reject() -> None:
    reset()
    (OUT / "ledger.sqlite3").unlink(missing_ok=True)
    stub = start_stub(0.0, 200, "budget-answer")
    try:
        config = write_teams_v2(
            f"http://127.0.0.1:{STUB_PORT}/v1/chat/completions",
            f"http://127.0.0.1:{WRITER_PORT}/v1/chat/completions",
        )
        registry = json.loads(config.read_text(encoding="utf-8"))
        registry["bots"] = [
            bot for bot in registry["bots"] if bot["id"] in ("planner", "research")
        ]
        registry["budgets"] = {"planner": {"delegations": 2}}
        config.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        ledger_path = OUT / "ledger.sqlite3"

        results = asyncio.run(_budget_mcp_calls(config, ledger_path))
        assert [row["status"] for row in results] == [
            "completed",
            "completed",
            "failed",
        ], results
        assert results[2]["error_code"] == "budget_exceeded", results[2]
        assert StubHandler.hits == 2, StubHandler.hits

        graph = subprocess.run(
            [str(PY_TEAMS), "-m", "nanobot_teams", "graph", "--config", str(config)],
            capture_output=True, text=True, env=os.environ, cwd=str(ROOT),
        )
        assert graph.returncode == 0, graph.stdout + graph.stderr
        assert "planner --> research" in graph.stdout

        budget = subprocess.run(
            [
                str(PY_TEAMS), "-m", "nanobot_teams", "budget",
                "--config", str(config), "--ledger", str(ledger_path),
            ],
            capture_output=True, text=True, env=os.environ, cwd=str(ROOT),
        )
        assert budget.returncode == 0, budget.stdout + budget.stderr
        assert "2/2 OVER" in budget.stdout, budget.stdout

        audit = subprocess.run(
            [
                str(PY_TEAMS), "-m", "nanobot_teams", "audit",
                "--ledger", str(ledger_path),
            ],
            capture_output=True, text=True, env=os.environ, cwd=str(ROOT),
        )
        assert audit.returncode == 0, audit.stdout + audit.stderr
        assert "planner -> research" in audit.stdout
        assert "completed=2" in audit.stdout
        log("PASS budget_reject")
    finally:
        stub.shutdown()
        stub.server_close()


def scenario_packaging_fresh() -> None:
    reset()
    stub = start_stub(0.0, 200, "fresh-env-answer")
    try:
        dist = OUT / "dist"
        shutil.rmtree(dist, ignore_errors=True)
        dist.mkdir(parents=True)
        build = subprocess.run(
            [str(PY_TEAMS), "-m", "build", "--wheel", "--outdir", str(dist), str(TEAMS)],
            capture_output=True, text=True, env=os.environ, cwd=str(TEAMS),
        )
        assert build.returncode == 0, build.stdout + build.stderr
        wheel = next(dist.glob("nanobot_teams-*.whl"))
        assert VERSION in wheel.name, wheel.name

        fresh = OUT / "fresh"
        shutil.rmtree(fresh, ignore_errors=True)
        venv_result = subprocess.run(
            [str(PY_TEAMS), "-m", "venv", str(fresh)],
            capture_output=True, text=True, env=os.environ,
        )
        assert venv_result.returncode == 0, venv_result.stderr
        fresh_py = fresh / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip_env = os.environ.copy()

        install = subprocess.run(
            [str(fresh_py), "-m", "pip", "install", "--quiet", str(wheel)],
            capture_output=True, text=True, env=pip_env,
        )
        assert install.returncode == 0, install.stdout + install.stderr

        probe = subprocess.run(
            [str(fresh_py), "-c", "import nanobot_teams; print(nanobot_teams.__version__)"],
            capture_output=True, text=True, env=pip_env,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == VERSION, probe.stdout

        config = write_teams(f"http://127.0.0.1:{STUB_PORT}/v1/chat/completions")

        async def fresh_session() -> dict:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=str(fresh_py),
                args=["-m", "nanobot_teams", "--config", str(config)],
                env={**os.environ, "TEAMS_RESEARCH_API_KEY": RESEARCH_KEY},
                cwd=str(OUT),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("list_bots", {})
                    result = await session.call_tool(
                        "delegate_task", {"bot_id": "research", "task": "fresh probe"}
                    )
                    return json.loads(result.content[0].text)

        payload = asyncio.run(fresh_session())
        assert payload["status"] == "completed", payload
        assert payload["content"] == "fresh-env-answer"

        uninstall = subprocess.run(
            [str(fresh_py), "-m", "pip", "uninstall", "-y", "--quiet", "nanobot-teams"],
            capture_output=True, text=True, env=pip_env,
        )
        assert uninstall.returncode == 0, uninstall.stderr
        gone = subprocess.run(
            [str(fresh_py), "-c", "import nanobot_teams"],
            capture_output=True, text=True, env=pip_env,
        )
        assert gone.returncode != 0, gone.stdout
        log("PASS packaging_fresh")
    finally:
        stub.shutdown()
        stub.server_close()


SCENARIOS = {
    "research_direct": scenario_research_direct,
    "delegate_basic": scenario_delegate_basic,
    "session_persistence": scenario_session_persistence,
    "long_timeout": scenario_long_timeout,
    "unknown_block": scenario_unknown_block,
    "multi_bot": scenario_multi_bot,
    "unauthorized_pair": scenario_unauthorized_pair,
    "chain_abc": scenario_chain_abc,
    "chain_loop": scenario_chain_loop,
    "chain_budget": scenario_chain_budget,
    "bot_init_live": scenario_bot_init_live,
    "check_errors": scenario_check_errors,
    "ledger_restart": scenario_ledger_restart,
    "budget_reject": scenario_budget_reject,
    "packaging_fresh": scenario_packaging_fresh,
    "plugin_manifest": scenario_plugin_manifest,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    args = parser.parse_args()
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for name in names:
        log(f"RUN {name}")
        SCENARIOS[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
