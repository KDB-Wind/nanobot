# Run-service audit evidence

Intent. Audit the current Nanobot Teams v0.5 implementation against the approved async workbench contract. This is an audit, with no fixes. The review used the inherited Codex model because the skill's Grok and Claude families are unavailable. This is same-family review.

The Prove It Works principle changed the review into rerunnable probes against current modules. The script reports the loaded service path. It uses the repository .venv-nanobot interpreter, temporary instance homes, an offline provider, local aiohttp test servers, and harmless local child processes. Every child process is stopped in cleanup. No real model calls or remote writes occur. No implementation or repository tests were edited.

Run from C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp.

Base command for every probe below.

    .\.venv-nanobot\Scripts\python.exe C:\Users\KDB\AppData\Local\Temp\nanobot-teams-review-20261109\runs\probe_runs.py MODE

Replace MODE with the named probe, or use all. The saved complete output is probe_output.txt beside the script. The parent independently reran all and confirmed the results before this report.

## 1. P1. A second writer can interrupt a live run and execute another

Location. nanobot/runs/store.py:131, RunStore.open. The normal CLI at nanobot/cli/commands.py:421 uses this store without a surrounding lifetime writer lock.

Command. Base command with MODE two-writers.

Actual output.

    {"probe":"two-writers","second_open_accepted":true,"same_instance":true,"first_process_still_alive":true,"first_run_interrupted_by_second":true,"second_executed":true}

The first real Python process is executing a run. The second process opens its same database and invokes RunService.start. That second service treats the first process's still-running execution as a crash, marks it interrupted, and executes a new task under the same instance identity. SQLite BEGIN IMMEDIATE protects individual writes, not ownership of the runtime.

Required. The second writer must fail startup. Live work must not be reclassified as interrupted or share an instance slot with another runtime.

Minimum direction. Acquire an OS-backed nonblocking lifetime lock for the instance control directory before opening or recovering the runs database. Hold it until the executor and store have closed.

## 2. P1. Restart recovery loses isolation and immediately executes queued work

Location. nanobot/runs/store.py:466, particularly the interrupted update at line 479. RunService.start invokes this at nanobot/runs/service.py:106 and then starts the worker.

Command. Base command with MODE recovery.

Actual output.

    {"probe":"recovery","crashed_state":"interrupted","crashed_effects_uncertain":true,"isolation":[false,""],"queued_executed":true}

The probe persists an active start marker plus a second queued run, closes the old store, then opens and starts a new service. Recovery marks the active record interrupted with uncertain effects. It leaves instance isolation false, so the queued run executes immediately. This failure persists even after the multiple-writer defect is fixed.

Required. Architecture section 6 requires interrupted active runs to preserve isolation. Unknown prior effects must block new execution until the operator inspects and clears quarantine locally.

Minimum direction. Commit interrupted state and instance isolation in the same recovery transaction before starting the worker.

## 3. P1. Completion claims stop confirmation while owned subprocesses remain alive

Locations. nanobot/runs/executor.py:87 returns after the main turn without reclaiming owned work. nanobot/runs/service.py:474 unconditionally persists stop_confirmed=True and ignores ExecutionResult.stop_confirmed.

Commands. Base command with MODE background. Base command with MODE false-stop.

Actual output from the real AgentLoop and real exec tool.

    {"next_run_state":"succeeded","cancelled_original_state":"succeeded","probe":"background","state":"succeeded","stop_confirmed":true,"effects_uncertain":false,"outcome":"succeeded","text":"Done","live_owned_processes":[{"session_id":"267f1bd73d00","returncode":null}],"provider_calls":3}

The offline provider asks the real exec tool to start a Python child that only sleeps, using yield_time_ms=0. The provider then returns a final answer. The run is succeeded and confirms stop while the owned child still runs. Another run executes. Cancelling the original terminal run is a no-op and does not stop the child. The test explicitly cleans it up through AgentLoop.aclose afterward.

Actual output for a typed executor that says stop is unconfirmed.

    {"probe":"false-stop","executor_stop_confirmed":false,"stored_state":"cancelled","stored_stop_confirmed":true,"isolated":false}

Required. Architecture section 6 permits stop confirmation only after all run-owned background tools and subprocesses have been reclaimed. Unknown stop must retain the slot and quarantine the instance. A completed main coroutine does not confirm process cleanup.

Minimum direction. Settle owned work on every completion path, including normal and exceptional returns, before returning a typed completion. RunService must respect typed stop confirmation and preserve active or isolated state when cleanup cannot be confirmed. These two probes are one stop-confirmation root, not separate findings.

