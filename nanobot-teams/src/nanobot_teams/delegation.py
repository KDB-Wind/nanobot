"""Delegation state, session mapping, and the single-flight guard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .config import TeamConfig
from .envelope import (
    Envelope,
    EnvelopeError,
    envelope_for_root,
    new_task_id,
    preamble_envelope,
    task_with_preamble,
    validate_continuation,
)
from .ledger import AdmissionError, Ledger, LedgerError, TaskRecord
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
    thread_key: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "root_task_id": self.root_task_id,
                "hop": self.hop,
                "thread_key": self.thread_key,
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


def session_id_for(config: TeamConfig, caller_id: str, target_bot_id: str,
                   thread_key: str | None = None) -> str:
    """Map a team thread, caller, and target to a stable session id; the hash only names a session."""
    canonical = json.dumps(
        [
            config.schema_version,
            config.team_id,
            caller_id,
            target_bot_id,
            config.thread_key,
            thread_key,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"teams-v1-{digest[:32]}"


class Delegator:
    """Own one in-flight request; the ledger owns durable admission and quarantine."""

    def __init__(
        self,
        config: TeamConfig,
        transport: HttpTransport,
        api_keys: Mapping[str, str],
        caller_id: str,
        logger: logging.Logger | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._api_keys = dict(api_keys)
        self._caller_id = caller_id
        self._logger = logger or logging.getLogger("nanobot_teams.delegation")
        self._ledger = ledger or Ledger(None)
        self._busy = False
        self._unsettled_endpoints: set[str] = set()
        if self._ledger.enabled:
            self._ledger.bind(config.team_id, caller_id)

    def list_tasks(
        self, limit: int = 50, target: str | None = None, status: str | None = None
    ) -> list[TaskRecord]:
        return self._ledger.list_tasks(limit=limit, target=target, status=status)

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._ledger.get_task(task_id)

    def list_bots(self) -> list[dict[str, Any]]:
        return [
            {
                "id": bot.id,
                "display_name": bot.display_name,
                "role": bot.role,
                "capabilities": list(bot.capabilities),
                "model_preset": bot.model_preset,
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
        thread_key: str | None = None,
        expires_at: float | None = None,
    ) -> DelegationResult:
        task_id = new_task_id()
        target = self._config.bot(bot_id)
        if target is None or not self._config.can_delegate(self._caller_id, bot_id):
            return self._refuse(task_id, bot_id, "invalid_target", _INVALID_TARGET)
        try:
            valid_task = bool(task.strip()) and len(task.encode("utf-8")) <= self._config.limits.task_max_bytes
        except UnicodeError:
            valid_task = False
        if not valid_task:
            return self._refuse(task_id, bot_id, "task_invalid", _INVALID_TASK)
        if self._busy:
            return self._refuse(task_id, bot_id, "busy", _BUSY)
        assert target.endpoint is not None
        if target.endpoint in self._unsettled_endpoints:
            return self._refuse(task_id, bot_id, "blocked", _BLOCKED)
        if not self._ledger.enabled:
            return self._refuse(task_id, bot_id, "ledger_unavailable", "A persistent --ledger is required.")
        if thread_key is not None and (not isinstance(thread_key, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", thread_key)):
            return self._refuse(task_id, bot_id, "invalid_thread", "Invalid thread key.")

        try:
            if any(
                value is not None
                for value in (parent_task_id, root_task_id, hop, deadline_remaining_s, chain, expires_at)
            ):
                if not self._config.allow_multihop:
                    return self._refuse(task_id, bot_id, "multihop_disabled", "Multi-hop requires explicit operator configuration.")
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
                    expires_at=expires_at,
                )
                root = self._config.bot(envelope.chain[0])
                if root is None or not root.can_start_root or any(
                    not self._config.can_delegate(left, right)
                    for left, right in zip(envelope.chain, envelope.chain[1:])
                ):
                    raise EnvelopeError("envelope_invalid", "Envelope ancestry does not follow the configured root and authorization graph.")
            else:
                caller = self._config.bot(self._caller_id)
                if caller is None or not caller.can_start_root:
                    return self._refuse(task_id, bot_id, "root_not_allowed", "This caller cannot start root tasks.")
                envelope = envelope_for_root(
                    task_id, self._caller_id, self._config.limits.total_timeout_s
                )
        except EnvelopeError as exc:
            return self._refuse(task_id, bot_id, exc.error_code, exc.detail)

        effective_deadline = min(self._config.limits.total_timeout_s, envelope.deadline_remaining_s)
        selected_thread = thread_key or task_id
        session_id = session_id_for(self._config, self._caller_id, bot_id, selected_thread)
        started = time.perf_counter()
        started_at = datetime.now(tz=timezone.utc).isoformat()
        entry = TaskRecord(
            task_id=task_id, root_task_id=envelope.root_task_id,
            parent_task_id=envelope.parent_task_id, caller=self._caller_id, target=bot_id,
            hop=envelope.hop, status="outcome_unknown", error_code="execution_pending",
            truncated=False, http_sent=True, request_bytes=0, response_bytes=0, result_bytes=0,
            started_at=started_at, finished_at=None, duration_ms=None,
            team_id=self._config.team_id, endpoint=target.endpoint,
            thread_key=selected_thread, session_id=session_id,
        )
        try:
            reservation = self._ledger.admit(
                entry, self._config.budgets.get(self._caller_id),
                self._config.limits.result_max_bytes, effective_deadline,
                continuing=thread_key is not None,
            )
        except AdmissionError as exc:
            return self._refuse(task_id, bot_id, exc.code, str(exc))
        except LedgerError:
            return self._refuse(task_id, bot_id, "ledger_unavailable", "Cannot commit admission; no request sent.")

        self._busy = True
        cancelled = False
        try:
            remaining = reservation.timeout_s - (time.perf_counter() - started)
            if remaining <= 0:
                outcome = TransportOutcome(OutcomeKind.failed, error_code="deadline_exceeded",
                                           detail="Deadline expired before HTTP submission.")
            else:
                envelope = replace(envelope, deadline_remaining_s=remaining,
                                   expires_at=min(envelope.expires_at, time.time() + remaining))
                outbound = task_with_preamble(
                    task, preamble_envelope(envelope, task_id, bot_id), task_id
                )
                try:
                    outcome = await self._transport.send(
                        endpoint=target.endpoint, api_key=self._api_keys[bot_id], task=outbound,
                        session_id=session_id, deadline_s=remaining,
                        result_max_bytes=reservation.result_max_bytes,
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    outcome = TransportOutcome(
                        OutcomeKind.outcome_unknown, error_code="cancelled",
                        detail="Local waiting was cancelled; remote effects are unknown.",
                        http_sent=True,
                    )
                except Exception:
                    outcome = TransportOutcome(
                        OutcomeKind.outcome_unknown, error_code="transport_error",
                        detail="Submission ended without a classified outcome.",
                        http_sent=True,
                    )
            duration_ms = math.ceil((time.perf_counter() - started) * 1000)
            result = replace(_from_outcome(task_id, bot_id, outcome, envelope), thread_key=selected_thread)
            finished = replace(
                entry, status=result.status.value, error_code=result.error_code,
                truncated=result.truncated, http_sent=outcome.http_sent,
                request_bytes=outcome.request_bytes, response_bytes=outcome.response_bytes,
                result_bytes=len(result.content.encode("utf-8")),
                finished_at=datetime.now(tz=timezone.utc).isoformat(), duration_ms=duration_ms,
            )
            try:
                self._ledger.finish(finished, unknown=outcome.blocks_future)
            except LedgerError:
                self._unsettled_endpoints.add(target.endpoint)
                result = replace(result, status=OutcomeKind.outcome_unknown, content="",
                                 error_code="ledger_unavailable",
                                 detail="Observation could not be committed; target remains quarantined.")
            self._log(result, duration_ms, outcome)
        finally:
            self._busy = False
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _refuse(self, task_id: str, bot_id: str, error_code: str, detail: str) -> DelegationResult:
        result = DelegationResult(
            task_id=task_id,
            bot_id=bot_id if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", bot_id) else "<invalid>",
            status=OutcomeKind.failed,
            error_code=error_code,
            detail=detail,
        )
        self._log(result, 0.0, TransportOutcome(OutcomeKind.failed, error_code=error_code))
        return result

    def _log(self, result: DelegationResult, duration_ms: float, outcome: TransportOutcome) -> None:
        self._logger.info(
            "delegation task_id=%s root=%s caller=%s target=%s hop=%s duration_ms=%d "
            "status=%s error_code=%s request_bytes=%d response_bytes=%d result_bytes=%d",
            result.task_id,
            result.root_task_id or "-",
            self._caller_id,
            result.bot_id,
            result.hop if result.hop is not None else "-",
            duration_ms,
            result.status.value,
            result.error_code or "-",
            outcome.request_bytes,
            outcome.response_bytes,
            len(result.content.encode("utf-8")),
        )


def _from_outcome(
    task_id: str,
    bot_id: str,
    outcome: TransportOutcome,
    envelope: Envelope,
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


_INVALID_TARGET = "Target is not an allowed delegation target."
_INVALID_TASK = "Task is empty or exceeds the size limit."
_BLOCKED = "A previous delegation ended with an unknown outcome; verify the target first."
_BUSY = "Another delegation is already running."
