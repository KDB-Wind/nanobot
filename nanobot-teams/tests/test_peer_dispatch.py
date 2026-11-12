"""P4 store layer: peer edges and conversation budgets on admission.

Covers the I3/I6 contracts the HTTP route rides on: explicit edges with
default deny, hop bound, recipient-reentry cycle rejection, per-pair rate
window, and the idempotency-first ordering for repeated operation keys.
"""

from __future__ import annotations

import uuid

import pytest

from nanobot_teams.coordinator.models import Bot, DeliveryMode
from nanobot_teams.coordinator.store import (
    PEER_MAX_HOPS,
    PEER_PAIR_WINDOW_MESSAGES,
    AdmissionError,
    CoordinatorStore,
)

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture
def store(tmp_path):
    s = CoordinatorStore(tmp_path / "coord.sqlite3")
    s.open()
    yield s
    s.close()


def bot(bot_id: str) -> Bot:
    index = {"research": 1, "writer": 2, "analyst": 3}.get(bot_id, 9)
    return Bot(
        bot_id=bot_id,
        display_name=bot_id.title(),
        role=bot_id,
        endpoint=f"http://127.0.0.1:910{index}",
        secret_env=f"ENV_{bot_id.upper()}",
        enabled=True,
        capabilities=("execute",),
    )


def seed_three(store: CoordinatorStore) -> None:
    store.create_project("peer", "Peer")
    for bot_id in ("research", "writer", "analyst"):
        store.upsert_bot(bot(bot_id))


BIG_QUEUE = {
    "parallel_bots": 64, "workflow_tasks": 200, "principal_open": 64,
    "global_open": 512, "bot_queue": 100, "workflow_result_bytes": 64 * 1024 * 1024,
}


def admit_peer(store: CoordinatorStore, sender: str, recipient: str, *,
               operation_key: str | None = None, conversation_root: str | None = None,
               quotas: dict | None = None):
    return store.admit_task(
        caller_principal=f"bot:{sender}",
        project_id="peer",
        workflow_id=conversation_root or str(uuid.uuid4()),
        bot_id=recipient,
        operation_key=operation_key or str(uuid.uuid4()),
        input_text="do the thing",
        deadline_at=None,
        delivery_mode=DeliveryMode.WORKBENCH_ONLY,
        bot=bot(recipient),
        kind="peer",
        conversation_root=conversation_root or str(uuid.uuid4()),
        sender_bot_id=sender,
        quotas=quotas,
    )


def test_peer_edges_require_existing_bots_and_roundtrip(store):
    seed_three(store)
    with pytest.raises(AdmissionError) as missing:
        store.upsert_peer_edge("research", "ghost")
    assert missing.value.code == "bot_not_found"
    with pytest.raises(AdmissionError) as self_edge:
        store.upsert_peer_edge("research", "research")
    assert self_edge.value.code == "invalid_edge"
    store.upsert_peer_edge("research", "writer")
    store.upsert_peer_edge("research", "writer")  # idempotent
    assert store.list_peer_edges() == [("research", "writer")]
    assert store.has_peer_edge("research", "writer")
    assert not store.has_peer_edge("writer", "analyst")
    assert store.delete_peer_edge("research", "writer")
    assert not store.has_peer_edge("research", "writer")
    assert not store.delete_peer_edge("research", "writer")


def test_peer_task_persists_envelope(store):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    root = str(uuid.uuid4())
    store.admit_task(
        caller_principal="planner", project_id="peer", workflow_id=root,
        bot_id="analyst", operation_key=str(uuid.uuid4()), input_text="first",
        deadline_at=None, delivery_mode=DeliveryMode.WORKBENCH_ONLY,
        bot=store.get_bot("analyst"), conversation_root=root, hop=0,
    )
    task, created = admit_peer(store, "research", "writer",
                               conversation_root=root)
    assert created
    assert task.kind == "peer"
    assert task.conversation_root == root
    # The hop is derived from the conversation, not from the client.
    assert task.hop == 1
    assert task.caller_principal == "bot:research"
    assert task.bot_id == "writer"


def test_peer_send_without_edge_is_denied(store):
    seed_three(store)
    with pytest.raises(AdmissionError) as exc:
        admit_peer(store, "writer", "analyst")
    assert exc.value.code == "unauthorized_edge"


def test_hop_bound_rejects_the_fifth_hop(store):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    root = str(uuid.uuid4())
    for _ in range(PEER_MAX_HOPS):
        store.admit_task(
            caller_principal="planner", project_id="peer", workflow_id=root,
            bot_id="analyst", operation_key=str(uuid.uuid4()), input_text="x",
            deadline_at=None, delivery_mode=DeliveryMode.WORKBENCH_ONLY,
            bot=store.get_bot("analyst"), conversation_root=root, hop=0,
        )
    with pytest.raises(AdmissionError) as exc:
        admit_peer(store, "research", "writer", conversation_root=root)
    assert exc.value.code == "hop_limit_exceeded"


