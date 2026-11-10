# Round-3 repair design

Scope: close the 10 P1 groups and 7 P2 items in ASYNC_WORKBENCH_REVIEW_ROUND2.md.
Order: T1 execution ownership, T2 continuation, T3 incidents and metering,
T4 API/config/UI boundaries, T5 workflow stop.

The shared fault is one premise: a local boolean or an in-memory return value is
treated as proof that the next layer acted. Dispatch used attempts=0 as "never
sent", the wake pump used a bus publish as "turn accepted", the executor used a
normal return as "work stopped", and the run service used a terminal observation
as "stop confirmed and cost settled". Each unit replaces that premise with a
persisted fact at the layer that owns it.

## T1 execution ownership and result commit

### Dispatch send fact

Add dispatch_outbox.submit_started_at. The worker writes it in its own
transaction before the first HTTP byte and never clears it. A cancel that sees
it set can no longer confirm "never sent"; it becomes unconfirmed and the worker
reconciles by the original operation key, then cancels the run it finds.

After every awaited preflight and before submit, re-read the task. A cancel or
isolation that arrived during the await stops the submit. If the submit was
already in flight, accept its run id, then drive a remote cancel on it in the
same pump.

_drive_cancel handles both requested and unconfirmed cancels and no longer
refuses a terminal run whose stop is unconfirmed. A cancel becomes confirmed
only on a remote observation with stop_confirmed true.

### Stop report

AgentLoop.stop_session_work returns SessionStopReport{cancelled,
live_subagents, live_exec_sessions}. It is built from the same cancellation
pass that already awaits tasks, subagents and exec sessions, then reads their
live state. Executor stop confirmation is report.confirmed, not "the call
returned". Every executor exit path, including an exception, runs the reap and
downgrades the result to stop_confirmed=false and effects_uncertain=true when
anything is still live.

### Result commit

RunService keeps an in-memory pending result when the store is unavailable. The
pump retries that exact result before reaping. A run with a pending result is
not an orphan, is never rewritten to cancelled, and is never re-executed. If
the process ends first, recovery marks it interrupted (unknown), not cancelled.

## T2 durable continuation

wake_due stays 1 until the turn is accepted. The pump no longer clears it. It holds an in-process lease per
delivery (30s) so one pump tick cannot double-publish; lease state dies with
the process, which is exactly when the obligation must be republished.

Consumption clears the obligation at the two real acceptance points:
- idle turn: AgentLoop._dispatch_one before _process_message;
- mid-turn injection: _drain_pending, after the message's generation check.

Every wake message carries session_generation and the report body under the
same non-authority envelope used for history injection. The pump validates the
current session generation and the workflow grant before publishing; both
checks repeat at consumption. A replaced generation marks the receipt
orphaned. A revoked workflow drops the wake but keeps the report receipt.

Every accepted genuine user input revokes the session's workflows, including
the injected path in _drain_pending, with the generation check first.

## T3 incidents, unknown metering, migration

Quarantine is an incident record: re-quarantining an instance clears the
previous resolved_at/by/note in the same transaction and emits
instance_quarantined with the prior reason. The resolution audit stays in the
event log.

Stop confirmation, execution state and settlement are separate:
- request_cancel confirms a terminal task only when stop_confirmed and not
  effects_uncertain; otherwise it is unconfirmed and drives a remote cancel.
- quota open counts and result reservation keep a task while it is terminal
  but unconfirmed, using the same reservation rule as an open task.
- a later cancel never inherits "confirmed" from a terminal run.

Dispatch identity: endpoint guard key is legacy:<normalized endpoint>. Admission
checks the bot's instance quarantine and, for an unbound bot, its endpoint
guard. Before the first submit to an unbound bot the worker probes
capabilities, binds the instance in one transaction, re-checks that instance's
quarantine, and only then submits. A legacy unknown therefore blocks the same
topology the migration imported, whether or not the bot was pre-bound.

Retention deletes only events whose run is eligible for row deletion; an
unconfirmed run keeps row, body and events together. Migration byte usage is
enforced alongside delegation count, using imported usage plus reservations and
settled bodies of tasks admitted since import.

## T4 API, config, UI boundaries

Storage faults are 503 retryable, never 404. The MCP control client uses the
same bounded read contract as BotRunClient (no compressed bodies, 256 KiB
decoded cap, streamed). The coordinator config loader accepts and validates
scaffold_model_preset and scaffold_user_config. Inbox field parsing happens
inside the structured 400 boundary. The WebUI draft identity includes project.

## T5 workflow stop

Add one operator/planner entry that revokes the workflow grant first, then
cancels its unsettled tasks, and reports both sets. The workbench exposes it.
