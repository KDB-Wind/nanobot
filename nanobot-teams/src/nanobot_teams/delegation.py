"""Delegation state, session mapping, and the single-flight guard."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import BotConfig, TeamConfig
from .envelope import (
    EnvelopeError,
    envelope_for_root,
    new_task_id,
    preamble_envelope,
    task_with_preamble,
    validate_continuation,
)
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
    root_task_id: str | None = None
    hop: int | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "root_task_id": self.root_task_id,
                "hop": self.hop,
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


def session_id_for(config: TeamConfig, caller_id: str, target_bot_id: str) -> str:
    """Map a team thread, caller, and target to a stable session id; the hash only names a session."""
    canonical = json.dumps(
        [
            config.schema_version,
            config.team_id,
            caller_id,
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
        caller_id: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._api_keys = dict(api_keys)
        self._caller_id = caller_id
        self._logger = logger or logging.getLogger("nanobot_teams.delegation")
        self._busy = False
        self._blocked = False

    def list_bots(self) -> list[dict[str, Any]]:
        return [
            {
                "id": bot.id,
                "display_name": bot.display_name,
                "role": bot.role,
                "capabilities": list(bot.capabilities),
            }
            for bot in self._config.targets_for(self._caller_id)
        ]

    async def delegate_task(
        self,
        bot_id: str,
        task: str,
        *,
        parent_task_id: str | None = None,
        root_task_id: str | None = None,
        hop: int | None = None,
        deadline_remaining_s: float | None = None,
        chain: list[str] | None = None,
    ) -> DelegationResult:
        task_id = new_task_id()
        target = self._config.bot(bot_id)
        if target is None or not self._config.can_delegate(self._caller_id, bot_id):
            return self._refuse(task_id, bot_id, "invalid_target", _INVALID_TARGET)
        if not task or len(task.encode("utf-8")) > self._config.limits.task_max_bytes:
            return self._refuse(task_id, bot_id, "task_invalid", _INVALID_TASK)
        if self._blocked:
            return self._refuse(task_id, bot_id, "blocked", _BLOCKED)
        if self._busy:
            return self._refuse(task_id, bot_id, "busy", _BUSY)

        try:
            if any(
                value is not None
                for value in (parent_task_id, root_task_id, hop, deadline_remaining_s, chain)
            ):
                envelope = validate_continuation(
                    caller_id=self._caller_id,
                    target_id=bot_id,
                    parent_task_id=parent_task_id,
                    root_task_id=root_task_id,
                    hop=hop,
                    deadline_remaining_s=deadline_remaining_s,
                    chain=chain,
                    max_hops=self._config.limits.max_hops,
                    known_bots={bot.id for bot in self._config.bots},
                )
            else:
                envelope = envelope_for_root(
                    task_id, self._caller_id, self._config.limits.total_timeout_s
                )
        except EnvelopeError as exc:
            return self._refuse(task_id, bot_id, exc.error_code, exc.detail)

        effective_deadline = min(
            self._config.limits.total_timeout_s, envelope.deadline_remaining_s
        )
        outbound = task_with_preamble(
            task, preamble_envelope(envelope, task_id, bot_id), task_id
        )

        self._busy = True
        started = time.monotonic()
        try:
            outcome = await self._transport.send(
                endpoint=_endpoint_of(target),
                api_key=self._api_keys[bot_id],
                task=outbound,
                session_id=session_id_for(self._config, self._caller_id, bot_id),
                deadline_s=effective_deadline,
            )
        finally:
            self._busy = False

        if outcome.blocks_future:
            self._blocked = True
        result = _from_outcome(task_id, bot_id, outcome, envelope)
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
            "delegation task_id=%s root=%s hop=%s bot_id=%s duration_ms=%d status=%s error_code=%s",
            result.task_id,
            result.root_task_id or "-",
            result.hop if result.hop is not None else "-",
            result.bot_id,
            duration_ms,
            result.status.value,
            result.error_code or "-",
        )


def _from_outcome(
    task_id: str,
    bot_id: str,
    outcome: TransportOutcome,
    envelope: Any,
) -> DelegationResult:
    return DelegationResult(
        task_id=task_id,
        bot_id=bot_id,
        status=outcome.kind,
        content=outcome.content,
        truncated=outcome.truncated,
        error_code=outcome.error_code,
        detail=outcome.detail,
        root_task_id=envelope.root_task_id,
        hop=envelope.hop,
    )


def _endpoint_of(bot: BotConfig) -> str:
    assert bot.endpoint is not None
    return bot.endpoint


_INVALID_TARGET = "Target is not an allowed delegation target."
_INVALID_TASK = "Task is empty or exceeds the size limit."
_BLOCKED = "A previous delegation ended with an unknown outcome; verify the target first."
_BUSY = "Another delegation is already running."
