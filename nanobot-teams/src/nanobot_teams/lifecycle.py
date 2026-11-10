"""Bot lifecycle tools: scaffold a new bot and check a registry for problems.

`bot init` writes a home, config, and workspace skeleton plus a printed
checklist, and appends the bot to the registry after the whole updated file
validates. It never starts a process. `check` loads the registry, probes
endpoints, environment variables, presets, and the installed nanobot version,
and prints one finding per line. Both write only where the operator points
them; the MCP server modules never import this file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import BotConfig, ConfigError, Limits, load_teams_config
from .file_lock import FileLock

TESTED_NANOBOT = "0.4.1"
_REGISTRY_KEYS = ("schema_version", "team_id", "thread_key", "bots", "limits", "budgets", "allow_multihop")
_BOT_KEYS = ("id", "display_name", "role", "capabilities", "model_preset", "endpoint", "api_key_env", "allowed_callers", "can_start_root")


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


def managed_bot_config(
    *,
    bot_id: str,
    home: Path,
    port: int,
    preset: str,
    runs_api_key_env: str,
    user_config: Path | None = None,
    coordinator_url: str = "",
    coord_token_env: str = "",
) -> tuple[dict, set[str]]:
    """Build a loadable JSON config for a managed async-workbench bot.

    The model preset is copied from the operator's user config with every
    credential-carrying string externalized to ``${ENV}`` references (the same
    convention as ``bot_init``), so the generated file never carries a raw
    service key and the browser-facing scaffold never sees one (F13). The
    runs API key comes from ``runs_api_key_env``. Returns the config dict and
    the set of environment names that must be set before the bot can load.

    When ``coordinator_url`` and ``coord_token_env`` are both given the bot
    also becomes a peer: its serve listener enables the runs inbox and a
    teams-async MCP client (send_to_bot and friends) is attached, with the
    coordinator credential referenced via ``${ENV}`` like every other secret.
    """
    source = user_config or user_config_path()
    user = _load_json(source)
    preset_block: dict | None = None
    provider_block: dict = {}
    required_env: set[str] = set()
    model = ""
    preset_env_prefix = "TEAMS_" + bot_id.upper().replace("-", "_").replace(".", "_") + "_PRESET"
    if isinstance(user, dict):
        presets = user.get("modelPresets") or {}
        if preset in presets:
            preset_block, preset_env = _connection_for_scaffold(presets[preset], preset_env_prefix, preset=True)
            required_env.update(preset_env)
            if isinstance(preset_block, dict):
                model = preset_block.get("model", "")
                provider_name = preset_block.get("provider")
                providers = user.get("providers") or {}
                if provider_name in providers:
                    block, provider_env = _connection_for_scaffold(providers[provider_name], provider_name)
                    required_env.update(provider_env)
                    provider_block = {provider_name: block}
    if not isinstance(preset_block, dict) or not isinstance(preset_block.get("model"), str) or not preset_block["model"]:
        raise ConfigError(
            f"model preset {preset!r} was not found with a usable model block in "
            f"{source}; register it before scaffolding a managed bot"
        )
    if not runs_api_key_env.isidentifier():
        raise ConfigError("runs_api_key_env must be a valid environment variable name")
    workspace = (home / "workspace").resolve()
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
        "modelPresets": {preset: preset_block},
        "providers": provider_block,
        "api": {
            "host": "127.0.0.1",
            "port": port,
            "timeout": 120,
            "runs": {
                "enabled": True,
                "apiKey": "${" + runs_api_key_env + "}",
                "principalId": "teams-coordinator",
                "executionEnabled": True,
                "exclusiveExecution": True,
                **({"inboxEnabled": True} if coordinator_url else {}),
            },
        },
        "tools": {
            "restrictToWorkspace": True,
            "exec": {"enable": False},
            "mcpServers": (
                {
                    "teams-async": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [
                            "-m", "nanobot_teams", "async-mcp",
                            "--url", coordinator_url,
                            "--token-env", coord_token_env,
                        ],
                        "env": {
                        coord_token_env: "${" + coord_token_env + "}",
                        "PYTHONPATH": os.pathsep.join(
                            p for p in (
                                str(Path(__file__).resolve().parent.parent),
                                os.environ.get("PYTHONPATH", ""),
                            ) if p
                        ),
                    },
                        "teamsAsync": True,
                        "toolTimeout": 600,
                    },
                }
                if coordinator_url and coord_token_env
                else {}
            ),
        },
        "channels": {},
    }
    required_env.add(runs_api_key_env)
    if coordinator_url and coord_token_env:
        required_env.add(coord_token_env)
    return config, required_env


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
    try:
        lock = FileLock(Path(str(registry_path.resolve()) + ".lock"))
    except OSError:
        return _scaffold(bot_id, home, port, registry_path, ()), [Finding("registry_busy", "error", "Cannot exclusively lock registry.")]
    try:
        findings: list[Finding] = []
        if not role.strip():
            findings.append(
                Finding("bad_role", "error", "role must be a non-empty one-line description")
            )
        if not 1 <= port <= 64535:
            findings.append(Finding("bad_port", "error", "API port must be 1 to 64535 (gateway uses port + 1000)"))
            return _scaffold(bot_id, home, port, registry_path, ()), findings
        if home.exists() or home.is_symlink():
            findings.append(Finding("home_exists", "error", "home must not already exist; choose a new home"))
        if port_in_use("127.0.0.1", port) or port_in_use("127.0.0.1", port + 1000):
            findings.append(
                Finding("port_busy", "error", f"port {port} on 127.0.0.1 already answers connections")
            )

        try:
            registry, registry_before = _read_registry(registry_path)
            if registry.get("schema_version") != 2:
                raise ConfigError("bot init needs a schema_version 2 registry")
            if any(bot.get("id") == bot_id for bot in registry.get("bots", [])):
                findings.append(Finding("duplicate_bot", "error", f"bots already lists {bot_id!r}"))
            if any(bot.get("api_key_env") == api_key_env_for(bot_id) for bot in registry.get("bots", [])):
                findings.append(Finding("key_env_collision", "error", "generated API key environment name collides with an existing bot"))
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
        required_env: set[str] = set()
        model = ""
        if isinstance(user, dict):
            presets = user.get("modelPresets") or {}
            if preset in presets:
                preset_block, preset_env = _connection_for_scaffold(presets[preset], "TEAMS_" + bot_id.upper().replace("-", "_").replace(".", "_") + "_PRESET", preset=True)
                required_env.update(preset_env)
                if isinstance(preset_block, dict):
                    model = preset_block.get("model", "")
                    provider_name = preset_block.get("provider")
                    providers = user.get("providers") or {}
                    if provider_name in providers:
                        block, provider_env = _connection_for_scaffold(providers[provider_name], provider_name)
                        required_env.update(provider_env)
                        provider_block = {provider_name: block}
            else:
                findings.append(
                    Finding(
                        "unknown_preset",
                        "error",
                        f"preset {preset!r} is not in {source_config}; the generated config "
                        "still names it, fix the model blocks before starting",
                    )
                )

        if not isinstance(preset_block, dict) or not isinstance(preset_block.get("model"), str) or not preset_block["model"]:
            findings.append(Finding("unknown_preset", "error", "A valid source model preset is required before creating a bot."))
            return _scaffold(bot_id, home, port, registry_path, ()), findings

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

        workspace = (home / "workspace").resolve()
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
        created_home = False
        try:
            if registry_path.read_text(encoding="utf-8") != registry_before:
                raise OSError("Registry changed during scaffold generation.")
            home.mkdir(parents=True, exist_ok=False, mode=0o700)
            created_home = True
            workspace.mkdir(mode=0o700)
            _atomic_write(home / "config.json", json.dumps(config, indent=2))
            _atomic_write(registry_path, registry_text)
        except OSError as exc:
            if created_home:
                (home / "config.json").unlink(missing_ok=True)
                if workspace.exists():
                    workspace.rmdir()
                home.rmdir()
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
            f"start the bot with: {_serve_command(home)}",
            f"the bot answers on http://127.0.0.1:{port}/v1/chat/completions",
            f"to let it delegate onward, register the Teams MCP with --caller {bot_id} "
            f"and --config {registry_path} in its config",
            f"model preset: {preset}",
        )
        checklist = (*checklist, *(f"export {name} from the source connection before starting the bot" for name in sorted(required_env)))
        return _scaffold(bot_id, home, port, registry_path, checklist), findings
    except (ConfigError, ValueError, TypeError, KeyError):
        return _scaffold(bot_id, home, port, registry_path, ()), [Finding("source_invalid", "error", "Source connection or preset is malformed; no configuration was copied.")]
    finally:
        lock.close()


def _atomic_write(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _connection_for_scaffold(block: Mapping[str, object], prefix: str, *, preset: bool = False) -> tuple[dict, set[str]]:
    safe_prefix = re.sub(r"[^A-Za-z0-9_]", "_", prefix).upper()
    if not safe_prefix or safe_prefix[0].isdigit():
        safe_prefix = "TEAMS_" + safe_prefix
    required: set[str] = set()

    def externalize(value, path: str):
        if isinstance(value, dict):
            return {key: externalize(item, path + "." + key) for key, item in value.items()}
        if isinstance(value, list):
            return [externalize(item, path + "." + str(index)) for index, item in enumerate(value)]
        if not isinstance(value, str) or not value:
            return value
        if re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value):
            name = value[2:-1]
        elif path == "apiKey":
            name = safe_prefix + "_API_KEY"
        else:
            suffix = re.sub(r"[^A-Za-z0-9_]", "_", path).upper()[:80]
            name = safe_prefix + "_" + suffix + "_" + hashlib.sha256(path.encode()).hexdigest()[:8].upper()
        required.add(name)
        return "${" + name + "}"

    aliases = {"api_key": "apiKey", "api_base": "apiBase", "extra_headers": "extraHeaders",
               "extra_query": "extraQuery", "extra_body": "extraBody"}
    allowed = ({"model", "provider", "maxTokens", "contextWindowTokens", "temperature", "reasoningEffort", "api", "extraHeaders", "extraBody", "extraQuery"}
               if preset else {"apiKey", "apiBase", "api", "extraHeaders", "extraBody", "extraQuery", "useProxy", "thinkingStyle", "displayName"})
    copied = {}
    for raw_key, value in block.items():
        key = aliases.get(raw_key, raw_key)
        if key == "api":
            if not isinstance(value, dict):
                raise ConfigError("api capabilities must be an object")
            valid_apis = {"chat_completions", "responses", "anthropic_messages"}
            supported = value.get("supportedApis", value.get("supported_apis"))
            preferred = value.get("preferredApi", value.get("preferred_api"))
            if not isinstance(supported, list) or not supported or any(item not in valid_apis for item in supported):
                raise ConfigError("invalid supported APIs")
            if preferred is not None and preferred not in supported:
                raise ConfigError("invalid preferred API")
            copied[key] = {"supportedApis": supported, **({"preferredApi": preferred} if preferred else {})}
        elif key in allowed:
            copied[key] = externalize(value, key) if key in {"apiKey", "apiBase", "extraHeaders", "extraBody", "extraQuery"} else value
    return copied, required


def check(
    registry_path: Path,
    *,
    caller: str | None = None,
    environ: Mapping[str, str] | None = None,
    user_config: Path | None = None,
    probe: bool = True,
    nanobot_version: str | None = None,
    deployment: Path | None = None,
) -> list[Finding]:
    """Inspect the registry and its environment; return every finding."""
    env = os.environ if environ is None else environ

    try:
        config = load_teams_config(registry_path)
    except ConfigError as exc:
        return [Finding("registry_invalid", "error", str(exc))]

    scope = config.bots if caller is None else config.targets_for(caller)
    if caller is not None and config.bot(caller) is None:
        return [Finding("unknown_caller", "error", f"caller {caller!r} is not in the registry")]

    findings: list[Finding] = []
    findings.extend(_check_timeouts(config.limits, deployment, [bot.id for bot in scope if bot.endpoint]))
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
        serve_command=_serve_command(home),
        checklist=checklist,
    )


def _read_registry(path: Path) -> tuple[dict, str]:
    try:
        original = path.read_text(encoding="utf-8")
        raw = json.loads(original)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or 'read failed'}") from exc
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ConfigError("registry must be a JSON object")
    return raw, original


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


def _check_timeouts(limits: Limits, deployment: Path | None, target_ids: list[str]) -> list[Finding]:
    if deployment is None:
        return [Finding("timeouts_unverified", "warn", "No deployment manifest: target queue deadline, MCP tool timeout and Planner entry timeout are unverified.")]
    data = _load_json(deployment)
    if data is None:
        return [Finding("deployment_invalid", "error", "Deployment manifest must be a JSON object.")]
    findings = []
    def positive(value):
        return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value) and value > 0
    tool = data.get("mcp_tool_timeout_s")
    planner = data.get("planner_timeout_s")
    if not positive(tool) or tool <= limits.total_timeout_s + 5:
        findings.append(Finding("tool_timeout_short", "error", "MCP tool timeout must exceed Teams total timeout by more than 5s."))
    if not positive(planner) or not positive(tool) or planner <= tool + 5:
        findings.append(Finding("planner_timeout_short", "error", "Planner entry timeout must exceed MCP tool timeout by more than 5s."))
    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        return [Finding("deployment_invalid", "error", "targets must be an object.")]
    for bot_id in target_ids:
        declaration = targets.get(bot_id, {})
        if not isinstance(declaration, dict):
            declaration = {}
        timeout = declaration.get("api_timeout_s")
        if not positive(timeout) or timeout + 5 >= min(limits.request_timeout_s, limits.total_timeout_s):
            findings.append(Finding("target_timeout_invalid", "error", f"{bot_id}: API deadline plus 5s must fit both local timeouts."))
        if declaration.get("queue_included") is not True:
            findings.append(Finding("queue_timeout_unverified", "error", f"{bot_id}: declare an API deadline that includes queue waiting."))
    findings.append(Finding("timeouts_declared", "warn", "Timeout values are operator declarations; remote settings are not authenticated or introspected."))
    return findings


def _serve_command(home: Path) -> str:
    def quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'" if os.name == "nt" else shlex.quote(str(path))
    return f"nanobot --home {quote(home)} serve --config {quote(home / 'config.json')}"
