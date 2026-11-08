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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
TEAMS = ROOT.parent
OUT = TEAMS / ".e2e"
WT = TEAMS.parent
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
PLANNER_PORT = 8902
RESEARCH_KEY = secrets.token_urlsafe(24)
PLANNER_KEY = secrets.token_urlsafe(24)
PRESET = os.environ.get("NANOBOT_TEAMS_PRESET", "step-5-preview")


def log(message: str) -> None:
    print(message, flush=True)


def user_config() -> dict:
    return json.loads(USER_CONFIG.read_text(encoding="utf-8"))


def provider_config(preset: dict, source: dict) -> dict:
    provider = dict(source["providers"][preset["provider"]])
    override = os.environ.get("NANOBOT_TEAMS_API_BASE")
    if override:
        provider["apiBase"] = override
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


def process_env() -> dict:
    env = os.environ.copy()
    env["RESEARCH_API_KEY"] = RESEARCH_KEY
    env["PLANNER_API_KEY"] = PLANNER_KEY
    env["TEAMS_RESEARCH_API_KEY"] = RESEARCH_KEY
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


def start_stub(delay: float, status: int, content: str) -> ThreadingHTTPServer:
    StubHandler.delay = delay
    StubHandler.status = status
    StubHandler.body = content.encode("utf-8")
    StubHandler.hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", RESEARCH_PORT), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def reset() -> None:
    for name in ("research", "planner", "logs"):
        shutil.rmtree(OUT / name, ignore_errors=True)
    (OUT / "research/workspace").mkdir(parents=True, exist_ok=True)
    (OUT / "planner/workspace").mkdir(parents=True, exist_ok=True)


def research_config() -> Path:
    config = base_config(OUT / "research/workspace", RESEARCH_PORT, "${RESEARCH_API_KEY}", restrict=True)
    path = OUT / "research/config.json"
    write_json(path, config)
    return path


def planner_config() -> Path:
    config = base_config(OUT / "planner/workspace", PLANNER_PORT, "${PLANNER_API_KEY}", restrict=True)
    config["tools"]["mcpServers"] = {
        "teams": {
            "type": "stdio",
            "command": str(PY_TEAMS),
            "args": ["-m", "nanobot_teams", "--config", str(OUT / "teams.json")],
            "cwd": str(ROOT),
            "env": {"TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"},
            "toolTimeout": 180,
            "enabledTools": ["list_bots", "delegate_task"],
        }
    }
    path = OUT / "planner/config.json"
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
    write_teams(f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions")
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
            assert names == {"list_bots", "delegate_task"}, names
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
        config = write_teams(f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions")
        results = asyncio.run(_mcp_calls(config, 2))
        first_error, first = results[0]
        second_error, second = results[1]
        assert first_error and first["status"] == "outcome_unknown", first
        assert second_error and second["error_code"] == "blocked", second
        assert StubHandler.hits == 1, StubHandler.hits
        log("PASS unknown_block")
    finally:
        stub.shutdown()


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


SCENARIOS = {
    "research_direct": scenario_research_direct,
    "delegate_basic": scenario_delegate_basic,
    "session_persistence": scenario_session_persistence,
    "long_timeout": scenario_long_timeout,
    "unknown_block": scenario_unknown_block,
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
