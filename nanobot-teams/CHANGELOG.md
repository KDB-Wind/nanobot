# Changelog

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
