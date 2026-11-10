# Run-layer continued audit

User intent is to audit the R0 to R5 repair result without making product fixes. This report covers current HEAD 64f0d46692f72fb49929250b295501937a25895e plus the original dirty files in C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp. The comparison baseline is 2e9c28cf4 and nanobot-teams/ASYNC_WORKBENCH_REVIEW.md.

All commands ran in that repository with pwsh and .venv-nanobot/Scripts/python.exe. Imported nanobot.runs.service and executor resolved to this repository. Product files and existing tests were not changed. Repros and logs are in this private directory. All child processes and resistant child tasks created by the repros were released or closed in finally blocks. Providers are offline Python test doubles. No model API requests were made.

The Prove It Works principle changed the verification choice. I checked live owned subprocesses and subagent tasks after the public run claimed a confirmed stop. I did not infer cleanup from terminal state or the green suite. This review uses the inherited GPT family and does not claim cross-family independence.

## Verified defects

### R2-RUN-01. P1. Cancellation and deadline confirmation ignore a live resistant owned subagent

Source anchors are nanobot/runs/executor.py:55, nanobot/agent/loop.py:982, nanobot/agent/subagent.py:423, nanobot/agent/subagent.py:972, and nanobot/runs/service.py:414.

Commands from the repository root are:

```pwsh
.\.venv-nanobot\Scripts\python.exe 'C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-round2-20261109\runs\probe_cleanup_round2.py' resistant
.\.venv-nanobot\Scripts\python.exe 'C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-round2-20261109\runs\probe_cleanup_round2.py' deadline
```

The real AgentLoop invokes the real subagent tool. Its offline child provider deliberately catches cancellation and remains alive until the probe releases it. The probe retains the default SubagentManager five-second cleanup waits. RunService.cancel_wait_s is shortened to .05 only for the HTTP-like caller wait. It does not shorten the manager cleanup wait.

Actual cancel output was state=cancelled, stop_confirmed=true, effects_uncertain=false, isolation=false, live_owned_subagents=1, cancellations_seen=1. A second run reached succeeded while prior_child_alive=true. The deadline variant produced outcome=deadline_exceeded with the same clean-stop flags and live child, then the second run also succeeded.

Required output keeps the run cancelling and the execution capacity blocked while owned work remains alive, or records an unconfirmed uncertain stop and isolates the instance. A bounded cancellation method returning an item count is not proof that the items exited.

The minimum repair is a typed cleanup observation that checks pending subagent execution and cleanup before confirming stop. The executor must propagate unconfirmed cleanup. The service must preserve the uncertainty and block future execution. This is an incomplete F04 closure. Logs are cleanup_resistant.log and cleanup_deadline.log.

### R2-RUN-02. P1. An exceptional process_direct exit skips cleanup and falsely confirms a live subprocess stopped

Source anchor is nanobot/runs/executor.py:87. The except branch returns ExecutionResult with its default stop_confirmed=true before reaching cleanup at line 102.

Command is:

```pwsh
.\.venv-nanobot\Scripts\python.exe 'C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-round2-20261109\runs\probe_cleanup_round2.py' exception
```

The real AgentLoop starts a real waiting Python child with the exec tool. A supported reraise=True AgentHook raises RuntimeError in after_run. Actual output was state=failed, code=execution_error, stop_confirmed=true, effects_uncertain=false, isolation=false, with one owned process whose returncode was null. A fresh reproduction produced session_id=b82de45174a8.

Required output reaps the owned process before confirming stop. A failed reap must set stop_confirmed=false and effects_uncertain=true and isolate the instance.

The minimum repair puts resource cleanup around all process_direct exits, including exceptions and cancellation during cleanup, and preserves the typed failure. The existing unexpected-exception test only checks code/message, so its pass does not establish cleanup. Log is cleanup_exception.log.

### R2-RUN-03. P1. A transient result write failure permanently replaces completed success with cancellation

Source anchors are nanobot/runs/service.py:387, nanobot/runs/service.py:395, and nanobot/runs/service.py:283.

Command is:

```pwsh
.\.venv-nanobot\Scripts\python.exe 'C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-round2-20261109\runs\probe_runs_round2.py' result_persistence
```

The executor returns succeeded with text PERSIST_ME. The probe uses actual SQLite PRAGMA query_only=1 during result application, not a replacement store method. Once the task exits it restores write access and pumps the real service again.

Actual intermediate output was state=running, tracked=false. After storage recovery, output was state=cancelled, outcome=cancelled, text="", stop_confirmed=true, effects_uncertain=true, isolation=true, executor_calls=1. The completed result is gone.

Required output retries persistence of the original succeeded/PERSIST_ME observation under the original run identity. It must not invent cancellation or require executor rerun. The architecture section 11 requires retaining the observation and fetching or persisting the original result after a write fault.

The minimum repair retains a pending result observation until it commits and keeps it distinguishable from an executor with unknown outcome. Do not send this case to orphan-cancelling finalization. Existing tests/runs/test_run_service.py:421 explicitly blesses cancellation on this path. tests/runs/test_store_failures.py:53 never opens fake.gate before waiting for a failed completion, then calls _apply_result itself at line 78. It does not prove automatic recovery. Log is result_persistence.log.

