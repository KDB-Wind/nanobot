# Round-4 repair result

Fixed head: this commit series on `fix/teams-round3-handoffs`.
Review baseline: `e90493646`.

Every finding was reproduced on the baseline before any product change. The
repros are committed in the preceding commit; this file records what they
observe after the fix. Each probe prints one JSON line and exits non-zero on a
contract violation.

## Finding to probe

| Finding | Probe | After |
| --- | --- | --- |
| P16-01 workflow stop bypasses ownership | `coordinator/probe_head.py stop_auth` | `direct_status=403, workflow_status=403, operator_task_cancel=none` |
| P16-02 a 404 is treated as a cancel confirmation | `coordinator/probe_head.py lost_submit_404` | `cancel_after_404=unconfirmed, stop_after_404=false, remote_cancels=2, local_dispatch=accepted, local_run=running, observable=1` |
| P16-03 wake consumption loses the continuation | `inbox/probe_consumed_crash.py` | `pending_user_turn=true` survives the kill and restart; `recovery.status=awaiting_user, reason=restart_requires_confirmation, provider_calls=0` |
| P16-04 repeated cancel interrupts cleanup | `runs/probe_cleanup_overlap.py resistant` | `stop_confirmed=false, effects_uncertain=true, isolation=true, live_owned_subagents=1`; next run `blocked_by_isolation` |
| P16-05 isolation write failure still admits work | `runs/probe_isolation_commit.py resistant` | while the meta insert is denied no terminal row exists; after release `stop_confirmed=false, effects_uncertain=true, isolation=true`; next run `blocked_by_isolation` |
| P16-06 migration isolation bypassed by an alias | `coordinator/probe_head.py migration_alias` | `original_admission=409, alias_admission=201, alias_bound_instance=null, remote_submits=0, unknown_isolated=1` |
| P16-07 result reservation released before the body | `coordinator/probe_head.py unfetched_reservation` | `reserved_bytes=32768, confirmed_budget=32769, second_task_admitted=false` |
| P16-08 terminal cancel response ignored | `coordinator/probe_head.py terminal_reconcile` | `status_requests=3, local_revision=3, local_cancel=confirmed, local_stop_confirmed=true, reserved_bytes=0` |

Baseline values for the same probes (recorded in the repro commit):

- P16-01: `direct_status=403, workflow_status=200, operator_task_cancel=confirmed`
- P16-02: `cancel_after_404=confirmed, stop_after_404=true, remote_cancels=0, observable=0`
- P16-03: `pending_user_turn` absent; `recovery=null, checkpoint=null`
- P16-04: `stop_confirmed=true, effects_uncertain=false, isolation=false` with one live owned subagent; next run `succeeded`
- P16-05: terminal row committed, isolation absent, next run executed
- P16-06: original endpoint quarantined, alias admitted, bound and dispatched
- P16-07: `reserved_bytes=0`, second task admitted
- P16-08: observation stopped at the terminal row; the stop carried by the cancel response was never recorded

## Suites

```
pytest nanobot-teams/tests -q                        278 passed
pytest tests/runs tests/session \
       tests/tools/test_mcp_teams_meta.py \
       tests/test_openai_api.py -q                   370 passed, 3 skipped
bun run test src/tests/teams-workbench.test.tsx \
             src/tests/workbench-model.test.ts \
             src/tests/pane-workbench.test.tsx       36 passed
bun run typecheck                                    clean
ruff check <changed Python files>                    all checks passed
```

A filtered strict `basedpyright` run over the changed `nanobot/` files reports
no new error. Its only hit is the pre-existing `nanobot/runs/store.py:154`
(`os.close` argument type), which this branch does not touch; the whole-tree
count is not comparable to CI because this virtualenv lacks the optional channel
dependencies.

The full webui suite was not used as acceptance: unrelated pre-existing tests
(i18n resource parity, settings channels) fail in this worktree, and this branch
touches no file under `webui/`.
