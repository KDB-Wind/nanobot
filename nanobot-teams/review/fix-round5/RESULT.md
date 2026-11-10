# Round-5 repair result

Fixed head: `fix/teams-round3-handoffs` on the fork. The fix commits are
`7f4f668e4` (runs), `376e63c08` (coordinator), and `054319701`, `dd2f77df6`
(session).
Review baseline: `e56d24cbf`.

Every finding was reproduced on the baseline before any product change. The
repros are committed in `test(teams,runs): reproduce round-5 cancellation,
upgrade and wake defects`. Each probe prints one JSON line per observation and
exits non-zero on a contract violation.

## Finding to probe

| Finding | Probe | Before (e56d24cbf) | After |
| --- | --- | --- | --- |
| R4-01 concurrent cancel shares one cleanup flag | `runs/probe_parallel_cancel.py a` (also `b`) | `resisting_final stop_confirmed=true, effects_uncertain=false, isolation=false, live_owned_subagents=1`; `next_run=succeeded` | both orders: `cooperative_final stop_confirmed=true, effects_uncertain=false`; `resisting_final stop_confirmed=false, effects_uncertain=true, isolation=true, live_owned_subagents=1`; `next_run=blocked_by_isolation` |
| R4-02 upgrade leaves an active legacy quarantine without a guard row | `coordinator/probe_upgrade_alias.py` | `guards=0`; `alias_before_resolve dispatch_state=accepted, remote_submits=1` | `guards=1, endpoint=http://127.0.0.1:<port>, secret_env=KEY`; reimport `imported=false` with quota `[1, 32768, 0]` unchanged; reopen idempotent; alias `pending, remote_submits=0`; after `resolve_quarantine` `accepted, instance_id=inst_a, submits=1` |
| R4-03 completed wake leaves the pending marker | `inbox/probe_completed_wake.py` | `pending_user_turn=true` after the turn; restart `recovery.status=awaiting_user` | `pending_user_turn=null, checkpoint=null`; restart `recovery=null` |

The upgrade probe creates its database with the real `e904936` source tree
(`old_import.py` under that tree's `nanobot-teams`), so the starting schema is
genuinely old rather than simulated.

Crash-after-consumption still holds: `inbox/probe_consumed_crash.py` leaves
`pending_user_turn=true` across the kill and restart yields
`recovery.status=awaiting_user, reason=restart_requires_confirmation, provider_calls=0`.

A strict `basedpyright` pass over the changed `nanobot/` files exposed the
mid-turn path as a type mismatch (`RequestContext.turn_id` is `str | None`).
The end-to-end test `test_active_turn_settles_its_wake_under_a_public_turn_id`
drives a live turn with the channel id `public-42`, injects a wake mid-turn and
asserts the marker is gone; it fails on the pre-fix code and passes now.

## Changes

- `nanobot/runs/executor.py`, `nanobot/runs/service.py`: `RunCancelled` carries
  `stop_confirmed` on the cancelled execution itself. `_execute_run` derives
  `unverified` from that exception; a plain `CancelledError` (deadline,
  shutdown, external task cancel) never proved cleanup and stays unverified.
- `nanobot-teams/src/nanobot_teams/coordinator/store.py`: `open()` backfills a
  guard row for every active `legacy:` quarantine. The insert is idempotent and
  only new rows emit `legacy_guard_backfilled`; an endpoint with no registered
  bot keeps the guard unresolved with an empty credential.
- `nanobot/session/external_inbox.py`, `nanobot/session/state.py`,
  `nanobot/session/recovery.py`, `nanobot/agent/loop.py`: the pending marker
  records `{delivery_id, generation, turn_id}`. The loop creates the turn id
  before consuming the wake and passes it to `finish_turn`, which clears the
  marker only when the persisted owner matches the completing turn.
- The mid-turn injection path takes the owner id from `_run_turn`
  (`turn_owner_id=ctx.turn_id`) instead of `RequestContext.turn_id`, which
  carries the channel's public id and may differ or be absent. A cancelled
  turn keeps the marker: it did not finish the wake it accepted.

## Suites

```
pytest nanobot-teams/tests -q                              280 passed
pytest tests/runs tests/session \
       tests/tools/test_mcp_teams_meta.py \
       tests/test_openai_api.py -q                        371 passed, 3 skipped
pytest tests/agent/test_loop_save_turn.py \
       tests/agent/test_auto_compact.py -q                141 passed
bun run test src/tests/teams-workbench.test.tsx \
             src/tests/workbench-model.test.ts \
             src/tests/pane-workbench.test.tsx             36 passed
bun run typecheck                                          clean
ruff check <changed Python files>                          all checks passed
```

The full `tests/agent` directory also runs: 2160 passed, 10 failed. All ten
failures are in `test_mcp_connection.py` and `test_mcp_transient_retry.py`, where
the test fake rejects the installed client's `meta` kwarg; they fail the same
way on the pre-fix tree and this change does not touch MCP.

Round-4 probes rerun on the fixed head: `coordinator/probe_head.py` in all
five modes, `runs/probe_cleanup_overlap.py resistant` and `deadline`,
`runs/probe_isolation_commit.py resistant`, `coordinator/probe_adjacent.py`,
and the inbox adjacent cases `ACTIVE_RESULT`, `REPLACED_GENERATION`,
`REVOKED_AFTER_INJECT`, `INJECTED_USER_INVOCATION`.

## Reproduce

Run the probes with the worktree root and the plugin source on `PYTHONPATH`:

```
export PYTHONPATH=$PWD:$PWD/nanobot-teams/src:$PWD/nanobot-teams/review/fix-round5/coordinator
python nanobot-teams/review/fix-round5/runs/probe_parallel_cancel.py a
python nanobot-teams/review/fix-round5/coordinator/probe_upgrade_alias.py
python nanobot-teams/review/fix-round5/inbox/probe_completed_wake.py
python nanobot-teams/review/fix-round5/inbox/probe_consumed_crash.py
```
