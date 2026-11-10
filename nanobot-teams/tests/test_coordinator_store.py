"""Coordinator store: admission transaction, quotas, idempotency, quarantine."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nanobot_teams.coordinator.models import Bot, DeliveryMode, new_task_id
from nanobot_teams.coordinator.store import (
    AdmissionError,
    CoordinatorStore,
    IdempotencyConflictError,
    utc_now,
)


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "coord.sqlite3")
    s.open()
    yield s
    s.close()


def make_bot(bot_id="research") -> Bot:
    return Bot(
        bot_id=bot_id,
        display_name=bot_id.title(),
        role="research",
        endpoint="http://127.0.0.1:8921",
        secret_env="TEAMS_BOT_RESEARCH_KEY",
        enabled=True,
    )


def admit(store, *, operation_key="key-1", workflow="wf-1", bot_id="research", principal="planner",
          text="do research", deadline_s=600.0, register=True):
    if register and store.get_bot(bot_id) is None:
        store.upsert_bot(make_bot(bot_id))
    bot = store.get_bot(bot_id)
    return store.admit_task(
        caller_principal=principal,
        project_id="proj-1",
        workflow_id=workflow,
        bot_id=bot_id,
        operation_key=operation_key,
        input_text=text,
        deadline_at=None if deadline_s is None else utc_now() + timedelta(seconds=deadline_s),
        delivery_mode=DeliveryMode.WORKBENCH_ONLY,
        bot=bot or make_bot(bot_id),
    )


def settle(store, task):
    store.update_after_submit(task.task_id, remote_run_id=f"run_{task.task_id[5:10]}",
                              remote_instance_id="inst_x")
    store.record_observation(task.task_id, revision=1, run_state="succeeded", stop_confirmed=True,
                             result_text="done")


def test_admit_creates_task_and_outbox_atomically(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    task, created = admit(store)
    assert created
    assert task.task_id.startswith("task_")
    assert task.dispatch_state.value == "pending"
    assert store.lookup_task("planner", "key-1").task_id == task.task_id
    entries = store.pending_dispatch(utc_now())
    assert [e.task_id for e in entries] == [task.task_id]


def test_same_key_same_semantics_returns_original(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    first, created = admit(store, deadline_s=None)
    again, created_again = admit(store, deadline_s=None)
    assert created_again is False
    assert again.task_id == first.task_id
    entries = store.pending_dispatch(utc_now())
    assert len(entries) == 1


def test_same_key_different_semantics_conflicts(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    admit(store, text="original")
    with pytest.raises(IdempotencyConflictError):
        admit(store, text="changed semantics")


def test_admission_requires_known_project_and_bot(store):
    with pytest.raises(AdmissionError) as exc:
        admit(store)
    assert exc.value.code == "project_not_found"
    store.create_project("proj-1", "Main")
    # The bot passed to admission must actually be the registered one.
    store.upsert_bot(make_bot("research"))
    with pytest.raises(AdmissionError) as exc:
        store.admit_task(
            caller_principal="planner", project_id="proj-1", workflow_id="wf", bot_id="research",
            operation_key="mismatch", input_text="x", deadline_at=None,
            delivery_mode=DeliveryMode.WORKBENCH_ONLY, bot=make_bot("other"),
        )
    assert exc.value.code == "bot_unavailable"


def test_bot_queue_quota_blocks_admission(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    for i in range(16):
        admit(store, operation_key=f"k{i}", workflow=f"wf-{i}")
    with pytest.raises(AdmissionError) as exc:
        admit(store, operation_key="overflow", workflow="wf-overflow")
    assert exc.value.code == "bot_queue_limit"


def test_workflow_task_quota_blocks_admission(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    for i in range(20):
        task, _ = admit(store, operation_key=f"k{i}")
        settle(store, task)  # settle so the per-bot queue does not trigger first
    with pytest.raises(AdmissionError) as exc:
        admit(store, operation_key="overflow")
    assert exc.value.code == "workflow_task_limit"


def test_principal_and_global_open_quotas(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot("research"))
    store.upsert_bot(make_bot("writer"))
    # Distinct workflows, distinct bots, one caller: principal limit 32.
    bot_ids = ["research", "writer"] * 16
    for i in range(32):
        admit(store, operation_key=f"k{i}", workflow=f"wf-{i}", bot_id=bot_ids[i])
    with pytest.raises(AdmissionError) as exc:
        admit(store, operation_key="overflow", workflow="wf-x", bot_id="research")
    assert exc.value.code == "principal_open_limit"


def test_quarantined_instance_rejects_new_tasks_and_resolution_restores(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    store.bind_instance("research", "inst_aaa")
    store.quarantine_instance("inst_aaa", "identity changed mid-flight")
    with pytest.raises(AdmissionError) as exc:
        admit(store, operation_key="blocked")
    assert exc.value.code == "bot_quarantined"
    store.resolve_quarantine("inst_aaa", "operator", "inspected")
    task, created = admit(store, operation_key="blocked")
    assert created


def test_alias_shares_isolation_through_instance_identity(store):
    store.create_project("proj-1", "Main")
    bot = make_bot()
    store.upsert_bot(bot)
    store.bind_instance("research", "inst_shared")
    # The same bot reached via a different endpoint alias is the same instance.
    alias = Bot(
        bot_id="research", display_name="Research", role="research",
        endpoint="http://127.0.0.1:8922", secret_env="TEAMS_BOT_RESEARCH_KEY",
    )
    store.upsert_bot(alias)  # instance_id stays bound to inst_shared
    assert store.get_bot("research").instance_id == "inst_shared"
    store.quarantine_instance("inst_shared", "unknown outcome")
    with pytest.raises(AdmissionError) as exc:
        admit(store, operation_key="alias-blocked")
    assert exc.value.code == "bot_quarantined"


def test_observation_applies_only_newer_revisions(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    task, _ = admit(store)
    store.update_after_submit(task.task_id, remote_run_id="run_1", remote_instance_id="inst_aaa")
    observed = store.record_observation(
        task.task_id, revision=2, run_state="running",
    )
    assert observed.remote_revision == 2
    stale = store.record_observation(
        task.task_id, revision=1, run_state="succeeded", result_text="late stale"
    )
    assert stale.remote_revision == 2
    assert stale.run_state == "running"
    terminal = store.record_observation(
        task.task_id, revision=3, run_state="succeeded", stop_confirmed=True,
        outcome_code="succeeded", result_text="final report",
    )
    assert terminal.run_state == "succeeded"
    assert terminal.result_text == "final report"
    assert terminal.result_available is True
    assert terminal.finished_at is not None


def test_cancel_request_states(store):
    store.create_project("proj-1", "Main")
    store.upsert_bot(make_bot())
    task, _ = admit(store)
    pending_cancel = store.request_cancel(task.task_id)
    # Not yet accepted remotely: cancelling before dispatch is confirmed locally.
    assert pending_cancel.cancel_state.value in ("confirmed", "requested")
    store.update_after_submit(task.task_id, remote_run_id="run_1", remote_instance_id="i")
    task2, _ = admit(store, operation_key="k2")
    store.update_after_submit(task2.task_id, remote_run_id="run_2", remote_instance_id="i")
    requested = store.request_cancel(task2.task_id)
    assert requested.cancel_state.value == "requested"
    again = store.request_cancel(task2.task_id)
    assert again.cancel_state.value == "requested"
    # Terminal with stop confirmed settles the cancel request.
    store.record_observation(
        task2.task_id, revision=2, run_state="cancelled", stop_confirmed=True,
    )
    settled = store.get_task(task2.task_id)
    assert settled.cancel_state.value == "confirmed"


def test_submit_started_is_the_cancel_send_fact(store):
    """N01: only the persisted send fact makes a cancel unconfirmed."""
    store.create_project("proj-1", "Main")
    task, _ = admit(store)
    entry = store.dispatch_entry_for_task(task.task_id)
    assert entry is not None and entry.submit_started_at is None
    store.mark_submit_started(entry.entry_id)
    started = store.dispatch_entry_for_task(task.task_id)
    assert started is not None and started.submit_started_at is not None
    first_stamp = started.submit_started_at
    store.mark_submit_started(entry.entry_id)
    again = store.dispatch_entry_for_task(task.task_id)
    assert again is not None and again.submit_started_at == first_stamp, (
        "the send fact must never be rewritten"
    )
    cancelled = store.request_cancel(task.task_id)
    assert cancelled is not None
    assert cancelled.cancel_state.value == "unconfirmed"
    assert cancelled.stop_confirmed is False
    assert cancelled.run_state == "unobserved"


def test_open_migrates_dispatch_outbox_send_fact(tmp_path):
    """N01: an existing database gains submit_started_at without losing rows."""
    import sqlite3

    path = tmp_path / "old-coord.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE dispatch_outbox ("
        "entry_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL,"
        " state TEXT NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " next_attempt_at TEXT NOT NULL,"
        " last_error TEXT NOT NULL DEFAULT ''"
        ");"
    )
    conn.execute(
        "INSERT INTO dispatch_outbox(task_id, state, next_attempt_at) VALUES (?,?,?)",
        ("task_legacy", "pending", "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO dispatch_outbox(task_id, state, attempts, next_attempt_at) "
        "VALUES (?,?,?,?)",
        ("task_in_doubt", "pending", 2, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    store = CoordinatorStore(path)
    store.open()
    try:
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(dispatch_outbox)")
        }
        assert "submit_started_at" in columns
        entries = {entry.task_id: entry for entry in store.pending_dispatch(utc_now())}
        assert entries["task_legacy"].submit_started_at is None
        # An old retried row keeps its "may have been sent" meaning.
        assert entries["task_in_doubt"].submit_started_at is not None
        store.mark_submit_started(entries["task_legacy"].entry_id)
        migrated = store.dispatch_entry_for_task("task_legacy")
        assert migrated is not None and migrated.submit_started_at is not None
    finally:
        store.close()


def test_new_task_id_shape():
    assert new_task_id().startswith("task_")
