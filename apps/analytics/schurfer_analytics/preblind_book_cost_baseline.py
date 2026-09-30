"""Outcome-blind, pre-2026-09-29 quote-cost reader.

The production read is deliberately disabled until the protocol has been merged
and the separate results PR activates it. No return, PnL, funding, OHLCV, or
trade-decision column is selected.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .outcome_repository import async_database_url

READER_VERSION = "preblind_book_cost_baseline_v1"
WINDOW_START = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 29, tzinfo=UTC)
READ_ENABLED = False
MAX_ROWS = 100_000
FEE_SCENARIOS_BPS = (0.0, 5.5, 10.0)  # each side, hypothetical; not observed fills
SPREAD_BUCKETS_BPS = (5.0, 20.0, 50.0)

PAPER_ROWS = text("""
    SELECT p.paper_id, p.paper_version, p.exchange, p.market_type, p.symbol,
           p.watch_decision_at, p.entry_status, p.position_status,
           p.entry_quote_observed_at, p.exit_quote_observed_at,
           p.entry_exchange_event_at, p.exit_exchange_event_at,
           (r.contract_json ->> 'position_notional_usd')::double precision
               AS requested_notional_usd,
           CASE WHEN p.entry_quote_observed_at < :end
                THEN p.entry_filled_notional_usd END AS entry_filled_notional_usd,
           CASE WHEN p.exit_quote_observed_at < :end
                THEN p.exit_filled_notional_usd END AS exit_filled_notional_usd,
           CASE WHEN p.entry_quote_observed_at < :end
                THEN p.entry_spread_bps END AS entry_spread_bps,
           CASE WHEN p.entry_quote_observed_at < :end
                THEN p.entry_impact_bps END AS entry_impact_bps,
           CASE WHEN p.exit_quote_observed_at < :end
                THEN p.exit_spread_bps END AS exit_spread_bps,
           CASE WHEN p.exit_quote_observed_at < :end
                THEN p.exit_impact_bps END AS exit_impact_bps
    FROM app.momentum_flow_paper_probes p
    JOIN app.momentum_flow_paper_runs r ON r.paper_version = p.paper_version
    WHERE p.watch_decision_at >= :start AND p.watch_decision_at < :end
      AND p.updated_at < :end
    ORDER BY p.paper_version, p.watch_decision_at, p.paper_id
    LIMIT :limit
""")

SOURCE_ROWS = text("""
    SELECT c.id AS capture_id, c.capture_version, c.source_exchange, c.base,
           c.source_first_observed_at, t.id AS target_id,
           t.target_exchange, t.status AS target_status, t.observed_at,
           t.requested_notional_usd,
           t.liquidity ->> 'spread_bps' AS spread_bps,
           t.liquidity ->> 'ask_impact_bps' AS ask_impact_bps,
           t.liquidity ->> 'bid_impact_bps' AS bid_impact_bps,
           t.liquidity ->> 'ask_filled_notional_usd' AS ask_filled_notional_usd,
           t.liquidity ->> 'bid_filled_notional_usd' AS bid_filled_notional_usd,
           t.liquidity -> 'quote_timing' ->> 'book_age_ms' AS book_age_ms
    FROM app.source_lead_captures c
    LEFT JOIN app.source_lead_target_observations t
      ON t.capture_id = c.id AND t.observed_at < :end AND t.updated_at < :end
    WHERE c.source_first_observed_at >= :start
      AND c.source_first_observed_at < :end
      AND c.updated_at < :end
    ORDER BY c.id, t.target_exchange
    LIMIT :limit
""")

EXCLUDED_COUNTS = text("""
    SELECT
      (SELECT count(*) FROM app.momentum_flow_paper_probes p
       WHERE p.watch_decision_at >= :start AND p.watch_decision_at < :end
         AND p.updated_at >= :end) AS paper_updated_after_cutoff,
      (SELECT count(*) FROM app.source_lead_captures c
       WHERE c.source_first_observed_at >= :start
         AND c.source_first_observed_at < :end
         AND c.updated_at >= :end) AS source_captures_updated_after_cutoff,
      (SELECT count(*) FROM app.source_lead_captures c
       JOIN app.source_lead_target_observations t ON t.capture_id = c.id
       WHERE c.source_first_observed_at >= :start
         AND c.source_first_observed_at < :end
         AND c.updated_at < :end
         AND (t.observed_at >= :end OR t.updated_at >= :end)
      ) AS source_targets_outside_preblind_snapshot