### R2-RUN-04. P2. Temporary SQLite read failures become authoritative nonretryable run-not-found responses

Source anchors are nanobot/api/agent_runs.py:276, nanobot/api/agent_runs.py:286, and nanobot/api/agent_runs.py:316.

Command is:

```pwsh
.\.venv-nanobot\Scripts\python.exe 'C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-round2-20261109\runs\probe_runs_round2.py' http_storage
```

The run exists and was accepted. The probe configures a real SQLite authorizer to deny SELECT, then calls the real aiohttp routes with valid dedicated bearer credentials.

Actual status, result, and cancel responses were all HTTP 404 with code=run_not_found and retryable=false. The failure is a database read error, not an absent run. Coordinator reconciliation can therefore treat a recoverable control-store outage as lost accepted execution.

Required output is a structured retryable storage-unavailable 5xx while preserving the original key and pending observation. Only a successful store read that proves absence may return 404.

The minimum repair catches the typed absent-run error separately and lets StoreUnavailableError reach an explicit storage-fault mapping. Log is http_storage.log.

### R2-RUN-05. P2. Retention still deletes the event evidence for an unconfirmed interrupted run

Source anchor is nanobot/runs/store.py:659. The body and row cleanup queries use _UNCONFIRMED_GUARD, but the run_events DELETE has only an age predicate.

Command is:

```pwsh
.\.venv-nanobot\Scripts\python.exe 'C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-round2-20261109\runs\probe_runs_round2.py' retention
```

The real store creates a run, marks it running, recovers it as interrupted with effects_uncertain=true, and invokes cleanup at a synthetic time 100 days later.

Actual output was counts={result_bodies:0,events:3,runs:0}, row_kept=true, effects_uncertain=true, events_before=3, events_after=0.

Required output retains the unresolved run and its investigation events. Architecture section 8 excludes unresolved work from automatic evidence cleanup. The new source comment at store.py:625 also explicitly promises to retain events. The minimum repair joins event deletion to the owning run retention eligibility and preserves a correct cursor floor. tests/runs/test_stop_confirmation.py:138 checks bodies and rows, but omits events. S01 is only partially closed. Log is retention.log.

## Operator recovery appendix

The new RunStore.recover isolates the Nanobot instance persistently. RunService admission and queue start honor that guard. The only production occurrence of clear_isolation is nanobot/runs/store.py:254 itself. Repository-wide rg shows all its callers are tests, including test_stop_confirmation.py:109 and test_kill_recovery.py:118. The supported CLI has no call to it.

I ran `.\.venv-nanobot\Scripts\python.exe -m nanobot --help`. It listed update, onboard, trigger, serve, webui, agent, status, remote, gateway, channels, plugins, and provider. `.\.venv-nanobot\Scripts\python.exe -m nanobot runs --help` returned No such command 'runs'. The selected command name is only an illustration; source inspection establishes that no production CLI invokes the helper under another name either.

Thus F06's recovery isolation guard closes the old unsafe restart, but the documented local operator recovery is unavailable. Restart alone does not clear isolation. A local operator command should record actor, reason, time, original isolation, and remaining risk, without rewriting historic run outcomes. This appendix does not claim that calling an arbitrarily chosen missing command alone proves every CLI is absent.

## Old finding closure facts

The command `.\.venv-nanobot\Scripts\python.exe -m pytest tests/runs -q` passed with 62 passed in 30.38s.

Old probes were copied to probe_old_runs.py in this private directory. Only its empty-response next-run expectation was adjusted from succeeded to failed, because the original probe asserted the old bug. It still calls the current real AgentLoop and executor. Observed outputs are saved in old_closure_observed.json. The current module path printed by every old invocation was this repository's nanobot/runs/service.py.

- F04's original false-stop probe now reports stored_stop_confirmed=false and isolated=true. The original successful background-exec probe now reports succeeded, stop_confirmed=true, live_owned_processes=[], next_run_state=succeeded. These exact original cases close. The new resistant and exception cases above show the all-path cleanup contract remains open.
- F05's second-writer lock test passes. The killed-process recovery test passes and sequential owners can reopen after close. The source acquires the writer lock in RunStore.open before recovery. Serve calls open before app lifecycle startup. I found no verified new writer-lock exceptional-exit defect.
- F06's Nanobot restart test passes. An active record becomes interrupted/effects_uncertain and isolates; queued work does not start until the test explicitly calls store.clear_isolation. The separate operator recovery limitation is above.
- S01 keeps unresolved rows and bodies. The original retention probe prints deleted_unconfirmed_run=false with result_bodies=0 and runs=0. It still prints events=3, which is the surviving defect above.
- S02's real offline empty reply now yields state=failed, outcome=execution_error, stop_confirmed=true, live_owned_processes=[]. The fallback text is no longer treated as success. The next empty reply also fails.
- S05's malformed JSON yields HTTP 400 application/json with code=invalid_json. Duplicate keys yield HTTP 400 application/json with code=duplicate_key.
- S09's expired event cursor yields HTTP 410 with code=event_cursor_expired before preparing SSE.
- S10's cleaned result status now has result_available=false, while the result route returns 410.

No source or existing test edits were made. git status matched the initial dirty-file inventory after the probes.
