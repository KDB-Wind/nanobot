# Changelog

## 0.6.0 - 2026-11-10

Bot-to-bot async collaboration (peer comms) and per-bot cache statistics.
The planner workbench keeps its 0.5 semantics; workers can now dispatch to
each other along explicit authorization edges, and every bot's usage is
queryable from the coordinator and visible in the workbench.

### Added

- Per-bot identity and receive bindings on the coordinator: the `bots`
  table carries `inbox_url`, `inbox_principal_id`, `inbox_key_env`, and
  `coord_secret_env` (idempotent ALTER on legacy databases). A bearer
  token matching a bot's coordinator credential identifies that bot as
  its own principal; every role-checked route denies bot principals by
  default except reading, looking up, and cancelling their OWN tasks.
- `POST /v1/teams/peer/send` plus the `send_to_bot` MCP tool: bot
  principals dispatch tasks along explicit `peer_edges` (default deny,
  operator-managed). Admission enforces the 4-hop conversation bound,
  recipient-reentry cycle rejection (A->B->A), a per-pair hourly message
  window, and the existing quota machinery (fanout rides principal_open,
  recipient concurrency rides bot_queue) inside one transaction; every
  typed rejection is audited with sender/recipient/code/hop. Repeated
  operation keys replay the original task.
- Peer replies return to the SENDER's inbox (I5): the capability is
  validated against the sender's own receive binding before admission and
  delivered there after the recipient finishes; on inbox-enabled hosts the
  run executor binds the durable `peer:<workflow>` conversation session so
  capabilities survive the ephemeral run and wakes continue the
  conversation.
- `nanobot serve` can host the runs inbox on its own API listener
  (ADR-001, `api.runs.inboxEnabled`, default off): one process per bot
  both executes dispatched runs and receives peer results. Generated
  worker scaffolds are fully peer-wired (inbox + teams-async MCP client
  with `$\{ENV\}` credentials and a self-contained PYTHONPATH).
- Per-bot usage and cache hit rates: `GET /v1/teams/usage` and
  `GET /v1/teams/bots/{id}/usage` read each bot's own
  `llm_usage.sqlite3` strictly read-only (`mode=ro` + `query_only`). The
  hit-rate denominator is the input tokens of calls that REPORTED cache
  fields; missing data stays null (never 0%) with a coverage ratio; the
  window selector covers 7d/30d/all with per-model splits. `serve`
  records llm usage like the gateway already did.
- Workbench: per-bot usage panel (hit rate, coverage, denominator note in
  the header tooltip), the bot graph of explicit peer edges, and a route
  column marking peer tasks with sender -> recipient and hop, localized
  in every supported locale.

### Verified

- The four-bot probe (`acceptance/peer_comms_probe.py`) runs the real
  chain green with real models: planner -> research -> (send_to_bot) ->
  writer -> reply delivered to research's inbox (validate 200, deliver
  202, receipt injected and the conversation woken) -> research's summary
  carries the writer's marker. Unauthorized edges, the fifth hop, and
  duplicate keys answer with typed rejections; killing the coordinator
  mid-flight converges to exactly one completed task after restart.
- Real usage evidence: research recorded 4 requests with 25,344
  cache-read tokens (94.0% hit rate, 100% coverage) during the live run.


## 0.5.0 - 2026-11-09

Async multi-bot workbench (one operator, one Planner, many managed bots).
The 0.4 synchronous MCP tools keep their semantics; the new async mode runs
alongside them and the two modes must not manage the same bot instances.

### Added

- Nanobot public agent-run protocol v1 (`/v1/agent-capabilities`,
  `/v1/agent-runs…`, `/v1/agent-inbox…`): idempotent submission by
  `Idempotency-Key`, persisted admission before execution, typed terminal
  outcomes with stop confirmation and effect-uncertainty flags, bounded
  bodies (16 KiB in, 32 KiB out, 256 KiB frames), SSE events with cursor
  expiry, and a dedicated `api.runs` key that the plain `api.apiKey` never
  receives. Exclusive managed bots answer 409 `managed_execution_only` on
  the chat endpoint.
- Persistent run domain (`nanobot/runs`): SQLite store with permanent
  idempotency tombstones across the 14/30/90-day retention tiers, deadline
  sweeps, cooperative cancellation with a 5-second stop-confirmation window,
  instance isolation on unconfirmed effects, and real hard-kill recovery
  tests (queued resumes; active runs become `interrupted` and never rerun).
- Resident Teams coordinator (`nanobot-teams coordinator`): single-writer
  control database, project/bot catalog with explicit instance binding and
  operator-only rebinding, quota-bounded admission with zero HTTP on
  failure, dispatch reconciliation by the original operation key after
  lost responses, endpoint-identity pre-checks that quarantine changed
  instances, and deterministic delivery of terminal results to the fixed
  planner inbox.
