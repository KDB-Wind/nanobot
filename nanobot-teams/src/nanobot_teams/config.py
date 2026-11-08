"""Static team registry: one caller, a fixed thread, and explicit targets."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_BOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TOP_LEVEL_KEYS = frozenset(
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
_BOT_KEYS = frozenset({"id", "display_name", "role", "endpoint", "api_key_env"})
_LIMIT_KEYS = (
    "task_max_bytes",
    "response_max_bytes",
    "result_max_bytes",
    "connect_timeout_s",
    "request_timeout_s",
    "total_timeout_s",
)
_BYTE_LIMITS = frozenset({"task_max_bytes", "response_max_bytes", "result_max_bytes"})
_API_PATH = "/v1/chat/completions"
_ID_MAX = 128
_LABEL_MAX = 256


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


@dataclass(frozen=True)
class BotConfig:
    id: str
    display_name: str
    role: str
    endpoint: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class TeamConfig:
    schema_version: int
    team_id: str
    caller_bot_id: str
    thread_key: str
    allowed_targets: tuple[str, ...]
    bots: tuple[BotConfig, ...]
    limits: Limits = Limits()

    def bot(self, bot_id: str) -> BotConfig | None:
        return next((bot for bot in self.bots if bot.id == bot_id), None)

    def targets(self) -> tuple[BotConfig, ...]:
        by_id = {bot.id: bot for bot in self.bots}
        return tuple(by_id[bot_id] for bot_id in self.allowed_targets)


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
    _reject_unknown(root, _TOP_LEVEL_KEYS, "config")

    schema_version = root.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ConfigError("schema_version must be 1")

    team_id = _text(root.get("team_id"), "team_id")
    caller_bot_id = _text(root.get("caller_bot_id"), "caller_bot_id")
    thread_key = _text(root.get("thread_key"), "thread_key")

    allowed_targets = _allowed_targets(root.get("allowed_targets"))
    bots = _bots(root.get("bots"))
    limits = _limits(root.get("limits"))

    _validate_registry(bots, allowed_targets, caller_bot_id)
    return TeamConfig(
        schema_version=schema_version,
        team_id=team_id,
        caller_bot_id=caller_bot_id,
        thread_key=thread_key,
        allowed_targets=allowed_targets,
        bots=bots,
        limits=limits,
    )


def resolve_api_keys(
    config: TeamConfig, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Read every target credential once, keyed by bot id. Values stay out of the config."""
    source = os.environ if environ is None else environ
    keys: dict[str, str] = {}
    for bot in config.targets():
        assert bot.api_key_env is not None
        value = source.get(bot.api_key_env, "")
        if not value:
            raise ConfigError(f"environment variable {bot.api_key_env} is missing or empty")
        keys[bot.id] = value
    return keys


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


def _bots(value: Any) -> tuple[BotConfig, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("bots must be a non-empty array")
    bots: list[BotConfig] = []
    for index, raw in enumerate(value):
        where = f"bots[{index}]"
        entry = _object(raw, where)
        _reject_unknown(entry, _BOT_KEYS, where)
        bot_id = _text(entry.get("id"), f"{where}.id")
        if not _BOT_ID.match(bot_id):
            raise ConfigError(f"{where}.id must match {_BOT_ID.pattern}")
        endpoint = entry.get("endpoint")
        api_key_env = entry.get("api_key_env")
        bots.append(
            BotConfig(
                id=bot_id,
                display_name=_text(
                    entry.get("display_name"), f"{where}.display_name", _LABEL_MAX
                ),
                role=_text(entry.get("role"), f"{where}.role", _LABEL_MAX),
                endpoint=None if endpoint is None else _endpoint(endpoint, f"{where}.endpoint"),
                api_key_env=(
                    None if api_key_env is None else _env_name(api_key_env, f"{where}.api_key_env")
                ),
            )
        )
    ids = [bot.id for bot in bots]
    duplicated = next((bot_id for bot_id in ids if ids.count(bot_id) > 1), None)
    if duplicated is not None:
        raise ConfigError(f"bots repeats id {duplicated!r}")
    return tuple(bots)


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
        if key in _BYTE_LIMITS and not isinstance(raw, int):
            raise ConfigError(f"limits.{key} must be an integer")
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


def _validate_registry(
    bots: tuple[BotConfig, ...], allowed_targets: tuple[str, ...], caller_bot_id: str
) -> None:
    by_id = {bot.id: bot for bot in bots}
    if caller_bot_id not in by_id:
        raise ConfigError("caller_bot_id is not present in bots")
    if caller_bot_id in allowed_targets:
        raise ConfigError("caller_bot_id must not be an allowed target")
    for target in allowed_targets:
        if target not in by_id:
            raise ConfigError(f"allowed target {target!r} is not present in bots")
    for bot in bots:
        carries_target_fields = bot.endpoint is not None or bot.api_key_env is not None
        if bot.id in allowed_targets:
            if bot.endpoint is None or bot.api_key_env is None:
                raise ConfigError(f"target bot {bot.id!r} needs endpoint and api_key_env")
        elif carries_target_fields:
            raise ConfigError(f"bot {bot.id!r} is not an allowed target and cannot set endpoint")
