"""Coordinator configuration: control dir, loopback listener, token env names."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

_ALLOWED_KEYS = frozenset(
    {
        "control_dir",
        "listen",
        "admin_token_env",
        "planner_token_env",
        "planner",
        "bot_homes_root",
        "scaffold_model_preset",
        "scaffold_user_config",
    }
)


class ConfigError(ValueError):
    """The coordinator config file is unusable and startup must stop."""


@dataclass(frozen=True)
class PlannerBinding:
    inbox_url: str
    instance_id: str
    principal_id: str
    inbox_key_env: str = "TEAMS_PLANNER_INBOX_KEY"


@dataclass(frozen=True)
class CoordinatorConfig:
    control_dir: Path
    host: str = "127.0.0.1"
    port: int = 8910
    admin_token_env: str = "TEAMS_COORDINATOR_ADMIN_TOKEN"
    planner_token_env: str = "TEAMS_COORDINATOR_PLANNER_TOKEN"
    planner: PlannerBinding | None = None
    bot_homes_root: Path | None = None
    # Model preset copied into scaffolded bot homes (from the operator's user
    # config); empty disables scaffolding until the operator names one.
    scaffold_model_preset: str = ""
    # Where the operator's model presets live; defaults to lifecycle's
    # user_config_path() when None.
    scaffold_user_config: Path | None = None
    source_path: Path | None = field(default=None, compare=False)

    def resolve_tokens(self, env: dict[str, str] | None = None) -> dict[str, str]:
        environ = env if env is not None else dict(os.environ)
        admin = environ.get(self.admin_token_env, "").strip()
        planner = environ.get(self.planner_token_env, "").strip()
        if not admin or not planner:
            raise ConfigError(
                f"Both {self.admin_token_env} and {self.planner_token_env} must be set "
                "and non-empty."
            )
        if admin == planner:
            raise ConfigError("The admin and planner role keys must differ.")
        return {"operator": admin, "planner": planner}

    def validate(self) -> None:
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ConfigError("v1 refuses non-loopback coordinator listeners.")
        if not 1 <= self.port <= 65535:
            raise ConfigError("listen.port must be a valid port.")
        self.resolve_tokens()


def load_coordinator_config(path: str | Path) -> CoordinatorConfig:
    file_path = Path(path).resolve()
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read coordinator config {file_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Coordinator config must be a JSON object.")
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"Unknown coordinator config keys: {sorted(unknown)}")
    listen = raw.get("listen") or {}
    if not isinstance(listen, dict):
        raise ConfigError("listen must be an object.")
    planner_raw = raw.get("planner")
    planner: PlannerBinding | None = None
    if planner_raw is not None:
        if not isinstance(planner_raw, dict):
            raise ConfigError("planner must be an object.")
        missing = {"inbox_url", "instance_id", "principal_id"} - set(planner_raw)
        if missing:
            raise ConfigError(f"planner binding missing fields: {sorted(missing)}")
        planner = PlannerBinding(
            inbox_url=str(planner_raw["inbox_url"]),
            instance_id=str(planner_raw["instance_id"]),
            principal_id=str(planner_raw["principal_id"]),
            inbox_key_env=str(planner_raw.get("inbox_key_env", "TEAMS_PLANNER_INBOX_KEY")),
        )
    control_dir = Path(raw.get("control_dir") or (file_path.parent / "coordinator-data")).resolve()
    homes = raw.get("bot_homes_root")
    preset_raw = raw.get("scaffold_model_preset")
    if preset_raw is None:
        scaffold_preset = ""
    else:
        if not isinstance(preset_raw, str):
            raise ConfigError("scaffold_model_preset must be a string.")
        scaffold_preset = preset_raw.strip()
        if not scaffold_preset:
            raise ConfigError("scaffold_model_preset must be non-empty when set.")
    user_config_raw = raw.get("scaffold_user_config")
    if user_config_raw is None:
        scaffold_user_config: Path | None = None
    else:
        if not isinstance(user_config_raw, str) or not user_config_raw.strip():
            raise ConfigError("scaffold_user_config must be a non-empty path string.")
        scaffold_user_config = Path(user_config_raw).expanduser().resolve()
    config = CoordinatorConfig(
        control_dir=control_dir,
        host=str(listen.get("host", "127.0.0.1")),
        port=int(listen.get("port", 8910)),
        admin_token_env=str(raw.get("admin_token_env", "TEAMS_COORDINATOR_ADMIN_TOKEN")),
        planner_token_env=str(raw.get("planner_token_env", "TEAMS_COORDINATOR_PLANNER_TOKEN")),
        planner=planner,
        bot_homes_root=Path(homes).resolve() if homes else None,
        scaffold_model_preset=scaffold_preset,
        scaffold_user_config=scaffold_user_config,
        source_path=file_path,
    )
    config.validate()
    return config
