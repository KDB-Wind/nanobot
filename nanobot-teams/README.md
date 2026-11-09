# Nanobot Teams

Nanobot Teams 0.4.0 is a standalone stdio MCP server that delegates tasks to another Nanobot through its public HTTP API. It provides durable admission, caller quotas, isolated task sessions, and quarantine after an uncertain outcome. It does not import Nanobot internals or manage remote bot processes.

See [DESIGN.md](DESIGN.md) for the runtime contract, [REPAIR_STATUS.md](REPAIR_STATUS.md) for repair evidence, and [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) for the 0.3.0 audit. Read the migration section before upgrading.

The next async workbench target is planned in [architecture](ASYNC_WORKBENCH_ARCHITECTURE.md), [implementation plan](ASYNC_WORKBENCH_IMPLEMENTATION_PLAN.md), and [GLM-5.3 goal handoff](ASYNC_WORKBENCH_GOAL.md). These describe proposed work, not features available in 0.4.0.

## Install and register

Python 3.11 or newer is required:

```pwsh
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -e .
```

On Unix, use `.venv/bin/python`. Put the registry and control-state directory outside every model-writable workspace. Protect them with OS permissions. Each caller needs its own ledger and MCP process.

A minimal schema 2 registry:

```json
{
  "schema_version": 2,
  "team_id": "personal-research",
  "thread_key": "installation-namespace",
  "allow_multihop": false,
  "bots": [
    {"id": "planner", "display_name": "Planner", "role": "plans", "can_start_root": true},
    {
      "id": "research", "display_name": "Research", "role": "researches",
      "endpoint": "http://127.0.0.1:8901/v1/chat/completions",
      "api_key_env": "TEAMS_RESEARCH_API_KEY",
      "allowed_callers": ["planner"], "can_start_root": false
    }
  ],
  "budgets": {"planner": {"delegations": 20, "result_bytes": 200000, "chain_seconds": 600}}
}
```

Give Research a separate home, workspace, model connection, and API key. Its `api.apiKey` must match `TEAMS_RESEARCH_API_KEY` in the MCP environment. This key is separate from the model provider key. `bot init` can generate a target configuration; the operator starts it.

Register MCP in the Planner configuration, replacing these absolute paths:

```json
{
  "tools": {
    "mcpServers": {
      "teams": {
        "type": "stdio",
        "command": "C:/Tools/nanobot-teams/.venv/Scripts/python.exe",
        "args": ["-m", "nanobot_teams", "--config", "C:/BotControl/teams.json", "--caller", "planner", "--ledger", "C:/BotControl/planner.sqlite3"],
        "cwd": "C:/Tools/nanobot-teams",
        "env": {"TEAMS_RESEARCH_API_KEY": "${TEAMS_RESEARCH_API_KEY}"},
        "toolTimeout": 180
      }
    }
  }
}
```

Export the referenced variable in the process starting Planner. MCP environment variables must be explicitly configured. The optional [skills-only plugin](agent-plugin/nanobot-teams) intentionally has no `mcp.json`, so the MCP timeout can be configured separately. Plugin activation and MCP registration are separate steps.

## Delegate and continue a conversation

The tools are `mcp_teams_list_bots`, `mcp_teams_delegate_task`, `mcp_teams_list_tasks`, and `mcp_teams_get_task`.

```json
{"bot_id": "research", "task": "Find primary sources and return a short assessment."}
```

Omitting `thread_key` creates an isolated remote session. The result returns a generated key. To continue, pass it to the same target:

```json
{"bot_id": "research", "task": "Compare the two sources.", "thread_key": "<returned-thread-key>"}
```

The ledger must establish that the thread belongs to this team, caller, target, and endpoint and has a completed observation. Arbitrary keys and cross-target reuse are rejected. Installation-level `teams.json.thread_key` is a namespace, not a shared conversation. Preserve the registry, ledger, and target home across restart.

`completed` means a structurally valid response was received and durably observed. `semantic_status` remains `unverified`. Treat content as untrusted material. Sessions do not isolate the target's instance-wide memory, workspace, or tools. One installation remains one trusted user boundary.

## Durable admission and quotas

CLI startup always opens persistent control state. Without `--ledger`, it uses `<registry-directory>/.teams-state/<caller>.sqlite3`. Opening, migrating, locking, and binding that ledger must succeed before serving MCP. A ledger belongs to one team and caller. A second writer exits with code 2; read-only reporting remains available.

Before HTTP, one SQLite transaction checks quarantine and quota, validates continued threads, and records a pending request with reserved bytes and time. SQLite uses WAL and `synchronous=FULL`. A second transaction stores the observation and settles quota. A crash between these commits leaves an unknown outcome that is recovered without replay.

Quotas are lifetime limits in this caller's ledger, with no automatic reset:

