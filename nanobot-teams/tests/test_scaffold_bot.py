"""R4: scaffolded bot homes are loadable and never leak the service key.

Regression lock for the 2026-11-09 audit findings F12 and F13: the generated
config is real JSON the Nanobot loader accepts (with a real ``serve`` start on
this interpreter when nanobot is importable), credentials live only in
``${ENV}`` references, and the raw key never reaches the browser response or
the config file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nanobot_teams.coordinator.config import (
    ConfigError,
    CoordinatorConfig,
    load_coordinator_config,
)
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.store import CoordinatorStore

pytest_plugins = ("pytest_asyncio",)

PRESET = "test-preset"
SECRET_ENV = "TEAMS_BOT_SPACE_BOT_KEY"


def _user_config(tmp_path: Path) -> Path:
    path = tmp_path / "user-config.json"
    path.write_text(
        json.dumps(
            {
                "modelPresets": {
                    PRESET: {
                        "model": "test-model",
                        "provider": "test-provider",
                        "apiKey": "sk-live-provider-key",
                    }
                },
                "providers": {
                    "test-provider": {"apiKey": "sk-live-provider-key", "apiBase": "https://api.example.test"}
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def scaffold_env(tmp_path):
    homes = tmp_path / "bot homes"  # a space exercises the start-command quoting
    store = CoordinatorStore(tmp_path / "scaffold.sqlite3")
    store.open()
    store.create_project("proj", "Main")
    config = CoordinatorConfig(
        control_dir=tmp_path,
        bot_homes_root=homes,
        scaffold_model_preset=PRESET,
        scaffold_user_config=_user_config(tmp_path),
    )
    return store, config, homes


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _scaffold(store, config, bot_id="space-bot"):
    app = build_app(store, config, {"operator": "op-token", "planner": "plan-token"})
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/teams/bots/scaffold",
            headers={"Authorization": "Bearer op-token"},
            json={"bot_id": bot_id},
        )
        assert response.status == 201, await response.text()
        return await response.json()


async def test_scaffold_home_loads_without_leaking_the_key(scaffold_env, tmp_path):
    """F12+F13: loadable JSON, env-ref credentials, zero raw-key exposure."""
    store, config, homes = scaffold_env
    body = await _scaffold(store, config)
    home = Path(body["home"])
    try:
        assert home.exists() and home.is_relative_to(homes)
        # The browser response never carries the raw key (F13).
        assert "api_key" not in body
        assert json.dumps(body).find("token_urlsafe") == -1
        # The config keeps only the env reference, never the raw key (F13).
        config_text = (home / "config.json").read_text(encoding="utf-8")
        assert '"runs"' in config_text and body["secret_env"] in config_text
        # The start command quotes the path (the homes root contains a space).
        assert body["start_command"].startswith("nanobot serve --config \"")
        assert body["start_command"].endswith("config.json\"")
        # The operator-local key file carries the generated secret.
        key_file = Path(body["key_file"])
        assert key_file.exists()
        line = [
            entry
            for entry in key_file.read_text(encoding="utf-8").splitlines()
            if entry.startswith(SECRET_ENV)
        ]
        raw_key = line[0].split("=", 1)[1]
        assert raw_key and raw_key not in config_text
        assert raw_key not in json.dumps(body)
        assert SECRET_ENV in body["required_env"]

        # F12: the real loader accepts the generated JSON and the runs key
        # resolves to the generated value (the same resolve serve performs).
        loader = pytest.importorskip("nanobot.config.loader")
        added: list[str] = []
        try:
            for name in body["required_env"]:
                if not os.environ.get(name):
                    os.environ[name] = "sk-test-provider-value"
                    added.append(name)
            os.environ[SECRET_ENV] = raw_key
            loaded = loader.resolve_config_env_vars(
                loader.load_config(home / "config.json"),
                config_path=home / "config.json",
            )
        finally:
            for name in added:
                os.environ.pop(name, None)
            os.environ.pop(SECRET_ENV, None)
        assert loaded.api.runs.api_key == raw_key
        assert loaded.api.runs.execution_enabled is True
        assert loaded.api.runs.exclusive_execution is True
        assert loaded.api.runs.principal_id == "teams-coordinator"
        assert loaded.agents.defaults.model_preset == PRESET
        workspace = loaded.agents.defaults.workspace
        assert workspace == str((home / "workspace").resolve())
        assert loaded.tools.restrict_to_workspace is True
        assert raw_key not in config_text
    finally:
        store.close()


async def test_scaffold_refuses_without_preset(tmp_path):
    """Scaffolding stays disabled until the operator names a model preset."""
    store = CoordinatorStore(tmp_path / "no-preset.sqlite3")
    store.open()
    try:
        config = CoordinatorConfig(
            control_dir=tmp_path,
            bot_homes_root=tmp_path / "homes",
        )
        app = build_app(store, config, {"operator": "op-token", "planner": "plan-token"})
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/teams/bots/scaffold",
                headers={"Authorization": "Bearer op-token"},
                json={"bot_id": "some-bot"},
            )
            assert response.status == 422
            assert (await response.json())["code"] == "preset_required"
    finally:
        store.close()


async def test_scaffold_rejects_unknown_preset(tmp_path):
    """An unknown preset fails with a clear error and leaves no home behind."""
    store = CoordinatorStore(tmp_path / "bad-preset.sqlite3")
    store.open()
    try:
        config = CoordinatorConfig(
            control_dir=tmp_path,
            bot_homes_root=tmp_path / "homes",
            scaffold_model_preset="missing-preset",
            scaffold_user_config=_user_config(tmp_path),
        )
        app = build_app(store, config, {"operator": "op-token", "planner": "plan-token"})
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/teams/bots/scaffold",
                headers={"Authorization": "Bearer op-token"},
                json={"bot_id": "some-bot"},
            )
            assert response.status == 422
            assert (await response.json())["code"] == "invalid_preset"
            homes = tmp_path / "homes"
            assert not homes.exists() or not any(homes.iterdir())
    finally:
        store.close()


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("nanobot") is None,
    reason="nanobot is not importable from this interpreter",
)
async def test_scaffolded_bot_really_starts(scaffold_env):
    """F12: the generated config starts a real ``nanobot serve`` that answers
    the agent-capabilities probe with the scaffolded service key."""
    store, config, _homes = scaffold_env
    body = await _scaffold(store, config, bot_id="launch-bot")
    home = Path(body["home"])
    raw_key = [
        entry.split("=", 1)[1]
        for entry in (home / "service-key.env").read_text(encoding="utf-8").splitlines()
        if entry.startswith(body["secret_env"])
    ][0]
    env = os.environ.copy()
    for name in body["required_env"]:
        env[name] = "sk-test-provider-value"
    env[body["secret_env"]] = raw_key
    # The spawned serve must run THIS repository's nanobot, not whatever an
    # editable install in the test venv resolves to from a foreign cwd.
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (repo_root, env.get("PYTHONPATH", "")) if p
    )
    serve_log = home / "serve-test.log"
    log_handle = open(serve_log, "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "nanobot", "serve", "--config", str(home / "config.json")],
        env=env,
        cwd=str(home),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60.0
        status = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"nanobot serve exited early with {process.returncode}")
            request = urllib.request.Request(
                f"{body['endpoint']}/v1/agent-capabilities",
                headers={"Authorization": f"Bearer {raw_key}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    status = response.status
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                await _sleep()
        assert status == 200
        assert payload["protocol"] == 1
        assert payload["execution"] is True
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        finally:
            log_handle.close()
            tail = serve_log.read_text(encoding="utf-8", errors="replace")[-3000:]
            if tail.strip():
                print(f"--- scaffolded serve log tail ---\n{tail}", end="")


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.5)

def _write_coordinator_config(tmp_path: Path, **extra: object) -> Path:
    body: dict[str, object] = {
        "control_dir": str(tmp_path / "control"),
        "bot_homes_root": str(tmp_path / "homes"),
        "admin_token_env": "TEST_COORDINATOR_OP",
        "planner_token_env": "TEST_COORDINATOR_PLANNER",
    }
    body.update(extra)
    path = tmp_path / "coordinator.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_loader_accepts_and_passes_the_scaffold_fields(tmp_path, monkeypatch):
    """M05: the real loader carries the scaffold preset and user config."""
    monkeypatch.setenv("TEST_COORDINATOR_OP", "op-token")
    monkeypatch.setenv("TEST_COORDINATOR_PLANNER", "plan-token")
    user_config = _user_config(tmp_path)
    config = load_coordinator_config(
        _write_coordinator_config(
            tmp_path,
            scaffold_model_preset=PRESET,
            scaffold_user_config=str(user_config),
        )
    )
    assert config.scaffold_model_preset == PRESET
    assert config.scaffold_user_config == user_config.resolve()


@pytest.mark.parametrize("preset", [5, "", "   ", None])
def test_loader_validates_the_scaffold_preset(tmp_path, monkeypatch, preset):
    """M05: a malformed preset is a startup ConfigError, not a silent default."""
    monkeypatch.setenv("TEST_COORDINATOR_OP", "op-token")
    monkeypatch.setenv("TEST_COORDINATOR_PLANNER", "plan-token")
    if preset is None:
        path = _write_coordinator_config(tmp_path)
    else:
        path = _write_coordinator_config(tmp_path, scaffold_model_preset=preset)
    if preset is None:
        assert load_coordinator_config(path).scaffold_model_preset == ""
    else:
        with pytest.raises(ConfigError, match="scaffold_model_preset"):
            load_coordinator_config(path)


@pytest.mark.parametrize("value", [5, "", "   ", ["path"]])
def test_loader_validates_the_scaffold_user_config(tmp_path, value):
    """M05: the user config must be a non-empty path string."""
    path = _write_coordinator_config(tmp_path, scaffold_user_config=value)
    with pytest.raises(ConfigError, match="scaffold_user_config"):
        load_coordinator_config(path)


async def test_loaded_config_reaches_the_scaffold_route(tmp_path, monkeypatch):
    """M05: coordinator.json -> loader -> POST scaffold is not 422 preset_required."""
    monkeypatch.setenv("TEST_COORDINATOR_OP", "op-token")
    monkeypatch.setenv("TEST_COORDINATOR_PLANNER", "plan-token")
    config = load_coordinator_config(
        _write_coordinator_config(
            tmp_path,
            scaffold_model_preset=PRESET,
            scaffold_user_config=str(_user_config(tmp_path)),
        )
    )
    store = CoordinatorStore(tmp_path / "loaded.sqlite3")
    store.open()
    try:
        app = build_app(store, config, config.resolve_tokens())
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/teams/bots/scaffold",
                headers={"Authorization": "Bearer op-token"},
                json={"bot_id": "loaded-bot"},
            )
            assert response.status == 201, await response.text()
            body = await response.json()
            assert body["bot_id"] == "loaded-bot"
    finally:
        store.close()
