"""Delegation envelopes: hop budget, chain ancestry, and the task preamble.

A root delegation carries no envelope arguments. Every task sent to a target
is prefixed with a machine-readable block so a middle-hop bot can continue the
chain: it copies the fields into its own delegate_task call per its skill
instructions. The values arrive through model mediation, so the MCP treats
them as structured, untrusted input and enforces the invariants locally:
hop bounds, chain shape, no revisited bot, and a deadline that only shrinks.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

_OPEN = "[teams-envelope]"
_CLOSE = "[/teams-envelope]"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_BOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DEADLINE_MAX = 86400.0
_CHILD_RESERVE = 0.9

ENVELOPE_KEYS = (
    "parent_task_id",
    "root_task_id",
    "hop",
    "deadline_remaining_s",
    "chain",
)


class EnvelopeError(ValueError):
    """The caller-supplied envelope fields are unusable; no request leaves."""

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class Envelope:
    root_task_id: str
    parent_task_id: str | None
    hop: int
    deadline_remaining_s: float
    chain: tuple[str, ...]


def envelope_for_root(task_id: str, caller_id: str, deadline_s: float) -> Envelope:
    return Envelope(
        root_task_id=task_id,
        parent_task_id=None,
        hop=1,
        deadline_remaining_s=deadline_s,
        chain=(caller_id,),
    )


def validate_continuation(
    *,
    caller_id: str,
    target_id: str,
    parent_task_id: Any,
    root_task_id: Any,
    hop: Any,
    deadline_remaining_s: Any,
    chain: Any,
    max_hops: int,
    known_bots: frozenset[str] | set[str],
) -> Envelope:
    """Check one middle-hop delegation and return the envelope it runs under."""
    present = [
        key
        for key, value in (
            ("parent_task_id", parent_task_id),
            ("root_task_id", root_task_id),
            ("hop", hop),
            ("deadline_remaining_s", deadline_remaining_s),
            ("chain", chain),
        )
        if value is not None
    ]
    if len(present) != len(ENVELOPE_KEYS):
        missing = sorted(set(ENVELOPE_KEYS) - set(present))
        raise EnvelopeError(
            "envelope_invalid",
            f"A continued delegation needs every envelope field; missing {missing}.",
        )

    for name, value in (("parent_task_id", parent_task_id), ("root_task_id", root_task_id)):
        if not isinstance(value, str) or not _UUID.match(value):
            raise EnvelopeError("envelope_invalid", f"{name} must be a task id.")

    if isinstance(hop, bool) or not isinstance(hop, int) or hop < 2:
        raise EnvelopeError("envelope_invalid", "hop must be an integer of 2 or more.")
    if hop > max_hops:
        raise EnvelopeError(
            "hop_limit",
            f"The chain allows at most {max_hops} hops.",
        )

    if isinstance(deadline_remaining_s, bool) or not isinstance(deadline_remaining_s, int | float):
        raise EnvelopeError("envelope_invalid", "deadline_remaining_s must be a number.")
    if not math.isfinite(deadline_remaining_s):
        raise EnvelopeError("envelope_invalid", "deadline_remaining_s must be a finite number.")
    if deadline_remaining_s <= 0:
        raise EnvelopeError(
            "budget_exhausted", "The chain deadline is spent; no budget remains."
        )
    if deadline_remaining_s > _DEADLINE_MAX:
        raise EnvelopeError("envelope_invalid", "deadline_remaining_s exceeds the cap.")

    if not isinstance(chain, list) or not chain:
        raise EnvelopeError("envelope_invalid", "chain must be a non-empty array of bot ids.")
    entries: list[str] = []
    for entry in chain:
        if not isinstance(entry, str) or not _BOT_ID.match(entry):
            raise EnvelopeError("envelope_invalid", "chain entries must be bot ids.")
        if entry not in known_bots:
            raise EnvelopeError("envelope_invalid", f"chain names unknown bot {entry!r}.")
        entries.append(entry)
    if len(set(entries)) != len(entries):
        raise EnvelopeError("envelope_invalid", "chain must not repeat a bot id.")
    if entries[-1] != caller_id:
        raise EnvelopeError("envelope_invalid", "chain must end with the calling bot.")
    if len(entries) != hop:
        raise EnvelopeError(
            "envelope_invalid", "chain length must equal hop (root caller through you)."
        )
    if target_id in entries:
        raise EnvelopeError(
            "loop_detected",
            "The target is already in the delegation chain; delegating back would loop.",
        )

    return Envelope(
        root_task_id=root_task_id,
        parent_task_id=parent_task_id,
        hop=hop,
        deadline_remaining_s=float(deadline_remaining_s),
        chain=tuple(entries),
    )


def preamble_envelope(envelope: Envelope, task_id: str, target_id: str) -> Envelope:
    """Derive the envelope block the target receives. The deadline only shrinks."""
    return Envelope(
        root_task_id=envelope.root_task_id,
        parent_task_id=task_id,
        hop=envelope.hop + 1,
        deadline_remaining_s=max(envelope.deadline_remaining_s * _CHILD_RESERVE, 0.0),
        chain=(*envelope.chain, target_id),
    )


def task_with_preamble(task: str, envelope: Envelope, task_id: str) -> str:
    lines = [
        _OPEN,
        f"root_task_id: {envelope.root_task_id}",
        f"task_id: {task_id}",
        f"parent_task_id: {envelope.parent_task_id or 'none'}",
        f"hop: {envelope.hop}",
        f"deadline_remaining_s: {round(envelope.deadline_remaining_s, 3):g}",
        f"chain: {','.join(envelope.chain)}",
        _CLOSE,
        "",
        task,
    ]
    return "\n".join(lines)


def parse_preamble(text: str) -> dict[str, Any]:
    """Read the envelope block a task arrived with. Used by tests and docs."""
    if not text.startswith(_OPEN + "\n"):
        raise ValueError("task does not start with an envelope block")
    end = text.find(_CLOSE)
    if end == -1:
        raise ValueError("envelope block is not closed")
    fields: dict[str, Any] = {}
    for line in text[len(_OPEN) + 1 : end].splitlines():
        key, _, value = line.partition(": ")
        if key not in {
            "root_task_id",
            "task_id",
            "parent_task_id",
            "hop",
            "deadline_remaining_s",
            "chain",
        }:
            raise ValueError(f"unknown envelope field {key!r}")
        fields[key] = value
    fields["hop"] = int(fields["hop"])
    fields["deadline_remaining_s"] = float(fields["deadline_remaining_s"])
    fields["chain"] = fields["chain"].split(",")
    fields["parent_task_id"] = None if fields["parent_task_id"] == "none" else fields["parent_task_id"]
    fields["task_id"] = str(fields.get("task_id", ""))
    return fields


def is_task_id(value: str) -> bool:
    return bool(_UUID.match(value))


def new_task_id() -> str:
    return str(uuid.uuid4())
