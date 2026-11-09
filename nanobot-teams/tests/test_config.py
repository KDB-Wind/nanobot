from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot_teams.config import (
    ConfigError,
    Limits,
    load_teams_config,
    resolve_api_keys,
    resolve_caller,
)

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
    assert config.default_caller_id == "planner"
    assert config.thread_key == "main"
    assert config.limits == Limits()
    assert config.bot("planner") is not None
    target = config.bot("research")
    assert target is not None
    assert target.endpoint == ENDPOINT
    assert target.api_key_env == "TEAMS_RESEARCH_API_KEY"
    assert target.allowed_callers == ("planner",)
    assert config.can_delegate("planner", "research") is True
    assert config.can_delegate("research", "planner") is False
    assert [bot.id for bot in config.targets_for("planner")] == ["research"]


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
    keys = resolve_api_keys(config, "planner", {"TEAMS_RESEARCH_API_KEY": "value"})

    assert keys == {"research": "value"}


def test_resolve_api_keys_missing(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    with pytest.raises(ConfigError, match="TEAMS_RESEARCH_API_KEY"):
        resolve_api_keys(config, "planner", {})


def test_resolve_api_keys_empty(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    with pytest.raises(ConfigError, match="missing or empty"):
        resolve_api_keys(config, "planner", {"TEAMS_RESEARCH_API_KEY": ""})


def test_config_carries_no_secret(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    assert "value" not in repr(config)
    assert all(not hasattr(bot, "api_key") for bot in config.bots)


WRITER_ENDPOINT = "http://127.0.0.1:8903/v1/chat/completions"


def v2_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 2,
        "team_id": "personal-research",
        "thread_key": "main",
        "bots": [
            {
                "id": "planner",
                "display_name": "Planner",
                "role": "planning",
                "capabilities": ["plan", "synthesize"],
                "model_preset": "step-5-preview",
            },
            {
                "id": "research",
                "display_name": "Research",
                "role": "research",
                "capabilities": ["search"],
                "model_preset": "glm-5.3-flash",
                "endpoint": ENDPOINT,
                "api_key_env": "TEAMS_RESEARCH_API_KEY",
                "allowed_callers": ["planner", "writer"],
            },
            {
                "id": "writer",
                "display_name": "Writer",
                "role": "drafting",
                "endpoint": WRITER_ENDPOINT,
                "api_key_env": "TEAMS_WRITER_API_KEY",
                "allowed_callers": ["planner"],
            },
        ],
    }
    data.update(overrides)
    return data


def test_v2_config_loads_and_builds_the_graph(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, v2_data()))

    assert config.schema_version == 2
    assert config.default_caller_id is None
    assert config.can_delegate("planner", "research") is True
    assert config.can_delegate("planner", "writer") is True
    assert config.can_delegate("writer", "research") is True
    assert config.can_delegate("research", "planner") is False
    assert config.can_delegate("research", "writer") is False
    assert config.can_delegate("planner", "planner") is False
    research = config.bot("research")
    assert research is not None
    assert research.capabilities == ("search",)
    assert research.model_preset == "glm-5.3-flash"
    assert {bot.id for bot in config.targets_for("planner")} == {"research", "writer"}
    assert {bot.id for bot in config.targets_for("writer")} == {"research"}
    assert config.targets_for("research") == ()


def test_v2_rejects_v1_top_level_keys(tmp_path: Path) -> None:
    data = v2_data(caller_bot_id="planner")
    with pytest.raises(ConfigError, match="unknown key"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_rejects_allowed_targets(tmp_path: Path) -> None:
    data = v2_data(allowed_targets=["research"])
    with pytest.raises(ConfigError, match="unknown key"):
        load_teams_config(write_config(tmp_path, data))


def test_v1_rejects_v2_bot_keys(tmp_path: Path) -> None:
    data = config_data()
    data["bots"][1]["allowed_callers"] = ["planner"]
    with pytest.raises(ConfigError, match="unknown key"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_self_caller_rejected(tmp_path: Path) -> None:
    data = v2_data()
    data["bots"][1]["allowed_callers"] = ["research"]
    with pytest.raises(ConfigError, match="must not list itself"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_unknown_caller_rejected(tmp_path: Path) -> None:
    data = v2_data()
    data["bots"][2]["allowed_callers"] = ["ghost"]
    with pytest.raises(ConfigError, match="unknown caller"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_target_without_endpoint_rejected(tmp_path: Path) -> None:
    data = v2_data()
    del data["bots"][2]["endpoint"]
    with pytest.raises(ConfigError, match="needs endpoint"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_non_target_cannot_carry_endpoint(tmp_path: Path) -> None:
    data = v2_data()
    data["bots"][0]["endpoint"] = ENDPOINT
    data["bots"][0]["api_key_env"] = "TEAMS_PLANNER_API_KEY"
    with pytest.raises(ConfigError, match="cannot set endpoint"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_duplicate_caller_rejected(tmp_path: Path) -> None:
    data = v2_data()
    data["bots"][1]["allowed_callers"] = ["planner", "planner"]
    with pytest.raises(ConfigError, match="must not repeat"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_bad_capability_rejected(tmp_path: Path) -> None:
    data = v2_data()
    data["bots"][0]["capabilities"] = ["Search!"]
    with pytest.raises(ConfigError, match="capabilities"):
        load_teams_config(write_config(tmp_path, data))


def test_v2_too_many_capabilities_rejected(tmp_path: Path) -> None:
    data = v2_data()
    data["bots"][0]["capabilities"] = [f"c{index}" for index in range(9)]
    with pytest.raises(ConfigError, match="at most 8"):
        load_teams_config(write_config(tmp_path, data))


def test_resolve_caller_v2_requires_argument(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, v2_data()))
    with pytest.raises(ConfigError, match="--caller"):
        resolve_caller(config, None)
    assert resolve_caller(config, "planner") == "planner"
    assert resolve_caller(config, "writer") == "writer"


def test_resolve_caller_v2_unknown_bot(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, v2_data()))
    with pytest.raises(ConfigError, match="not present in bots"):
        resolve_caller(config, "ghost")


def test_resolve_caller_v1_defaults_and_pins(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, config_data()))
    assert resolve_caller(config, None) == "planner"
    assert resolve_caller(config, "planner") == "planner"
    with pytest.raises(ConfigError, match="must match caller_bot_id"):
        resolve_caller(config, "research")


def test_resolve_api_keys_scopes_to_caller(tmp_path: Path) -> None:
    config = load_teams_config(write_config(tmp_path, v2_data()))
    keys = resolve_api_keys(
        config, "writer", {"TEAMS_RESEARCH_API_KEY": "research-value"}
    )
    assert keys == {"research": "research-value"}
