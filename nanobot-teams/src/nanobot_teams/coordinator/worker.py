"""Dispatch and observation worker: committed outbox only, same key and body on retry."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from datetime import timedelta
from typing import Any

from .client import BotRunClient, BotUnreachableError
from .models import TERMINAL_RUN_STATES, DeliveryState
from .store import CoordinatorStore, utc_now

log = logging.getLogger("nanobot_teams.coordinator.worker")

BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)
MAX_PARALLEL_BOTS = 4
QUARANTINE_RECHECK_S = 30.0


class MissingSecretError(Exception):
    """The bot's service environment does not carry its secret."""


class ClientFactory:
    """Build one client per bot endpoint; keys come from the live environment."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = secrets if secrets is not None else dict(os.environ)
        self._clients: dict[str, BotRunClient] = {}

    def secret_for(self, secret_env: str) -> str:
        value = self._secrets.get(secret_env, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise MissingSecretError(f"{secret_env} is not set in the coordinator environment")

    def for_bot(self, endpoint: str, secret_env: str) -> BotRunClient:
        key = self.secret_for(secret_env)
        client = self._clients.get((endpoint, key))
        if client is None:
            client = BotRunClient(endpoint, key)
            self._clients[(endpoint, key)] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


class DispatchWorker:
    def __init__(
        self,
        store: CoordinatorStore,
        clients: ClientFactory,
        planner: Any | None = None,
    ) -> None:
        self._store = store
        self._clients = clients
        self._planner = planner
        self._running = False

    async def run_loop(self, *, poll_s: float = 0.5) -> None:
        self._running = True
        while self._running:
            try:
                progressed = await self.pump_once()
            except Exception:
                log.exception("coordinator worker iteration failed")
                progressed = False
            if not progressed:
                await asyncio.sleep(poll_s)

    def stop(self) -> None:
        self._running = False

    async def pump_once(self) -> bool:
        progressed = False
        progressed |= await self._dispatch_due()
        progressed |= await self._observe_accepted()
        progressed |= await self._fetch_missing_results()
        progressed |= await self._deliver_results()
        return progressed

    # ---- dispatch ----

    async def _dispatch_due(self) -> bool:
        now = utc_now()
        entries = self._store.pending_dispatch(now, limit=16)
        if not entries:
            return False
        progressed = False
        # Bound concurrent dispatch to distinct bots (default 4).
        by_bot: dict[str, list] = {}
        for entry in entries:
            task = self._store.get_task(entry.task_id)
            if task is None:
                continue
            by_bot.setdefault(task.bot_id, []).append((entry, task))
        chosen = list(by_bot.items())[:MAX_PARALLEL_BOTS]
        results = await asyncio.gather(
            *(self._dispatch_bot_tasks(tasks) for _bot, tasks in chosen)
        )
        progressed = any(results)
        return progressed

    async def _dispatch_bot_tasks(self, tasks: list) -> bool:
        progressed = False
        for entry, task in tasks:
            progressed |= await self._dispatch_one(entry, task)
        return progressed

    async def _dispatch_one(self, entry, task) -> bool:
        bot = self._store.get_bot(task.bot_id)
        if bot is None:
            self._store.mark_indeterminate(task.task_id, "Bot vanished from the catalog.")
            return True
        try:
            client = self._clients.for_bot(bot.endpoint, bot.secret_env)
        except MissingSecretError as exc:
            # Missing credentials are deterministic: stop auto-dispatch.
            self._store.mark_indeterminate(
                task.task_id, f"Coordinator secret missing: {exc}"
            )
            return True
        # Isolation is re-checked at dispatch time, not only at admission: a
        # task admitted before a quarantine must not create remote work now.
        if bot.instance_id and bot.instance_id in self._store.active_quarantines():
            self._store.defer_dispatch(
                entry.entry_id,
                utc_now() + timedelta(seconds=QUARANTINE_RECHECK_S),
                "bot instance quarantined; dispatch deferred",
            )
            return True
        # An unconfirmed cancel on a never-accepted task means a submit may
        # have been lost in flight: reconcile by the original key first, and
        # never create a new run after a cancel request.
        if (
            task.cancel_state.value == "unconfirmed"
            and task.dispatch_state.value == "pending"
            and task.remote_run_id is None
        ):
            await self._reconcile_cancelled_pending(client, task, entry)
            return True
        if task.cancel_state.value != "none" and task.dispatch_state.value == "pending":
            # Cancel known-confirmed and outbox closed: nothing to dispatch.
            return False
        now = utc_now()
        if task.deadline_at <= now and entry.attempts == 0:
            # Never sent and already past the business deadline: no new dispatch.
            self._store.expire_task_before_dispatch(
                task.task_id, "deadline passed before first dispatch"
            )
            return True
        body = {
            "protocol": 1,
            "task_id": task.task_id,
            "workflow_id": task.workflow_id,
            "input_text": task.input_text,
            "deadline_at": task.deadline_at.isoformat(),
        }
        if bot.instance_id:
            # Verify the endpoint identity before creating any run on it.
            try:
                caps = await client.capabilities()
            except BotUnreachableError as exc:
                await self._reconcile_unknown(client, task, entry, str(exc))
                return True
            if caps.status != 200 or not isinstance(caps.payload, dict):
                await self._reconcile_unknown(
                    client, task, entry, f"capabilities returned {caps.status}"
                )
                return True
            seen_instance = str(caps.payload.get("instance_id", ""))
            if seen_instance and seen_instance != bot.instance_id:
                await self._indeterminate_identity(task, bot.instance_id, seen_instance)
                return True
        try:
            response = await client.submit(operation_key=task.operation_key, protocol_body=body)
        except BotUnreachableError as exc:
            # Unknown delivery: retry the same key and body with backoff; also reconcile.
            await self._reconcile_unknown(client, task, entry, str(exc))
            return True
        if response.status in (200, 202):
            payload = response.payload or {}
            instance = str(payload.get("instance_id", ""))
            run_id = str(payload.get("run_id", ""))
            if not run_id:
                await self._reconcile_unknown(
                    client, task, entry, "accepted response missing run_id"
                )
                return True
            if instance and bot.instance_id and instance != bot.instance_id:
                await self._indeterminate_identity(task, bot.instance_id or "", instance)
                return True
            if instance:
                self._store.bind_instance(bot.bot_id, instance)
            self._store.update_after_submit(
                task.task_id, remote_run_id=run_id, remote_instance_id=instance
            )
            return True
        if response.status == 409 and (response.payload or {}).get("code") in (
            "idempotency_conflict",
            "operation_key_retired",
        ):
            # The remote disagrees about this key: its storage state is not the
            # coordinator's, so nothing new may run on that instance until an
            # operator reconciles.
            self._store.mark_indeterminate(
                task.task_id,
                f"Remote rejected the operation key: {(response.payload or {}).get('code')}",
                quarantine_instance=bot.instance_id,
            )
            return True
        if response.status == 410:
            self._store.mark_indeterminate(
                task.task_id,
                "Remote storage lost the key.",
                quarantine_instance=bot.instance_id,
            )
            return True
        # 4xx/5xx: retryable classification is deferred; unknown delivery for 5xx.
        if response.status >= 500:
            await self._reconcile_unknown(
                client, task, entry, f"remote {response.status}"
            )
            return True
        # Deterministic client error (401/403/400): no retry with this config.
        self._store.mark_dispatch_failure(
            entry.entry_id,
            error=f"remote {response.status}: {(response.payload or {}).get('code', '')}",
            retry_at=utc_now() + timedelta(days=3650),
        )
        self._store.mark_indeterminate(
            task.task_id,
            f"Remote refused the submission with {response.status}; operator must fix "
            "credentials or protocol.",
        )
        return True

    async def _reconcile_unknown(
        self, client: BotRunClient, task, entry, reason: str
    ) -> None:
        """Response lost or transport unknown: reconcile by original key before retrying."""
        try:
            lookup = await client.lookup(task.operation_key)
        except BotUnreachableError:
            lookup = None
        if lookup is not None and lookup.status == 200 and (lookup.payload or {}).get("run_id"):
            payload = lookup.payload or {}
            instance = str(payload.get("instance_id", ""))
            bot = self._store.get_bot(task.bot_id)
            if bot is not None and bot.instance_id and instance and instance != bot.instance_id:
                await self._indeterminate_identity(task, bot.instance_id, instance)
                return
            self._store.update_after_submit(
                task.task_id,
                remote_run_id=str(payload["run_id"]),
                remote_instance_id=instance,
            )
            return
        delay = BACKOFF_SECONDS[min(entry.attempts, len(BACKOFF_SECONDS) - 1)]
        jitter = random.uniform(0, delay * 0.25)
        self._store.bump_dispatch_attempt(
            entry.entry_id, utc_now() + timedelta(seconds=delay + jitter), reason
        )

    async def _reconcile_cancelled_pending(
        self, client: BotRunClient, task, entry
    ) -> None:
        """Settle an unconfirmed cancel on a never-accepted task.

        The lookup proves whether the lost submit created a run: found means the
        cancel becomes a real remote cancel; not found means nothing was ever
        created and the task closes as cancelled-before-dispatch.
        """
        try:
            lookup = await client.lookup(task.operation_key)
        except BotUnreachableError:
            lookup = None
        if lookup is not None and lookup.status == 200 and (lookup.payload or {}).get("run_id"):
            payload = lookup.payload or {}
            run_id = str(payload["run_id"])
            instance = str(payload.get("instance_id", ""))
            bot = self._store.get_bot(task.bot_id)
            if bot is not None and bot.instance_id and instance and instance != bot.instance_id:
                await self._indeterminate_identity(task, bot.instance_id, instance)
                return
            self._store.update_after_submit(
                task.task_id, remote_run_id=run_id, remote_instance_id=instance
            )
            try:
                await client.cancel(run_id)
            except BotUnreachableError:
                pass  # the accepted task's cancel path keeps driving the stop
            return
        if lookup is not None and lookup.status == 404:
            # The remote definitively never saw this key: no work exists.
            self._store.close_cancelled_unknown(
                task.task_id, "reconciled: remote never created this run"
            )
            return
        # Unknown transport: retry the same key on the normal backoff.
        delay = BACKOFF_SECONDS[min(entry.attempts, len(BACKOFF_SECONDS) - 1)]
        jitter = random.uniform(0, delay * 0.25)
        self._store.bump_dispatch_attempt(
            entry.entry_id,
            utc_now() + timedelta(seconds=delay + jitter),
            "cancel reconciliation pending",
        )

    async def _indeterminate_identity(self, task, expected: str, actual: str) -> None:
        self._store.quarantine_instance(
            actual, f"endpoint identity changed from {expected} to {actual}"
        )
        self._store.mark_indeterminate(
            task.task_id, f"Instance identity changed from {expected} to {actual}."
        )

    # ---- delivery ----

    async def _poll_receipt(self, task, client=None, inbox_key=None, delivery_id=None):
        """Observe the planner receipt until it reaches a stable state."""
        import hashlib

        import httpx

        planner = self._planner
        if planner is None:
            return
        delivery_id = delivery_id or "del_" + hashlib.sha256(
            f"{task.task_id}:{task.remote_revision}".encode("utf-8")
        ).hexdigest()[:24]
        inbox_key = inbox_key or os.environ.get(planner.inbox_key_env, "").strip()
        if not inbox_key:
            return
        try:
            if client is None:
                async with httpx.AsyncClient(
                    timeout=10.0, follow_redirects=False, trust_env=False
                ) as owned:
                    receipt = await owned.get(
                        planner.inbox_url.rstrip("/") + f"/v1/agent-inbox/{delivery_id}",
                        headers={"Authorization": f"Bearer {inbox_key}"},
                    )
            else:
                receipt = await client.get(
                    planner.inbox_url.rstrip("/") + f"/v1/agent-inbox/{delivery_id}",
                    headers={"Authorization": f"Bearer {inbox_key}"},
                )
        except httpx.HTTPError:
            return
        if receipt.status_code != 200:
            return
        try:
            receipt_payload = receipt.json()
        except ValueError:
            return  # Non-JSON body (proxy/error page); retry next pump.
        state = str(receipt_payload.get("state", ""))
        mapping = {
            "injected": DeliveryState.INJECTED,
            "orphaned": DeliveryState.ORPHANED,
            "card_only": DeliveryState.CARD_ONLY,
        }
        if state in mapping:
            self._store.set_delivery_state(task.task_id, mapping[state])

    async def _deliver_results(self) -> bool:
        """Deliver terminal auto_resume results to the fixed planner inbox."""
        tasks = self._store.auto_resume_ready_tasks()
        if not tasks:
            return False
        for task in tasks:
            if task.delivery_state.value == "persisted":
                await self._poll_receipt(task)
            else:
                await self._deliver_one(task)
        return True

    async def _deliver_one(self, task) -> None:
        import httpx

        planner = self._planner
        if planner is None or not task.reply_capability:
            self._store.set_delivery_state(task.task_id, DeliveryState.FAILED)
            return
        delivery_id = "del_" + hashlib.sha256(
            f"{task.task_id}:{task.remote_revision}".encode("utf-8")
        ).hexdigest()[:24]
        inbox_key = os.environ.get(planner.inbox_key_env, "").strip()
        if not inbox_key:
            self._store.set_delivery_state(task.task_id, DeliveryState.PENDING)
            return
        payload = {
            "delivery_id": delivery_id,
            "reply_capability": task.reply_capability,
            "operation_key": task.operation_key,
            "workflow_id": task.workflow_id,
            "bot_id": task.bot_id,
            "instance_id": task.remote_instance_id or "",
            "task_id": task.task_id,
            "run_id": task.remote_run_id or "",
            "terminal_revision": task.remote_revision,
            "outcome": {"code": task.outcome_code or task.run_state},
            "result_text": task.result_text,
            "result_truncated": task.result_truncated,
        }
        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.post(
                    planner.inbox_url.rstrip("/") + "/v1/agent-inbox",
                    headers={"Authorization": f"Bearer {inbox_key}"},
                    json=payload,
                )
                if response.status_code in (200, 202):
                    self._store.set_delivery_state(
                        task.task_id, DeliveryState.PERSISTED
                    )
                    await self._poll_receipt(task, client=client, inbox_key=inbox_key,
                                             delivery_id=delivery_id)
                    return
                if response.status_code == 410:
                    self._store.set_delivery_state(task.task_id, DeliveryState.ORPHANED)
                    return
                if response.status_code == 409:
                    self._store.set_delivery_state(task.task_id, DeliveryState.FAILED)
                    return
        except httpx.HTTPError:
            pass
        # Retryable: keep pending; the next pump retries the same delivery id.
        self._store.set_delivery_state(task.task_id, DeliveryState.PENDING)

    # ---- observation ----

    async def _observe_accepted(self) -> bool:
        tasks = self._store.tasks_to_observe(limit=32)
        if not tasks:
            return False
        progressed = False
        for task in tasks:
            if task.remote_run_id is None:
                continue
            bot = self._store.get_bot(task.bot_id)
            if bot is None:
                continue
            try:
                client = self._clients.for_bot(bot.endpoint, bot.secret_env)
            except MissingSecretError:
                # Observations resume when the operator restores the secret;
                # other bots must keep flowing.
                continue
            try:
                response = await client.get_run(task.remote_run_id)
            except BotUnreachableError:
                continue
            if response.status != 200 or not isinstance(response.payload, dict):
                if response.status == 404:
                    # The remote lost the run storage; identity check decides.
                    await self._check_identity_or_indeterminate(task, bot, client)
                continue
            payload = response.payload
            instance = str(payload.get("instance_id", ""))
            if bot.instance_id and instance and instance != bot.instance_id:
                await self._indeterminate_identity(task, bot.instance_id, instance)
                progressed = True
                continue
            run_state = str(payload.get("state", ""))
            if self._store.get_task(task.task_id).remote_revision >= int(
                payload.get("revision", 0)
            ):
                progressed |= await self._drive_cancel(task, client)
                continue
            terminal = run_state in TERMINAL_RUN_STATES
            result_text = None
            result_truncated = None
            if terminal and payload.get("result_available"):
                try:
                    result = await client.get_result(task.remote_run_id)
                    if result.status == 200 and isinstance(result.payload, dict):
                        outcome = result.payload.get("outcome") or {}
                        result_text = str(outcome.get("text", ""))
                        # Preserve the remote's typed truncation metadata (S08):
                        # a partial body must never present as a full artifact.
                        result_truncated = bool(outcome.get("truncated", False))
                except BotUnreachableError:
                    result_text = None
            self._store.record_observation(
                task.task_id,
                revision=int(payload.get("revision", 0)),
                run_state=run_state,
                stop_confirmed=bool(payload.get("stop_confirmed", False)),
                effects_uncertain=bool(payload.get("effects_uncertain", False)),
                outcome_code=(
                    str((payload.get("outcome") or {}).get("code"))
                    if payload.get("outcome")
                    else None
                ),
                result_text=result_text,
                result_truncated=result_truncated,
                result_advertised=bool(payload.get("result_available", False)),
            )
            # Unverified effects from the remote isolate the instance here, not
            # only at admission: no new task may start on it until an operator
            # resolves the quarantine.
            if bool(payload.get("effects_uncertain", False)) and bot.instance_id:
                self._store.quarantine_instance(
                    bot.instance_id,
                    f"Task {task.task_id} reported unconfirmed background effects.",
                )
            progressed = True
            progressed |= await self._drive_cancel(task, client)
        return progressed

    async def _fetch_missing_results(self) -> bool:
        """Retry result fetches whose body never landed (F07).

        A terminal run whose result body was not persisted keeps being polled
        here; the observation state is already terminal, so this is a pure read
        until the body settles. A definitive 404 means the remote lost the
        result storage: the task becomes indeterminate and the instance
        quarantines, consistent with the observation 404 path.
        """
        tasks = self._store.tasks_with_unfetched_results(limit=16)
        if not tasks:
            return False
        progressed = False
        for task in tasks:
            if task.remote_run_id is None:
                continue
            bot = self._store.get_bot(task.bot_id)
            if bot is None:
                continue
            try:
                client = self._clients.for_bot(bot.endpoint, bot.secret_env)
            except MissingSecretError:
                continue
            try:
                result = await client.get_result(task.remote_run_id)
            except BotUnreachableError:
                continue  # transient; the same task is retried next pump
            if result.status == 200 and isinstance(result.payload, dict):
                outcome = result.payload.get("outcome") or {}
                self._store.persist_result_body(
                    task.task_id,
                    result_text=str(outcome.get("text", "")),
                    result_truncated=bool(outcome.get("truncated", False)),
                )
                progressed = True
            elif result.status == 404:
                await self._check_identity_or_indeterminate(task, bot, client)
                progressed = True
        return progressed

    async def _drive_cancel(self, task, client: BotRunClient) -> bool:
        current = self._store.get_task(task.task_id)
        if (
            current is None
            or current.cancel_state.value != "requested"
            or current.remote_run_id is None
            or current.run_state in TERMINAL_RUN_STATES
        ):
            return False
        try:
            response = await client.cancel(current.remote_run_id)
        except BotUnreachableError:
            return False
        return response.status in (200, 202)

    async def _check_identity_or_indeterminate(self, task, bot, client: BotRunClient) -> None:
        try:
            caps = await client.capabilities()
        except BotUnreachableError:
            return
        if caps.status == 200 and isinstance(caps.payload, dict):
            instance = str(caps.payload.get("instance_id", ""))
            if bot.instance_id and instance and instance != bot.instance_id:
                await self._indeterminate_identity(task, bot.instance_id, instance)
                return
            # Identity confirmed and the run is genuinely gone: the remote lost
            # storage for an admitted run. The task becomes indeterminate and
            # the instance quarantines; nothing else may run there until an
            # operator reconciles.
            self._store.mark_indeterminate(
                task.task_id,
                "Remote confirmed its identity but lost the run storage (404).",
                quarantine_instance=bot.instance_id,
            )


def task_public_json(task) -> dict:
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "workflow_id": task.workflow_id,
        "bot_id": task.bot_id,
        "operation_key": task.operation_key,
        "dispatch_state": task.dispatch_state.value,
        "run_state": task.run_state,
        "remote_run_id": task.remote_run_id,
        "remote_revision": task.remote_revision,
        "stop_confirmed": task.stop_confirmed,
        "effects_uncertain": task.effects_uncertain,
        "cancel_state": task.cancel_state.value,
        "delivery_state": task.delivery_state.value,
        "outcome_code": task.outcome_code,
        "result_available": task.result_available,
        "result_fetched": task.result_fetched,
        "result_truncated": task.result_truncated,
        "deadline_at": task.deadline_at.isoformat(),
        "created_at": task.created_at.isoformat(),
        "finished_at": None if task.finished_at is None else task.finished_at.isoformat(),
        "last_error": task.last_error,
    }


__all__ = [
    "BACKOFF_SECONDS",
    "ClientFactory",
    "DispatchWorker",
    "task_public_json",
]
