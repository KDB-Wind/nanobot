"""Read-only views for operators: the graph, budget usage, and the audit trail.

Everything here renders from the registry and the ledger files the operator
names. Nothing writes, nothing talks to Nanobot internals.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .config import Budget, TeamConfig
from .ledger import Ledger


def render_graph(config: TeamConfig) -> str:
    lines: list[str] = []
    for bot in config.bots:
        suffix = f"  ({', '.join(bot.capabilities)})" if bot.capabilities else ""
        lines.append(f"{bot.id}  {bot.role}{suffix}")
        for target in config.bots:
            if bot.id in target.allowed_callers:
                lines.append(f"  -> {target.id}  {target.role}")
    lines.append("")
    lines.append("```mermaid")
    lines.append("flowchart LR")
    for bot in config.bots:
        safe = bot.id.replace(".", "_").replace("-", "_")
        lines.append(f"  {safe}[\"{bot.id}\"]")
    for target in config.bots:
        for caller in target.allowed_callers:
            src = caller.replace(".", "_").replace("-", "_")
            dst = target.id.replace(".", "_").replace("-", "_")
            lines.append(f"  {src} --> {dst}")
    lines.append("```")
    return "\n".join(lines)


def render_budget(config: TeamConfig, ledger_paths: Sequence[Path]) -> str:
    rows: list[str] = []
    header = f"{'bot':<16} {'delegations':<16} {'result_bytes':<22} {'chain_seconds':<18}"
    rows.append(header)
    usage = _combined_usage(ledger_paths)
    for bot in config.bots:
        budget = config.budgets.get(bot.id)
        count, result_bytes, seconds = usage.get(bot.id, (0, 0, 0.0))
        rows.append(
            f"{bot.id:<16} {_pair(count, budget.delegations if budget else None, '')}"
            f" {_pair(result_bytes, budget.result_bytes if budget else None, '')}"
            f" {_pair(round(seconds, 1), budget.chain_seconds if budget else None, 's')}"
        )
    return "\n".join(rows)


def render_audit(
    ledger_paths: Sequence[Path],
    *,
    limit: int = 50,
    target: str | None = None,
    status: str | None = None,
) -> str:
    rows: list[tuple[str, object]] = []
    for path in ledger_paths:
        ledger = Ledger(path)
        try:
            for record in ledger.list_tasks(limit=limit, target=target, status=status):
                rows.append((str(path), record))
        finally:
            ledger.close()
    rows.sort(key=lambda item: item[1].started_at, reverse=True)
    bounded = rows[: max(1, min(limit, 500))]
    lines = [
        f"{record.started_at}  {record.caller} -> {record.target}  hop={record.hop}  "
        f"{record.status}{'' if record.error_code is None else ' (' + record.error_code + ')'}  "
        f"bytes req={record.request_bytes} resp={record.response_bytes} "
        f"result={record.result_bytes}  {record.duration_ms}ms  [{path}]"
        for path, record in bounded
    ]
    counts: dict[str, int] = {}
    for _, record in bounded:
        counts[record.status] = counts.get(record.status, 0) + 1
    summary = "  ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    lines.append(f"total={len(bounded)}  {summary}")
    return "\n".join(lines)


def _combined_usage(ledger_paths: Sequence[Path]) -> dict[str, tuple[int, int, float]]:
    usage: dict[str, tuple[int, int, float]] = {}
    callers: set[str] = set()
    for path in ledger_paths:
        ledger = Ledger(path)
        try:
            ledger_callers = ledger.list_tasks(limit=500)
        finally:
            ledger.close()
        for record in ledger_callers:
            callers.add(record.caller)
    for caller in callers:
        total = [0, 0, 0.0]
        for path in ledger_paths:
            ledger = Ledger(path)
            try:
                count, result_bytes, seconds = ledger.usage_for(caller)
            finally:
                ledger.close()
            total[0] += count
            total[1] += result_bytes
            total[2] += seconds
        usage[caller] = (total[0], total[1], total[2])
    return usage


def _pair(used: int | float, limit: int | float | None, unit: str) -> str:
    if limit is None:
        return f"{used}{unit}/-"
    state = "ok" if used < limit else "OVER"
    return f"{used}{unit}/{limit}{unit} {state}"


def budget_hint(budget: Budget) -> str:
    parts = []
    if budget.delegations is not None:
        parts.append(f"{budget.delegations} delegations")
    if budget.result_bytes is not None:
        parts.append(f"{budget.result_bytes} result bytes")
    if budget.chain_seconds is not None:
        parts.append(f"{budget.chain_seconds} seconds")
    return ", ".join(parts) or "unlimited"
