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


class RunCancelled(asyncio.CancelledError):
    """A cancelled execution carrying its own cleanup conclusion.

    The conclusion belongs to the coroutine that observed it, so overlapping
    cancellations cannot overwrite each other's stop evidence.
    """

    def __init__(self, *, stop_confirmed: bool) -> None:
        super().__init__()
        self.stop_confirmed = stop_confirmed


class AgentLoopRunExecutor:
    """Execute one run in its own isolated ephemeral session on the bot's loop.

    With ``durable_peer_sessions`` the executor binds a PeerConversationBinding
    for the durable ``peer:<workflow>`` session while the run executes:
    send_to_bot mints reply capabilities against that durable session (the
    ephemeral run session could never receive a wake), and the run's own
    working memory stays isolated exactly as before.
    """

    def __init__(self, loop: "AgentLoop", *, durable_peer_sessions: bool = False) -> None:
        self._loop = loop
        self._durable_peer_sessions = durable_peer_sessions

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
        peer_token = None
        if self._durable_peer_sessions and run.workflow_id:
            from nanobot.agent.tools.context import (
                PeerConversationBinding,
                bind_peer_conversation,
            )

            durable_key = f"peer:{run.workflow_id}"
            conversation = await self._loop.sessions.state.get(durable_key)
            peer_token = bind_peer_conversation(
                PeerConversationBinding(
                    session_key=durable_key,
                    session_generation=conversation.generation,
                    workflow_id=run.workflow_id,
                )
            )
        try:
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
                # tools and exec sessions gone, not just this coroutine. The
                # conclusion stays conservative until the reap proves otherwise,
                # and a second cancel raises this same typed result as unconfirmed.
                stop_confirmed = False
                try:
                    if await self._reap_session_work(session_key):
                        stop_confirmed = True
                finally:
                    raise RunCancelled(stop_confirmed=stop_confirmed) from None
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
                raise RunCancelled(stop_confirmed=False) from None
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
        finally:
            if peer_token is not None:
                from nanobot.agent.tools.context import reset_peer_conversation

                reset_peer_conversation(peer_token)


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)
