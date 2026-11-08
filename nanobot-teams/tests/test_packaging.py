from __future__ import annotations

import re
import tomllib
from dataclasses import fields
from pathlib import Path

from nanobot_teams.config import BotConfig, TeamConfig, load_teams_config

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "nanobot_teams"


def test_no_nanobot_imports() -> None:
    pattern = re.compile(r"^\s*(?:from|import)\s+nanobot(?:\.[A-Za-z_]|\s|$)")
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(SRC.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.match(line)
    ]

    assert offenders == []


def test_console_script_and_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "nanobot-teams"
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["nanobot-teams"] == "nanobot_teams.__main__:main"
    assert "mcp>=1.26,<2" in project["dependencies"]
    assert "httpx>=0.28,<1" in project["dependencies"]


def test_config_has_no_secret_fields() -> None:
    bot_fields = {field.name for field in fields(BotConfig)}
    team_fields = {field.name for field in fields(TeamConfig)}

    assert bot_fields == {"id", "display_name", "role", "endpoint", "api_key_env"}
    assert team_fields == {
        "schema_version",
        "team_id",
        "caller_bot_id",
        "thread_key",
        "allowed_targets",
        "bots",
        "limits",
    }
    assert not (bot_fields | team_fields) & {"api_key", "token", "secret", "authorization"}


def test_example_teams_config_loads() -> None:
    config = load_teams_config(ROOT / "examples" / "teams.example.json")

    assert config.caller_bot_id == "planner"
    assert [bot.id for bot in config.targets()] == ["research"]
