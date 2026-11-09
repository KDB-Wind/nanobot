"""Run repaired Teams against two real Nanobot processes and an approved preset.

Run with ../.venv-nanobot/Scripts/python.exe acceptance/repair_live.py.
Creates a new .e2e/repair-* directory. Never modifies the user's installation.
Provider credentials are resolved in memory and passed through child environments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nanobot.config.loader import load_config, resolve_config_env_vars  # noqa: E402

from nanobot_teams.ledger import Ledger  # noqa: E402
from nanobot_teams.lifecycle import bot_init  # noqa: E402


def free_port() -> int:
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port <= 64535:
            return port


def export_refs(copied, original, env: dict) -> None:
    if isinstance(copied, dict):
        for key, value in copied.items():
            if key in original:
                export_refs(value, original[key], env)
    elif isinstance(copied, list):
        for value, source in zip(copied, original, strict=True):
            export_refs(value, source, env)
    elif isinstance(copied, str) and re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", copied):
        assert isinstance(original, str)
        env[copied[2:-1]] = original


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=15)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def healthy(client: httpx.AsyncClient, port: int, process: subprocess.Popen) -> None:
    async with asyncio.timeout(45):
        while True:
            if process.poll() is not None:
                raise RuntimeError("isolated Nanobot process exited; inspect the local run logs")
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.2)


async def run(preset_name: str) -> None:
    out = ROOT / ".e2e" / ("repair-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    out.mkdir(parents=True, mode=0o700)
    results: list[dict] = []

    def record(case: str) -> None:
        item = {"case": case, "status": "PASS", "preset": preset_name}
        results.append(item)
        (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(item), flush=True)

    source_path = Path.home() / ".nanobot" / "config.json"
    source = load_config(source_path)
    preset = resolve_config_env_vars(source.model_presets[preset_name])
    provider = resolve_config_env_vars(getattr(source.providers, preset.provider))
    source_preset = preset.model_dump(by_alias=True, mode="json", exclude_none=True)
    source_provider = provider.model_dump(by_alias=True, mode="json", exclude_none=True)
    target_port, planner_port = free_port(), free_port()
    target_key, planner_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    registry = out / "teams.json"
    registry.write_text(json.dumps({
        "schema_version": 2, "team_id": "repair-live", "thread_key": "test",
        "bots": [{"id": "planner", "display_name": "Planner", "role": "plans",
                  "can_start_root": True}],
    }), encoding="utf-8")
    home = out / "research"
    _, findings = bot_init("research", registry_path=registry, home=home, port=target_port,
        preset=preset_name, role="reads its isolated workspace", allowed_callers=["planner"],
        user_config=source_path)
    assert not [item for item in findings if item.severity == "error"]
    generated = json.loads((home / "config.json").read_text(encoding="utf-8"))
    env = dict(os.environ)
    export_refs(generated["providers"][preset.provider], source_provider, env)
    export_refs(generated["modelPresets"][preset_name], source_preset, env)
    env["TEAMS_RESEARCH_API_KEY"] = target_key
    env["REPAIR_PLANNER_KEY"] = planner_key
    os.environ.update(env)
    loaded = resolve_config_env_vars(load_config(home / "config.json"))
    assert loaded.api.api_key == target_key
    assert loaded.model_presets[preset_name].model == preset.model
    for value in [source_provider.get("apiKey"), target_key, planner_key]:
        if value:
            assert value not in (home / "config.json").read_text(encoding="utf-8")
    record("generated_config_real_loader")

    nonce = "ONLY_RESEARCH_" + secrets.token_hex(12)
    (home / "workspace" / "secret.txt").write_text(nonce, encoding="utf-8")
    planner_home = out / "planner"
    planner_workspace = planner_home / "workspace"
    planner_workspace.mkdir(parents=True)
    planner = json.loads(json.dumps(generated))
    planner["agents"]["defaults"]["workspace"] = str(planner_workspace)
    planner["api"].update(port=planner_port, apiKey="${REPAIR_PLANNER_KEY}", timeout=210)
    planner["gateway"]["port"] = planner_port + 1000
    ledger = out / "control" / "planner.sqlite3"
    teams_python = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    mcp_args = ["-m", "nanobot_teams", "--config", str(registry), "--caller", "planner", "--ledger", str(ledger)]
    planner["tools"]["mcpServers"] = {"teams": {
        "type": "stdio", "command": str(teams_python), "args": mcp_args,
        "cwd": str(ROOT), "env": {"TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"},
        "toolTimeout": 180,
    }}
    planner_path = planner_home / "config.json"
    planner_path.write_text(json.dumps(planner, indent=2), encoding="utf-8")
    nb_cli = Path(sys.executable).parent / ("nanobot.exe" if os.name == "nt" else "nanobot")
    processes = []

    def start(name: str, path: Path) -> subprocess.Popen:
        with (out / f"{name}.log").open("ab") as log:
            process = subprocess.Popen([str(nb_cli), "--home", str(path), "serve", "--config", str(path / "config.json")],
                cwd=ROOT.parent, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)
        return process

    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            research = start("research", home)
            await healthy(client, target_port, research)
            planner_process = start("planner", planner_home)
            await healthy(client, planner_port, planner_process)
            answer = await client.post(f"http://127.0.0.1:{planner_port}/v1/chat/completions",
                headers={"Authorization": f"Bearer {planner_key}"}, timeout=220, json={
                    "session_id": "live-delegation",
                    "messages": [{"role": "user", "content":
                        "Use mcp_teams_delegate_task exactly once with bot_id research. Ask it to read "
                        "secret.txt in its own workspace using its read_file tool and return the exact "
                        "contents. You cannot read that file. After the tool returns, reply only with "
                        "its content. Do not guess or substitute any text."}],
                })
            assert answer.status_code == 200
            assert nonce in answer.json()["choices"][0]["message"]["content"]
            record("planner_mcp_research_workspace_nonce")
            stop(planner_process)
            params = StdioServerParameters(command=str(teams_python), args=mcp_args, cwd=str(ROOT),
                env={"TEAMS_RESEARCH_API_KEY": target_key})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    code = "REMEMBER_" + secrets.token_hex(10)
                    first = await session.call_tool("delegate_task", {
                        "bot_id": "research", "task": f"Remember the code {code} for this conversation. Reply only OK."})
                    first_data = json.loads(first.content[0].text)
                    assert first_data["status"] == "completed"
                    stop(research)
                    research = start("research-restarted", home)
                    await healthy(client, target_port, research)
                    second = await session.call_tool("delegate_task", {
                        "bot_id": "research", "task": "What exact code did I ask you to remember? Reply only with the code.",
                        "thread_key": first_data["thread_key"]})
                    second_data = json.loads(second.content[0].text)
                    assert second_data["status"] == "completed" and code in second_data["content"]
                    record("explicit_thread_after_target_restart")
            book = Ledger(ledger, read_only=True)
            try:
                rows = book.list_tasks()
                assert len(rows) == 3
                continued = [row for row in rows if row.thread_key == first_data["thread_key"]]
                assert len(continued) == 2 and continued[0].session_id == continued[1].session_id
                assert all(row.status == "completed" and not row.blocked for row in rows)
                assert all(nonce not in row.to_json() and code not in row.to_json() for row in rows)
            finally:
                book.close()
            record("durable_content_free_observations")
    finally:
        for process in reversed(processes):
            stop(process)
    print(json.dumps({"results": str(out / "results.json"), "passed": len(results)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="glm-5.3-flash")
    args = parser.parse_args()
    asyncio.run(run(args.preset))
