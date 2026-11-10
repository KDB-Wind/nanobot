"""AgentLoop adapter for the run service: typed outcomes, real stop confirmation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nanobot.agent.hook import AgentHook, AgentRunHookContext

from .types import ExecutionResult, OutcomeCode, Run

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop

_STOP_REASON_TO_OUTCOME: dict[str, OutcomeCode] = {
    "completed": OutcomeCode.SUCCEEDED,
    "empty_final_response": OutcomeCode.EXECUTION_ERROR,
    "error": OutcomeCode.PROVIDER_ERROR,
    "max_iterations": OutcomeCode.ITERATION_LIMIT,
}


class _RunOutcomeCaptureHook(AgentHook):
    """Capture the typed stop reason the runner reports for one run."""

    def __init__(self) -> None:
        super().__init__()
        self.stop_reason: str | None = None
        self.error: str | None = None

    async def after_run(self, context: AgentRunHookContext) -> None:
        self.stop_reason = context.stop_reason
        self.error = context.error


class AgentLoopRunExecutor:
    """Execute one run in its own isolated ephemeral session on the bot's loop."""

    def __init__(self, loop: "AgentLoop") -> None:
        self._loop = loop
        self._cancel_unverified = False

    def consume_cancel_unverified(self) -> bool:
        """One-shot flag: the last cancellation could not confirm cleanup."""
        value = self._cancel_unverified
        self._cancel_unverified = False
        return value

    async def _reap_session_work(self, session_key: str) -> bool:
        """Stop the run session's tools, subagents, and exec sessions.

        Returns True only when the loop reports that nothing it owns is still
        alive; a normal return from the stop call is not that evidence.
        """
        try:
            report = await self._loop.stop_session_work(session_key)
        except Exception:
            return False
        return report.confirmed

    async def execute(self, run: Run) -> ExecutionResult:
        capture = _RunOutcomeCaptureHook()
        session_key = f"run:{run.run_id}"
        try:
            message = await self._loop.process_direct(
                content=run.input_text,
                session_key=session_key,
                channel="api",
                chat_id=run.run_id,
                hooks=[capture],
                ephemeral=True,
                # The typed-outcome capture must survive the ephemeral hook
                # gate; without this flag the runner drops caller hooks.
                _run_extra_hooks_for_ephemeral=True,
                persist_user_message=False,
            )
        except asyncio.CancelledError:
            # Cancel the loop-owned work for this run's session before letting
            # the cancellation surface: stop confirmation needs the background
            # tools and exec sessions gone, not just this coroutine. Set the
            # conservative fact before the await: a second cancel can end this
            # task here, and the flag must already say the cleanup is unproven.
            self._cancel_unverified = True
            if await self._reap_session_work(session_key):
                self._cancel_unverified = False
            raise
        except Exception as exc:
            # The loop may have started tools or subprocesses before failing;
            # the reap below owns them like every other exit path.
            result = ExecutionResult(
                code=OutcomeCode.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}"[:512],
            )
        else:
            text = _response_text(message)
            stop_reason = capture.stop_reason or ""
            code = _STOP_REASON_TO_OUTCOME.get(stop_reason, OutcomeCode.EXECUTION_ERROR)
            message_text = ""
            if code is not OutcomeCode.SUCCEEDED:
                message_text = (capture.error or stop_reason or "execution failed")[:512]
            result = ExecutionResult(code=code, text=text, message=message_text)
        # Every terminal path reaps the session's background work before the
        # result is recorded: success, provider error, iteration limit, and
        # execution error all own tools and subprocesses they started.
        try:
            stopped = await self._reap_session_work(session_key)
        except asyncio.CancelledError:
            self._cancel_unverified = True
            raise
        if not stopped:
            result = ExecutionResult(
                code=result.code,
                text=result.text,
                message=result.message
                or "background cleanup after the run was unconfirmed"[:512],
                stop_confirmed=False,
                effects_uncertain=True,
            )
        return result


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)
