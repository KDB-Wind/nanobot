"""Optional runs-inbox assembly for ``serve`` (ADR-001, peer comms design).

A managed worker bot that both executes coordinator-dispatched runs and
receives peer results needs both route families in one process; a second
process sharing the home would fight over the session database. This mirrors
the gateway sidecar assembly (``cli/gateway_runtime.py``): one
``ExternalInbox`` bound to the session state, the agent-inbox routes on the
serve API app, the trusted teams meta hook for capability minting, and an
``InboxWakePump`` the caller runs as a task. Off unless
``api.runs.inbox_enabled`` is set.
"""

from __future__ import annotations

from aiohttp import web

from nanobot.agent.loop import AgentLoop
from nanobot.config.schema import ApiRunsConfig
from nanobot.session.inbox_wake import InboxWakePump
from nanobot.session.manager import SessionManager


async def _session_alive(session_manager: SessionManager, session_key: str) -> bool:
    """Whether the origin session still exists for an inbox wake turn."""
    try:
        session = await session_manager.state.read(session_key)
    except Exception:
        return False
    return session is not None


def mount_serve_inbox(
    app: web.Application,
    *,
    agent_loop: AgentLoop,
    runs_config: ApiRunsConfig,
) -> InboxWakePump:
    """Mount the agent-inbox routes and return the wake pump to run."""
    from nanobot.agent.tools.mcp import set_teams_async_meta_hook
    from nanobot.api.agent_inbox import register_inbox_routes
    from nanobot.cli.gateway_runtime import mint_teams_meta
    from nanobot.session.external_inbox import ExternalInbox

    session_manager = agent_loop.sessions
    external_inbox = ExternalInbox(session_manager.state)
    agent_loop.external_inbox = external_inbox
    register_inbox_routes(app, external_inbox, runs_config)

    def _teams_meta_provider(_server_name: str):
        return lambda: mint_teams_meta(external_inbox, runs_config.principal_id)

    set_teams_async_meta_hook(_teams_meta_provider)

    return InboxWakePump(
        external_inbox,
        agent_loop.bus.publish_inbound,
        lambda session_key: _session_alive(session_manager, session_key),
    )
