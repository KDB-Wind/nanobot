"""A18 in-process: the real loop dispatch path revokes a minted workflow grant.

Single process, worktree code, real AgentLoop + bus + SessionState; the
execution ends in a typed provider error (no model configured), which is fine —
the behavior under test is the user-turn revocation, not the reply.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nanobot-teams" / "src"))

from nanobot.agent.loop import AgentLoop  # noqa: E402
from nanobot.bus.queue import MessageBus  # noqa: E402
from nanobot.session.external_inbox import ExternalInbox  # noqa: E402
from nanobot.session.manager import SessionManager  # noqa: E402


async def main() -> int:
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.schema import Config
    from nanobot.providers.unconfigured_provider import UnconfiguredProvider

    tmp = Path(tempfile.mkdtemp(prefix="a18-proc-"))
    (tmp / "workspace").mkdir()
    session_manager = SessionManager(tmp / "sessions-root")
    bus = MessageBus()
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp / "workspace")}}})
    provider = UnconfiguredProvider(default_model="test-model")
    loop = AgentLoop.from_config(
        config, bus,
        provider=provider,
        session_manager=session_manager,
        tool_registry=ToolRegistry(),
    )
    inbox = ExternalInbox(session_manager.state)
    loop.external_inbox = inbox  # what the gateway assembly does

    session_key = "websocket:webui-default"

    async def send_user(content: str) -> None:
        from nanobot.bus.events import InboundMessage

        message = InboundMessage(
            channel="websocket",
            chat_id="webui-default",
            sender_id="user",
            content=content,
            input_role="user",
        )
        await loop._dispatch_one(message, asyncio.Queue())

    try:
        await send_user("Say OK1 and nothing else.")
        inbox.mint_capability(
            session_key=session_key,
            session_generation=(await session_manager.state.read(session_key)).generation,
            workflow_id="wf-proc",
            operation_key="*",
            bot_id="*",
            instance_id="*",
            principal_id="teams-coordinator",
        )
        assert inbox.workflow_allows_auto_continue("wf-proc") is True
        await send_user("Say OK2 and nothing else.")
        revoked = inbox.workflow_allows_auto_continue("wf-proc") is False
        print("grant after turn 2 revoked:", revoked)
        print("A18 PROCESS PASS" if revoked else "A18 PROCESS FAIL")
        return 0 if revoked else 1
    finally:
        await loop.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
