# Nanobot Teams

Nanobot Teams is a standalone stdio MCP server that delegates one task from a Planner Nanobot instance to a Research Nanobot instance. It uses only the public OpenAI-compatible HTTP API of the Research instance. It does not import nanobot, read its files, or run a second agent loop.

The Planner keeps its own identity, workspace, model, memory, and session. The Research instance keeps its own. Teams moves one user message and one answer.

## What it does

- `list_bots` returns the allowed targets with their id, display name, and role.
- `delegate_task` sends one task and waits for one answer in the same tool call.
- The target session id is deterministic for one team, caller, target, and thread.
- One delegation runs at a time. A second call returns `busy` immediately.
- No retries. A timeout or an unknown result blocks later delegations until the process restarts.
- Task, HTTP response, and returned content have fixed byte limits.

## Requirements

- Nanobot 0.4.1, tested at commit `1e2dc02fa228b6c5c62b8fc84de2a2d77dd925f3`. Record the package source you actually run and repeat the verify steps against it.
- Python 3.11 or newer.
- Two Nanobot instances with separate homes, config directories, workspaces, model credentials, and sessions.
- A Planner workspace where you can install an Agent Plugin.

## Support matrix

| Item | Tested value |
| --- | --- |
| Nanobot baseline | 0.4.1 at commit `1e2dc02fa228b6c5c62b8fc84de2a2d77dd925f3`, installed editable from the local worktree |
| Python | 3.11.4 on Windows, 3.11.15 on Ubuntu (WSL2) |
| Platforms | Windows 11, Ubuntu 22.04 kernel 6.18 on WSL2 in NAT mode |
| Resolved deps | `mcp` 1.30.0, `httpx` 0.28.1, `anyio` 4.16.0, `pydantic` 2.14.3, identical on both platforms |
| Server acceptance | 6/6 scenarios pass on Windows and on Ubuntu, see `acceptance/README.md` |
| WebUI acceptance | Pass on Windows |

The Ubuntu run routes egress through a host-side byte relay because the WSL NAT
guest has no direct network access; `acceptance/README.md` describes the setup.
That relay is an environment adapter for this machine, not a product dependency.

## Install the server

```bash
cd nanobot-teams
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -e .
```

On Unix, replace the interpreter path with `.venv/bin/python`. This installs a console script named `nanobot-teams`. The server also runs as `python -m nanobot_teams`.

The package has two runtime dependencies, `mcp` and `httpx`. It has no dependency on the `nanobot` package.

## Configure the team

Copy `examples/teams.example.json` to a directory outside every model-writable workspace. Protect that directory with operating system permissions.

```json
{
  "schema_version": 2,
  "team_id": "personal-research",
  "thread_key": "main",
  "bots": [
    {
      "id": "planner",
      "display_name": "Planner",
      "role": "Planning and synthesis",
      "capabilities": ["plan", "synthesize"],
      "model_preset": "step-5-preview"
    },
    {
      "id": "research",
      "display_name": "Research",
      "role": "Retrieval with evidence and uncertainty",
      "capabilities": ["search"],
      "endpoint": "http://127.0.0.1:8901/v1/chat/completions",
      "api_key_env": "TEAMS_RESEARCH_API_KEY",
      "allowed_callers": ["planner"]
    },
    {
      "id": "writer",
      "display_name": "Writer",
      "role": "Drafting long-form answers",
      "endpoint": "http://127.0.0.1:8903/v1/chat/completions",
      "api_key_env": "TEAMS_WRITER_API_KEY",
      "allowed_callers": ["planner"]
    }
  ]
}
```

Authorization is a graph: a delegation from caller to target is allowed only
when the target lists the caller in `allowed_callers`. One registry can serve
every bot's MCP instance; each instance states who it acts for with
`--caller <bot id>` on the command line, which is trusted control-plane
configuration, never model input. `capabilities` and `model_preset` are
advisory metadata surfaced through `list_bots` so the caller can choose a
target; they do not configure the target instance itself.

Rules enforced at startup:

