# Acceptance runs

Harnesses that exercise the real integration. They sit outside the package:
they need two live Nanobot processes, a browser, and provider credentials, so
they are run by hand rather than in CI. The server path runs on Windows and on
Linux; the WebUI path needs Edge and has only been run on Windows.

## Prerequisites

- The worktree root has `.venv-nanobot` with the baseline Nanobot installed.
- `..\.venv` has this package installed in editable mode.
- `webui/node_modules` exists and Playwright can launch Edge.
- `~/.nanobot/config.json` holds working provider credentials for the chosen
  preset. The harness copies the preset and overrides only its API key.

Select a preset with `NANOBOT_TEAMS_PRESET`; it defaults to `step-5-preview`.

`NANOBOT_TEAMS_API_BASE` overrides the preset provider's base URL. Use it when the
subscription endpoint differs from the provider default, for example
`https://api.stepfun.com/step_plan/v1`.

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
| `multi_bot` | A schema 2 registry with three bots; Planner delegates to two authorized targets. |
| `unauthorized_pair` | A caller not in `allowed_callers` is refused before any HTTP. |
| `chain_abc` | A real three-instance chain: Planner delegates to Mid, Mid reads its envelope block and delegates to Research, the answer climbs back. |
| `chain_loop` | A continuation whose target is already in the chain is refused with `loop_detected`, no HTTP. |
| `chain_budget` | A spent deadline fails with `budget_exhausted`; a tiny deadline clamps the wait into `timed_out`, never `completed`. |
| `bot_init_live` | `bot init` scaffolds a bot, the generated config starts a real instance, `check` approves it, and a Planner delegates to it. |
| `check_errors` | `check` exits 1 and names the missing key variable and the unknown preset. |
| `ledger_restart` | A delegation lands in the SQLite ledger, a fresh MCP process reads it back, and the database holds no task text, answer, or key. |
| `budget_reject` | A two-delegation cap refuses the third call with `budget_exceeded` before HTTP; graph, budget, and audit print the state. |
| `packaging_fresh` | Builds the wheel, installs it into a clean venv with resolved dependencies, runs a delegation from it, and uninstalls. |
| `plugin_manifest` | Real `discover_agent_plugins` accepts the package. |

`packaging_fresh` reaches PyPI for dependencies; on a WSL NAT guest export the
proxy variables before running it, as for the other scenarios.

## Unix path

`run_e2e.py` picks `.venv/bin` interpreter paths on POSIX. A Linux run needs
the same layout inside a clone of this worktree: `.venv-nanobot` at the root
with `.[api]` installed, and `nanobot-teams/.venv` with this package. The
`git` path quirk matters: clone the main repository, not the worktree
directory, because a worktree's `.git` file points back at an absolute Windows
path that POSIX git cannot follow.

When the guest is WSL in NAT mode with no direct egress, start `relay.py` on
the Windows host bound to the WSL gateway address, forwarding to the
loopback-bound host proxy. Then export the proxy plus a localhost bypass and
run the suite:

```bash
py acceptance/relay.py --listen <gateway-ip>:17897 --target 127.0.0.1:<proxy-port>
export http_proxy=http://<gateway-ip>:17897 https_proxy=http://<gateway-ip>:17897
export no_proxy=127.0.0.1,localhost
.venv/bin/python acceptance/run_e2e.py all
```

`no_proxy` matters. The harness health checks hit 127.0.0.1 and must not be
routed into the proxy. The Teams transport is unaffected either way because it
runs with `trust_env=False` and only talks to loopback.

Recorded Linux result, 2026-11-08 on Ubuntu 22.04 (WSL2, kernel 6.18.33,
Python 3.11.15): 6/6 scenarios pass, `long_timeout` elapsed 38.2 s.

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
