"""Delegation state, session mapping, and the single-flight guard."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from .config import BotConfig, TeamConfig
from .transport import HttpTransport, OutcomeKind, TransportOutcome

SEMANTIC_STATUS = "unverified"


@dataclass(frozen=True)
class DelegationResult:
    task_id: str
    bot_id: str
    status: OutcomeKind
    content: str = ""
    truncated: bool = False
    error_code: str | None = None
    detail: str | None = None
    semantic_status: str = SEMANTIC_STATUS

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "bot_id": self.bot_id,
                "status": self.status.value,
                "semantic_status": self.semantic_status,
                "content": self.content,
                "truncated": self.truncated,
                "error_code": self.error_code,
                "detail": self.detail,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def session_id_for(config: TeamConfig, target_bot_id: str) -> str:
    """Map a team thread and target to a stable session id; the hash only names a session."""
    canonical = json.dumps(
        [
            config.schema_version,
            config.team_id,
            config.caller_bot_id,
            target_bot_id,
            config.thread_key,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"teams-v1-{digest[:32]}"


class Delegator:
    """Owns one in-flight delegation and the process-wide block flag.

    A delegation that ends outcome_unknown or timed_out sets a permanent
    in-process block. Later calls fail with error_code=blocked and make no HTTP
    request. The flag dies with the process, so an MCP reconnect that restarts
    the subprocess clears it; a restart is not evidence that remote work ended
    and does not make the next delegation safe.
    """

    def __init__(
        self,
        config: TeamConfig,
        transport: HttpTransport,
        api_keys: Mapping[str, str],
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._api_keys = dict(api_keys)
        self._logger = logger or logging.getLogger("nanobot_teams.delegation")
        self._busy = False
        self._blocked = False

    def list_bots(self) -> list[dict[str, str]]:
        return [
            {"id": bot.id, "display_name": bot.display_name, "role": bot.role}
            for bot in self._config.targets()
        ]

    async def delegate_task(self, bot_id: str, task: str) -> DelegationResult:
        task_id = str(uuid.uuid4())
        target = self._config.bot(bot_id)
        if target is None or bot_id not in self._config.allowed_targets:
            return self._refuse(task_id, bot_id, "invalid_target", _INVALID_TARGET)
        if not task or len(task.encode("utf-8")) > self._config.limits.task_max_bytes:
            return self._refuse(task_id, bot_id, "task_invalid", _INVALID_TASK)
        if self._blocked:
            return self._refuse(task_id, bot_id, "blocked", _BLOCKED)
        if self._busy:
            return self._refuse(task_id, bot_id, "busy", _BUSY)

        self._busy = True
        started = time.monotonic()
        try:
            outcome = await self._transport.send(
                endpoint=_endpoint_of(target),
                api_key=self._api_keys[bot_id],
                task=task,
                session_id=session_id_for(self._config, bot_id),
            )
        finally:
            self._busy = False

        if outcome.blocks_future:
            self._blocked = True
        result = _from_outcome(task_id, bot_id, outcome)
        self._log(result, (time.monotonic() - started) * 1000)
        return result

    def _refuse(self, task_id: str, bot_id: str, error_code: str, detail: str) -> DelegationResult:
        result = DelegationResult(
            task_id=task_id,
            bot_id=bot_id,
            status=OutcomeKind.failed,
            error_code=error_code,
            detail=detail,
        )
        self._log(result, 0.0)
        return result

    def _log(self, result: DelegationResult, duration_ms: float) -> None:
        self._logger.info(
            "delegation task_id=%s bot_id=%s duration_ms=%d status=%s error_code=%s",
            result.task_id,
            result.bot_id,
            duration_ms,
            result.status.value,
            result.error_code or "-",
        )


def _endpoint_of(bot: BotConfig) -> str:
    assert bot.endpoint is not None
    return bot.endpoint


def _from_outcome(task_id: str, bot_id: str, outcome: TransportOutcome) -> DelegationResult:
    return DelegationResult(
        task_id=task_id,
        bot_id=bot_id,
        status=outcome.kind,
        content=outcome.content,
        truncated=outcome.truncated,
        error_code=outcome.error_code,
        detail=outcome.detail,
    )


_INVALID_TARGET = "Target is not an allowed delegation target."
_INVALID_TASK = "Task is empty or exceeds the size limit."
_BLOCKED = "A previous delegation ended with an unknown outcome; verify the target first."
_BUSY = "Another delegation is already running."