- `schema_version` must be `2` (or `1` for the legacy single-caller shape; see below).
- Bot ids are lowercase letters, digits, dots, underscores, and hyphens, up to 64 characters, unique.
- A bot with `allowed_callers` must carry an `endpoint` and an `api_key_env`; a bot without them must not.
- `allowed_callers` entries must be bot ids in the registry, unique, and never the bot itself.
- `--caller` must name a bot in the registry. Schema 2 refuses to start without it; schema 1 defaults to `caller_bot_id` and pins `--caller` to that value.
- The endpoint must be `http://127.0.0.1:<port>/v1/chat/completions`. No other host, scheme, path, query, userinfo, or fragment is accepted.
- The environment variable named by `api_key_env` must be set and non-empty for every target the caller can reach.
- Unknown fields are rejected. A bad config writes one line to stderr and exits with code 2.

Schema 1 files (`caller_bot_id` plus `allowed_targets` at the top level) keep
loading: each allowed target is normalized to `allowed_callers: [<caller>]` and
the fixed caller becomes the instance default. `examples/teams.v1.example.json`
shows the legacy shape.

Add an optional `limits` object to override the defaults:

```json
{
  "limits": {
    "task_max_bytes": 16384,
    "response_max_bytes": 262144,
    "result_max_bytes": 32768,
    "connect_timeout_s": 3.0,
    "request_timeout_s": 135.0,
    "total_timeout_s": 145.0
  }
}
```

## Prepare the Research instance

Use a separate home and config directory. Do not share an absolute workspace path with the Planner.

```bash
nanobot --home D:/BotData/research serve --config D:/BotData/research/config.json
```

A minimal Research config fragment:

```json
{
  "api": {
    "host": "127.0.0.1",
    "port": 8901,
    "timeout": 120,
    "apiKey": "${RESEARCH_API_KEY}"
  },
  "tools": {
    "restrictToWorkspace": true,
    "exec": {"enable": false}
  }
}
```

Set `RESEARCH_API_KEY` in the Research process environment. Use the same value for `TEAMS_RESEARCH_API_KEY` in the Planner process. This key belongs to the Research API server. It is not a model provider key.

Give Research its own model preset, its own tools, and its own workspace. The API key holder can choose any session on that instance, so treat the instance as a single trusted caller boundary.

## Register the MCP server

Add this to the Planner config. Keep the timeout above the Research request timeout. The `--caller` argument tells this MCP instance which bot it acts for.

```json
{
  "tools": {
    "mcpServers": {
      "teams": {
        "type": "stdio",
        "command": "D:/Tools/nanobot-teams/.venv/Scripts/python.exe",
        "args": [
          "-m",
          "nanobot_teams",
          "--config",
          "D:/BotData/teams/teams.json",
          "--caller",
          "planner"
        ],
        "cwd": "D:/BotData/teams",
        "env": {
          "TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"
        },
        "toolTimeout": 180,
        "enabledTools": ["list_bots", "delegate_task"]
      }
    }
  }
}
```

A bot that can itself delegate (a middle hop) registers the same server with
`--caller <its own id>` in its own Nanobot config. One registry file serves
every instance; each instance resolves only the credentials for the targets its
caller is authorized to reach.

The explicit `env` entry matters. A custom MCP server does not inherit the plugin variables or every parent environment variable. Set `TEAMS_RESEARCH_API_KEY` in the process that starts the Planner.

Restart the Planner or reload MCP servers. The tools appear as `mcp_teams_list_bots` and `mcp_teams_delegate_task`.

## Install the Agent Plugin

Copy `agent-plugin/nanobot-teams` into `<planner workspace>/plugins/nanobot-teams`. Open Apps in the WebUI and enable `Nanobot Teams`.

The package is skills-only. It contains no `mcp.json`, because a plugin MCP entry cannot set `toolTimeout` and the default 30 seconds is too short. Do not add a second `mcp.json` that starts the same server.

## Verify

1. Start the Research instance. Confirm `POST /v1/chat/completions` answers with the Research API key.
2. Restart the Planner. Confirm both Teams tools appear.
3. Ask the Planner a question that needs the Research bot. Confirm the tool call, the returned JSON, and the Planner's final answer.
4. Ask Research to remember a random word. Restart Research. Ask it to repeat the word in the same team thread. A persistent answer confirms the session mapping.
5. Ask two questions that overlap. Confirm the second delegation returns `busy`.
6. Stop Research and delegate again. Confirm a clean failure, no secret in the error, and no retry.
7. Start Research, send one long task that runs past 30 seconds but under the Research timeout. Confirm the MCP call returns the answer.