## 4. P2. Retention deletes evidence for an unconfirmed interrupted run

Locations. nanobot/runs/store.py:553 clears bodies by finished_at alone. nanobot/runs/store.py:583 selects rows for metadata deletion by finished_at alone.

Command. Base command with MODE retention.

Actual output.

    {"probe":"retention-unconfirmed","deleted_unconfirmed_run":true,"counts":{"result_bodies":1,"events":3,"runs":1}}

The probe creates an active run, recovers it to interrupted with stop_confirmed=False and effects_uncertain=True, and invokes cleanup with a simulated time 100 days later. The input, events, and run record are removed despite unresolved stop.

Required. Architecture section 8 explicitly excludes unconfirmed stops and isolation evidence from automatic cleanup. The permanent idempotency tombstone cannot replace the full unresolved execution evidence.

Minimum direction. Make body, event, and metadata retention eligibility depend on resolved stop and isolation state, rather than the existence of a terminal finished_at alone.

## 5. P2. An empty model result is reported as execution success

Location. nanobot/runs/executor.py:17 maps empty_final_response to OutcomeCode.SUCCEEDED.

Command. Base command with MODE empty.

Actual output, abbreviated to stable fields.

    {"probe":"empty","state":"succeeded","stop_confirmed":true,"effects_uncertain":false,"outcome":"succeeded","text":"I completed the tool steps but couldn't produce a final answer. Please try again or narrow the task."}

This goes through the real AgentLoop with an offline provider returning empty content. AgentRunner records stop_reason=empty_final_response and an error. The adapter replaces that typed failure with succeeded and suppresses the error message.

Required. Architecture sections 4 and 6 require typed completed outcomes rather than false success with fallback text. An error-bearing empty final response must not be recorded as a successful task result.

Minimum direction. Map empty_final_response to a typed failure and retain its error. Only the typed successful completion reason should produce succeeded.

## 6. P2. JSON rejection crashes into an unstructured HTTP 500

Location. nanobot/api/agent_runs.py:112 uses except protocol_error, but protocol_error is a response-construction function, not an exception class.

Command. Base command with MODE http.

Actual output.

    {"probe":"invalid-json","status":500,"content_type":"text/plain","body":"500 Internal Server Error\n\nServer got itself in trouble"}
    {"probe":"duplicate-key","status":500,"content_type":"text/plain","body":"500 Internal Server Error\n\nServer got itself in trouble"}

The traceback is TypeError: catching classes that do not inherit from BaseException is not allowed. Bodies were { and {"protocol":1,"protocol":1}.

Required. Architecture section 5 requires bounded validation errors in the {code,message,retryable,request_id} envelope. Invalid JSON must return 400 invalid_json. Duplicate keys must return 400 duplicate_key. An HTTP 500 incorrectly signals an unknown/retryable server outcome to the Coordinator.

Minimum direction. Catch and preserve the actual web.HTTPException class before translating JSON decoding exceptions. Do not put a function in an except clause.

## 7. P2. An expired SSE cursor commits HTTP 200 instead of the required 410

Location. nanobot/api/agent_runs.py:334 prepares the response before service.events at line 339 validates cursor retention.

Command. Base command with MODE http.

Actual output.

    {"probe":"expired-sse","status":200,"body":"event: cursor_expired\ndata: {\"code\":\"event_cursor_expired\"}\n\n"}

The probe emits events, backdates them by 31 days, applies real cleanup, and requests after=0. The handler has already committed status 200 when it discovers cursor expiration.

Required. Architecture section 5 requires HTTP 410 event_cursor_expired so clients can resnapshot. A successful SSE response carrying a different unversioned error event does not satisfy that recovery contract.

Minimum direction. Validate cursor retention before preparing the stream, returning the protocol 410 envelope for the initial expired cursor.

## 8. P3. Status advertises a result that retention has already removed

Location. nanobot/api/agent_runs.py:273 computes result_available from run.terminal alone. The same computation appears in submit, lookup, and cancel responses.

Command. Base command with MODE http.

Actual output.

    {"probe":"cleaned-result","status_result_available":true,"result_http_status":410}

The probe finishes a run, backdates its completion by 15 days, applies cleanup, then fetches its status and result. Status advertises result_available=True while the same authenticated client's result request returns 410.

Required. The public status contract says whether the result is readable. Cleaned results must advertise false.

Minimum direction. Use terminal state together with result_cleaned_at is None for availability in every status response.

No speculative or unrun defects are included. The false-stop and real owned-child demonstrations are grouped under one finding. There were no runtime-disproved findings to retain. No changes were applied.
