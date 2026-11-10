"""Coordinator service assembly: store, worker, HTTP listener, lifecycle, import."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from .config import CoordinatorConfig, load_coordinator_config
from .models import DeliveryMode
from .store import AdmissionError, CoordinatorStore
from .worker import ClientFactory, DispatchWorker

log = logging.getLogger("nanobot_teams.coordinator.service")


class CoordinatorService:
    def __init__(self, config: CoordinatorConfig, store: CoordinatorStore) -> None:
        self.config = config
        self.store = store
        self.clients = ClientFactory()  # secrets resolve live from the environment

    @classmethod
    def from_config_path(cls, path: str | Path) -> "CoordinatorService":
        config = load_coordinator_config(path)
        store = CoordinatorStore(config.control_dir / "coordinator.sqlite3")
        # A hard-killed predecessor's writer lock can take a moment to clear.
        store.open(lock_wait_s=10.0)
        return cls(config, store)

    def close(self) -> None:
        self.store.close()

    async def run_forever(
        self, app_builder: Callable[[CoordinatorStore, CoordinatorConfig, dict[str, str]], Any]
    ) -> int:
        from aiohttp import web

        tokens = self.config.resolve_tokens()
        app = app_builder(self.store, self.config, tokens)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        try:
            await site.start()
        except OSError as exc:
            log.error("cannot listen on %s:%s: %s", self.config.host, self.config.port, exc)
            await runner.cleanup()
            return 1
        worker = DispatchWorker(self.store, self.clients, planner=self.config.planner)
        worker_task = asyncio.create_task(worker.run_loop())
        log.info(
            "coordinator listening on http://%s:%s (control dir %s)",
            self.config.host,
            self.config.port,
            self.config.control_dir,
        )
        try:
            await asyncio.Event().wait()  # until cancelled by signal
        except asyncio.CancelledError:
            pass
        finally:
            worker.stop()
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            await self.clients.aclose()
            await runner.cleanup()
        return 0

    # ---- 0.4 migration import ----

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        """Canonical form for matching legacy 0.4 endpoints to registered bots."""
        from urllib.parse import urlsplit

        raw = endpoint.strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = f"http://{raw}"
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
        path = parts.path.rstrip("/")
        return f"{host}:{port}{path}" if port else f"{host}{path}"

    def _instance_for_endpoint(self, endpoint: str) -> str | None:
        """Map a legacy endpoint to a registered bot's instance identity."""
        normalized = self._normalize_endpoint(endpoint)
        if not normalized:
            return None
        for bot in self.store.list_bots():
            if bot.instance_id and self._normalize_endpoint(bot.endpoint) == normalized:
                return bot.instance_id
        return None

    def import_04_ledger(
        self,
        *,
        teams_config: dict[str, Any],
        ledger_path: Path,
        caller_id: str,
        declared_budgets: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Idempotent one-shot import of a 0.4 teams.json + ledger.

        Preserves caller usage as an enforced admission guard, isolates unknown
        outcomes on the registered instance the endpoint maps to (falling back
        to a legacy key when no bot matches), and never releases unknown
        reservations. Re-importing the same fingerprint is a no-op. New
        admissions for the caller stay blocked until an operator confirms fresh
        quota via ``confirm_caller_quota``.
        """
        from ..ledger import Ledger, LedgerError

        fingerprint_source = json.dumps(
            {"team": teams_config.get("team_id"), "caller": caller_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(
            fingerprint_source + str(ledger_path.resolve()).encode("utf-8")
        ).hexdigest()
        if fingerprint in self.store.migration_fingerprints("teams-0.4"):
            return {"imported": False, "reason": "already imported", "fingerprint": fingerprint}
        try:
            ledger = Ledger(ledger_path, read_only=True)
        except LedgerError as exc:
            raise AdmissionError("ledger_unreadable", str(exc)) from exc
        imported = {
            "tasks": 0,
            "usage_delegations": 0,
            "usage_result_bytes": 0,
            "unknown_isolated": 0,
            "budgets": {},
        }
        try:
            for record in ledger.list_tasks(limit=500):
                imported["tasks"] += 1
                usage = ledger.usage_for(record.caller)
                if usage:
                    imported["usage_delegations"] = max(
                        imported["usage_delegations"], usage[0]
                    )
                    imported["usage_result_bytes"] = max(
                        imported["usage_result_bytes"], usage[1]
                    )
                if record.status in ("outcome_unknown", "timed_out") or record.blocked:
                    endpoint = record.endpoint or "unknown-endpoint"
                    instance = self._instance_for_endpoint(endpoint)
                    if instance is not None:
                        # The unknown maps to a live registered instance: its
                        # admission and dispatch guard must actually engage.
                        self.store.quarantine_instance(
                            instance,
                            f"0.4 unknown outcome for task {record.task_id} on "
                            f"{endpoint}; verify before reuse",
                        )
                    else:
                        self.store.quarantine_instance(
                            f"legacy:{endpoint}",
                            f"0.4 unknown outcome for task {record.task_id}; verify "
                            "before reuse",
                        )
                    imported["unknown_isolated"] += 1
        finally:
            ledger.close()
        if imported["usage_delegations"] or imported["usage_result_bytes"]:
            self.store.record_caller_usage(
                caller_id,
                delegations_used=imported["usage_delegations"],
                result_bytes_used=imported["usage_result_bytes"],
            )
        if declared_budgets:
            imported["budgets"] = declared_budgets
            budgets_for_caller = declared_budgets.get(caller_id)
            if isinstance(budgets_for_caller, dict):
                self.store.confirm_caller_quota(
                    caller_id,
                    delegations_limit=int(budgets_for_caller.get("delegations", 0)),
                    result_bytes_limit=int(budgets_for_caller.get("result_bytes", 0)),
                    actor="migration-import",
                )
        details = {
            "caller": caller_id,
            "team": teams_config.get("team_id"),
            "counts": imported,
            "declared_new_quota_confirmed": bool(declared_budgets),
        }
        self.store.record_migration("teams-0.4", fingerprint, details)
        log.info(
            "imported 0.4 ledger: %s tasks, usage %s delegations / %s bytes, %s unknown isolated",
            imported["tasks"],
            imported["usage_delegations"],
            imported["usage_result_bytes"],
            imported["unknown_isolated"],
        )
        return {"imported": True, "fingerprint": fingerprint, **imported}


def build_service(config: CoordinatorConfig) -> CoordinatorService:
    store = CoordinatorStore(config.control_dir / "coordinator.sqlite3")
    store.open()
    return CoordinatorService(config, store)


__all__ = ["CoordinatorService", "build_service", "DeliveryMode"]
