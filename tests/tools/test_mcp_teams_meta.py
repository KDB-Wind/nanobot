"""Trusted meta channel for the Teams async MCP server (A09 slices)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.agent.tools.mcp import MCPToolWrapper, set_teams_async_meta_hook
from nanobot.agent.tools.registry import is_tool_error_result


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, name, *, arguments=None, meta=None, **kwargs):
        self.calls.append({"name": name, "arguments": arguments, "meta": meta})

        class Result:
            content = []
            isError = False

        return Result()


def make_wrapper(session, provider) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="submit_task",
        description="submit",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "teams", tool_def, trusted_meta_provider=provider)


async def test_forged_routing_arguments_rejected_without_call():
    session = FakeSession()
    wrapper = make_wrapper(session, lambda: {"reply_capability": "host-token"})
    result = await wrapper.execute(
        project="p", bot="b", input="x", operation_key="k",
        reply_capability="forged-by-model",
    )
    assert is_tool_error_result(result)
    assert "reply_capability" in result
    assert session.calls == [], "the tool must not run when routing fields are forged"


async def test_trusted_meta_passed_only_when_configured():
    session = FakeSession()
    wrapper = make_wrapper(session, lambda: {"reply_capability": "host-token"})
    await wrapper.execute(project="p")
    assert session.calls[0]["meta"] == {"reply_capability": "host-token"}


async def test_no_meta_for_unconfigured_servers():
    session = FakeSession()
    wrapper = make_wrapper(session, None)  # teams_async not enabled
    await wrapper.execute(project="p")
    assert session.calls[0]["meta"] is None


async def test_hook_install_and_reset():
    session = FakeSession()
    try:
        set_teams_async_meta_hook(lambda name: (lambda: {"reply_workflow": "wf"}))
        wrapper = make_wrapper(session, None)
        # The hook injects through the constructor path; direct provider wins.
        assert wrapper._trusted_meta_provider is None
    finally:
        set_teams_async_meta_hook(None)