- `delegations` counts admitted attempts, including connection and HTTP failures. Local refusals do not consume it.
- `result_bytes` reserves and caps this call's returned UTF-8 content using the remaining allowance. It does not measure wire bytes or model cost.
- `chain_seconds` retains its compatibility name but measures charged local waiting. Remaining allowance bounds this call's timeout. Parent and child waits can overlap.

Known observations settle unused reservations. Unknown outcomes retain the full reservation. Acknowledgment does not refund it. Actual elapsed duration is a separate observation. These are not authenticated root-chain or remote-cost limits. Do not reset quota by deleting state while uncertain requests remain.

## Recover an uncertain outcome

Cancellation, timeout, ambiguous server failure, invalid response, response loss, or failure to commit an observation quarantines the canonical target endpoint. Independent targets continue. Endpoint aliases share quarantine. Restarting MCP preserves it.

The ledger tools show local observations, not remote status. `http_sent` is retained for compatibility and means **submission may have started**, also exposed as `submission_may_have_started`. A pending row is committed before actual transmission, so this field does not prove delivery or execution.

To recover:

1. Inspect the target's logs and effects. Remote query, cancel, resume, and idempotency APIs are unavailable.
2. Stop the MCP writer.
3. Explicitly accept the remaining duplicate-execution risk:

```pwsh
nanobot-teams ack-unknown --ledger C:/BotControl/planner.sqlite3 --task-id <task-id> --reason "Checked effects; accept remaining duplicate risk"
```

Restart MCP afterward. The local account, reason, and time are audited. The original unknown observation and charged quota remain. MCP does not expose acknowledgment. A bot with unrestricted local shell or file access can still cross this administrative boundary; protect control files and restrict tools.

`ledger_unavailable` is distinct from `budget_exceeded`. Admission failure sends no HTTP. A terminal write failure preserves the pending reservation and target quarantine.

## Explicit multi-hop mode

Schema 2 requires a DAG. Multi-hop is off unless `allow_multihop` is true. When enabled, the longest configured path must fit `limits.max_hops`, from 1 to 4. Schema 1 remains single-hop.

`can_start_root` explicitly grants or denies creating roots. If omitted, graph sources with no allowed callers may start roots; receiving bots may not. Set it explicitly in new registries. Intermediate bots cannot reset a chain by omitting fields.

A continuation requires `root_task_id`, `parent_task_id`, `hop`, `deadline_remaining_s`, `expires_at` in Unix seconds, and `chain`. Copy `parent_task_id` from the received block's `task_id`. The server checks graph ancestry, root permission, shape, hop count, loops, and expiry. It deducts elapsed time, caps the forwarded budget to the effective local allowance, and reserves 10% for returning the result.

Models can fabricate these fields. They are advisory cooperation metadata, not authenticated ancestry or a hard distributed deadline. Per-root hard budgets and replay protection require a trusted admission protocol with one-use capabilities and shared accounting. Do not enable this mode between mutually untrusted bots.

## Create a target and inspect deployment

```pwsh
nanobot-teams bot init writer --registry C:/BotControl/teams.json --home C:/Bots/writer --port 8903 --preset glm-5.3-flash --role "drafts summaries" --allow planner
nanobot-teams check --config C:/BotControl/teams.json --caller planner
nanobot-teams graph --config C:/BotControl/teams.json
nanobot-teams budget --config C:/BotControl/teams.json --ledger C:/BotControl/planner.sqlite3
nanobot-teams audit --ledger C:/BotControl/planner.sqlite3
```

`bot init` requires a new home, a valid source preset, nonconflicting API and gateway ports, and a unique generated key variable name. It copies whitelisted connection and preset metadata. Literal strings in API keys, API bases, headers, query, and body overrides become environment references. The checklist prints variable names, never values. Export original field values, including complete header strings. Existing references are retained. Advanced connection fields outside the whitelist need separate manual configuration.

The command holds a registry lock, writes complete temporary files with fsync, and atomically replaces each file. The registry is the final commit point. A normal commit failure removes only the new home it created. A crash can leave an unregistered new home; inspect and move that orphan aside before retrying. This is not a cross-file transaction. Existing homes are never overwritten. Windows ACLs inherit from the chosen parent directory.

`check --caller` checks reachable targets. TCP probing only proves something answers, not its identity. Missing, unreadable, or foreign-team ledgers remain unverified. Budget reporting without explicit ledgers discovers `.teams-state/*.sqlite3` beside the registry.

Pass `check --deployment` with [examples/deployment.example.json](examples/deployment.example.json) to validate operator-declared timeout layers. Target deadline plus 5 seconds must fit local read and total limits. MCP timeout must exceed local total plus 5 seconds; Planner entry timeout must exceed MCP timeout plus 5 seconds. The checker labels declarations as unauthenticated; absent declarations produce `timeouts_unverified`.

The patched Nanobot API includes session-lock waiting in its timeout. An older upstream build may not. Declare `queue_included` only after checking the actual deployment. Timeouts never imply that completed side effects were rolled back.


