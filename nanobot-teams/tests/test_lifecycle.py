from __future__ import annotations

import json
from pathlib import Path

from nanobot_teams.config import load_teams_config
from nanobot_teams.lifecycle import bot_init, check, port_in_use

REGISTRY: dict = {
    "schema_version": 2,
    "team_id": "personal-research",
    "thread_key": "main",
    "bots": [
        {"id": "planner", "display_name": "Planner", "role": "planning"},
        {
            "id": "research",
            "display_name": "Research",
            "role": "research",
            "endpoint": "http://127.0.0.1:59901/v1/chat/completions",
            "api_key_env": "TEAMS_RESEARCH_API_KEY",
            "allowed_callers": ["planner"],
        },
    ],
}


def write_registry(tmp_path: Path, data: dict | None = None) -> Path:
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data or REGISTRY), encoding="utf-8")
    return path


def write_user_config(tmp_path: Path, key: str = "literal-secret") -> Path:
    path = tmp_path / "user.json"
    path.write_text(
        json.dumps(
            {
                "modelPresets": {
                    "step-5-preview": {"model": "step-5-preview", "provider": "demo"}
                },
                "providers": {
                    "demo": {"apiKey": key, "apiBase": "https://example.invalid/v1"}
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_bot_init_creates_skeleton_and_extends_registry(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    home = tmp_path / "writer2"

    user_config = write_user_config(tmp_path)
    scaffold, findings = bot_init(
        "writer2",
        registry_path=registry,
        home=home,
        port=59905,
        preset="step-5-preview",
        role="drafting",
        allowed_callers=["planner"],
        capabilities=["draft"],
        user_config=user_config,
    )

    assert [f for f in findings if f.severity == "error"] == []
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["api"]["port"] == 59905
    assert config["api"]["apiKey"] == "${TEAMS_WRITER2_API_KEY}"
    assert config["providers"]["demo"]["apiKey"] == "${DEMO_API_KEY}"
    assert "literal-secret" not in json.dumps(config)
    assert (home / "workspace").is_dir()
    assert config["agents"]["defaults"]["workspace"] == str((home / "workspace").resolve())

    updated = load_teams_config(registry)
    writer2 = updated.bot("writer2")
    assert writer2 is not None
    assert writer2.api_key_env == "TEAMS_WRITER2_API_KEY"
    assert writer2.allowed_callers == ("planner",)
    assert writer2.model_preset == "step-5-preview"
    assert writer2.capabilities == ("draft",)
    assert "TEAMS_WRITER2_API_KEY" in "\n".join(scaffold.checklist)
    assert "DEMO_API_KEY" in "\n".join(scaffold.checklist)
    assert "nanobot --home" in scaffold.serve_command


def test_bot_init_duplicate_refused_and_writes_nothing(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    home = tmp_path / "dupe"
    before = registry.read_text(encoding="utf-8")

    _, findings = bot_init(
        "research",
        registry_path=registry,
        home=home,
        port=59906,
        preset="step-5-preview",
        role="drafting",
    )

    assert any(f.code == "duplicate_bot" and f.severity == "error" for f in findings)
    assert not home.exists()
    assert registry.read_text(encoding="utf-8") == before


def test_bot_init_unknown_caller_refused(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    _, findings = bot_init(
        "writer2",
        registry_path=registry,
        home=tmp_path / "writer2",
        port=59907,
        preset="step-5-preview",
        role="drafting",
        allowed_callers=["ghost"],
    )
    assert any(f.code == "bad_caller" for f in findings)


def test_bot_init_needs_schema_2(tmp_path: Path) -> None:
    v1 = {
        "schema_version": 1,
        "team_id": "t",
        "caller_bot_id": "planner",
        "thread_key": "main",
        "allowed_targets": ["research"],
        "bots": REGISTRY["bots"],
    }
    registry = write_registry(tmp_path, v1)
    _, findings = bot_init(
        "writer2",
        registry_path=registry,
        home=tmp_path / "writer2",
        port=59908,
        preset="step-5-preview",
        role="drafting",
    )
    assert any(f.code == "registry_invalid" for f in findings)


def test_check_flags_missing_env_and_unknown_preset(tmp_path: Path) -> None:
    registry = write_registry(
        tmp_path,
        {
            **REGISTRY,
            "bots": [
                REGISTRY["bots"][0],
                {
                    **REGISTRY["bots"][1],
                    "model_preset": "no-such-preset",
                },
            ],
        },
    )
    user_config = tmp_path / "user.json"
    user_config.write_text(json.dumps({"modelPresets": {"step-5-preview": {}}}), encoding="utf-8")

    findings = check(
        registry,
        environ={},
        user_config=user_config,
        probe=False,
        nanobot_version="0.4.1",
    )

    codes = {(f.code, f.severity) for f in findings}
    assert ("missing_env", "error") in codes
    assert ("unknown_preset", "error") in codes


def test_check_clean_registry_passes(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    user_config = tmp_path / "user.json"
    user_config.write_text(json.dumps({"modelPresets": {}}), encoding="utf-8")

    findings = check(
        registry,
        environ={"TEAMS_RESEARCH_API_KEY": "value"},
        user_config=user_config,
        probe=False,
        nanobot_version="0.4.1",
    )

    assert [f for f in findings if f.severity == "error"] == []
    assert all(f.code in {"endpoint_down", "endpoint_up", "timeouts_unverified"} for f in findings)


def test_check_scopes_to_caller_and_flags_unknown(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    user_config = tmp_path / "user.json"
    user_config.write_text(json.dumps({"modelPresets": {}}), encoding="utf-8")

    findings = check(
        registry,
        caller="planner",
        environ={},
        user_config=user_config,
        probe=False,
        nanobot_version="0.4.1",
    )
    assert [f.code for f in findings if f.severity == "error"] == ["missing_env"]

    findings = check(
        registry,
        caller="ghost",
        environ={},
        user_config=user_config,
        probe=False,
        nanobot_version="0.4.1",
    )
    assert [f.code for f in findings] == ["unknown_caller"]


def test_check_registry_error_short_circuits(tmp_path: Path) -> None:
    path = tmp_path / "teams.json"
    path.write_text("{", encoding="utf-8")
    findings = check(path, probe=False, nanobot_version="0.4.1")
    assert [f.code for f in findings] == ["registry_invalid"]


def test_check_reports_untested_nanobot(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    user_config = tmp_path / "user.json"
    user_config.write_text(json.dumps({"modelPresets": {}}), encoding="utf-8")
    findings = check(
        registry,
        environ={"TEAMS_RESEARCH_API_KEY": "v"},
        user_config=user_config,
        probe=False,
        nanobot_version="9.9.9",
    )
    assert any(f.code == "nanobot_untested" and f.severity == "warn" for f in findings)


def test_port_in_use_detects_listener() -> None:
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert port_in_use("127.0.0.1", port) is True
        assert port_in_use("127.0.0.1", port + 1) is False
    finally:
        server.close()


def test_cli_routing_smoke() -> None:
    import subprocess
    import sys

    for args, expected in (
        (["bot"], 2),
        (["bot", "init", "--help"], 0),
        (["check", "--help"], 0),
        (["graph", "--help"], 0),
        (["budget", "--help"], 0),
        (["audit", "--help"], 0),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "nanobot_teams", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == expected, (args, result.returncode, result.stderr)


def test_bot_init_preserves_budgets(tmp_path: Path) -> None:
    data = {**REGISTRY, "budgets": {"planner": {"delegations": 3}}}
    registry = write_registry(tmp_path, data)

    _, findings = bot_init(
        "writer3",
        registry_path=registry,
        home=tmp_path / "writer3",
        port=59909,
        preset="step-5-preview",
        role="drafting",
        allowed_callers=["planner"],
        user_config=write_user_config(tmp_path),
    )

    assert [f for f in findings if f.severity == "error"] == []
    assert json.loads(registry.read_text(encoding="utf-8"))["budgets"] == {
        "planner": {"delegations": 3}
    }
    assert load_teams_config(registry).bot("writer3") is not None


def test_bot_init_empty_role_writes_nothing(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    before = registry.read_text(encoding="utf-8")
    home = tmp_path / "norole"

    _, findings = bot_init(
        "writer3",
        registry_path=registry,
        home=home,
        port=59910,
        preset="step-5-preview",
        role="",
    )

    assert any(f.code == "bad_role" and f.severity == "error" for f in findings)
    assert not home.exists()
    assert registry.read_text(encoding="utf-8") == before


def test_bot_init_invalid_capability_writes_nothing(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    before = registry.read_text(encoding="utf-8")
    home = tmp_path / "badcap"

    _, findings = bot_init(
        "writer3",
        registry_path=registry,
        home=home,
        port=59911,
        preset="step-5-preview",
        role="drafting",
        capabilities=["Bad Token"],
        user_config=write_user_config(tmp_path),
    )

    assert any(f.code == "registry_invalid" and f.severity == "error" for f in findings)
    assert not home.exists()
    assert registry.read_text(encoding="utf-8") == before


def test_bot_init_keeps_existing_provider_key_reference(tmp_path: Path) -> None:
    registry = write_registry(tmp_path)
    home = tmp_path / "writer4"

    _, findings = bot_init(
        "writer4",
        registry_path=registry,
        home=home,
        port=59912,
        preset="step-5-preview",
        role="drafting",
        allowed_callers=["planner"],
        user_config=write_user_config(tmp_path, key="${STEPFUN_API_KEY}"),
    )

    assert [f for f in findings if f.severity == "error"] == []
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["providers"]["demo"]["apiKey"] == "${STEPFUN_API_KEY}"


def test_scaffold_externalizes_all_connection_overrides(tmp_path: Path) -> None:
    source = write_user_config(tmp_path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["providers"]["demo"].update({
        "extraHeaders": {"Authorization": "FAKE_HEADER_SECRET"},
        "apiBase": "https://example.invalid/token/FAKE_URL_SECRET",
        "extraQuery": {"key": "FAKE_QUERY_SECRET"},
        "extraBody": {"nested": ["FAKE_BODY_SECRET", 123]},
    })
    raw["modelPresets"]["step-5-preview"].update({
        "extraHeaders": {"X-Api-Key": "FAKE_PRESET_SECRET"},
        "api": {"supportedApis": ["chat_completions"], "apiKey": "FAKE_UNKNOWN_SECRET"},
    })
    source.write_text(json.dumps(raw), encoding="utf-8")
    home = tmp_path / "safe-bot"
    scaffold, findings = bot_init("safe", registry_path=write_registry(tmp_path), home=home,
        port=59913, preset="step-5-preview", role="writes", allowed_callers=["planner"],
        user_config=source)
    assert not [item for item in findings if item.severity == "error"]
    written = (home / "config.json").read_text(encoding="utf-8")
    assert "FAKE_" not in written and "literal-secret" not in written
    config = json.loads(written)
    assert config["providers"]["demo"]["extraBody"]["nested"][1] == 123
    assert config["modelPresets"]["step-5-preview"]["api"] == {"supportedApis": ["chat_completions"]}
    assert len([line for line in scaffold.checklist if line.startswith("export ")]) == 6


def test_scaffold_refuses_existing_home_and_key_collision(tmp_path: Path) -> None:
    path = write_registry(tmp_path)
    home = tmp_path / "existing"
    home.mkdir()
    protected = home / "config.json"
    protected.write_text("keep", encoding="utf-8")
    _, findings = bot_init("new", registry_path=path, home=home, port=59914,
                          preset="step-5-preview", role="writes", allowed_callers=["planner"])
    assert any(item.code == "home_exists" for item in findings)
    assert protected.read_text(encoding="utf-8") == "keep"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["bots"][1]["api_key_env"] = "TEAMS_NEW_BOT_API_KEY"
    path.write_text(json.dumps(raw), encoding="utf-8")
    _, findings = bot_init("new.bot", registry_path=path, home=tmp_path/"other", port=59914,
                          preset="step-5-preview", role="writes", allowed_callers=["planner"])
    assert any(item.code == "key_env_collision" for item in findings)
    assert not (tmp_path/"other").exists()


def test_scaffold_rolls_back_its_home_on_registry_commit_failure(tmp_path: Path, monkeypatch) -> None:
    from nanobot_teams import lifecycle

    path = write_registry(tmp_path)
    original = path.read_bytes()
    real_write = lifecycle._atomic_write

    def fail_registry(destination, text):
        if destination == path:
            raise OSError("simulated full disk")
        real_write(destination, text)

    monkeypatch.setattr(lifecycle, "_atomic_write", fail_registry)
    home = tmp_path / "rolled-back"
    _, findings = bot_init("new", registry_path=path, home=home, port=59915,
        preset="step-5-preview", role="writes", allowed_callers=["planner"],
        user_config=write_user_config(tmp_path))
    assert any(item.code == "registry_write_failed" for item in findings)
    assert path.read_bytes() == original
    assert not home.exists()


def test_check_validates_declared_timeout_layers(tmp_path: Path) -> None:
    path = write_registry(tmp_path)
    manifest = tmp_path / "deployment.json"
    data = {"mcp_tool_timeout_s": 180, "planner_timeout_s": 210,
            "targets": {"research": {"api_timeout_s": 120, "queue_included": True}}}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    findings = check(path, caller="planner", environ={"TEAMS_RESEARCH_API_KEY": "fake"},
        probe=False, nanobot_version="0.4.1", deployment=manifest)
    assert not [item for item in findings if item.severity == "error"]
    assert any(item.code == "timeouts_declared" for item in findings)
    data["mcp_tool_timeout_s"] = 30
    data["planner_timeout_s"] = 20
    data["targets"]["research"]["api_timeout_s"] = 140
    data["targets"]["research"]["queue_included"] = False
    manifest.write_text(json.dumps(data), encoding="utf-8")
    findings = check(path, caller="planner", environ={"TEAMS_RESEARCH_API_KEY": "fake"},
        probe=False, nanobot_version="0.4.1", deployment=manifest)
    codes = {item.code for item in findings if item.severity == "error"}
    assert codes == {"tool_timeout_short", "planner_timeout_short", "target_timeout_invalid", "queue_timeout_unverified"}
