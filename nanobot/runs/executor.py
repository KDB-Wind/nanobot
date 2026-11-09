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
    "empty_final_response": OutcomeCode.SUCCEEDED,
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

    async def execute(self, run: Run) -> ExecutionResult:
        capture = _RunOutcomeCaptureHook()
        message: Any = None
        try:
            message = await self._loop.process_direct(
                content=run.input_text,
                session_key=f"run:{run.run_id}",
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
            # tools and exec sessions gone, not just this coroutine.
            try:
                await self._loop.cancel_session_work(f"run:{run.run_id}")
            except Exception:
                # Background tool/exec cleanup could not be confirmed; the
                # service must isolate the instance instead of trusting stop.
                self._cancel_unverified = True
            raise
        except Exception as exc:
            return ExecutionResult(
                code=OutcomeCode.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}"[:512],
            )
        text = _response_text(message)
        stop_reason = capture.stop_reason or ""
        code = _STOP_REASON_TO_OUTCOME.get(stop_reason, OutcomeCode.EXECUTION_ERROR)
        message_text = ""
        if code is not OutcomeCode.SUCCEEDED:
            message_text = (capture.error or stop_reason or "execution failed")[:512]
        return ExecutionResult(code=code, text=text, message=message_text)


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)
