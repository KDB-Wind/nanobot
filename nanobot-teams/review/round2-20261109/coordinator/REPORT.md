# Coordinator round 2 audit

Intent. Verify the R0–R5 repair claim against current executable behavior without modifying implementation or tests. Repo HEAD 64f0d46692f72fb49929250b295501937a25895e plus existing dirty tree. All probes use synthetic tokens, local HTTP, SQLite and the actual current coordinator. No model calls or existing process changes.

Run each command from C:/Users/KDB/orca/workspaces/nanobot/nanobot-teams-mvp with .venv-nanobot/Scripts/python.exe followed by this directory's script path. Outputs saved beside scripts. Every listed finding reproduced twice.

## Verified findings

### C1. P1. Cancelling during the first in-flight submit still confirms a phantom stop.

Command. `.venv-nanobot/Scripts/python.exe C:/Users/KDB/AppData/Local/Temp/nanobot-teams-review-round2-20261109/coordinator/probe_round2.py`, `cancel_race` output.

Actual. `attempts_while_submit_in_flight=0, cancel_response=confirmed, stop_claim_while_in_flight=true, submits=1, cancels=0, final_cancel=confirmed, final_run=cancelled, final_dispatch=accepted, observable=0`.

The fake Bot handler blocks after receiving the actual POST. The coordinator accepts cancellation while that request is in flight, and then the handler creates the remote run. No cancellation reaches the Bot, and the local cancelled row is excluded from observation forever.

Source. coordinator/store.py:920–940 treats attempts=0 as proof of never sent. worker.py:183 submits without a preceding durable started marker. store.py:748–765 blindly accepts the response against the locally cancelled row. store.py:1109–1116 excludes cancelled tasks.

Expected. Cancellation remains unconfirmed once a submission may have started. Reconcile the original key and drive cancellation until actual remote stop. Persist dispatch intent before any await that can overlap cancellation, and re-read task state after awaited preflight.

### C2. P1. A resolved quarantine can never become active on a later incident.

Command. `.../probe_quarantine.py`.

Actual. `first_isolated=true, second_effects_uncertain=true, second_stop_confirmed=false, second_isolated=false, third_task_admitted=true, remote_submits=2`.

The first real worker observation quarantines the instance. The local operator resolves it. A second real task reports uncertain effects, but the UPSERT changes only the reason and leaves the old resolved_at. The third task is accepted while the second incident is unconfirmed.

Source. coordinator/store.py:457–465, especially UPSERT line463; same pattern in mark_indeterminate at1028–1031. resolve_quarantine sets resolved_at at471.

Expected. Each new incident activates isolation again while preserving previous acknowledgement history. Clear resolution columns or record distinct incidents rather than updating a resolved row in place.

### C3. P1. Terminal uncertainty releases quotas, lies about subsequent cancellation, and cannot later converge.

Command. `.../probe_round2.py`, `uncertainty` output.

Actual. A real worker observes `effects_uncertain=true, stop_confirmed=false`, but `principal_open=0, reserved_bytes=0`; a later `request_cancel` returns `confirmed`; after ack and a remote state change, `old_observations_remaining=0, old_run_after_remote_resolved=interrupted`. New task admission succeeds after ack.

Source. coordinator/store.py:507–546 predicates quota retention only on run-state nonterminal, excluding interrupted even when stop is not confirmed; request_cancel at893–901 unconditionally confirms all terminal states; tasks_to_observe at1109–1116 excludes all terminal states. worker.py:564–568 refuses to drive cancellation for terminal state regardless of uncertainty.

Expected. Unknown stop/effects retain open slot and maximum reservation, even after operator acknowledgement. Later cancel cannot claim confirmation without proof. Continue authoritative observation/cancel reconciliation independently of execution terminal state.

### C4. P1. Real management registration leaves migrated unknown isolation disconnected from dispatch.

Command. `.../probe_migration.py`.

Actual. `registered_instance=null, unknown_isolated=1, quarantines=[legacy:http://127.0.0.1:PORT], admission_status=201, remote_submits=1, bound_instance_after_dispatch=inst_a`.

The probe registers a Bot through actual POST /v1/teams/bots, imports a real Ledger containing an unconfirmed unknown, and confirms quota using actual HTTP. New work is accepted and executed against the same endpoint while old uncertainty remains. Registration cannot bind an instance, so this is the normal first-use migration path.

