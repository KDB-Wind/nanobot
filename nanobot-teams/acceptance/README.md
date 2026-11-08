# Acceptance runs

Windows-only harnesses that exercise the real integration. They sit outside the
package: they need two live Nanobot processes, a browser, and provider
credentials, so they are run by hand rather than in CI.

## Prerequisites

- The worktree root has `.venv-nanobot` with the baseline Nanobot installed.
- `..\.venv` has this package installed in editable mode.
- `webui/node_modules` exists and Playwright can launch Edge.
- `~/.nanobot/config.json` holds working provider credentials for the chosen
  preset. The harness copies the preset and overrides only its API key.

Select a preset with `NANOBOT_TEAMS_PRESET`; it defaults to `step-5-preview`.

## Server path

```bash
NANOBOT_TEAMS_PRESET=glm-5.3-flash python acceptance/run_e2e.py all
```

| Scenario | What it proves |
| --- | --- |
| `research_direct` | The Research instance answers on `POST /v1/chat/completions`. |
| `delegate_basic` | Planner calls `mcp_teams_delegate_task` and carries the answer back. |
| `session_persistence` | The deterministic session id survives a Research restart. |
| `long_timeout` | A 35 second task outlives the 30 second default and returns at ~48 s. |
| `unknown_block` | A 500 marks the target and a retry never reaches HTTP. |
| `plugin_manifest` | Real `discover_agent_plugins` accepts the package. |

## WebUI path

```bash
NANOBOT_TEAMS_PRESET=glm-5.3-flash python acceptance/run_webui.py
```

Starts Research on 8901 and a Planner gateway on 18790 with the WebSocket
channel on 8766, opens Edge at `#/apps`, enables the plugin, then sends a task
from `#/new`. Passing means the enable marker exists and a nonce that only the
Research workspace can read comes back through the Planner.

## Output

Everything lands in `.e2e/` and is gitignored: `logs/` for process output,
`shots/` for WebUI screenshots. The generated `config.json` files contain
resolved API keys; delete them after a run.

```bash
find .e2e -name config.json -delete
```
