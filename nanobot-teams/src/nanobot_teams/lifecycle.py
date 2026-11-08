"""Bot lifecycle tools: scaffold a new bot and check a registry for problems.

`bot init` writes a home, config, and workspace skeleton plus a printed
checklist, and appends the bot to the registry after the whole updated file
validates. It never starts a process. `check` loads the registry, probes
endpoints, environment variables, presets, and the installed nanobot version,
and prints one finding per line. Both write only where the operator points
them; the MCP server modules never import this file.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import BotConfig, ConfigError, load_teams_config

TESTED_NANOBOT = "0.4.1"
_REGISTRY_KEYS = ("schema_version", "team_id", "thread_key", "bots", "limits", "budgets")
_BOT_KEYS = ("id", "display_name", "role", "capabilities", "model_preset", "endpoint", "api_key_env", "allowed_callers")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # "error" or "warn"
    message: str


@dataclass(frozen=True)
class Scaffold:
    bot_id: str
    home: Path
    port: int
    api_key_env: str
    serve_command: str
    checklist: tuple[str, ...]


def api_key_env_for(bot_id: str) -> str:
    return "TEAMS_" + bot_id.upper().replace("-", "_").replace(".", "_") + "_API_KEY"


def port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def user_config_path() -> Path:
    return Path.home() / ".nanobot" / "config.json"


def bot_init(
    bot_id: str,
    *,
    registry_path: Path,
    home: Path,
    port: int,
    preset: str,
    display_name: str | None = None,
    role: str = "",
    allowed_callers: Sequence[str] = (),
    capabilities: Sequence[str] = (),
    user_config: Path | None = None,
) -> tuple[Scaffold, list[Finding]]:
    """Generate the bot skeleton, extend the registry, and return the checklist.

    The registry is validated in full before anything is written, so a schema
    refusal leaves no home directory behind. The generated provider block
    references credentials through an environment variable instead of copying
    the user's literal API key.
    """
    findings: list[Finding] = []
    if not role.strip():
        findings.append(
            Finding("bad_role", "error", "role must be a non-empty one-line description")
        )
    if port_in_use("127.0.0.1", port):
        findings.append(
            Finding("port_busy", "error", f"port {port} on 127.0.0.1 already answers connections")
        )

    try:
        registry = _read_registry(registry_path)
        if registry.get("schema_version") != 2:
            raise ConfigError("bot init needs a schema_version 2 registry")
        if any(bot.get("id") == bot_id for bot in registry.get("bots", [])):
            findings.append(Finding("duplicate_bot", "error", f"bots already lists {bot_id!r}"))
        caller_ids = {bot.get("id") for bot in registry.get("bots", [])}
        for caller in allowed_callers:
            if caller == bot_id:
                findings.append(
                    Finding("bad_caller", "error", "a bot cannot be its own allowed caller")
                )
            elif caller not in caller_ids:
                findings.append(
                    Finding(
                        "bad_caller", "error", f"allowed caller {caller!r} is not in the registry"
                    )
                )
    except ConfigError as exc:
        findings.append(Finding("registry_invalid", "error", str(exc)))

    if findings:
        return _scaffold(bot_id, home, port, registry_path, ()), findings

    api_key_env = api_key_env_for(bot_id)
    source_config = user_config or user_config_path()
    user = _load_json(source_config)
    preset_block = None
    provider_block: dict = {}
    provider_key_env: str | None = None
    model = ""
    if isinstance(user, dict):
        presets = user.get("modelPresets") or {}
        if preset in presets:
            preset_block = presets[preset]
            if isinstance(preset_block, dict):
                model = preset_block.get("model", "")
                provider_name = preset_block.get("provider")
                providers = user.get("providers") or {}
                if provider_name in providers:
                    block, provider_key_env = _provider_for_scaffold(
                        providers[provider_name], provider_name
                    )
                    provider_block = {provider_name: block}
        else:
            findings.append(
                Finding(
                    "unknown_preset",
                    "warn",
                    f"preset {preset!r} is not in {source_config}; the generated config "
                    "still names it, fix the model blocks before starting",
                )
            )

    entry = {
        "id": bot_id,
        "display_name": display_name or bot_id.replace("-", " ").replace("_", " ").title(),
        "role": role,
        **({"capabilities": list(capabilities)} if capabilities else {}),
        "model_preset": preset,
        "endpoint": f"http://127.0.0.1:{port}/v1/chat/completions",
        "api_key_env": api_key_env,
        "allowed_callers": list(allowed_callers),
    }
    updated = {**registry, "bots": [*registry.get("bots", []), entry]}
    try:
        registry_text = _registry_text(updated)
    except ConfigError as exc:
        findings.append(Finding("registry_invalid", "error", str(exc)))
        return _scaffold(bot_id, home, port, registry_path, ()), findings

    home.mkdir(parents=True, exist_ok=True)
    workspace = (home / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    config = {
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "modelPreset": preset,
                "model": model,
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
        "modelPresets": {preset: preset_block} if preset_block else {},
        "providers": provider_block,
        "api": {
            "host": "127.0.0.1",
            "port": port,
            "timeout": 120,
            "apiKey": "$" + "{" + api_key_env + "}",
        },
        "gateway": {"host": "127.0.0.1", "port": port + 1000, "restartMode": "exit"},
        "tools": {"restrictToWorkspace": True, "exec": {"enable": False}, "mcpServers": {}},
        "channels": {},
    }
    (home / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    try:
        registry_path.write_text(registry_text, encoding="utf-8")
    except OSError as exc:
        findings.append(
            Finding(
                "registry_write_failed",
                "error",
                f"cannot write {registry_path}: {exc.strerror or 'write failed'}",
            )
        )
        return _scaffold(bot_id, home, port, registry_path, ()), findings

    checklist = (
        f"created {home / 'config.json'} and {home / 'workspace'}",
        f"appended bot {bot_id!r} to {registry_path} with allowed_callers {list(allowed_callers)}",
        f"set {api_key_env} in the environment of the process that starts this bot",
        f"start the bot with: nanobot --home {home} serve --config {home / 'config.json'}",
        f"the bot answers on http://127.0.0.1:{port}/v1/chat/completions",
        f"to let it delegate onward, register the Teams MCP with --caller {bot_id} "
        f"and --config {registry_path} in its config",
        f"model preset: {preset}",
    )
    if provider_key_env:
        checklist = (
            *checklist,
            f"export {provider_key_env} with the provider key before starting the bot",
        )
    return _scaffold(bot_id, home, port, registry_path, checklist), findings


def _provider_for_scaffold(
    block: Mapping[str, object], provider_name: str
) -> tuple[dict, str | None]:
    """Copy a provider block, replacing a literal API key with an env reference."""
    copied = json.loads(json.dumps(block))
    if not isinstance(copied, dict):
        return {}, None
    key = copied.get("apiKey")
    if not isinstance(key, str) or not key:
        return copied, None
    if key.startswith("$" + "{") and key.endswith("}"):
        return copied, key[2:-1]
    safe = "".join(char if char.isalnum() else "_" for char in provider_name).upper()
    env_name = f"{safe}_API_KEY"
    copied["apiKey"] = "$" + "{" + env_name + "}"
    return copied, env_name


def check(
    registry_path: Path,
    *,
    caller: str | None = None,
    environ: Mapping[str, str] | None = None,
    user_config: Path | None = None,
    probe: bool = True,
    nanobot_version: str | None = None,
) -> list[Finding]:
    """Inspect the registry and its environment; return every finding."""
    env = os.environ if environ is None else environ

    try:
        config = load_teams_config(registry_path)
    except ConfigError as exc:
        return [Finding("registry_invalid", "error", str(exc))]

    scope = config.bots if caller is None else [bot for bot in config.bots if bot.id == caller]
    if caller is not None and not scope:
        return [Finding("unknown_caller", "error", f"caller {caller!r} is not in the registry")]

    findings: list[Finding] = []
    for bot in scope:
        findings.extend(_check_bot(bot, env, user_config or user_config_path(), probe))

    version = nanobot_version if nanobot_version is not None else _nanobot_version()
    if version is None:
        findings.append(
            Finding("nanobot_missing", "warn", "nanobot CLI not found on PATH; version unverified")
        )
    elif version != TESTED_NANOBOT:
        findings.append(
            Finding(
                "nanobot_untested",
                "warn",
                f"nanobot {version} differs from the tested baseline {TESTED_NANOBOT}",
            )
        )
    return findings


def _check_bot(
    bot: BotConfig, env: Mapping[str, str], user_config: Path, probe: bool
) -> list[Finding]:
    findings: list[Finding] = []
    if bot.api_key_env and not env.get(bot.api_key_env):
        findings.append(
            Finding("missing_env", "error", f"{bot.id}: {bot.api_key_env} is not set or empty")
        )
    if probe and bot.endpoint:
        parts = urlsplit(bot.endpoint)
        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        if port_in_use(host, port):
            findings.append(
                Finding("endpoint_up", "warn", f"{bot.id}: something answers on {host}:{port}")
            )
        else:
            findings.append(
                Finding(
                    "endpoint_down",
                    "warn",
                    f"{bot.id}: nothing answers on {host}:{port}; the bot may be stopped",
                )
            )
    if bot.model_preset:
        presets = _load_json(user_config)
        known = isinstance(presets, dict) and bot.model_preset in (
            presets.get("modelPresets") or {}
        )
        if not known:
            findings.append(
                Finding(
                    "unknown_preset",
                    "error",
                    f"{bot.id}: preset {bot.model_preset!r} is not in {user_config}",
                )
            )
    return findings


def _scaffold(
    bot_id: str,
    home: Path,
    port: int,
    registry_path: Path,
    checklist: tuple[str, ...],
) -> Scaffold:
    return Scaffold(
        bot_id=bot_id,
        home=home,
        port=port,
        api_key_env=api_key_env_for(bot_id),
        serve_command=f"nanobot --home {home} serve --config {home / 'config.json'}",
        checklist=checklist,
    )


def _read_registry(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or 'read failed'}") from exc
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ConfigError("registry must be a JSON object")
    return raw


def _registry_text(updated: dict) -> str:
    _reject_keys(updated, _REGISTRY_KEYS, "registry")
    for index, bot in enumerate(updated["bots"]):
        _reject_keys(bot, _BOT_KEYS, f"bots[{index}]")
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "probe.json"
        probe_path.write_text(json.dumps(updated), encoding="utf-8")
        load_teams_config(probe_path)
    return json.dumps(updated, indent=2, ensure_ascii=False) + "\n"


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _reject_keys(value: Mapping[str, object], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ConfigError(f"{where} has unknown key {unknown[0]!r}")


def _nanobot_version() -> str | None:
    try:
        result = subprocess.run(
            ["nanobot", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for token in result.stdout.split():
        if token[:1].isdigit():
            return token
    return None


def print_findings(findings: Sequence[Finding], stream=sys.stdout) -> int:
    for finding in findings:
        print(f"[{finding.severity}] {finding.code}: {finding.message}", file=stream)
    if any(finding.severity == "error" for finding in findings):
        return 1
    return 0