A 200 response is not proof that the research succeeded. The result carries `semantic_status: unverified` for that reason.

`acceptance/README.md` describes the two harnesses that replay these checks against live processes, including the WebUI path.

## Operate

The server logs to stderr only. Each delegation logs the task id, bot id, duration, status, and error code. It does not log the task or the answer.

A `timed_out` or `outcome_unknown` result sets an in-process block. Later delegations return `failed` with `error_code: blocked` and make no HTTP request. Restart the MCP server only after you check the Research logs and any side effects. A restart of the MCP server clears the block but does not cancel or recover a remote task.

A native MCP reconnect can rebuild the stdio process and clear the block. The block is not durable across process restarts.

## Upgrade

1. Stop in-flight delegations and confirm the Research instance is idle.
2. Update the `nanobot-teams` package in its own environment.
3. If any plugin package file changed, review and enable the plugin again in Apps.
4. Restart the Planner so the MCP server starts the new code.
5. Repeat the verify steps.

## Disable and uninstall

Disable the MCP server first. Remove the `teams` entry from `tools.mcpServers` and reload or restart the Planner. Confirm both tools are gone. Then disable the Agent Plugin in Apps. Disabling only the skill leaves the MCP tools active.

To uninstall, remove the plugin directory and the `nanobot-teams` distribution. Keep the two Nanobot homes and the Registry unless you intend to delete the bots. Uninstall does not delete identity, memory, sessions, or the Research API key.

## Security boundaries

- The Registry, the executable, and the MCP environment are the control plane. Do not let model output create or edit them.
- The Planner holds only the Research API key. Research holds no Planner key and no Teams outbound tools.
- The transport does not use environment proxies, does not follow redirects, and does not retry. The Bearer token and task cannot be forwarded to another host by the client.
- `restrictToWorkspace` is an application path guard, not an operating system sandbox. Two processes under the same OS user can usually read each other's files. Use separate OS accounts or containers when the bots do not trust each other.
- Research web pages, tool output, and answers are untrusted data. The Planner must not promote them to instructions or use them to choose an endpoint.
- Logs keep task ids, bot ids, timing, status, and error codes. Nanobot itself may log session data and prompt prefixes. Review its log retention separately.

## Limits

- One trusted user and one fixed team thread per installation.
- One active delegation per MCP process. No queue.
- No remote task query, cancel, resume, or idempotency key.
- No automatic retry and no exactly-once delivery.
- No file attachments, group chat, mentions, or proactive messages.
- A running HTTP request does not survive a restart of either process.

## Roadmap

`ROADMAP.md` covers the stages after this MVP: multiple bots with an authorization
graph, multi-hop delegation, lifecycle tooling, a local task ledger, and release
hardening.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Tools are missing | MCP config not loaded, or the server exited at startup | Check the Planner MCP status and the server's stderr line |
| Startup exits 2 | Invalid Registry, endpoint, or missing key variable | Fix the reported field. The message never prints the key |
| `failed` with `auth_failed` | `TEAMS_RESEARCH_API_KEY` differs from Research `api.apiKey` | Set both to the same value and restart both processes |
| `failed` with `connect_failed` | Research is not listening on the exact loopback endpoint | Start `nanobot serve` and check host and port |
| `busy` | A delegation is still running | Wait for the current tool call to finish |
| `blocked` | An earlier result was timed out or unknown | Check Research logs and side effects, then restart the MCP server |
| `outcome_unknown` | The request may have reached Research | Do not retry. Check Research before you resume |
| `response_too_large` | The answer exceeded 256 KiB before parsing | Narrow the task or raise `response_max_bytes` after review |
| `truncated: true` | The answer exceeded 32 KiB after parsing | The full answer is not available. Ask a narrower follow-up |
| `failed` with `provider_unavailable`, or a provider 402 | The subscription plan uses a different base URL than the API default | Set `providers.<name>.apiBase` to the plan endpoint, for example `https://api.stepfun.com/step_plan/v1`, and restart |
