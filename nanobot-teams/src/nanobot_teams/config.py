"""Team registry: bots with per-target caller allowlists, plus limits.

Schema 1 fixes one global caller and an allowed-target list. Schema 2 moves
authorization into each target's allowed_callers list and takes the instance
identity from the --caller command line argument. Both versions normalize into
the same TeamConfig model.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_BOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

_TOP_LEVEL_KEYS_V1 = frozenset(
    {
        "schema_version",
        "team_id",
        "caller_bot_id",
        "thread_key",
        "allowed_targets",
        "bots",
        "limits",
    }
)
_TOP_LEVEL_KEYS_V2 = frozenset({"schema_version", "team_id", "thread_key", "bots", "limits"})
_BOT_KEYS_V1 = frozenset({"id", "display_name", "role", "endpoint", "api_key_env"})
_BOT_KEYS_V2 = frozenset(
    {
        "id",
        "display_name",
        "role",
        "capabilities",
        "model_preset",
        "endpoint",
        "api_key_env",
        "allowed_callers",
    }
)
_LIMIT_KEYS = (
    "task_max_bytes",
    "response_max_bytes",
    "result_max_bytes",
    "connect_timeout_s",
    "request_timeout_s",
    "total_timeout_s",
    "max_hops",
)
_BYTE_LIMITS = frozenset({"task_max_bytes", "response_max_bytes", "result_max_bytes"})
_INT_LIMITS = frozenset({"task_max_bytes", "response_max_bytes", "result_max_bytes", "max_hops"})
_API_PATH = "/v1/chat/completions"
_ID_MAX = 128
_LABEL_MAX = 256
_MAX_CAPABILITIES = 8


class ConfigError(ValueError):
    """A teams.json file is unusable and startup must stop."""


@dataclass(frozen=True)
class Limits:
    task_max_bytes: int = 16384
    response_max_bytes: int = 262144
    result_max_bytes: int = 32768
    connect_timeout_s: float = 3.0
    request_timeout_s: float = 135.0
    total_timeout_s: float = 145.0
    max_hops: int = 2


@dataclass(frozen=True)
class BotConfig:
    id: str
    display_name: str
    role: str
    capabilities: tuple[str, ...] = ()
    model_preset: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    allowed_callers: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeamConfig:
    schema_version: int
    team_id: str
    thread_key: str
    bots: tuple[BotConfig, ...]
    limits: Limits = Limits()
    default_caller_id: str | None = None

    def bot(self, bot_id: str) -> BotConfig | None:
        return next((bot for bot in self.bots if bot.id == bot_id), None)

    def targets_for(self, caller_id: str) -> tuple[BotConfig, ...]:
        return tuple(bot for bot in self.bots if caller_id in bot.allowed_callers)

    def can_delegate(self, caller_id: str, target_id: str) -> bool:
        target = self.bot(target_id)
        return target is not None and caller_id in target.allowed_callers


def load_teams_config(path: str | Path) -> TeamConfig:
    """Read and validate a registry file. Never reads or stores secret values."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc.strerror or 'read failed'}") from exc
    except ValueError as exc:
        raise ConfigError(f"{config_path} is not valid JSON") from exc

    root = _object(raw, "config")
    schema_version = root.get("schema_version")
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise ConfigError("schema_version must be 1 or 2")

    if schema_version == 1:
        return _load_v1(root)
    return _load_v2(root)


def resolve_caller(config: TeamConfig, caller_arg: str | None) -> str:
    """Pick the instance caller identity and check it against the registry."""
    caller = caller_arg or config.default_caller_id
    if not caller:
        raise ConfigError("schema 2 needs --caller naming a bot in the registry")
    if config.bot(caller) is None:
        raise ConfigError(f"caller {caller!r} is not present in bots")
    if config.schema_version == 1 and caller != config.default_caller_id:
        raise ConfigError("schema 1 fixes the caller; --caller must match caller_bot_id")
    return caller


