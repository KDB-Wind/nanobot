"""Typed, checkable result of stopping one session's owned work.

A cancellation call returning is not evidence that the work stopped: bounded
waits can time out and leave a subagent or exec session alive. This report is
the fact the run layer records instead.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStopReport:
    """What was cancelled, and what is still alive after the pass finished."""

    cancelled: int
    live_subagents: int
    live_exec_sessions: tuple[str, ...]

    @property
    def confirmed(self) -> bool:
        """The session owns no live subagent and no exec session without an exit."""
        return self.live_subagents == 0 and not self.live_exec_sessions


__all__ = ["SessionStopReport"]
