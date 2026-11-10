"""Per-bot cache hit-rate reader (PEER_COMMS_GOAL.md §5).

Opens each bot's own ``llm_usage.sqlite3`` strictly read-only (URI mode=ro
plus ``PRAGMA query_only=ON``) and computes, per model and window:

    observed_input   = SUM(input_tokens)  over calls with cache_read_tokens NOT NULL
    cache_read_rate  = SUM(cache_read_tokens) / observed_input
    coverage         = observed_input / SUM(input_tokens)

The denominator is the reported-cache input, never total tokens and never
request counts; calls without cache fields stay out of it. Missing data is
``None`` — the rate is never zero-filled. A missing database, a missing
table, or an unknown schema version returns ``available=False`` with the
reason; nothing is guessed. Only token counts are read; no credentials and
no prompt or reply content exist in this table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

WINDOW_DELTAS = {"7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
SUPPORTED_SCHEMA_VERSION = 1
DAY_MS = 86_400_000


@dataclass(frozen=True)
class ModelUsage:
    provider: str
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    observed_input_tokens: int
    cache_read_rate: float | None
    coverage: float | None


@dataclass(frozen=True)
class BotUsage:
    bot_id: str
    display_name: str
    window: str
    available: bool
    reason: str = ""
    totals: ModelUsage | None = None
    models: tuple[ModelUsage, ...] = field(default_factory=tuple)


def _empty_model(provider: str, model: str) -> ModelUsage:
    return ModelUsage(provider, model, 0, 0, 0, 0, 0, 0, None, None)


def _model_usage(provider, model, requests, input_tokens, output_tokens,
                 cache_read, cache_write, observed) -> ModelUsage:
    return ModelUsage(
        provider=provider,
        model=model,
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        observed_input_tokens=observed,
        cache_read_rate=(cache_read / observed) if observed > 0 else None,
        coverage=(observed / input_tokens) if input_tokens > 0 else None,
    )


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.resolve().as_posix().replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_bot_usage(
    bot_id: str,
    display_name: str,
    homes_root: Path,
    window: str = "30d",
    *,
    now_ms: int | None = None,
) -> BotUsage:
    if window not in WINDOW_DELTAS:
        raise ValueError(f"window must be one of {sorted(WINDOW_DELTAS)}")
    db_path = homes_root / bot_id / "llm_usage.sqlite3"
    if not db_path.exists():
        return BotUsage(bot_id, display_name, window, False,
                        reason="usage database missing")
    try:
        conn = _open_read_only(db_path)
    except sqlite3.Error as exc:
        return BotUsage(bot_id, display_name, window, False,
                        reason=f"usage database unreadable: {exc}")
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SUPPORTED_SCHEMA_VERSION:
            return BotUsage(
                bot_id, display_name, window, False,
                reason=f"unknown usage schema version {version}",
            )
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_calls'"
        ).fetchone()
        if table is None:
            return BotUsage(bot_id, display_name, window, False,
                            reason="llm_calls table missing")
        params: tuple[int, ...] = ()
        where = ""
        delta = WINDOW_DELTAS[window]
        if delta is not None:
            cutoff = (now_ms if now_ms is not None
                      else _now_epoch_ms()) - int(delta.total_seconds() * 1000)
            where = " WHERE started_at_ms>=?"
            params = (cutoff,)
        rows = conn.execute(
            "SELECT provider, model, COUNT(*),"
            " COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
            " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0),"
            " COALESCE(SUM(CASE WHEN cache_read_tokens IS NOT NULL"
            " THEN input_tokens ELSE 0 END),0)"
            " FROM llm_calls" + where +
            " GROUP BY provider, model ORDER BY model",
            params,
        ).fetchall()
    except sqlite3.Error as exc:
        return BotUsage(bot_id, display_name, window, False,
                        reason=f"usage query failed: {exc}")
    finally:
        conn.close()
    models = tuple(
        _model_usage(*r) for r in rows
    )
    if models:
        totals = _model_usage(
            "", "(all)",
            sum(m.requests for m in models),
            sum(m.input_tokens for m in models),
            sum(m.output_tokens for m in models),
            sum(m.cache_read_tokens for m in models),
            sum(m.cache_write_tokens for m in models),
            sum(m.observed_input_tokens for m in models),
        )
    else:
        totals = _empty_model("", "(all)")
    return BotUsage(bot_id, display_name, window, True, "", totals, models)


def _now_epoch_ms() -> int:
    import time

    return int(time.time() * 1000)


def bot_usage_json(usage: BotUsage) -> dict:
    def model_json(m: ModelUsage) -> dict:
        return {
            "provider": m.provider,
            "model": m.model,
            "requests": m.requests,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "cache_read_tokens": m.cache_read_tokens,
            "cache_write_tokens": m.cache_write_tokens,
            "observed_input_tokens": m.observed_input_tokens,
            "cache_read_rate": m.cache_read_rate,
            "coverage": m.coverage,
        }

    payload: dict = {
        "bot_id": usage.bot_id,
        "display_name": usage.display_name,
        "window": usage.window,
        "available": usage.available,
    }
    if not usage.available:
        payload["reason"] = usage.reason
        return payload
    assert usage.totals is not None
    payload["totals"] = model_json(usage.totals)
    payload["models"] = [model_json(m) for m in usage.models]
    return payload
