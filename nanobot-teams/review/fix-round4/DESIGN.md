# Round-4 repair design

Premise: each finding treats a local observation as proof of a remote fact. The fix gives each handoff one persisted fact and makes the claim read that fact back.

## P16-01 workflow stop authorization

Authorization is a predicate over the caller and the task set, computed before any grant revocation or task write. A planner sees only tasks with `caller_principal='planner'`; an empty set is 403. An operator sees the workflow's full set. The revoke happens only after that check.

## P16-02 a 404 is a negative observation

A missing row at one instant cannot fence an in-flight POST. `_reconcile_cancelled_pending` keeps the cancel unconfirmed, leaves the outbox pending, and reconciles the same operation key again. `close_cancelled_unknown` goes away because no local observation can prove a request will never commit.

## P16-03 wake consumption handoff

The session owner's `pending_user_turn` marker is written in the same session-store transaction that clears `wake_due`. A crash after consumption leaves the marker, so `RecoveryCoordinator.scan` turns it into an awaiting-user card. A completed turn clears the marker in `_save_turn`, so no stale state survives success.

## P16-04 cancellation uncertainty

The executor sets `_cancel_unverified=True` before awaiting the reap and clears it only after a typed confirmed report. `RunService.cancel` never injects a second `CancelledError` into a CANCELLING run. `_sweep_deadlines` only cancels when it moved RUNNING to CANCELLING.

## P16-05 terminal state and isolation in one transaction

`RunStore.transition` takes `isolation_reason` and writes the `instance_meta` isolation row inside the same `BEGIN IMMEDIATE` transaction. `_finalize_cancelled`, `_reap_orphan_cancelling`, and `_apply_result` use it. A failed meta write rolls back the terminal row, so the run stays for the pump to retry.

## P16-06 legacy endpoint identity

A new `legacy_endpoint_guards` table records the guard key, the original endpoint, the credential environment the 0.4 task used, and the instance once it is mapped. Preflight probes the original endpoint with that credential before binding a new instance. Same instance means defer and persist the mapping; a different instance maps and allows. A rejected probe, a missing credential, or an unreachable guard defers, and the operator resolves the incident.

## P16-07 result byte reservation

Keep `unsettled` for open slots. Add `result_unsettled = unsettled OR (result_available=1 AND result_fetched=0)` for both byte sums. The maximum reservation stays until `persist_result_body` lands the body.

## P16-08 terminal cancel observation

`tasks_to_observe` includes terminal tasks whose cancel is requested or unconfirmed and whose stop is not confirmed. The existing validated observation path records the newer revision and confirms the cancel. No new remote promise is added.