Source. http_api.py:504–513 creates Bot without an instance ID; service.py:100–106 maps only pre-bound instances and151–163 falls back to a legacy key; admission store.py:615–619 and dispatch worker.py:132–138 check only bound instance keys. First dispatch only binds after submit, worker.py:206–207.

Expected. Unknown endpoint isolation blocks admission and dispatch even before first instance bind; discover authenticated identity before activation, atomically map legacy guard, retain endpoint guard until resolved.

R5 evidence consequence. acceptance/live_workbench_r5.py scenario_migrate registers Bots through this exact management route and expects admission201 after quota confirmation; its saved migrate-results.json records unknown_isolated1 followed by post-confirm201. This live result does not prove preserved unknown isolation. The private probe demonstrates actual dispatch as well.

### C5. P2. Imported result-byte limits are stored but never enforced.

Command. `.../probe_round2.py`, `migration_bytes` output.

Actual. `imported_result_used=32768, confirmed_result_limit=32768, new_task_admitted=true`.

Source. coordinator/store.py:624–646 only selects and checks imported delegation limits. result_bytes_limit is written at1188–1194 and read for display at1208–1223, but does not enter admission or result settlement.

Expected. With no remaining byte budget, admission must reject. Import counters and limits must constrain reservation and settlement in addition to new-workflow limits.

### C6. P2. MCP CoordinatorClient still accepts compressed and oversized control responses.

Command. `.../probe_round2.py`, `mcp_bounds` output.

Actual. `gzip_accepted=true, decoded_bytes=1048576` for GET /v1/teams/bots through actual CoordinatorClient.

Source. async_mcp.py:120–127 calls the eager request and response.json without the bounded protocol reader. BotRunClient's fix did not reach this authenticated control client.

Expected. Reject compressed payload before consuming body and enforce 256KiB raw cap, strict JSON and depth before parsing. The proxy exclusion itself is fixed (`trust_env=False`).

### C7. P2. Scaffold cannot be enabled through the real coordinator config loader.

Command. `.../probe_scaffold_config.py`.

Actual. Setting scaffold_model_preset in coordinator.json raises `Unknown coordinator config keys: ['scaffold_model_preset']`; omitting it loads `scaffold_model_preset=''`; actual POST scaffold returns422 `preset_required`.

Source. coordinator/config.py:16–26 excludes new scaffold fields; loader at110–121 does not transfer them to CoordinatorConfig, whose defaults at44–48 are empty. http_api.py:557–565 refuses empty preset.

Expected. The supported CLI config must accept and populate scaffold_model_preset and scaffold_user_config so the otherwise repaired generator can be used. Existing tests construct CoordinatorConfig directly and therefore miss this boundary.

## Closed old behavior confirmed

Executed selected formal regressions with actual current code. Command `.venv-nanobot/Scripts/python.exe -m pytest nanobot-teams/tests/test_coordinator_guards.py nanobot-teams/tests/test_result_pipeline.py nanobot-teams/tests/test_control_client_bounds.py nanobot-teams/tests/test_scaffold_bot.py nanobot-teams/tests/test_async_mcp_stdio.py -q --basetemp=C:/Users/KDB/AppData/Local/Temp/nanobot-teams-review-round2-20261109/coordinator/pytest`. Result31 passed in8.24s.

This confirms the specific covered corrections. A cancel before worker submission closes the outbox; prebound instance quarantine blocks admission and old outbox; prebound aliases share queue reservations; first idempotency conflict/uncertain observation/storage-loss quarantine; single transient result failure recovers; remote truncation propagates; authenticated public result route and MCP include_result retrieve content; BotRunClient rejects gzip/oversize/deep/duplicate JSON and excludes environment proxy; generated JSON loads and a real new nanobot serve answers capabilities with the service key while browser/config lack raw key; real stdio carries MCP meta and pins workflow root. These tests do not close C1–C7.

## Other checked facts, not additional verified findings

The real coordinator --help exposes only --config. ack-unknown --help targets the old Ledger with --ledger/--task-id. Searches show coordinator resolve_quarantine and force_rebind_instance only declared, never called from production CLI or management code. I did not turn absence into another proved failure, since C2 uses the explicitly present local operator method directly and the strongest current runtime failures are above.

The R5 chain script embeds its marker in Planner's initial prompt, checks writer existence without checking creation after injected wake, and records browser-accept success using turn_started or True. These are observed source limitations, not a verified claim that the saved live run skipped continuation. No additional finding claimed without executing a counterexample.

No further agents were used. No source or test file was edited.
