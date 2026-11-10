# Inbox re-review

Intent. Audit the repaired asynchronous result continuation against the architecture. Do not fix source. Current HEAD 64f0d46692f72fb49929250b295501937a25895e, with the user's dirty files preserved. Compared scoped diff against 2e9c28cf4.

All probes run local current code with FakeProvider. No model network calls. Real AgentLoop, SessionState and actual aiohttp HTTP are used as relevant. crash_queued launches and forcibly kills its own child before bus dequeue, reopens the persisted session database, then invokes the actual RecoveryCoordinator.scan followed by the inbox recovery scan/pump.

Command from the nanobot-teams-mvp root. Use ./.venv-nanobot/Scripts/python.exe followed by this directory's probe.py and the named scenario. Full retained output is results.txt.

## Proven findings

1. P1. Queue publication discards the only durable continuation obligation before turn admission or processing.
   - nanobot/session/inbox_wake.py:83 and nanobot/session/external_inbox.py:599.
   - Scenario crash_queued. Before kill queued=1, bus_size=1, due=[]. After actual child kill and real recovery scan recovery_pending=[], recovered_wakes=0, planner_calls=0, receipt=injected, recovery_metadata=null, runtime_checkpoint=null.
   - Required. A result that never reached processing must retain a durable scheduling obligation or an explicit recoverable record. This is not a request to auto-replay an interrupted model/tool execution. No execution began and no confirmation is offered.
   - Repair. Persist pending/admitted/processing/settled disposition and delivery identity through the session owner. Keep queued-but-not-admitted delivery recoverable. Transition only with durable admission/processing state and expose uncertain interrupted execution through the existing confirmation policy.

2. P1. Old injected reports wake replacement sessions and revoked workflows.
   - nanobot/session/external_inbox.py:581 selects neither generation nor current grant; nanobot/session/inbox_wake.py:57 checks existence alone; :62 constructs a message without bound session_generation; :76 publishes without revalidating the grant. AgentLoop has no wake-specific grant check at processing admission.
   - Scenario replaced_generation. Apply report, delete original session, recreate same key with unrelated intent, pump, process real turn. queued=1, message_generation_before_processing=null, planner_calls=1, saw_new_intent=true, saw_old_report=false, RequestContext source_workflow=old wf-root, receipt=injected. Required orphaned and zero calls in the replacement generation.
   - Scenario revoked_after_inject. Apply report, process a real new user turn, pump, process wake. grant=false, user_calls=1, queued=1, extra_planner_calls=1, second context source_workflow=old wf-root. Required old wake suppressed after revocation.
   - Repair. Carry the original generation with the obligation and InboundMessage. Revalidate original generation, session ownership and the current workflow grant atomically at actual admission/consumption, including already queued wakes. Revoke/orphan pending obligations without reviving the old root.

3. P1. A report arriving during active Planner work is never supplied to the model.
   - nanobot/session/inbox_wake.py:62 emits an ordinary injectable system input containing only a notice; nanobot/agent/loop.py:1006 permits its mid-turn injection; :1132 converts only that notice while the active transcript predates the separately committed report.
   - Scenario active_result. Real agent.run receives a user turn; FakeProvider mints capability in the actual RequestContext and blocks first call; receive/apply report, pump through actual bus, release first call. planner_calls=2, first_saw_report=false, second_saw_report=false, second_saw_wake=true, history_report_count=1, due=[], receipt=injected.
   - Required. The continuation consumes actual report data under its original workflow. Persisted history alone is insufficient when the running transcript is already materialized.
   - Repair. Treat external report wakes as independent FIFO turns that load the committed history after active work finishes, or inject the actual deduplicated report envelope into the active transcript with preserved workflow scope and durable consumption acknowledgement. The former aligns with the architecture's serial turns requirement.

4. P1. Real user follow-up injection bypasses revocation entirely.
   - nanobot/agent/loop.py:1144 handles a real user input injected into an active turn, while revocation is only called at _process_message:1911 (and process_direct).
   - Scenario injected_user_revocation. Start real agent.run, block first FakeProvider call after minting a capability in its real request context, enqueue another real user input NEW UNRELATED INTENT, release. planner_calls=2, second_saw_new_intent=true, old_workflow_grant=true. A report delivered afterward yields late_receive=received, late_apply=injected, wake_due=1.
   - Required. Every accepted genuine new user input supersedes the previous workflow. The late result must be card_only.
   - Repair. Apply generation-validated revocation at the session admission/consumption boundary shared by independent turns and injected user inputs. Preserve the rejected-stale-input guarantee.

5. P2. An invalid terminal_revision still escapes as HTTP 500.
   - nanobot/api/agent_inbox.py:170 invokes _bounded_int outside the exception boundary at :184.
   - Scenario malformed_revision. Authenticated local POST with otherwise valid capability and terminal_revision='bad' returns HTTP500, Content-Type text/plain, body 500 Internal Server Error. ValueError from int(value) reaches aiohttp.
   - Required structured HTTP400 invalid_field. Parse/validate delivery fields inside the error boundary and before persisting first-use bindings.
   - This is an existing protocol defect retained through repairs, not introduced by the wake pump.

## Closures and verification

- happy_path runs the actual bus and AgentLoop. Exactly one FakeProvider call now occurs, its model-visible transcript includes the external-data/nonapproval envelope, and its RequestContext preserves source_workflow=wf-root. The former blanket no-wake finding is closed on the idle happy path; findings 1 and 3 cover the remaining lifecycle failures.
- closures uses actual local inbox HTTP. Revoked late delivery now returns 200/card_only and persists a receipt. A second receipt under the same capability with a different task/run returns 409 delivery_conflict. mint_teams_meta preserves the original continuation root and returns {} for a nonpersistent, background-incapable host.
- Existing checks passed. ./.venv-nanobot/Scripts/python.exe -m pytest tests/session/test_inbox_continuation.py tests/session/test_external_inbox.py tests/runs/test_a18_revoke.py -q -o asyncio_default_fixture_loop_scope=function. Result 17 passed in 2.18s.
- No repository source or test edits were made. Private artifacts remain in this directory.
- Prove It Works guided keeping runnable current-code failures and rejecting static-only suspicions. No claim is made that an actual model executed an unsafe external action.