def test_recipient_reentry_is_a_cycle(store):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    store.upsert_peer_edge("writer", "research")
    root = str(uuid.uuid4())
    # The real shape: the planner's root delegation makes research a
    # participant before any peer hop exists.
    store.admit_task(
        caller_principal="planner", project_id="peer", workflow_id=root,
        bot_id="research", operation_key=str(uuid.uuid4()), input_text="root",
        deadline_at=None, delivery_mode=DeliveryMode.WORKBENCH_ONLY,
        bot=bot("research"), conversation_root=root, hop=0,
    )
    admit_peer(store, "research", "writer", conversation_root=root)
    with pytest.raises(AdmissionError) as exc:
        admit_peer(store, "writer", "research", conversation_root=root)
    assert exc.value.code == "conversation_cycle"


def test_relay_chain_is_not_a_cycle(store):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    store.upsert_peer_edge("writer", "analyst")
    root = str(uuid.uuid4())
    admit_peer(store, "research", "writer", conversation_root=root)
    task, created = admit_peer(store, "writer", "analyst",
                               conversation_root=root)
    assert created and task.hop == 1


def test_pair_rate_window(store, monkeypatch):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    for i in range(PEER_PAIR_WINDOW_MESSAGES):
        admit_peer(store, "research", "writer", quotas=BIG_QUEUE)
    with pytest.raises(AdmissionError) as exc:
        admit_peer(store, "research", "writer", quotas=BIG_QUEUE)
    assert exc.value.code == "pair_rate_limited"


def test_peer_send_without_root_is_invalid(store):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    with pytest.raises(AdmissionError):
        store.admit_task(
            caller_principal="bot:research",
            project_id="peer",
            workflow_id=str(uuid.uuid4()),
            bot_id="writer",
            operation_key=str(uuid.uuid4()),
            input_text="x",
            deadline_at=None,
            delivery_mode=DeliveryMode.WORKBENCH_ONLY,
            bot=bot("writer"),
            kind="peer",
            conversation_root="",
            sender_bot_id="research",
        )


def test_same_operation_key_returns_the_original_peer_task(store):
    seed_three(store)
    store.upsert_peer_edge("research", "writer")
    key = str(uuid.uuid4())
    root = str(uuid.uuid4())
    first, created_first = admit_peer(store, "research", "writer",
                                      operation_key=key, conversation_root=root)
    second, created_second = admit_peer(store, "research", "writer",
                                        operation_key=key, conversation_root=root)
    assert created_first and not created_second
    assert first.task_id == second.task_id


def test_plain_delegation_unchanged_without_envelope(store):
    seed_three(store)
    task, created = store.admit_task(
        caller_principal="planner",
        project_id="peer",
        workflow_id=str(uuid.uuid4()),
        bot_id="research",
        operation_key=str(uuid.uuid4()),
        input_text="hello",
        deadline_at=None,
        delivery_mode=DeliveryMode.WORKBENCH_ONLY,
        bot=bot("research"),
    )
    assert created
    assert task.kind == "user_delegation"
    assert task.conversation_root == ""
    assert task.hop == 0


def test_legacy_tasks_table_upgrades_with_envelope_columns(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL, caller_principal TEXT NOT NULL,
            bot_id TEXT NOT NULL, operation_key TEXT NOT NULL,
            request_digest TEXT NOT NULL, input_text TEXT NOT NULL,
            deadline_at TEXT NOT NULL, delivery_mode TEXT NOT NULL,
            dispatch_state TEXT NOT NULL, remote_run_id TEXT,
            remote_instance_id TEXT, remote_revision INTEGER NOT NULL DEFAULT 0,
            run_state TEXT NOT NULL DEFAULT 'unobserved',
            stop_confirmed INTEGER NOT NULL DEFAULT 0,
            effects_uncertain INTEGER NOT NULL DEFAULT 0,
            outcome_code TEXT, outcome_message TEXT NOT NULL DEFAULT '',
            result_text TEXT NOT NULL DEFAULT '',
            result_truncated INTEGER NOT NULL DEFAULT 0,
            result_available INTEGER NOT NULL DEFAULT 0,
            result_fetched INTEGER NOT NULL DEFAULT 0,
            cancel_state TEXT NOT NULL DEFAULT 'none',
            delivery_state TEXT NOT NULL DEFAULT 'not_ready',
            created_at TEXT NOT NULL, finished_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            reply_capability TEXT NOT NULL DEFAULT '',
            migrated_usage INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO tasks (task_id, project_id, workflow_id, caller_principal,
            bot_id, operation_key, request_digest, input_text, deadline_at,
            delivery_mode, dispatch_state, remote_revision, run_state, created_at)
            VALUES ('t1', 'p', 'w', 'planner', 'research', 'k', 'd', 'x',
            '2026-01-01T00:00:00+00:00', 'workbench_only', 'accepted', 1,
            'succeeded', '2026-01-01T00:00:00+00:00');
        """
    )
    raw.commit()
    raw.close()

    store = CoordinatorStore(db)
    store.open()
    try:
        task = store.get_task("t1")
        assert task is not None
        assert task.kind == "user_delegation"
        assert task.conversation_root == ""
        assert task.hop == 0
    finally:
        store.close()