- Async stdio MCP (`nanobot-teams async-mcp`): `submit_task` returns the
  Task id immediately; trusted routing meta travels outside the model's
  tool schema and forged routing arguments are rejected.
- Trusted reply channel: host-minted reply capabilities (digest-only
  storage, first-use binding), durable planner inbox with atomic history
  injection (crash-safe replay), workflow auto-continue revocation on new
  user turns, and card-only delivery after revocation or session loss.
- WebUI workbench: `teams.async.v1` capability, gateway proxy with
  WebSocket-gated mutations, coordinator event relay, project/bot/task
  views in English and Simplified Chinese, and managed-bot home scaffold
  that prints a one-time service key and a manual start command.
- Cron path: `teams_task` jobs derive the submission key from the job id
  and fire time, so duplicated fires reconcile to one coordinator task.
- 0.4 migration import: idempotent ledger import that preserves caller
  usage counters and quarantines historical unknown outcomes.

## 0.4.0 - 2026-11-08

### Changed

- Commit durable admission and byte/time reservations before HTTP. Quarantine unknown endpoints across MCP restarts, preserve pending requests after process death, and enforce one writer per scoped ledger.
- Isolate new task sessions. Continue only with a previously returned thread key belonging to the same caller and target.
- Disable multi-hop by default. Require a DAG, root permissions, complete expiry fields, and bounded advisory propagation. Authenticated root-chain quotas remain unsupported.
- Replace ledger write fail-open behavior with `ledger_unavailable`. Add the local `ack-unknown` CLI with an audited account and reason; confirmation preserves the unknown observation and charged quota.
- Reject compressed, deeply nested, and invalid-Unicode responses. Record possible submission after transport loss and conservatively classify unrecognized errors.
- Externalize provider and preset connection strings, reject existing homes and key-variable collisions, serialize registry updates, and commit generated configuration safely.
- Check reachable targets and operator-declared timeout layers; report unverifiable ledger data explicitly.
- Patch the accompanying Nanobot API to include session-lock waiting in its deadline and log text length instead of a prompt prefix. Teams runtime still imports no Nanobot internals.

Migration and remaining trust boundaries are documented in README, DESIGN, and REPAIR_STATUS. Version 0.3 records below describe the historical baseline.

## 0.3.0 - 2026-11-08

First release beyond the single-caller MVP. Everything stays inside the plugin:
no Nanobot core changes, no private imports, no direct session reads.

### Added

- Schema 2 registry: per-bot `role`, `capabilities`, `model_preset`,
  `allowed_callers`, and one `--caller` identity per installation.
- Multi-hop delegation with an envelope (`task_id`, `parent_task_id`,
  `root_task_id`, `hop`, `deadline_remaining_s`, `chain`), hop and loop limits,
  and deadline propagation.
- Caller budgets (`delegations`, `result_bytes`, `chain_seconds`) enforced before
  each delegation.
- SQLite ledger behind `--ledger`, with `list_tasks` and `get_task` MCP tools.
  Entries are local observations, not proof of remote success.
- `bot init` and `check` lifecycle commands, plus `graph`, `budget`, and
  `audit` views.
- CI workflow for Ubuntu and Windows running `ruff` and the unit suite.

### Changed

- `bot init` requires `--role`, validates the whole registry before writing, and
  emits provider credentials as `${PROVIDER_API_KEY}` references.
- `audit` and `budget` open the ledger read-only and report missing files instead
  of creating empty databases.
- `list_bots` returns `model_preset` alongside role and capabilities.
- Acceptance harnesses run from a standalone checkout through
  `NANOBOT_TEAMS_WORKTREE` and `NANOBOT_TEAMS_WEBUI`; the packaging check reads
  the version from `pyproject.toml`.

### Fixed

- Ledger `started_at` used a monotonic clock as wall time and displayed 1970.
- Ledger write failures were silent; they are logged now.
- Generated bot configs copied the provider's literal API key.

### Verified

- 152 tests on Windows 11 and Ubuntu 22.04 (WSL2); `ruff` clean on both.
- CI green on `ubuntu-latest` and `windows-latest` (`ruff` plus `pytest`), with
  ruff pinned to 0.17.4 through the `dev` extra.
- 16 live server scenarios and the WebUI scenario on Windows with
  `step-5-preview`.
- `packaging_fresh`: wheel built, installed into a clean venv, delegated, then
  uninstalled.

### Not supported

- Remote task query, cancel, resume, or idempotency.
- Autonomous group chat, mentions, or proactive pushes.
- Auto-starting or supervising bot processes.
- Cross-process budget locking.
- A UI for defining bots.
