"""WebUI mutation routing for the teams workbench."""

import pytest

from nanobot.webui.ws_http import GatewayHTTPHandler


def test_stop_workflow_maps_to_the_coordinator_route() -> None:
    path = GatewayHTTPHandler._webui_mutation_path(
        "teams.stop_workflow",
        {"workflow_id": "wf-a_b.c-1"},
    )

    assert path == "/api/teams/workflows/wf-a_b.c-1/stop"


def test_stop_workflow_encodes_reserved_path_characters() -> None:
    path = GatewayHTTPHandler._webui_mutation_path(
        "teams.stop_workflow",
        {"workflow_id": "wf:root"},
    )

    assert path == "/api/teams/workflows/wf%3Aroot/stop"


def test_stop_workflow_requires_a_present_workflow_id() -> None:
    for payload in ({}, {"workflow_id": None}, {"workflow_id": 7}):
        response = GatewayHTTPHandler._webui_mutation_path("teams.stop_workflow", payload)
        assert getattr(response, "status_code", None) == 400


@pytest.mark.parametrize(
    "workflow_id",
    ["", "wf 1", "wf/1", "wf?x", "../etc/passwd", "wf\n1", "a" * 129],
)
def test_stop_workflow_rejects_ids_outside_the_safe_charset(workflow_id: str) -> None:
    response = GatewayHTTPHandler._webui_mutation_path(
        "teams.stop_workflow",
        {"workflow_id": workflow_id},
    )

    assert getattr(response, "status_code", None) == 400


class _RecordingProxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    async def forward(self, method: str, path: str, body=None):
        from nanobot.webui.teams_proxy import TeamsProxyResult

        self.calls.append((method, path, body))
        return TeamsProxyResult(200, {"ok": True})


class _MinimalHandler(GatewayHTTPHandler):
    """Only the surface _handle_teams_proxy touches."""

    def __init__(self, proxy) -> None:
        self.teams_proxy = proxy

    def check_api_token(self, request) -> bool:
        return True


class _WebsocketsStyleRequest:
    """The websockets parser Request shape: path and headers only."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.headers = []


class _BridgedStyleRequest(_WebsocketsStyleRequest):
    """The aiohttp bridge shape: carries the method."""

    def __init__(self, path: str, method: str) -> None:
        super().__init__(path)
        self.method = method


@pytest.mark.asyncio
async def test_teams_proxy_forwards_parser_gets_without_a_method_attribute() -> None:
    """Regression: plain GETs ride the websockets parser whose Request has no
    .method; every workbench read (snapshot, usage, edges) crashed with
    AttributeError before the method fallback."""
    proxy = _RecordingProxy()
    handler = _MinimalHandler(proxy)
    response = await handler._handle_teams_proxy(
        _WebsocketsStyleRequest("/api/teams/usage"), "/api/teams/usage"
    )
    assert response.status_code == 200
    assert proxy.calls == [("GET", "/api/teams/usage", None)]


@pytest.mark.asyncio
async def test_teams_proxy_still_reads_the_method_from_bridged_posts() -> None:
    proxy = _RecordingProxy()
    handler = _MinimalHandler(proxy)
    response = await handler._handle_teams_proxy(
        _BridgedStyleRequest("/api/teams/tasks", "POST"), "/api/teams/tasks"
    )
    # POSTs without the authenticated-WebSocket mutation marker stay refused.
    assert response.status_code == 405
    assert proxy.calls == []
