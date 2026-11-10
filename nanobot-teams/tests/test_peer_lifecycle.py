"""P4 lifecycle: scaffolded workers become peers, plain configs stay plain.

A managed bot generated with coordinator wiring must carry the runs inbox on
its own listener plus a teams-async MCP client whose credential is a
``${ENV}`` reference — never a raw token (F13 discipline).
"""

from __future__ import annotations

import json
from pathlib import Path

from nanobot_teams.lifecycle import managed_bot_config

pytest_plugins = ("pytest_asyncio",)

PRESET = "test-preset"


def user_config(tmp_path: Path) -> Path:
    path = tmp_path / "user-config.json"
    path.write_text(
        json.dumps(
            {
                "modelPresets": {
                    PRESET: {
                        "model": "test-model",
                        "provider": "test-provider",
                        "apiKey": "sk-live",
                    }
                },
                "providers": {
                    "test-provider": {"apiKey": "sk-live", "apiBase": "https://api.example.test"}
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_peer_wiring_adds_inbox_and_mcp_client(tmp_path):
    config, required_env = managed_bot_config(
        bot_id="writer",
        home=tmp_path / "writer",
        port=9210,
        preset=PRESET,
        runs_api_key_env="TEAMS_BOT_WRITER_KEY",
        coordinator_url="http://127.0.0.1:8910",
        coord_token_env="TEAMS_BOT_WRITER_COORD_TOKEN",
        user_config=user_config(tmp_path),
    )
    assert config["api"]["runs"]["inboxEnabled"] is True
    servers = config["tools"]["mcpServers"]
    assert set(servers) == {"teams-async"}
    server = servers["teams-async"]
    assert server["teamsAsync"] is True
    assert "http://127.0.0.1:8910" in server["args"]
    assert "TEAMS_BOT_WRITER_COORD_TOKEN" in server["args"]
    assert (
        server["env"]["TEAMS_BOT_WRITER_COORD_TOKEN"]
        == "${TEAMS_BOT_WRITER_COORD_TOKEN}"
    )
    # The child imports nanobot_teams without inheriting the parent's path.
    assert server["env"]["PYTHONPATH"].endswith("nanobot-teams\\src") or \
        server["env"]["PYTHONPATH"].endswith("nanobot-teams/src")
    raw = json.dumps(config)
    assert "sk-live" not in raw
    assert "TEAMS_BOT_WRITER_COORD_TOKEN" in required_env


def test_plain_config_stays_backward_compatible(tmp_path):
    config, required_env = managed_bot_config(
        bot_id="plain",
        home=tmp_path / "plain",
        port=9211,
        preset=PRESET,
        runs_api_key_env="TEAMS_BOT_PLAIN_KEY",
        user_config=user_config(tmp_path),
    )
    assert "inboxEnabled" not in config["api"]["runs"]
    assert config["tools"]["mcpServers"] == {}
    assert "TEAMS_BOT_PLAIN_COORD_TOKEN" not in required_env
