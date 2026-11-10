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
