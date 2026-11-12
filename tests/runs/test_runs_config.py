"""ApiRunsConfig boundary: default off, dedicated key, exclusive coherence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanobot.config.schema import ApiConfig, ApiRunsConfig


def test_defaults_are_disabled_and_inert():
    config = ApiRunsConfig()
    assert config.enabled is False
    assert config.api_key == ""
    assert config.execution_enabled is False
    assert config.inbox_enabled is False
    assert ApiConfig().runs.enabled is False


def test_enabled_requires_dedicated_key():
    with pytest.raises(ValidationError):
        ApiRunsConfig(enabled=True, api_key="", execution_enabled=True)
    ok = ApiRunsConfig(enabled=True, api_key="runs-secret", execution_enabled=True)
    assert ok.principal_id == "teams-coordinator"


def test_exclusive_requires_execution():
    with pytest.raises(ValidationError):
        ApiRunsConfig(
            enabled=True, api_key="k", execution_enabled=False, exclusive_execution=True
        )


def test_plain_api_key_never_becomes_runs_key():
    config = ApiConfig(api_key="plain")
    assert config.runs.api_key != "plain"
    assert config.runs.enabled is False
