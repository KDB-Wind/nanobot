# Changelog

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
