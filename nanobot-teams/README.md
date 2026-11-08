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
  "schema_version": 1,
  "team_id": "personal-research",
  "caller_bot_id": "planner",
  "thread_key": "main",
  "allowed_targets": ["research"],
  "bots": [
    {"id": "planner", "display_name": "Planner", "role": "Planning and synthesis"},
    {
      "id": "research",
      "display_name": "Research",
      "role": "Retrieval with evidence and uncertainty",
      "endpoint": "http://127.0.0.1:8901/v1/chat/completions",
      "api_key_env": "TEAMS_RESEARCH_API_KEY"
    }
  ]
}
```

Rules enforced at startup:

- `schema_version` must be `1`.
- `caller_bot_id` must name a bot in `bots`, and that bot cannot appear in `allowed_targets`.
- Every allowed target must exist in `bots` and must carry an `endpoint` and an `api_key_env`.
- Bot ids are lowercase letters, digits, dots, underscores, and hyphens, up to 64 characters.
- The endpoint must be `http://127.0.0.1:<port>/v1/chat/completions`. No other host, scheme, path, query, userinfo, or fragment is accepted.
- The environment variable named by `api_key_env` must be set and non-empty when the server starts.
- Unknown fields are rejected. A bad config writes one line to stderr and exits with code 2.

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

Add this to the Planner config. Keep the timeout above the Research request timeout.

```json
{
  "tools": {
    "mcpServers": {
      "teams": {
        "type": "stdio",
        "command": "D:/Tools/nanobot-teams/.venv/Scripts/python.exe",
        "args": ["-m", "nanobot_teams", "--config", "D:/BotData/teams/teams.json"],
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
