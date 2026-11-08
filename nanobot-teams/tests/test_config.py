from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nanobot_teams.config import ConfigError, Limits, load_teams_config, resolve_api_keys

ENDPOINT = "http://127.0.0.1:8901/v1/chat/completions"


def config_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "team_id": "personal-research",
        "caller_bot_id": "planner",
        "thread_key": "main",
        "allowed_targets": ["research"],
        "bots": [
            {"id": "planner", "display_name": "Planner", "role": "planning"},
            {
                "id": "research",
                "display_name": "Research",
                "role": "research",
                "endpoint": ENDPOINT,
                "api_key_env": "TEAMS_RESEARCH_API_KEY",
            },
        ],
    }
    data.update(overrides)
    return data


def write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_valid_config_loads(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))

    assert config.schema_version == 1
    assert config.team_id == "personal-research"
    assert config.caller_bot_id == "planner"
    assert config.thread_key == "main"
    assert config.allowed_targets == ("research",)
    assert config.limits == Limits()
    assert config.bot("planner") is not None
    target = config.bot("research")
    assert target is not None
    assert target.endpoint == ENDPOINT
    assert target.api_key_env == "TEAMS_RESEARCH_API_KEY"
    assert [bot.id for bot in config.targets()] == ["research"]


def test_limits_override(tmp_path: Path) -> None:
    data = config_data(limits={"task_max_bytes": 10, "total_timeout_s": 5})
    limits = load_teams_config(write_config(tmp_path, data)).limits

    assert limits.task_max_bytes == 10
    assert limits.total_timeout_s == 5
    assert limits.response_max_bytes == 262144
    assert limits.connect_timeout_s == 3.0


def test_unknown_top_level_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_teams_config(write_config(tmp_path, config_data(extra=True)))


def test_unknown_bot_key(tmp_path: Path) -> None:
    data = config_data()
    data["bots"][1]["model"] = "x"
    with pytest.raises(ConfigError, match="unknown key"):
        load_teams_config(write_config(tmp_path, data))


def test_unknown_limits_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_teams_config(write_config(tmp_path, config_data(limits={"retries": 3})))


def test_duplicate_bot_id(tmp_path: Path) -> None:
    data = config_data()
    data["bots"].append({"id": "research", "display_name": "Again", "role": "research"})
    with pytest.raises(ConfigError, match="repeats id"):
        load_teams_config(write_config(tmp_path, data))


def test_missing_caller(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="caller_bot_id is not present"):
        load_teams_config(write_config(tmp_path, config_data(caller_bot_id="ghost")))


def test_caller_is_an_allowed_target(tmp_path: Path) -> None:
    data = config_data(allowed_targets=["planner", "research"])
    with pytest.raises(ConfigError, match="must not be an allowed target"):
        load_teams_config(write_config(tmp_path, data))


def test_allowed_target_missing_from_bots(tmp_path: Path) -> None:
    data = config_data(allowed_targets=["research", "analyst"])
    with pytest.raises(ConfigError, match="not present in bots"):
        load_teams_config(write_config(tmp_path, data))


def test_empty_allowed_targets(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        load_teams_config(write_config(tmp_path, config_data(allowed_targets=[])))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8901/v1/chat/completions",
        "http://localhost:8901/v1/chat/completions",
        "http://[::1]:8901/v1/chat/completions",
        "http://127.0.0.1/v1/chat/completions",
        "http://127.0.0.1:0/v1/chat/completions",
        "http://127.0.0.1:70000/v1/chat/completions",
        "http://127.0.0.1:8901/v1/models",
        "http://127.0.0.1:8901/v1/chat/completions/",
        "http://user:pass@127.0.0.1:8901/v1/chat/completions",
        "http://127.0.0.1:8901/v1/chat/completions?a=1",
        "http://127.0.0.1:8901/v1/chat/completions#x",
        "http://127.0.0.1:8901/v1/chat/completions#",
        "http://127.0.0.1:8901/v1/chat/completions?",
        " http://127.0.0.1:8901/v1/chat/completions",
        "http://10.0.0.1:8901/v1/chat/completions",
    ],
)
def test_bad_endpoint(tmp_path: Path, endpoint: str) -> None:
    data = config_data()
    data["bots"][1]["endpoint"] = endpoint
    with pytest.raises(ConfigError):
        load_teams_config(write_config(tmp_path, data))


def test_target_without_endpoint(tmp_path: Path) -> None:
    data = config_data()
    del data["bots"][1]["endpoint"]
    with pytest.raises(ConfigError, match="needs endpoint"):
        load_teams_config(write_config(tmp_path, data))


def test_non_target_cannot_carry_endpoint(tmp_path: Path) -> None:
    data = config_data()
    data["bots"][0]["endpoint"] = ENDPOINT
    data["bots"][0]["api_key_env"] = "TEAMS_RESEARCH_API_KEY"
    with pytest.raises(ConfigError, match="cannot set endpoint"):
        load_teams_config(write_config(tmp_path, data))


def test_invalid_bot_id(tmp_path: Path) -> None:
    data = config_data()
    data["bots"][1]["id"] = "Research"
    with pytest.raises(ConfigError, match="must match"):
        load_teams_config(write_config(tmp_path, data))


def test_team_id_control_character(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="control characters"):
        load_teams_config(write_config(tmp_path, config_data(team_id="team\x07")))


def test_team_id_too_long(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="128"):
        load_teams_config(write_config(tmp_path, config_data(thread_key="t" * 129)))


def test_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be positive"):
        load_teams_config(write_config(tmp_path, config_data(limits={"request_timeout_s": 0})))


def test_byte_limit_must_be_integer(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="integer"):
        load_teams_config(write_config(tmp_path, config_data(limits={"task_max_bytes": 1.5})))


def test_invalid_env_name(tmp_path: Path) -> None:
    data = config_data()
    data["bots"][1]["api_key_env"] = "TEAMS-KEY"
    with pytest.raises(ConfigError, match="environment variable name"):
        load_teams_config(write_config(tmp_path, data))


def test_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "teams.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_teams_config(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_teams_config(tmp_path / "absent.json")


def test_resolve_api_keys(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    keys = resolve_api_keys(config, {"TEAMS_RESEARCH_API_KEY": "value"})

    assert keys == {"research": "value"}


def test_resolve_api_keys_missing(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    with pytest.raises(ConfigError, match="TEAMS_RESEARCH_API_KEY"):
        resolve_api_keys(config, {})


def test_resolve_api_keys_empty(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    with pytest.raises(ConfigError, match="missing or empty"):
        resolve_api_keys(config, {"TEAMS_RESEARCH_API_KEY": ""})


def test_config_carries_no_secret(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    assert "value" not in repr(config)
    assert all(not hasattr(bot, "api_key") for bot in config.bots)