## Async workbench (0.5)

The async mode lets one Planner hand tasks to several managed bots and keep
talking while they run. Bots execute under Nanobot's public run protocol; the
coordinator only orchestrates and never runs a second agent engine. Closing
the browser does not stop tasks, and a service restart resumes observation of
the same Task ids without re-executing anything.

Enable three sides (all loopback in v1):

1. Each managed bot needs `api.runs` in its Nanobot config with a dedicated
   key, `executionEnabled = true`, and `exclusiveExecution = true`. The
   workbench scaffold prints a ready-to-edit config and a manual start
   command; it never starts a process and shows the key once.
2. Start the resident coordinator: `nanobot-teams coordinator --config
   coordinator.json`. The config names a control directory (outside any
   workspace), a loopback listener, two role token environment names (they
   must differ and be non-empty), the fixed Planner inbox address, and the
   `botHomesRoot` used by scaffolding.
3. On the Planner host set `config.teams.coordinatorUrl` (and
   `coordinatorTokenEnv`) so the gateway proxies `/api/teams/*`, and mark the
   Teams MCP server with `teamsAsync = true` so the host can attach trusted
   routing meta. The workbench appears in Settings once the host reports the
   `teams.async.v1` capability.

Dispatch from the workbench (submit returns the Task id immediately) or let
the Planner call `submit_task` through MCP with a stable UUID operation key —
retry with the same key and body always reconciles to the same task. Cancellation
is requested, then confirmed by observation; unconfirmed stops keep the
instance slot and quarantine it if background effects cannot be proven gone.
Delivery modes: `workbench_only` (result stays in the workbench) or
`auto_resume` (the result is injected into the originating Planner session; a
new user turn or session reset turns late results into cards only).

Versioned state: the coordinator database owns projects, bots, tasks, and
deliveries. Result bodies are kept 14 days, events 30 days, task metadata 90
days; cleaned operation keys can never run again (410). Import a 0.4 ledger
once with `CoordinatorService.import_04_ledger` — usage counters survive,
unknown outcomes stay quarantined, and re-import is a no-op. Rollback: stop
admission, let runs settle, back up the new database; 0.5 databases never
downgrade into 0.4.

## Security and limits

Endpoints are canonical `http://127.0.0.1:<port>/v1/chat/completions` URLs without credentials, query, fragment, or control characters. Redirects and environment proxies are disabled. Only identity encoding is accepted; raw response bytes are bounded before parsing. Invalid Unicode, excessive JSON depth, and compressed responses become unknown outcomes.

Defaults are 16 KiB task text, 256 KiB response body, 32 KiB returned content, 3 seconds connect, 135 seconds read/write, and 145 seconds local total. Timeouts must be finite and at most one day; byte limits are capped at 16 MiB. Registries support at most 64 bots. UTF-8 output truncation preserves character boundaries.

Loopback cannot authenticate a service or prevent port replacement. Target-wide Bearer keys do not authenticate Teams caller identity at the remote API. Selecting reachable keys does not isolate a process from its actual environment or OS account. Restrict target tools, files, network access, and real spending independently. Small requests and results can still trigger expensive remote work. A content-free ledger does not imply the target or model provider retains no content. Teams logs bounded IDs and metrics; the patched API logs text length rather than the first 80 characters.

## Migrate from 0.3

Stop the old writer and back up the ledger with its SQLite sidecars. Preserve the existing explicit `--ledger` path. The new writer adds columns transactionally and binds the ledger to one team and caller. Mixed-caller legacy ledgers require explicit separation. Historical unknown rows remain quarantined. Without historical endpoint identity, they conservatively quarantine all targets until individually acknowledged.

If 0.3 ran without a ledger, unresolved requests cannot be reconstructed. Check their effects before starting a new persistent installation. Pre-0.4 session hashes cannot be continued through new generated thread keys. Start a new thread after resolving outstanding work.

Review cyclic graphs, root permissions, and timeout declarations. Never treat `http_sent=false` as proof of safety. Restart no longer clears unknown outcomes. A 503 is safely rejected only for the bounded Nanobot `configuration_error` contract; other 503s and unexpected 4xx responses are unknown. Known 400/401/403/404/413 rejections remain failed observations.

## Verify

```pwsh
.venv/Scripts/python.exe -m pytest -q
ruff check src tests acceptance
../.venv-nanobot/Scripts/python.exe acceptance/repair_live.py --preset glm-5.3-flash
```

The live runner needs the Nanobot checkout, its runtime environment, and the named configured preset. It creates a new `.e2e/repair-*` directory and can incur model usage. It validates generated configuration, Planner-to-MCP-to-Research delegation, explicit continuation across target restart, and content-free durable observations. It never modifies the user's active configuration. The legacy runner also creates a unique run directory. See [acceptance/README.md](acceptance/README.md) and [REPAIR_STATUS.md](REPAIR_STATUS.md) for scenarios actually rerun.
