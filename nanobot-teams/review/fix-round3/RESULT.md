# Round-3 result

Baseline for the fixes: 64f0d4669 plus the untracked review artifacts from
round 2. All findings were reproduced on that baseline before any product
change; the probes in review/round2-20261109 record the corrected contract and
exit 0 on this HEAD.

## Finding to observed contract

| ID | Probe | Observed after the fix |
| --- | --- | --- |
| N01 | coordinator/probe_round2.py cancel_race | cancel_response=unconfirmed, stop_claim_while_in_flight=false, submits=1, cancels=2, observable=1 |
| N02 | inbox/probe.py crash_queued | due list still holds the delivery after publish; recovered_wakes=1, planner_calls=1 |
| N03 | inbox/probe.py replaced_generation / revoked_after_inject | queued=0 and planner_calls=0 with receipt=orphaned; extra_planner_calls=0 with receipt=injected |
| N04 | inbox/probe.py injected_user_revocation | old_workflow_grant=false, late_apply=card_only, wake_due=0 |
| N05 | inbox/probe.py active_result | second_saw_report=true, second_saw_wake=true, due=[] |
| N06 | runs/probe_cleanup_round2.py resistant/deadline/exception | stop_confirmed=false, effects_uncertain=true, isolation=true, next run blocked_by_isolation; exception path reaped the child (real returncode asserted in tests/runs/test_executor_reap.py) |
| N07 | coordinator/probe_quarantine.py | second_isolated=true, third_task_admitted=false |
| N08 | coordinator/probe_round2.py uncertainty | principal_open=1, reserved_bytes=32768, cancel_after_terminal_uncertain=unconfirmed |
| N09 | coordinator/probe_migration.py | admission_status=409 bot_quarantined, remote_submits=0 |
| N10 | runs/probe_runs_round2.py result_persistence | state=succeeded, text=PERSIST_ME, executor_calls=1, no isolation |
| M01 | runs/probe_runs_round2.py http_storage | 503 storage_unavailable retryable=true on status/result/cancel |
| M02 | runs/probe_runs_round2.py retention | row_kept=true, events_before=3, events_after=3 |
| M03 | coordinator/probe_round2.py migration_bytes | new_task_admitted=false |
| M04 | coordinator/probe_round2.py mcp_bounds | gzip_accepted=false, code=response_compressed |
| M05 | coordinator/probe_scaffold_config.py | with preset: loaded and scaffold 201; without: 422 preset_required |
| M06 | inbox/probe.py malformed_revision | 400 JSON invalid_field |
| M07 | ui/submission.test.tsx | 3 passed, distinctKeys=2 across projects |
| stop | tests/webui/test_teams_mutations.py, test_coordinator_http.py | teams.stop_workflow maps to the route; grant revoked before task cancels |
| recovery | test_coordinator_http.py, test_agent_runs_http.py | quarantine resolve records actor/note; clear-isolation records actor/reason |

## Suites

- `pytest nanobot-teams/tests -q`: 277 passed.
- `pytest tests/runs tests/session tests/tools/test_mcp_teams_meta.py tests/test_openai_api.py -q`: 370 passed, 3 skipped.
- `webui`: teams-workbench 11 passed, typecheck clean; round-2 UI review 3 passed.
- `ruff check` on every changed non-review Python file: All checks passed.

## Notes

- basedpyright is not installed in .venv-nanobot, so the strict type gate was
  not run. The project command `uv run --no-sync basedpyright` needs the full
  dev sync first.
- The probes create temporary working directories next to themselves; those are
  ignored by review/round2-20261109/.gitignore.
- The 0.5.0 wheel in nanobot-teams/dist predates these fixes and is not used by
  the tests (they run from src). Rebuild it before a fresh-install acceptance.
