#!/usr/bin/env python
"""WebUI acceptance: enable the Teams plugin in Apps, then delegate from chat."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import run_e2e as base  # noqa: E402

TEAMS = ROOT.parent
WT = Path(os.environ.get("NANOBOT_TEAMS_WORKTREE") or TEAMS.parent)
WEBUI = Path(os.environ.get("NANOBOT_TEAMS_WEBUI") or WT / "webui")
OUT = TEAMS / ".e2e"
NANOBOT = base.NANOBOT
RESEARCH_PORT = 8901
WEBUI_PORT = 8766
GATEWAY_PORT = 18790


def reset_webui() -> None:
    shutil.rmtree(OUT / "webui", ignore_errors=True)
    (OUT / "webui/research/workspace").mkdir(parents=True)
    (OUT / "webui/planner/workspace").mkdir(parents=True)
    shutil.copytree(
        TEAMS / "agent-plugin/nanobot-teams",
        OUT / "webui/planner/workspace/plugins/nanobot-teams",
    )


def write_configs() -> tuple[Path, Path]:
    research = base.base_config(
        OUT / "webui/research/workspace", RESEARCH_PORT, "${RESEARCH_API_KEY}", restrict=True
    )
    research["gateway"]["heartbeat"] = {"enabled": False}
    research_path = OUT / "webui/research/config.json"
    base.write_json(research_path, research)

    planner = base.base_config(
        OUT / "webui/planner/workspace", 8902, "${PLANNER_API_KEY}", restrict=True
    )
    planner["gateway"] = {
        "host": "127.0.0.1",
        "port": GATEWAY_PORT,
        "restartMode": "exit",
        "heartbeat": {"enabled": False},
    }
    planner["channels"] = {
        "websocket": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": WEBUI_PORT,
            "websocket_requires_token": False,
        }
    }
    planner["tools"]["mcpServers"] = {
        "teams": {
            "type": "stdio",
            "command": str(TEAMS / ".venv/Scripts/python.exe"),
            "args": ["-m", "nanobot_teams", "--config", str(OUT / "teams.json")],
            "cwd": str(OUT),
            "env": {"TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"},
            "toolTimeout": 180,
            "enabledTools": ["list_bots", "delegate_task"],
        }
    }
    planner_path = OUT / "webui/planner/config.json"
    base.write_json(planner_path, planner)
    return research_path, planner_path


def wait_http(url: str, proc: subprocess.Popen, name: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with {proc.returncode}. See .e2e/logs/{name}.log")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not answer at {url}")


def main() -> int:
    reset_webui()
    nonce = f"WEBUI-{secrets.token_hex(4).upper()}"
    (OUT / "webui/research/workspace/secret.txt").write_text(nonce, encoding="utf-8")
    research_path, planner_path = write_configs()
    base.write_teams(f"http://127.0.0.1:{RESEARCH_PORT}/v1/chat/completions")
    env = base.process_env()
    shots = OUT / "shots"
    shots.mkdir(exist_ok=True)

    research_proc = base.start(
        "webui-research",
        [str(NANOBOT), "--home", str(OUT / "webui/research"), "serve", "--config", str(research_path)],
        env,
    )
    planner_proc = None
    try:
        base.wait_health(RESEARCH_PORT, research_proc, "webui-research")
        planner_proc = base.start(
            "webui-planner",
            [str(NANOBOT), "--home", str(OUT / "webui/planner"), "gateway", "--config", str(planner_path)],
            env,
        )
        wait_http(f"http://127.0.0.1:{WEBUI_PORT}/", planner_proc, "webui-planner")
        result = subprocess.run(
            [
                "node",
                str(ROOT / "webui_drive.mjs"),
                f"http://127.0.0.1:{WEBUI_PORT}",
                nonce,
                str(shots),
                str(WEBUI / "package.json"),
            ],
            cwd=str(WEBUI),
            capture_output=True,
            text=True,
            timeout=300,
        )
        print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, flush=True)
        if result.returncode != 0:
            return result.returncode

        marker = next((OUT / "webui/planner").glob("plugin-data/*/nanobot-teams/enabled"), None)
        if marker is None:
            print("FAIL plugin enable marker missing", file=sys.stderr)
            return 1
        print("PASS webui_delegate", flush=True)
    finally:
        base.stop(planner_proc)
        base.stop(research_proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