""")


def finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def spread_bucket(spread_bps: float) -> str:
    for bound in SPREAD_BUCKETS_BPS:
        if spread_bps < bound:
            return f"lt_{bound:g}_bps"
    return "gte_50_bps"


def quote_age_quality(age_ms: float | None) -> str:
    if age_ms is None or not math.isfinite(age_ms):
        return "unknown"
    return "fresh" if -1000 <= age_ms <= 2000 else "outside_2s"


def paper_quote_quality(row: dict[str, Any]) -> str:
    ages: list[float] = []
    for prefix in ("entry", "exit"):
        observed = row[f"{prefix}_quote_observed_at"]
        event = row[f"{prefix}_exchange_event_at"]
        if observed is None or event is None:
            return "unknown"
        ages.append((observed - event).total_seconds() * 1000)
    if all(quote_age_quality(age) == "fresh" for age in ages):
        return "fresh"
    return "outside_2s"


def break_even_mid_move_bps(
    entry_ask_impact_bps: float, exit_bid_impact_bps: float, fee_per_side_bps: float
) -> float:
    """Required midpoint rise for fixed observed impact fractions and fixed quantity.

    Ask impact and bid impact already include the respective half-spreads.
    This is a quote-cost threshold, not an expected return or an executed fill.
    """
    if (
        any(
            not math.isfinite(value) or value < 0
            for value in (entry_ask_impact_bps, exit_bid_impact_bps, fee_per_side_bps)
        )
        or exit_bid_impact_bps >= 10_000
        or fee_per_side_bps >= 10_000
    ):
        raise ValueError("invalid impact or fee bps")
    entry_multiplier = (1 + entry_ask_impact_bps / 10_000) * (1 + fee_per_side_bps / 10_000)
    exit_multiplier = (1 - exit_bid_impact_bps / 10_000) * (1 - fee_per_side_bps / 10_000)
    return (entry_multiplier / exit_multiplier - 1) * 10_000


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)

    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)

    return {
        "n": len(ordered),
        "mean": fmean(ordered) if ordered else None,
        "p50": percentile(0.5),
        "p90": percentile(0.9),
        "p99": percentile(0.99),
        "max": ordered[-1] if ordered else None,
    }


def _eligible_costs(
    *,
    entry_at: datetime | None,
    exit_at: datetime | None,
    entry_spread: Any,
    entry_impact: Any,
    exit_impact: Any,
    entry_filled: Any,
    exit_filled: Any,
    requested: Any,
) -> tuple[float, float, float] | None:
    if entry_at is None or exit_at is None or not (WINDOW_START <= entry_at < WINDOW_END):
        return None
    if not (WINDOW_START <= exit_at < WINDOW_END):
        return None
    spread = finite_nonnegative(entry_spread)
    ask_impact = finite_nonnegative(entry_impact)
    bid_impact = finite_nonnegative(exit_impact)
    buy_filled = finite_nonnegative(entry_filled)
    sell_filled = finite_nonnegative(exit_filled)
    notional = finite_nonnegative(requested)
    if (
        spread is None
        or ask_impact is None
        or bid_impact is None
        or buy_filled is None
        or sell_filled is None
        or notional is None
    ):
        return None
    if notional <= 0 or buy_filled + 0.01 < notional or sell_filled + 0.01 < notional:
        return None
    if bid_impact >= 10_000:
        return None
    return spread, ask_impact, bid_impact


def summarize_cost_rows(
    paper_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    excluded_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Keep paper versions and source/target venues separate; never pool them."""
    counts: Counter[str] = Counter()
    buckets: dict[tuple[str, str, str, str, str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in paper_rows:
        notional = finite_nonnegative(row["requested_notional_usd"])
        paper_group = (
            "paper",
            str(row["paper_version"]),
            str(row["exchange"]),
            f"${notional:g}" if notional is not None and notional > 0 else "unknown",
        )
        counts[f"paper:{row['paper_version']}:all"] += 1
        if row["entry_status"] != "opened":
            counts[f"paper:{row['paper_version']}:entry_not_open"] += 1
            continue
        if row["position_status"] != "closed":
            counts[f"paper:{row['paper_version']}:position_not_closed"] += 1
            continue
        if row["entry_quote_observed_at"] is None or row["exit_quote_observed_at"] is None:
            counts[f"paper:{row['paper_version']}:missing_quote_time"] += 1
            continue
        if row["entry_quote_observed_at"] >= WINDOW_END:
            counts[f"paper:{row['paper_version']}:entry_after_cutoff"] += 1
            continue
        if row["exit_quote_observed_at"] >= WINDOW_END:
            counts[f"paper:{row['paper_version']}:exit_after_cutoff"] += 1
            continue
        costs = _eligible_costs(
            entry_at=row["entry_quote_observed_at"],
            exit_at=row["exit_quote_observed_at"],
            entry_spread=row["entry_spread_bps"],
            entry_impact=row["entry_impact_bps"],
            exit_impact=row["exit_impact_bps"],
            entry_filled=row["entry_filled_notional_usd"],
            exit_filled=row["exit_filled_notional_usd"],
            requested=row["requested_notional_usd"],
        )
        if costs is None:
            counts[f"paper:{row['paper_version']}:invalid_or_short_depth"] += 1
            continue
        _add_costs(
            buckets[
                (
                    paper_group[0],
                    paper_group[1],
                    paper_group[2],
                    paper_group[3],
                    spread_bucket(costs[0]),
                    paper_quote_quality(row),
                )
            ],
            costs,
        )

    seen_captures: set[tuple[str, int]] = set()
    for row in source_rows:
        capture_version = str(row["capture_version"])
        capture_key = (capture_version, int(row["capture_id"]))
        if capture_key not in seen_captures:
            counts[f"source:{capture_version}:captures"] += 1
            seen_captures.add(capture_key)
        counts[f"source:{capture_version}:all_rows"] += 1
        if row["target_id"] is None:
            counts[f"source:{capture_version}:no_pre_cutoff_target"] += 1
            continue
        if row["target_status"] != "sampled":
            counts[f"source:{capture_version}:status:{row['target_status']}"] += 1
            continue
        # The two impacts are from one capture-time book. This represents an
        # immediate hypothetical crossing, not an observed 30-minute exit.
        costs = _eligible_costs(
            entry_at=row["observed_at"],
            exit_at=row["observed_at"],
            entry_spread=row["spread_bps"],
            entry_impact=row["ask_impact_bps"],
            exit_impact=row["bid_impact_bps"],
            entry_filled=row["ask_filled_notional_usd"],
            exit_filled=row["bid_filled_notional_usd"],
            requested=row["requested_notional_usd"],
        )
        if costs is None:
            counts[f"source:{capture_version}:invalid_or_short_depth"] += 1
            continue
        source_group = (
            "source_immediate_cross",
            capture_version,
            f"{row['source_exchange']}->{row['target_exchange']}",
            f"${float(row['requested_notional_usd']):g}",
            spread_bucket(costs[0]),
            quote_age_quality(_finite(row["book_age_ms"])),
        )
        _add_costs(buckets[source_group], costs)

    return {
        "reader_version": READER_VERSION,
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "fee_scenarios_bps_per_side": list(FEE_SCENARIOS_BPS),
        "excluded_from_preblind_snapshot": dict(sorted((excluded_counts or {}).items())),
        "counts": dict(sorted(counts.items())),
        "groups": [
            {
                "population": population,
                "version": version,
                "venue": venue,
                "requested_notional": notional,
                "entry_spread_bucket": bucket,
                "quote_age_quality": quality,
                "entry_spread_bps": _distribution(values["entry_spread_bps"]),
                "entry_ask_impact_bps": _distribution(values["entry_ask_impact_bps"]),
                "exit_bid_impact_bps": _distribution(values["exit_bid_impact_bps"]),
                "break_even_mid_move_bps": {
                    f"fee_{fee:g}": _distribution(values[f"fee_{fee:g}"])
                    for fee in FEE_SCENARIOS_BPS
                },
            }
            for (population, version, venue, notional, bucket, quality), values in sorted(
                buckets.items()
            )
        ],
    }


def _add_costs(bucket: dict[str, list[float]], costs: tuple[float, float, float]) -> None:
    spread, ask_impact, bid_impact = costs
    bucket["entry_spread_bps"].append(spread)
    bucket["entry_ask_impact_bps"].append(ask_impact)
    bucket["exit_bid_impact_bps"].append(bid_impact)
    for fee in FEE_SCENARIOS_BPS:
        bucket[f"fee_{fee:g}"].append(break_even_mid_move_bps(ask_impact, bid_impact, fee))


async def load_registered_rows(
    db_url: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Read a single fixed pre-blind snapshot only after the separate activation PR."""
    if not READ_ENABLED:
        raise RuntimeError("preblind book-cost read is not registered for execution")
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            async with connection.begin():
                params = {"start": WINDOW_START, "end": WINDOW_END, "limit": MAX_ROWS + 1}
                paper = [
                    dict(row) for row in (await connection.execute(PAPER_ROWS, params)).mappings()
                ]
                source = [
                    dict(row) for row in (await connection.execute(SOURCE_ROWS, params)).mappings()
                ]
                excluded = dict(
                    (await connection.execute(EXCLUDED_COUNTS, params)).mappings().one()
                )
                if len(paper) > MAX_ROWS or len(source) > MAX_ROWS:
                    raise ValueError("book-cost row limit exceeded")
                return paper, source, {key: int(value) for key, value in excluded.items()}
    finally:
        await engine.dispose()
