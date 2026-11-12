"""Read-only views for operators: the graph, budget usage, and the audit trail.

Everything here renders from the registry and the ledger files the operator
names. Nothing writes, nothing talks to Nanobot internals.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .config import TeamConfig
from .ledger import Ledger, LedgerError

_MERMAQ_SAFE = str.maketrans({".": "_", "-": "_"})


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
        lines.append(f"  {bot.id.translate(_MERMAQ_SAFE)}[\"{bot.id}\"]")
    for target in config.bots:
        for caller in target.allowed_callers:
            src = caller.translate(_MERMAQ_SAFE)
            dst = target.id.translate(_MERMAQ_SAFE)
            lines.append(f"  {src} --> {dst}")
    lines.append("```")
    return "\n".join(lines)


def render_budget(config: TeamConfig, ledger_paths: Sequence[Path]) -> str:
    if not ledger_paths:
        return "unavailable: no control ledgers found; usage is unverified"
    rows: list[str] = []
    header = f"{'bot':<16} {'delegations':<16} {'result_bytes':<22} {'chain_seconds':<18}"
    rows.append(header)
    missing = [str(path) for path in ledger_paths if not Path(path).exists()]
    if missing:
        return "missing: " + ", ".join(missing) + "; usage is unverified"
    try:
        usage = _combined_usage(ledger_paths, config.team_id)
    except LedgerError:
        return "unavailable: ledger cannot be read or requires migration; usage is unverified"
    for bot in config.bots:
        budget = config.budgets.get(bot.id)
        count, result_bytes, seconds = usage.get(bot.id, (0, 0, 0.0))
        rows.append(
            f"{bot.id:<16} {_pair(count, budget.delegations if budget else None, '')}"
            f" {_pair(result_bytes, budget.result_bytes if budget else None, '')}"
            f" {_pair(round(seconds, 1), budget.chain_seconds if budget else None, 's')}"
        )
    if missing:
        rows.append("missing: " + ", ".join(missing))
    return "\n".join(rows)


def render_audit(
    ledger_paths: Sequence[Path],
    *,
    limit: int = 50,
    target: str | None = None,
    status: str | None = None,
) -> str:
    rows: list[tuple[str, object]] = []
    missing = [str(path) for path in ledger_paths if not Path(path).exists()]
    for path in ledger_paths:
        if not Path(path).exists():
            continue
        try:
            ledger = Ledger(path, read_only=True)
        except LedgerError:
            return "unavailable: ledger cannot be opened; history is unverified"
        try:
            for record in ledger.list_tasks(limit=limit, target=target, status=status):
                rows.append((str(path), record))
        except LedgerError:
            return "unavailable: ledger cannot be read or requires migration; history is unverified"
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
    if missing:
        lines.append("missing: " + ", ".join(missing))
    lines.append(f"total={len(bounded)}  {summary}")
    return "\n".join(lines)


def _combined_usage(ledger_paths: Sequence[Path], team_id: str) -> dict[str, tuple[int, int, float]]:
    usage: dict[str, tuple[int, int, float]] = {}
    callers: set[str] = set()
    existing = [path for path in ledger_paths if Path(path).exists()]
    for path in existing:
        ledger = Ledger(path, read_only=True)
        try:
            identity = ledger.identity()
            if identity is None or identity[0] != team_id:
                raise LedgerError("Ledger belongs to another team or is unverified.")
            ledger_callers = ledger.callers()
        finally:
            ledger.close()
        callers.update(ledger_callers)
    for caller in callers:
        total = [0, 0, 0.0]
        for path in existing:
            ledger = Ledger(path, read_only=True)
            try:
                totals = ledger.usage_for(caller)
                if totals is None:
                    raise LedgerError("Budget usage unavailable.")
                count, result_bytes, seconds = totals
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