def resolve_api_keys(
    config: TeamConfig, caller_id: str, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Read one credential per reachable target, keyed by bot id. Values stay out of the config."""
    source = os.environ if environ is None else environ
    keys: dict[str, str] = {}
    for bot in config.targets_for(caller_id):
        assert bot.api_key_env is not None
        value = source.get(bot.api_key_env, "")
        if not value:
            raise ConfigError(f"environment variable {bot.api_key_env} is missing or empty")
        keys[bot.id] = value
    return keys


def _load_v1(root: dict[str, Any]) -> TeamConfig:
    _reject_unknown(root, _TOP_LEVEL_KEYS_V1, "config")
    team_id = _text(root.get("team_id"), "team_id")
    caller_bot_id = _text(root.get("caller_bot_id"), "caller_bot_id")
    thread_key = _text(root.get("thread_key"), "thread_key")
    allowed_targets = _allowed_targets(root.get("allowed_targets"))
    bots = _bots(root.get("bots"), _BOT_KEYS_V1)

    by_id = {bot.id: bot for bot in bots}
    if caller_bot_id not in by_id:
        raise ConfigError("caller_bot_id is not present in bots")
    if caller_bot_id in allowed_targets:
        raise ConfigError("caller_bot_id must not be an allowed target")
    for target in allowed_targets:
        if target not in by_id:
            raise ConfigError(f"allowed target {target!r} is not present in bots")
    normalized = []
    for bot in bots:
        if bot.id in allowed_targets:
            if bot.endpoint is None or bot.api_key_env is None:
                raise ConfigError(f"target bot {bot.id!r} needs endpoint and api_key_env")
            normalized.append(replace(bot, allowed_callers=(caller_bot_id,)))
        elif bot.endpoint is not None or bot.api_key_env is not None:
            raise ConfigError(f"bot {bot.id!r} is not an allowed target and cannot set endpoint")
        else:
            normalized.append(bot)
    return TeamConfig(
        schema_version=1,
        team_id=team_id,
        thread_key=thread_key,
        bots=tuple(normalized),
        limits=_limits(root.get("limits")),
        default_caller_id=caller_bot_id,
    )


def _load_v2(root: dict[str, Any]) -> TeamConfig:
    _reject_unknown(root, _TOP_LEVEL_KEYS_V2, "config")
    team_id = _text(root.get("team_id"), "team_id")
    thread_key = _text(root.get("thread_key"), "thread_key")
    bots = _bots(root.get("bots"), _BOT_KEYS_V2)
    _validate_v2_graph(bots)
    return TeamConfig(
        schema_version=2,
        team_id=team_id,
        thread_key=thread_key,
        bots=bots,
        limits=_limits(root.get("limits")),
    )


def _validate_v2_graph(bots: tuple[BotConfig, ...]) -> None:
    by_id = {bot.id: bot for bot in bots}
    for bot in bots:
        if bot.allowed_callers:
            if bot.endpoint is None or bot.api_key_env is None:
                raise ConfigError(
                    f"bot {bot.id!r} accepts callers and needs endpoint and api_key_env"
                )
        elif bot.endpoint is not None or bot.api_key_env is not None:
            raise ConfigError(
                f"bot {bot.id!r} accepts no callers and cannot set endpoint or api_key_env"
            )
        for caller in bot.allowed_callers:
            if caller == bot.id:
                raise ConfigError(f"bot {bot.id!r} must not list itself in allowed_callers")
            if caller not in by_id:
                raise ConfigError(
                    f"bot {bot.id!r} lists unknown caller {caller!r} in allowed_callers"
                )


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a JSON object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{where} has unknown key {unknown[0]!r}")


def _text(value: Any, where: str, max_len: int = _ID_MAX) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string")
    if not value or len(value) > max_len:
        raise ConfigError(f"{where} must be 1 to {max_len} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ConfigError(f"{where} must not contain control characters")
    return value


def _allowed_targets(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("allowed_targets must be a non-empty array")
    targets = tuple(value)
    for target in targets:
        if not isinstance(target, str) or not _BOT_ID.match(target):
            raise ConfigError("allowed_targets entries must be bot ids")
    if len(set(targets)) != len(targets):
        raise ConfigError("allowed_targets must not repeat a bot id")
    return targets


def _bots(value: Any, bot_keys: frozenset[str]) -> tuple[BotConfig, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("bots must be a non-empty array")
    bots: list[BotConfig] = []
    for index, raw in enumerate(value):
        where = f"bots[{index}]"
        entry = _object(raw, where)
        _reject_unknown(entry, bot_keys, where)
        bot_id = _text(entry.get("id"), f"{where}.id")
        if not _BOT_ID.match(bot_id):
            raise ConfigError(f"{where}.id must match {_BOT_ID.pattern}")
        endpoint = entry.get("endpoint")
        api_key_env = entry.get("api_key_env")
        model_preset = entry.get("model_preset")
        bots.append(
            BotConfig(
                id=bot_id,
                display_name=_text(
                    entry.get("display_name"), f"{where}.display_name", _LABEL_MAX
                ),
                role=_text(entry.get("role"), f"{where}.role", _LABEL_MAX),
                capabilities=_capabilities(entry.get("capabilities"), where),
                model_preset=(
                    None
                    if model_preset is None
                    else _text(model_preset, f"{where}.model_preset", _LABEL_MAX)
                ),
                endpoint=None if endpoint is None else _endpoint(endpoint, f"{where}.endpoint"),
                api_key_env=(
                    None if api_key_env is None else _env_name(api_key_env, f"{where}.api_key_env")
                ),
                allowed_callers=_caller_list(entry.get("allowed_callers"), where),
            )
        )
    ids = [bot.id for bot in bots]
    duplicated = next((bot_id for bot_id in ids if ids.count(bot_id) > 1), None)
    if duplicated is not None:
        raise ConfigError(f"bots repeats id {duplicated!r}")
    return tuple(bots)


def _capabilities(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}.capabilities must be a non-empty array")
    if len(value) > _MAX_CAPABILITIES:
        raise ConfigError(f"{where}.capabilities must list at most {_MAX_CAPABILITIES} entries")
    items = []
    for item in value:
        if not isinstance(item, str) or not _CAPABILITY.match(item):
            raise ConfigError(f"{where}.capabilities entries must be short lowercase tokens")
        items.append(item)
    if len(set(items)) != len(items):
        raise ConfigError(f"{where}.capabilities must not repeat an entry")
    return tuple(items)


def _caller_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}.allowed_callers must be a non-empty array")
    callers = tuple(value)
    for caller in callers:
        if not isinstance(caller, str) or not _BOT_ID.match(caller):
            raise ConfigError(f"{where}.allowed_callers entries must be bot ids")
    if len(set(callers)) != len(callers):
        raise ConfigError(f"{where}.allowed_callers must not repeat a bot id")
    return callers


def _limits(value: Any) -> Limits:
    if value is None:
        return Limits()
    entry = _object(value, "limits")
    _reject_unknown(entry, frozenset(_LIMIT_KEYS), "limits")
    overrides: dict[str, Any] = {}
    for key in _LIMIT_KEYS:
        if key not in entry:
            continue
        raw = entry[key]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ConfigError(f"limits.{key} must be a number")
        if raw <= 0:
            raise ConfigError(f"limits.{key} must be positive")
        if key in _INT_LIMITS and not isinstance(raw, int):
            raise ConfigError(f"limits.{key} must be an integer")
        if key == "max_hops" and not 1 <= raw <= 4:
            raise ConfigError("limits.max_hops must be between 1 and 4")
        overrides[key] = raw
    return Limits(**overrides)


def _endpoint(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string")
    if value != value.strip():
        raise ConfigError(f"{where} must not have surrounding whitespace")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"{where} is not a valid URL") from exc
    if parts.scheme != "http":
        raise ConfigError(f"{where} scheme must be http")
    if parts.hostname != "127.0.0.1":
        raise ConfigError(f"{where} host must be 127.0.0.1")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{where} must not contain userinfo")
    if port is None or not 1 <= port <= 65535:
        raise ConfigError(f"{where} needs an explicit port between 1 and 65535")
    if parts.path != _API_PATH:
        raise ConfigError(f"{where} path must be {_API_PATH}")
    if "?" in value or "#" in value:
        raise ConfigError(f"{where} must not contain a query or fragment")
    return value


def _env_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.match(value):
        raise ConfigError(f"{where} must be an environment variable name")
    return value
